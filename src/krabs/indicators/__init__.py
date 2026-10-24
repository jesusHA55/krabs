"""Indicator library.

Public functions take/return plain Python sequences. When the Rust extension
``krabs_core`` is importable it is used for the numeric work; otherwise an
equivalent pure-Python implementation runs. Both produce NaN during warm-up so
downstream code has one contract.

Hot-path rule: call indicators once per bar with a *window*, never once per
tick. The FFI boundary copies the window once; the Rust loop then runs at
native speed over hundreds of points. For a 300-point window this is a clear
win on a Pi; for a 3-point window, pure Python would be faster.
"""

from __future__ import annotations

import math
from typing import Sequence

try:  # pragma: no cover - depends on the optional native build
    import krabs_core as _rust  # type: ignore[import-not-found]

    BACKEND = "rust"
except Exception:  # pragma: no cover - default on a fresh checkout
    _rust = None
    BACKEND = "python"

Number = float | int


def backend_name() -> str:
    return BACKEND


# ---------------------------------------------------------------------------
# Pure-Python reference implementations
# ---------------------------------------------------------------------------


def _sma_py(values: Sequence[float], period: int) -> list[float]:
    n = len(values)
    out = [math.nan] * n
    if period <= 0 or n < period:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, n):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def _ema_py(values: Sequence[float], period: int) -> list[float]:
    n = len(values)
    out = [math.nan] * n
    if period <= 0 or n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = values[i] * alpha + prev * (1.0 - alpha)
        out[i] = prev
    return out


def _rsi_py(values: Sequence[float], period: int) -> list[float]:
    n = len(values)
    out = [math.nan] * n
    if period <= 0 or n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _atr_py(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int
) -> list[float]:
    n = len(closes)
    out = [math.nan] * n
    if period <= 0 or n <= period or len(highs) != n or len(lows) != n:
        return out
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    prev = sum(trs[1 : period + 1]) / period
    out[period] = prev
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def _rolling_std_py(values: Sequence[float], period: int) -> list[float]:
    n = len(values)
    out = [math.nan] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        out[i] = math.sqrt(var)
    return out


def _max_drawdown_py(equity: Sequence[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def _realized_vol_py(returns: Sequence[float], periods_per_year: int = 252) -> float:
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def sma(values: Sequence[Number], period: int) -> list[float]:
    floats = [float(v) for v in values]
    if _rust is not None:
        return list(_rust.sma(floats, int(period)))
    return _sma_py(floats, int(period))


def ema(values: Sequence[Number], period: int) -> list[float]:
    floats = [float(v) for v in values]
    if _rust is not None:
        return list(_rust.ema(floats, int(period)))
    return _ema_py(floats, int(period))


def rsi(values: Sequence[Number], period: int) -> list[float]:
    floats = [float(v) for v in values]
    if _rust is not None:
        return list(_rust.rsi(floats, int(period)))
    return _rsi_py(floats, int(period))


def atr(
    highs: Sequence[Number], lows: Sequence[Number], closes: Sequence[Number], period: int
) -> list[float]:
    h = [float(v) for v in highs]
    low = [float(v) for v in lows]
    c = [float(v) for v in closes]
    if _rust is not None:
        return list(_rust.atr(h, low, c, int(period)))
    return _atr_py(h, low, c, int(period))


def rolling_std(values: Sequence[Number], period: int) -> list[float]:
    floats = [float(v) for v in values]
    if _rust is not None:
        return list(_rust.rolling_std(floats, int(period)))
    return _rolling_std_py(floats, int(period))


def max_drawdown(equity: Sequence[Number]) -> float:
    floats = [float(v) for v in equity]
    if _rust is not None:
        return float(_rust.max_drawdown(floats))
    return _max_drawdown_py(floats)


def realized_volatility(returns: Sequence[Number], periods_per_year: int = 252) -> float:
    floats = [float(v) for v in returns]
    if _rust is not None:
        return float(_rust.realized_volatility(floats, int(periods_per_year)))
    return _realized_vol_py(floats, int(periods_per_year))


def last(values: Sequence[float]) -> float:
    return values[-1] if values else math.nan


def crosses_above(fast: Sequence[float], slow: Sequence[float]) -> bool:
    if len(fast) < 2 or len(slow) < 2:
        return False
    a0, a1 = fast[-2], fast[-1]
    b0, b1 = slow[-2], slow[-1]
    if any(math.isnan(x) for x in (a0, a1, b0, b1)):
        return False
    return a0 <= b0 and a1 > b1


def crosses_below(fast: Sequence[float], slow: Sequence[float]) -> bool:
    if len(fast) < 2 or len(slow) < 2:
        return False
    a0, a1 = fast[-2], fast[-1]
    b0, b1 = slow[-2], slow[-1]
    if any(math.isnan(x) for x in (a0, a1, b0, b1)):
        return False
    return a0 >= b0 and a1 < b1
