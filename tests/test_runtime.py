from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from krabs.brokers.paper import FillModel, PaperBroker
from krabs.clock import RealClock
from krabs.config import Config
from krabs.events import Bar
from krabs.risk import KillSwitch, RiskEngine
from krabs.runtime import TradingRuntime
from krabs.strategy.examples.buy_hold import BuyHold
from krabs.wallet import PaperWallet


class FakeFeed:
    def __init__(self, bars):
        self.bars = bars
        self.history_calls = 0
        self.latest_calls = 0

    def history(self, symbols, timeframe, start, end=None):
        self.history_calls += 1
        return [b for b in self.bars if b.symbol in symbols][:2]

    def latest_bars(self, symbols, timeframe, lookback=10):
        self.latest_calls += 1
        return [b for b in self.bars if b.symbol in symbols]


class RuntimeTests(unittest.TestCase):
    def test_paper_cycle_places_and_fills_orders(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        bars = [
            Bar("SPY", now - timedelta(minutes=5 - i), 400 + i, 401 + i, 399 + i, 400 + i, 1e6)
            for i in range(5)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_dict(
                {
                    "app": {"mode": "paper", "data_dir": tmp},
                    "market": {
                        "symbols": ["SPY"],
                        "timeframe": "1Min",
                        "enforce_market_hours": False,
                    },
                    "wallet": {"starting_cash": 10_000.0},
                    "execution": {
                        "slippage_bps": 0.0,
                        "half_spread_bps": 0.0,
                        "max_participation": 0.0,
                    },
                    "risk": {
                        "max_order_notional": 100_000.0,
                        "max_position_pct": 1.0,
                        "max_gross_exposure_pct": 1.0,
                        "max_daily_loss_pct": 0.5,
                        "max_open_orders": 10,
                        "max_data_age_s": 600.0,
                        "duplicate_window_s": 0.0,
                    },
                    "feed": {"provider": "synthetic", "poll_seconds": 0.01, "warmup_bars": 100},
                    "storage": {"enabled": False},
                }
            )
            wallet = PaperWallet(10_000)
            kill = KillSwitch(Path(tmp) / "KILL")
            risk = RiskEngine(cfg.risk, wallet, symbols=["SPY"], kill=kill)
            broker = PaperBroker(
                wallet,
                RealClock(),
                FillModel(slippage_bps=0, half_spread_bps=0, max_participation=0),
                run_id="runtime-test",
            )
            runtime = TradingRuntime(
                cfg,
                BuyHold(allocation=0.5),
                FakeFeed(bars),
                broker,
                wallet,
                risk,
                run_id="runtime-test",
                mode="paper",
                kill=kill,
            )
            stats = asyncio.run(runtime.run(max_cycles=3))

            self.assertEqual(stats.bars, 3)
            self.assertEqual(stats.orders, 1)
            self.assertEqual(stats.fills, 1)
            self.assertGreater(wallet.position("SPY").qty, 0)
            self.assertIsNotNone(stats.stopped_at)
            self.assertTrue((Path(tmp) / "heartbeat.json").exists())

            heartbeat = (Path(tmp) / "heartbeat.json").read_text()
            self.assertIn('"stopped": true', heartbeat)

    def test_kill_switch_blocks_new_orders(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        bars = [
            Bar("SPY", now - timedelta(minutes=3 - i), 400, 401, 399, 400, 1e6)
            for i in range(3)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_dict(
                {
                    "app": {"mode": "paper", "data_dir": tmp},
                    "market": {
                        "symbols": ["SPY"],
                        "timeframe": "1Min",
                        "enforce_market_hours": False,
                    },
                    "execution": {"max_participation": 0.0},
                    "risk": {"max_data_age_s": 600.0, "duplicate_window_s": 0.0},
                    "feed": {"provider": "synthetic", "poll_seconds": 0.01},
                    "storage": {"enabled": False},
                }
            )
            wallet = PaperWallet(10_000)
            kill = KillSwitch(Path(tmp) / "KILL")
            kill.engage("test")
            risk = RiskEngine(cfg.risk, wallet, symbols=["SPY"], kill=kill)
            broker = PaperBroker(wallet, RealClock(), FillModel(max_participation=0), run_id="k")
            runtime = TradingRuntime(
                cfg, BuyHold(allocation=0.5), FakeFeed(bars), broker, wallet, risk,
                run_id="k", mode="paper", kill=kill,
            )
            stats = asyncio.run(runtime.run(max_cycles=2))
            self.assertEqual(stats.orders, 0)
            self.assertEqual(stats.fills, 0)


class SplitFeed:
    """Warmup history ends at the previous close; live polling returns today."""

    def __init__(self, history_bars, live_bars):
        self.history_bars = history_bars
        self.live_bars = live_bars

    def history(self, symbols, timeframe, start, end=None):
        return [b for b in self.history_bars if b.symbol in symbols]

    def latest_bars(self, symbols, timeframe, lookback=10):
        return [b for b in self.live_bars if b.symbol in symbols]


class LarryDayStartTests(unittest.TestCase):
    def test_day_start_targets_are_not_rejected_as_stale(self):
        """Regression: on_day_start ran before the day's first bar was recorded,
        so with yesterday's close as the newest event every order was rejected
        as stale market data, and the targets were never retried."""
        import json

        from krabs.market.calendar import EASTERN
        from krabs.strategy.examples.larry import LarryTargets

        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        yesterday = now - timedelta(hours=18)
        history = [Bar("SPY", yesterday, 400, 401, 399, 400, 1e6)]
        live = [
            Bar("SPY", now - timedelta(minutes=2 - i), 400, 401, 399, 400, 1e6)
            for i in range(2)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            targets = Path(tmp) / "targets.json"
            as_of = live[0].ts.astimezone(EASTERN).date().isoformat()
            targets.write_text(json.dumps({"as_of": as_of, "targets": {"SPY": 0.5}}))
            cfg = Config.from_dict(
                {
                    "app": {"mode": "paper", "data_dir": tmp},
                    "market": {
                        "symbols": ["SPY"],
                        "timeframe": "1Min",
                        "enforce_market_hours": False,
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
                        "max_data_age_s": 300.0,
                        "duplicate_window_s": 0.0,
                    },
                    "feed": {"provider": "synthetic", "poll_seconds": 0.01},
                    "storage": {"enabled": False},
                }
            )
            wallet = PaperWallet(10_000)
            kill = KillSwitch(Path(tmp) / "KILL")
            risk = RiskEngine(cfg.risk, wallet, symbols=["SPY"], kill=kill)
            broker = PaperBroker(
                wallet,
                RealClock(),
                FillModel(slippage_bps=0, half_spread_bps=0, max_participation=0),
                run_id="larry-day-start",
            )
            strategy = LarryTargets(source=str(targets), drift_band=0.0)
            runtime = TradingRuntime(
                cfg,
                strategy,
                SplitFeed(history, live),
                broker,
                wallet,
                risk,
                run_id="larry-day-start",
                mode="paper",
                kill=kill,
            )
            stats = asyncio.run(runtime.run(max_cycles=2, cancel_on_exit=False))

            self.assertEqual(stats.orders, 1)
            self.assertIsNone(strategy.diagnostics()["pending_day"])
            self.assertEqual(strategy.diagnostics()["last_applied_day"], as_of)

    def test_catch_up_bars_do_not_raise_a_stale_halt(self):
        """Regression: the first fetch of the day returns pre-market bars from
        over an hour ago plus fresh ones. Each old chunk raised a stale-data
        halt (logged as an error, with cancel_all) before the fresh chunk
        cleared it."""
        import json

        from krabs.market.calendar import EASTERN
        from krabs.strategy.examples.larry import LarryTargets

        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        history = [Bar("SPY", now - timedelta(hours=18), 400, 401, 399, 400, 1e6)]
        live = [
            Bar("SPY", now - timedelta(minutes=80), 400, 401, 399, 400, 1e6),
            Bar("SPY", now - timedelta(minutes=1), 400, 401, 399, 400, 1e6),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            targets = Path(tmp) / "targets.json"
            as_of = live[-1].ts.astimezone(EASTERN).date().isoformat()
            targets.write_text(json.dumps({"as_of": as_of, "targets": {"SPY": 0.5}}))
            cfg = Config.from_dict(
                {
                    "app": {"mode": "paper", "data_dir": tmp},
                    "market": {
                        "symbols": ["SPY"],
                        "timeframe": "1Min",
                        "enforce_market_hours": False,
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
                        "max_data_age_s": 300.0,
                        "duplicate_window_s": 0.0,
                    },
                    "feed": {"provider": "synthetic", "poll_seconds": 0.01},
                    "storage": {"enabled": False},
                }
            )
            wallet = PaperWallet(10_000)
            kill = KillSwitch(Path(tmp) / "KILL")
            risk = RiskEngine(cfg.risk, wallet, symbols=["SPY"], kill=kill)
            broker = PaperBroker(
                wallet,
                RealClock(),
                FillModel(slippage_bps=0, half_spread_bps=0, max_participation=0),
                run_id="larry-catch-up",
            )
            strategy = LarryTargets(source=str(targets), drift_band=0.0)
            runtime = TradingRuntime(
                cfg,
                strategy,
                SplitFeed(history, live),
                broker,
                wallet,
                risk,
                run_id="larry-catch-up",
                mode="paper",
                kill=kill,
            )
            with self.assertLogs("krabs", level="INFO") as logs:
                stats = asyncio.run(runtime.run(max_cycles=1, cancel_on_exit=False))

            self.assertFalse(
                [line for line in logs.output if "risk halt" in line], logs.output
            )
            self.assertEqual(stats.orders, 1)
            self.assertEqual(strategy.diagnostics()["last_applied_day"], as_of)


if __name__ == "__main__":
    unittest.main()
