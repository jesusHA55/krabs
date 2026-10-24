from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from krabs.feeds.synthetic_live import SyntheticLiveFeed


class SyntheticLiveFeedTests(unittest.TestCase):
    def test_history_and_incremental_bars(self):
        now = datetime.now(timezone.utc)
        feed = SyntheticLiveFeed(["SPY", "QQQ"], seed=11, history_days=1)
        history = feed.history(["SPY", "QQQ"], "1Min", now - timedelta(days=3), now - timedelta(hours=2))
        self.assertGreater(len(history), 0)
        self.assertEqual({b.symbol for b in history}, {"SPY", "QQQ"})

        first = feed.latest_bars(["SPY", "QQQ"], "1Min")
        self.assertEqual(len(first), 2)
        for bar in first:
            self.assertLessEqual(bar.ts, datetime.now(timezone.utc))
            self.assertGreater(bar.close, 0)
            self.assertGreaterEqual(bar.high, max(bar.open, bar.close))
            self.assertLessEqual(bar.low, min(bar.open, bar.close))

        second = feed.latest_bars(["SPY", "QQQ"], "1Min")
        first_sorted = sorted(first, key=lambda b: b.symbol)
        second_sorted = sorted(second, key=lambda b: b.symbol)
        for before, after in zip(first_sorted, second_sorted):
            self.assertEqual(before.symbol, after.symbol)
            self.assertGreater(after.ts, before.ts)

    def test_next_bar_is_continuous_with_history(self):
        now = datetime.now(timezone.utc)
        feed = SyntheticLiveFeed(["SPY"], seed=3, history_days=1)
        history = feed.history(["SPY"], "1Min", now - timedelta(days=2), now - timedelta(hours=1))
        last_close = history[-1].close
        bar = feed.latest_bars(["SPY"], "1Min")[0]
        self.assertAlmostEqual(bar.open, last_close, places=4)


if __name__ == "__main__":
    unittest.main()
