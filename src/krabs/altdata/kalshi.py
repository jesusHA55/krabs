"""Read-only Kalshi market history for event research.

Kalshi's trade API is public for market data. This module only fetches
candlesticks; it does not place orders or require an account.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MAX_CANDLES = 5000


@dataclass(frozen=True, slots=True)
class ProbabilityPoint:
    ts: datetime
    probability: float


@dataclass(frozen=True)
class ProbabilitySeries:
    points: tuple[ProbabilityPoint, ...]

    def at(self, ts: datetime) -> float | None:
        """Most recent probability at or before ``ts``."""
        if not self.points:
            return None
        stamps = [point.ts for point in self.points]
        index = bisect_right(stamps, ts) - 1
        return self.points[index].probability if index >= 0 else None

    def change(self, start: datetime, end: datetime) -> float | None:
        first = self.at(start)
        last = self.at(end)
        if first is None or last is None:
            return None
        return last - first


class KalshiClient:
    def __init__(self, base_url: str = BASE_URL, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url, headers={"User-Agent": "Krabs research", "Accept": "application/json"}
        )
        last: Exception | None = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                last = exc
                time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"Kalshi request failed: {last}")

    def candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        start: datetime,
        end: datetime,
        *,
        period_interval: int = 1440,
    ) -> list[ProbabilityPoint]:
        points: list[ProbabilityPoint] = []
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        step = max(60, int(period_interval) * 60)
        # The endpoint caps the number of candles; split long ranges.
        cursor = start_ts
        while cursor < end_ts:
            chunk_end = min(end_ts, cursor + step * (MAX_CANDLES - 1))
            payload = self._get(
                f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
                {
                    "start_ts": cursor,
                    "end_ts": chunk_end,
                    "period_interval": int(period_interval),
                },
            )
            for candle in payload.get("candlesticks") or []:
                point = _candle_point(candle)
                if point is not None:
                    points.append(point)
            cursor = chunk_end
        unique = {point.ts: point for point in points}
        return [unique[key] for key in sorted(unique)]

    def series(self, *args, **kwargs) -> ProbabilitySeries:
        return ProbabilitySeries(tuple(self.candlesticks(*args, **kwargs)))


def _candle_point(candle: dict) -> ProbabilityPoint | None:
    bid = (candle.get("yes_bid") or {}).get("close_dollars")
    ask = (candle.get("yes_ask") or {}).get("close_dollars")
    price = (candle.get("price") or {}).get("close_dollars")
    try:
        if bid is not None and ask is not None:
            low, high = float(bid), float(ask)
            if 0.0 < low < 1.0 and 0.0 < high < 1.0 and high >= low:
                probability = (low + high) / 2.0
            else:
                probability = None
        else:
            probability = float(price) if price is not None else None
    except (TypeError, ValueError):
        probability = None
    if probability is None or not 0.0 <= probability <= 1.0:
        return None
    stamp = candle.get("end_period_ts")
    if stamp is None:
        return None
    return ProbabilityPoint(
        datetime.fromtimestamp(int(stamp), tz=timezone.utc), probability
    )


def points_from_payload(payload: dict) -> ProbabilitySeries:
    """Offline helper for tests and cached payloads."""
    points: list[ProbabilityPoint] = []
    for candle in payload.get("candlesticks") or []:
        point = _candle_point(candle)
        if point is not None:
            points.append(point)
    points.sort(key=lambda point: point.ts)
    return ProbabilitySeries(tuple(points))
