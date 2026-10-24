from __future__ import annotations

import math
import unittest

from krabs.indicators import (
    atr,
    backend_name,
    crosses_above,
    crosses_below,
    ema,
    max_drawdown,
    realized_volatility,
    rolling_std,
    rsi,
    sma,
)


class IndicatorTests(unittest.TestCase):
    def test_sma(self):
        out = sma([1, 2, 3, 4, 5], 3)
        self.assertTrue(math.isnan(out[0]) and math.isnan(out[1]))
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[4], 4.0)

    def test_ema(self):
        out = ema([1, 2, 3, 4, 5], 3)
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], 3.0)
        self.assertAlmostEqual(out[4], 4.0)

    def test_rsi_all_gains(self):
        out = rsi([float(i) for i in range(1, 30)], 14)
        self.assertAlmostEqual(out[-1], 100.0)

    def test_rsi_all_losses(self):
        out = rsi([float(i) for i in range(30, 1, -1)], 14)
        self.assertAlmostEqual(out[-1], 0.0)

    def test_atr_constant_range(self):
        highs = [11.0] * 20
        lows = [9.0] * 20
        closes = [10.0] * 20
        out = atr(highs, lows, closes, 14)
        self.assertAlmostEqual(out[-1], 2.0)

    def test_rolling_std(self):
        out = rolling_std([1, 2, 3, 4], 2)
        self.assertAlmostEqual(out[-1], 0.5)

    def test_max_drawdown(self):
        self.assertAlmostEqual(max_drawdown([100, 120, 90, 110]), -0.25)

    def test_realized_vol(self):
        vol = realized_volatility([0.01, -0.01, 0.01, -0.01], 252)
        self.assertGreater(vol, 0)

    def test_crosses(self):
        self.assertTrue(crosses_above([1, 3], [2, 2]))
        self.assertFalse(crosses_above([1, 2], [2, 2]))
        self.assertFalse(crosses_above([3, 4], [2, 2]))
        self.assertTrue(crosses_below([3, 1], [2, 2]))
        self.assertFalse(crosses_below([3, 2], [2, 2]))

    def test_backend(self):
        self.assertIn(backend_name(), ("rust", "python"))


if __name__ == "__main__":
    unittest.main()
