# Leveraged trend (200-day SMA)

`leveraged_trend` is a long-only timing overlay for 2x/3x index ETFs:

- If the **signal** asset's close is above its 200-day simple moving average,
  hold the leveraged trade ETF.
- Otherwise hold the defensive asset (BIL/T-bills).

It rebalances monthly. The signal uses the last completed bar before the first
bar of the new month, and orders fill on the next bar. The default signal is
the underlying index ETF (SPY or QQQ), not the leveraged wrapper, so the filter
tracks the economic trend rather than the wrapper's volatility-decay path.
`signal_symbol` may be set equal to `trade_symbol` to filter the leveraged ETF
on its own price.

## Data

```bash
python3 scripts/fetch_sample_data.py --only leveraged_trend --start 2010-01-01
```

This writes `data/samples/leveraged_trend.csv` with SPY, QQQ, SSO, UPRO, QLD,
TQQQ and BIL. TQQQ starts in February 2010; the 200-day warm-up delays the
first trade to late 2010.

## Run

```bash
./krabs backtest -c configs/leveraged-trend.toml
```

Default: SPY signal, UPRO (3x S&P) risk-on, BIL defensive.

## Rules

```
sma200 = mean(last 200 daily closes of signal_symbol, excluding today)
risk_on = close(signal) > sma200
target = trade_symbol if risk_on else defensive_symbol
```

Allocation is 97% (the risk engine keeps a 2% buying-power buffer). One target
per month; sells are submitted before buys so a switch does not need an
unfilled sell for cash.

## Results

2010-01 → 2026-10, first trade 2010-11, adjusted prices, costs charged with the
configured spreads. Buy-and-hold is measured from the first trade date.

| Variant | Return | CAGR | Sharpe | Max DD | OOS return |
|---|---:|---:|---:|---:|---:|
| SSO / SPY signal (2x) | +710% | 13.26% | 0.64 | −45.8% | +61.3% |
| UPRO / SPY signal (3x) | +1,324% | 17.13% | 0.62 | −61.5% | +78.5% |
| QLD / QQQ signal (2x) | +2,535% | 21.50% | 0.76 | −50.4% | +64.8% |
| TQQQ / QQQ signal (3x) | +5,864% | 27.55% | 0.75 | −68.3% | +68.3% |
| SSO own-price signal | +724% | 13.38% | 0.66 | −40.2% | +61.3% |
| UPRO own-price signal | +1,235% | 16.68% | 0.63 | −45.8% | +58.4% |
| QLD own-price signal | +1,634% | 18.51% | 0.69 | −50.4% | +64.8% |
| TQQQ own-price signal | +2,173% | 20.43% | 0.64 | −68.3% | +50.6% |

Buy-and-hold from the same start:

| Asset | Return | CAGR | Sharpe | Max DD |
|---|---:|---:|---:|---:|
| SPY | +742% | 14.27% | 0.87 | −33.7% |
| SSO (2x) | +2,660% | 23.09% | 0.79 | −59.3% |
| UPRO (3x) | +6,239% | 29.66% | 0.77 | −76.8% |
| QLD (2x) | +7,282% | 30.91% | 0.86 | −63.7% |
| TQQQ (3x) | +19,784% | 39.28% | 0.85 | −81.7% |

## What this says

The 200-day filter **reduces drawdowns but gives up much more return**. Every
leveraged variant has a lower Sharpe than its buy-and-hold counterpart over
this period:

- UPRO filter: 17.13% CAGR, −61.5% DD, Sharpe 0.62 vs buy-and-hold 29.66%,
  −76.8%, 0.77.
- TQQQ filter: 27.55% CAGR, −68.3% DD, 0.75 vs 39.28%, −81.7%, 0.85.
- SSO filter: 13.26% CAGR, −45.8% DD, 0.64 vs 23.09%, −59.3%, 0.79.

Filtering the leveraged ETF on its own price cuts the UPRO drawdown to −45.8%
at a similar CAGR (16.68%), but the Sharpe is still lower than buy-and-hold.
The 3x variants remain extremely risky even with the filter; a −45% to −68%
drawdown is not a safe profile.

This is the familiar result for trend overlays on leveraged products: the
monthly 200-day rule avoids some prolonged bear markets but is whipsawed in
fast crashes, and the missed upside compounds against it. It is a risk-reduction
tool, not an alpha engine, at least over 2010–2026.

## Stress

| Scenario | Full return | Sharpe | OOS return |
|---|---:|---:|---:|
| Baseline | +1,324% | 0.62 | +78.5% |
| Spread ×3 | +1,309% | 0.62 | +78.1% |
| Delay +1 bar | +1,413% | 0.63 | +70.0% |
| Participation 0.01% | +2,720% | 0.75 | +75.1% |
| Capacity $5M | +1,077% | 0.58 | +58.2% |

Monthly rebalancing makes spread and borrow shocks small; capacity and partial
fills change the path. The stress scenarios are positive, but they do not fix
the underperformance versus buy-and-hold.

## Status

`candidate` — implemented and measured, not banked.

Reasons:

- Lower Sharpe than buy-and-hold for every 2x/3x variant tested.
- The drawdown reduction is real but still very large for 3x products.
- The universe uses current leveraged ETFs; funds that closed are absent.
- Monthly execution misses fast crashes (2020).
- No volatility targeting, tax or financing model beyond the ETF prices.

See `docs/strategy-bank.md`. The bank remains conservative: positive returns
are not enough; the strategy should improve risk-adjusted outcomes or serve a
specific allocation role.
