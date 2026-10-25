# Krabs

**Algorithmic trading on a Raspberry Pi — backtest, paper, and live with one strategy API.**

Krabs is a small, auditable trading platform built for a single-board computer.
It connects to Alpaca for US equities, runs your strategy against a **simulated
wallet** or a **real brokerage account**, and treats safety and reproducibility
as first-class features rather than afterthoughts.

```
backtest ──► paper ──► broker-paper ──► live
  logic       logic       plumbing       money
```

- **No look-ahead by construction.** Orders decided on bar `t` can only fill on
  bar `t+1`; the engine enforces it.
- **Realistic paper fills.** Slippage, half-spread, commissions, volume
  participation, partial fills, limit/stop triggers and TIF semantics.
- **Two wallets, one interface.** Local `PaperWallet` for training, broker-mirrored
  `BrokerWallet` for real money. Strategies cannot tell the difference.
- **Risk first.** Per-order clamps, position and gross-exposure caps, daily loss
  halt, stale-feed halt, duplicate guard, file-based kill switch.
- **Unattended on a Pi.** systemd unit, SQLite WAL audit trail, heartbeat file,
  startup reconciliation, exponential backoff, thermal/disk/NTP doctor.
- **Mobile dashboard.** Read-only web UI for equity, P&L, positions, orders,
  fills, runs, reports, risk and health; optional kill-switch control.
- **Pearl model layer.** Causal Kalman filters, pairs/cointegration statistics,
  mean reversion, Hurst, CUSUM, HP, Haar and GARCH; compose features, filters
  and signals into a stateful model with replayable state.
- **Optional Rust hot path.** SMA/EMA/RSI/ATR and metrics, with an identical
  pure-Python fallback so the platform always runs.

Full design: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Quickstart (offline, no API keys)

Requires Python 3.11+. There are **no runtime dependencies**.

```bash
git clone <this repo> Krabs && cd Krabs

./krabs strategies                              # list built-ins
./krabs backtest -c configs/example.toml        # synthetic 90-day backtest
./krabs doctor -c configs/example.toml          # pre-flight checks
```

The backtest writes `reports/<run_id>/` with `result.json`, `summary.txt`,
`equity.csv`, `trades.csv` and `orders.csv`.

Try different strategies and parameters:

```bash
./krabs backtest -c configs/example.toml --strategy rsi_reversion \
    --param period=14 --param entry=25 --param allocation=0.2

./krabs backtest -c configs/example.toml --strategy buy_hold --days 20 --seed 42

# compare a grid of variants offline and rank by Sharpe
make sweep
# or: PYTHONPATH=src python3 scripts/sweep.py --strategy sma_cross \
#       --grid fast=5,10,20 --grid slow=30,60,90 --days 30
```

Run the test suite (137 cases, stdlib `unittest`):

```bash
make test
# or: PYTHONPATH=src python3 -m unittest discover -s tests -v
```

---

## Try it on your Ubuntu workstation (no root, no keys)

The checkout is self-contained: the launcher adds `src/` to `PYTHONPATH`, so
there is nothing to install and no virtualenv needed.

```bash
./krabs backtest -c configs/example.toml
./krabs doctor   -c configs/example.toml
make test
```

To exercise the **full runtime** (polling loop, risk, paper fills, SQLite,
heartbeat, kill switch) without a broker or API keys, run the offline
simulation:

```bash
./krabs paper --offline -c configs/offline.toml
```

Or install it as a per-user systemd service, which starts at login and needs no
root:

```bash
./deploy/install-dev.sh --enable
journalctl --user -u krabs -f

./krabs status -c configs/offline.toml
./krabs kill   -c configs/offline.toml    # blocks new orders
./krabs kill --clear -c configs/offline.toml
systemctl --user disable --now krabs.service
```

For real Alpaca paper trading on the same workstation:

```bash
./deploy/install-dev.sh --paper --enable
# first run creates ~/.config/krabs/krabs.env; add your keys and restart:
systemctl --user restart krabs
```

`loginctl enable-linger "$USER"` lets the user service run before login (useful
on a headless box).

## Sample historical data (daily bars, by sector)

A regenerable sample lives in `data/samples/`: 11 GICS-style sectors, three
large caps each, plus SPY, daily bars from 2020 to today. It is generated from
Yahoo Finance and kept out of git for licensing reasons.

```bash
# generate/refresh (about 30 seconds)
python3 scripts/fetch_sample_data.py

# backtest the default technology sample
./krabs backtest -c configs/sample-daily.toml

# any sector; symbols are auto-detected from the CSV
./krabs backtest -c configs/sample-daily.toml --csv data/samples/energy.csv
./krabs backtest -c configs/sample-daily.toml --csv data/samples/benchmark.csv \
    --strategy buy_hold --param allocation=0.95
```

See [data/samples/README.md](data/samples/README.md) for the files, caveats
(survivorship bias, adjusted prices) and flags. This is a plumbing sample, not
a research-grade universe.

## Modes

| Mode | Data | Wallet | Execution | Use |
|---|---|---|---|---|
| `backtest` | Synthetic / CSV / Alpaca history | Simulated | Local matching engine | Research, CI |
| `paper` | Alpaca live | Simulated | Local matching engine | Train strategies with real prices, zero broker risk |
| `broker-paper` | Alpaca live | Broker mirror | Alpaca paper account | Validate integration and order lifecycle |
| `live` | Alpaca live | Broker mirror | Alpaca real account | Real money |

### Paper trading with real prices

```bash
export ALPACA_API_KEY="..."
export ALPACA_SECRET_KEY="..."
./krabs paper -c configs/alpaca-paper.toml
```

`paper` polls Alpaca bars and fills orders **locally** at the next bar with
simulated slippage/costs. It exercises the strategy, not the broker API.

### Real money

```bash
cp configs/live.example.toml configs/live.toml   # git-ignored; review every limit
export ALPACA_API_KEY="..."
export ALPACA_SECRET_KEY="..."
./krabs live -c configs/live.toml --i-understand-the-risk
```

Live mode additionally requires `[broker].paper = false`,
`[storage].enabled = true`, a clear kill switch, and the explicit CLI flag.
On startup it cancels orphan orders and reconciles broker positions.

> **Start small.** A $250 max order and a 1% daily loss limit on a $2,000
> account is a reasonable first live config. Do not skip `paper` and
> `broker-paper`. Use a **dedicated account**: Krabs cancels every open order on
> startup and assumes it owns the account.

---

## Safety controls

```bash
./krabs kill -c configs/live.toml            # block all orders, cancel working orders
./krabs status -c configs/live.toml          # heartbeat, equity, kill state, audit
./krabs kill --clear -c configs/live.toml    # resume (a human decision, never automatic)
kill -USR1 <pid>                             # same as `krabs kill`, from systemd
./krabs flatten -c configs/live.toml --yes   # cancel orders + liquidate (Alpaca)
```

Defaults in `[risk]`: max order notional, per-symbol position cap, gross
exposure cap, daily loss halt, open-order cap, price collar, stale-data halt,
duplicate suppression. Unknown config keys are startup errors.

---

## Raspberry Pi deployment

```bash
sudo ./deploy/install.sh          # user, venv, units, /etc/krabs
sudo nano /etc/krabs/krabs.toml   # start with paper mode
sudo nano /etc/krabs/krabs.env    # API keys, chmod 600
./krabs doctor -c /etc/krabs/krabs.toml
sudo systemctl enable --now krabs.service
journalctl -u krabs -f
```

Hardware, SSD boot, UPS, thermal limits, backups, monitoring and the full
runbook: **[docs/deployment.md](docs/deployment.md)** and
**[docs/operations.md](docs/operations.md)**.

---

## CLI

```
krabs [-c CONFIG] backtest [--strategy NAME] [--param K=V] [--days N] [--seed N]
                              [--csv PATH] [--symbols A,B] [--timeframe TF]
                              [--report-dir DIR] [--store] [--quiet]
krabs [-c CONFIG] dashboard [--host H] [--port 8787] [--allow-controls]
krabs [-c CONFIG] optimize --strategy NAME --space NAME=LOW:HIGH[:STEP] ...
                              [--method random|grid|de] [--trials N] [--folds N]
                              [--holdout 0.2] [--objective sharpe|...]
krabs [-c CONFIG] paper        [--strategy NAME] [--param K=V]
krabs [-c CONFIG] broker-paper [--strategy NAME] [--param K=V]
krabs [-c CONFIG] live         --i-understand-the-risk [--flatten-on-exit]
krabs [-c CONFIG] kill         [--clear] [--reason TEXT]
krabs [-c CONFIG] status
krabs [-c CONFIG] doctor
krabs [-c CONFIG] strategies
krabs [-c CONFIG] flatten      --yes
```

`-c` works before or after the subcommand. Without it, `configs/example.toml`
is used when present.

---

## Project layout

```
src/krabs/            Python core (stdlib only)
  events.py           value types: Bar, OrderIntent, Order, Fill, AccountState
  wallet.py           PaperWallet and BrokerWallet (average cost)
  brokers/            paper matching engine + Alpaca REST adapter
  feeds/              synthetic, CSV, Alpaca
  risk/               hard halts, pre-trade rules, kill switch
  strategy/           StrategyContext, registry, examples
  backtest/           event loop, metrics, round trips
  optimize.py         parameter search, objectives, holdout, reports
  dashboard/          HTTP dashboard, JSON API, static mobile UI
  pearl/              causal model DAG: filters, estimators, signals
  runtime/            live/paper loop, reconciliation, heartbeat
  indicators/         Rust-or-Python dispatch
crates/krabs_core/    optional PyO3 extension
configs/              offline, Alpaca paper, live, samples, Pearl/ETF pairs
data/samples/         generated daily bars by sector (git-ignored)
deploy/               systemd units, env template, installer
docs/                 deployment, operations, strategy guide, ADRs
tests/                unit tests
```

---

## Pearl: causal models and pairs trading

Pearl is Krabs' statistical modelling layer. Models are small DAGs of stateful
nodes — features → filters/estimators → signals — and they only ever see the
current and past bars.

```python
from krabs.pearl import ModelBuilder, PairSpread, Series, Threshold

model = (
    ModelBuilder()
    .add("left", Series("AAPL"))
    .add("right", Series("MSFT"))
    .add("spread", PairSpread("left", "right", window=60, min_obs=30))
    .add("signal", Threshold("spread.z", entry=2.0, exit=0.5))
    .build()
)

snap = model.update(observation)      # one timestamp, one or more symbols
snap.ready
snap.value("spread.beta")
snap.signal("signal")                 # +1 long spread, -1 short, 0 flat
```

Included in the first release: linear Kalman filters (local level, local trend,
AR(1), time-varying regression), rolling OLS pairs/hedge ratios, spread
z-scores, half-life, approximate ADF, Johansen cointegration, Hurst, CUSUM
change detection, rolling HP and Haar trends, rolling PCA, GARCH(1,1)
volatility and threshold signals. Fixed parameters are the
default; Kalman observation-noise adaptation is optional per model.

A complete pairs strategy is registered as `pearl_pairs`:

```bash
python3 scripts/fetch_sample_data.py
./krabs backtest -c configs/pearl-pairs.toml
./krabs backtest -c configs/pearl-johansen.toml
```

Model parameters are nested (`model.window`, `model.method`, ...) and the
optimizer can search them with dotted spaces:

```bash
./krabs optimize -c configs/pearl-pairs.toml --strategy pearl_pairs \
    --space model.window=30:120 --space model.entry=1.5:2.5 \
    --space model.method=ols,johansen --method de --trials 40
```

A full sector-ETF implementation is included: Johansen screening, half-life,
zero-crossing and bid/ask filters, then a Kalman z-score entry with a friction
threshold. The default spread filter is an AR(1)/OU state model (`model = "ou"`);
`local_level` reproduces the original random-walk implementation. Captured
quotes drive both the liquidity filter and simulated fill costs:

```bash
python3 scripts/fetch_sample_data.py           # prices, no API key needed
python3 scripts/fetch_etf_spreads.py           # optional: observed bid/ask (Alpaca key)
./krabs backtest -c configs/etf-pairs.toml
```

See **[docs/etf-pairs.md](docs/etf-pairs.md)** for the exact rules and
assumptions. Design rules, the node catalog, causality guarantees, state
serialization and the roadmap (advanced wavelets, nonlinear filters) are in
**[docs/pearl.md](docs/pearl.md)**.

A panel-level alternative trades PCA residuals instead of pairwise spreads: a
rolling correlation PCA on the ETF panel, with a sparse factor-mimicking hedge
and cost-aware entries:

```bash
./krabs backtest -c configs/pca-residual.toml
```

See **[docs/pca-residual.md](docs/pca-residual.md)** for the model, portfolio
construction and results.

A long-only Gary Antonacci dual-momentum implementation (SPY / VEU / AGG, with
a BIL absolute filter) is also included:

```bash
python3 scripts/fetch_sample_data.py --only dual_momentum --start 2007-01-01
./krabs backtest -c configs/dual-momentum.toml
```

See **[docs/dual-momentum.md](docs/dual-momentum.md)** for the rules, parameters
and the honest benchmark comparison. It is the first entry in the
**[strategy bank](docs/strategy-bank.md)** at `research-approved` status — not
yet paper- or live-approved.

An intraday overnight-drift prototype (buy near the close, sell after the next
open) is included as a `candidate`, not banked:

```bash
python3 scripts/fetch_intraday_sample.py --range 60d
./krabs backtest -c configs/overnight-drift.toml
```

See **[docs/overnight-drift.md](docs/overnight-drift.md)** for the daily
close-to-open study, costs and the execution caveat.

Stress the same strategies with wider spreads, borrow costs, extra execution
delay and participation caps:

```bash
python3 scripts/stress.py configs/etf-pairs.toml configs/pca-residual.toml
```

See **[docs/stress-testing.md](docs/stress-testing.md)** for the levers and the
latest matrix.

Audit the data itself (calendar gaps, duplicate bars, price outliers, static
spreads, borrow coverage, survivorship):

```bash
python3 scripts/audit_data.py configs/etf-pairs.toml
```

See **[docs/data-audit.md](docs/data-audit.md)** for the checks, metadata
formats and current limitations.

## Dashboard

A mobile-friendly web dashboard reads the heartbeat, audit database, kill switch
and reports without touching the trading process:

```bash
./krabs dashboard -c configs/offline.toml --allow-controls
cat var/dashboard.token          # first run generates it
```

Open `http://127.0.0.1:8787/` and log in with the token. It shows equity,
P&L, positions, orders, fills, runs, backtest/optimization reports, risk limits
and health. The kill switch can be engaged/cleared from the UI when
`--allow-controls` is set; everything else is read-only.

```bash
# on a phone: Tailscale Serve in front of loopback
tailscale serve --bg 8787
```

Read-only by default, token auth, CSRF-protected controls and a strict CSP.
See **[docs/dashboard.md](docs/dashboard.md)** for remote access (Tailscale/SSH/
Caddy), the systemd unit, the JSON API and the security model.

## Optimizing a strategy

`krabs optimize` searches strategy parameters against a risk-adjusted objective,
using a chronological **holdout** and contiguous folds so the winning parameters
are not selected on the data used to judge them:

```bash
./krabs optimize -c configs/sample-daily.toml --strategy sma_cross \
    --space fast=5:30 --space slow=40:120 --space allocation=0.1:0.3 \
    --method de --trials 40 --folds 4 --holdout 0.2 --objective sharpe
```

It writes every trial to `reports/optimize-<run_id>/` and reports the holdout
separately. A high in-sample score with a bad holdout is a rejected candidate,
not a discovery.

The built-in methods are **random search, grid and differential evolution** —
all stdlib-only and Pi-friendly. An RNN is not a parameter optimizer; it is a
forecasting model or a policy. See **[docs/optimization.md](docs/optimization.md)**
for the objective formula, overfitting controls, and how to integrate RNN/RL
strategies properly.

## Writing a strategy

```python
from krabs.strategy import Strategy, register

@register
class MyStrategy(Strategy):
    id = "my_strategy"
    defaults = {"allocation": 0.2}

    def on_bar(self, ctx, bar):
        fast = ctx.sma(bar.symbol, 20)
        slow = ctx.sma(bar.symbol, 60)
        ctx.target_weight(bar.symbol, self.p("allocation") if fast > slow else 0.0)
```

Drop it in `src/krabs/strategy/examples/`, register the import in
`examples/__init__.py`, and it appears in `krabs strategies`. Guide:
**[docs/strategy-authoring.md](docs/strategy-authoring.md)**.

---

## Optional Rust core

```bash
source .venv/bin/activate
./scripts/build_rust.sh
# RUSTFLAGS="-C target-cpu=native" ./scripts/build_rust.sh   # Pi-specific
./krabs doctor -c configs/example.toml   # "indicator backend: rust"
```

Without it, the pure-Python fallback is used. Builds are optional and never
required to run the platform.

---

## Limitations (v1)

US equities cash accounts, long-only by default (shorting is configurable but
untested at the broker), average-cost accounting, no options/futures/crypto, no
tick-level order book, REST polling rather than streaming, one strategy per
process, holidays hard-coded through 2026. See ARCHITECTURE.md §1 and §8 for the
full list, and §19 for how to extend each boundary.

---

## License

MIT. Trading is risky; this software is provided as-is and is not financial
advice. You are responsible for every order sent to a real account.
