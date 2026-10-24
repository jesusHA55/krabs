# PCA residual strategy

`pca_residual` trades idiosyncratic ETF returns against a rolling
principal-component hedge. It is the panel-level counterpart to the pairwise
ETF strategy: instead of asking whether two spreads cointegrate, it asks
whether one ETF has moved too far relative to the common factors of the whole
sector panel.

Run it with:

```bash
python3 scripts/fetch_sample_data.py
./krabs backtest -c configs/pca-residual.toml
```

The sample price file is generated, not committed. Captured spreads are
optional; without them the strategy falls back to `default_spread_bps`.

## Model

Every `refit` bars the strategy fits a correlation PCA on the trailing `window`
of daily log returns for all symbols:

```
x_i,t = (r_i,t - mean_i) / sigma_i          standardised returns
C     = X^T X / (T - 1)                     correlation matrix
V     = top K eigenvectors of C             factor loadings (N x K)
F     = X V                                 factor scores
E     = X - F V^T                           idiosyncratic residuals
```

For asset `i`, the residual portfolio is row `i` of the projector
`M = I - V V^T`:

```
q_j = side * M[i, j]            position in standardised-return space
w_j = q_j / sigma_j             actual asset weight
```

Because `M V = 0`, the portfolio has no exposure to the fitted factors,
including the market component. Weights are scaled to `allocation` gross per
signal. The entry signal is

```
z_i = E[i] / residual_sd[i]
```

A positive `z` is a high residual, so the strategy **shorts the residual**:
short the ETF and hold the factor-mimicking basket. A negative `z` goes long
the residual.

The fit uses returns up to the current close and orders fill on the next bar.
An open signal keeps the hedge vector from its entry fit, so a later PCA refit
cannot silently redefine the position; exits use that same fit.

## Portfolio rules

- One signal per symbol; up to `max_positions` concurrent signals.
- `allocation` is the gross weight of one signal (default 10%).
- `max_legs` keeps only the largest hedge legs and renormalises. A full
  32-asset hedge traded too many small legs and lost money to costs; six legs
  keeps most of the hedge at a fraction of the turnover.
- Total target gross is capped at `max_gross`.
- Targets are resubmitted only when a signal opens or closes, not every bar, so
  positions are allowed to drift between events.
- Exit when `z` crosses `exit_z` in the reversion direction, when `z` moves
  further against the trade than `stop_z`, or after `max_hold_bars`.
  `min_hold_bars` stops the exit rule from closing a fresh trade on noise.
- The entry cost filter compares the expected reversion (from `z` to zero) with
  twice the one-way trading cost of the chosen legs:

  ```
  expected = |w_i| * sigma_i * |z| * residual_sd_i
  require expected >= friction_multiple * 2 * sum_j |w_j| * cost_bps_j
  ```

  where `cost_bps_j = spread_j / 2 + slippage_bps`.

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `symbols` | ETF panel | Tradeable panel; must match the feed symbols |
| `window` | 120 | trailing return rows for the PCA |
| `refit` | 20 | bars between PCA refits |
| `components` | 5 | number of factors hedged |
| `entry_z` | 2.5 | absolute residual entry threshold |
| `exit_z` | 1.0 | reversion exit band |
| `stop_z` | 4.0 | adverse residual stop |
| `friction_multiple` | 1.0 | required edge / cost multiple |
| `allocation` | 0.10 | gross per signal |
| `max_legs` | 6 | hedge legs kept (traded asset always included) |
| `max_positions` | 4 | concurrent signals |
| `max_gross` | 0.60 | aggregate target gross |
| `max_hold_bars` | 40 | time stop |
| `min_hold_bars` | 3 | minimum holding period before a reversion exit |
| `min_resid_bps` | 5.0 | minimum residual daily volatility |
| `max_bid_ask_bps` | 5.0 | traded asset liquidity filter |
| `spreads_csv` | `""` | captured spread table |
| `slippage_bps` | 1.0 | cost estimate used by the entry filter |

## Results

Sample 2020-01-02 → 2026-10-23, 32 ETFs, captured spreads and transaction
costs charged by the paper broker:

| Metric | Net | Gross (no execution cost) |
|---|---:|---:|
| Total return | +4.13% | +7.41% |
| CAGR | 0.60% | 1.06% |
| Sharpe (daily) | +0.32 | 0.56 |
| Sortino | 0.32 | — |
| Max drawdown | −4.44% | −3.67% |
| Average exposure | 18.7% | 18.7% |
| Turnover | 129.2× equity | 129.4× |
| Fills | 6,612 | 6,612 |
| Win rate | 49.2% | — |
| Profit factor | 1.06 | — |

Cost drag is about 3.3 percentage points. The result looks positive, but it is
entirely a 2020 effect:

| Segment | Return | Sharpe | Max DD | Fills |
|---|---:|---:|---:|---:|
| 2020 | **+5.47%** | **+2.70** | −0.71% | 435 |
| 2021 → 2023 | −1.07% | −0.25 | −3.57% | 2,001 |
| 2023 → 2025-06 | +0.21% | +0.06 | −2.17% | 2,564 |
| 2025-06 → 2026-10 (OOS) | −0.43% | −0.15 | −1.83% | 1,612 |

The parameters were selected on the pre-holdout sample (2020-01 → 2025-06),
where the strategy returned +4.54% (Sharpe 0.44). The holdout is negative.
Nearby settings also cluster around +1.5% to +4.5% pre-holdout and −1.3% to
+0.2% out of sample: a pre-holdout plateau, no OOS edge.

For comparison, the OU pairs model returned −1.55% for the full sample
(Sharpe −0.21) but +1.26% in the same OOS segment (Sharpe 1.51). Neither model
is validated; PCA is the better full-sample number only because of 2020.

## Numerical note

The first implementation used the repository's fixed-sweep Jacobi
eigendecomposition. At 32 assets and 80 sweeps it left a reconstruction error
of ~0.32 in the correlation matrix, so the "factors" were inaccurate and the
hedges were not neutral. Correcting this changed the OOS result from +2.55% to
−0.43%. The fit now uses subspace iteration (`linalg.top_eigenvectors`) and
reconstruction error is negligible. Inaccurate linear algebra can manufacture
backtest alpha; check it before trusting a residual strategy.

## Selection path and caveats

- The default parameters were chosen on the pre-holdout sample and the holdout
  was then inspected. It is not a clean walk-forward.
- The dense 32-leg hedge produced +3.98% gross but −2.04% net and 297× turnover.
  Sparsifying to six legs and widening the exit band cut turnover to 129×.
- Survivorship bias: the universe is today's ETF list.
- Spreads are a static captured snapshot, not a historical time series, and
  short-borrow costs are not modelled.
- `max_legs` deliberately trades exact factor neutrality for lower turnover;
  residual factor exposure remains.
- The component count and hedging horizon are fixed, not cross-validated.
- Hedges are not rebalanced between signal events, so factor drift can
  accumulate on longer holds.
- The strategy is research code, not a deployment candidate.
