"""Linear Kalman filters as Pearl nodes.

Supported models:

* ``local_level``   — scalar random walk observed with noise
* ``local_trend``   — level + slope (linear trend)
* ``ar1``           — AR(1) state observed with noise
* ``regression``    — time-varying linear regression coefficients

Parameters are fixed by default. ``adaptation="covariance"`` enables bounded
innovation covariance matching for the observation noise; the model stays
deterministic for a fixed bar sequence.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .. import linalg
from ..nodes.base import Node
from ..types import Observation


class Kalman(Node):
    kind = "kalman"

    MODELS = {"local_level", "local_trend", "ar1", "regression"}

    def __init__(
        self,
        source: str,
        model: str = "local_level",
        *,
        level_var: float = 1e-4,
        slope_var: float = 1e-6,
        obs_var: float = 1e-2,
        phi: float = 0.95,
        features: list[str] | None = None,
        intercept: bool = True,
        coeff_var: float = 1e-4,
        warmup: int = 5,
        adaptation: str = "fixed",
        forgetting: float = 0.98,
        min_obs_var: float = 1e-8,
        max_obs_var: float = 1e4,
        init_var: float = 1e6,
    ) -> None:
        super().__init__()
        if model not in self.MODELS:
            raise ValueError(f"model must be one of {self.MODELS}")
        if adaptation not in ("fixed", "covariance"):
            raise ValueError("adaptation must be 'fixed' or 'covariance'")
        if obs_var <= 0 or level_var < 0 or slope_var < 0 or coeff_var < 0:
            raise ValueError("variances must be non-negative and obs_var positive")
        if not 0 < forgetting < 1:
            raise ValueError("forgetting must be in (0, 1)")
        self.source = source
        self.model = model
        self.level_var = level_var
        self.slope_var = slope_var
        self.obs_var = obs_var
        self.phi = phi
        self.features = list(features or [])
        self.intercept = bool(intercept)
        self.coeff_var = coeff_var
        self.warmup = max(1, warmup)
        self.adaptation = adaptation
        self.forgetting = forgetting
        self.min_obs_var = min_obs_var
        self.max_obs_var = max_obs_var
        self.init_var = init_var
        self._x: list[float] | None = None
        self._P: linalg.Matrix | None = None
        self._R = obs_var
        self._n = 0
        self._loglik = 0.0
        self._last: dict[str, Any] = {}
        if model == "regression" and not self.features:
            raise ValueError("regression model requires at least one feature reference")

    @property
    def input_nodes(self):
        return self.input_names(self.source, *self.features)

    @property
    def ready(self) -> bool:
        return self._n >= self.warmup

    def reset(self) -> None:
        self._x = None
        self._P = None
        self._R = self.obs_var
        self._n = 0
        self._loglik = 0.0
        self._last = {}

    def update(self, obs: Observation, inputs: Mapping[str, Mapping[str, Any]]):
        z = self.read(inputs, self.source)
        if not math.isfinite(z):
            return dict(self._last) or {"value": 0.0, "ready": 0.0}
        feature_values = [self.read(inputs, ref) for ref in self.features]
        if self._x is None or self._P is None:
            self._initialize(z, feature_values)
            n = len(self._x)
        else:
            n = len(self._x)

        F, H, Q = self._system(obs.dt, feature_values)
        # Predict.
        x_pred = linalg.matvec(F, self._x)
        P_pred = linalg.add(
            linalg.matmul(linalg.matmul(F, self._P), linalg.transpose(F)), Q
        )
        P_pred = linalg.symmetrize(P_pred)

        # Update.
        hph = linalg.dot(H, linalg.matvec(P_pred, H))
        S = max(1e-15, hph + self._R)
        innovation = z - linalg.dot(H, x_pred)
        K = [sum(P_pred[i][j] * H[j] for j in range(n)) / S for i in range(n)]
        self._x = [x_pred[i] + K[i] * innovation for i in range(n)]
        KH = linalg.outer(K, H)
        A = linalg.sub(linalg.identity(n), KH)
        self._P = linalg.add(
            linalg.matmul(linalg.matmul(A, P_pred), linalg.transpose(A)),
            linalg.scale(linalg.outer(K, K), self._R),
        )
        self._P = linalg.symmetrize(self._P)
        self._loglik += -0.5 * (math.log(2 * math.pi * S) + innovation * innovation / S)

        if self.adaptation == "covariance" and self._n >= self.warmup:
            estimate = innovation * innovation - hph
            estimate = min(self.max_obs_var, max(self.min_obs_var, estimate))
            self._R = (
                self.forgetting * self._R + (1.0 - self.forgetting) * estimate
            )
            self._R = min(self.max_obs_var, max(self.min_obs_var, self._R))

        self._n += 1
        out = self._outputs(z, feature_values, innovation, S, obs.dt, H)
        self._last = out
        return out

    # -- internals -------------------------------------------------------
    def _initialize(self, z: float, features: list[float]) -> None:
        if self.model == "local_level":
            self._x = [z]
            self._P = [[max(self.obs_var, 1e-9)]]
        elif self.model == "local_trend":
            self._x = [z, 0.0]
            self._P = [[max(self.obs_var, 1e-9), 0.0], [0.0, 1.0]]
        elif self.model == "ar1":
            self._x = [z]
            self._P = [[max(self.obs_var, 1e-9)]]
        else:
            dim = len(features) + (1 if self.intercept else 0)
            self._x = [0.0] * dim
            self._P = linalg.diag([self.init_var] * dim)

    def _system(self, dt: float, features: list[float]):
        dt = max(1e-9, float(dt))
        if self.model == "local_level":
            return [[1.0]], [1.0], [[self.level_var]]
        if self.model == "local_trend":
            F = [[1.0, dt], [0.0, 1.0]]
            Q = [[self.level_var, 0.0], [0.0, self.slope_var]]
            return F, [1.0, 0.0], Q
        if self.model == "ar1":
            return [[self.phi]], [1.0], [[self.level_var]]
        dim = len(features) + (1 if self.intercept else 0)
        F = linalg.identity(dim)
        Q = linalg.diag([self.coeff_var] * dim)
        H = ([1.0] if self.intercept else []) + list(features)
        return F, H, Q

    def _outputs(
        self,
        z: float,
        features: list[float],
        innovation: float,
        S: float,
        dt: float,
        H: list[float],
    ) -> dict[str, Any]:
        x = self._x or [0.0]
        P = self._P or [[0.0]]
        out: dict[str, Any] = {
            "innovation": innovation,
            "innovation_z": innovation / math.sqrt(S) if S > 0 else 0.0,
            "loglik": self._loglik,
            "obs_var": self._R,
            "state_var": P[0][0],
            "ready": 1.0 if self.ready else 0.0,
        }
        if self.model == "local_level":
            out.update({"value": x[0], "level": x[0]})
        elif self.model == "local_trend":
            out.update(
                {
                    "value": x[0],
                    "level": x[0],
                    "slope": x[1],
                    "forecast": x[0] + x[1] * max(1e-9, dt),
                }
            )
        elif self.model == "ar1":
            out.update({"value": x[0], "level": x[0], "phi": self.phi})
        else:
            prediction = sum(x[i] * H[i] for i in range(len(x)))
            out.update(
                {
                    "value": prediction,
                    "prediction": prediction,
                    "residual": z - prediction,
                }
            )
            for i, coef in enumerate(x):
                out[f"coef{i}"] = coef
        return out

    def state(self) -> dict[str, Any]:
        return {
            "x": self._x,
            "P": self._P,
            "R": self._R,
            "n": self._n,
            "loglik": self._loglik,
        }

    def load_state(self, state: Mapping[str, Any]) -> None:
        self._x = list(state["x"]) if state.get("x") is not None else None
        self._P = [list(row) for row in state["P"]] if state.get("P") is not None else None
        self._R = float(state.get("R", self.obs_var))
        self._n = int(state.get("n", 0))
        self._loglik = float(state.get("loglik", 0.0))
        self._last = {}

    def params(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "model": self.model,
            "level_var": self.level_var,
            "slope_var": self.slope_var,
            "obs_var": self.obs_var,
            "phi": self.phi,
            "features": list(self.features),
            "intercept": self.intercept,
            "coeff_var": self.coeff_var,
            "warmup": self.warmup,
            "adaptation": self.adaptation,
            "forgetting": self.forgetting,
        }
