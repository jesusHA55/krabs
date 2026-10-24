"""Pairs trading with Pearl: rolling hedge ratio + z-score hysteresis."""

from __future__ import annotations

from ...events import Bar
from ...pearl import (
    Johansen,
    ModelBuilder,
    PairSpread,
    PearlSnapshot,
    PearlStrategy,
    Series,
    Threshold,
)
from ...strategy.base import StrategyContext
from ..registry import register


@register
class PearlPairs(PearlStrategy):
    id = "pearl_pairs"
    description = (
        "Pearl pairs: rolling OLS hedge ratio, spread z-score and hysteresis. "
        "Long the spread when z is low, short when z is high."
    )
    defaults = {
        "left": "AAPL",
        "right": "MSFT",
        "model": {
            "window": 60,
            "entry": 2.0,
            "exit": 0.5,
            "allocation": 0.4,
            "min_obs": 30,
            "use_log": True,
            "adf": False,
            "rebalance_tolerance": 0.20,
            "method": "ols",
        },
    }

    def __init__(self, **params: object) -> None:
        # Backwards-friendly: flat and dotted model parameters are promoted
        # into `model`.
        model = dict(params.pop("model", {}) or {})
        for key in (
            "window",
            "entry",
            "exit",
            "allocation",
            "min_obs",
            "use_log",
            "adf",
            "rebalance_tolerance",
            "method",
        ):
            if key in params:
                model[key] = params.pop(key)
            dotted = f"model.{key}"
            if dotted in params:
                model[key] = params.pop(dotted)
        params["model"] = model
        super().__init__(**params)
        left = str(self.p("left")).upper()
        right = str(self.p("right")).upper()
        if left == right:
            raise ValueError("left and right must be different symbols")
        if not 0 < float(self.p("model.allocation")) <= 1:
            raise ValueError("allocation must be in (0, 1]")
        if int(self.p("model.window")) < 5:
            raise ValueError("window must be >= 5")
        if not 0 <= float(self.p("model.rebalance_tolerance")) < 1:
            raise ValueError("rebalance_tolerance must be in [0, 1)")
        if str(self.p("model.method")) not in ("ols", "johansen"):
            raise ValueError("model.method must be 'ols' or 'johansen'")
        self.symbols = [left, right]
        self._last_side = 0
        self._last_beta: float | None = None

    def on_start(self, ctx: StrategyContext) -> None:
        super().on_start(ctx)
        self._last_side = 0
        self._last_beta = None

    def build_model(self) -> object:
        if str(self.p("model.method")) == "johansen":
            spread = Johansen(
                ["left", "right"],
                window=int(self.p("model.window")),
                min_obs=int(self.p("model.min_obs")),
                use_log=bool(self.p("model.use_log")),
            )
        else:
            spread = PairSpread(
                "left",
                "right",
                window=int(self.p("model.window")),
                min_obs=int(self.p("model.min_obs")),
                use_log=bool(self.p("model.use_log")),
                adf=bool(self.p("model.adf")),
            )
        return (
            ModelBuilder()
            .add("left", Series(str(self.p("left"))))
            .add("right", Series(str(self.p("right"))))
            .add("spread", spread)
            .add(
                "entry",
                Threshold(
                    "spread.z",
                    entry=float(self.p("model.entry")),
                    exit=float(self.p("model.exit")),
                ),
            )
            .build()
        )

    def on_snapshot(self, ctx: StrategyContext, snapshot: PearlSnapshot) -> None:
        if str(self.p("model.method")) == "johansen":
            rank = snapshot.value("spread.rank", 0.0) or 0.0
            if rank < 1:
                return
        side = snapshot.signal("entry")
        beta = snapshot.value("spread.beta", 1.0) or 1.0
        left = self.symbols[0]
        right = self.symbols[1]
        left_flat = ctx.position(left).is_flat
        right_flat = ctx.position(right).is_flat

        if side == 0 and left_flat and right_flat:
            self._last_side = 0
            return
        tolerance = float(self.p("model.rebalance_tolerance"))
        if (
            side == self._last_side
            and self._last_beta is not None
            and not (left_flat and right_flat)
        ):
            drift = abs(beta - self._last_beta) / max(0.1, abs(self._last_beta))
            if drift < tolerance:
                return

        gross = float(self.p("model.allocation"))
        denom = 1.0 + abs(beta)
        left_weight = side * gross / denom
        right_weight = -side * beta * gross / denom
        reason = (
            f"pearl z={snapshot.value('spread.z', 0.0):+.2f} "
            f"beta={beta:.2f} side={side:+d}"
        )
        left_order = ctx.target_weight(left, left_weight, reason=reason)
        right_order = ctx.target_weight(right, right_weight, reason=reason)
        # Only remember the target if both legs were accepted by risk/broker.
        if left_order is not None and right_order is not None:
            self._last_side = side
            self._last_beta = beta
