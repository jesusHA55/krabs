"""Synthetic live feed for deployment smoke tests.

Generates one fresh bar per cycle from a random walk, with wall-clock
timestamps. It exercises the full runtime (polling, risk, paper fills, SQLite,
heartbeat, kill switch) without a broker, API keys or network access.

It is **not** a market simulator for research: use the backtester for that. Bars
are accelerated (one per poll), so do not read anything into strategy PnL here.
"""

from __future__ import annotations

import math
import random
import zlib
from datetime import datetime, timedelta, timezone
from typing import Sequence

from ..events import Bar
from .synthetic import SyntheticFeed

TRADING_MINUTES_PER_YEAR = 252 * 390


class SyntheticLiveFeed:
    def __init__(
        self,
        symbols: Sequence[str],
        *,
        seed: int = 7,
        history_days: int = 2,
        volatility: float = 0.22,
        drift: float = 0.08,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.seed = seed
        self.history_days = max(1, history_days)
        self.volatility = max(0.0, volatility)
        self.drift = drift
        self._rng = random.Random(seed)
        self._sigma = self.volatility / math.sqrt(TRADING_MINUTES_PER_YEAR)
        self._mu = self.drift / TRADING_MINUTES_PER_YEAR
        self._last_close: dict[str, float] = {}
        self._last_ts: dict[str, datetime] = {}

    # -- protocol --------------------------------------------------------
    def history(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Bar]:
        end = end or datetime.now(timezone.utc)
        # If the market is closed, a one-day lookback can start after the last
        # session close and return no bars. Expand the window; the generator
        # still emits only its configured number of sessions.
        start = min(start, end - timedelta(days=self.history_days + 2))
        generator = SyntheticFeed(
            days=self.history_days,
            seed=self.seed,
            volatility=self.volatility,
            drift=self.drift,
        )
        wanted = {s.upper() for s in symbols}
        bars: list[Bar] = []
        for chunk in generator.bars(list(wanted), timeframe, start, end):
            for bar in chunk:
                if bar.symbol not in wanted:
                    continue
                bars.append(bar)
                self._last_close[bar.symbol] = bar.close
                previous = self._last_ts.get(bar.symbol)
                if previous is None or bar.ts > previous:
                    self._last_ts[bar.symbol] = bar.ts
        return bars

    def latest_bars(
        self, symbols: Sequence[str], timeframe: str = "1Min", lookback: int = 10
    ) -> list[Bar]:
        del lookback  # one new bar per call: the runtime controls the cadence
        now = datetime.now(timezone.utc)
        base_ts = now - timedelta(seconds=1)
        out: list[Bar] = []
        for symbol in symbols:
            symbol = symbol.upper()
            previous_ts = self._last_ts.get(symbol)
            ts = base_ts
            if previous_ts is not None and ts <= previous_ts:
                ts = previous_ts + timedelta(milliseconds=1)
            if ts > now:
                ts = now
            out.append(self._next_bar(symbol, ts, timeframe))
        return out

    # -- internals -------------------------------------------------------
    def _next_bar(self, symbol: str, ts: datetime, timeframe: str) -> Bar:
        previous = self._last_close.get(symbol)
        if previous is None:
            previous = 60.0 + (zlib.crc32(symbol.encode()) % 240)
        ret = self._rng.gauss(self._mu, self._sigma)
        close = max(0.01, previous * math.exp(ret))
        wick = abs(self._rng.gauss(0.0, self._sigma))
        high = max(previous, close) * (1.0 + wick * self._rng.uniform(0.2, 1.0))
        low = min(previous, close) * (1.0 - wick * self._rng.uniform(0.2, 1.0))
        volume = 20_000 * self._rng.lognormvariate(0.0, 0.4)
        bar = Bar(
            symbol=symbol,
            ts=ts,
            open=round(previous, 4),
            high=round(max(high, previous, close), 4),
            low=round(max(0.01, min(low, previous, close)), 4),
            close=round(close, 4),
            volume=round(volume, 2),
            timeframe=timeframe,
        )
        self._last_close[symbol] = bar.close
        self._last_ts[symbol] = bar.ts
        return bar
