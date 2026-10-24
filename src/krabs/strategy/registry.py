"""Strategy registry: name -> class, with typed parameter overrides."""

from __future__ import annotations

from typing import Any

from .base import Strategy, StrategyInfo
from .params import flatten, set_path

_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    if not cls.id or cls.id == "strategy":
        raise ValueError(f"{cls.__name__} must define a unique `id`")
    if cls.id in _REGISTRY:
        raise ValueError(f"duplicate strategy id: {cls.id}")
    _REGISTRY[cls.id] = cls
    return cls


def get(name: str) -> type[Strategy]:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise KeyError(f"unknown strategy {name!r}; available: {known}") from exc


def build(name: str, params: dict[str, Any] | None = None) -> Strategy:
    cls = get(name)
    return cls(**(params or {}))


def describe() -> list[StrategyInfo]:
    return [
        StrategyInfo(id=cls.id, description=cls.description, defaults=dict(cls.defaults), cls=cls)
        for cls in sorted(_REGISTRY.values(), key=lambda c: c.id)
    ]


def coerce_params(name: str, raw: dict[str, str]) -> dict[str, Any]:
    """Turn ``--param fast=10`` strings into values typed like the defaults."""
    cls = get(name)
    defaults = flatten(cls.defaults)
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in defaults:
            raise KeyError(f"unknown parameter {key!r} for strategy {name!r}")
        default = defaults[key]
        try:
            if isinstance(default, bool):
                parsed = value.lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                parsed = int(value)
            elif isinstance(default, float):
                parsed = float(value)
            else:
                parsed = value
        except ValueError as exc:
            raise ValueError(f"cannot parse {key}={value!r} as {type(default).__name__}") from exc
        set_path(out, key, parsed)
    return out


def load_builtin() -> None:
    """Import bundled strategies so registration happens as a side effect."""
    from .examples import buy_hold, pca_residual, rsi_reversion, sma_cross  # noqa: F401
