"""Strategy integration for Pearl.

Pearl never touches the broker. A ``PearlStrategy`` builds a model, feeds it one
observation per timestamp, and exposes the ready snapshot to the strategy.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, ClassVar

from ..events import Bar
from ..strategy.base import Strategy, StrategyContext
from .builder import PearlModel
from .types import Observation, PearlSnapshot


class PearlStrategy(Strategy):
    """Base class for strategies backed by a Pearl model.

    Subclasses implement ``build_model()`` and ``on_snapshot()``. For a pair,
    set ``symbols`` to the two legs; the model is updated once per timestamp,
    when the primary symbol's bar arrives. The backtester has already appended
    every symbol's bar for that timestamp to the context history before the
    strategy loop runs.
    """

    symbols: ClassVar[list[str]] = []
    state_path: ClassVar[str | None] = None
    persist_every: ClassVar[int] = 0

    def __init__(self, **params: object) -> None:
        super().__init__(**params)
        self.model: PearlModel | None = None
        self._primary: str | None = None
        self._updates = 0
        self._diagnostics: dict[str, Any] = {}
        self.log = logging.getLogger(f"krabs.pearl.{self.id}")

    def build_model(self) -> PearlModel:
        raise NotImplementedError

    def on_start(self, ctx: StrategyContext) -> None:
        self.model = self.build_model()
        if self.state_path:
            path = Path(self.state_path)
            if path.is_file():
                try:
                    self.model.load_state_file(path)
                    self.log.info("loaded Pearl state from %s", path)
                except (OSError, ValueError, KeyError) as exc:
                    self.log.warning("could not load Pearl state from %s: %s", path, exc)
        self._primary = self.symbols[0] if self.symbols else None

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        if self.model is None:
            return
        if self._primary is None:
            self._primary = bar.symbol
        if bar.symbol != self._primary:
            return

        symbols = list(self.symbols) or [bar.symbol]
        bars: list[Bar] = []
        for symbol in symbols:
            try:
                bars.append(ctx.bar(symbol))
            except Exception:  # noqa: BLE001 - missing leg means wait
                return
        if len({b.ts for b in bars}) != 1:
            return

        snapshot = self.model.update(Observation.from_bars(bars))
        self._updates += 1
        self._diagnostics = {
            "prediction_ts": snapshot.ts.isoformat(),
            "ready": snapshot.ready,
            "signals": {
                name: output.get("side")
                for name, output in snapshot.outputs.items()
                if "side" in output
            },
            "values": {
                name: {
                    key: value
                    for key, value in output.items()
                    if isinstance(value, (int, float)) and key not in {"ready"}
                }
                for name, output in snapshot.outputs.items()
            },
        }
        if self.state_path and self.persist_every > 0 and self._updates % self.persist_every == 0:
            self._save_state()
        if snapshot.ready:
            self.on_snapshot(ctx, snapshot)

    def on_snapshot(self, ctx: StrategyContext, snapshot: PearlSnapshot) -> None:
        """Called only when every node in the model has warmed up."""

    def on_end(self, ctx: StrategyContext) -> None:
        self._save_state()

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._diagnostics)

    def _save_state(self) -> None:
        if not self.state_path or self.model is None:
            return
        try:
            self.model.save_state(self.state_path)
        except OSError as exc:
            self.log.warning("could not save Pearl state to %s: %s", self.state_path, exc)
