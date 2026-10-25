"""Overnight drift: buy near the close, sell near the next open.

This is the executable form of the close-to-open anomaly. It needs intraday
bars because the entry is before the close and the exit just after the next
open; with daily bars and next-open fills the trade is not representable.

The bar timestamp is the *close* of the interval. On a 5-minute feed,
``entry_time = "15:55"`` submits during the 15:50–15:55 bar and fills at the
15:55 open of the next bar. ``exit_time = "09:35"`` submits on the first
five-minute bar after the open and fills at 09:35.

Long-only, equal weight across ``symbols``. One overnight hold per symbol per
day; the position is closed the next morning.
"""

from __future__ import annotations

import logging
from datetime import date, time
from typing import Any

from ...events import Bar
from ...market.calendar import EASTERN, session_for
from ...strategy.base import Strategy, StrategyContext
from ..registry import register


@register
class OvernightDrift(Strategy):
    id = "overnight_drift"
    description = (
        "Long near the close, flat after the next open; the executable "
        "close-to-open overnight-drift strategy for intraday bars."
    )
    defaults = {
        "symbols": ["SPY"],
        "allocation": 0.97,
        "entry_time": "15:55",
        "exit_time": "09:35",
    }

    def __init__(self, **params: object) -> None:
        super().__init__(**params)
        symbols = [str(symbol).upper() for symbol in (self.p("symbols") or [])]
        if not symbols:
            raise ValueError("symbols must not be empty")
        if len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be unique")
        if not 0 < float(self.p("allocation")) <= 1:
            raise ValueError("allocation must be in (0, 1]")
        self.symbols = symbols
        try:
            self.entry_time = time.fromisoformat(str(self.p("entry_time")))
            self.exit_time = time.fromisoformat(str(self.p("exit_time")))
        except ValueError as exc:
            raise ValueError("entry_time/exit_time must be HH:MM") from exc
        if self.entry_time <= self.exit_time:
            raise ValueError("entry_time must be later than exit_time")
        self.log = logging.getLogger("krabs.strategy.overnight_drift")
        self._entered_on: dict[str, date] = {}
        self._entry_day: dict[str, date] = {}
        self._last_action = ""

    def on_start(self, ctx: StrategyContext) -> None:
        self._entered_on.clear()
        self._entry_day.clear()
        self._last_action = ""

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        symbol = bar.symbol.upper()
        if symbol not in self.symbols:
            return
        if ctx.has_open_order(symbol):
            return
        now = bar.ts.astimezone(EASTERN)
        day = now.date()
        clock = now.time()
        session = session_for(day)
        if session is None:
            return
        position = ctx.position(symbol)

        if not position.is_flat:
            entered = self._entry_day.get(symbol)
            if entered is not None and day > entered and clock >= self.exit_time:
                ctx.target_weight(symbol, 0.0, reason="overnight_drift exit")
                self._last_action = f"exit {symbol} {day}"
                self.log.debug("exit %s at %s", symbol, clock)
            return

        if clock < self.entry_time:
            return
        if clock >= session.closes.astimezone(EASTERN).time():
            return
        if self._entered_on.get(symbol) == day:
            return
        weight = float(self.p("allocation")) / len(self.symbols)
        order = ctx.target_weight(symbol, weight, reason="overnight_drift entry")
        if order is not None:
            self._entered_on[symbol] = day
            self._entry_day[symbol] = day
            self._last_action = f"enter {symbol} {day}"
            self.log.debug("enter %s at %s", symbol, clock)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "ready": bool(self._entered_on),
            "model": "overnight_drift",
            "symbols": self.symbols,
            "entry_time": self.entry_time.isoformat(timespec="minutes"),
            "exit_time": self.exit_time.isoformat(timespec="minutes"),
            "entered_on": {symbol: day.isoformat() for symbol, day in self._entered_on.items()},
            "entry_day": {symbol: day.isoformat() for symbol, day in self._entry_day.items()},
            "last_action": self._last_action,
        }
