"""Ports (hexagonal architecture).

Strategies and the runtime depend only on these Protocols. Adapters live in
``krabs.brokers`` and ``krabs.feeds``. Swapping a simulated wallet for a live
broker is a wiring decision, never a strategy change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from .events import AccountState, Bar, Fill, Order, OrderIntent, Position


class HistoricalFeed(Protocol):
    """Backtest data source: completed bars grouped by timestamp."""

    def bars(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ): ...


class LiveFeed(Protocol):
    """Live data source: polled history plus the most recent closed bars."""

    def history(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Bar]: ...

    def latest_bars(
        self, symbols: Sequence[str], timeframe: str, lookback: int = 10
    ) -> list[Bar]: ...


class ExecutionBroker(Protocol):
    """Anything that can turn an order intent into a broker-side order."""

    def submit(self, intent: OrderIntent) -> Order: ...

    def cancel(self, client_id: str) -> bool: ...

    def get_order(self, client_id: str) -> Order | None: ...

    def open_orders(self) -> list[Order]: ...

    def account(self) -> AccountState: ...

    def positions(self) -> dict[str, Position]: ...


class Wallet(Protocol):
    """Accounting view used by risk and strategies.

    Paper wallets own the ledger; live wallets mirror the broker and treat the
    broker as the source of truth.
    """

    def snapshot(self, ts: datetime | None = None) -> AccountState: ...

    def positions(self) -> dict[str, Position]: ...

    def position(self, symbol: str) -> Position: ...

    def apply_fill(self, fill: Fill) -> None: ...

    def mark(self, symbol: str, price: float) -> None: ...

    def can_afford(
        self,
        symbol: str,
        side,
        qty: float,
        price: float,
        extra_buying_power: float = 0.0,
    ) -> tuple[bool, str]: ...


class Store(Protocol):
    """Durable audit trail: runs, orders, fills, equity, events."""

    def start_run(self, **kwargs) -> None: ...

    def record_order(self, run_id: str, order: Order) -> None: ...

    def record_fill(self, run_id: str, fill: Fill) -> None: ...

    def record_equity(self, run_id: str, account: AccountState) -> None: ...

    def log_event(self, run_id: str, level: str, topic: str, payload: dict) -> None: ...

    def finish_run(self, run_id: str, status: str, final_equity: float) -> None: ...

    def load_orders(self, run_id: str, *, open_only: bool = False) -> list[dict]: ...

    def close(self) -> None: ...
