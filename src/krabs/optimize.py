"""Strategy parameter search with an explicit objective and a holdout.

This is **black-box optimization** of a handful of strategy parameters. It is
deliberately not an RNN: sequence models are for learning from time series, not
for searching a 2–6 dimensional parameter space. See docs/optimization.md for
when to use reinforcement learning or a learned policy instead.

Design choices:

* The optimization period is the first ``1 - holdout`` of the data; the final
  ``holdout`` slice is evaluated once, after selection, and never optimized on.
* Within the optimization period, contiguous folds are scored independently.
  The objective is the mean metric minus a dispersion penalty, a drawdown
  penalty and a turnover penalty.
* Methods: grid, random search and differential evolution (stdlib only).
* Every trial and its fold scores are written to ``reports/optimize-*``.

No gradients, no external dependencies, and deterministic for a given seed.
"""

from __future__ import annotations

import itertools
import logging
import math
import random
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from .backtest import BacktestEngine
from .config import Config
from .events import Bar
from .feeds.base import InMemoryFeed
from .strategy import build
from .strategy.params import expand_paths, flatten
from .strategy.registry import get as get_strategy

OBJECTIVES = ("sharpe", "sortino", "total_return", "calmar", "profit_factor")
METHODS = ("random", "grid", "de")


# ---------------------------------------------------------------------------
# Parameter spaces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParamSpace:
    name: str
    kind: str  # "int" | "float" | "categorical"
    low: float = 0.0
    high: float = 0.0
    step: float = 0.0
    values: tuple[Any, ...] = ()

    def sample(self, rng: random.Random) -> Any:
        if self.kind == "categorical":
            return rng.choice(self.values)
        return self._quantize(self.low + rng.random() * (self.high - self.low))

    def grid(self) -> list[Any]:
        if self.kind == "categorical":
            return list(self.values)
        if self.kind == "int":
            step = max(1, int(self.step or 1))
            return list(range(int(self.low), int(self.high) + 1, step))
        if self.step > 0:
            count = int(round((self.high - self.low) / self.step))
            return [round(self.low + i * self.step, 10) for i in range(count + 1)]
        return [round(self.low + (self.high - self.low) * i / 24, 10) for i in range(25)]

    def encode(self, value: Any) -> float:
        if self.kind == "categorical":
            if len(self.values) <= 1:
                return 0.5
            return self.values.index(value) / (len(self.values) - 1)
        if self.high == self.low:
            return 0.5
        return min(1.0, max(0.0, (float(value) - self.low) / (self.high - self.low)))

    def decode(self, x: float) -> Any:
        x = min(1.0, max(0.0, float(x)))
        if self.kind == "categorical":
            if len(self.values) == 1:
                return self.values[0]
            return self.values[round(x * (len(self.values) - 1))]
        return self._quantize(self.low + x * (self.high - self.low))

    def _quantize(self, value: float) -> Any:
        if self.kind == "int":
            step = max(1, int(self.step or 1))
            base = int(self.low)
            value = base + round((value - base) / step) * step
            return int(min(int(self.high), max(int(self.low), value)))
        if self.step > 0:
            value = self.low + round((value - self.low) / self.step) * self.step
        value = min(self.high, max(self.low, value))
        return round(value, 10)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "low": self.low,
            "high": self.high,
            "step": self.step,
            "values": list(self.values),
        }


def parse_space(name: str, spec: str, default: Any) -> ParamSpace:
    """Parse ``fast=5:40[:5]`` or ``mode=a,b,c`` into a typed space."""
    if ":" in spec:
        if isinstance(default, bool):
            raise ValueError(f"{name}: boolean parameters must be categorical (true,false)")
        parts = spec.split(":")
        if len(parts) not in (2, 3):
            raise ValueError(f"{name}: expected low:high[:step], got {spec!r}")
        low, high = float(parts[0]), float(parts[1])
        if high <= low:
            raise ValueError(f"{name}: high must be greater than low")
        step = float(parts[2]) if len(parts) == 3 else 0.0
        if step < 0:
            raise ValueError(f"{name}: step must be non-negative")
        if isinstance(default, int):
            low_i, high_i = math.ceil(low), math.floor(high)
            if high_i < low_i:
                raise ValueError(f"{name}: no integer values in {spec!r}")
            return ParamSpace(
                name=name,
                kind="int",
                low=low_i,
                high=high_i,
                step=max(1.0, float(step or 1)),
            )
        return ParamSpace(name=name, kind="float", low=low, high=high, step=step)

    values = tuple(_cast(v.strip(), default) for v in spec.split(",") if v.strip())
    if not values:
        raise ValueError(f"{name}: empty categorical space")
    return ParamSpace(name=name, kind="categorical", values=values)


def _cast(raw: str, default: Any) -> Any:
    if isinstance(default, bool):
        if raw.lower() not in ("1", "0", "true", "false", "yes", "no", "on", "off"):
            raise ValueError(f"cannot parse boolean value {raw!r}")
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Evaluation:
    params: dict[str, Any]
    score: float
    folds: list[dict[str, Any]] = field(default_factory=list)
    trades: int = 0
    metric_mean: float | None = None
    metric_std: float | None = None
    error: str = ""

    @property
    def valid(self) -> bool:
        return self.score > -1e8

    def to_dict(self) -> dict[str, Any]:
        return {
            "params": self.params,
            "score": self.score,
            "folds": self.folds,
            "trades": self.trades,
            "metric_mean": self.metric_mean,
            "metric_std": self.metric_std,
            "error": self.error,
        }


@dataclass(slots=True)
class OptimizationResult:
    run_id: str
    strategy: str
    method: str
    objective: str
    warmup_bars: int
    spaces: list[ParamSpace]
    base_params: dict[str, Any]
    bounds: tuple[datetime, datetime]
    optimize_range: tuple[datetime, datetime]
    folds: list[tuple[datetime, datetime]]
    holdout_range: tuple[datetime, datetime] | None
    best: Evaluation | None
    evaluations: list[Evaluation]
    holdout: dict[str, Any] | None
    elapsed_s: float
    config: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "method": self.method,
            "objective": self.objective,
            "warmup_bars": self.warmup_bars,
            "spaces": [s.to_dict() for s in self.spaces],
            "base_params": self.base_params,
            "bounds": [_iso(self.bounds[0]), _iso(self.bounds[1])],
            "optimize_range": [_iso(self.optimize_range[0]), _iso(self.optimize_range[1])],
            "folds": [[_iso(a), _iso(b)] for a, b in self.folds],
            "holdout_range": (
                [_iso(self.holdout_range[0]), _iso(self.holdout_range[1])]
                if self.holdout_range
                else None
            ),
            "best": self.best.to_dict() if self.best else None,
            "evaluations": [e.to_dict() for e in self.evaluations],
            "holdout": self.holdout,
            "elapsed_s": self.elapsed_s,
            "config": self.config,
        }


# ---------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------


class StrategyOptimizer:
    def __init__(
        self,
        cfg: Config,
        strategy_name: str,
        feed: InMemoryFeed,
        spaces: Sequence[ParamSpace],
        *,
        base_params: dict[str, Any] | None = None,
        folds: int = 4,
        holdout: float = 0.2,
        objective: str = "sharpe",
        warmup_bars: int = 0,
        min_trades: int = 1,
        penalty: float = 0.5,
        dd_penalty: float = 0.5,
        turnover_penalty: float = 0.01,
        seed: int = 7,
        logger: logging.Logger | None = None,
    ) -> None:
        if not spaces:
            raise ValueError("at least one --space is required")
        if objective not in OBJECTIVES:
            raise ValueError(f"objective must be one of {OBJECTIVES}")
        if folds < 1:
            raise ValueError("folds must be >= 1")
        if not 0 <= holdout < 1:
            raise ValueError("holdout must be in [0, 1)")
        if warmup_bars < 0:
            raise ValueError("warmup_bars must be >= 0")
        bounds = feed.bounds()
        if bounds is None:
            raise ValueError("no bars available for optimization")
        known = flatten(get_strategy(strategy_name).defaults)
        for space in spaces:
            if space.name not in known:
                raise ValueError(
                    f"unknown parameter {space.name!r} for {strategy_name}; "
                    f"available: {', '.join(sorted(known))}"
                )
            if space.name in {"qty", "notional"}:
                raise ValueError(f"cannot optimize {space.name!r}")

        self.cfg = cfg
        self.strategy_name = strategy_name
        self.feed = feed
        self.spaces = list(spaces)
        self.base_params = dict(base_params or {})
        self.objective = objective
        self.warmup_bars = int(warmup_bars)
        self.min_trades = min_trades
        self.penalty = penalty
        self.dd_penalty = dd_penalty
        self.turnover_penalty = turnover_penalty
        self.seed = seed
        self.log = logger or logging.getLogger("krabs.optimize")
        self.run_id = f"opt-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        self._cache: dict[tuple, Evaluation] = {}
        self._counter = 0
        self._quiet = logging.getLogger(f"krabs.optimize.run.{id(self)}")
        self._quiet.handlers = [logging.NullHandler()]
        self._quiet.propagate = False
        self._quiet.setLevel(logging.CRITICAL)

        raw_start, end = bounds
        cut = (
            raw_start + (end - raw_start) * (1.0 - holdout) if holdout > 0 else None
        )
        scored_start = (
            max(
                raw_start,
                _after_warmup(feed, cfg.market.timeframe, raw_start, self.warmup_bars),
            )
            if self.warmup_bars
            else raw_start
        )
        if cut is not None and scored_start >= cut:
            raise ValueError("warmup_bars consumes the entire optimization period")
        self.bounds = (scored_start, end)
        if cut is not None:
            self.optimize_range = (scored_start, cut)
            self.holdout_range = (cut, end)
        else:
            self.optimize_range = (scored_start, end)
            self.holdout_range = None
        self.folds = _split_range(*self.optimize_range, max(1, folds))

    # -- public ----------------------------------------------------------
    def run(self, method: str = "random", trials: int = 40) -> OptimizationResult:
        if method not in METHODS:
            raise ValueError(f"method must be one of {METHODS}")
        if trials < 1:
            raise ValueError("trials must be >= 1")
        started = time.perf_counter()

        if method == "grid":
            evaluations = self._grid(trials)
        elif method == "de":
            evaluations = self._de(trials)
        else:
            evaluations = self._random(trials)

        unique: dict[tuple, Evaluation] = {}
        for evaluation in evaluations:
            unique[_param_key(evaluation.params)] = evaluation
        evaluations = sorted(unique.values(), key=lambda e: e.score, reverse=True)
        best = next((e for e in evaluations if e.valid), None)
        if best is None:
            errors = sorted({e.error for e in evaluations if e.error})
            raise RuntimeError(
                "no valid parameter set found. "
                + ("; ".join(errors[:5]) if errors else "all trials failed")
            )
        holdout = self._holdout(best.params) if self.holdout_range else None
        elapsed = time.perf_counter() - started
        self.log.info(
            "best %s=%s score=%.3f holdout=%s",
            self.strategy_name,
            best.params,
            best.score,
            holdout,
        )
        return OptimizationResult(
            run_id=self.run_id,
            strategy=self.strategy_name,
            method=method,
            objective=self.objective,
            warmup_bars=self.warmup_bars,
            spaces=self.spaces,
            base_params=self.base_params,
            bounds=self.bounds,
            optimize_range=self.optimize_range,
            folds=self.folds,
            holdout_range=self.holdout_range,
            best=best,
            evaluations=evaluations,
            holdout=holdout,
            elapsed_s=elapsed,
            config=self.cfg.as_dict(),
        )

    # -- evaluation ------------------------------------------------------
    def _evaluate(self, params: dict[str, Any]) -> Evaluation:
        key = _param_key(params)
        if key in self._cache:
            return self._cache[key]
        self._counter += 1
        try:
            build(self.strategy_name, params)  # fail fast on invalid combinations
        except Exception as exc:  # noqa: BLE001 - user strategy validation
            evaluation = Evaluation(params=params, score=-1e9, error=str(exc))
            self._cache[key] = evaluation
            return evaluation

        fold_metrics: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, (start, end) in enumerate(self.folds):
            try:
                strategy = build(self.strategy_name, params)
                engine = BacktestEngine(
                    self.cfg,
                    strategy,
                    self.feed,
                    run_id=f"{self.run_id}-t{self._counter}-f{index}",
                    logger=self._quiet,
                )
                result = engine.run(
                    start=start, end=end, warmup_bars=self.warmup_bars
                )
            except Exception as exc:  # noqa: BLE001 - keep the search alive
                errors.append(f"fold {index}: {exc}")
                continue
            if not result.equity_curve:
                errors.append(f"fold {index}: no bars")
                continue
            metrics = _fold_metrics(result.metrics)
            metrics["from"] = _iso(start)
            metrics["to"] = _iso(end)
            fold_metrics.append(metrics)

        if not fold_metrics:
            evaluation = Evaluation(
                params=params, score=-1e9, error="; ".join(errors) or "no fold produced data"
            )
            self._cache[key] = evaluation
            return evaluation

        values = [_metric(m, self.objective) for m in fold_metrics]
        values = [v for v in values if v is not None and math.isfinite(v)]
        trades = sum(int(m.get("trades") or 0) for m in fold_metrics)
        if not values or trades <= 0 or trades < self.min_trades:
            reason = (
                f"too few trades ({trades} < {max(1, self.min_trades)})"
                if values
                else f"objective {self.objective!r} unavailable"
            )
            evaluation = Evaluation(
                params=params, score=-1e9, folds=fold_metrics, trades=trades, error=reason
            )
            self._cache[key] = evaluation
            return evaluation

        mean = statistics.fmean(values)
        stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
        drawdown = statistics.fmean(abs(float(m.get("max_drawdown") or 0.0)) for m in fold_metrics)
        turnover = statistics.fmean(float(m.get("turnover") or 0.0) for m in fold_metrics)
        score = (
            mean
            - self.penalty * stdev
            - self.dd_penalty * drawdown
            - self.turnover_penalty * math.log1p(max(0.0, turnover))
        )
        evaluation = Evaluation(
            params=params,
            score=score,
            folds=fold_metrics,
            trades=trades,
            metric_mean=mean,
            metric_std=stdev,
            error="; ".join(errors),
        )
        self._cache[key] = evaluation
        return evaluation

    def _holdout(self, params: dict[str, Any]) -> dict[str, Any] | None:
        if not self.holdout_range:
            return None
        start, end = self.holdout_range
        try:
            strategy = build(self.strategy_name, params)
            result = BacktestEngine(
                self.cfg,
                strategy,
                self.feed,
                run_id=f"{self.run_id}-holdout",
                logger=self._quiet,
            ).run(start=start, end=end, warmup_bars=self.warmup_bars)
        except Exception as exc:  # noqa: BLE001
            return {"error": repr(exc)}
        return _fold_metrics(result.metrics)

    # -- search methods ---------------------------------------------------
    def _random(self, trials: int) -> list[Evaluation]:
        rng = random.Random(self.seed)
        evaluations: list[Evaluation] = []
        seen: set[tuple] = set()
        attempts = 0
        while len(evaluations) < trials and attempts < trials * 5:
            attempts += 1
            params = self._merge({s.name: s.sample(rng) for s in self.spaces})
            key = _param_key(params)
            if key in seen:
                continue
            seen.add(key)
            evaluations.append(self._evaluate(params))
        return evaluations

    def _grid(self, trials: int) -> list[Evaluation]:
        dimensions = [s.grid() for s in self.spaces]
        size = math.prod(len(d) for d in dimensions)
        if size > trials:
            raise ValueError(
                f"grid has {size:,} combinations but --trials is {trials}; "
                "raise --trials or use --method random/de"
            )
        evaluations = []
        for values in itertools.product(*dimensions):
            params = self._merge(dict(zip([s.name for s in self.spaces], values)))
            evaluations.append(self._evaluate(params))
        return evaluations

    def _de(self, trials: int) -> list[Evaluation]:
        """Differential evolution over the unit hypercube, decoded per space."""
        rng = random.Random(self.seed)
        dims = len(self.spaces)
        pop_size = min(trials, max(4, 4 * dims))
        population = [[rng.random() for _ in range(dims)] for _ in range(pop_size)]
        scores: list[Evaluation] = []
        evaluated: list[Evaluation] = []
        for vector in population:
            evaluation = self._evaluate(self._merge(self._decode(vector)))
            scores.append(evaluation)
            evaluated.append(evaluation)
        budget = trials - pop_size

        while budget > 0:
            improved = False
            for target in range(pop_size):
                if budget <= 0:
                    break
                others = [i for i in range(pop_size) if i != target]
                if len(others) < 3:
                    break
                a, b, c = rng.sample(others, 3)
                mutant = [
                    population[a][j] + 0.7 * (population[b][j] - population[c][j])
                    for j in range(dims)
                ]
                trial = [
                    mutant[j] if rng.random() < 0.9 else population[target][j]
                    for j in range(dims)
                ]
                candidate = self._evaluate(self._merge(self._decode(trial)))
                evaluated.append(candidate)
                budget -= 1
                if candidate.score > scores[target].score:
                    population[target] = [min(1.0, max(0.0, x)) for x in trial]
                    scores[target] = candidate
                    improved = True
            if not improved and budget <= 0:
                break
        return evaluated

    # -- helpers ---------------------------------------------------------
    def _merge(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return expand_paths({**self.base_params, **candidate})

    def _decode(self, vector: Sequence[float]) -> dict[str, Any]:
        return {space.name: space.decode(x) for space, x in zip(self.spaces, vector)}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_optimization_summary(result: OptimizationResult, top: int = 10) -> str:
    lines = []
    width = 92
    lines.append("=" * width)
    lines.append(
        f" Krabs optimize | {result.strategy} | {result.method} | objective={result.objective}"
    )
    lines.append(
        f" data {_short_ts(result.bounds[0])} -> {_short_ts(result.bounds[1])} | "
        f"optimize {_short_ts(result.optimize_range[0])} -> {_short_ts(result.optimize_range[1])}"
    )
    if result.warmup_bars:
        lines.append(f" warm start: {result.warmup_bars} bars before every fold/holdout")
    if result.holdout_range:
        lines.append(
            f" holdout {_short_ts(result.holdout_range[0])} -> "
            f"{_short_ts(result.holdout_range[1])} (never used for selection)"
        )
    lines.append(f" folds={len(result.folds)}  trials={len(result.evaluations)}")
    lines.append("-" * width)
    if result.best:
        lines.append(f" best score : {result.best.score:.4f}")
        lines.append(f" best params: {result.best.params}")
        lines.append(
            f" in-sample  : mean={_fmt(result.best.metric_mean)} "
            f"std={_fmt(result.best.metric_std)} trades={result.best.trades}"
        )
        lines.append(" folds:")
        lines.append(
            f"   {'#':>2} {'from':<11} {'to':<11} {'sharpe':>8} {'return':>9} "
            f"{'maxDD':>8} {'trades':>7}"
        )
        for i, fold in enumerate(result.best.folds):
            lines.append(
                f"   {i:>2} {_short_ts(fold.get('from')):<11} {_short_ts(fold.get('to')):<11} "
                f"{_fmt(fold.get('sharpe')):>8} {_pct(fold.get('total_return')):>9} "
                f"{_pct(fold.get('max_drawdown')):>8} {int(fold.get('trades') or 0):>7}"
            )
    if result.holdout:
        lines.append("-" * width)
        if "error" in result.holdout:
            lines.append(f" holdout    : ERROR {result.holdout['error']}")
        else:
            lines.append(
                f" holdout    : sharpe={_fmt(result.holdout.get('sharpe'))} "
                f"return={_pct(result.holdout.get('total_return'))} "
                f"maxDD={_pct(result.holdout.get('max_drawdown'))} "
                f"trades={int(result.holdout.get('trades') or 0)}"
            )
    valid = [e for e in result.evaluations if e.valid]
    if valid:
        lines.append("-" * width)
        lines.append(" top trials")
        lines.append(f"   {'rank':<5} {'score':>9} {'sharpe':>8} {'return':>9} {'maxDD':>8}  params")
        for rank, evaluation in enumerate(valid[:top], 1):
            fold0 = evaluation.folds[0] if evaluation.folds else {}
            lines.append(
                f"   {rank:<5} {evaluation.score:>9.3f} "
                f"{_fmt(fold0.get('sharpe')):>8} {_pct(fold0.get('total_return')):>9} "
                f"{_pct(fold0.get('max_drawdown')):>8}  {evaluation.params}"
            )
    lines.append("=" * width)
    return "\n".join(lines)


def write_optimization_report(result: OptimizationResult, report_dir: str | Path) -> Path:
    out = Path(report_dir) / result.run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(
        _json_dumps(result.to_dict()), encoding="utf-8"
    )
    (out / "summary.txt").write_text(render_optimization_summary(result) + "\n", encoding="utf-8")

    with (out / "trials.csv").open("w", encoding="utf-8", newline="") as fh:
        import csv

        writer = csv.writer(fh)
        writer.writerow(
            ["rank", "score", "params", "sharpe", "return", "max_drawdown", "trades", "error"]
        )
        for rank, evaluation in enumerate(result.evaluations, 1):
            fold0 = evaluation.folds[0] if evaluation.folds else {}
            writer.writerow(
                [
                    rank,
                    f"{evaluation.score:.6f}",
                    _json_dumps(evaluation.params),
                    _fmt(fold0.get("sharpe")),
                    _fmt(fold0.get("total_return")),
                    _fmt(fold0.get("max_drawdown")),
                    evaluation.trades,
                    evaluation.error,
                ]
            )
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_bars(
    feed,
    symbols: Sequence[str],
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Bar]:
    """Materialise a feed once so the optimizer does not re-read it per trial."""
    if hasattr(feed, "history"):
        if start is None:
            start = datetime.now(timezone.utc) - timedelta(days=365)
        return list(feed.history(list(symbols), timeframe, start, end))
    return [bar for chunk in feed.bars(list(symbols), timeframe, start, end) for bar in chunk]


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((str(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _param_key(params: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted((key, _freeze(value)) for key, value in flatten(params).items()))


def _after_warmup(feed, timeframe: str, start: datetime, warmup_bars: int) -> datetime:
    """First timestamp after ``warmup_bars`` distinct bars of feed history."""
    if warmup_bars <= 0:
        return start
    count = 0
    for chunk in feed.bars(None, timeframe):
        if not chunk:
            continue
        count += 1
        if count > warmup_bars:
            return max(start, chunk[0].ts)
    return start


def _split_range(
    start: datetime, end: datetime, folds: int
) -> list[tuple[datetime, datetime]]:
    if folds <= 1 or end <= start:
        return [(start, end)]
    span = end - start
    out = []
    for i in range(folds):
        a = start + span * (i / folds)
        b = start + span * ((i + 1) / folds) if i < folds - 1 else end
        out.append((a, b))
    return out


def _fold_metrics(metrics: dict) -> dict[str, Any]:
    return {
        "sharpe": _finite(metrics.get("sharpe")),
        "sortino": _finite(metrics.get("sortino")),
        "total_return": _finite(metrics.get("total_return")),
        "max_drawdown": _finite(metrics.get("max_drawdown")),
        "profit_factor": _finite(metrics.get("profit_factor")),
        "turnover": _finite(metrics.get("turnover")),
        "trades": int(metrics.get("trades") or 0),
        "exposure": _finite(metrics.get("exposure")),
        "final_equity": _finite(metrics.get("final_equity")),
    }


def _metric(metrics: dict[str, Any], objective: str) -> float | None:
    value = metrics.get(objective)
    if objective == "calmar":
        ret = metrics.get("total_return")
        dd = metrics.get("max_drawdown")
        if ret is None or dd in (None, 0):
            return None
        return float(ret) / abs(float(dd))
    if objective == "profit_factor" and value is None:
        # No losing trades in the fold: a large but finite reward, not infinity.
        return 10.0
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(_clean(value), indent=None)


def _clean(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _short_ts(ts: datetime | str | None) -> str:
    if isinstance(ts, str):
        return ts[:10]
    return ts.strftime("%Y-%m-%d") if ts else "n/a"


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"
