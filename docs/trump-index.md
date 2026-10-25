# Trump index research

A public-information event study for the "Trump index" idea. It asks whether
public Truth Social posts and related prediction-market moves predict tradable
sector ETF returns. **It is not a strategy and not banked.** No live trading, no
whale data, and no non-public information.

## Data

| Source | What it gives | Notes |
|---|---|---|
| [CNN Truth Social archive](https://ix.cnn.io/data/truth-social/truth_archive.csv) | 37,251 public posts, 2022-02 → 2026-10, timestamps and text | Public third-party snapshot; no direct Truth Social scraping |
| Kalshi market data | Prediction-market probabilities, candlesticks | Read-only public market data; no account/orders |
| `data/samples/etf_sectors.csv` | 32 sector/industry ETF daily bars, 2020–2026 | Adjusted OHLC |
| `data/samples/benchmark.csv` | SPY daily bars | Benchmark for abnormal returns |

The direct Truth Social API is behind Cloudflare from this environment. The
pipeline uses the public archive instead. The paid Truth API is not used.

```bash
python3 scripts/download_truth_archive.py
python3 scripts/trump_event_study.py
python3 scripts/trump_prediction_study.py
```

## Post → sector event study

- Clean and classify posts by keyword topic.
- Map topics to liquid sector ETFs (tariffs → XLI/XLB/XLF/XLE/XRT/XLP, energy →
  XLE/XOP/OIH/XES, defense → ITA/XAR, etc.).
- Deduplicate to one event per topic per session.
- If a post is before 09:30 ET on a session, the event is that session;
  otherwise the next session.
- Abnormal return = ETF open-to-close minus SPY open-to-close.
- Train: 2022–2024. Test: 2025–2026.

Top results (1-day abnormal return, full sample):

| Topic | ETF | n | Mean bps | t | Train bps | Test bps |
|---|---|---|---:|---:|---:|---:|
| Defense | ITA | 667 | −9.33 | −2.81 | −8.49 | −10.20 |
| Defense | XAR | 667 | −9.74 | −2.65 | −9.91 | −9.55 |
| Energy | XES | 302 | −20.74 | −2.00 | −31.34 | −11.71 |
| Energy | OIH | 302 | −18.44 | −1.91 | −29.90 | −8.66 |
| Immigration | XRT | 845 | −5.99 | −1.84 | −6.28 | −5.57 |
| Tariffs | XLI | 265 | −6.24 | −1.71 | −10.11 | −5.44 |
| Tariffs | XLE | 265 | −8.93 | −1.31 | −35.94 | −3.41 |
| Tariffs | XLB | 265 | −5.89 | −1.28 | −10.15 | −5.02 |

The signs are consistently negative for defense, energy, immigration and tariff
posts, and the train/test signs agree for several pairs. But the effects are
small (6–20 bps), the t-stats are weak after accounting for 22 topic/ETF cells,
and the keyword classifier is noisy (e.g., “defense” can be a legal term).
This is a lead, not an edge.

## Tariff posts + Kalshi probability

Market: `KXTARIFFBILL-27JAN01` — “Will any legislation imposing new or increased
tariffs become law before Jan 1, 2027?” Daily probabilities from Feb–Sep 2026,
39 usable tariff post-days.

| ETF | n | Abnormal bps | Correlation with prior-day move | Correlation with same-day move | Sharp moves n | Sharp mean bps |
|---|---:|---:|---:|---:|---:|---:|
| XLI | 39 | −37.05 | −0.05 | +0.03 | 6 | −40.83 |
| XLB | 39 | −16.94 | −0.16 | +0.22 | 6 | +11.08 |
| XLF | 39 | +3.71 | +0.07 | +0.12 | 6 | −0.69 |

Tariff post-days were followed by a large negative XLI drift, but the
prediction-market move does **not** explain it: correlations are near zero and
the “sharp move” subset is inconsistent. The daily probability timing is also
too coarse to claim that the market move caused the sector move.

## Verdict

**The event-study gate is not passed.**

- There is a weak negative post-day effect in some sectors, but it is small,
  noisy and not clearly causal.
- Prediction-market surprise does not explain the cross-section in this sample.
- Daily bars cannot capture the seconds-to-minutes reaction; the archive and
  Kalshi series have different timestamps and update frequencies.
- The keyword classifier is a first pass, not a policy-event model.
- Multiple testing and overlapping events are unaddressed.

No long/short strategy was implemented from this. Doing so would be a
storytelling backtest.

## What would make it credible

1. **Intraday timestamps** for posts, Kalshi quotes and ETF bars (1-minute or
   tick), with a latency model.
2. **Event taxonomy** with hand labels or a carefully evaluated classifier,
   not just keywords.
3. **Prediction-market mapping** from a specific post to the relevant market,
   with liquidity and volume filters.
4. **Causal design**: measure the market surprise in a tight window and trade
   only after the post is public; compare with a matched non-event day.
5. **Out-of-sample** and multiple-testing control.
6. **Legal/operational review** before any live use. Public information only;
   no non-public tips or manipulation.

## Status

Research pipeline only. Not a strategy-bank entry. The bank still has
`dual_momentum` as the sole `research-approved` entry.
