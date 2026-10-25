# Dual momentum (GEM)

`dual_momentum` is a long-only implementation of Gary Antonacci's dual momentum
framework, configured as Global Equities Momentum (GEM):

- **Relative momentum:** rank the risk assets by trailing 12-month return.
- **Absolute momentum:** keep only the winner(s) that beat the absolute
  benchmark (T-bills, proxied by `BIL`).
- **Defensive fallback:** if no risk asset passes, hold aggregate bonds (`AGG`).

The classic universe is US equities (`SPY`), ex-US equities (`VEU`), bonds
(`AGG`) and T-bills (`BIL`). The risk assets, defensive asset, absolute
benchmark, lookback and top-N are configurable, so the same engine can run a
sector-rotation variant.

## Rules

At the first bar of each month, using month-end closes **before** that bar:

```
r_12m(symbol) = close(last_month_end) / close(month_end_12_months_ago) - 1
ranked        = risk assets sorted by r_12m descending
selected      = first top_n ranked assets with r_12m > r_12m(absolute_asset)
if selected is empty:
    target = defensive_asset
else:
    target = selected, equal weight within `allocation`
```

The strategy sets the target for every symbol in its universe, so leaving a
risk asset also liquidates it. Sells are submitted before buys so a switch does
not depend on an unfilled sell for buying power.

The signal is causal:

- the current bar is excluded from the 12-month calculation;
- the decision is made on the first bar of the new month;
- orders fill on the following bar;
- monthly rebalancing, not daily.

## Data

The sample data fetch now includes the four ETFs:

```bash
python3 scripts/fetch_sample_data.py --only dual_momentum --start 2007-01-01
```

This writes `data/samples/dual_momentum.csv` with adjusted daily bars for
`SPY`, `VEU`, `AGG` and `BIL`. Adjusted prices include dividends, which is what
the total-return comparison needs.

## Run it

```bash
./krabs backtest -c configs/dual-momentum.toml
```

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `risk_assets` | `["SPY", "VEU"]` | Assets ranked by relative momentum |
| `defensive_asset` | `"AGG"` | Held when no risk asset passes |
| `absolute_asset` | `"BIL"` | Absolute benchmark; its 12m return is the hurdle |
| `absolute_threshold` | `0.0` | Used when `absolute_asset` is empty |
| `lookback_months` | 12 | Momentum window in months |
| `top_n` | 1 | Number of risk assets held (equal weight) |
| `allocation` | 0.97 | Gross invested fraction; the rest is cash |
| `driver` | `"SPY"` | Bar that triggers the once-per-month rebalance |

`allocation` is 0.97 rather than 1.0 because the risk engine reserves a 2%
buying-power buffer. Setting 0.98 leaves the order exactly at the buffer and
produces rejected buys; 0.97 is the largest clean setting.

## Results

Sample 2007-01 → 2026-10, including the global financial crisis. The first
rebalance is 2008-06 after the 12-month warm-up. Adjusted prices; costs charged
with the per-symbol spreads in the config.

| Metric | Dual momentum | SPY buy & hold |
|---|---:|---:|
| Total return | +310.7% | +670.6% |
| CAGR | 7.39% | 10.86% |
| Sharpe (daily) | 0.55 | 0.63 |
| Sortino | 0.49 | 0.77 |
| Max drawdown | **−32.8%** | **−55.2%** |
| Calmar (CAGR / maxDD) | 0.225 | 0.197 |
| Average exposure | 90.1% | 100% |
| Turnover | 64.8× equity | — |
| Round trips | 156 | — |
| Win rate | 86.5% | — |
| Profit factor | 3.67 | — |
| Rejections | 0 | — |

Selection breakdown over 221 monthly decisions: SPY 142, VEU 43, AGG 36.

### By regime

| Period | GEM | SPY |
|---|---:|---:|
| 2008 | **+6.2%** | −36.2% |
| 2020 | −3.8% | +17.2% |
| 2022 | −17.4% | −18.7% |
| Full 2007–2026 | +310.7% | +670.6% |

GEM did its job in the GFC: it moved to bonds in June 2008, avoided most of the
equity crash, and kept the maximum drawdown to −32.8% versus −55.2% for SPY.
It still underperformed SPY on total return because:

- 2010 onward was a persistent US large-cap bull market; ex-US equity and
  bond sleeves lagged.
- GEM's absolute filter reacts at month-end, so it did not avoid the fast COVID
  crash; it held equities into March 2020 and then sold after the fall.
- Defensive bonds did not protect in 2022.

This is a risk-control result, not a return-maximization result. It is also the
first strategy in this project that stays positive across the full sample and
the stress matrix.

## Sensitivity

All variants are positive over 2007–2026, so the sign is not a single-parameter
artifact. OOS is the final 20% (2022-11 → 2026-10).

| Variant | Return | CAGR | Sharpe | Max DD | OOS | OOS Sharpe |
|---|---:|---:|---:|---:|---:|---:|
| Lookback 6m | +215.8% | 5.98% | 0.48 | −32.8% | +88.5% | 1.28 |
| Lookback 9m | +156.5% | 4.87% | 0.40 | −32.8% | +86.1% | 1.18 |
| Lookback 12m (default) | **+310.7%** | **7.39%** | **0.55** | **−32.8%** | **+79.3%** | **1.08** |
| Lookback 15m | +381.0% | 8.25% | 0.59 | −32.8% | +62.5% | 0.92 |
| Top 2 risk assets | +241.0% | 6.39% | 0.50 | −33.5% | +79.5% | 1.20 |
| Absolute hurdle = 0 (no BIL) | +337.3% | 7.74% | 0.56 | −32.8% | +89.0% | 1.16 |
| Defensive = BIL | +168.6% | 5.12% | 0.44 | −32.5% | +76.7% | 1.01 |

The 12-month lookback is not the best on this sample; the 15-month variant
returned more. Do not select it from this table: that would fit the sample. The
point is that every reasonable variant is positive and keeps drawdown near
−33%.

## Stress

Stress scenarios on the 2007–2026 data. The final 20% OOS window is
**2022-11 → 2026-10**, not the 2025-06 window used for the ETF strategies.

| Scenario | Full return | Sharpe | OOS return | Sharpe | Fills |
|---|---:|---:|---:|---:|---:|
| Baseline | +310.7% | 0.55 | +79.3% | 1.08 | 252 |
| Spread ×3 | +306.4% | 0.54 | +78.6% | 1.07 | 252 |
| Borrow 3% | +310.7% | 0.55 | +79.3% | 1.08 | 252 |
| Delay +1 bar | +327.9% | 0.56 | +79.3% | 1.08 | 252 |
| Participation 0.01% | +260.3% | 0.49 | +75.1% | 1.03 | 874 |
| Capacity $5M | +511.6% | 0.74 | +72.2% | 1.04 | 2,488 |
| ×3 spread + 3% borrow + delay | +323.4% | 0.56 | +78.6% | 1.07 | 252 |

Monthly rebalancing keeps turnover low, so spread and borrow shocks barely
matter; borrow is zero because the strategy is long-only. The 0.01%
participation cap does bite, and the $5M path differs because partial fills
change the entry prices. Every scenario remains positive.

See `docs/stress-testing.md` for the levers and the other strategies.

## Variants

- **Sector rotation:** set `risk_assets` to sector ETFs, `top_n` to 2–3,
  `defensive_asset` to `BIL` or `AGG`, and keep the absolute filter.
- **Fixed hurdle:** leave `absolute_asset` empty and set
  `absolute_threshold = 0.0` (or a cash yield) to compare returns to zero.
- **More/less defensive:** change `defensive_asset` and `top_n`.

## Limitations

- Only monthly rebalancing; no intra-month risk control.
- No shorting, leverage or volatility targeting.
- The signal uses month-end closes derived from daily bars; missing month-end
  data reduces the candidate set.
- The sample starts in 2007 because VEU and BIL did not exist earlier. Extending
  it to the 1970s requires index or mutual-fund data.
- The universe is four broad asset classes rather than a cross-section of
  sector ETFs, so cross-sectional survivorship bias is much smaller. The four
  funds are still current choices made with hindsight.
- Spreads are static per symbol unless a time-series table is supplied.
- A 2–3% cash sleeve is held for the risk engine's buying-power buffer.
- This is a research implementation; it is not a recommendation.
