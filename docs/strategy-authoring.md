# Strategy authoring guide

A Krabs strategy is a Python class with one required method. It never touches a
socket, a broker or a clock. That constraint is what makes the same code
backtestable, paper-tradeable and live-deployable.

---

## 1. Minimal strategy

```python
from krabs.events import Bar
from krabs.strategy import Strategy, register

@register
class Momentum(Strategy):
    id = "momentum"
    description = "Long when the 20-bar return is positive."
    defaults = {"lookback": 20, "allocation": 0.2}

    def __init__(self, **params):
        super().__init__(**params)
        if int(self.p("lookback")) < 2:
            raise ValueError("lookback must be >= 2")

    def on_bar(self, ctx, bar: Bar) -> None:
        closes = ctx.history(bar.symbol, int(self.p("lookback")) + 1)
        if len(closes) < int(self.p("lookback")) + 1:
            return
        momentum = closes[-1] / closes[0] - 1.0
        ctx.target_weight(
            bar.symbol,
            float(self.p("allocation")) if momentum > 0 else 0.0,
            reason=f"20-bar momentum {momentum:+.2%}",
        )
```

Register it by importing the module in `src/krabs/strategy/examples/__init__.py`,
then:

```bash
./krabs backtest -c configs/example.toml --strategy momentum --param lookback=30
```

Parameter types are inferred from `defaults`; `--param lookback=30` becomes an
`int`, allocation becomes a `float`, unknown parameters are rejected.

---

## 2. Lifecycle and hooks

| Hook | When | Use for |
|---|---|---|
| `on_start(ctx)` | Once, before the first bar | Warm state, log parameters, validate. |
| `on_day_start(ctx, day)` | First bar of each US session | Reset daily counters, avoid overnight gaps. |
| `on_bar(ctx, bar)` | Every closed bar, per symbol, in timestamp order | The strategy itself. |
| `on_quote(ctx, quote)` | Only if a streaming feed emits quotes | Microstructure / spread logic. |
| `on_fill(ctx, fill)` | After a fill is confirmed | Track entries, trailing stops, logging. |
| `on_end(ctx)` | End of backtest or graceful shutdown | Summary, persist nothing critical. |

Bars for different symbols at the same timestamp are delivered in alphabetical
symbol order, after the broker has already processed the fills for that
timestamp. New orders cannot fill until the next timestamp.

---

## 3. Context API

### Portfolio

| Call | Returns |
|---|---|
| `ctx.equity`, `ctx.cash` | Floats from the latest account snapshot. |
| `ctx.snapshot()` | Full `AccountState` (buying power, gross, day PnL). |
| `ctx.position(symbol)` | `Position` (qty, avg_price, market_price, unrealized_pnl). |
| `ctx.positions()` | Non-flat positions dict. |

### Market data

| Call | Returns |
|---|---|
| `ctx.bar(symbol)` | Latest `Bar`. |
| `ctx.bars(symbol, n)` | Last `n` bars (oldest first). |
| `ctx.price(symbol)` | Latest close. |
| `ctx.history(symbol, n, field="close")` | Last `n` values of `open/high/low/close/volume`. |
| `ctx.sma(symbol, period)` | Latest SMA (NaN during warm-up). |
| `ctx.ema`, `ctx.rsi`, `ctx.atr` | Same contract. |

History is bounded by `max(1000, 2 × warmup_bars)` bars per symbol. Ask for what
you need; don't scan years of data every bar.

### Orders

| Call | Notes |
|---|---|
| `ctx.target_weight(symbol, w)` | **Preferred.** Rebalances to `w × equity` using a delta market order. |
| `ctx.buy(symbol, qty=...)` / `ctx.sell(...)` | Market by default; supports limit/stop/stop-limit and TIF. |
| `ctx.buy(symbol, notional=...)` | Market only; converted to shares at the reference price. |
| `ctx.close(symbol)` | Closes the position without flipping it. |
| `ctx.flatten()` | Closes every position (best effort). |

`target_weight` is per-position, not per-portfolio. Three symbols at 0.2 each
means up to 60% gross, subject to `[risk]` caps.

Orders are asynchronous: the return value is the accepted `Order` (or `None` if
risk rejected it), not a fill. Use `on_fill` for post-trade state.

---

## 4. A complete example: Donchian breakout

```python
from __future__ import annotations

from krabs.events import Bar
from krabs.strategy import Strategy, register


@register
class DonchianBreakout(Strategy):
    id = "donchian"
    description = "Buy a new N-bar high, exit on a new N/2-bar low."
    defaults = {"entry": 20, "exit": 10, "allocation": 0.2}

    def __init__(self, **params):
        super().__init__(**params)
        entry, exit_n = int(self.p("entry")), int(self.p("exit"))
        if entry <= exit_n or exit_n < 2:
            raise ValueError("require entry > exit >= 2")
        if not 0 < float(self.p("allocation")) <= 1:
            raise ValueError("allocation must be in (0, 1]")

    def on_bar(self, ctx, bar: Bar) -> None:
        entry, exit_n = int(self.p("entry")), int(self.p("exit"))
        bars = ctx.bars(bar.symbol, entry + 1)
        if len(bars) < entry + 1:
            return

        prior = bars[:-1]          # never include the bar you are deciding on
        position = ctx.position(bar.symbol)

        if position.is_flat:
            high = max(b.high for b in prior[-entry:])
            if bar.close > high:
                ctx.target_weight(
                    bar.symbol,
                    float(self.p("allocation")),
                    reason=f"breakout above {high:.2f}",
                )
        else:
            low = min(b.low for b in prior[-exit_n:])
            if bar.close < low:
                ctx.target_weight(bar.symbol, 0.0, reason=f"breakdown below {low:.2f}")
```

Note the `prior = bars[:-1]` line. The current bar is already included in
`ctx.bars`; using its high in the breakout level would be look-ahead.

---

## 5. Rules that keep you honest

1. **Never index the future.** `ctx.bars` ends at the current bar. Slice it
   (`bars[:-1]`) when computing a level the current bar must exceed.
2. **No external state that assumes one pass.** Backtests and restarts may
   deliver the same bar twice; `target_weight` deltas and regime flags must be
   idempotent.
3. **Size through `target_weight`.** It automatically respects buying power,
   position caps and notional caps, and it produces readable rejections.
4. **Expect rejections.** Check `ctx.position` instead of assuming the previous
   order filled. A rejected order is logged with a reason.
5. **Validate parameters in `__init__`.** A live process must fail at startup,
   not at 10:03 with a division by zero.
6. **Respect the clock.** Mean-reversion at 09:30 and momentum at 15:59 are
   different strategies. Use the bar timestamps if you need session logic.
7. **No wall clock, no network, no file writes in `on_bar`.** Use the context.

---

## 6. Testing a strategy offline

Use the synthetic feed and a fixed date range so results never depend on today:

```python
from krabs.backtest import BacktestEngine
from krabs.config import Config
from krabs.feeds.synthetic import SyntheticFeed
from krabs.strategy.examples.donchian import DonchianBreakout

def test_donchian_runs():
    cfg = Config.from_dict({
        "market": {"symbols": ["SPY"], "timeframe": "5Min"},
        "wallet": {"starting_cash": 10_000},
        "execution": {"slippage_bps": 2, "half_spread_bps": 2},
        "risk": {"max_order_notional": 5_000, "max_position_pct": 0.5},
        "feed": {"provider": "synthetic"},
        "storage": {"enabled": False},
    })
    result = BacktestEngine(
        cfg, DonchianBreakout(), SyntheticFeed(days=10, seed=7), run_id="test"
    ).run()
    assert len(result.fills) > 0
    assert result.metrics["max_drawdown"] <= 0
```

More useful tests assert **behaviour**, not PnL:

- given a crafted bar sequence, the strategy enters on the breakout bar and not
  before;
- it does not re-enter while already long;
- it exits on the breakdown;
- a risk rejection leaves the position unchanged.

The engine's `ListFeed` pattern in `tests/test_backtest.py` is the template.

---

## 7. Historical data for testing

The repo ships a regenerable daily-bar sample under `data/samples/`, split by
GICS-style sector (technology, financials, health care, energy, ...), plus SPY:

```bash
python3 scripts/fetch_sample_data.py
./krabs backtest -c configs/sample-daily.toml --csv data/samples/energy.csv
```

`--csv` auto-detects symbols; use `--symbols` and `--timeframe` to narrow or
override. The sample is current large caps only, so it has survivorship bias.
For research-grade data use Alpaca's SIP feed, CRSP/Compustat, Sharadar,
Norgate or Databento; see `data/samples/README.md` for the trade-offs.

### 7.1 How long does a backtest take?

Two different questions: how much **market history** you need, and how much
**wall-clock time** the machine needs.

Use at least 5–10 years of daily data so the sample contains a bear market and a
recovery; 2–3 years for 5-minute bars; 6–12 months for 1-minute bars. Aim for
100+ round trips before trusting a win rate. Always hold out an out-of-sample
period and test with doubled costs.

Wall-clock time scales with `symbols × sessions × bars_per_session × years`:

| Universe | Bars | This x86 machine | Raspberry Pi (est.) |
|---|---:|---:|---:|
| 3 symbols, daily, 7 years | 5k | <1 s | 2–5 s |
| 34 symbols, daily, 7 years | 56k | ~4 s | 20–40 s |
| 10 symbols, 5-min, 3 years | 590k | ~35 s | 2–4 min |
| 10 symbols, 1-min, 3 years | 2.9M | ~3 min | 10–20 min |
| 33 symbols, 1-min, 7 years | 22.7M | ~20 min | 1.5–3 h |

Measured throughput for the built-in SMA strategy is ~15–20k bars/s on a modern
x86 core; the Pi is roughly 3–5× slower. A complex strategy, per-bar history
scans, storage writes, or many fills can change this by an order of magnitude.
Storage: 1-minute bars are roughly 60–80 bytes per row in CSV, so 10 symbols ×
3 years is ~200 MB; 33 symbols × 7 years is ~1.5–2 GB.

A **full research cycle** is more than one pass:

- data download/cleaning: minutes to hours, depending on vendor and rate limits;
- parameter sweep: single-backtest time × number of combinations;
- walk-forward: that total × number of folds;
- paper trading: at least 2–4 weeks;
- broker-paper: another 1–2 weeks.

Daily data is usually the right place to start: seconds per run, enough history
for regime analysis, and it exposes overfitting faster than an expensive
minute-bar sweep.

## 8. Parameter optimization

For anything beyond a tiny grid, use the optimizer rather than a shell loop:

```bash
./krabs optimize -c configs/sample-daily.toml --strategy sma_cross \
    --space fast=5:30 --space slow=40:120 --space allocation=0.1:0.3 \
    --method de --trials 40 --folds 4 --holdout 0.2 --objective sharpe
```

It splits the data chronologically, optimizes on the first 80%, scores candidates
across contiguous folds, and evaluates the winner once on the final 20%. Read
`docs/optimization.md` before trusting any result; the holdout is the point.

## 9. Parameter sweeps

The CLI is intentionally scriptable:

```bash
for fast in 5 10 20; do
  for slow in 30 60 90; do
    [ "$fast" -ge "$slow" ] && continue
    ./krabs backtest -c configs/example.toml --strategy sma_cross \
      --param fast=$fast --param slow=$slow --param allocation=0.2 \
      --quiet --report-dir reports/sweep >/dev/null
  done
done

python3 - <<'PY'
import json, pathlib
rows = []
for f in pathlib.Path("reports/sweep").glob("*/result.json"):
    d = json.loads(f.read_text())
    rows.append((d["strategy"], d["params"], d["metrics"]["sharpe"], d["metrics"]["total_return"]))
for row in sorted(rows, key=lambda r: (r[2] or -9), reverse=True)[:10]:
    print(row)
PY
```

Rules for a sweep you can trust:

- same data, same costs, same risk limits across runs;
- report Sharpe, max drawdown and turnover, not just return;
- prefer a **plateau** (neighbouring parameters behave similarly) over a spike;
- re-test the chosen plateau on data you did not use to choose it;
- include buy-and-hold as the benchmark.

---

## 10. Promotion checklist

Before `broker-paper`:

- [ ] Deterministic backtest over ≥ 6 months including a drawdown.
- [ ] Beats buy-and-hold on risk-adjusted return after slippage/commission.
- [ ] Max drawdown within your tolerance; turnover is affordable.
- [ ] No strategy code paths depend on wall-clock time or external state.
- [ ] Unit tests cover entry, exit, no-reentry and rejection behaviour.

Before `live`:

- [ ] ≥ 2 weeks in `paper` with no unhandled errors or unexplained halts.
- [ ] ≥ 1 week in `broker-paper`, clean reconciliation, no unexplained rejects.
- [ ] Live limits are ≤ 10% of equity per order, with a daily loss stop.
- [ ] Kill switch and flatten tested on the paper account.
- [ ] Heartbeat watchdog and an external alert are working.
- [ ] You can explain exactly when the strategy buys, sells and gives up.

---

## 11. Common failure modes

| Symptom | Likely cause |
|---|---|
| Hundreds of tiny trades | `target_weight` called every bar with a drifting price; only act on state changes. |
| Backtest amazing, paper flat | Look-ahead (using the current bar's high/low) or no costs in the backtest. |
| Orders rejected: risk-adjusted to zero | `max_position_pct` smaller than the requested weight, or notional below $1. |
| `duplicate intent` | Same symbol/side/size submitted twice inside `duplicate_window_s`; add regime state. |
| Position never fills | Limit too far away, volume cap too small, or market closed. |
| Strategy shorted by accident | `target_weight(0.0)` vs negative weights, or a sell larger than the position with `allow_short=true`. |
| Results change daily | Synthetic feed uses "today" as the end date; pass an explicit seed/end for reproducibility. |
| Works on 1-min, fails on 5-min | Indicators assume a bar count, not time; re-tune periods per timeframe. |

---

## 12. Machine-learning strategies

Krabs is a runtime and backtest harness, not a training framework. Train
offline, freeze an artifact, and load it read-only in the strategy:

```python
def on_start(self, ctx):
    import joblib
    self.model = joblib.load("/opt/krabs/models/momentum-v3.joblib")
    self.features = ["ret_1", "ret_5", "rsi_14", "atr_14"]

def on_bar(self, ctx, bar):
    x = [[...]]                      # built only from ctx.history(...)
    score = float(self.model.predict_proba(x)[0, 1])
    ctx.target_weight(bar.symbol, 0.2 if score > 0.55 else 0.0)
```

Rules that prevent the classic disasters:

- **Train/test split by time, never randomly.** Use walk-forward validation;
  the test window must come strictly after the training window.
- **Feature parity.** Training features and live features must call the same
  functions on the same bar schema. A single off-by-one in an indicator
  invalidates every result.
- **No training in `on_bar`.** Inference must be milliseconds and deterministic.
- **Version everything.** Log the model hash, feature list and training window
  in `on_start`; keep the artifact next to the config, not in `/tmp`.
- **Pin the environment.** NumPy/scikit-learn/ONNX versions on the Pi must match
  training; re-validate after every upgrade.
- **Costs and slippage still apply.** A model with a 51% hit rate is usually
  unprofitable after spread; test with `slippage_bps` ≥ 2.
- **Optional dependencies stay optional.** Do not import ML libraries in core
  modules; import them inside `on_start` so the platform still runs if the model
  extra is missing.
- On a Pi, prefer small linear models or gradient boosting over deep learning;
  if you need a neural net, export to ONNX and benchmark inference on the Pi
  before trusting it in the loop. `krabs_core` does not accelerate model
  inference.

---

## 13. Where to look next

- `src/krabs/strategy/examples/` — three reference implementations.
- `src/krabs/strategy/base.py` — the full context surface.
- `ARCHITECTURE.md` §8 — execution semantics and why fills happen next bar.
- `tests/test_backtest.py` — how to drive the engine deterministically.
