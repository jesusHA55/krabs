"""Nested strategy parameters and dotted paths.

Strategies may declare nested defaults::

    defaults = {
        "left": "AAPL",
        "right": "MSFT",
        "model": {"window": 60, "entry": 2.0},
    }

Operators and the optimizer can then address the same value as
``model.window``. This keeps flat strategies working unchanged while letting
Pearl models expose filter/estimator parameters without name collisions.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

_MISSING = object()


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    result: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def expand_paths(values: Mapping[str, Any]) -> dict[str, Any]:
    """Turn dotted keys into nested dictionaries, keeping literal dicts."""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, Mapping):
            value = expand_paths(value)
        set_path(out, key, value)
    return out


def flatten(values: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts into ``a.b.c`` keys."""
    out: dict[str, Any] = {}
    for key, value in values.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def empty_dict_paths(values: Mapping[str, Any], prefix: str = "") -> set[str]:
    """Paths whose default is an empty mapping, i.e. open key/value bags."""
    out: set[str] = set()
    for key, value in values.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            if not value:
                out.add(path)
            else:
                out.update(empty_dict_paths(value, path))
    return out


def unknown_paths(provided: Mapping[str, Any], defaults: Mapping[str, Any]) -> set[str]:
    provided_flat = flatten(provided)
    defaults_flat = flatten(defaults)
    open_paths = empty_dict_paths(defaults)
    unknown = set()
    for path in provided_flat:
        if path in defaults_flat:
            continue
        if any(path == prefix or path.startswith(prefix + ".") for prefix in open_paths):
            continue
        unknown.add(path)
    return unknown


def get_path(values: Mapping[str, Any], path: str, default: Any = _MISSING) -> Any:
    current: Any = values
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            if default is _MISSING:
                raise KeyError(path)
            return default
        current = current[part]
    return current


def set_path(values: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = values
    for part in parts[:-1]:
        existing = current.get(part)
        if not isinstance(existing, dict):
            existing = {}
            current[part] = existing
        current = existing
    existing = current.get(parts[-1])
    if isinstance(existing, dict) and isinstance(value, dict):
        current[parts[-1]] = deep_merge(existing, value)
    else:
        current[parts[-1]] = value
