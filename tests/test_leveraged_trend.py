from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from krabs.backtest import BacktestEngine
from krabs.config import Config
from krabs.events import Bar
from krabs.feeds.base import InMemoryFeed
from krabs.strategy import build, load_builtin
from krabs.strategy.examples.leveraged_trend import LeveragedTrend

START = datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc)
SYMBOLS = ["SPY", "UPRO", "BIL"]


def bars(days: int = 340) -> list[Bar]:
    out = []
    for i in range(days):
        if i <= 260:
            spy = 100.0 + 0.20 * i
            upro = 50.0 + 0.60 * i
        else:
            spy = 100.0 + 0.20 * 260 - 0.40 * (i - 260)
            upro = 50.0 + 0.60 * 260 - 1.20 * (i - 260)
        bil = 100.0 + 0.005 * i
        for symbol, price in (("SPY", spy), ("UPRO", upro), ("BIL", bil)):
            out.append(
                Bar(
                    symbol,
                    START + timedelta(days=i),
                    price,
                    price,
                    price,
                    price,
                    1_000_000,
                    timeframe="1Day",
                )
            )
    return out


def config() -> Config:
    return Config.from_dict(
        {
            "app": {"mode": "backtest"},
            "market": {"symbols": SYMBOLS, "timeframe": "1Day"},
            "wallet": {"starting_cash": 100_000.0, "allow_short": False},
            "execution": {
                "slippage_bps": 0.0,
                "half_spread_bps": 0.0,
                "max_participation": 0.0,
            },
            "risk": {
                "max_order_notional": 1_000_000.0,
                "max_position_pct": 1.0,
                "max_gross_exposure_pct": 1.0,
                "max_daily_loss_pct": 0.9,
                "max_open_orders": 10,
                "duplicate_window_s": 0.0,
                "max_data_age_s": 0.0,
            },
            "feed": {"provider": "csv", "csv_path": "unused"},
            "storage": {"enabled": False},
            "strategy": {"name": "leveraged_trend", "params": {}},
        }
    )


class LeveragedTrendTests(unittest.TestCase):
    def test_holds_leverage_above_sma_and_defensive_below(self):
        strategy = LeveragedTrend(
            signal_symbol="SPY",
            trade_symbol="UPRO",
            defensive_symbol="BIL",
            sma_days=200,
            allocation=0.95,
        )
        result = BacktestEngine(
            config(), strategy, InMemoryFeed(bars()), run_id="leveraged"
        ).run()
        symbols = {fill.symbol for fill in result.fills}
        self.assertIn("UPRO", symbols)
        self.assertIn("BIL", symbols)
        self.assertGreater(strategy.diagnostics()["rebalances"], 0)
        self.assertEqual(strategy.diagnostics()["above"], 0)

    def test_validation(self):
        with self.assertRaises(ValueError):
            LeveragedTrend(trade_symbol="UPRO", defensive_symbol="UPRO")
        with self.assertRaises(ValueError):
            LeveragedTrend(signal_symbol="BIL", defensive_symbol="BIL")
        with self.assertRaises(ValueError):
            LeveragedTrend(sma_days=1)
        with self.assertRaises(ValueError):
            LeveragedTrend(allocation=0)

    def test_example_config_builds(self):
        load_builtin()
        cfg = Config.from_file("configs/leveraged-trend.toml")
        strategy = build(cfg.strategy.name, cfg.strategy.params)
        self.assertEqual(strategy.id, "leveraged_trend")
        self.assertEqual(strategy.trade_symbol, "UPRO")
        self.assertEqual(strategy.defensive_symbol, "BIL")


if __name__ == "__main__":
    unittest.main()
