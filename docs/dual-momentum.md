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
python3 scripts/fetch_sample_data.py --only dual_momentum --start 2010-01-01
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

Sample 2010-01 → 2026-10, with the first rebalance in 2011-02 after the
12-month warm-up. Adjusted prices; costs charged with the per-symbol spreads in
the config:

| Metric | Dual momentum | SPY buy & hold |
|---|---:|---:|
| Total return | +220.8% | +708.0% |
| CAGR | 7.18% | 13.98% |
| Sharpe (daily) | 0.54 | 0.86 |
| Sortino | 0.48 | 1.06 |
| Max drawdown | −32.8% | −33.7% |
| Average exposure | 90.8% | 100% |
| Turnover | 57.6× equity | — |
| Round trips | 134 | — |
| Win rate | 85.8% | — |
| Profit factor | 3.51 | — |
| Rejections | 0 | — |

Selection breakdown over 189 monthly decisions: SPY 134, VEU 35, AGG 20.

**GEM underperformed a simple SPY buy-and-hold over this particular period**
and had almost the same drawdown. That is a real result, not an implementation
failure:

- 2010–2026 was an unusually persistent US large-cap bull market. Ex-US equities
  lagged for most of it, and bonds had a poor 2022.
- Relative momentum selected VEU for 35 months and the defensive AGG for 20
  months; those periods dragged on the compounded return.
- GEM's protection is against prolonged equity bear markets, not fast crashes.
  It did not avoid the COVID crash; the absolute filter reacts at the next
  month-end.

The original Antonacci results cover a different, longer sample (1972 onward)
with different US/ex-US/bond behavior. This backtest should not be read as a
refutation of the framework, but it also does not reproduce the historical
premium.

## Stress

The stress harness was also run on this config. Because the sample starts in
2010, its final 20% OOS window is **2023-04 → 2026-10**, not the 2025-06 window
used for the ETF strategies.

| Scenario | Full return | Sharpe | OOS return | Sharpe | Fills |
|---|---:|---:|---:|---:|---:|
| Baseline | +220.8% | 0.54 | +72.3% | 1.12 | 218 |
| Spread ×3 | +217.7% | 0.53 | +71.8% | 1.11 | 218 |
| Borrow 3% | +220.8% | 0.54 | +72.3% | 1.12 | 218 |
| Delay +1 bar | +239.6% | 0.56 | +67.2% | 1.06 | 218 |
| Participation 0.01% | +189.6% | 0.48 | +66.7% | 1.05 | 602 |
| Capacity $5M | +335.3% | 0.66 | +69.2% | 1.09 | 1,612 |
| ×3 spread + 3% borrow + delay | +236.3% | 0.55 | +66.7% | 1.06 | 218 |

Monthly rebalancing keeps turnover low, so spread and borrow shocks barely
matter; borrow is zero because the strategy is long-only. The 0.01%
participation cap does bite, and the $5M path differs because partial fills
change the entry prices. The OOS result is strong in 2023–2026, but that is a
favorable period for both US and ex-US equities; it does not change the
full-sample comparison with SPY.

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
- The sample universe is today's ETF list, with survivorship bias.
- Spreads are static per symbol unless a time-series table is supplied.
- A 2–3% cash sleeve is held for the risk engine's buying-power buffer.
- This is a research implementation; it is not a recommendation.
