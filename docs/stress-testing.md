# Stress testing costs and execution

`scripts/stress.py` re-runs a config under harsher execution assumptions. It is
meant to answer one question: **does the result survive when the easy
assumptions are removed?**

```bash
python3 scripts/stress.py configs/etf-pairs.toml configs/pca-residual.toml
```

Each scenario runs over the full sample and over the final 20% (with the
configured warm-up prefix). Results are written to
`reports/stress/stress-<timestamp>/results.{json,csv}`.

## Levers

| Lever | Config | Effect |
|---|---|---|
| Spread multiplier | `execution.spread_multiplier` and the strategy's `spread_multiplier` | Scales the quoted spread used by both the liquidity filter and the paper fill model, so the strategy pays the wider spread it sees |
| Borrow rate | `execution.borrow_bps_per_year` | Charged daily on short market value and deducted from cash |
| Execution delay | `execution.delay_bars` | Holds newly accepted orders for extra bars before they are eligible to fill |
| Participation | `execution.max_participation` | Caps each fill at a fraction of bar volume; remaining quantity stays open |

Strategies can inspect pending orders through `ctx.open_orders()` and
`ctx.has_open_order(symbol)`. The ETF pair strategy waits while either leg has
a live order; the PCA strategy defers rebalances until accepted orders finish.
Without that, delayed or partially filled orders can be stacked and create
unintended gross exposure.

## Scenarios

- `baseline`
- `spread_2x`, `spread_3x`
- `borrow_1pct`, `borrow_3pct`
- `delay_1bar`
- `participation_0.01pct`
- `combo_2x_borrow1_delay1`
- `combo_3x_borrow3_delay1`

## Results (config defaults, 2020-01 → 2026-10)

Fresh OOS run starts 2025-06-13 with the configured warm-up prefix; it is not
the continuation segment.

### ETF pairs (`configs/etf-pairs.toml`)

| Scenario | Full return | Full Sharpe | OOS return | OOS Sharpe | Fills |
|---|---:|---:|---:|---:|---:|
| Baseline | −1.66% | −0.23 | +0.16% | +0.13 | 514 |
| Spread ×2 | −0.79% | −0.20 | +1.34% | +1.66 | 196 |
| Spread ×3 | no trades | — | no trades | — | 0 |
| Borrow 1% | −2.13% | −0.30 | +0.04% | +0.04 | 514 |
| Borrow 3% | −3.07% | −0.44 | −0.20% | −0.14 | 514 |
| Delay +1 bar | −2.88% | −0.42 | +0.28% | +0.23 | 514 |
| Participation 0.01% | −1.25% | −0.18 | +0.14% | +0.11 | 580 |
| ×2 + 1% borrow + delay | −1.23% | −0.32 | +1.46% | +1.86 | 200 |
| ×3 + 3% borrow + delay | no trades | — | no trades | — | 0 |

At ×3 spreads every leg fails the 5 bps liquidity filter. The strategy
"survives" by withdrawing, which is not the same as surviving with a positive
expectancy. Borrow costs and a one-bar delay both make the full-period result
worse; the OOS window remains small and noisy.

### PCA residual (`configs/pca-residual.toml`)

| Scenario | Full return | Full Sharpe | OOS return | OOS Sharpe | Fills |
|---|---:|---:|---:|---:|---:|
| Baseline | +4.13% | +0.32 | +0.05% | +0.03 | 6,612 |
| Spread ×2 | +0.70% | +0.08 | +1.82% | +1.06 | 4,839 |
| Spread ×3 | −0.07% | −0.01 | +1.01% | +0.77 | 3,557 |
| Borrow 1% | +3.55% | +0.28 | −0.08% | −0.02 | 6,612 |
| Borrow 3% | +2.40% | +0.19 | −0.34% | −0.12 | 6,612 |
| Delay +1 bar | +7.31% | +0.56 | +0.64% | +0.27 | 6,136 |
| Participation 0.01% | +9.90% | +0.72 | +0.05% | +0.03 | 5,073 |
| ×2 + 1% borrow + delay | −1.07% | −0.10 | +0.92% | +0.54 | 4,560 |
| ×3 + 3% borrow + delay | −1.82% | −0.29 | −0.02% | −0.01 | 3,554 |

The full-period PCA result is not robust to spreads: at ×2 it falls to +0.70%,
at ×3 it is flat, and the combined stress is negative. The OOS window stays
near zero in the baseline and in the combined stress. Delay and participation
change the path materially, which is sensitivity, not evidence of edge.

## Interpretation rules

- A strategy that stops trading under stress has not "survived"; it has
  withdrawn. Say so explicitly.
- Compare scenarios against the **same baseline run** in the same report. The
  pending-order guard changes trade paths even at baseline.
- This harness is not a substitute for point-in-time data: spreads are still a
  static table scaled by a multiplier, borrow has no availability/recall, and
  the universe is still survivorship-biased.
- At $100k, the 0.01% participation cap rarely binds for these ETFs; capacity
  becomes relevant at larger AUM or smaller names.
