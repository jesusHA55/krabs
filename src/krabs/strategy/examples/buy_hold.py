"""Buy and hold: the benchmark every strategy must beat."""

from __future__ import annotations

from ...events import Bar
from ..base import Strategy, StrategyContext
from ..registry import register


@register
class BuyHold(Strategy):
    id = "buy_hold"
    description = "Allocate a fixed weight to each symbol on its first bar and never trade again."
    defaults = {"allocation": 0.9}

    def __init__(self, **params: object) -> None:
        super().__init__(**params)
        if not 0 < float(self.p("allocation")) <= 1:
            raise ValueError("allocation must be in (0, 1]")
        self._entered: set[str] = set()

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        if bar.symbol in self._entered:
            return
        self._entered.add(bar.symbol)
        ctx.target_weight(
            bar.symbol, float(self.p("allocation")), reason="initial allocation"
        )
