"""RSI mean reversion: buy oversold, exit when momentum normalises."""

from __future__ import annotations

import math

from ...events import Bar
from ..base import Strategy, StrategyContext
from ..registry import register


@register
class RsiReversion(Strategy):
    id = "rsi_reversion"
    description = "Buy when RSI crosses below the entry level; exit above the exit level."
    defaults = {"period": 14, "entry": 30.0, "exit": 55.0, "allocation": 0.2}

    def __init__(self, **params: object) -> None:
        super().__init__(**params)
        period = int(self.p("period"))
        if period < 2:
            raise ValueError("period must be >= 2")
        if not 0 < float(self.p("entry")) < float(self.p("exit")) < 100:
            raise ValueError("require 0 < entry < exit < 100")
        if not 0 < float(self.p("allocation")) <= 1:
            raise ValueError("allocation must be in (0, 1]")

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        value = ctx.rsi(bar.symbol, int(self.p("period")))
        if math.isnan(value):
            return
        position = ctx.position(bar.symbol)
        if position.is_flat and value < float(self.p("entry")):
            ctx.target_weight(
                bar.symbol,
                float(self.p("allocation")),
                reason=f"RSI {value:.1f} < {self.p('entry')}",
            )
        elif position.qty > 0 and value > float(self.p("exit")):
            ctx.target_weight(bar.symbol, 0.0, reason=f"RSI {value:.1f} > {self.p('exit')}")
