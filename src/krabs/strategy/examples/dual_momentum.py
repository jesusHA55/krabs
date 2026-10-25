"""Gary Antonacci-style dual momentum (GEM).

Monthly, long-only:

1. **Relative momentum** — rank the risk assets by trailing 12-month total
   return and take the top ``top_n``.
2. **Absolute momentum** — keep only candidates whose 12-month return beats
   the absolute benchmark (T-bills / ``absolute_asset``, or a fixed threshold).
3. If no candidate passes, hold the defensive asset (aggregate bonds).

The signal is computed from month-end closes before the current bar. Orders
fill on the next bar, so the strategy never uses the bar it trades on.

This is the classic two-asset GEM (SPY / VEU / AGG / BIL), but the universe,
lookback, top-N and absolute benchmark are configurable for sector-rotation
variants.
"""

from __future__ import annotations

import logging
from typing import Any

from ...events import Bar
from ...strategy.base import Strategy, StrategyContext
from ..registry import register


@register
class DualMomentum(Strategy):
    id = "dual_momentum"
    description = (
        "Antonacci dual momentum: relative momentum across risk assets plus an "
        "absolute T-bill filter, with a defensive bond fallback."
    )
    defaults = {
        "risk_assets": ["SPY", "VEU"],
        "defensive_asset": "AGG",
        "absolute_asset": "BIL",
        "absolute_threshold": 0.0,
        "lookback_months": 12,
        "top_n": 1,
        "allocation": 0.97,
        "driver": "SPY",
    }

    def __init__(self, **params: object) -> None:
        super().__init__(**params)
        risk = [str(symbol).upper() for symbol in (self.p("risk_assets") or [])]
        if not risk:
            raise ValueError("risk_assets must not be empty")
        if len(set(risk)) != len(risk):
            raise ValueError("risk_assets must be unique")
        defensive = str(self.p("defensive_asset") or "").upper()
        if not defensive:
            raise ValueError("defensive_asset is required")
        if defensive in risk:
            raise ValueError("defensive_asset must not be a risk asset")
        absolute = str(self.p("absolute_asset") or "").upper()
        if absolute and absolute in risk:
            raise ValueError("absolute_asset must not be a risk asset")
        if absolute and absolute == defensive:
            raise ValueError("absolute_asset must differ from the defensive asset")
        if int(self.p("lookback_months")) < 1:
            raise ValueError("lookback_months must be >= 1")
        if not 1 <= int(self.p("top_n")) <= len(risk):
            raise ValueError("top_n must be between 1 and len(risk_assets)")
        if not 0 < float(self.p("allocation")) <= 1:
            raise ValueError("allocation must be in (0, 1]")
        self.risk_assets = risk
        self.defensive_asset = defensive
        self.absolute_asset = absolute
        self._driver = str(self.p("driver") or risk[0]).upper()
        if not self._driver:
            raise ValueError("driver is required")
        self.log = logging.getLogger("krabs.strategy.dual_momentum")
        self._last_month: tuple[int, int] | None = None
        self._last_returns: dict[str, float] = {}
        self._last_selected: list[str] = []
        self._last_targets: dict[str, float] = {}
        self._rebalances = 0

    @property
    def universe(self) -> list[str]:
        symbols = list(self.risk_assets) + [self.defensive_asset]
        if self.absolute_asset:
            symbols.append(self.absolute_asset)
        return list(dict.fromkeys(symbols))

    # -- lifecycle -------------------------------------------------------
    def on_start(self, ctx: StrategyContext) -> None:
        self._last_month = None
        self._last_returns = {}
        self._last_selected = []
        self._last_targets = {}
        self._rebalances = 0

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        if bar.symbol != self._driver:
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

    # -- signal ----------------------------------------------------------
    def _rebalance(self, ctx: StrategyContext, bar: Bar) -> None:
        returns: dict[str, float] = {}
        for symbol in self.universe:
            value = self._trailing_return(ctx, symbol, bar)
            if value is not None:
                returns[symbol] = value
        needed = [*self.risk_assets, self.defensive_asset]
        if self.absolute_asset:
            needed.append(self.absolute_asset)
        if any(symbol not in returns for symbol in needed):
            self.log.debug(
                "not enough monthly history at %s: %s", bar.ts.date(), sorted(returns)
            )
            return

        if self.absolute_asset:
            absolute_return = returns[self.absolute_asset]
        else:
            absolute_return = float(self.p("absolute_threshold"))
        selected, fallback = self._select(returns, absolute_return)
        allocation = float(self.p("allocation"))
        weights = {symbol: 0.0 for symbol in self.universe}
        if selected:
            each = allocation / len(selected)
            for symbol in selected:
                weights[symbol] = each
        if fallback:
            weights[fallback] = allocation

        # Sell first so a switch does not need buying power from an unfilled sell.
        held = [
            symbol
            for symbol in self.universe
            if not ctx.position(symbol).is_flat
        ]
        order = [symbol for symbol in held if abs(weights[symbol]) < 1e-12]
        order += [symbol for symbol in self.universe if symbol not in order]
        for symbol in order:
            ctx.target_weight(
                symbol,
                weights[symbol],
                reason=(
                    f"dual_momentum {bar.ts:%Y-%m} "
                    f"12m={returns.get(symbol, float('nan')):+.2%}"
                ),
            )

        self._last_returns = returns
        self._last_selected = selected
        self._last_targets = weights
        self._rebalances += 1
        self.log.info(
            "rebalance %s: rank=%s selected=%s fallback=%s",
            bar.ts.date(),
            sorted(
                self.risk_assets,
                key=lambda symbol: returns[symbol],
                reverse=True,
            ),
            selected or "-",
            fallback or "-",
        )

    def _select(
        self, returns: dict[str, float], absolute_return: float
    ) -> tuple[list[str], str | None]:
        ranked = sorted(
            self.risk_assets, key=lambda symbol: returns[symbol], reverse=True
        )
        candidates = [
            symbol
            for symbol in ranked[: int(self.p("top_n"))]
            if returns[symbol] > absolute_return
        ]
        if candidates:
            return candidates, None
        return [], self.defensive_asset

    def _trailing_return(
        self, ctx: StrategyContext, symbol: str, bar: Bar
    ) -> float | None:
        """Total return over ``lookback_months`` using month-end closes."""
        # Calendar-day window: 31 days per month plus a buffer. ``ctx.bars``
        # caps this at the available history, so a long lookback stays safe.
        need_days = min(1000, int(self.p("lookback_months")) * 31 + 45)
        bars = [row for row in ctx.bars(symbol, need_days) if row.ts < bar.ts]
        if len(bars) < 2:
            return None
        month_end: dict[tuple[int, int], float] = {}
        for row in bars:
            month_end[(row.ts.year, row.ts.month)] = row.close
        closes = [month_end[key] for key in sorted(month_end)]
        needed = int(self.p("lookback_months")) + 1
        if len(closes) < needed:
            return None
        first = closes[-needed]
        last = closes[-1]
        if first <= 0:
            return None
        return last / first - 1.0

    # -- observability ---------------------------------------------------
    def diagnostics(self) -> dict[str, Any]:
        return {
            "ready": bool(self._last_selected or self._last_targets),
            "model": "dual_momentum",
            "returns": self._last_returns,
            "selected": self._last_selected,
            "targets": self._last_targets,
            "signals": {
                symbol: 1 if abs(weight) > 1e-12 else 0
                for symbol, weight in self._last_targets.items()
            },
            "values": {
                symbol: {
                    "target": weight,
                    "return_12m": self._last_returns.get(symbol),
                }
                for symbol, weight in self._last_targets.items()
            },
            "rebalances": self._rebalances,
            "lookback_months": int(self.p("lookback_months")),
            "top_n": int(self.p("top_n")),
        }
