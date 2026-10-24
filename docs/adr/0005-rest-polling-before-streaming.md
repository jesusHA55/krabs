# ADR 0005 — REST polling before streaming

**Status:** accepted · **Date:** 2026-10

## Context

Alpaca offers a WebSocket stream with lower latency. A Pi on Wi-Fi experiences
reconnects, duplicate bars and silent stalls. Streaming adds stateful connection
management, heartbeats and subscription recovery to the critical path.

## Decision

Default to REST polling of completed bars at a configurable interval
(`feed.poll_seconds`). Treat a bar as usable only after its close timestamp.
Deduplicate by `(symbol, ts)`, discard stale bars after downtime, and halt when
data exceeds `risk.max_data_age_s`.

The feed interface (`history`/`latest_bars`) is transport-agnostic. A WebSocket
adapter can be added later without touching the runtime.

## Consequences

- Latency is bounded by the poll interval (15–30 s is fine for 5-min bars).
- Simpler failure modes; a failed poll is just a retry.
- No partial/in-progress bar handling beyond timestamp filtering.
- Bar strategies lose nothing material; tick strategies would need streaming.

## Alternatives

- WebSocket-first: better latency, more code and more ways to stall silently.
- Vendor SDK: extra dependency and its own release cadence on ARM.
