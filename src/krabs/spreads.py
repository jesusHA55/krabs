"""Per-symbol spread tables shared by strategies and cost models.

Two formats are supported:

Static (the default sample):

    symbol,samples,median_spread_bps,mean_spread_bps,...
    XLK,81,1.2,1.4,...

Time series (point-in-time quotes):

    date,symbol,spread_bps
    2020-01-02,XLK,1.4
    2020-01-03,XLK,1.7
    ...

Lookups are **as-of**: the most recent observation at or before the timestamp
is used. There is no forward fill from the future. A missing symbol falls back
to the caller's default (usually ``default_spread_bps``).
"""

from __future__ import annotations

import bisect
import csv
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

LOG = logging.getLogger("krabs.spreads")

_COLUMNS = ("median_spread_bps", "spread_bps")


def _as_date(value: datetime | date | None) -> date:
    if value is None:
        return date.min
    if isinstance(value, datetime):
        return value.date()
    return value


@dataclass(frozen=True)
class SpreadTable:
    """Static values plus optional as-of series, with one global multiplier."""

    static: dict[str, float] = field(default_factory=dict)
    series: dict[str, tuple[tuple[date, float], ...]] = field(default_factory=dict)
    multiplier: float = 1.0
    source: str = "default"

    def at(
        self,
        ts: datetime | date | None,
        symbol: str,
        default: float | None = None,
    ) -> float | None:
        key = symbol.upper()
        points = self.series.get(key)
        if points:
            if ts is None:
                return points[-1][1] * self.multiplier
            day = _as_date(ts)
            index = bisect.bisect_right([point[0] for point in points], day) - 1
            if index >= 0:
                return points[index][1] * self.multiplier
        value = self.static.get(key)
        if value is None:
            return default
        return value * self.multiplier

    def snapshot(
        self,
        ts: datetime | date | None,
        symbols: list[str] | tuple[str, ...] | None = None,
        default: float | None = None,
    ) -> dict[str, float]:
        wanted = symbols or sorted(set(self.static) | set(self.series))
        result: dict[str, float] = {}
        for symbol in wanted:
            value = self.at(ts, symbol, default)
            if value is not None:
                result[symbol.upper()] = value
        return result

    def latest(self, default: float | None = None) -> dict[str, float]:
        symbols = sorted(set(self.static) | set(self.series))
        result: dict[str, float] = {}
        for symbol in symbols:
            value = self.at(None, symbol, default)
            if value is not None:
                result[symbol] = value
        return result

    def __bool__(self) -> bool:
        return bool(self.static or self.series)


def _parse_date(value: str) -> date:
    text = (value or "").strip()
    if not text:
        raise ValueError("empty date")
    if len(text) >= 10:
        return date.fromisoformat(text[:10])
    return date.fromisoformat(text)


def load_spread_table(
    path: str | Path,
    *,
    multiplier: float = 1.0,
    base: dict[str, float] | None = None,
    logger: logging.Logger | None = None,
) -> SpreadTable:
    """Load a static or time-series spread CSV."""
    log = logger or LOG
    table_path = Path(path)
    if not table_path.is_file():
        log.warning("spread table not found: %s", table_path)
        return SpreadTable(static=dict(base or {}), multiplier=multiplier, source="params")
    static = {str(k).upper(): float(v) for k, v in (base or {}).items()}
    series: dict[str, list[tuple[date, float]]] = {}
    with table_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = {name.strip().lower() for name in (reader.fieldnames or [])}
        time_series = "date" in fields
        for row in reader:
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            value = None
            for column in _COLUMNS:
                raw = row.get(column)
                if raw not in (None, ""):
                    try:
                        value = float(raw)
                    except ValueError:
                        log.warning(
                            "invalid %s for %s in %s: %r",
                            column,
                            symbol,
                            table_path,
                            raw,
                        )
                        value = None
                    break
            if value is None or value < 0:
                continue
            if time_series:
                try:
                    day = _parse_date(row.get("date") or "")
                except ValueError:
                    log.warning("invalid date for %s in %s", symbol, table_path)
                    continue
                series.setdefault(symbol, []).append((day, value))
            else:
                static[symbol] = value
    frozen = {
        symbol: tuple(sorted(points, key=lambda point: point[0]))
        for symbol, points in series.items()
    }
    source = f"csv:{table_path} ({len(static)} static, {len(frozen)} series)"
    return SpreadTable(
        static=static,
        series=frozen,
        multiplier=float(multiplier),
        source=source,
    )


def load_spreads(path: str | Path, *, logger: logging.Logger | None = None) -> dict[str, float]:
    """Backward-compatible latest-value view of a spread table."""
    table = load_spread_table(path, logger=logger)
    return table.latest()
