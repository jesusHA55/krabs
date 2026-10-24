from __future__ import annotations

import math
import random
import unittest
from datetime import datetime, timedelta, timezone

from krabs.backtest import BacktestEngine
from krabs.config import Config
from krabs.events import Bar
from krabs.feeds.base import group_bars
from krabs.strategy import build, load_builtin
from krabs.strategy.examples.pca_residual import PCAResidual

START = datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc)
SYMBOLS = [f"E{i}" for i in range(8)]


def make_bar(symbol: str, i: int, close: float) -> Bar:
    return Bar(
        symbol,
        START + timedelta(days=i),
        close,
        close * 1.002,
        close * 0.998,
        close,
        1_000_000,
        timeframe="1Day",
    )


def synthetic_bars(days: int = 420, seed: int = 3) -> list[Bar]:
    rng = random.Random(seed)
    loadings = [0.7 + 0.05 * i for i in range(len(SYMBOLS))]
    idio = [0.0] * len(SYMBOLS)
    levels = [100.0] * len(SYMBOLS)
    bars: list[Bar] = []
    for day in range(days):
        factor = rng.gauss(0.0002, 0.009)
        for i, symbol in enumerate(SYMBOLS):
            idio[i] = 0.55 * idio[i] + rng.gauss(0.0, 0.006)
            levels[i] *= math.exp(loadings[i] * factor + idio[i])
            bars.append(make_bar(symbol, day, levels[i]))
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
            "wallet": {"starting_cash": 100_000.0, "allow_short": True},
            "execution": {"max_participation": 0.0, "slippage_bps": 0.0},
            "risk": {
                "max_order_notional": 100_000.0,
                "max_position_pct": 0.4,
                "max_gross_exposure_pct": 0.9,
                "max_daily_loss_pct": 0.5,
                "max_open_orders": 100,
                "duplicate_window_s": 0.0,
                "max_data_age_s": 0.0,
            },
            "feed": {"provider": "synthetic"},
            "storage": {"enabled": False},
            "strategy": {"name": "pca_residual", "params": {}},
        }
    )


class PCAResidualStrategyTests(unittest.TestCase):
    def test_trades_the_panel_and_reports_state(self):
        strategy = PCAResidual(
            symbols=SYMBOLS,
            window=60,
            refit=10,
            components=3,
            entry_z=1.0,
            stop_z=5.0,
            friction_multiple=0.0,
            allocation=0.10,
            max_positions=4,
            max_gross=0.40,
            max_hold_bars=20,
            min_resid_bps=0.0,
            default_spread_bps=0.0,
            slippage_bps=0.0,
        )
        result = BacktestEngine(
            config(), strategy, Feed(synthetic_bars()), run_id="pca-test"
        ).run()

        self.assertGreater(len(result.fills), 0)
        self.assertLessEqual(set(fill.symbol for fill in result.fills), set(SYMBOLS))
        diagnostics = strategy.diagnostics()
        self.assertEqual(diagnostics["model"], "pca_residual")
        self.assertTrue(diagnostics["ready"])
        self.assertGreater(diagnostics["explained"], 0.0)
        self.assertGreater(diagnostics["entries"], 0)
        self.assertLessEqual(diagnostics["gross"], 0.40 + 1e-9)
        self.assertEqual(len(result.rejections), 0)

    def test_registry_builds_from_params(self):
        load_builtin()
        strategy = build(
            "pca_residual",
            {"symbols": SYMBOLS, "window": 60, "components": 2, "driver": SYMBOLS[0]},
        )
        self.assertEqual(strategy.id, "pca_residual")
        self.assertEqual(strategy.symbols, SYMBOLS)

    def test_example_config_builds(self):
        cfg = Config.from_file("configs/pca-residual.toml")
        strategy = build(cfg.strategy.name, cfg.strategy.params)
        self.assertEqual(strategy.id, "pca_residual")
        self.assertEqual(strategy.p("components"), 5)
        self.assertEqual(strategy.p("entry_z"), 2.5)
        self.assertEqual(len(strategy.symbols), 32)

    def test_validation(self):
        with self.assertRaises(ValueError):
            PCAResidual(symbols=SYMBOLS, components=len(SYMBOLS))
        with self.assertRaises(ValueError):
            PCAResidual(symbols=SYMBOLS, entry_z=3.0, stop_z=2.0)
        with self.assertRaises(ValueError):
            PCAResidual(symbols=["AAA", "BBB"])


if __name__ == "__main__":
    unittest.main()
