# Larry paper integration

Krabs is the execution and risk boundary. Larry writes an expiring target-weight
file; Krabs reads it, reloads it before each trading day, and applies it through
the normal risk engine. Larry has no broker credentials.

## Config

`configs/larry-paper.toml`:

- mode: `broker-paper` when invoked with that command; `paper` for local
  simulated fills;
- symbols: `SPY`, `SSO`, `BIL`;
- timeframe: `5Min`;
- account: **$1k Alpaca paper**;
- strategy: `larry`;
- source: `../Larry/data/processed/larry-targets.json`;
- max age: 5 calendar days;
- fail closed if missing/stale.

## Target file

```json
{
  "as_of": "2026-10-23",
  "targets": {"BIL": 0.5}
}
```

Historical backtests can use `{"rows": [...]}` or CSV. A single "latest" file is
for paper only; using it against historical bars would be look-ahead.

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
