# ADR 0003 — Separate wallet from execution

**Status:** accepted · **Date:** 2026-10

## Context

"Simulated vs real wallet" could be implemented as a flag inside the broker, or
as a separate accounting object. The first couples PnL accounting to execution
details; the second risks dual bookkeeping in live mode.

## Decision

Model the wallet as the accounting boundary:

- `PaperWallet` owns cash/positions; the paper broker mutates it on fills.
- `BrokerWallet` mirrors Alpaca; the broker is authoritative, and `refresh()`
  replaces local state.

Both expose the same `snapshot()`/`position()`/`can_afford()` surface. Execution
adapters never compute equity; wallets never send orders.

## Consequences

- Strategies and risk are identical across venues.
- Paper vs live PnL comparison is apples-to-apples.
- Live mode requires reconciliation discipline; local fills are estimates
  between broker syncs.
- Average-cost accounting is deliberately simple; tax lots are future work.

## Alternatives

- One `Portfolio` inside the broker: simpler wiring, harder to test risk in
  isolation, easy to leak execution state into accounting.
- Two independent implementations: guaranteed drift.
