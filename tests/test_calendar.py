from __future__ import annotations

import unittest
from datetime import datetime, timezone

from krabs.market.calendar import (
    bar_is_closed,
    is_open,
    is_trading_day,
    next_open,
    previous_close,
    session_for,
    session_days,
)


def dt(y, m, d, hh=15, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


class CalendarTests(unittest.TestCase):
    def test_regular_session(self):
        self.assertTrue(is_open(dt(2024, 1, 2, 15)))
        self.assertFalse(is_open(dt(2024, 1, 2, 13)))  # 08:00 ET
        self.assertFalse(is_open(dt(2024, 1, 2, 22)))  # 17:00 ET

    def test_holiday_and_weekend(self):
        self.assertFalse(is_open(dt(2024, 1, 1, 15)))
        self.assertFalse(is_open(dt(2024, 1, 6, 15)))
        self.assertFalse(is_trading_day(datetime(2024, 1, 6).date()))

    def test_early_close(self):
        session = session_for(datetime(2024, 11, 29).date())
        self.assertIsNotNone(session)
        self.assertTrue(session.is_early_close)
        self.assertFalse(is_open(dt(2024, 11, 29, 19)))  # 14:00 ET
        self.assertTrue(is_open(dt(2024, 11, 29, 17)))  # 12:00 ET

    def test_next_open_after_friday_close(self):
        # Friday 2024-01-05 17:00 ET -> Monday 09:30 ET = 14:30 UTC (EST)
        self.assertEqual(next_open(dt(2024, 1, 5, 22)), dt(2024, 1, 8, 14, 30))

    def test_next_open_before_session(self):
        self.assertEqual(next_open(dt(2024, 1, 2, 12)), dt(2024, 1, 2, 14, 30))

    def test_previous_close(self):
        self.assertEqual(previous_close(dt(2024, 1, 2, 15)), dt(2023, 12, 29, 21))

    def test_session_days_skips_weekend(self):
        days = session_days(datetime(2024, 1, 5).date(), datetime(2024, 1, 9).date())
        self.assertEqual(len(days), 3)

    def test_bar_closed(self):
        self.assertTrue(bar_is_closed(dt(2024, 1, 2, 15), "1Min", dt(2024, 1, 2, 15)))
        self.assertFalse(bar_is_closed(dt(2024, 1, 2, 15), "1Min", dt(2024, 1, 2, 14, 59)))


if __name__ == "__main__":
    unittest.main()
