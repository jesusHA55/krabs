# Optimizing strategy parameters

A backtest is a function from parameters to a performance metric. Optimizing a
strategy means searching for parameters that score well **without fooling
yourself**. This document covers the built-in search, the statistics that keep
it honest, and where RNNs / reinforcement learning actually fit.

---

## 1. First, separate three different problems

| Problem | Tool | What “parameters” means |
|---|---|---|
| **Hyperparameter search** | grid, random, Bayesian optimization, CMA-ES/DE | `fast`, `slow`, `allocation`, thresholds |
| **Forecasting** | RNN/LSTM/Transformer, gradient boosting | model weights trained on features/labels |
| **Policy learning** | reinforcement learning (PPO, SAC, DQN) | weights of a policy that maps state → action |

`krabs optimize` solves the first one. An RNN is not a hyperparameter optimizer;
it is a sequence model. Using an RNN to choose `fast=20, slow=60` is a category
error, and training one on the backtest window simply overfits faster.

The second and third are valid, but they are **strategies**, not search methods.
Section 7 explains how they plug in.

---

## 2. The built-in optimizer

```bash
./krabs optimize -c configs/sample-daily.toml \
    --strategy sma_cross \
    --space fast=5:30 \
    --space slow=40:120 \
    --space allocation=0.1:0.3 \
    --method de --trials 40 --folds 4 --holdout 0.2 \
    --objective sharpe --seed 7
```

What it does:

1. Loads the bars once (`InMemoryFeed`) and keeps them immutable.
2. Splits the data chronologically:
   - **optimization period** = first `1 - holdout` (default 80%);
   - **holdout** = final 20%, touched once after selection.
3. Reserves a **warm-up prefix** of `[feed].warmup_bars` bars (or
   `--warmup-bars`) before the scored range. Every fold and the holdout start
   with that history preloaded into `ctx.bars`, so screening and model warm-up
   are ready at the fold boundary. The prefix is not traded, scored, or included
   in the equity curve. Without it, a cold start silently loses the first
   formation window and understates trades. The optimization range is shifted
   forward past the prefix so fold 1 is warm too; the holdout stays at the
   original 80/20 boundary so runs remain comparable.
4. Splits the optimization period into `--folds` contiguous windows. Each
   candidate is backtested independently in every fold, always starting with a
   fresh wallet and strategy instance.
5. Scores each candidate:

   ```
   score = mean(metric)
           - penalty        × stdev(metric)
           - dd_penalty     × |mean(max_drawdown)|
           - turnover_penalty × log1p(mean(turnover))
   ```

   Candidates with fewer than `--min-trades` round trips are rejected.
6. Searches with one of:
   - `random` — strong, simple baseline;
   - `grid` — exhaustive; useful only for tiny spaces;
   - `de` — differential evolution, a derivative-free evolutionary method that
     works on integer, float and categorical spaces.
7. Evaluates the winner **once** on the holdout and writes everything to
   `reports/optimize-<run_id>/` (`result.json`, `trials.csv`, `summary.txt`).

### Reading the output

A real run on the technology sample:

```
best params: {'fast': 21, 'slow': 96, 'allocation': 0.232}
in-sample  : mean Sharpe 1.16, std 0.14, 15 trades
folds      : 1.35 / 1.10 / 1.02 Sharpe
holdout    : Sharpe -0.28, return -4.2%, maxDD -14.3%
```

The in-sample score looks excellent and the holdout says no. **Believe the
holdout.** This is the optimizer doing its job: it found a parameter set that
fits 2020–2025 and did not generalize to 2025–2026. Reject it.

The report records every trial, including invalid ones (`fast >= slow`), so you
can see the search rather than just the winner.

---

## 3. Choosing the objective

`--objective` accepts:

| Objective | Use when |
|---|---|
| `sharpe` | Default; comparable across strategies, penalises volatility |
| `sortino` | You care about downside volatility only |
| `total_return` | Early exploration; easy to distort with one lucky trade |
| `calmar` | Return per unit of max drawdown |
| `profit_factor` | Trade-level quality; sensitive to tiny samples |

The penalties are not decoration:

- `--penalty` (default `0.5`) punishes inconsistency across folds. A strategy
  that makes all its money in one regime gets a lower score.
- `--dd-penalty` (default `0.5`) makes deep drawdowns expensive.
- `--turnover-penalty` (default `0.01`) discourages high-turnover solutions that
  look good before realistic costs.
- `--min-trades` (default `1`) rejects the degenerate “never trade” solution.
  For daily strategies, use 20–50; for intraday, 100+.

Costs matter more than the optimizer. Set `execution.slippage_bps` and
`half_spread_bps` to realistic values *before* searching. Otherwise you are
optimizing the frictionless fantasy.

---

## 4. Why a holdout is mandatory

If you evaluate 40 candidates and keep the best, the winner’s in-sample score is
biased upward. The holdout is the only number that has not been selected on.

Rules that prevent self-deception:

1. **Touch the holdout once.** If you change the objective and look again, it
   becomes part of the search.
2. **Keep the trial count.** Record `--trials` and the full `trials.csv`. Fifty
   trials of a 3-parameter space already produce selection bias.
3. **Prefer plateaus.** Re-run the top 5–10 parameter sets with ±10–20%
   perturbations. If performance collapses between neighbours, you found noise.
4. **Walk forward.** A single holdout is one sample. For stronger evidence,
   optimize on rolling windows and evaluate on the next window, then aggregate
   the out-of-sample equity curve.
5. **Adjust for multiple testing.** For serious claims, compute a deflated
   Sharpe ratio or run a White’s Reality Check / probability of backtest
   overfitting (PBO). A Sharpe of 1.5 found after 500 trials is not a Sharpe of
   1.5.
6. **Re-test on a different universe or period.** A parameter set should survive
   a reasonable change of symbols, costs and dates.

The built-in optimizer uses contiguous folds but does **not** re-optimize inside
each fold. That is cross-validation for selection, not full walk-forward
optimization. Treat the holdout as the minimum standard, not the gold standard.

---

## 5. Search methods and budgets

| Method | Best for | Cost |
|---|---|---|
| `grid` | ≤ 2–3 parameters, small ranges | combinations = product of grid sizes |
| `random` | 3–8 parameters, quick baseline | `--trials` backtests × folds |
| `de` | 3–10 numeric/integer parameters | `--trials` backtests × folds; population-based |
| Bayesian (Optuna TPE) | expensive evaluations, 5–20 parameters | model overhead, fewer trials |
| CMA-ES | continuous parameters, rugged landscapes | population, more complex |

For 3–6 parameters, random search and DE are usually enough. Bayesian
optimization wins when a single backtest takes minutes and you can only afford
50–100 evaluations. `krabs optimize` is dependency-free on purpose; an Optuna
adapter is a natural extension of `StrategyOptimizer._evaluate`.

Budget arithmetic:

```
wall_clock ≈ trials × folds × single_backtest_time
```

On the sample data, a daily 3-symbol backtest is ~0.3 s, so 40 trials × 4 folds
is ~1 minute. A 33-symbol 1-minute backtest at 20 minutes each makes a 40-trial
search a 13-hour job before folds; reduce the universe/period or run it on a
beefier machine.

---

## 6. What “improved” means

The optimizer maximizes a proxy, not future profit. A parameter set is a
candidate only if:

- it survives the holdout;
- neighbouring parameters behave similarly;
- it survives doubled costs;
- it survives a different but related universe;
- the number of trials is disclosed;
- the mechanism is explainable (“faster trend filter”, not “lucky number”).

If the best parameters sit on a cliff edge, prefer a slightly worse but stable
neighbour. Stability is a feature.

---

## 7. Where RNNs and reinforcement learning fit

### 7.1 RNN as a forecasting model

Train offline, freeze the artifact, and use its prediction as one input to a
normal strategy:

```python
def on_start(self, ctx):
    import joblib
    self.model = joblib.load("/opt/krabs/models/return-v3.joblib")

def on_bar(self, ctx, bar):
    features = build_features(ctx.history(bar.symbol, 128))  # same code as training
    expected = float(self.model.predict([features])[0])
    ctx.target_weight(bar.symbol, 0.2 if expected > 0.001 else 0.0)
```

Requirements:

- **Walk-forward training.** Train on `[t0, t1)`, predict `[t1, t2)`, roll.
  Never shuffle time.
- **Feature parity.** The exact same feature code and bar schema at train and
  inference time; one off-by-one invalidates everything.
- **No training in `on_bar`.** Inference must be milliseconds and deterministic.
- **Version the model.** Log hash, training window, feature list.
- **Pin dependencies.** A model trained with one NumPy/PyTorch version may drift
  after an upgrade.
- **Measure on the Pi.** ONNX Runtime or a small linear/GBM model is usually the
  right deployment target; deep models may be too slow.

An RNN predicts; it does not search parameters. You can still wrap it in
`krabs optimize` to tune its decision threshold, position size or ensemble
weight.

### 7.2 Reinforcement learning as a policy

RL learns the action directly. The mapping is:

| RL concept | Krabs equivalent |
|---|---|
| Environment | `BacktestEngine` + market data |
| State | recent bars, indicators, position, cash, time features |
| Action | target weight / side / size (continuous or discrete) |
| Reward | net PnL − drawdown/turnover penalties − transaction costs |
| Episode | one walk-forward window |

You would wrap `BacktestEngine` in a Gym-like environment, train PPO/SAC with
Stable-Baselines3 or Ray RLlib (GPU, not the Pi), then export a frozen policy
and load it in a `Strategy`. Critical pitfalls:

- Reward hacking (the agent learns to exploit the simulator, not the market);
- look-ahead in features or episode boundaries;
- non-stationarity and sim-to-real gap;
- enormous sample requirements and weak interpretability;
- overfitting through repeated evaluation on the same test period.

RL is a research project, not a shortcut to a better SMA. For 3–6 numeric
parameters, the optimizer in this repo will beat an RNN nine times out of ten.

---

## 8. Extending the optimizer

- **Optuna/TPE**: implement a thin wrapper that samples from `ParamSpace` and
  calls `StrategyOptimizer._evaluate`; keep the holdout logic.
- **Nested walk-forward**: loop over rolling windows, optimize on each train
  window, apply the winner to the next test window, and stitch the out-of-sample
  results.
- **Constraints**: add a custom objective that rejects parameter sets by
  turnover, drawdown, exposure or regime dependence.
- **Multi-objective**: return a Pareto front (return vs drawdown vs turnover)
  instead of a scalar; NSGA-II is the usual choice.
- **More data**: nothing here depends on synthetic data; point `--csv` at your
  survivorship-bias-free dataset.

---

## 9. Limitations

- The search is only as good as the backtest’s fill model and data.
- Three large caps per sector are not a robust universe.
- Contiguous folds ignore regime labels; a fold can be all bull or all bear.
- The objective is scalar; multi-objective trade-offs need a Pareto method.
- The holdout can be unlucky. One holdout is evidence, not proof.
- Warm starts give the strategy price history, not positions. A fold always
  begins flat, so carry-in risk and hedge drift are not modelled. Strategies
  that keep their own rolling buffers in `on_bar` (rather than reading
  `ctx.bars`) still start cold.
- A holdout evaluated as a fresh warm start can differ from the same date in
  one continuous run, because the continuous run carries open signals and
  filter state across the boundary. Report both when the strategy is
  stateful.
- `warmup_bars` must cover the slowest warm-up in the search space. If a
  parameter samples a longer window than the prefix, that trial still starts
  partially cold.
- Optimizing parameters cannot fix a strategy with no edge. It can only make
  the overfit more convincing.
