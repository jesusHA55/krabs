"""200-day SMA trend filter on leveraged index ETFs.

Monthly, long-only:

- if the *signal* asset's close is above its 200-day simple moving average,
  hold the leveraged trade ETF;
- otherwise hold the defensive asset (T-bills / bonds).

The signal is the underlying index ETF (SPY or QQQ), not the leveraged wrapper.
This avoids a volatility-decay artifact in the filter: the wrapper's price path
is path-dependent, while the index trend is the economic signal.

The signal uses the last completed bar before the current bar and orders fill
on the next bar, so it is causal.
"""

from __future__ import annotations

import logging
from typing import Any

from ...events import Bar
from ...strategy.base import Strategy, StrategyContext
from ..registry import register


@register
class LeveragedTrend(Strategy):
    id = "leveraged_trend"
    description = (
        "200-day SMA trend filter on a leveraged index ETF, with a defensive "
        "cash/bond asset when the trend is down."
    )
    defaults = {
        "signal_symbol": "SPY",
        "trade_symbol": "UPRO",
        "defensive_symbol": "BIL",
        "sma_days": 200,
        "allocation": 0.97,
    }

    def __init__(self, **params: object) -> None:
        super().__init__(**params)
        self.signal_symbol = str(self.p("signal_symbol") or "").upper()
        self.trade_symbol = str(self.p("trade_symbol") or "").upper()
        self.defensive_symbol = str(self.p("defensive_symbol") or "").upper()
        if not all((self.signal_symbol, self.trade_symbol, self.defensive_symbol)):
            raise ValueError("signal, trade and defensive symbols are required")
        if self.trade_symbol == self.defensive_symbol:
            raise ValueError("trade and defensive symbols must differ")
        if self.signal_symbol == self.defensive_symbol:
            raise ValueError("signal and defensive symbols must differ")
        if int(self.p("sma_days")) < 2:
            raise ValueError("sma_days must be >= 2")
        if not 0 < float(self.p("allocation")) <= 1:
            raise ValueError("allocation must be in (0, 1]")
        self.log = logging.getLogger("krabs.strategy.leveraged_trend")
        self._last_month: tuple[int, int] | None = None
        self._last_above: bool | None = None
        self._last_sma = 0.0
        self._last_price = 0.0
        self._targets: dict[str, float] = {}
        self._rebalances = 0

    def on_start(self, ctx: StrategyContext) -> None:
        self._last_month = None
        self._last_above = None
        self._last_sma = 0.0
        self._last_price = 0.0
        self._targets = {}
        self._rebalances = 0

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        if bar.symbol != self.signal_symbol:
            return
        if ctx.has_open_order():
            return
        month = (bar.ts.year, bar.ts.month)
        if self._last_month is None:
            self._last_month = month
            self._rebalance(ctx, bar)
            return
        if month == self._last_month:
            return
        self._last_month = month
        self._rebalance(ctx, bar)

    def _rebalance(self, ctx: StrategyContext, bar: Bar) -> None:
        days = int(self.p("sma_days"))
        bars = [row for row in ctx.bars(self.signal_symbol, days + 10) if row.ts < bar.ts]
        if len(bars) < days:
            self.log.debug("not enough history at %s: %d bars", bar.ts.date(), len(bars))
            return
        closes = [row.close for row in bars[-days:]]
        sma = sum(closes) / len(closes)
        price = closes[-1]
        above = price > sma
        allocation = float(self.p("allocation"))
        weights = {self.trade_symbol: 0.0, self.defensive_symbol: 0.0}
        if above:
            weights[self.trade_symbol] = allocation
        else:
            weights[self.defensive_symbol] = allocation

        # Sell first so switching does not depend on an unfilled sell.
        held = [
            symbol
            for symbol in (self.trade_symbol, self.defensive_symbol)
            if not ctx.position(symbol).is_flat
        ]
        order = [symbol for symbol in held if weights[symbol] <= 0]
        order += [symbol for symbol in weights if symbol not in order]
        for symbol in order:
            ctx.target_weight(
                symbol,
                weights[symbol],
                reason=(
                    f"leveraged_trend {bar.ts:%Y-%m} "
                    f"{self.signal_symbol} {'above' if above else 'below'} SMA"
                ),
            )

        self._last_above = above
        self._last_sma = sma
        self._last_price = price
        self._targets = weights
        self._rebalances += 1
        self.log.info(
            "rebalance %s: %s %.2f vs SMA %.2f -> %s",
            bar.ts.date(),
            self.signal_symbol,
            price,
            sma,
            "risk-on" if above else "defensive",
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "ready": self._last_above is not None,
            "model": "leveraged_trend",
            "signal_symbol": self.signal_symbol,
            "trade_symbol": self.trade_symbol,
            "defensive_symbol": self.defensive_symbol,
            "price": self._last_price,
            "sma": self._last_sma,
            "above": None if self._last_above is None else int(self._last_above),
            "targets": self._targets,
            "signals": {
                self.trade_symbol: 1 if self._targets.get(self.trade_symbol, 0) > 0 else 0,
                self.defensive_symbol: 1
                if self._targets.get(self.defensive_symbol, 0) > 0
                else 0,
            },
            "values": {
                "trend": {
                    "price": self._last_price,
                    "sma": self._last_sma,
                }
            },
            "rebalances": self._rebalances,
        }
