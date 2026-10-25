from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

from krabs.backtest import BacktestEngine
from krabs.config import Config
from krabs.events import Bar
from krabs.feeds.base import group_bars
from krabs.strategy import build, load_builtin
from krabs.strategy.examples.dual_momentum import DualMomentum

START = datetime(2020, 1, 2, 21, 0, tzinfo=timezone.utc)
SYMBOLS = ["SPY", "VEU", "AGG", "BIL"]


def make_bar(symbol: str, day: int, close: float) -> Bar:
    return Bar(
        symbol,
        START + timedelta(days=day),
        close,
        close,
        close,
        close,
        1_000_000,
        timeframe="1Day",
    )


def synthetic_bars(days: int = 900) -> list[Bar]:
    monthly = {"SPY": 0.015, "VEU": 0.005, "AGG": 0.003, "BIL": 0.0005}
    levels = {symbol: 100.0 for symbol in SYMBOLS}
    bars: list[Bar] = []
    for day in range(days):
        month = day // 21
        for symbol in SYMBOLS:
            growth = (1.0 + monthly[symbol]) ** (1.0 / 21.0) - 1.0
            levels[symbol] *= 1.0 + growth
            bars.append(make_bar(symbol, day, levels[symbol]))
    return bars


class Feed:
    def __init__(self, bars):
        self._bars = bars

    def bars(self, symbols, timeframe, start=None, end=None):
        yield from group_bars(
            [bar for bar in self._bars if bar.symbol in symbols], timeframe
        )


def config() -> Config:
    return Config.from_dict(
        {
            "app": {"mode": "backtest"},
            "market": {"symbols": SYMBOLS, "timeframe": "1Day"},
            "wallet": {
                "starting_cash": 100_000.0,
                "allow_short": False,
                "fractional": True,
            },
            "execution": {
                "slippage_bps": 0.0,
                "half_spread_bps": 0.0,
                "max_participation": 0.0,
            },
            "risk": {
                "max_order_notional": 100_000.0,
                "max_position_pct": 1.0,
                "max_gross_exposure_pct": 1.0,
                "max_daily_loss_pct": 0.9,
                "max_open_orders": 10,
                "duplicate_window_s": 0.0,
                "max_data_age_s": 0.0,
            },
            "feed": {"provider": "csv", "csv_path": "unused"},
            "storage": {"enabled": False},
            "strategy": {"name": "dual_momentum", "params": {}},
        }
    )


class DualMomentumSelectionTests(unittest.TestCase):
    def setUp(self):
        self.strategy = DualMomentum(
            risk_assets=["SPY", "VEU"],
            defensive_asset="AGG",
            absolute_asset="BIL",
            allocation=0.5,
            top_n=1,
        )

    def test_relative_and_absolute_momentum_pick_the_strongest(self):
        selected, fallback = self.strategy._select(
            {"SPY": 0.12, "VEU": 0.20, "AGG": 0.03, "BIL": 0.02},
            absolute_return=0.02,
        )
        self.assertEqual(selected, ["VEU"])
        self.assertIsNone(fallback)

    def test_absolute_failure_falls_back_to_defensive(self):
        selected, fallback = self.strategy._select(
            {"SPY": -0.10, "VEU": -0.15, "AGG": 0.03, "BIL": 0.02},
            absolute_return=0.02,
        )
        self.assertEqual(selected, [])
        self.assertEqual(fallback, "AGG")

    def test_top_n_requires_each_candidate_to_pass_absolute_filter(self):
        strategy = DualMomentum(
            risk_assets=["SPY", "VEU", "XLE"],
            defensive_asset="BIL",
            absolute_asset="",
            absolute_threshold=0.0,
            top_n=2,
            allocation=0.5,
        )
        selected, fallback = strategy._select(
            {"SPY": 0.12, "VEU": -0.02, "XLE": -0.03, "BIL": 0.01},
            absolute_return=0.0,
        )
        self.assertEqual(selected, ["SPY"])
        self.assertIsNone(fallback)

    def test_validation(self):
        with self.assertRaises(ValueError):
            DualMomentum(risk_assets=["SPY"], defensive_asset="SPY")
        with self.assertRaises(ValueError):
            DualMomentum(risk_assets=["SPY"], top_n=2)
        with self.assertRaises(ValueError):
            DualMomentum(risk_assets=["SPY"], allocation=0)


class DualMomentumBacktestTests(unittest.TestCase):
    def test_monthly_rebalance_holds_the_strongest_asset(self):
        strategy = DualMomentum(
            risk_assets=["SPY", "VEU"],
            defensive_asset="AGG",
            absolute_asset="BIL",
            lookback_months=12,
            top_n=1,
            allocation=0.95,
            driver="SPY",
        )
        result = BacktestEngine(
            config(), strategy, Feed(synthetic_bars()), run_id="dual-momentum"
        ).run()

        self.assertGreater(len(result.fills), 0)
        self.assertGreater(result.metrics["final_equity"], 100_000.0)
        self.assertEqual(strategy.diagnostics()["selected"], ["SPY"])
        self.assertIn("SPY", strategy.diagnostics()["returns"])

    def test_example_config_builds(self):
        load_builtin()
        cfg = Config.from_file("configs/dual-momentum.toml")
        strategy = build(cfg.strategy.name, cfg.strategy.params)
        self.assertEqual(strategy.id, "dual_momentum")
        self.assertEqual(strategy.risk_assets, ["SPY", "VEU"])
        self.assertEqual(strategy.defensive_asset, "AGG")


if __name__ == "__main__":
    unittest.main()
