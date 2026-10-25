"""Larry target-weight strategy.

Larry generates an expiring target-weight JSON file. This strategy is the only
Krabs-side adapter: it never computes Larry's signal and never gives Larry broker
access. It applies the most recent target row at the start of a trading day.

Expected file shapes::

    {"as_of": "2026-10-23", "targets": {"BIL": 0.5}}

or a historical file::

    {"rows": [
      {"as_of": "2026-10-22", "targets": {"BIL": 0.5}},
      {"as_of": "2026-10-23", "targets": {"SSO": 0.5}}
    ]}

Weights are per-position fractions of current equity. Missing or stale targets
fail closed by flattening the managed book.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from ...events import Bar
from ...strategy.base import Strategy, StrategyContext
from ..registry import register


@register
class LarryTargets(Strategy):
    id = "larry"
    description = "Apply Larry's expiring target weights from a local file or HTTP endpoint."
    defaults = {
        "source": "",
        "format": "json",  # json | csv
        "allocation": 1.0,
        "max_age_days": 5,
        "drift_band": 0.01,
        "timeout_s": 10.0,
        "fail_closed": True,
        "targets": {},
    }

    def __init__(self, **params: object) -> None:
        super().__init__(**params)
        self.source = str(self.p("source") or "").strip()
        self.static_targets = {
            str(symbol).upper(): float(weight)
            for symbol, weight in (self.p("targets") or {}).items()
        }
        if not self.source and not self.static_targets:
            raise ValueError("source or static targets are required")
        if not 0.0 < float(self.p("allocation")) <= 1.0:
            raise ValueError("allocation must be in (0, 1]")
        if int(self.p("max_age_days")) < 0:
            raise ValueError("max_age_days must be >= 0")
        if float(self.p("drift_band")) < 0:
            raise ValueError("drift_band must be >= 0")
        self.log = logging.getLogger("krabs.strategy.larry")
        self._rows: list[tuple[date, dict[str, float]]] = []
        self._last_targets: dict[str, float] = {}
        self._last_as_of: date | None = None
        self._last_applied_day: date | None = None
        self._load_error: str = ""

    # -- lifecycle -------------------------------------------------------
    def on_start(self, ctx: StrategyContext) -> None:
        self._rows = []
        self._last_targets = {}
        self._last_as_of = None
        self._last_applied_day = None
        self._load_error = ""
        try:
            self._rows = self._load_rows()
        except Exception as exc:  # missing file / bad JSON must not crash the runtime
            self._load_error = str(exc)
            self.log.error("could not load Larry targets: %s", exc)
            return
        self.log.info("loaded %d Larry target row(s)", len(self._rows))

    def on_day_start(self, ctx: StrategyContext, day: date) -> None:
        if self._last_applied_day == day:
            return
        if not self.static_targets:
            # Larry writes a fresh file after each close; the Krabs runtime is
            # long-lived, so reload before the first bar of each day.
            try:
                self._rows = self._load_rows()
                self._load_error = ""
            except Exception as exc:
                self._load_error = str(exc)
                self.log.error("could not reload Larry targets: %s", exc)
                self._rows = []
        targets = self._targets_for(day)
        if targets is None:
            if bool(self.p("fail_closed")):
                self.log.warning("no fresh Larry targets for %s; flattening", day)
                ctx.flatten(reason="larry missing/stale targets")
                self._last_targets = {}
                self._last_applied_day = day
            return
        if ctx.has_open_order():
            self.log.info("skipping Larry apply for %s: open orders", day)
            return
        self._apply(ctx, targets, day)
        self._last_applied_day = day

    # -- loading ---------------------------------------------------------
    def _load_rows(self) -> list[tuple[date, dict[str, float]]]:
        if self.static_targets:
            return [(datetime.now(timezone.utc).date(), self.static_targets)]

        raw = self._read_source(self.source)
        fmt = str(self.p("format")).lower()
        if fmt == "json":
            payload = json.loads(raw)
            rows: list[tuple[date, dict[str, float]]] = []
            if isinstance(payload, dict) and "targets" in payload:
                rows.append((_parse_day(payload.get("as_of") or payload.get("date")), _weights(payload["targets"])))
            elif isinstance(payload, dict):
                for item in payload.get("rows", []):
                    rows.append(
                        (
                            _parse_day(item.get("as_of") or item.get("date")),
                            _weights(item.get("targets") or {}),
                        )
                    )
            elif isinstance(payload, list):
                for item in payload:
                    rows.append(
                        (
                            _parse_day(item.get("as_of") or item.get("date")),
                            _weights(item.get("targets") or {}),
                        )
                    )
            else:
                raise ValueError("unsupported Larry JSON shape")
            return sorted(rows)
        if fmt == "csv":
            reader = csv.DictReader(raw.splitlines())
            merged: dict[date, dict[str, float]] = {}
            for row in reader:
                day = _parse_day(row.get("as_of") or row.get("date"))
                symbol = str(row.get("symbol") or "").upper()
                weight = row.get("weight") or row.get("target")
                if not symbol or weight in (None, ""):
                    continue
                merged.setdefault(day, {})[symbol] = float(weight)
            return sorted(merged.items())
        raise ValueError(f"unsupported Larry format: {fmt}")

    def _read_source(self, source: str) -> str:
        if source.startswith(("http://", "https://")):
            request = Request(
                source,
                headers={"Accept": "application/json,text/csv", "User-Agent": "krabs-larry/0.1"},
            )
            with urlopen(request, timeout=float(self.p("timeout_s"))) as response:  # noqa: S310
                return response.read().decode("utf-8")
        if source.startswith("file://"):
            source = source[7:]
        return Path(source).expanduser().read_text(encoding="utf-8")

    def _targets_for(self, day: date) -> dict[str, float] | None:
        eligible = [row for row in self._rows if row[0] <= day]
        if not eligible:
            return None
        as_of, targets = eligible[-1]
        max_age = int(self.p("max_age_days"))
        if max_age and (day - as_of).days > max_age:
            return None
        self._last_as_of = as_of
        return targets

    # -- execution -------------------------------------------------------
    def _apply(self, ctx: StrategyContext, targets: dict[str, float], day: date) -> None:
        allocation = float(self.p("allocation"))
        gross = sum(abs(weight) for weight in targets.values())
        scale = min(1.0, allocation / gross) if gross > 0 else 1.0
        weights = {
            symbol.upper(): weight * scale
            for symbol, weight in targets.items()
            if abs(weight) > 1e-12
        }
        if weights == self._last_targets:
            return

        drift_band = float(self.p("drift_band"))
        # Sell/close first so a rotation does not depend on an unfilled buy.
        for symbol in list(ctx.positions()):
            if symbol not in weights and not ctx.position(symbol).is_flat:
                ctx.close(symbol, reason=f"larry {day} exit")
        for symbol, weight in weights.items():
            current = 0.0
            if ctx.equity > 0:
                pos = ctx.position(symbol)
                current = pos.qty * ctx.price(symbol) / ctx.equity
            if abs(current - weight) < drift_band:
                continue
            ctx.target_weight(symbol, weight, reason=f"larry {day} target {weight:.2%}")

        self._last_targets = weights
        self.log.info("applied Larry targets as_of=%s day=%s: %s", self._last_as_of, day, weights)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "ready": bool(self._last_targets),
            "model": "larry",
            "as_of": self._last_as_of.isoformat() if self._last_as_of else None,
            "targets": self._last_targets,
            "load_error": self._load_error,
        }


def _parse_day(value: object) -> date:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def _weights(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ValueError("Larry targets must be an object")
    return {str(symbol).upper(): float(weight) for symbol, weight in raw.items()}
