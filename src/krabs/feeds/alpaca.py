"""Alpaca market data adapter (REST polling).

Polling is the default because it is boring and reliable on a Pi. A WebSocket
upgrade can be added behind the same ``latest_bars``/``history`` surface without
touching the runtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..brokers.alpaca import AlpacaClient
from ..brokers.base import BrokerError
from ..events import Bar, ensure_utc
from ..market.calendar import EASTERN, session_for, timeframe_delta
from .base import group_bars


class AlpacaFeed:
    def __init__(
        self,
        client: AlpacaClient,
        *,
        feed: str = "iex",
        adjustment: str = "raw",
    ) -> None:
        self.client = client
        self.feed = feed
        self.adjustment = adjustment

    def history(
        self,
        symbols: list[str],
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Bar]:
        if not symbols:
            return []
        bars: list[Bar] = []
        token: str | None = None
        pages = 0
        for _ in range(200):  # page cap: fail loudly instead of looping forever
            pages += 1
            params = {
                "symbols": ",".join(symbols),
                "timeframe": timeframe,
                "start": _iso(start),
                "end": _iso(end) if end else None,
                "limit": 10_000,
                "adjustment": self.adjustment,
                "feed": self.feed,
                "sort": "asc",
                "page_token": token,
            }
            payload = self.client.request("GET", "/v2/stocks/bars", params=params)
            for symbol, rows in (payload.get("bars") or {}).items():
                for row in rows:
                    bars.append(_bar(symbol, row, timeframe))
            token = payload.get("next_page_token")
            if not token:
                break
        else:
            raise BrokerError(f"bar pagination exceeded 200 pages ({pages}) for {symbols}")
        bars.sort(key=lambda b: (b.ts, b.symbol))
        return bars

    def bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ):
        if start is None:
            start = datetime.now(timezone.utc) - timedelta(days=5)
        yield from group_bars(self.history(symbols, timeframe, start, end), timeframe)

    def latest_bars(self, symbols: list[str], timeframe: str, lookback: int = 10) -> list[Bar]:
        now = datetime.now(timezone.utc)
        span = timeframe_delta(timeframe) * max(lookback * 3, 20)
        start = now - span - timedelta(days=1)
        return self.history(symbols, timeframe, start, now)


def _bar(symbol: str, row: dict, timeframe: str) -> Bar:
    start = ensure_utc(datetime.fromisoformat(row["t"].replace("Z", "+00:00")))
    if timeframe == "1Day":
        session = session_for(start.astimezone(EASTERN).date())
        ts = session.closes if session else start
    else:
        ts = start + timeframe_delta(timeframe)
    return Bar(
        symbol=symbol.upper(),
        ts=ts,
        open=float(row["o"]),
        high=float(row["h"]),
        low=float(row["l"]),
        close=float(row["c"]),
        volume=float(row.get("v", 0.0)),
        timeframe=timeframe,
    )


def _iso(ts: datetime) -> str:
    return ensure_utc(ts).isoformat(timespec="seconds").replace("+00:00", "Z")
