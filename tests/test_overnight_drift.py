from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from krabs.backtest import BacktestEngine
from krabs.config import Config
from krabs.events import Bar, Side
from krabs.market.calendar import EASTERN
from krabs.feeds.base import InMemoryFeed
from krabs.strategy import build, load_builtin
from krabs.strategy.examples.overnight_drift import OvernightDrift

START = datetime(2024, 1, 2, 9, 35, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def day_bars(day: int, price: float = 100.0) -> list[Bar]:
    bars = []
    for minute in range(0, 78):
        ts = START + timedelta(days=day, minutes=5 * minute)
        bars.append(
            Bar("SPY", ts, price, price, price, price, 1_000_000, timeframe="5Min")
        )
    return bars


def config() -> Config:
    return Config.from_dict(
        {
            "app": {"mode": "backtest"},
            "market": {"symbols": ["SPY"], "timeframe": "5Min"},
            "wallet": {"starting_cash": 100_000.0, "allow_short": False},
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
            "strategy": {"name": "overnight_drift", "params": {}},
        }
    )


class OvernightDriftTests(unittest.TestCase):
    def test_enters_before_close_and_exits_after_open(self):
        bars = day_bars(0) + day_bars(1) + day_bars(2)
        strategy = OvernightDrift(
            symbols=["SPY"],
            allocation=0.95,
            entry_time="15:55",
            exit_time="09:35",
        )
        result = BacktestEngine(
            config(), strategy, InMemoryFeed(bars), run_id="overnight"
        ).run()

        self.assertGreaterEqual(len(result.fills), 3)
        first = result.fills[0]
        self.assertIs(first.side, Side.BUY)
        self.assertEqual(first.ts.astimezone(EASTERN).time().strftime("%H:%M"), "16:00")
        exit_fill = next(fill for fill in result.fills if fill.side is Side.SELL)
        self.assertEqual(exit_fill.ts.astimezone(EASTERN).time().strftime("%H:%M"), "09:40")
        self.assertGreater(exit_fill.ts.date(), first.ts.date())

    def test_validation(self):
        with self.assertRaises(ValueError):
            OvernightDrift(symbols=[])
        with self.assertRaises(ValueError):
            OvernightDrift(entry_time="09:35", exit_time="15:55")
        with self.assertRaises(ValueError):
            OvernightDrift(allocation=0)

    def test_example_config_builds(self):
        load_builtin()
        cfg = Config.from_file("configs/overnight-drift.toml")
        strategy = build(cfg.strategy.name, cfg.strategy.params)
        self.assertEqual(strategy.id, "overnight_drift")
        self.assertEqual(strategy.symbols, ["SPY", "SMH", "XME", "XOP"])


if __name__ == "__main__":
    unittest.main()
