# ADR 0004 — File-based kill switch

**Status:** accepted · **Date:** 2026-10

## Context

When something is wrong, the operator needs one action that works over SSH,
survives a crash, needs no API, and cannot silently fail. An HTTP endpoint adds
a listener and an auth surface to a device with no inbound ports. A database
flag requires a working database.

## Decision

The kill switch is the presence of `var/KILL`. `krabs kill` writes it atomically
(timestamp + reason). The runtime checks it every cycle, cancels working orders
and rejects new ones. `SIGUSR1` does the same. Only a human clears it.

## Consequences

- Works offline, in an emergency, from any shell or watchdog.
- Visible in `krabs status`, `doctor` and the heartbeat.
- Accidentally left engaged → no trades (fail-safe direction).
- File permissions are the security boundary; the service user owns `var/`.

## Alternatives

- REST admin endpoint: attack surface, availability coupling.
- Broker-side cancel-all only: does not stop the process from re-entering.
