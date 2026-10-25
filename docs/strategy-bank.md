# Strategy bank

The bank is a curated list of strategies that have passed a defined evidence
bar. “In the bank” is not the same as “proven” or “ready for capital.” Each
entry records its status, evidence and the gates it has not passed yet.

## Status levels

| Status | Meaning |
|---|---|
| `candidate` | Implemented and measured, but the evidence is insufficient for the bank |
| `rejected` | Failed the full-sample, sensitivity or stress tests, or has no economic rationale worth pursuing |
| `research-approved` | Positive full sample, positive OOS, positive sensitivity and stress; still a research candidate |
| `paper-approved` | Passed walk-forward and a paper-trading period with the production runtime |
| `live-approved` | Paper-approved plus operational, risk, monitoring and capital-allocation review |

The current bank has one `research-approved` entry, one `candidate` and two
`rejected` entries.

## Entry criteria

A strategy is considered for `research-approved` only when:

1. **Economic rationale** — there is a reason the edge should exist, not just a
   backtest pattern.
2. **Causal implementation** — no look-ahead; signals use data up to the bar,
   and fills happen on the next bar.
3. **Full-sample result** — positive return and a drawdown that can be
   tolerated; a positive Sharpe alone is not enough.
4. **Out-of-sample** — positive after the selection period.
5. **Sensitivity** — reasonable parameter neighbours are also positive; the
   result is not a single spike.
6. **Stress** — survives wider spreads, borrow, execution delay and capacity.
7. **Data audit** — no unexplained gaps, duplicates or price outliers; the
   remaining metadata gaps are documented.
8. **Reproducibility** — config, data command, report and tests are committed.

`paper-approved` additionally requires a walk-forward test and at least three
months of paper trading with the production runtime.

## Bank entries

### `dual_momentum` — research-approved

| Field | Value |
|---|---|
| Strategy | `src/krabs/strategy/examples/dual_momentum.py` |
| Config | `configs/dual-momentum.toml` |
| Docs | `docs/dual-momentum.md` |
| Data | `python3 scripts/fetch_sample_data.py --only dual_momentum --start 2007-01-01` |
| Universe | SPY / VEU (risk), AGG (defensive), BIL (absolute hurdle) |
| Rebalance | Monthly, month-end signal, next-bar fills |
| Sample | 2007-01 → 2026-10 |
| Full return | +310.7% |
| CAGR | 7.39% |
| Sharpe | 0.55 |
| Max drawdown | −32.8% |
| OOS (2022-11 → 2026-10) | +79.3%, Sharpe 1.08 |
| 2008 | +6.2% vs SPY −36.2% |
| Stress | Positive in every scenario: ×3 spreads, 3% borrow, delay, participation, $5M capacity |
| Sensitivity | Positive for 6/9/12/15-month lookbacks, top-2, zero hurdle and BIL defensive |

**Why it passed:** positive across the full sample, positive in the final 20%,
positive in every sensitivity and stress variant, with a clear economic
rationale (relative plus absolute momentum) and a materially smaller GFC
drawdown than SPY.

**What it did not prove:** it underperformed SPY over 2007–2026 (+310.7% vs
+670.6%; Sharpe 0.55 vs 0.63). It kept the drawdown much lower (−32.8% vs
−55.2%) but did not avoid the fast 2020 crash or the 2022 bond selloff.

**Open gates before paper trading:**

- Walk-forward test with a rolling out-of-sample window.
- Paper trading for at least three months with the production runtime.
- Data audit with point-in-time metadata, historical spreads and borrow data.
- Decide whether the lower return is acceptable for the risk reduction, or
  whether it belongs in a sleeve alongside a return-seeking allocation.
- The universe is four current broad ETFs; earlier history requires index or
  mutual-fund data.

### `overnight_drift` — candidate

Intraday close-to-open prototype: buy at 15:55 ET, sell at 09:35 ET the next
morning, equal weight across SPY/SMH/XME/XOP.

- Daily study: SPY gross +8.18%/yr but net **+0.62%/yr** after a 3 bps round
  trip; 20/32 sector ETFs positive, median +1.04%/yr.
- 60-day 5-minute prototype: −2.39%, 236 round trips, 52.97% win rate,
  profit factor 0.92. Too short to conclude; execution validated.
- Not banked: cost-sensitive, 60-day sample, auction fills approximate, no
  multi-year minute data.

See `docs/overnight-drift.md`.

### `etf_pairs` (OU) — rejected

Full sample −1.66%, Sharpe −0.23; warm-start optimizer folds negative; the
positive OOS is small and not supported in-sample. At ×3 spreads the strategy
stops trading. See `docs/etf-pairs.md` and `docs/stress-testing.md`.

### `pca_residual` — rejected

Full sample +4.13% is driven by 2020; the 2025-06 → 2026-10 holdout is
negative (−0.43%), and at $5M capacity it is −1.97% OOS. The combined
spread/borrow/delay scenario is negative. See `docs/pca-residual.md`.

## How to add an entry

1. Run the data audit and record any remaining warnings.
2. Run the full backtest and the final-20% OOS.
3. Run the sensitivity matrix and the stress harness.
4. Add the evidence table here and link the config, docs and report paths.
5. Do not move the status to `paper-approved` without a walk-forward and a
   paper-trading record.

The bank is deliberately conservative. A strategy can be interesting without
being in it; the bank is about evidence, not enthusiasm.
