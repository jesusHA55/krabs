from __future__ import annotations

import unittest
from datetime import datetime, timezone

from krabs.events import Fill, Side
from krabs.wallet import PaperWallet

TS = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)


def fill(side: Side, qty: float, price: float, commission: float = 0.0) -> Fill:
    return Fill("o", "AAPL", side, qty, price, TS, commission=commission)


class WalletTests(unittest.TestCase):
    def test_long_round_trip(self):
        w = PaperWallet(10_000)
        w.apply_fill(fill(Side.BUY, 10, 100))
        state = w.snapshot(TS)
        self.assertAlmostEqual(state.cash, 9_000)
        self.assertAlmostEqual(state.equity, 10_000)

        w.mark("AAPL", 110)
        self.assertAlmostEqual(w.snapshot(TS).equity, 10_100)
        self.assertAlmostEqual(w.position("AAPL").unrealized_pnl, 100)

        w.apply_fill(fill(Side.SELL, 10, 110))
        state = w.snapshot(TS)
        self.assertAlmostEqual(state.cash, 10_100)
        self.assertAlmostEqual(state.realized_pnl, 100)
        self.assertEqual(w.positions(), {})

    def test_short_round_trip(self):
        w = PaperWallet(10_000, allow_short=True)
        w.apply_fill(fill(Side.SELL, 10, 100))
        self.assertAlmostEqual(w.snapshot(TS).cash, 11_000)
        self.assertAlmostEqual(w.position("AAPL").qty, -10)

        w.mark("AAPL", 90)
        self.assertAlmostEqual(w.snapshot(TS).equity, 10_100)

        w.apply_fill(fill(Side.BUY, 10, 90))
        self.assertAlmostEqual(w.snapshot(TS).realized_pnl, 100)
        self.assertAlmostEqual(w.snapshot(TS).cash, 10_100)

    def test_average_price_when_adding(self):
        w = PaperWallet(10_000)
        w.apply_fill(fill(Side.BUY, 10, 100))
        w.apply_fill(fill(Side.BUY, 10, 120))
        self.assertAlmostEqual(w.position("AAPL").avg_price, 110)

    def test_partial_close_keeps_average(self):
        w = PaperWallet(10_000)
        w.apply_fill(fill(Side.BUY, 10, 100))
        w.apply_fill(fill(Side.SELL, 4, 120))
        pos = w.position("AAPL")
        self.assertAlmostEqual(pos.qty, 6)
        self.assertAlmostEqual(pos.avg_price, 100)
        self.assertAlmostEqual(w.snapshot(TS).realized_pnl, 80)

    def test_buying_power_respects_positions(self):
        w = PaperWallet(10_000)
        w.apply_fill(fill(Side.BUY, 50, 100))
        state = w.snapshot(TS)
        self.assertAlmostEqual(state.buying_power, 5_000)

    def test_commission_reduces_equity(self):
        w = PaperWallet(10_000)
        w.apply_fill(fill(Side.BUY, 10, 100, commission=2.5))
        self.assertAlmostEqual(w.snapshot(TS).cash, 8_997.5)
        self.assertAlmostEqual(w.snapshot(TS).equity, 9_997.5)

    def test_affordability(self):
        w = PaperWallet(1_000)
        ok, reason = w.can_afford("AAPL", Side.BUY, 11, 100)
        self.assertFalse(ok)
        self.assertIn("buying power", reason)
        ok, _ = w.can_afford("AAPL", Side.BUY, 5, 100)
        self.assertTrue(ok)
        ok, reason = w.can_afford("AAPL", Side.SELL, 1, 100)
        self.assertFalse(ok)
        self.assertIn("short", reason)

    def test_day_reset(self):
        w = PaperWallet(10_000)
        w.apply_fill(fill(Side.BUY, 10, 100))
        w.mark("AAPL", 120)
        w.reset_day(TS)
        self.assertAlmostEqual(w.snapshot(TS).day_start_equity, 10_200)
        self.assertAlmostEqual(w.snapshot(TS).day_return, 0.0)


if __name__ == "__main__":
    unittest.main()
