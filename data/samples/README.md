# Sample historical data (daily bars, by sector)

This directory is a **local, regenerable sample** for trying `krabs backtest
--csv`. It is deliberately not committed to git (`*.csv` is ignored) because the
data comes from Yahoo Finance and redistribution is subject to their terms.

## What is here

| File | Sector | Symbols |
|---|---|---|
| `information_technology.csv` | Information Technology | AAPL, MSFT, NVDA |
| `financials.csv` | Financials | JPM, BAC, GS |
| `health_care.csv` | Health Care | JNJ, UNH, PFE |
| `consumer_discretionary.csv` | Consumer Discretionary | AMZN, TSLA, HD |
| `consumer_staples.csv` | Consumer Staples | PG, KO, WMT |
| `energy.csv` | Energy | XOM, CVX, COP |
| `industrials.csv` | Industrials | CAT, UNP, HON |
| `materials.csv` | Materials | LIN, SHW, FCX |
| `utilities.csv` | Utilities | NEE, DUK, SO |
| `real_estate.csv` | Real Estate | PLD, AMT, SPG |
| `communication_services.csv` | Communication Services | GOOGL, META, DIS |
| `benchmark.csv` | S&P 500 ETF (benchmark) | SPY |

- `etf_sectors.csv` — 32 sector/industry ETFs for the `etf_pairs` example.
- Date range: **2020-01-01 → today** (generated at download time).
- Timeframe: **1Day**, timestamps at the US session close (UTC).
- Prices: adjusted for splits and **dividends**; volume is unadjusted.
- Schema: `ts,symbol,open,high,low,close,volume`.
- Metadata, row counts and failures: `manifest.json`.

## Use it

```bash
# one sector (symbols are auto-detected from the CSV)
./krabs backtest -c configs/sample-daily.toml --csv data/samples/energy.csv

# the default technology file configured in sample-daily.toml
./krabs backtest -c configs/sample-daily.toml

# benchmark
./krabs backtest -c configs/sample-daily.toml --csv data/samples/benchmark.csv \
    --strategy buy_hold --param allocation=0.95

# optimize parameters on one sector (holdout is the final 20% of history)
./krabs optimize -c configs/sample-daily.toml --csv data/samples/energy.csv \
    --strategy sma_cross --space fast=5:30 --space slow=40:120 \
    --space allocation=0.1:0.3 --method de --trials 40 --folds 3

# compare two strategies on the same sector
./krabs backtest -c configs/sample-daily.toml --csv data/samples/financials.csv \
    --strategy rsi_reversion --param period=14 --param allocation=0.2
```

Useful flags:

```bash
--timeframe 1Day          # required if your config still says 1Min
--symbols AAPL,MSFT       # narrow the universe
--strategy NAME           # override [strategy].name
--param key=value         # override strategy parameters
--days N                  # synthetic lookback only; ignored for CSV
```

`--csv` makes the file self-describing: it reads the unique symbols and
replaces `[market].symbols`, so the risk engine accepts them.

## Regenerate / extend

```bash
python3 scripts/fetch_sample_data.py
python3 scripts/fetch_sample_data.py --start 2018-01-01 --raw
```

The script uses Yahoo's public chart endpoint (unofficial, no SLA) and sleeps
between requests. Edit `SECTORS` in the script to add symbols or sectors.

## Better data sources

| Source | Cost | Good for | Caveats |
|---|---|---|---|
| **Alpaca Market Data** | Free Basic / $99/mo Pro | US stocks & ETFs; already integrated | Basic = IEX real-time, SIP 15-min delayed, 200 req/min; Pro = full SIP |
| **Yahoo Finance** | Free | Daily bars, quick samples | Unofficial, no SLA, limited intraday history, survivorship/selection bias |
| **Stooq** | Free | Daily US/EU quotes | Bot protection; browser/manual use |
| **Polygon.io** | Paid (free tier) | Intraday US equities, aggregates | Rate/plan limits; check adjusted data |
| **Databento** | Paid | High-quality intraday/tick | Pricier; overkill for daily |
| **Tiingo / EODHD** | Paid/freemium | EOD and intraday | Plan limits; verify delisted coverage |
| **Sharadar (Nasdaq Data Link)** | Paid | Survivorship-bias-free fundamentals/prices | Subscription |
| **Norgate Data** | Paid | Delisted-inclusive daily history | Windows-centric tooling |
| **CRSP / Compustat (WRDS)** | Academic | Gold-standard research | Institutional access |
| **Kenneth French Data Library** | Free | Industry portfolios, factors | Returns, not OHLC bars |
| **SEC EDGAR / FRED** | Free | Fundamentals, macro | Not price data |

For industry research, GICS is licensed; alternatives are ICB, NAICS, SEC SIC
codes, Kenneth French's 5/12/17/30/48 industry portfolios, or liquid sector
ETFs (XLK, XLF, XLE, ...). Our sample uses GICS-style sectors with current large
caps, which is convenient but not survivorship-bias-free.

## Read this before drawing conclusions

1. **Survivorship bias.** The universe is today's large caps. Companies that
   failed or were acquired are absent, so returns are optimistic.
2. **Selection bias.** Three large caps per sector are not a sector index.
3. **Adjusted prices.** Total-return adjustment is right for signal research,
   but limit/stop levels are not the raw prices you would have seen intraday.
4. **No intraday data.** Daily bars cannot validate stop placement, slippage,
   partial fills or anything that depends on the path within a day.
5. **Yahoo is not a research-grade source.** Use CRSP/Compustat, Sharadar,
   Norgate, Databento or Polygon for serious work, and point-in-time index
   membership for survivorship-bias-free universes.

For a quick sanity check of the data, compare `buy_hold` on a sector with
`benchmark.csv` over the same dates. If the sample is loaded correctly, both
should produce plausible equity curves.
