"""Ornstein-Uhlenbeck / AR(1) spread filter as a Pearl node.

The local-level Kalman filter models the spread as a random walk, so its
standardised deviation is measured against a *drifting* level. This node
instead fits a mean-reverting AR(1) process on a trailing window:

    d_t = phi * d_{t-1} + w_t          (deviation from equilibrium)
    y_t = mu + d_t + v_t               (observed spread)

and runs a scalar Kalman filter on the deviation. ``phi``, ``mu``, the process
variance and the observation variance are re-estimated every ``refit`` bars from
the trailing ``window`` values, so the model stays causal and can follow a
slowly changing hedge.

Outputs: ``value`` (filtered spread), ``z`` (filtered deviation in standard
deviations), ``obs_z`` (raw deviation), ``innovation``/``innovation_z``,
``phi``, ``half_life`` (bars), ``mu``, ``sd`` and ``expected_reversion``.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any, Mapping

from ..nodes.base import Node
from ..types import Observation

_TINY = 1e-12


class OU(Node):
    kind = "ou"

    def __init__(
        self,
        source: str,
        *,
        window: int = 120,
        refit: int = 20,
        obs_ratio: float = 0.5,
        warmup: int = 5,
        max_phi: float = 0.999,
        min_phi: float = 0.005,
    ) -> None:
        super().__init__()
        if window < 20:
            raise ValueError("window must be >= 20")
        if refit < 1:
            raise ValueError("refit must be >= 1")
        if not 0 < obs_ratio < 1:
            raise ValueError("obs_ratio must be in (0, 1)")
        if not 0 < min_phi < max_phi < 1:
            raise ValueError("phi bounds must satisfy 0 < min_phi < max_phi < 1")
        self.source = source
        self.window = int(window)
        self.refit = int(refit)
        self.obs_ratio = float(obs_ratio)
        self.warmup = max(1, int(warmup))
        self.max_phi = float(max_phi)
        self.min_phi = float(min_phi)
        self._values: deque[float] = deque(maxlen=self.window)
        self._phi: float | None = None
        self._mu: float | None = None
        self._q = 0.0
        self._r = 0.0
        self._d = 0.0
        self._P = 0.0
        self._since_fit = 0
        self._steps = 0
        self._last: dict[str, Any] = {}

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    @property
    def ready(self) -> bool:
        return self._phi is not None and self._steps >= self.warmup

    def reset(self) -> None:
        self._values.clear()
        self._phi = None
        self._mu = None
        self._q = self._r = 0.0
        self._d = self._P = 0.0
        self._since_fit = self._steps = 0
        self._last = {}

    def update(self, obs: Observation, inputs: Mapping[str, Mapping[str, Any]]):
        y = self.read(inputs, self.source)
        if not math.isfinite(y):
            return dict(self._last) or self._empty_output()
        self._values.append(y)

        if self._phi is None or self._since_fit >= self.refit:
            self._fit()

        if self._phi is None:
            self._last = self._empty_output()
            return self._last

        phi, mu, q, r = self._phi, self._mu or 0.0, self._q, self._r
        d_pred = phi * self._d
        p_pred = phi * phi * self._P + q
        innovation = y - (mu + d_pred)
        s = p_pred + r
        gain = p_pred / s if s > 0 else 0.0
        self._d = d_pred + gain * innovation
        self._P = max(_TINY, (1.0 - gain) * p_pred)
        self._steps += 1
        self._since_fit += 1

        denom = math.sqrt(max(self._P + r, _TINY))
        stationary = q / max(1.0 - phi * phi, _TINY)
        sd = math.sqrt(max(stationary + r, _TINY))
        self._last = {
            "value": mu + self._d,
            "z": self._d / denom,
            "obs_z": (y - mu) / sd,
            "innovation": innovation,
            "innovation_z": innovation / math.sqrt(s) if s > 0 else 0.0,
            "phi": phi,
            "half_life": -math.log(2.0) / math.log(phi),
            "mu": mu,
            "sd": sd,
            "expected_reversion": -self._d,
            "ready": 1.0 if self.ready else 0.0,
        }
        return self._last

    # -- internals -------------------------------------------------------
    def _fit(self) -> None:
        self._since_fit = 0
        values = list(self._values)
        if len(values) < max(20, self.window // 2):
            return
        xs = values[:-1]
        ys = values[1:]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx <= _TINY:
            return
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        phi = sxy / sxx
        if not math.isfinite(phi) or not self.min_phi < phi < self.max_phi:
            return
        c = my - phi * mx
        mu = c / (1.0 - phi)
        residuals = [y - c - phi * x for x, y in zip(xs, ys)]
        sigma2 = sum(e * e for e in residuals) / len(residuals)
        if not math.isfinite(sigma2) or sigma2 <= 0:
            return
        q = max((1.0 - self.obs_ratio) * sigma2, _TINY)
        r = max(self.obs_ratio * sigma2, _TINY)
        if self._phi is None:
            self._d = 0.0
            self._P = q / max(1.0 - phi * phi, _TINY)
        self._phi, self._mu, self._q, self._r = phi, mu, q, r

    @staticmethod
    def _empty_output() -> dict[str, Any]:
        return {
            "value": 0.0,
            "z": 0.0,
            "obs_z": 0.0,
            "innovation": 0.0,
            "innovation_z": 0.0,
            "phi": 0.0,
            "half_life": 0.0,
            "mu": 0.0,
            "sd": 0.0,
            "expected_reversion": 0.0,
            "ready": 0.0,
        }

    def state(self) -> dict[str, Any]:
        return {
            "values": list(self._values),
            "phi": self._phi,
            "mu": self._mu,
            "q": self._q,
            "r": self._r,
            "d": self._d,
            "P": self._P,
            "since_fit": self._since_fit,
            "steps": self._steps,
        }

    def load_state(self, state: Mapping[str, Any]) -> None:
        self._values = deque(state.get("values", []), maxlen=self.window)
        self._phi = state.get("phi")
        self._mu = state.get("mu")
        self._q = float(state.get("q", 0.0))
        self._r = float(state.get("r", 0.0))
        self._d = float(state.get("d", 0.0))
        self._P = float(state.get("P", 0.0))
        self._since_fit = int(state.get("since_fit", 0))
        self._steps = int(state.get("steps", 0))
        self._last = {}

    def params(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "window": self.window,
            "refit": self.refit,
            "obs_ratio": self.obs_ratio,
            "warmup": self.warmup,
            "max_phi": self.max_phi,
            "min_phi": self.min_phi,
        }
