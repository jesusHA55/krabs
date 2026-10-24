"""Node protocol and shared helpers."""

from __future__ import annotations

from abc import ABC
from typing import Any, ClassVar, Mapping

from ..types import Observation, split_ref


class Node(ABC):
    """A stateful cell in the model graph.

    Nodes receive the full output of each upstream node, keyed by node name.
    References may be ``"node"`` or ``"node.field"``; the field defaults to
    ``"value"``.
    """

    kind: ClassVar[str] = "node"

    def __init__(self) -> None:
        self.name = ""

    @property
    def input_nodes(self) -> tuple[str, ...]:
        return ()

    @property
    def ready(self) -> bool:
        return True

    def update(
        self, obs: Observation, inputs: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, Any]:
        raise NotImplementedError

    def reset(self) -> None:
        """Return to the state before the first observation."""

    def state(self) -> dict[str, Any]:
        return {}

    def load_state(self, state: Mapping[str, Any]) -> None:
        """Restore state produced by :meth:`state`."""

    def params(self) -> dict[str, Any]:
        return {}

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def input_names(*refs: str | None) -> tuple[str, ...]:
        names = []
        for ref in refs:
            if not ref:
                continue
            node, _ = split_ref(ref)
            if node not in names:
                names.append(node)
        return tuple(names)

    @staticmethod
    def read(
        inputs: Mapping[str, Mapping[str, Any]],
        ref: str,
        default: float = 0.0,
    ) -> float:
        node, field = split_ref(ref)
        try:
            value = inputs[node][field]
            return float(value)
        except (KeyError, TypeError, ValueError):
            return default

    @staticmethod
    def read_raw(
        inputs: Mapping[str, Mapping[str, Any]], ref: str
    ) -> Any:
        node, field = split_ref(ref)
        return inputs.get(node, {}).get(field)


class Identity(Node):
    kind = "identity"

    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source

    @property
    def input_nodes(self) -> tuple[str, ...]:
        return self.input_names(self.source)

    def update(self, obs, inputs):
        return {"value": self.read(inputs, self.source)}
