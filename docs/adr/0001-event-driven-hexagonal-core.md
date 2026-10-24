# ADR 0001 — Event-driven hexagonal core

**Status:** accepted · **Date:** 2026-10

## Context

A retail algo platform must run the same strategy against historical data, a
simulated wallet and a real broker. Coupling strategies to Alpaca would make
backtests impossible to trust and live code impossible to test.

## Decision

Use a hexagonal (ports and adapters) architecture with an event-driven core:

- The domain (`events.py`) is pure data.
- Ports (`ports.py`) define `DataFeed`, `ExecutionBroker`, `Wallet`, `Store`.
- Strategies receive a narrow `StrategyContext`, never an adapter.
- The engine consumes market events in timestamp order; brokers fill, risk
  gates, wallet accounts.

## Consequences

- New broker/feed = new adapter, no strategy changes.
- Backtest and live share the loop shape; differences are the clock and feed.
- Slight indirection cost; every boundary is explicit and testable.
- Multi-strategy books and streaming feeds fit without redesign.

## Alternatives

- **Framework-first (backtrader/vectorbt)**: faster to start, harder to make
  live-safe and auditable, heavy on a Pi.
- **Direct Alpaca calls from strategies**: simplest demo, untestable and unsafe.
