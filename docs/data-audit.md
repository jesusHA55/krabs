# Data audit

`scripts/audit_data.py` checks what a backtest is actually built on. It does not
fetch data; it validates the files and reports what is still missing.

```bash
python3 scripts/audit_data.py configs/etf-pairs.toml
python3 scripts/audit_data.py configs/pca-residual.toml --strict
```

Reports are written to `reports/data-audit/audit.json`. `--strict` returns a
non-zero exit code if any warning is present, which is useful in CI.

## What it checks

| Area | Checks |
|---|---|
| Bars | Symbol/row/session counts, start/end, missing sessions vs. the exchange calendar, duplicate timestamps, non-positive prices, daily moves above 50% |
| Spreads | Static vs. time series, symbol coverage, series date range, multiplier, missing feed symbols |
| Borrow | Symbol coverage, unavailable names, median/max rates, missing feed symbols |
| Universe | Listing count, delisted count, symbols missing from either side, as-of counts at the start and end |
| Warnings | Static spreads, no borrow table, no delisted funds, missing metadata |

## Current sample

`configs/etf-pairs.toml`, 2020-01-02 → 2026-10-23:

```
bars      32 symbols, 54,784 rows, 1,712 sessions
coverage  expected 1712 sessions; missing=0; duplicates=0
quality   non-positive=0, >50% moves=0
spreads   static; static=32, series=0, missing=0
borrow    symbols=0, unavailable=0
universe  listings=32, delisted=0, as-of start/end=32/32
warnings  3
  - spreads are static per symbol; no historical time variation
  - no borrow table; only execution.borrow_bps_per_year applies
  - universe has no delisted funds; results remain survivorship-biased
```

The bars themselves are clean after fixing the exchange calendar: the holiday
table had been missing 2020–2023 and the 2025-01-09 market closure, which made
the sample look like it had 37 gaps per symbol. It does not.

The audit still fails the **investability** test:

1. **Static spreads.** One median per symbol is applied to 2020–2026. Real
   spreads widen in stress and vary by time of day.
2. **No borrow table.** The strategy can short any ETF for free unless a global
   rate is set; availability and recalls are not modelled.
3. **No delisted funds.** The universe is today's ETF list with approximate
   inception dates. Funds that closed or changed mandate are absent, so
   selection is survivorship-biased.

## Metadata formats

### Point-in-time universe

```csv
symbol,name,listed,delisted,cluster,source
XLK,Technology Select Sector SPDR,1998-12-16,,technology,approximate
OLD,Delisted Sector ETF,2005-01-03,2021-06-30,energy,vendor
```

`delisted` empty means still listed. The loader is
`krabs.market.universe.PointInTimeUniverse`; the audit reports delisted counts
and whether the feed symbols have listing rows. **The sample manifest does not
contain dead funds and is not enforced by the feed yet.** Real PIT metadata from
a vendor is required to remove survivorship bias.

### Historical spreads

```csv
date,symbol,spread_bps
2020-01-02,XLK,1.4
2020-03-16,XLK,8.2
```

`spreads_csv` accepts either the static sample format or this time-series
format. Lookups are as-of: the most recent observation at or before the bar is
used, never a future one. The same table feeds both the strategy's liquidity
filter and the paper broker's half-spread. A missing symbol falls back to
`default_spread_bps`.

### Borrow

```csv
symbol,borrow_bps_per_year,available
XLK,40,true
XTL,350,true
TINY,2500,false
```

Per-symbol rates are charged daily on short market value. `available=false` is
reported by the audit; the engine still charges the rate but does not yet block
a short entry. A real locate/borrow check belongs in the live risk layer.

## How to improve the data

1. Get a point-in-time ETF universe that includes delisted and merged funds.
2. Get historical NBBO or effective spreads (daily or intraday), not one static
   median.
3. Get borrow rates and availability, at least for the short leg universe.
4. Re-run the audit with `--strict` and require zero warnings before tuning.
5. Re-run the stress matrix; the combined scenario is the gate.

Until then, the strategies can be researched, but no result here should be
treated as investable.
