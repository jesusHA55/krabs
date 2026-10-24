"""Shared broker exceptions and helpers."""

from __future__ import annotations

import hashlib


class BrokerError(RuntimeError):
    """Transport/API failure. Retryable or fatal, callers decide."""


class OrderRejected(BrokerError):
    """The broker refused the order (not a transport failure)."""


def run_prefix(run_id: str) -> str:
    """Stable per-run namespace for order ids (8 hex chars)."""
    digest = hashlib.blake2s(run_id.encode("utf-8"), digest_size=6).hexdigest()
    return f"krabs-{digest}"


def client_order_id(run_id: str, seq: int) -> str:
    """Deterministic, idempotent client order id.

    Alpaca requires <=48 chars and [A-Za-z0-9-_]. Reusing the same id after a
    crash is what prevents duplicate orders. The run id is hashed rather than
    truncated: run ids share a date prefix, so truncation would collide across
    runs and the broker would reject the second order.
    """
    return f"{run_prefix(run_id)}-{seq:06d}"
