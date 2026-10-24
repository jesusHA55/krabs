"""Statistical primitives used by Pearl estimators.

All functions are deterministic and operate on Python sequences. Missing values
and degenerate samples raise ``ValueError``; callers decide whether that means
"not ready" or a hard configuration error.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from . import linalg


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("mean of empty sequence")
    return sum(values) / len(values)


def variance(values: Sequence[float], *, sample: bool = True) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    avg = mean(values)
    divisor = n - 1 if sample else n
    return sum((x - avg) ** 2 for x in values) / divisor


def std(values: Sequence[float], *, sample: bool = True) -> float:
    return math.sqrt(max(0.0, variance(values, sample=sample)))


def covariance(x: Sequence[float], y: Sequence[float], *, sample: bool = True) -> float:
    n = min(len(x), len(y))
    if n < 2:
        return 0.0
    mx, my = sum(x[:n]) / n, sum(y[:n]) / n
    divisor = n - 1 if sample else n
    return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / divisor


def correlation(x: Sequence[float], y: Sequence[float]) -> float:
    sx, sy = std(x), std(y)
    if sx == 0 or sy == 0:
        return 0.0
    return covariance(x, y) / (sx * sy)


def zscore(value: float, values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    sigma = std(values)
    if sigma <= 0:
        return 0.0
    return (value - mean(values)) / sigma


def ols(
    y: Sequence[float],
    x: Sequence[float],
    *,
    intercept: bool = True,
) -> dict:
    """Ordinary least squares for one regressor.

    Returns coefficients (intercept first), residuals, R² and the inverse of
    X'X (useful for standard errors).
    """
    n = min(len(y), len(x))
    if n < (3 if intercept else 2):
        raise ValueError("not enough observations for regression")
    ys = [float(v) for v in y[:n]]
    xs = [float(v) for v in x[:n]]
    if intercept:
        design = [[1.0, xs[i]] for i in range(n)]
    else:
        design = [[xs[i]] for i in range(n)]
    xtx = linalg.matmul(linalg.transpose(design), design)
    xty = [sum(design[i][j] * ys[i] for i in range(n)) for j in range(len(design[0]))]
    try:
        inv = linalg.inverse(xtx)
    except ValueError:
        return {
            "coef": [0.0] * len(design[0]),
            "residuals": [0.0] * n,
            "r2": 0.0,
            "xtx_inv": linalg.zeros(len(design[0]), len(design[0])),
            "n": n,
        }
    coef = linalg.matvec(inv, xty)
    residuals = [
        ys[i] - sum(coef[j] * design[i][j] for j in range(len(coef))) for i in range(n)
    ]
    y_mean = mean(ys)
    ss_tot = sum((value - y_mean) ** 2 for value in ys)
    ss_res = sum(value * value for value in residuals)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {"coef": coef, "residuals": residuals, "r2": r2, "xtx_inv": inv, "n": n}


def adf_tstat(residuals: Sequence[float]) -> float:
    """ADF t-statistic for a unit root in residuals (constant, no trend).

    Approximate only: use it as a feature, not as a substitute for a proper
    ADF implementation with MacKinnon p-values.
    """
    values = [float(v) for v in residuals]
    if len(values) < 10:
        return 0.0
    y = [values[i] - values[i - 1] for i in range(1, len(values))]
    x = [values[i - 1] for i in range(1, len(values))]
    result = ols(y, x, intercept=True)
    coef = result["coef"]
    residuals2 = result["residuals"]
    n = result["n"]
    k = 2
    if n <= k:
        return 0.0
    sigma2 = sum(v * v for v in residuals2) / (n - k)
    try:
        se = math.sqrt(max(0.0, sigma2 * result["xtx_inv"][1][1]))
    except (IndexError, ValueError):
        return 0.0
    return coef[1] / se if se > 0 else 0.0


def half_life(residuals: Sequence[float]) -> float:
    """Half-life of mean reversion from an AR(1) on the level series."""
    values = [float(v) for v in residuals]
    if len(values) < 5:
        return 0.0
    y = [values[i] - values[i - 1] for i in range(1, len(values))]
    x = [values[i - 1] for i in range(1, len(values))]
    result = ols(y, x, intercept=True)
    beta = result["coef"][1]
    theta = -beta
    if theta <= 0:
        return 0.0
    return math.log(2.0) / theta


def hurst_rs(values: Sequence[float], *, min_window: int = 8) -> float:
    """Hurst exponent via rescaled range (R/S).

    A random walk tends to ~0.5; trending series >0.5; mean-reverting <0.5.
    This is a coarse estimator and needs a few hundred observations.
    """
    series = [float(v) for v in values]
    if len(series) < min_window * 2:
        return 0.5
    logs: list[float] = []
    sizes: list[float] = []
    size = min_window
    while size <= len(series) // 2:
        chunks = len(series) // size
        ratios = []
        for chunk in range(chunks):
            window = series[chunk * size : (chunk + 1) * size]
            avg = mean(window)
            deviations = [v - avg for v in window]
            cumulative = []
            total = 0.0
            for value in deviations:
                total += value
                cumulative.append(total)
            spread = max(cumulative) - min(cumulative)
            sigma = std(window, sample=False)
            if sigma > 0:
                ratios.append(spread / sigma)
        if ratios:
            logs.append(math.log(sum(ratios) / len(ratios)))
            sizes.append(math.log(size))
        size *= 2
    if len(logs) < 2:
        return 0.5
    mx = sum(sizes) / len(sizes)
    my = sum(logs) / len(logs)
    denom = sum((x - mx) ** 2 for x in sizes)
    if denom == 0:
        return 0.5
    return sum((sizes[i] - mx) * (logs[i] - my) for i in range(len(logs))) / denom


def hp_trend(values: Sequence[float], *, lam: float = 1600.0, iterations: int = 40) -> list[float]:
    """Hodrick–Prescott trend via conjugate gradient on the SPD system.

    The filter is two-sided; Pearl applies it to a rolling window ending at the
    current bar, so it never sees the future but the recent trend can revise.
    """
    y = [float(v) for v in values]
    n = len(y)
    if n < 3:
        return list(y)
    # A = I + lambda * D'D, applied matrix-free. D is the second-difference matrix.
    def apply_a(x: list[float]) -> list[float]:
        d = [x[i] - 2.0 * x[i + 1] + x[i + 2] for i in range(n - 2)]
        out = list(x)
        for i in range(n - 2):
            out[i] += lam * d[i]
            out[i + 1] -= 2.0 * lam * d[i]
            out[i + 2] += lam * d[i]
        return out

    x = list(y)
    r = [y[i] - apply_a(x)[i] for i in range(n)]
    p = list(r)
    rs_old = sum(v * v for v in r)
    for _ in range(iterations):
        if rs_old < 1e-12:
            break
        ap = apply_a(p)
        denom = sum(p[i] * ap[i] for i in range(n))
        if denom <= 1e-18:
            break
        alpha = rs_old / denom
        for i in range(n):
            x[i] += alpha * p[i]
            r[i] -= alpha * ap[i]
        rs_new = sum(v * v for v in r)
        beta = rs_new / rs_old
        for i in range(n):
            p[i] = r[i] + beta * p[i]
        rs_old = rs_new
    return x
