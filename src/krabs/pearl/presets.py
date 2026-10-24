"""Ready-made Pearl models.

Presets are ordinary builders; they exist so common models have a name and a
stable default configuration. Code remains the source of truth, and every
parameter can be overridden.
"""

from __future__ import annotations

from .builder import ModelBuilder, PearlModel
from .estimators import Johansen, PairSpread
from .features import Series, ZScore
from .filters import Kalman
from .signals import Threshold, ZeroCross


def trend_model(
    symbol: str,
    *,
    level_var: float = 1e-4,
    slope_var: float = 1e-6,
    obs_var: float = 1e-2,
    warmup: int = 10,
    adaptation: str = "fixed",
) -> PearlModel:
    """Local linear trend: long when the estimated slope is positive."""
    return (
        ModelBuilder()
        .add("px", Series(symbol))
        .add(
            "trend",
            Kalman(
                "px",
                model="local_trend",
                level_var=level_var,
                slope_var=slope_var,
                obs_var=obs_var,
                warmup=warmup,
                adaptation=adaptation,
            ),
        )
        .add("signal", ZeroCross("trend.slope"))
        .build()
    )


def zscore_model(
    symbol: str,
    *,
    window: int = 60,
    entry: float = 2.0,
    exit: float = 0.5,
) -> PearlModel:
    """Single-symbol z-score reversion."""
    return (
        ModelBuilder()
        .add("px", Series(symbol))
        .add("z", ZScore("px", window=window))
        .add("signal", Threshold("z", entry=entry, exit=exit))
        .build()
    )


def pairs_model(
    left: str,
    right: str,
    *,
    window: int = 60,
    min_obs: int = 30,
    entry: float = 2.0,
    exit: float = 0.5,
    use_log: bool = True,
    adf: bool = False,
    method: str = "ols",
) -> PearlModel:
    """Rolling hedge-ratio pairs model with a z-score threshold."""
    if method == "johansen":
        spread = Johansen(
            ["left", "right"], window=window, min_obs=min_obs, use_log=use_log
        )
    elif method == "ols":
        spread = PairSpread(
            "left",
            "right",
            window=window,
            min_obs=min_obs,
            use_log=use_log,
            adf=adf,
        )
    else:
        raise ValueError("method must be 'ols' or 'johansen'")
    return (
        ModelBuilder()
        .add("left", Series(left))
        .add("right", Series(right))
        .add("spread", spread)
        .add("signal", Threshold("spread.z", entry=entry, exit=exit))
        .build()
    )
