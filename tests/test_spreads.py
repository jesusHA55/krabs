from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from krabs.brokers.paper import FillModel, PaperBroker
from krabs.clock import SimulatedClock
from krabs.config import Config, ConfigError
from krabs.events import Bar, OrderIntent, Side
from krabs.spreads import load_spreads
from krabs.wallet import PaperWallet

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)


def make_bar(symbol: str, minute: int, price: float) -> Bar:
    return Bar(symbol, T0 + timedelta(minutes=minute), price, price, price, price, 1_000_000)


class LoadSpreadsTests(unittest.TestCase):
    def test_parses_median_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spreads.csv"
            path.write_text(
                "symbol,samples,median_spread_bps,mean_spread_bps\n"
                "xlk,10,1.25,1.4\nSMH,10,3.5,4.0\n",
                encoding="utf-8",
            )
            self.assertEqual(load_spreads(path), {"XLK": 1.25, "SMH": 3.5})

    def test_accepts_spread_bps_column_and_skips_bad_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spreads.csv"
            path.write_text(
                "symbol,spread_bps\nAAA,2.0\n,4.0\nBBB,oops\nCCC,-1\nDDD,0\n",
                encoding="utf-8",
            )
            with self.assertLogs("krabs.spreads", level=logging.WARNING):
                spreads = load_spreads(path)
            self.assertEqual(spreads, {"AAA": 2.0, "DDD": 0.0})

    def test_missing_file_is_a_warning_not_an_error(self):
        with self.assertLogs("krabs.spreads", level=logging.WARNING):
            self.assertEqual(load_spreads("/nonexistent/spreads.csv"), {})


class ExecutionSpreadCostTests(unittest.TestCase):
    def test_config_merges_inline_and_csv_spreads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spreads.csv"
            path.write_text("symbol,median_spread_bps\nAAA,5.0\nBBB,8.0\n", encoding="utf-8")
            cfg = Config.from_dict(
                {"execution": {"spread_bps": {"AAA": 2.0, "CCC": 1.0}, "spreads_csv": str(path)}}
            )
            self.assertEqual(cfg.execution.spread_costs(), {"AAA": 5.0, "BBB": 8.0, "CCC": 1.0})

    def test_spread_multiplier_scales_execution_costs(self):
        cfg = Config.from_dict(
            {
                "execution": {
                    "spread_bps": {"AAA": 3.0},
                    "spread_multiplier": 2.0,
                }
            }
        )
        self.assertAlmostEqual(cfg.execution.spread_costs()["AAA"], 6.0)

    def test_negative_spread_is_rejected(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"execution": {"spread_bps": {"AAA": -1.0}}})

    def test_fill_uses_half_of_symbol_spread(self):
        clock = SimulatedClock(T0)
        wallet = PaperWallet(100_000)
        broker = PaperBroker(
            wallet,
            clock,
            FillModel(
                slippage_bps=0,
                half_spread_bps=1.0,
                max_participation=0,
                spread_bps_by_symbol={"AAA": 40.0},
            ),
        )
        broker.on_event(make_bar("AAA", 1, 100.0))
        broker.submit(OrderIntent("AAA", Side.BUY, qty=1))
        broker.on_event(make_bar("AAA", 2, 100.0))
        # 40 bps full spread -> 20 bps half-spread on the buy.
        self.assertAlmostEqual(wallet.position("AAA").avg_price, 100.20, places=4)

    def test_unknown_symbol_falls_back_to_global_half_spread(self):
        clock = SimulatedClock(T0)
        wallet = PaperWallet(100_000)
        broker = PaperBroker(
            wallet,
            clock,
            FillModel(
                slippage_bps=0,
                half_spread_bps=10.0,
                max_participation=0,
                spread_bps_by_symbol={"AAA": 40.0},
            ),
        )
        broker.on_event(make_bar("BBB", 1, 100.0))
        broker.submit(OrderIntent("BBB", Side.BUY, qty=1))
        broker.on_event(make_bar("BBB", 2, 100.0))
        self.assertAlmostEqual(wallet.position("BBB").avg_price, 100.10, places=4)


if __name__ == "__main__":
    unittest.main()
