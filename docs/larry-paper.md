# Larry paper integration

Krabs is the execution and risk boundary. Larry writes an expiring target-weight
file; Krabs reads it, reloads it before each trading day, and applies it through
the normal risk engine. Larry has no broker credentials.

## Config

`configs/larry-paper.toml`:

- mode: `broker-paper` when invoked with that command; `paper` for local
  simulated fills;
- symbols: the full Larry medium universe plus `SPY`, `SSO`, `BIL` (58 symbols);
- timeframe: `5Min`;
- account: **$1k Alpaca paper**;
- sleeves: slow 50% and medium 35% (scaled by regime);
- strategy: `larry`;
- source: `../Larry/data/processed/larry-targets.json`;
- max age: 5 calendar days;
- fail closed if missing/stale.

## Target file

```json
{
  "as_of": "2026-10-23",
  "targets": {
    "BIL": 0.5,
    "DBC": 0.0875,
    "XLE": 0.0875,
    "EWZ": 0.0875
  }
}
```

The slow and medium targets are combined in one file. Medium adds up to four
ETF weights on a weekly rebalance; slow contributes SSO/SPY/BIL at 50%.

Historical backtests can use `{"rows": [...]}` or CSV. A single "latest" file is
for paper only; using it against historical bars would be look-ahead.

## Fast overnight overlay

With `fast_overnight = true` (set in `configs/larry-paper.toml`), the target
file may carry Larry's `fast_overnight` permission:

```json
"fast_overnight": {"session": "2026-11-17", "symbol": "SPY", "weight": 0.1,
                   "min_price": 671.2, "stop_pct": 0.01,
                   "entry_after": "15:55", "exit_after": "09:35"}
```

- On `session`, the first bar at or after `entry_after` ET (the 15:55 5-minute
  bar) raises SPY to its base target + `weight` if the bar close is above
  `min_price`. One decision per session; skipped if the day's base targets are
  still pending or an SPY order is open.
- No separate exit order: the next session's base targets, applied on its first
  bar, take SPY back down.
- If that morning's first bar opens more than `stop_pct` below the entry price,
  that day's entry is skipped.
- Early-close days never reach 15:55, so there is no entry.

`strategy_diagnostics.fast_overnight.status` in the heartbeat shows the last
decision (`entered …`, `skipped …: reason`, `overnight stop …`).

## Run

```bash
set -a; . ../Larry/larry.env; set +a
./krabs doctor -c configs/larry-paper.toml
./krabs broker-paper -c configs/larry-paper.toml
```

Local simulated fills:

```bash
./krabs paper --offline -c configs/larry-paper.toml --max-cycles 3
```

## Schedule

The Krabs user service is `deploy/systemd/krabs-larry.service`. The matching
Larry signal timer lives in the Larry repo. See Larry's
`docs/paper-testing.md`.

## Stop

```bash
./krabs kill -c configs/larry-paper.toml
./krabs flatten -c configs/larry-paper.toml --yes
```

The strategy reloads the source before each day, so a fresh Larry file is picked
up without restarting Krabs.
