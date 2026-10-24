"""Feed helpers shared by synthetic, CSV and broker-backed data sources."""

from __future__ import annotations

import itertools
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from typing import Protocol, Sequence

from ..events import Bar
from ..market.calendar import EASTERN, TIMEFRAME_MINUTES, session_for


class HistoricalFeed(Protocol):
    def bars(
        self, symbols: Sequence[str], timeframe: str, start, end
    ) -> Iterator[list[Bar]]: ...


def group_bars(bars: Iterable[Bar], timeframe: str = "1Min") -> Iterator[list[Bar]]:
    """Sort and group bars into one list per timestamp (the engine's unit of work)."""
    ordered = sorted(bars, key=lambda b: (b.ts, b.symbol))
    for _, chunk in itertools.groupby(ordered, key=lambda b: b.ts):
        yield list(chunk)


def _session_key(bar: Bar):
    eastern = bar.ts.astimezone(EASTERN)
    return eastern.date()


def resample(bars: Sequence[Bar], timeframe: str) -> list[Bar]:
    """Aggregate 1-minute bars into a larger timeframe.

    Timestamps stay at the *close* of the aggregated bar. Overnight gaps are
    preserved because buckets never span sessions.
    """
    if timeframe == "1Min" or not bars:
        return list(bars)
    ordered = sorted(bars, key=lambda b: (b.symbol, b.ts))
    if timeframe == "1Day":
        out: list[Bar] = []
        for (symbol, _day), chunk_iter in itertools.groupby(
            ordered, key=lambda b: (b.symbol, _session_key(b))
        ):
            chunk = list(chunk_iter)
            out.append(_combine(symbol, chunk, timeframe))
        return sorted(out, key=lambda b: (b.ts, b.symbol))

    minutes = TIMEFRAME_MINUTES.get(timeframe)
    if minutes is None:
        raise ValueError(f"cannot resample to {timeframe}")
    out = []
    for (symbol, _day, bucket), chunk_iter in itertools.groupby(
        ordered, key=lambda b: (b.symbol, _session_key(b), _bucket(b, minutes))
    ):
        chunk = list(chunk_iter)
        out.append(_combine(symbol, chunk, timeframe))
    return sorted(out, key=lambda b: (b.ts, b.symbol))


def _bucket(bar: Bar, minutes: int) -> int:
    session = session_for(bar.ts.astimezone(EASTERN).date())
    if session is None:
        return 0
    elapsed = (bar.ts - session.opens).total_seconds() / 60.0
    return int((elapsed - 1e-9) // minutes)


def _combine(symbol: str, chunk: Sequence[Bar], timeframe: str) -> Bar:
    return Bar(
        symbol=symbol,
        ts=chunk[-1].ts,
        open=chunk[0].open,
        high=max(b.high for b in chunk),
        low=min(b.low for b in chunk),
        close=chunk[-1].close,
        volume=sum(b.volume for b in chunk),
        timeframe=timeframe,
    )


def bar_span(bars: Sequence[Bar]) -> timedelta:
    if len(bars) < 2:
        return timedelta(0)
    return bars[-1].ts - bars[0].ts


class InMemoryFeed:
    """Bars preloaded once, sliced cheaply for repeated backtests.

    The optimizer runs thousands of evaluations over the same dataset; re-reading
    a CSV each time would dominate the runtime. Bars must be immutable, which
    they are in Krabs.
    """

    def __init__(self, bars: Iterable[Bar]) -> None:
        self._bars: list[Bar] = sorted(bars, key=lambda b: (b.ts, b.symbol))
        self._times: list[datetime] = [b.ts for b in self._bars]

    def __len__(self) -> int:
        return len(self._bars)

    def bounds(self) -> tuple[datetime, datetime] | None:
        if not self._bars:
            return None
        return self._bars[0].ts, self._bars[-1].ts

    def symbols(self) -> list[str]:
        return sorted({b.symbol for b in self._bars})

    def bars(
        self,
        symbols: Sequence[str] | None,
        timeframe: str = "1Min",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[list[Bar]]:
        wanted = {s.upper() for s in symbols} if symbols else None
        lo = bisect_left(self._times, start) if start else 0
        hi = bisect_right(self._times, end) if end else len(self._bars)
        for _, chunk in itertools.groupby(self._bars[lo:hi], key=lambda b: b.ts):
            rows = [b for b in chunk if wanted is None or b.symbol in wanted]
            if rows:
                yield rows
