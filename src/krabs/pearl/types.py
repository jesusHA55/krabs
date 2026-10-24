"""Core value types for Pearl.

Pearl consumes *observations* (one bar or a synchronized set of bars) and emits
immutable *snapshots* containing every node's output. Strategies never touch
node internals; they read named values and signals from the snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from ..events import Bar, ensure_utc


@dataclass(frozen=True, slots=True)
class Observation:
    """One or more symbols at the same timestamp.

    ``dt`` is the step size in whatever unit the model uses (usually 1 bar).
    It must be explicit because filters behave differently across daily bars,
    5-minute bars and overnight gaps.
    """

    ts: datetime
    data: Mapping[str, Mapping[str, float]]
    dt: float = 1.0

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("observation must contain at least one symbol")
        if self.dt <= 0:
            raise ValueError("dt must be positive")

    @classmethod
    def from_bar(cls, bar: Bar, *, dt: float = 1.0) -> Observation:
        return cls(
            ts=ensure_utc(bar.ts),
            data={
                bar.symbol: {
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
            },
            dt=dt,
        )

    @classmethod
    def from_bars(cls, bars: list[Bar] | tuple[Bar, ...], *, dt: float = 1.0) -> Observation:
        if not bars:
            raise ValueError("at least one bar is required")
        ts = ensure_utc(bars[0].ts)
        data: dict[str, dict[str, float]] = {}
        for bar in bars:
            if ensure_utc(bar.ts) != ts:
                raise ValueError("all bars in an observation must share a timestamp")
            if bar.symbol in data:
                raise ValueError(f"duplicate symbol in observation: {bar.symbol}")
            data[bar.symbol] = {
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
        return cls(ts=ts, data=data, dt=dt)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self.data)

    def field(self, symbol: str, name: str = "close") -> float:
        try:
            return float(self.data[symbol][name])
        except KeyError as exc:
            raise KeyError(f"{symbol}.{name} not present in observation") from exc

    def close(self, symbol: str) -> float:
        return self.field(symbol, "close")

    def with_dt(self, dt: float) -> Observation:
        return Observation(ts=self.ts, data=self.data, dt=dt)


@dataclass(frozen=True, slots=True)
class PearlSnapshot:
    ts: datetime
    outputs: Mapping[str, Mapping[str, Any]]
    ready: bool
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def output(self, node: str) -> Mapping[str, Any]:
        return self.outputs.get(node, {})

    def value(self, ref: str, default: float | None = None) -> float | None:
        node, field = split_ref(ref)
        value = self.outputs.get(node, {}).get(field)
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def signal(self, ref: str = "signal", field: str = "side") -> int:
        value = self.value(f"{ref}.{field}", 0.0)
        return int(value or 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "ready": self.ready,
            "outputs": {k: dict(v) for k, v in self.outputs.items()},
            "diagnostics": dict(self.diagnostics),
        }


def split_ref(ref: str) -> tuple[str, str]:
    """Split ``node.field`` into its parts. Bare names mean ``.value``."""
    if "." in ref:
        node, field = ref.rsplit(".", 1)
        if not node or not field:
            raise ValueError(f"invalid node reference: {ref!r}")
        return node, field
    return ref, "value"
