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

Fast overnight (``fast_overnight = true``): a single-file payload may carry a
``fast_overnight`` permission from Larry::

    {"session": "2026-11-17", "symbol": "SPY", "weight": 0.1,
     "min_price": 671.2, "stop_pct": 0.01, "entry_after": "15:55", ...}

On the bar at or after ``entry_after`` ET on ``session`` (and before the close),
the strategy raises ``symbol`` to its base target plus ``weight`` if the bar
close is above ``min_price``. There is no separate exit order: the next
session's base targets are applied after the open and remove the overlay. If
that morning's open gapped down by more than ``stop_pct`` from the entry
price, the same day's entry is skipped.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, time, timezone
import json
import logging
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from ...events import Bar, Order
from ...market.calendar import EASTERN, session_for
from ...strategy.base import Strategy, StrategyContext, StrategyError
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
        "fast_overnight": False,
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
        self._pending_day: date | None = None
        self._last_attempt_ts: datetime | None = None
        self._load_error: str = ""
        self._reset_fast()

    def _reset_fast(self) -> None:
        self._fast_spec: dict[str, Any] | None = None
        self._fast_decided_day: date | None = None
        self._fast_entry: tuple[date, float, float] | None = None  # day, price, stop
        self._fast_blocked_day: date | None = None
        self._fast_status: str = ""

    # -- lifecycle -------------------------------------------------------
    def on_start(self, ctx: StrategyContext) -> None:
        self._rows = []
        self._last_targets = {}
        self._last_as_of = None
        self._last_applied_day = None
        self._pending_day = None
        self._last_attempt_ts = None
        self._load_error = ""
        self._reset_fast()
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
        self._reload()
        self._check_fast_gap(ctx, day)
        self._pending_day = day
        self._attempt(ctx)

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        # A day's targets stay pending until every order they need has been
        # accepted. Retry at most once per new bar timestamp.
        if self._pending_day is not None and (
            self._last_attempt_ts is None or bar.ts > self._last_attempt_ts
        ):
            self._attempt(ctx)
        if bool(self.p("fast_overnight")):
            self._maybe_enter_fast(ctx, bar)

    # -- fast overnight ----------------------------------------------------
    def _check_fast_gap(self, ctx: StrategyContext, day: date) -> None:
        """On the exit morning, block today's entry after a large overnight gap."""

        if self._fast_entry is None or day <= self._fast_entry[0]:
            return
        entry_day, entry_price, stop = self._fast_entry
        self._fast_entry = None
        symbol = str((self._fast_spec or {}).get("symbol") or "SPY").upper()
        today = [b for b in ctx.bars(symbol, 0) if b.ts.astimezone(EASTERN).date() == day]
        if not today or entry_price <= 0:
            return
        gap = today[0].open / entry_price - 1.0
        if stop > 0 and gap < -stop:
            self._fast_blocked_day = day
            self._fast_status = f"overnight stop: {symbol} gap {gap:.2%} on {day}"
            self.log.warning("fast overnight stop hit: %s gap %.2f%%", symbol, gap * 100)

    def _maybe_enter_fast(self, ctx: StrategyContext, bar: Bar) -> None:
        spec = self._fast_spec
        if not spec:
            return
        symbol = str(spec.get("symbol") or "").upper()
        if bar.symbol.upper() != symbol:
            return
        local = bar.ts.astimezone(EASTERN)
        day = local.date()
        if self._fast_decided_day == day:
            return
        try:
            session = date.fromisoformat(str(spec["session"]))
            weight = float(spec.get("weight") or 0.0)
            entry_after = time.fromisoformat(str(spec.get("entry_after") or "15:55"))
            raw_min = spec.get("min_price")
            min_price = float(raw_min) if raw_min is not None else None
            stop = float(spec.get("stop_pct") or 0.0)
        except (KeyError, TypeError, ValueError) as exc:
            self._fast_decided_day = day
            self._fast_status = f"invalid fast_overnight spec: {exc}"
            self.log.error(self._fast_status)
            return
        if session != day:
            return
        market = session_for(day)
        if market is None or local.time() < entry_after:
            return
        if bar.ts >= market.closes:
            return  # the closing bar arrives after the bell; nothing to trade on
        self._fast_decided_day = day  # one decision per session

        skip = ""
        if weight <= 0:
            skip = "no permission from Larry"
        elif self._pending_day is not None:
            skip = "base targets still pending"
        elif min_price is None or bar.close <= min_price:
            skip = f"{symbol} {bar.close:.2f} not above {min_price}"
        elif self._fast_blocked_day == day:
            skip = "overnight stop hit this morning"
        elif ctx.has_open_order(symbol):
            skip = f"open order in {symbol}"
        if skip:
            self._fast_status = f"skipped {day}: {skip}"
            self.log.info("fast overnight %s", self._fast_status)
            return

        base = self._last_targets.get(symbol, 0.0)
        order = ctx.target_weight(
            symbol,
            base + weight,
            price=bar.close,
            reason=f"larry fast overnight {day} +{weight:.2%}",
        )
        if _accepted(order):
            self._fast_entry = (day, bar.close, stop)
            self._fast_status = f"entered {day}: {symbol} +{weight:.2%} at ~{bar.close:.2f}"
        else:
            self._fast_status = f"entry rejected {day}"
        self.log.info("fast overnight %s", self._fast_status)

    def _reload(self) -> None:
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

    def _attempt(self, ctx: StrategyContext) -> None:
        day = self._pending_day
        if day is None:
            return
        self._last_attempt_ts = ctx.now
        if ctx.has_open_order():
            self.log.info("deferring Larry apply for %s: open orders", day)
            return
        targets = self._targets_for(day)
        if targets is None:
            if not bool(self.p("fail_closed")):
                self._pending_day = None
                return
            self.log.warning("no fresh Larry targets for %s; flattening", day)
            done = True
            for symbol, pos in list(ctx.positions().items()):
                if not pos.is_flat:
                    done &= _accepted(ctx.close(symbol, reason="larry missing/stale targets"))
            self._last_targets = {}
        else:
            done = self._apply(ctx, targets, day)
        if done:
            self._last_applied_day = day
            self._pending_day = None
        else:
            self.log.warning("Larry targets for %s not fully accepted; will retry", day)

    # -- loading ---------------------------------------------------------
    def _load_rows(self) -> list[tuple[date, dict[str, float]]]:
        if self.static_targets:
            return [(datetime.now(timezone.utc).date(), self.static_targets)]

        raw = self._read_source(self.source)
        fmt = str(self.p("format")).lower()
        self._fast_spec = None
        if fmt == "json":
            payload = json.loads(raw)
            rows: list[tuple[date, dict[str, float]]] = []
            if isinstance(payload, dict) and "targets" in payload:
                fast = payload.get("fast_overnight")
                self._fast_spec = fast if isinstance(fast, dict) else None
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
    def _apply(self, ctx: StrategyContext, targets: dict[str, float], day: date) -> bool:
        """Send the orders needed to reach ``targets``; True if all were accepted.

        There is deliberately no "same targets as last time" shortcut: the
        drift band already makes an on-target book a no-op, and a shortcut would
        hide targets whose orders were rejected.
        """
        allocation = float(self.p("allocation"))
        gross = sum(abs(weight) for weight in targets.values())
        scale = min(1.0, allocation / gross) if gross > 0 else 1.0
        weights = {
            symbol.upper(): weight * scale
            for symbol, weight in targets.items()
            if abs(weight) > 1e-12
        }

        drift_band = float(self.p("drift_band"))
        done = True
        # Sell/close first so a rotation does not depend on an unfilled buy.
        for symbol in list(ctx.positions()):
            if symbol not in weights and not ctx.position(symbol).is_flat:
                done &= _accepted(ctx.close(symbol, reason=f"larry {day} exit"))
        equity = ctx.equity
        for symbol, weight in weights.items():
            try:
                price = ctx.price(symbol)
            except StrategyError as exc:
                self.log.warning("cannot target %s yet: %s", symbol, exc)
                done = False
                continue
            current = ctx.position(symbol).qty * price / equity if equity > 0 else 0.0
            if abs(current - weight) < drift_band:
                continue
            if abs(weight - current) * equity < ctx.min_trade_notional:
                continue  # target_weight would not send anything
            order = ctx.target_weight(
                symbol, weight, price=price, reason=f"larry {day} target {weight:.2%}"
            )
            done &= _accepted(order)

        self._last_targets = weights
        self.log.info(
            "%s Larry targets as_of=%s day=%s: %s",
            "applied" if done else "partially applied",
            self._last_as_of,
            day,
            weights,
        )
        return done

    def diagnostics(self) -> dict[str, Any]:
        return {
            "ready": bool(self._last_targets),
            "pending_day": self._pending_day.isoformat() if self._pending_day else None,
            "last_applied_day": (
                self._last_applied_day.isoformat() if self._last_applied_day else None
            ),
            "model": "larry",
            "as_of": self._last_as_of.isoformat() if self._last_as_of else None,
            "targets": self._last_targets,
            "load_error": self._load_error,
            "fast_overnight": {
                "enabled": bool(self.p("fast_overnight")),
                "session": (self._fast_spec or {}).get("session"),
                "weight": (self._fast_spec or {}).get("weight"),
                "status": self._fast_status,
            },
        }


def _accepted(order: Order | None) -> bool:
    return order is not None and order.status.value != "rejected"


def _parse_day(value: object) -> date:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def _weights(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ValueError("Larry targets must be an object")
    return {str(symbol).upper(): float(weight) for symbol, weight in raw.items()}
