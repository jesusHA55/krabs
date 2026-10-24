"""SMA crossover: the reference implementation of a Krabs strategy."""

from __future__ import annotations

import math

from ...events import Bar
from ...indicators import sma
from ..base import Strategy, StrategyContext
from ..registry import register


@register
class SmaCross(Strategy):
    id = "sma_cross"
    description = "Long when fast SMA > slow SMA, flat otherwise (one position per symbol)."
    defaults = {"fast": 20, "slow": 60, "allocation": 0.25}

    def __init__(self, **params: object) -> None:
        super().__init__(**params)
        if int(self.p("fast")) >= int(self.p("slow")):
            raise ValueError("fast period must be smaller than slow period")
        if not 0 < float(self.p("allocation")) <= 1:
            raise ValueError("allocation must be in (0, 1]")
        self._regime: dict[str, bool] = {}

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        fast = int(self.p("fast"))
        slow = int(self.p("slow"))
        closes = ctx.history(bar.symbol, slow + 2)
        if len(closes) < slow:
            return
        fast_ma = sma(closes, fast)[-1]
        slow_ma = sma(closes, slow)[-1]
        if math.isnan(fast_ma) or math.isnan(slow_ma):
            return

        want_long = fast_ma > slow_ma
        previous = self._regime.get(bar.symbol)
        if previous is None:
            # First fully-warmed bar: adopt the current regime instead of
            # waiting for a crossover that may not happen for days.
            self._regime[bar.symbol] = want_long
            if want_long:
                ctx.target_weight(
                    bar.symbol,
                    float(self.p("allocation")),
                    reason=f"initial {fast}/{slow} uptrend",
                )
            return

        if want_long == previous:
            return
        self._regime[bar.symbol] = want_long
        ctx.target_weight(
            bar.symbol,
            float(self.p("allocation")) if want_long else 0.0,
            reason=f"{fast}/{slow} {'bull' if want_long else 'bear'} cross",
        )
