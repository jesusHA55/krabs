"""Causal feature nodes: prices, returns, transforms and spreads."""

from __future__ import annotations

import math
from collections import deque
from typing import Any, Mapping

from .nodes.base import Node
from .types import Observation
from . import stats


class Series(Node):
    """Read one field from the observation for a fixed symbol."""

    kind = "series"

    def __init__(self, symbol: str, field: str = "close") -> None:
        super().__init__()
        self.symbol = symbol.upper()
        self.field = field

    def update(self, obs: Observation, inputs: Mapping[str, Mapping[str, Any]]):
        out = {name: obs.field(self.symbol, name) for name in ("open", "high", "low", "close", "volume")}
        out["value"] = out[self.field]
        return out

    def params(self):
        return {"symbol": self.symbol, "field": self.field}


class LogSeries(Series):
    """Natural log of one field, for models that work in log-price space."""

    kind = "log_series"

    def update(self, obs: Observation, inputs: Mapping[str, Mapping[str, Any]]):
        out = {name: obs.field(self.symbol, name) for name in ("open", "high", "low", "close", "volume")}
        price = out[self.field]
        out["value"] = math.log(price) if price > 0 else 0.0
        return out


class LogReturn(Node):
    kind = "log_return"

    def __init__(self, source: str, period: int = 1) -> None:
        super().__init__()
        if period < 1:
            raise ValueError("period must be >= 1")
        self.source = source
        self.period = period
        self._buffer: deque[float] = deque(maxlen=period + 1)

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    @property
    def ready(self) -> bool:
        return len(self._buffer) > self.period

    def update(self, obs, inputs):
        price = self.read(inputs, self.source)
        self._buffer.append(price)
        value = 0.0
        if len(self._buffer) > self.period and self._buffer[-self.period - 1] > 0 and price > 0:
            value = math.log(price / self._buffer[-self.period - 1])
        return {"value": value, "price": price}

    def reset(self):
        self._buffer.clear()

    def state(self):
        return {"buffer": list(self._buffer)}

    def load_state(self, state):
        self._buffer = deque(state.get("buffer", []), maxlen=self.period + 1)

    def params(self):
        return {"source": self.source, "period": self.period}


class SimpleReturn(LogReturn):
    kind = "simple_return"

    def update(self, obs, inputs):
        price = self.read(inputs, self.source)
        self._buffer.append(price)
        value = 0.0
        if len(self._buffer) > self.period and self._buffer[-self.period - 1]:
            value = price / self._buffer[-self.period - 1] - 1.0
        return {"value": value, "price": price}


class Difference(Node):
    kind = "difference"

    def __init__(self, source: str, period: int = 1) -> None:
        super().__init__()
        if period < 1:
            raise ValueError("period must be >= 1")
        self.source = source
        self.period = period
        self._buffer: deque[float] = deque(maxlen=period + 1)

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    @property
    def ready(self) -> bool:
        return len(self._buffer) > self.period

    def update(self, obs, inputs):
        value_now = self.read(inputs, self.source)
        self._buffer.append(value_now)
        value = value_now - self._buffer[0] if len(self._buffer) > self.period else 0.0
        return {"value": value}

    def reset(self):
        self._buffer.clear()

    def state(self):
        return {"buffer": list(self._buffer)}

    def load_state(self, state):
        self._buffer = deque(state.get("buffer", []), maxlen=self.period + 1)

    def params(self):
        return {"source": self.source, "period": self.period}


class ZScore(Node):
    kind = "zscore"

    def __init__(self, source: str, window: int = 60) -> None:
        super().__init__()
        if window < 2:
            raise ValueError("window must be >= 2")
        self.source = source
        self.window = window
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
        avg = stats.mean(self._buffer) if self._buffer else 0.0
        sigma = stats.std(self._buffer) if len(self._buffer) > 1 else 0.0
        z = (value - avg) / sigma if sigma > 0 else 0.0
        return {"value": z, "mean": avg, "std": sigma, "raw": value}

    def reset(self):
        self._buffer.clear()

    def state(self):
        return {"buffer": list(self._buffer)}

    def load_state(self, state):
        self._buffer = deque(state.get("buffer", []), maxlen=self.window)

    def params(self):
        return {"source": self.source, "window": self.window}


class EWMA(Node):
    kind = "ewma"

    def __init__(self, source: str, span: int = 20) -> None:
        super().__init__()
        if span < 1:
            raise ValueError("span must be >= 1")
        self.source = source
        self.span = span
        self.alpha = 2.0 / (span + 1.0)
        self._value: float | None = None

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    @property
    def ready(self) -> bool:
        return self._value is not None

    def update(self, obs, inputs):
        raw = self.read(inputs, self.source)
        self._value = raw if self._value is None else self.alpha * raw + (1 - self.alpha) * self._value
        return {"value": self._value, "raw": raw}

    def reset(self):
        self._value = None

    def state(self):
        return {"value": self._value}

    def load_state(self, state):
        self._value = state.get("value")

    def params(self):
        return {"source": self.source, "span": self.span}


class EWMVariance(Node):
    kind = "ewm_variance"

    def __init__(self, source: str, span: int = 20, min_periods: int = 5) -> None:
        super().__init__()
        if span < 1:
            raise ValueError("span must be >= 1")
        self.source = source
        self.span = span
        self.min_periods = max(1, min_periods)
        self.alpha = 2.0 / (span + 1.0)
        self._mean: float | None = None
        self._var: float | None = None
        self._n = 0

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    @property
    def ready(self) -> bool:
        return self._n >= self.min_periods and self._var is not None

    def update(self, obs, inputs):
        raw = self.read(inputs, self.source)
        if self._mean is None:
            self._mean = raw
            self._var = 0.0
        else:
            previous = self._mean
            self._mean = self.alpha * raw + (1 - self.alpha) * previous
            self._var = (1 - self.alpha) * (
                (self._var or 0.0) + self.alpha * (raw - previous) ** 2
            )
        self._n += 1
        variance = self._var or 0.0
        return {"value": math.sqrt(max(0.0, variance)), "variance": variance, "mean": self._mean}

    def reset(self):
        self._mean = None
        self._var = None
        self._n = 0

    def state(self):
        return {"mean": self._mean, "var": self._var, "n": self._n}

    def load_state(self, state):
        self._mean = state.get("mean")
        self._var = state.get("var")
        self._n = int(state.get("n", 0))

    def params(self):
        return {"source": self.source, "span": self.span, "min_periods": self.min_periods}


class Spread(Node):
    kind = "spread"

    def __init__(self, left: str, right: str, hedge: float = 1.0, log: bool = False) -> None:
        super().__init__()
        self.left = left
        self.right = right
        self.hedge = float(hedge)
        self.log = log

    @property
    def input_nodes(self):
        return self.input_names(self.left, self.right)

    def update(self, obs, inputs):
        left_value = self.read(inputs, self.left)
        right_value = self.read(inputs, self.right)
        if self.log:
            if left_value <= 0 or right_value <= 0:
                return {"value": 0.0, "left": left_value, "right": right_value}
            value = math.log(left_value) - self.hedge * math.log(right_value)
        else:
            value = left_value - self.hedge * right_value
        return {"value": value, "left": left_value, "right": right_value}

    def params(self):
        return {"left": self.left, "right": self.right, "hedge": self.hedge, "log": self.log}


class Ratio(Spread):
    kind = "ratio"

    def __init__(self, left: str, right: str) -> None:
        super().__init__(left, right, hedge=1.0, log=False)

    def update(self, obs, inputs):
        left_value = self.read(inputs, self.left)
        right_value = self.read(inputs, self.right)
        value = left_value / right_value if right_value else 0.0
        return {"value": value, "left": left_value, "right": right_value}

    def params(self):
        return {"left": self.left, "right": self.right}


class Combine(Node):
    kind = "combine"

    OPS = {"add", "sub", "mul", "div"}

    def __init__(self, left: str, right: str, op: str = "sub") -> None:
        super().__init__()
        if op not in self.OPS:
            raise ValueError(f"op must be one of {self.OPS}")
        self.left = left
        self.right = right
        self.op = op

    @property
    def input_nodes(self):
        return self.input_names(self.left, self.right)

    def update(self, obs, inputs):
        a = self.read(inputs, self.left)
        b = self.read(inputs, self.right)
        if self.op == "add":
            value = a + b
        elif self.op == "sub":
            value = a - b
        elif self.op == "mul":
            value = a * b
        else:
            value = a / b if b else 0.0
        return {"value": value, "left": a, "right": b}

    def params(self):
        return {"left": self.left, "right": self.right, "op": self.op}


class Lag(Node):
    kind = "lag"

    def __init__(self, source: str, period: int = 1) -> None:
        super().__init__()
        if period < 1:
            raise ValueError("period must be >= 1")
        self.source = source
        self.period = period
        self._buffer: deque[float] = deque(maxlen=period + 1)

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    @property
    def ready(self) -> bool:
        return len(self._buffer) > self.period

    def update(self, obs, inputs):
        current = self.read(inputs, self.source)
        self._buffer.append(current)
        value = self._buffer[0] if len(self._buffer) > self.period else 0.0
        return {"value": value, "current": current}

    def reset(self):
        self._buffer.clear()

    def state(self):
        return {"buffer": list(self._buffer)}

    def load_state(self, state):
        self._buffer = deque(state.get("buffer", []), maxlen=self.period + 1)

    def params(self):
        return {"source": self.source, "period": self.period}
