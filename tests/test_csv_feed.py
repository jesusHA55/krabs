from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from krabs.feeds.csv import CsvFeed


HEADER = "ts,symbol,open,high,low,close,volume\n"


def row(ts: str, symbol: str, price: float, volume: float = 1000) -> str:
    return f"{ts},{symbol},{price},{price + 1},{price - 1},{price},{volume}\n"


class CsvFeedTests(unittest.TestCase):
    def test_symbols_and_bars(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.csv"
            path.write_text(
                HEADER
                + row("2024-01-02T21:00:00+00:00", "AAPL", 100)
                + row("2024-01-02T21:00:00+00:00", "MSFT", 200)
                + row("2024-01-03T21:00:00+00:00", "AAPL", 101)
                + row("2024-01-03T21:00:00+00:00", "MSFT", 202),
                encoding="utf-8",
            )
            feed = CsvFeed(path, "1Day")
            self.assertEqual(feed.symbols(), ["AAPL", "MSFT"])
            chunks = list(feed.bars(["AAPL", "MSFT"], "1Day"))
            self.assertEqual(len(chunks), 2)
            self.assertEqual(len(chunks[0]), 2)
            self.assertEqual(chunks[0][0].ts, datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc))
            self.assertEqual(chunks[0][0].timeframe, "1Day")

    def test_missing_column_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            path.write_text("ts,symbol,close\n2024-01-02T21:00:00+00:00,AAPL,100\n")
            feed = CsvFeed(path, "1Day")
            with self.assertRaises(ValueError):
                list(feed.bars([], "1Day"))


if __name__ == "__main__":
    unittest.main()
