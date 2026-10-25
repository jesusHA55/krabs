# Overnight drift (close to open)

The overnight drift is the tendency for equity returns to accrue between the
close and the next open. The tradable version is:

- buy near the close,
- sell just after the next open,
- repeat.

This cannot be represented with daily bars in Krabs. A market order submitted
after the close fills at the **next open**, which is the exit, not the entry.
Any close-to-open backtest needs intraday bars.

## Daily evidence

`scripts/overnight_study.py` measures `open_t / close_{t-1} - 1` and
`close_t / open_t - 1` and charges a round-trip cost of half-spread plus
slippage on each side.

SPY, 2007–2026, with 1 bps spread and 1 bps slippage per side:

| Metric | Value |
|---|---:|
| Overnight mean | +3.24 bps/night |
| Gross annualized | +8.18% |
| t-stat | 3.1 |
| Round-trip cost | 3.0 bps |
| **Net annualized** | **+0.62%** |
| Intraday mean | +1.58 bps |

The raw effect is large and statistically visible, but **costs consume almost
all of it at index level**. With a 4 bps round trip, SPY is negative.

Sector ETFs, 2020–2026, using the captured static spreads:

| Symbol | Overnight | Net annual | Round-trip cost |
|---|---:|---:|---:|
| SMH | 12.45 bps | +18.2% | 5.2 bps |
| XME | 10.83 bps | +13.4% | 5.5 bps |
| XOP | 10.07 bps | +12.6% | 5.1 bps |
| GDX | 9.35 bps | +11.1% | 4.9 bps |
| XLE | 8.03 bps | +11.1% | 3.6 bps |
| XPH | 5.76 bps | −18.7% | 13.2 bps |

Across the 32 sector ETFs: median net **+1.04%/year**, positive for **20 of
32**. The effect is real in the cross-section but highly cost-sensitive; the
highest-overnight names are often the widest-spread ones.

Run it yourself:

```bash
python3 scripts/overnight_study.py
python3 scripts/overnight_study.py --csv data/samples/dual_momentum.csv --symbols SPY,VEU,AGG
```

## Intraday implementation

`overnight_drift` is a long-only strategy for intraday bars:

- At `entry_time` (default 15:55 ET) it buys each symbol, equal weight across
  `symbols`, using `allocation` total.
- At `exit_time` (default 09:35 ET) the next morning it sets the target to zero.
- One overnight hold per symbol per day; no signal filter.

With 5-minute bars, a bar stamped 15:55 covers 15:50–15:55. Submitting on that
bar fills at the 15:55 open of the next bar (15:55–16:00). The exit submits on
the first bar after the open (09:35) and fills at 09:35. The strategy checks the
session close so it cannot enter after the close.

Data and run:

```bash
python3 scripts/fetch_intraday_sample.py --range 60d
./krabs backtest -c configs/overnight-drift.toml
```

The Yahoo 5-minute sample is only **60 days**. It is enough to validate the
execution logic, not the edge.

## 60-day prototype

Universe: SPY, SMH, XME, XOP. Entry 15:55, exit 09:35, 97% gross, static
spreads from the config.

| Metric | Value |
|---|---:|
| Period | 2026-07-31 → 2026-10-23 |
| Return | −2.39% |
| Max drawdown | −7.14% |
| Round trips | 236 |
| Win rate | 52.97% |
| Profit factor | 0.92 |
| Fills | 476 |
| Rejections | 0 |

This is **not** evidence for or against the strategy: 60 days is a single
regime. It does show that the execution path works — buys fill at the 15:55
bar, sells fill at the 09:40 bar, and there are four overnight holds per
session.

The reported average exposure (2.47%) is misleading for this strategy. The
equity curve has no bars overnight, so the exposure metric only samples the
brief windows where intraday points exist. Actual overnight exposure is ~97%
of equity. Do not use the exposure column for overnight strategies without an
overnight marking scheme.

## Stress

The same 60-day sample, with wider costs and execution shocks:

| Scenario | Return | Sharpe |
|---|---:|---:|
| Baseline | −2.39% | −0.67 |
| Spread ×2 | −4.18% | −1.29 |
| Spread ×3 | −5.93% | −1.92 |
| Delay +1 bar | −5.19% | −4.78 |
| ×2 spread + delay | −6.92% | −6.43 |
| ×3 spread + delay | −8.61% | −8.08 |

Borrow has no effect because the strategy is long-only. Participation and
capacity scenarios can turn the 60-day result positive by changing the fill
path, but the OOS slice remains negative and the sample is far too short to
read anything into it.

## Status

`candidate` — implemented and measured, not in the bank.

Reasons not to bank it yet:

- The daily net edge at index level is thin (+0.62%/yr for SPY) and highly
  sensitive to a single basis point of cost.
- The 60-day intraday sample cannot establish an edge.
- The sector results are in-sample over 2020–2026 and the high-overnight names
  are often the widest-spread names.
- The close and open auction fills are approximations; actual MOC/LOO
  execution, queue position and imbalance are not modelled.
- A proper test needs multi-year minute bars and quote data.

## What would move it forward

1. Fetch multi-year minute bars for a liquid universe (Alpaca or a vendor).
2. Model the closing-auction entry and opening-auction exit explicitly.
3. Re-run the daily study and the intraday backtest out of sample.
4. Test a conditional signal (e.g., only names with high overnight persistence
   and tight spreads) with a holdout, not a cross-sectional in-sample sort.
5. Keep it `candidate` until it passes the same gates as dual momentum.
