# Krabs — Architecture

**A single-process, event-driven algorithmic trading platform designed to run
unattended on a Raspberry Pi, with simulated and real wallets that share one
strategy API.**

This document is the framework definition: what the system is, how its parts
fit, and where the safety boundaries are. It intentionally specifies *one*
opinionated design instead of a menu.

---

## 1. Goals and non-goals

### Goals

1. **One strategy, four venues.** Backtest, local paper, broker paper and live
   trading run the same strategy code. Promotion is a config change.
2. **No look-ahead by construction.** A decision made on bar `t` can only fill
   on bar `t+1`. The engine makes this impossible to violate accidentally.
3. **Simulation fidelity where it matters.** Slippage, half-spread, commissions,
   volume participation, partial fills, limit/stop triggers, TIF semantics and
   buying-power checks are modelled locally — not assumed away.
4. **Safe by default on real money.** Simulated wallet is the default. Live
   trading requires an explicit CLI flag, a non-paper config, an enabled audit
   trail, and an operator-cleared kill switch.
5. **Unattended operation.** Survives reboots, network drops, broker outages,
   stale feeds, disk pressure and thermal throttling without losing state or
   duplicating orders.
6. **Auditable.** Every order, fill, equity mark, risk rejection and halt is in
   SQLite and structured logs.
7. **Runs on a £60 computer.** Stdlib-only Python core; an optional Rust core
   for numeric hot paths; no database server, no message broker, no Docker
   requirement.

### Non-goals

- HFT, colocation, or sub-millisecond execution. The Pi is the wrong tool and
  the polling/REST design reflects that.
- Options, futures, crypto and FX in v1. US equities cash accounts first.
- Multi-tenant or multi-user operation. One Pi, one account, one process.
- A GUI. The interface is a CLI plus files (heartbeat, kill switch, reports).
- Tax lots, wash-sale accounting, T+1 settlement simulation, corporate actions.
  Average cost only; document and extend if needed.
- A general-purpose backtesting framework (no tick-level L2 book replay).

---

## 2. Design principles

| Principle | Consequence in the code |
|---|---|
| **Hexagonal / ports and adapters** | Strategies depend on `StrategyContext`; execution depends on `ExecutionBroker`; data depends on the feed ports. Alpaca is an adapter, never the core. |
| **One clock** | Nothing calls `datetime.now()` in the engine. `Clock` is injected: `SimulatedClock` in backtests, `RealClock` in production. |
| **Immutable market facts** | `Bar`, `Quote`, `Trade`, `Fill`, `OrderIntent` are frozen dataclasses. Mutable state is confined to `Order`, `Position`, `Wallet`. |
| **Truth has one owner** | Simulated wallet owns its ledger. Live wallet mirrors the broker and loses every disagreement. |
| **Fail closed** | Missing price, missing data, unknown config key, missing credentials, stale feed, duplicate client id → refuse to trade, loudly. |
| **Small hot path, boring control path** | Rust only for numeric kernels. Orchestration, risk rules and I/O stay in Python where they are easy to audit. |
| **Files over servers** | Kill switch, heartbeat, reports and SQLite are files. Works over SSH, survives crashes, needs no daemon. |

---

## 3. Operating modes

| Mode | Market data | Wallet | Execution | Purpose |
|---|---|---|---|---|
| `backtest` | Synthetic / CSV / Alpaca history | `PaperWallet` | `PaperBroker` (replays bars as fast as possible) | Research, CI, parameter sweeps |
| `paper` | Alpaca live (polled) | `PaperWallet` | `PaperBroker` (fills against live bars) | Validate behaviour with zero broker risk |
| `broker-paper` | Alpaca live | `BrokerWallet` | `AlpacaBroker` → paper account | Validate API integration, order lifecycle, reconciliation |
| `live` | Alpaca live | `BrokerWallet` | `AlpacaBroker` → real account | Real money |

Both simulated flavours matter. `paper` isolates **strategy logic** (no broker
quirks, instant fills at next bar, deterministic). `broker-paper` isolates
**integration** (real order states, partial fills, rejects, buying power). A
strategy is only promotable after both, in that order.

```
backtest ──► paper ──► broker-paper ──► live
   logic      logic       plumbing        money
```

---

## 4. System context

```
                         ┌──────────────────────────────────────────────┐
                         │                Raspberry Pi                  │
                         │                                              │
  Alpaca REST  ◄────────►│  krabs process (systemd, one instance)       │
  (orders,               │                                              │
   account,              │  ┌──────────┐   ┌───────────┐   ┌─────────┐  │
   positions)            │  │  feed    │──►│  engine   │──►│  risk   │  │
                         │  │ adapter  │   │ (runtime  │   │ engine  │  │
  Alpaca data  ◄────────►│  └──────────┘   │  or       │   └────┬────┘  │
  (bars)                 │                 │  backtest)│        │       │
                         │                 └────┬──────┘        │       │
                         │                      │               │       │
                         │              ┌───────▼───────┐ ┌─────▼─────┐ │
                         │              │    broker     │ │  wallet   │ │
                         │              │ paper|alpaca  │ │ paper|brk │ │
                         │              └───────┬───────┘ └───────────┘ │
                         │                      │                       │
                         │        ┌─────────────▼─────────────┐         │
                         │        │ SQLite audit + JSON logs  │         │
                         │        │ heartbeat.json  KILL file │         │
                         │        └───────────────────────────┘         │
                         └──────────────────────────────────────────────┘
                                          │
                                    SSH / Tailscale
                                          │
                                     operator
```

---

## 5. Components

| Component | Module | Responsibility |
|---|---|---|
| Domain types | `events.py` | Bars, quotes, trades, intents, orders, fills, positions, account snapshots. No I/O. |
| Config | `config.py` | Strict TOML → typed dataclasses; unknown keys are errors; secrets only from env. |
| Clock | `clock.py` | Simulated or real time. Only source of `now`. |
| Ports | `ports.py` | `HistoricalFeed`, `LiveFeed`, `ExecutionBroker`, `Wallet`, `Store` protocols. |
| Wallet | `wallet.py` | Average-cost ledger, buying power, day PnL; `BrokerWallet` mirror. |
| Paper broker | `brokers/paper.py` | Matching engine with slippage, spread, commission, participation, stops/limits, TIF. |
| Alpaca adapter | `brokers/alpaca.py` | Stdlib REST client, order translation, idempotent client order ids, retries. |
| Feeds | `feeds/` | Synthetic (deterministic), CSV, Alpaca REST; bar aggregation. |
| Calendar | `market/calendar.py` | US sessions, holidays, early closes, timeframes. |
| Risk | `risk/` | Hard halts, pre-trade rules, kill switch. |
| Strategies | `strategy/` | `StrategyContext`, base class, registry, built-ins. |
| Backtester | `backtest/` | Event loop, metrics, round trips, report data. |
| Optimizer | `optimize.py` | Parameter spaces, objective, folds/holdout, random/grid/DE search. |
| Dashboard | `dashboard/` | Separate read-only HTTP process: heartbeat, audit trail, reports, kill switch, mobile UI. |
| Pearl | `pearl/` | Causal model DAG: Kalman filters, pairs statistics, estimators, signals, state. |
| Runtime | `runtime/live.py` | Wall-clock loop, broker sync, heartbeats, signals, shutdown. |
| Persistence | `store.py` | SQLite WAL audit trail. |
| Interfaces | `cli.py`, `doctor.py`, `report.py` | Commands, pre-flight checks, reports. |
| Hot path | `crates/krabs_core` | Rust indicators and metrics, optional. |

**Dependency rule:** arrows point inward. `events` imports nothing from Krabs.
`strategy` imports `events`, `indicators`, `ports` — never `brokers` or `feeds`.
Adapters import the domain, never each other's internals. This is what makes
the simulator swappable.

---

## 6. Event and data model

All timestamps are timezone-aware UTC. A bar's `ts` is its **close** time. The
exchange calendar is the only place that knows about `America/New_York`.

```
MarketEvent = Bar | Quote | Trade

OrderIntent                      Order                      Fill
  symbol                           client_id                  order_id
  side (buy/sell)                  intent                     symbol / side
  qty | notional                   qty (resolved)             qty / price
  type (market/limit/stop/...)     status                     commission
  limit_price / stop_price         filled_qty / avg_price     ts
  tif (day/gtc/ioc/fok)            broker_id                  strategy_id / tag
  strategy_id / reason / tag       timestamps, reject_reason
```

`OrderIntent` is immutable and validated in `__post_init__` (exactly one of qty
or notional; limit price required for limit; notional only for market). Risk
rules may return an **adjusted copy**; the original is logged for audit.

`AccountState` is a snapshot, not a live object: cash, equity, buying power,
long/short value, realized/unrealized PnL, day-start equity. Strategies and risk
always read a snapshot, so there is no mid-calculation state to corrupt.

---

## 7. Wallets: simulated vs real

The wallet is the accounting boundary, separate from execution. This is the
central abstraction that makes training on simulated money meaningful.

### `PaperWallet`

- Owns cash, positions (average cost), realized PnL.
- `apply_fill()` is the only mutation: it updates cash by
  `-side * qty * price - commission`, averages entries, realizes PnL on
  reductions, handles flips.
- `mark()` updates market prices; `snapshot()` computes equity, gross exposure
  and buying power (`equity * margin - gross`, floored at zero).
- `can_afford()` enforces cash/margin and shorting permissions. Pending sell
  proceeds can be passed in so a same-bar rotation is possible without
  over-allocating.
- `reset_day()` rolls the daily-loss baseline at the first bar of a session.

### `BrokerWallet`

- Mirrors the broker's account and positions via `refresh(account, positions)`.
- Broker values always win. Local fills are optimistic estimates between syncs.
- Used in `broker-paper` and `live`; reconciliation runs at startup and on
  every runtime cycle.

### Why two wallets, not one?

Because "simulated wallet" and "real wallet" differ in **who is authoritative**,
not in what a strategy sees. Keeping the interface identical means a strategy
cannot detect (or exploit) the difference, and comparing paper vs live PnL is
apples-to-apples.

---

## 8. Execution semantics

### No look-ahead

Backtest loop for each timestamp `t`:

1. Feed delivers all bars closing at `t` (one per symbol, sorted).
2. `PaperBroker.on_event(bar)` fills orders that were **already working**:
   market orders at `bar.open`, limits/stops if the bar's range crosses them.
3. Wallet marks positions at `bar.close`.
4. Risk polls hard halts.
5. Strategy `on_bar()` runs and may submit new intents.
6. Orders submitted at `t` remain open; they cannot fill until the next event.

This ordering is the reason a strategy cannot peek at a close and get that
close as its fill. It also means every strategy has exactly **one bar of
latency**, which is realistic for bar-driven retail systems.

Live/paper uses the same order: bars arrive (polled), the paper broker fills
working orders against them, then the strategy sees the closed bars. Stale bars
after downtime are **discarded**, never replayed as if live.

### Fill model (`execution` config)

| Parameter | Semantics |
|---|---|
| `slippage_bps` | Adverse price move applied to market fills. |
| `half_spread_bps` | Additional adverse adjustment; together with slippage models crossing the spread. |
| `commission_per_share` / `min_commission` | `max(min, per_share * qty)`. |
| `max_participation` | Cap per bar as a fraction of bar volume; `0` disables the cap. |
| `allow_partial_fills` | When false, orders fill whole or not at all. |

Limit fills never violate the limit: a buy that gaps below its limit fills at
the better open; otherwise it fills at the limit. Stops trigger on the bar range
and then execute as market (or convert to a resting limit for stop-limit). IOC
and FOK are honoured; DAY/GTC remain working. All orders for a symbol share the
bar's participation budget.

### What is *not* modelled

Queue position, exchange latency, auctions, halts/LULD, borrow availability,
T+1 settlement, dividends/splits, odd-lot rules, short-sale locate fees. These
are documented gaps, not hidden assumptions. `broker-paper` is the mitigation:
if behaviour differs materially there, the strategy is not ready for live.

---

## 9. Risk

Two layers, checked in order. The runtime also cancels all working orders the
first time a halt appears.

### 9.1 Hard halts (`RiskEngine.poll`)

| Condition | Behaviour |
|---|---|
| Kill switch file exists | Block all orders, cancel working orders. Sticky until cleared + risk resumes. |
| Feed older than `max_data_age_s` | Block all orders (prevents trading on stale prices). |
| Day PnL ≤ `-max_daily_loss_pct` | Block all orders for the rest of the session; resets next session. |
| Repeated broker/transport errors (runtime) | Halt after 20 errors. |

### 9.2 Pre-trade rules (`RiskEngine.check`)

Rules are composable and ordered; each returns OK, ADJUST (clamped intent) or
REJECT (with a human-readable reason that is persisted).

| Rule | Effect |
|---|---|
| `SymbolRule` | Symbol must be subscribed and in `allowed_symbols`; price must exist. |
| `PriceCollarRule` | Limit/stop price within `price_collar_pct` of last trade. |
| `MinNotionalRule` | Reject dust orders (< $1). |
| `OrderSizeRule` | Clamp to `max_order_notional`. |
| `PositionLimitRule` | Clamp to `max_position_pct` of equity per symbol. |
| `GrossExposureRule` | Reject if projected gross exposure exceeds `max_gross_exposure_pct`, accounting for other working orders (rotation-friendly). |
| `BuyingPowerRule` | Wallet-level affordability plus a 2% cash buffer. |
| `OpenOrdersRule` | Cap concurrent working orders. |
| `DuplicateRule` | Suppress identical symbol/side/size intents inside `duplicate_window_s`. |

### 9.3 Kill switch

A file at `var/KILL`. Created by `krabs kill`, `SIGUSR1`, a watchdog, or the
operator over SSH. Presence cancels working orders and blocks new ones. The
runtime never deletes it; a human must do that (`krabs kill --clear`). This is
deliberate: automatic recovery from an unknown bad state is how accounts blow
up.

### 9.4 Promotion gates

`live` additionally requires:

- `--i-understand-the-risk` on the command line;
- `[broker].paper = false`;
- `[storage].enabled = true`;
- kill switch not engaged at startup;
- credentials present;
- startup cancellation of orphan orders and position reconciliation.

There is no config-file-only path to live trading.

---

## 10. Order management, idempotency and reconciliation

- **Client order ids** are deterministic: `krabs-{run8}-{seq:06}`. Alpaca treats
  a repeated id as the same order, so a retry after a timeout cannot double-fill.
- **Startup reconciliation** (`live`/`broker-paper`): fetch open orders, cancel
  them (they are orphans from a crashed process), adopt broker positions into
  `BrokerWallet`, seed the filled-quantity watermark, and log every mismatch.
  The platform never assumes local state is correct after a restart.
- **Fill detection**: the runtime polls recent orders, computes the delta in
  `filled_qty` per client id, synthesises a `Fill`, persists it, then notifies
  the strategy. `BrokerWallet` is refreshed from the broker afterwards, so the
  local ledger cannot drift.
- **Shutdown**: SIGINT/SIGTERM stops after the current cycle, cancels working
  orders by default, optionally flattens (`--flatten-on-exit`), writes a final
  heartbeat and closes the audit run.

---

## 11. Strategy framework

```python
@register
class SmaCross(Strategy):
    id = "sma_cross"
    defaults = {"fast": 20, "slow": 60, "allocation": 0.2}

    def on_bar(self, ctx, bar):
        fast = ctx.sma(bar.symbol, 20)
        slow = ctx.sma(bar.symbol, 60)
        if fast > slow:
            ctx.target_weight(bar.symbol, 0.2, reason="uptrend")
        else:
            ctx.target_weight(bar.symbol, 0.0)
```

Hooks: `on_start`, `on_day_start`, `on_bar`, `on_quote`, `on_fill`, `on_end`.
`ctx` exposes equity/cash/positions, bar history, indicators, `buy`, `sell`,
`target_weight`, `close`, `flatten`, and a scoped logger. It exposes **no**
broker, socket, filesystem, or wall clock.

For stateful statistical models, `PearlStrategy` (from `krabs.pearl`) updates a
causal model once per timestamp — including multi-asset pairs observations — and
only calls `on_snapshot` when every node is warmed up. The engine does not need
to know about Pearl.

Design rules enforced by the API:

- `target_weight` is the preferred sizing primitive: it converts a portfolio
  decision into a delta order and respects risk clamps.
- Orders are asynchronous. Never assume a submitted order filled.
- Strategies must be **idempotent per bar**: if the same bar is processed twice
  after a restart, the duplicate guard and regime state prevent double entry.
- Parameter validation happens in `__init__`; invalid configs fail at startup.
- One strategy instance per process. Multi-strategy books are a future port,
  not a hidden feature.

---

## 12. Data layer

| Feed | Use | Guarantees |
|---|---|---|
| `SyntheticFeed` | Offline dev, CI, demos | Deterministic from `(seed, symbol, days)`; GBM with overnight gaps, wicks, U-shaped volume; no network. |
| `CsvFeed` | Vendor exports, research | Requires `ts,symbol,open,high,low,close,volume`; naive timestamps assumed UTC; can aggregate to higher timeframes. |
| `AlpacaFeed` | Live/paper/backtest | REST bars, paginated, IEX feed by default (free plan), adjusts start timestamps to close timestamps. |
| `SyntheticLiveFeed` | `paper --offline` | Accelerated wall-clock bars; exercises runtime, risk, audit and heartbeat without a broker. Not for research. |

Bars are grouped by close timestamp; higher timeframes are aggregated per
session so buckets never span the overnight gap. The calendar supplies session
bounds, holidays and early closes (2024–2026 table; extend before 2027 or swap
in `exchange_calendars`).

**Polling, not streaming, is the default.** `poll_seconds` trades latency for
simplicity and reconnect safety on a Pi. The WebSocket upgrade is isolated to
the feed adapter; the runtime never sees it.

---

## 13. Persistence and observability

### SQLite (`storage.path`, WAL)

Tables: `runs`, `orders` (full JSON snapshot), `fills`, `equity`, `events`.
`PRAGMA synchronous=NORMAL`; each record committed immediately. An SSD is
strongly recommended on a Pi; the database is the audit trail, logs are for
humans.

### Files

| Path | Contents |
|---|---|
| `var/KILL` | Kill switch (timestamp + reason). |
| `var/heartbeat.json` | Timestamp, mode, equity, cash, positions, halt state, counters, PID. Atomic replace. |
| `var/krabs.db` | Audit trail. |
| `reports/{run_id}/` | `result.json`, `summary.txt`, `equity.csv`, `trades.csv`, `orders.csv`. |

### Logs

Structured JSON when `app.log_json = true` (recommended for journald). Every
order, fill, rejection, adjustment, halt and reconciliation issue is logged with
the run id. No secrets are ever logged.

### Health signal

A watchdog should alert if `heartbeat.json` is older than ~3× the poll interval,
if `halted` is true, if `errors` grows, or if the process is not `active` in
systemd. `krabs doctor` checks all of this plus temperature, disk, NTP, network
and credentials.

The optional dashboard (`krabs dashboard`) is a separate read-only process that
aggregates the heartbeat, SQLite audit trail, reports and kill switch for a
mobile browser. It never shares memory with the runtime and is read-only unless
started with `--allow-controls` (kill switch only).

---

## 14. Rust hot path

The native crate (`crates/krabs_core`) exposes *pure functions only*:
`sma`, `ema`, `rsi`, `atr`, `rolling_std`, `max_drawdown`,
`realized_volatility`.

Design constraints:

- **Batch at the boundary.** Call once per bar with a window (100–500 points),
  not once per tick. The list is copied once into Rust; the kernel then runs at
  native speed. A 300-point RSI in Python is ~100 µs; in Rust it is ~2 µs plus
  ~2 µs of conversion.
- **Identical numerics.** Rust and Python implementations produce the same
  values (including NaN warm-up). The Python test suite and the Rust unit tests
  assert the same reference values.
- **Optional by design.** `krabs.indicators` falls back to pure Python if the
  extension is absent. The platform never fails to start because Rust is
  missing; `krabs doctor` reports which backend is active.
- **No state, no I/O, no panics across FFI.** Release profile unwinds panics;
  pyo3 converts them to Python exceptions.
- Built with `maturin`, `abi3-py311`, so one wheel covers Python 3.11+.

---

## 15. Raspberry Pi platform design

### Hardware

| Item | Recommendation | Why |
|---|---|---|
| Board | Pi 5 (8 GB) or Pi 4 (4 GB min) | 8 GB gives headroom for pandas/research; 4 GB is enough for live trading. |
| Storage | NVMe SSD via HAT, or USB 3 SSD | SD cards die from database writes. Boot from SSD. |
| Power | Official PSU + small UPS | A graceful shutdown beats a corrupted DB; UPS rides out brownouts. |
| Cooling | Active cooler / heatsink case | Sustained 100% CPU during backtests otherwise throttles. |
| Network | Ethernet where possible; Wi-Fi + Tailscale for remote ops | Fewer reconnect cycles. |
| RTC | Pi 5 has one; Pi 4 uses NTP | Correct timestamps matter for bars. |

### OS

64-bit Raspberry Pi OS Lite (Bookworm or newer). No desktop. Enable:

- `chrony` (NTP) and a correct timezone (system stays UTC; only the calendar
  converts);
- `zram-tools` or a small swapfile (avoid SD swap);
- `unattended-upgrades` for security patches (schedule outside market hours);
- `logrotate`/journald limits (logs must not fill the disk);
- `noatime` on the data mount.

### Process model

One `systemd` service (`krabs.service`) runs one config/mode. Hardening:
dedicated unprivileged user, `ProtectSystem=strict`, `ProtectHome=read-only`,
`PrivateTmp`, `NoNewPrivileges`, `RestrictAddressFamilies=AF_INET AF_INET6`,
`MemoryMax=1G`, `CPUQuota=200%`, `UMask=0077`, writable paths limited to
`/opt/krabs/var` and `/opt/krabs/reports`.

`Restart=on-failure`, `RestartSec=10`. The runtime is designed to be killed at
any point: startup reconciliation makes restarts safe. `SIGUSR1` engages the
kill switch; `SIGTERM` stops gracefully.

Backups run from a systemd timer: SQLite online backup → gzip → retain 14 days,
preferably copied off-device.

### Network resilience

- REST retries with exponential backoff on 429/5xx and transport errors.
- Feed staleness halts trading before it can act on old prices.
- After downtime, bars older than `max_data_age_s` are skipped, not replayed.
- Broker sync every cycle re-establishes account truth.
- A broker outage does not crash the process; repeated failures halt it.

### Security

- Alpaca keys restricted to trading (no withdrawals), ideally IP-allowlisted.
- Secrets in `/etc/krabs/krabs.env`, mode `0600`, owned by root, read via
  systemd `EnvironmentFile`; never in TOML, git, logs or reports.
- No inbound ports. Remote access over Tailscale/WireGuard or SSH keys only.
- Unprivileged service user; no sudo.
- `krabs` never needs root. Installation does; running does not.

### Failure modes

| Failure | Detection | Response |
|---|---|---|
| Feed stalls | `max_data_age_s` | Halt, cancel orders, heartbeat shows `halted`, alert. |
| Broker API down | retries → error counter | Retry; halt after threshold; positions remain at broker. |
| Process crash / reboot | systemd restart | Startup cancels orphan orders, reconciles positions, resumes. |
| Order timeout after send | client order id | Retry is idempotent; never duplicates. |
| Disk full | doctor / systemd | SQLite writes fail → exception → halt; alert. |
| Thermal throttle | doctor temperature | Warn at 75 °C, fail at 85 °C; backtests scheduled off-hours. |
| Clock drift | NTP check in doctor | Bars misaligned; fix NTP before trading. |
| Bad deploy | `krabs status`, audit | `krabs kill`, revert, keep audit trail. |

---

## 16. Configuration map

All keys live in `configs/example.toml` with defaults and comments. Summary:

| Section | Key decisions |
|---|---|
| `[app]` | `mode`, `data_dir`, `log_json`. |
| `[market]` | Symbols, timeframe, warmup days, market-hours enforcement. |
| `[wallet]` | Starting cash, cash vs margin, shorting, fractional shares. |
| `[execution]` | The fill model: slippage, spread, commission, participation. |
| `[risk]` | Hard limits. These are the last line of defence; treat them as code. |
| `[broker]` | Alpaca paper/live, base URLs, env var names for secrets. |
| `[feed]` | Synthetic/CSV/Alpaca, poll cadence, warmup, Alpaca data feed. |
| `[storage]` | Audit trail on/off and path. |
| `[backtest]` | Report directory. |
| `[strategy]` | Registered strategy id and default parameters. |

Validation is strict: unknown keys, non-positive limits, inconsistent modes and
missing CSV paths fail before any connection is opened.

---

## 17. Repository layout

```
src/krabs/
  events.py  config.py  clock.py  ports.py  wallet.py  store.py
  brokers/    paper.py, alpaca.py, base.py
  feeds/      synthetic.py, csv.py, alpaca.py, base.py
  market/     calendar.py
  risk/       engine.py, rules.py, kill.py
  strategy/   base.py, registry.py, examples/
  backtest/   engine.py, metrics.py
  runtime/    live.py
  dashboard/  data.py, server.py, static/   # monitoring UI
  pearl/      builder.py, features.py, filters/, estimators/, signals/
  indicators/ __init__.py          # Rust-or-Python dispatch
  cli.py  doctor.py  report.py
crates/krabs_core/                 # optional native extension
configs/                           # offline, paper, live example
deploy/                            # systemd, env template, installer
docs/                              # deployment, operations, strategy guide, ADRs
tests/                             # 137 stdlib unittest cases
```

---

## 18. Testing strategy

| Layer | What is asserted |
|---|---|
| Domain | Wallet average cost, shorts, commissions, day reset; bar invariants. |
| Paper broker | Next-bar fills, slippage direction, limit gaps, partial fills, IOC, cancels, rejects. |
| Risk | Clamps, exposure, duplicates, stale data, daily halt and reset, kill switch. |
| Backtest | **No look-ahead** (fill timestamp/price), determinism across runs, metrics. |
| Runtime | Real async loop, paper fills, heartbeat, kill switch blocking. |
| Config/store | Strict loading, SQLite round trip, audit counts. |
| Calendar | Sessions, holidays, early closes, next open, DST. |
| Rust | Same reference values as Python (`cargo test --no-default-features`). |

Run: `make test` (`PYTHONPATH=src python3 -m unittest discover -s tests`).

---

## 19. Extension points

- **New broker**: implement `ExecutionBroker` + `Wallet`, add a config branch in
  `cli.py`, keep `client_order_id` semantics. No strategy changes.
- **New feed**: implement `history()`/`latest_bars()` (or `bars()` for
  backtests). Normalise timestamps to close time.
- **New strategy**: subclass + `@register`; it appears in `krabs strategies`.
- **New risk rule**: implement `Rule.check()` and append it in
  `risk.rules.default_rules`.
- **New search method**: implement a method on `StrategyOptimizer` that returns
  `Evaluation` objects; keep the holdout and objective logic unchanged. Optuna
  TPE, CMA-ES and nested walk-forward are natural extensions.
- **New Pearl node**: subclass `Node`, implement `update`/`state`/`load_state`,
  and add it to a builder. The DAG and strategy integration are unchanged.
- **Streaming data**: add a WebSocket adapter behind the feed interface; the
  runtime already handles `on_quote` hooks.
- **Multi-strategy**: wrap the current single-strategy runtime in a portfolio
  allocator; `RiskEngine` and `PaperWallet` are already shared-state friendly.
- **Metrics/observability**: `heartbeat.json` is trivial to scrape; add a
  Prometheus textfile exporter without touching the engine.

---

## 20. Decisions

- `docs/adr/0001-event-driven-hexagonal-core.md`
- `docs/adr/0002-python-core-rust-hot-path.md`
- `docs/adr/0003-wallet-execution-separation.md`
- `docs/adr/0004-file-based-kill-switch.md`
- `docs/adr/0005-rest-polling-before-streaming.md`
- `docs/adr/0006-pearl-causal-model-layer.md`

---

## 21. What "done" means for v1

A strategy is considered deployable when:

1. It runs deterministically in `backtest` and beats buy-and-hold on a
   risk-adjusted basis over a period that includes a drawdown.
2. It survives a parameter sweep without collapsing (neighbouring parameters
   behave similarly).
3. It runs for at least two weeks in `paper` with no unhandled errors, no
   unexpected halts and a PnL distribution consistent with the backtest after
   costs.
4. It runs for at least one week in `broker-paper` with clean reconciliation and
   no unexplained rejects.
5. Only then: `live`, with a small account, a tight daily loss limit, and an
   operator watching the heartbeat.
