from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from krabs.config import Config
from krabs.events import Bar
from krabs.feeds.base import InMemoryFeed
from krabs.optimize import (
    ParamSpace,
    StrategyOptimizer,
    parse_space,
    render_optimization_summary,
    write_optimization_report,
)
from krabs.strategy.examples.sma_cross import SmaCross

START = datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc)


def make_bars(days: int = 120) -> list[Bar]:
    bars = []
    price = 100.0
    for i in range(days):
        # Alternate trends so a crossover strategy actually trades.
        drift = 0.004 if (i // 15) % 2 == 0 else -0.003
        close = max(1.0, price * (1.0 + drift + 0.002 * math.sin(i / 3)))
        bars.append(
            Bar(
                "SPY",
                START + timedelta(days=i),
                open=price,
                high=max(price, close) * 1.005,
                low=min(price, close) * 0.995,
                close=close,
                volume=1_000_000,
                timeframe="1Day",
            )
        )
        price = close
    return bars


def cfg() -> Config:
    return Config.from_dict(
        {
            "app": {"mode": "backtest"},
            "market": {"symbols": ["SPY"], "timeframe": "1Day"},
            "wallet": {"starting_cash": 100_000.0},
            "execution": {"max_participation": 0.0, "slippage_bps": 0.0, "half_spread_bps": 0.0},
            "risk": {
                "max_order_notional": 100_000.0,
                "max_position_pct": 1.0,
                "max_gross_exposure_pct": 1.0,
                "max_daily_loss_pct": 0.9,
                "duplicate_window_s": 0.0,
                "max_data_age_s": 0.0,
            },
            "feed": {"provider": "synthetic", "warmup_bars": 100},
            "storage": {"enabled": False},
            "strategy": {"name": "sma_cross", "params": {}},
        }
    )


class ParamSpaceTests(unittest.TestCase):
    def test_int_space(self):
        space = parse_space("fast", "5:30", 20)
        self.assertEqual(space.kind, "int")
        self.assertEqual(space.grid(), list(range(5, 31)))
        value = space.decode(0.0)
        self.assertEqual(value, 5)
        self.assertAlmostEqual(space.encode(value), 0.0)

    def test_float_space_with_step(self):
        space = parse_space("allocation", "0.1:0.4:0.05", 0.2)
        self.assertEqual(space.kind, "float")
        self.assertAlmostEqual(space.grid()[0], 0.1)
        self.assertAlmostEqual(space.grid()[-1], 0.4)
        self.assertAlmostEqual(space.decode(0.5), 0.25, places=6)

    def test_categorical_space(self):
        space = parse_space("mode", "a,b,c", "a")
        self.assertEqual(space.kind, "categorical")
        self.assertEqual(space.grid(), ["a", "b", "c"])
        self.assertEqual(space.decode(1.0), "c")

    def test_invalid_space(self):
        with self.assertRaises(ValueError):
            parse_space("fast", "10:5", 20)
        with self.assertRaises(ValueError):
            parse_space("fast", "5:10", True)


class InMemoryFeedTests(unittest.TestCase):
    def test_bounds_and_slicing(self):
        bars = make_bars(20)
        feed = InMemoryFeed(bars)
        start, end = feed.bounds()
        self.assertEqual(start, bars[0].ts)
        self.assertEqual(end, bars[-1].ts)
        chunks = list(feed.bars(["SPY"], "1Day", bars[5].ts, bars[9].ts))
        self.assertEqual(len(chunks), 5)
        self.assertEqual(chunks[0][0].ts, bars[5].ts)


class OptimizerTests(unittest.TestCase):
    def test_warmup_shifts_start_and_warms_the_holdout(self):
        bars = make_bars(200)
        optimizer = StrategyOptimizer(
            cfg(),
            "sma_cross",
            InMemoryFeed(bars),
            [parse_space("fast", "2:6", 5)],
            folds=2,
            holdout=0.2,
            objective="total_return",
            warmup_bars=50,
            min_trades=1,
            seed=3,
        )
        self.assertEqual(optimizer.warmup_bars, 50)
        self.assertEqual(optimizer.bounds[0], bars[50].ts)
        raw_start, raw_end = bars[0].ts, bars[-1].ts
        self.assertEqual(
            optimizer.holdout_range[0],
            raw_start + (raw_end - raw_start) * 0.8,
        )

        metrics = optimizer._holdout({"fast": 5, "slow": 20, "allocation": 0.2})
        self.assertIsNotNone(metrics)
        self.assertGreater(metrics.get("trades", 0), 0)

    def test_param_key_handles_nested_lists(self):
        from krabs.optimize import _param_key

        first = {"model": {"symbols": ["AAA", "BBB"], "window": 20}, "entry": 2.0}
        second = {"entry": 2.0, "model": {"window": 20, "symbols": ["AAA", "BBB"]}}
        different = {"model": {"symbols": ["AAA", "CCC"], "window": 20}, "entry": 2.0}
        self.assertEqual(_param_key(first), _param_key(second))
        self.assertNotEqual(_param_key(first), _param_key(different))
        self.assertEqual(len({_param_key(first), _param_key(second)}), 1)

    def test_random_search_produces_best_and_holdout(self):
        spaces = [
            parse_space("fast", "2:6", 5),
            parse_space("slow", "8:20", 15),
            parse_space("allocation", "0.1:0.4", 0.2),
        ]
        optimizer = StrategyOptimizer(
            cfg(),
            "sma_cross",
            InMemoryFeed(make_bars(140)),
            spaces,
            folds=2,
            holdout=0.25,
            objective="total_return",
            min_trades=1,
            penalty=0.2,
            dd_penalty=0.2,
            seed=3,
        )
        result = optimizer.run("random", trials=8)

        self.assertIsNotNone(result.best)
        self.assertTrue(result.best.valid)
        self.assertEqual(len(result.folds), 2)
        self.assertIsNotNone(result.holdout)
        self.assertGreater(len(result.evaluations), 0)
        self.assertIn("sharpe", result.holdout)

        params = result.best.params
        self.assertGreaterEqual(params["fast"], 2)
        self.assertLessEqual(params["fast"], 6)
        self.assertGreaterEqual(params["slow"], 8)
        self.assertLessEqual(params["slow"], 20)
        self.assertGreaterEqual(params["allocation"], 0.1)
        self.assertLessEqual(params["allocation"], 0.4)
        self.assertLess(params["fast"], params["slow"])

        summary = render_optimization_summary(result)
        self.assertIn("holdout", summary)
        self.assertIn("best params", summary)

        with tempfile.TemporaryDirectory() as tmp:
            path = write_optimization_report(result, tmp)
            self.assertTrue((path / "result.json").is_file())
            self.assertTrue((path / "trials.csv").is_file())
            payload = json.loads((path / "result.json").read_text())
            self.assertEqual(payload["best"]["params"], params)

    def test_grid_rejects_oversized_space(self):
        optimizer = StrategyOptimizer(
            cfg(),
            "sma_cross",
            InMemoryFeed(make_bars(60)),
            [parse_space("fast", "2:6", 5), parse_space("slow", "8:20", 15)],
            folds=1,
            holdout=0.0,
        )
        with self.assertRaises(ValueError):
            optimizer.run("grid", trials=5)

    def test_de_runs_and_reports_budget(self):
        optimizer = StrategyOptimizer(
            cfg(),
            "sma_cross",
            InMemoryFeed(make_bars(100)),
            [parse_space("fast", "2:5", 5), parse_space("slow", "8:16", 10)],
            folds=1,
            holdout=0.2,
            objective="sharpe",
            min_trades=0,
            seed=5,
        )
        result = optimizer.run("de", trials=10)
        self.assertIsNotNone(result.best)
        self.assertLessEqual(len(result.evaluations), 10)
        self.assertGreaterEqual(len(result.evaluations), 1)

    def test_unknown_param_is_rejected(self):
        with self.assertRaises(ValueError):
            StrategyOptimizer(
                cfg(),
                "sma_cross",
                InMemoryFeed(make_bars(20)),
                [ParamSpace("nope", "int", low=1, high=2)],
            )


if __name__ == "__main__":
    unittest.main()
