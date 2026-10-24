"""Model builder and runtime.

A model is a DAG of named, stateful nodes. The builder enforces that a node only
references earlier nodes, which gives a deterministic execution order and makes
look-ahead impossible by construction: no node can read a future output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .nodes.base import Node
from .types import Observation, PearlSnapshot, split_ref


class ModelBuilder:
    def __init__(self) -> None:
        self._nodes: list[Node] = []
        self._names: set[str] = set()

    def add(self, name: str, node: Node) -> ModelBuilder:
        if not name or "." in name:
            raise ValueError(f"node name must be non-empty and contain no dots: {name!r}")
        if name in self._names:
            raise ValueError(f"duplicate node name: {name!r}")
        for ref in node.input_nodes:
            upstream, _ = split_ref(ref)
            if upstream not in self._names:
                raise ValueError(
                    f"node {name!r} references {upstream!r} before it is defined"
                )
        node.name = name
        self._nodes.append(node)
        self._names.add(name)
        return self

    def build(self) -> PearlModel:
        if not self._nodes:
            raise ValueError("cannot build an empty model")
        return PearlModel(list(self._nodes))

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": node.name,
                "type": node.kind,
                "inputs": list(node.input_nodes),
                "params": node.params(),
            }
            for node in self._nodes
        ]


class PearlModel:
    def __init__(self, nodes: list[Node]) -> None:
        if not nodes:
            raise ValueError("model needs at least one node")
        self.nodes = list(nodes)
        self.by_name = {node.name: node for node in self.nodes}
        if len(self.by_name) != len(self.nodes):
            raise ValueError("duplicate node names")
        self.last_snapshot: PearlSnapshot | None = None

    def update(self, obs: Observation) -> PearlSnapshot:
        outputs: dict[str, dict[str, Any]] = {}
        ready = True
        for node in self.nodes:
            inputs = {name: outputs[name] for name in node.input_nodes}
            try:
                node_output = node.update(obs, inputs)
            except Exception as exc:  # noqa: BLE001 - add context to node failures
                raise RuntimeError(f"node {node.name!r} failed: {exc}") from exc
            outputs[node.name] = node_output
            ready = ready and node.ready
        snapshot = PearlSnapshot(ts=obs.ts, outputs=outputs, ready=ready)
        self.last_snapshot = snapshot
        return snapshot

    def transform(
        self, observations: Iterable[Observation], *, reset: bool = True
    ) -> list[PearlSnapshot]:
        if reset:
            self.reset()
        return [self.update(obs) for obs in observations]

    def reset(self) -> None:
        for node in self.nodes:
            node.reset()
        self.last_snapshot = None

    def state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "nodes": {node.name: node.state() for node in self.nodes},
        }

    def load_state(self, state: dict[str, Any]) -> None:
        if state.get("version") != 1:
            raise ValueError(f"unsupported Pearl state version: {state.get('version')!r}")
        nodes = state.get("nodes") or {}
        known = set(self.by_name)
        unknown = set(nodes) - known
        if unknown:
            raise ValueError(f"state contains unknown nodes: {sorted(unknown)}")
        for name, node_state in nodes.items():
            self.by_name[name].load_state(node_state)

    def save_state(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state(), indent=2), encoding="utf-8")
        tmp.replace(target)

    def load_state_file(self, path: str | Path) -> None:
        self.load_state(json.loads(Path(path).read_text(encoding="utf-8")))

    def describe(self) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "name": node.name,
                    "type": node.kind,
                    "inputs": list(node.input_nodes),
                    "params": node.params(),
                }
                for node in self.nodes
            ]
        }

    def __len__(self) -> int:
        return len(self.nodes)
