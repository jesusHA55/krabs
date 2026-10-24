# ETF pairs strategy

The `etf_pairs` example implements the sector-cluster pairs process:

```
ETF universe
   │  group by sector
   ▼
pair candidates
   │  Johansen cointegration (rank ≥ 1)
   │  2 ≤ half-life ≤ 15 bars/days
   │  ≥ 12 zero crossings per year (annualised)
   │  each leg's bid/ask < 0.05% of price
   ▼
best pair per cluster (no shared legs)
   │  warm a Kalman local-level filter on the log spread
   ▼
enter when |z| > 2 and |innovation| > 2.5 × friction
hold until z crosses 0
```

The code is in `src/krabs/strategy/examples/etf_pairs.py`; the screening logic is
`src/krabs/pearl/screening.py`.

## Run it

```bash
python3 scripts/fetch_sample_data.py       # also creates data/samples/etf_sectors.csv
./krabs backtest -c configs/etf-pairs.toml
```

The sample universe contains liquid sector and industry ETFs:

| Cluster | ETFs |
|---|---|
| technology | XLK, SMH, IGV, VGT |
| financials | XLF, KBE, KRE, IAI |
| health care | XLV, XBI, IHI, XPH |
| energy | XLE, XOP, OIH, XES |
| consumer | XLY, XRT, XLP |
| industrials | XLI, XAR, ITA |
| materials | XLB, XME, GDX |
| utilities | XLU, VPU |
| real estate | XLRE, IYR, VNQ |
| communication | XLC, XTL |

Edit `[strategy.params.clusters]` to use your own sector groups. Clusters must be
disjoint; a symbol cannot be in two clusters.

## Selection

At every `rescreen_days` bars:

1. Take the trailing `formation_days` of log prices for every symbol in each
   cluster.
2. Run Pearl's Johansen test for every pair in the cluster.
3. Keep pairs where:
   - `rank >= 1` (a stationary linear combination exists);
   - `2 <= half_life <= 15` bars (days on daily data);
   - annualised zero crossings `>= min_crossings_per_year`;
   - both legs' configured bid/ask spreads are strictly below
     `max_bid_ask_bps` (default `5`, i.e. 0.05%);
   - `min_abs_beta <= |beta| <= max_abs_beta` (defaults `0.2`–`5.0`) so a
     near-zero hedge is not treated as a pair.
4. Pick the highest-scoring pair per cluster, then the top `max_pairs` overall.
   The score is `crossings_per_year / max(half_life, 0.5)`; selection never
   shares a leg.

Zero crossings are counted on the demeaned spread and annualised with
`periods_per_year`:

```
crossings_per_year = crossings * periods_per_year / (formation_bars - 1)
```

For daily bars use `252`; for 5-minute bars use roughly `252 × 78 = 19656`.
Half-life is measured in bars. Set the bounds in the same units.

### Bid/ask data

The bundled price sample has OHLCV only, so spreads come from three sources,
in increasing precedence:

1. `default_spread_bps` — fallback for any symbol without a specific value.
2. `[strategy.params.spread_bps]` — per-symbol estimates.
3. `spreads_csv` — captured quotes, which override everything above.

```toml
default_spread_bps = 2.0
max_bid_ask_bps = 5.0
spreads_csv = "data/samples/etf_spreads.csv"

[strategy.params.spread_bps]
XLK = 1.0
SMH = 2.0
```

`spreads_csv` accepts a `median_spread_bps` (preferred) or `spread_bps`
column keyed by `symbol`. Generate it with observed quotes:

```bash
export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...
python3 scripts/fetch_etf_spreads.py --start 2020-01-01
```

That script samples a one-second window at the last session close of each
month, tries SIP then IEX, and writes the median spread per symbol. It is a
**static per-symbol table**, not a spread time series, and needs Alpaca
credentials (free plans can read older SIP history and real-time IEX, not
recent SIP). If the file is missing at startup the strategy logs a warning and
falls back to the configured estimates; `diagnostics()["spreads_source"]`
records which source was used.

The same table drives simulated transaction costs, so the backtest pays the
spread it screens on instead of a flat assumption:

```toml
[execution]
slippage_bps = 1.0
half_spread_bps = 1.0            # fallback when a symbol is absent
spreads_csv = "data/samples/etf_spreads.csv"
# or inline, merged over the CSV:
# [execution.spread_bps]
# XLK = 1.0
```

`spread_bps` is the **full quoted spread**; paper fills charge half of it per
side on top of `slippage_bps`. Limit fills never pay the synthetic half-spread.

For live/paper trading, replace these with the actual observed spreads. The
filter is applied to **each leg**; the pair friction used by the entry test is:

```
friction_bps = 0.5 * (bid_ask_left_bps + |beta| * bid_ask_right_bps)
```

## Spread model

`model` selects how the log spread is filtered:

```toml
model = "ou"          # local_level | ou | ou_dynamic
ou_window = 120       # trailing values for the AR(1) fit
ou_refit = 20         # bars between parameter refits
ou_obs_ratio = 0.5    # share of AR(1) variance treated as observation noise
ou_warmup = 5         # filter steps after the first fit before trading
```

| Model | Pipeline | Use |
|---|---|---|
| `local_level` | static spread → Kalman random-walk level | original behaviour |
| `ou` | static spread → AR(1) Kalman | **default**; mean-reverting state |
| `ou_dynamic` | log prices → random-walk `[alpha, beta]` regression → OU on the residual | experimental |

The local-level filter treats the spread level as a random walk, so `z` is a
deviation from a *drifting* level. `ou` instead fits

```
d_t = phi * d_{t-1} + w_t          (deviation from equilibrium)
y_t = mu + d_t + v_t               (observed spread)
```

on the trailing window and reports `z = d / sqrt(P + R)`. It also exposes the
estimated `phi`, half-life, equilibrium `mu` and `sd`. If the trailing window is
not stationary (`phi` outside `(0, 0.999)`), the node is not ready and the
strategy will not open a pair; an open pair is flattened if the fit is lost.

`ou_dynamic` lets the hedge ratio follow a random walk (`beta_var`) and applies
the OU filter to the regression residual. In testing it reduced exposure and
did not improve returns, so it is kept as an option, not a recommendation.

The hedge ratio comes from the Johansen vector and remains fixed until the next
screen (subject to the 20% drift tolerance; otherwise the pair is closed and
reopened). The model is warmed on the formation history before it can trade.

- `z` is the filtered deviation (`ou.z` or `kf.innovation_z`).
- `innovation` is in log-spread units.
- `friction` is `friction_bps / 10_000`.

Entry:

- `z >= entry_z` and `|innovation| >= friction_multiple * friction` →
  **short the spread** (short left, long `beta` of right).
- `z <= -entry_z` and the same innovation test → **long the spread**.

Exit:

- short spread: close when `z >= 0`;
- long spread: close when `z <= 0`.

The strategy is symmetric; `entry_z` defaults to `2.0` and
`friction_multiple` to `2.5`.

## Position sizing

Each pair targets:

```
gross = allocation
weight_left  = side * gross / (1 + |beta|)
weight_right = -side * beta * gross / (1 + |beta|)
```

`side=+1` is long the spread, `-1` short. This keeps gross exposure per pair equal
to `allocation` (default 10%) and makes the two legs roughly beta-neutral.
`max_pairs` and `[risk]` caps still apply; rejected legs are retried.

## Causality and lifecycle

- Screening uses only bars up to the current close; orders fill next bar.
- New pairs are built and warmed after an old pair's exit fills, so legs are not
  mixed.
- An exit is recorded as complete when the closing orders are submitted. If a
  leg's close is rejected, the next bars keep flattening both legs until they
  are flat.
- If an entry fills on only one leg, the strategy re-sends the target for up to
  two bars, then flattens the remaining leg.
- If a pair disappears at the next screen, it is flattened and removed.
- `max_hold_bars` is an optional time stop. It is off by default because the
  stated rule is to hold until `z=0`; it exists to bound the slow, non-reverting
  trades that account for most of the sample's losses.
- If the hedge ratio drifts more than 20% between screens, the pair is rebuilt.
- Existing pairs continue to be updated once per timestamp, gated on the driver
  symbol (`driver`, default the first symbol in the first cluster).
- No stop-loss is added beyond Krabs' risk engine: the stated rule is to hold
  until `z=0`. A real deployment should still use per-pair loss limits.

## Configuration

`configs/etf-pairs.toml` contains the full example. Key parameters:

| Parameter | Default | Meaning |
|---|---|---|
| `formation_days` | 252 | trailing bars for screening/warm-up |
| `rescreen_days` | 21 | bars between selections |
| `min_half_life` / `max_half_life` | 2 / 15 | bars |
| `min_crossings_per_year` | 12 | annualised |
| `periods_per_year` | 252 | annualisation for crossings |
| `max_bid_ask_bps` | 5.0 | per leg, strict |
| `default_spread_bps` | 2.0 | fallback estimate |
| `spreads_csv` | `""` | captured per-symbol spread table |
| `model` | `ou` | `local_level`, `ou` or `ou_dynamic` |
| `ou_window` / `ou_refit` | 120 / 20 | AR(1) fit window and refit cadence |
| `ou_obs_ratio` | 0.5 | observation-noise share of AR(1) variance |
| `beta_var` | 1e-5 | random-walk hedge variance (`ou_dynamic`) |
| `entry_z` | 2.0 | absolute |
| `friction_multiple` | 2.5 | innovation threshold |
| `max_hold_bars` | 0 | optional time stop (bars; 0 = off) |
| `allocation` | 0.10 | gross per pair |
| `max_pairs` | 5 | overall |
| `min_abs_beta` / `max_abs_beta` | 0.2 / 5.0 | reject degenerate hedges |
| `kf_level_var` / `kf_obs_var` | 1e-6 / 1e-4 | log-spread Kalman variances |
| `kf_warmup` | 20 | observations before trading |
| `driver` | first symbol | bar that drives the once-per-timestamp update |

## Model comparison

Full sample 2020-01 → 2026-10 with captured spreads and transaction costs, run
as one continuous portfolio (the holdout is the tail of the same run, not a
cold start):

| Model | Return | Sharpe | Max DD | Fills | Avg exposure |
|---|---:|---:|---:|---:|---:|
| `local_level` | −2.98% | −0.45 | −3.69% | 498 | 5.4% |
| `ou` | **−1.55%** | **−0.21** | **−3.17%** | 794 | 15.1% |

Holdout segment, 2025-06-13 → 2026-10:

| Model | Return | Sharpe | Max DD | Fills |
|---|---:|---:|---:|---:|
| `local_level` | +0.15% | +0.20 | −0.48% | 60 |
| `ou` | +1.26% | +1.51 | −0.35% | 138 |

The OU filter improves every summary statistic and is stable across nearby
window/refit settings, but two of three segments are still negative. This is a
better model, not evidence of a durable edge. Tuning the OU window on the
pre-holdout sample (`w=60`, `r=10`, `o=0.7`) did **not** carry into the holdout,
which is another reminder that the remaining problem is the signal, not the
filter.

## Limitations

- Johansen uses one lag, centered levels and approximate critical values.
- The pair beta is fixed between screens; the `ou_dynamic` variant adapts it but
  did not improve results in this sample.
- The default run uses estimated spreads; `spreads_csv` switches to a captured
  snapshot, but that snapshot is static per symbol rather than a time series.
- This is an example, not evidence of profitability. It is intentionally
  conservative on liquidity and half-life, so some periods select no pairs.
- Selecting one pair per cluster avoids leg conflicts; a portfolio allocator that
  aggregates overlapping legs would allow more pairs.
- Event risk (mergers, index changes, halts) is not modelled.
