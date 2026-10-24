"""Deterministic synthetic market data.

Used for offline development and CI: same seed + same date range => byte-for-byte
identical bars, on every machine and Python version. The generator is a
per-minute geometric random walk with overnight gaps, fat-ish wicks and a
U-shaped intraday volume profile. It is *not* a market model; do not calibrate
strategies on it.
"""

from __future__ import annotations

import math
import random
import zlib
from datetime import date, datetime, timedelta, timezone
from typing import Iterator, Sequence

from ..events import Bar
from ..market.calendar import session_days, session_for
from .base import group_bars, resample

SECONDS_UTC = timezone.utc
TRADING_MINUTES = 390


class SyntheticFeed:
    def __init__(
        self,
        *,
        days: int = 90,
        seed: int = 7,
        volatility: float = 0.22,
        drift: float = 0.08,
        end: date | None = None,
    ) -> None:
        self.days = max(1, days)
        self.seed = seed
        self.volatility = max(0.0, volatility)
        self.drift = drift
        self.end = end

    # -- public API ------------------------------------------------------
    def bars(
        self,
        symbols: Sequence[str],
        timeframe: str = "1Min",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[list[Bar]]:
        if end is None:
            end = (
                datetime.combine(self.end, datetime.max.time(), tzinfo=SECONDS_UTC)
                if self.end
                else datetime.now(SECONDS_UTC)
            )
        if start is None:
            start = end - timedelta(days=self.days * 2)
        first_day = start.astimezone(SECONDS_UTC).date()
        last_day = end.astimezone(SECONDS_UTC).date()
        trading_days = session_days(first_day, last_day)
        if len(trading_days) > self.days:
            trading_days = trading_days[-self.days :]

        raw: list[Bar] = []
        for symbol in symbols:
            raw.extend(self._generate(symbol.upper(), trading_days, start, end))
        bars = resample(raw, timeframe)
        for chunk in group_bars(bars, timeframe):
            filtered = [b for b in chunk if start <= b.ts <= end]
            if filtered:
                yield filtered

    # -- generation ------------------------------------------------------
    def _generate(
        self, symbol: str, days: Sequence[date], start: datetime, end: datetime
    ) -> list[Bar]:
        rng = random.Random(self.seed ^ zlib.crc32(symbol.encode()))
        # Per-symbol character so the three demo symbols diverge.
        vol = self.volatility * rng.uniform(0.7, 1.4)
        drift = self.drift + rng.uniform(-0.06, 0.10)
        sigma_day = vol / math.sqrt(252.0)
        sigma_min = vol / math.sqrt(252.0 * TRADING_MINUTES)
        mu_min = drift / (252.0 * TRADING_MINUTES)

        price = 60.0 + (zlib.crc32(symbol.encode()) % 240)
        base_volume = 30_000 + (zlib.crc32(symbol.encode()) % 120_000)
        out: list[Bar] = []

        for day in days:
            session = session_for(day)
            if session is None:
                continue
            # Overnight gap.
            price *= math.exp(rng.gauss(0.0, sigma_day * 0.6))
            for minute in range(TRADING_MINUTES):
                ts = session.opens + timedelta(minutes=minute + 1)
                if ts > end:
                    return out
                open_ = price
                ret = rng.gauss(mu_min, sigma_min)
                close = open_ * math.exp(ret)
                wick = abs(rng.gauss(0.0, sigma_min))
                high = max(open_, close) * (1.0 + wick * rng.uniform(0.2, 1.0))
                low = min(open_, close) * (1.0 - wick * rng.uniform(0.2, 1.0))
                if low <= 0:
                    low = min(open_, close) * 0.999
                frac = minute / (TRADING_MINUTES - 1)
                profile = 0.55 + 1.8 * (2.0 * frac - 1.0) ** 2
                volume = base_volume * profile * rng.lognormvariate(0.0, 0.35)
                out.append(
                    Bar(
                        symbol=symbol,
                        ts=ts,
                        open=round(open_, 4),
                        high=round(max(high, open_, close), 4),
                        low=round(min(low, open_, close), 4),
                        close=round(close, 4),
                        volume=round(volume, 2),
                        timeframe="1Min",
                    )
                )
                price = close
                if ts >= end:
                    return out
        return out


def synthetic_bars(
    symbols: Sequence[str],
    days: int = 30,
    seed: int = 7,
    timeframe: str = "1Min",
    end: date | None = None,
) -> Iterator[list[Bar]]:
    """Convenience wrapper used by tests and the CLI."""
    return SyntheticFeed(days=days, seed=seed, end=end).bars(symbols, timeframe)
