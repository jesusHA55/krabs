"""Statistical estimator nodes: pairs, mean reversion, regime and volatility."""

from __future__ import annotations

import math
from collections import deque
from typing import Any, Mapping

from .. import linalg, stats
from ..nodes.base import Node
from ..types import Observation


class PairSpread(Node):
    """Rolling OLS hedge ratio for a pair, plus z-score and half-life.

    The regression is estimated on the rolling window ending at the current bar.
    It never sees future bars, but it is an in-sample estimate: that is normal
    for a pairs signal, and the order still fills on the next bar.
    """

    kind = "pair_spread"

    def __init__(
        self,
        left: str,
        right: str,
        *,
        window: int = 60,
        min_obs: int | None = None,
        use_log: bool = True,
        adf: bool = False,
    ) -> None:
        super().__init__()
        if window < 5:
            raise ValueError("window must be >= 5")
        self.left = left
        self.right = right
        self.window = window
        self.min_obs = min_obs or max(10, window // 2)
        self.use_log = use_log
        self.adf = adf
        self._left: deque[float] = deque(maxlen=window)
        self._right: deque[float] = deque(maxlen=window)
        self._last: dict[str, Any] = {}

    @property
    def input_nodes(self):
        return self.input_names(self.left, self.right)

    @property
    def ready(self) -> bool:
        if len(self._left) < self.min_obs or len(self._right) < self.min_obs:
            return False
        return self._last.get("ready", 0.0) == 1.0

    def reset(self) -> None:
        self._left.clear()
        self._right.clear()
        self._last = {}

    def update(self, obs: Observation, inputs: Mapping[str, Mapping[str, Any]]):
        left = self.read(inputs, self.left)
        right = self.read(inputs, self.right)
        if left <= 0 or right <= 0:
            return self._empty(left, right)
        if self.use_log:
            left, right = math.log(left), math.log(right)
        self._left.append(left)
        self._right.append(right)
        n = len(self._left)
        if n < self.min_obs:
            return self._empty(left, right)

        result = stats.ols(list(self._left), list(self._right), intercept=True)
        alpha, beta = result["coef"][0], result["coef"][1]
        residuals = result["residuals"]
        current = residuals[-1]
        mean_r = stats.mean(residuals)
        std_r = stats.std(residuals)
        z = (current - mean_r) / std_r if std_r > 0 else 0.0
        half_life = stats.half_life(residuals) if n >= 10 else 0.0
        adf_t = stats.adf_tstat(residuals) if self.adf and n >= 20 else 0.0
        out = {
            "value": current,
            "spread": current,
            "beta": beta,
            "alpha": alpha,
            "z": z,
            "mean": mean_r,
            "std": std_r,
            "half_life": half_life,
            "r2": result["r2"],
            "adf_t": adf_t,
            "ready": 1.0 if std_r > 0 else 0.0,
        }
        self._last = out
        return out

    def _empty(self, left: float, right: float) -> dict[str, Any]:
        out = {
            "value": 0.0,
            "spread": 0.0,
            "beta": 1.0,
            "alpha": 0.0,
            "z": 0.0,
            "mean": 0.0,
            "std": 0.0,
            "half_life": 0.0,
            "r2": 0.0,
            "adf_t": 0.0,
            "ready": 0.0,
            "left": left,
            "right": right,
        }
        self._last = out
        return out

    def state(self) -> dict[str, Any]:
        return {"left": list(self._left), "right": list(self._right), "last": self._last}

    def load_state(self, state: Mapping[str, Any]) -> None:
        self._left = deque(state.get("left", []), maxlen=self.window)
        self._right = deque(state.get("right", []), maxlen=self.window)
        self._last = dict(state.get("last", {}))

    def params(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "window": self.window,
            "min_obs": self.min_obs,
            "use_log": self.use_log,
            "adf": self.adf,
        }


class Hurst(Node):
    kind = "hurst"

    def __init__(self, source: str, window: int = 120, min_window: int = 8) -> None:
        super().__init__()
        if window < 16:
            raise ValueError("window must be >= 16")
        self.source = source
        self.window = window
        self.min_window = min_window
        self._buffer: deque[float] = deque(maxlen=window)

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    @property
    def ready(self) -> bool:
        return len(self._buffer) >= self.window

    def update(self, obs, inputs):
        self._buffer.append(self.read(inputs, self.source))
        value = stats.hurst_rs(self._buffer, min_window=self.min_window) if self.ready else 0.5
        return {"value": value, "ready": 1.0 if self.ready else 0.0}

    def reset(self):
        self._buffer.clear()

    def state(self):
        return {"buffer": list(self._buffer)}

    def load_state(self, state):
        self._buffer = deque(state.get("buffer", []), maxlen=self.window)

    def params(self):
        return {"source": self.source, "window": self.window, "min_window": self.min_window}


class CUSUM(Node):
    kind = "cusum"

    def __init__(self, source: str, threshold: float = 5.0, drift: float = 0.5) -> None:
        super().__init__()
        if threshold <= 0 or drift < 0:
            raise ValueError("threshold must be positive and drift non-negative")
        self.source = source
        self.threshold = threshold
        self.drift = drift
        self._pos = 0.0
        self._neg = 0.0

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    def update(self, obs, inputs):
        value = self.read(inputs, self.source)
        self._pos = max(0.0, self._pos + value - self.drift)
        self._neg = max(0.0, self._neg - value - self.drift)
        alarm = 0
        if self._pos > self.threshold:
            alarm = 1
            self._pos = 0.0
        elif self._neg > self.threshold:
            alarm = -1
            self._neg = 0.0
        return {
            "value": alarm,
            "alarm": alarm,
            "cusum_pos": self._pos,
            "cusum_neg": self._neg,
            "input": value,
        }

    def reset(self):
        self._pos = 0.0
        self._neg = 0.0

    def state(self):
        return {"pos": self._pos, "neg": self._neg}

    def load_state(self, state):
        self._pos = float(state.get("pos", 0.0))
        self._neg = float(state.get("neg", 0.0))

    def params(self):
        return {"source": self.source, "threshold": self.threshold, "drift": self.drift}


class HPFilter(Node):
    """Rolling two-sided Hodrick–Prescott trend.

    Uses a window ending at the current bar, so it does not see the future, but
    the trend estimate can revise as new bars arrive.
    """

    kind = "hp_filter"

    def __init__(self, source: str, window: int = 64, lam: float = 1600.0) -> None:
        super().__init__()
        if window < 8:
            raise ValueError("window must be >= 8")
        if lam <= 0:
            raise ValueError("lambda must be positive")
        self.source = source
        self.window = window
        self.lam = lam
        self._buffer: deque[float] = deque(maxlen=window)

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    @property
    def ready(self) -> bool:
        return len(self._buffer) >= self.window

    def update(self, obs, inputs):
        value = self.read(inputs, self.source)
        self._buffer.append(value)
        if not self.ready:
            return {"value": 0.0, "trend": 0.0, "cycle": 0.0, "ready": 0.0}
        trend = stats.hp_trend(self._buffer, lam=self.lam)[-1]
        return {"value": value - trend, "trend": trend, "cycle": value - trend, "ready": 1.0}

    def reset(self):
        self._buffer.clear()

    def state(self):
        return {"buffer": list(self._buffer)}

    def load_state(self, state):
        self._buffer = deque(state.get("buffer", []), maxlen=self.window)

    def params(self):
        return {"source": self.source, "window": self.window, "lam": self.lam}


class GARCH11(Node):
    """GARCH(1,1) conditional variance with fixed parameters.

    Parameters can be tuned by the optimizer or supplied from a calibration run;
    online MLE is intentionally not hidden inside the update path.
    """

    kind = "garch11"

    def __init__(
        self,
        source: str,
        *,
        omega: float = 1e-6,
        alpha: float = 0.08,
        beta: float = 0.9,
        warmup: int = 20,
    ) -> None:
        super().__init__()
        if omega <= 0 or alpha < 0 or beta < 0 or alpha + beta >= 1:
            raise ValueError("require omega > 0, alpha/beta >= 0, alpha + beta < 1")
        self.source = source
        self.omega = omega
        self.alpha = alpha
        self.beta = beta
        self.warmup = max(1, warmup)
        self._variance: float | None = None
        self._n = 0

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    @property
    def ready(self) -> bool:
        return self._n >= self.warmup and self._variance is not None

    def update(self, obs, inputs):
        value = self.read(inputs, self.source)
        if self._variance is None:
            self._variance = max(self.omega, value * value)
        else:
            self._variance = self.omega + self.alpha * value * value + self.beta * self._variance
        self._n += 1
        variance = max(1e-18, self._variance)
        return {
            "value": math.sqrt(variance),
            "variance": variance,
            "z": value / math.sqrt(variance),
            "ready": 1.0 if self.ready else 0.0,
        }

    def reset(self):
        self._variance = None
        self._n = 0

    def state(self):
        return {"variance": self._variance, "n": self._n}

    def load_state(self, state):
        self._variance = state.get("variance")
        self._n = int(state.get("n", 0))

    def params(self):
        return {
            "source": self.source,
            "omega": self.omega,
            "alpha": self.alpha,
            "beta": self.beta,
            "warmup": self.warmup,
        }


class RollingPCA(Node):
    """Project a multivariate window onto its first principal component."""

    kind = "rolling_pca"

    def __init__(self, sources: list[str], *, window: int = 60, min_obs: int | None = None) -> None:
        super().__init__()
        if len(sources) < 2:
            raise ValueError("PCA needs at least two sources")
        if window < 10:
            raise ValueError("window must be >= 10")
        self.sources = list(sources)
        self.window = window
        self.min_obs = min_obs or max(10, window // 2)
        self._buffers: list[deque[float]] = [deque(maxlen=window) for _ in sources]
        self._last: dict[str, Any] = {}

    @property
    def input_nodes(self):
        return self.input_names(*self.sources)

    @property
    def ready(self) -> bool:
        return (
            len(self._buffers[0]) >= self.min_obs
            and self._last.get("ready", 0.0) == 1.0
            if self._buffers
            else False
        )

    def reset(self):
        for buffer in self._buffers:
            buffer.clear()
        self._last = {}

    def update(self, obs, inputs):
        values = [self.read(inputs, ref) for ref in self.sources]
        if not all(math.isfinite(value) for value in values):
            return self._empty()
        for buffer, value in zip(self._buffers, values):
            buffer.append(value)
        n = len(self._buffers[0])
        if n < self.min_obs:
            return self._empty()

        columns = [list(buffer) for buffer in self._buffers]
        means = [stats.mean(column) for column in columns]
        centered = [[column[i] - means[j] for j, column in enumerate(columns)] for i in range(n)]
        covariance = linalg.zeros(len(columns), len(columns))
        for i in range(len(columns)):
            for j in range(i, len(columns)):
                cov = sum(centered[k][i] * centered[k][j] for k in range(n)) / (n - 1)
                covariance[i][j] = covariance[j][i] = cov
        eigenvalues, eigenvectors = linalg.jacobi_eigen(covariance)
        order = sorted(range(len(eigenvalues)), key=lambda i: eigenvalues[i], reverse=True)
        top = order[0]
        vector = [eigenvectors[i][top] for i in range(len(columns))]
        # Fix the sign so the projection is stable across windows.
        if sum(vector) < 0:
            vector = [-v for v in vector]
        current = [values[i] - means[i] for i in range(len(values))]
        projection = linalg.dot(current, vector)
        total = sum(max(0.0, value) for value in eigenvalues)
        explained = eigenvalues[top] / total if total > 0 else 0.0
        out: dict[str, Any] = {
            "value": projection,
            "explained": explained,
            "ready": 1.0,
        }
        for i, loading in enumerate(vector):
            out[f"loading{i}"] = loading
        self._last = out
        return out

    def _empty(self):
        out = {"value": 0.0, "explained": 0.0, "ready": 0.0}
        for i in range(len(self.sources)):
            out[f"loading{i}"] = 0.0
        self._last = out
        return out

    def state(self):
        return {"buffers": [list(buffer) for buffer in self._buffers], "last": self._last}

    def load_state(self, state):
        for buffer, values in zip(self._buffers, state.get("buffers", [])):
            buffer.clear()
            buffer.extend(values)
        self._last = dict(state.get("last", {}))

    def params(self):
        return {"sources": list(self.sources), "window": self.window, "min_obs": self.min_obs}


class HaarTrend(Node):
    """Rolling Haar approximation and detail energy.

    A pure-Python alternative to a full wavelet package: recursively average
    adjacent points in the window and accumulate the squared detail
    coefficients. The approximation is a multi-scale smoother; the detail
    energy is a compact volatility/roughness feature.
    """

    kind = "haar_trend"

    def __init__(self, source: str, *, level: int = 3) -> None:
        super().__init__()
        if level < 1 or level > 10:
            raise ValueError("level must be in [1, 10]")
        self.source = source
        self.level = level
        self.window = 2 ** level
        self._buffer: deque[float] = deque(maxlen=self.window)

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    @property
    def ready(self) -> bool:
        return len(self._buffer) >= self.window

    def update(self, obs, inputs):
        value = self.read(inputs, self.source)
        self._buffer.append(value)
        if not self.ready:
            return {"value": 0.0, "approximation": 0.0, "detail_energy": 0.0, "ready": 0.0}
        approximation = list(self._buffer)
        detail_energy = 0.0
        for _ in range(self.level):
            next_level = []
            for i in range(0, len(approximation) - 1, 2):
                average = (approximation[i] + approximation[i + 1]) / 2.0
                detail = (approximation[i] - approximation[i + 1]) / 2.0
                next_level.append(average)
                detail_energy += detail * detail
            approximation = next_level
        trend = approximation[0] if approximation else value
        return {
            "value": trend,
            "approximation": trend,
            "detail_energy": detail_energy,
            "ready": 1.0,
        }

    def reset(self):
        self._buffer.clear()

    def state(self):
        return {"buffer": list(self._buffer)}

    def load_state(self, state):
        self._buffer = deque(state.get("buffer", []), maxlen=self.window)

    def params(self):
        return {"source": self.source, "level": self.level}


JOHANSEN_CRITICAL_95 = {
    2: [15.41, 3.76],
    3: [29.68, 15.41, 3.76],
    4: [47.21, 29.68, 15.41, 3.76],
    5: [62.99, 47.21, 29.68, 15.41, 3.76],
}


def johansen_test(levels: list[list[float]], *, critical_95: dict | None = None) -> dict[str, Any]:
    """Johansen trace test on a matrix of levels (rows are observations).

    One lag, centered levels, approximate 95% critical values. Returns an empty
    dict when the covariance matrices are singular. The first cointegrating
    vector is normalized on its first weight.
    """
    n = len(levels)
    if n < 10 or not levels or not levels[0]:
        return {}
    k = len(levels[0])
    if k < 2 or any(len(row) != k for row in levels):
        return {}
    deltas = [
        [levels[i][j] - levels[i - 1][j] for j in range(k)] for i in range(1, n)
    ]
    lagged = [list(row) for row in levels[:-1]]
    for rows in (deltas, lagged):
        means = [stats.mean([row[j] for row in rows]) for j in range(k)]
        for row in rows:
            for j in range(k):
                row[j] -= means[j]
    count = len(deltas)
    if count < 3:
        return {}

    s00 = linalg.zeros(k, k)
    s01 = linalg.zeros(k, k)
    s11 = linalg.zeros(k, k)
    for i in range(k):
        for j in range(k):
            s00[i][j] = sum(deltas[t][i] * deltas[t][j] for t in range(count)) / (count - 1)
            s01[i][j] = sum(deltas[t][i] * lagged[t][j] for t in range(count)) / (count - 1)
            s11[i][j] = sum(lagged[t][i] * lagged[t][j] for t in range(count)) / (count - 1)
    s10 = linalg.transpose(s01)
    ridge = max(1e-10, 1e-10 * (sum(s11[i][i] for i in range(k)) / k))
    for i in range(k):
        s00[i][i] += ridge
        s11[i][i] += ridge
    try:
        lower_11 = linalg.cholesky(s11)
        lower_00 = linalg.cholesky(s00)
        inv_11 = linalg.inverse(lower_11)
        inv_00 = linalg.inverse(lower_00)
        b_matrix = linalg.matmul(linalg.matmul(inv_11, s10), linalg.transpose(inv_00))
        bbt = linalg.matmul(b_matrix, linalg.transpose(b_matrix))
        eigenvalues, eigenvectors = linalg.jacobi_eigen(bbt)
    except ValueError:
        return {}

    order = sorted(range(k), key=lambda i: eigenvalues[i], reverse=True)
    eigenvalues = [max(0.0, min(0.999999999, eigenvalues[i])) for i in order]
    top_vector = [eigenvectors[i][order[0]] for i in range(k)]
    vector = linalg.matvec(linalg.transpose(inv_11), top_vector)
    scale = vector[0]
    if abs(scale) < 1e-8:
        scale = max(vector, key=abs)
    if abs(scale) > 1e-12:
        vector = [value / scale for value in vector]

    spread_series = [sum(vector[j] * levels[t][j] for j in range(k)) for t in range(n)]
    spread = spread_series[-1]
    mean_spread = stats.mean(spread_series)
    std_spread = stats.std(spread_series)
    z = (spread - mean_spread) / std_spread if std_spread > 0 else 0.0
    traces = [
        -n * sum(math.log1p(-eigenvalues[i]) for i in range(r, k)) for r in range(k)
    ]
    critical = (critical_95 or JOHANSEN_CRITICAL_95).get(k, [])
    rank = 0
    for r, critical_value in enumerate(critical[:k]):
        if traces[r] > critical_value:
            rank = r + 1
        else:
            break
    result: dict[str, Any] = {
        "spread": spread,
        "z": z,
        "spread_series": spread_series,
        "half_life": stats.half_life(spread_series),
        "rank": rank,
        "trace0": traces[0] if traces else 0.0,
        "trace1": traces[1] if len(traces) > 1 else 0.0,
        "eigen0": eigenvalues[0] if eigenvalues else 0.0,
        "eigenvalues": eigenvalues,
        "weights": vector,
    }
    if k == 2:
        result["beta"] = -vector[1] / vector[0] if vector[0] else 0.0
    return result


class Johansen(Node):
    """Johansen cointegration test and first cointegrating vector.

    The implementation uses one lag in the short-run dynamics and a constant
    removed by centering. It is intended for small baskets (2–5 series) and
    reports trace statistics plus approximate 95% critical values.
    """

    kind = "johansen"
    CRITICAL_95 = JOHANSEN_CRITICAL_95

    def __init__(
        self,
        sources: list[str],
        *,
        window: int = 120,
        min_obs: int | None = None,
        use_log: bool = True,
        lags: int = 1,
    ) -> None:
        super().__init__()
        if len(sources) < 2:
            raise ValueError("Johansen needs at least two sources")
        if lags != 1:
            raise ValueError("Pearl's Johansen implementation currently supports lags=1")
        if window < 30:
            raise ValueError("window must be >= 30")
        self.sources = list(sources)
        self.window = window
        self.min_obs = min_obs or max(40, window // 2)
        self.use_log = use_log
        self.lags = lags
        self._buffers: list[deque[float]] = [deque(maxlen=window) for _ in sources]
        self._last: dict[str, Any] = {}

    @property
    def input_nodes(self):
        return self.input_names(*self.sources)

    @property
    def ready(self) -> bool:
        return (
            len(self._buffers[0]) >= self.min_obs
            and self._last.get("ready", 0.0) == 1.0
            if self._buffers
            else False
        )

    def reset(self):
        for buffer in self._buffers:
            buffer.clear()
        self._last = {}

    def update(self, obs, inputs):
        values = [self.read(inputs, ref) for ref in self.sources]
        if not all(math.isfinite(value) for value in values):
            return self._empty()
        if self.use_log:
            if any(value <= 0 for value in values):
                return self._empty()
            values = [math.log(value) for value in values]
        for buffer, value in zip(self._buffers, values):
            buffer.append(value)
        n = len(self._buffers[0])
        if n < self.min_obs:
            return self._empty()
        levels = [[buffer[i] for buffer in self._buffers] for i in range(n)]
        result = johansen_test(levels, critical_95=self.CRITICAL_95)
        if not result:
            return self._empty()
        out = {key: value for key, value in result.items() if key != "spread_series"}
        out["value"] = out["spread"]
        out["ready"] = 1.0
        self._last = out
        return out

    def _empty(self):
        out = {
            "value": 0.0,
            "spread": 0.0,
            "beta": 1.0,
            "z": 0.0,
            "half_life": 0.0,
            "rank": 0,
            "trace0": 0.0,
            "trace1": 0.0,
            "eigen0": 0.0,
            "ready": 0.0,
        }
        for i in range(len(self.sources)):
            out[f"weight{i}"] = 1.0 if i == 0 else 0.0
        self._last = out
        return out

    def state(self):
        return {"buffers": [list(buffer) for buffer in self._buffers], "last": self._last}

    def load_state(self, state):
        for buffer, values in zip(self._buffers, state.get("buffers", [])):
            buffer.clear()
            buffer.extend(values)
        self._last = dict(state.get("last", {}))

    def params(self):
        return {
            "sources": list(self.sources),
            "window": self.window,
            "min_obs": self.min_obs,
            "use_log": self.use_log,
            "lags": self.lags,
        }
