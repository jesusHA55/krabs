"""Signal nodes: turn filtered values into discrete sides with hysteresis."""

from __future__ import annotations

from typing import Any, Mapping

from ..nodes.base import Node
from ..types import Observation


class Threshold(Node):
    """Z-score threshold with hysteresis.

    ``side`` is +1 for long the spread, -1 for short, 0 for flat. For a pairs
    model, +1 means long the left leg and short ``beta`` of the right leg.
    """

    kind = "threshold"

    def __init__(
        self,
        source: str,
        *,
        entry: float = 2.0,
        exit: float = 0.5,
        allow_short: bool = True,
    ) -> None:
        super().__init__()
        if entry <= 0:
            raise ValueError("entry must be positive")
        if exit < 0 or exit >= entry:
            raise ValueError("exit must be in [0, entry)")
        self.source = source
        self.entry = entry
        self.exit = exit
        self.allow_short = allow_short
        self._side = 0

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    @property
    def ready(self) -> bool:
        return True

    def update(self, obs: Observation, inputs: Mapping[str, Mapping[str, Any]]):
        value = self.read(inputs, self.source)
        if self._side == 0:
            if value <= -self.entry:
                self._side = 1
            elif value >= self.entry and self.allow_short:
                self._side = -1
        elif self._side > 0 and value >= -self.exit:
            self._side = 0
        elif self._side < 0 and value <= self.exit:
            self._side = 0
        return {
            "value": self._side,
            "side": self._side,
            "z": value,
            "entry": self.entry,
            "exit": self.exit,
        }

    def reset(self):
        self._side = 0

    def state(self):
        return {"side": self._side}

    def load_state(self, state):
        self._side = int(state.get("side", 0))

    def params(self):
        return {
            "source": self.source,
            "entry": self.entry,
            "exit": self.exit,
            "allow_short": self.allow_short,
        }


class ZeroCross(Node):
    """Long when the source is positive, short when negative."""

    kind = "zero_cross"

    def __init__(self, source: str, allow_short: bool = False) -> None:
        super().__init__()
        self.source = source
        self.allow_short = allow_short

    @property
    def input_nodes(self):
        return self.input_names(self.source)

    def update(self, obs, inputs):
        value = self.read(inputs, self.source)
        side = 1 if value > 0 else (-1 if value < 0 and self.allow_short else 0)
        return {"value": side, "side": side, "raw": value}

    def params(self):
        return {"source": self.source, "allow_short": self.allow_short}
