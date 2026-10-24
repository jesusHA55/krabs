# Pearl

**Pearl is Krabs’ causal, composable statistical modelling layer.** It turns
bars into filtered states and signals using Kalman filters, rolling regressions,
pairs statistics, mean-reversion estimators, volatility models and change-point
detectors. It never places orders; strategies consume Pearl snapshots and Krabs
keeps execution, risk and wallets.

The first release focuses on a strong foundation and **pairs trading**. Nonlinear
filters (EKF/UKF/particle) are deliberately deferred.

---

## 1. Why a model layer?

Indicators answer “what is the value of this formula?” Pearl answers:

- what is the **state** of the process (level, slope, hedge ratio, volatility)?
- how uncertain is that state (covariance, innovation, likelihood)?
- when is the model **ready**?
- does the state imply a **position** (signal with hysteresis)?
- can the state survive a restart or be audited later?

A model is a named DAG:

```
Series(left) ─┐
              ├─► PairSpread (β, spread, z, half-life) ─► Threshold (side)
Series(right) ┘
```

Nodes only reference nodes defined before them. Look-ahead is impossible by
construction: there is no API for a node to read a future output.

---

## 2. Quick start

```python
from krabs.pearl import ModelBuilder, PairSpread, Series, Threshold

model = (
    ModelBuilder()
    .add("left", Series("AAPL"))
    .add("right", Series("MSFT"))
    .add("spread", PairSpread("left", "right", window=60, min_obs=30))
    .add("entry", Threshold("spread.z", entry=2.0, exit=0.5))
    .build()
)

snap = model.update(observation)
snap.ready
snap.value("spread.beta")
snap.value("spread.z")
snap.signal("entry")      # +1 long spread, -1 short spread, 0 flat
```

For a ready-made model:

```python
from krabs.pearl import pairs_model
model = pairs_model("AAPL", "MSFT", window=60, entry=2.0, exit=0.5)
```

### Pairs semantics

The complete sector-ETF strategy built on this layer is documented in
[docs/etf-pairs.md](etf-pairs.md).

Pairs can use rolling OLS or Johansen:

```python
model = pairs_model("AAPL", "MSFT", method="johansen", window=120)
```

`PairSpread` regresses the left leg on the right leg over a rolling window:

```
left = alpha + beta * right + residual
spread = residual
z = (spread - rolling_mean) / rolling_std
```

`side = +1` means **long the spread**: long the left leg and short `beta` of the
right leg. `side = -1` is the reverse. If `use_log=True` (default), prices are
log-transformed first, so `beta` is an elasticity in log space.

`Johansen` estimates the cointegrating vector from the trace test, reports the
rank, and normalizes the first weight. For a pair it produces the same
`beta`/`spread`/`z` contract, so a strategy can switch methods without changing
its sizing code. The current implementation uses one lag, centered levels and
approximate 95% critical values; it is a feature, not a substitute for a
full econometric report.

---

## 3. Node catalog

| Node | Outputs | Purpose |
|---|---|---|
| `Series(symbol)` | close/open/high/low/volume | Read an observation field |
| `LogSeries(symbol)` | value, OHLCV | Log price, for level-space models |
| `LogReturn(source, period)` | value | Causal log return |
| `SimpleReturn`, `Difference`, `Lag` | value | Basic transforms |
| `ZScore(source, window)` | value, mean, std | Rolling standardization |
| `EWMA`, `EWMVariance` | value, variance | Exponential smoothing |
| `Spread`, `Ratio`, `Combine` | value | Static spreads and arithmetic |
| `Kalman(source, model=...)` | level/slope/prediction, innovation, state_var | Local level, local trend, AR(1), time-varying regression |
| `OU(source, window, refit)` | value, z, obs_z, innovation, phi, half_life, mu, sd | AR(1) mean-reversion filter with causal parameter refits |
| `PairSpread(left, right, window)` | beta, alpha, spread, z, half_life, ADF t, R² | Pairs regression (OLS) |
| `Johansen(sources, window)` | cointegrating weights, beta, spread, z, rank, trace stats | Johansen cointegration test |
| `Hurst(source, window)` | value | Rescaled-range Hurst on **returns** |
| `CUSUM(source, threshold)` | alarm, cusum_pos/neg | Change detection |
| `HPFilter(source, window)` | trend, cycle | Rolling Hodrick–Prescott |
| `HaarTrend(source, level)` | approximation, detail energy | Rolling Haar smoother/roughness |
| `GARCH11(source, omega, alpha, beta)` | variance, volatility, z | Conditional volatility |
| `RollingPCA(sources, window)` | projected value, explained variance, loadings | First principal component of a basket |
| `Threshold(source, entry, exit)` | side, z | Hysteresis signal |
| `ZeroCross(source)` | side | Trend/slope sign |

All nodes expose `params()`, `state()` and `load_state()`. A model’s state can be
saved and reloaded:

```python
model.save_state("var/pearl/AAPL-MSFT.json")
# later, after rebuilding the same model
model.load_state_file("var/pearl/AAPL-MSFT.json")
```

The state contains filter covariances, buffers and signal sides. It does not
contain parameters: those remain in code/config so a restart cannot silently
change the model.

---

## 4. Multi-asset observations

Pairs and baskets use one observation per timestamp:

```python
from krabs.pearl import Observation

obs = Observation.from_bars([bar_left, bar_right])
```

`Observation.from_bars` requires identical timestamps. `dt` is explicit (1.0 for
one daily bar) so filters can distinguish daily, intraday and gapped sessions.

---

## 5. Strategy integration

`PearlStrategy` handles the per-timestamp lifecycle:

```python
from krabs.pearl import PearlStrategy

class MyPairs(PearlStrategy):
    symbols = ["AAPL", "MSFT"]

    def build_model(self):
        return pairs_model("AAPL", "MSFT")

    def on_snapshot(self, ctx, snap):
        side = snap.signal("signal")
        beta = snap.value("spread.beta", 1.0)
        # size both legs, submit via ctx.target_weight(...)
```

- The model is updated **once per timestamp**, on the primary symbol’s bar.
- `on_snapshot` is called only when every node is warmed up.
- State can be loaded on start and saved on end:
  `state_path = "var/pearl/<name>.json"`, `persist_every = 0`.
- It never touches a broker; it only emits target weights.

A complete example is registered as `pearl_pairs`:

```bash
python3 scripts/fetch_sample_data.py          # generates the daily sample
./krabs backtest -c configs/pearl-pairs.toml
./krabs backtest -c configs/pearl-johansen.toml
# or override the model method on the OLS config:
./krabs backtest -c configs/pearl-pairs.toml --param model.method=johansen
```

Model parameters are nested under `model`, so they can be addressed with dotted
paths by the optimizer:

```bash
./krabs optimize -c configs/pearl-pairs.toml --strategy pearl_pairs \
    --space model.window=30:120 --space model.entry=1.5:2.5 \
    --space model.method=ols,johansen --method de --trials 40 --folds 4
```

`configs/pearl-pairs.toml` trades AAPL/MSFT with a rolling hedge ratio,
hysteresis and short selling.

---

## 6. Causality and state rules

1. `update(observation)` only sees current and past observations.
2. `transform(observations)` is batch convenience over the same update path.
3. Every model reports `snapshot.ready`; strategies must not trade before it.
4. `PairSpread` uses a rolling window ending at the current bar. It does not see
   the future, although the hedge ratio is an in-sample estimate at each step.
5. `HPFilter` is two-sided **within its rolling window**; it never sees future
   bars, but its recent trend can revise as the window rolls. Treat it as a
   smoother, not a predictive signal.
6. `Hurst` expects a return-like series. Applying it to prices measures the
   persistence of the integrated level and will usually be near 1.
7. `adf_t` is an approximate Engle–Granger statistic. Use it as a feature, not
   as a substitute for a full ADF implementation with MacKinnon p-values.
8. State is per model instance. Never share a model across symbols unless the
   model is intentionally multivariate.

---

## 7. Adaptive vs fixed

Kalman nodes support:

```python
Kalman("ret", model="local_level", adaptation="fixed")
Kalman("ret", model="local_level", adaptation="covariance", forgetting=0.98)
```

- `fixed` is the default and is recommended for research and optimization.
- `covariance` performs bounded innovation covariance matching for the
  observation noise (`R`). It remains deterministic for a fixed bar sequence.
- The adaptation is visible in `obs_var` and the serialized state.

GARCH and pair windows are adaptive in the sense that their estimates roll, but
their explicit parameters stay fixed per model unless rebuilt. Online MLE is a
future extension; explicit parameters keep backtests reproducible.

---

## 8. Optional dependencies

The implemented core is **pure Python**. No NumPy, SciPy, pandas or PyWavelets is
required, which keeps the Raspberry Pi install simple.

Planned optional modules:

- Johansen cointegration: NumPy/SciPy acceleration.
- Full wavelet families and band-pass filters: PyWavelets/SciPy (Haar is in
  the core).
- GARCH calibration: pure-Python Nelder–Mead first, SciPy later.

These will be lazy imports behind extras, so a missing package produces a clear
error instead of breaking the core.

---

## 9. Testing and validation

`tests/test_pearl.py` covers:

- Observation timestamps and multi-asset construction.
- Builder validation and duplicate names.
- Log returns, z-scores and batch/streaming parity.
- No-lookahead: future bars cannot change earlier snapshots.
- Kalman local level/trend recovery and state round trips.
- Pairs hedge-ratio recovery, z-scores and state round trips.
- Johansen rank/beta recovery on a simulated cointegrated pair.
- Rolling PCA explained variance and Jacobi eigen-decomposition.
- Hurst, HP filter, Haar, CUSUM and GARCH sanity.
- End-to-end `pearl_pairs` backtest with both legs filled.

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_pearl -v
```

---

## 10. Roadmap

1. **State persistence in the runtime:** load/save through a state store rather
   than only `PearlStrategy.state_path`.
2. **Advanced estimators:** Johansen with multiple lags/deterministic terms,
   band-pass/full wavelets, GARCH calibration, Bayesian online change-point.
3. **Nonlinear filters:** EKF, UKF, particle filter — deferred by design.
4. **Rust hot path:** only if profiling shows the pure-Python matrices matter at
   the chosen bar cadence.

---

## 11. Limitations

- No model serialization of the graph itself yet; rebuild the model in code and
  load the state. Presets reduce duplication.
- Pairs need synchronized bars; missing one leg skips that timestamp.
- The example pairs strategy rebalances when the hedge ratio drifts beyond a
  tolerance; tuning that tolerance materially changes turnover.
- Rolling regressions are in-sample at each step; they are not a claim of
  predictive accuracy.
- Johansen uses one lag and centered levels; ranks and critical values are
  approximate and should not be read as a full econometric conclusion.
- Short selling must be enabled in the wallet and broker configuration.
- The broad statistical suite is intentionally staged; see the roadmap.
