from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from krabs.altdata.kalshi import ProbabilitySeries, points_from_payload
from krabs.altdata.truth import classify, load_archive, strip_html


class TruthPostsTests(unittest.TestCase):
    def test_strip_and_classify(self):
        self.assertEqual(
            strip_html('<p>Tariffs on <b>China</b> now!</p>'),
            "Tariffs on China now!",
        )
        self.assertIn("tariffs_trade", classify("New tariffs on China today"))
        self.assertIn("energy", classify("Oil and gas drilling"))
        self.assertNotIn("tariffs_trade", classify("Happy birthday to a great friend"))

    def test_load_csv_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "posts.csv"
            with path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=["id", "created_at", "content", "url"]
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "id": "1",
                        "created_at": "2026-10-01T12:00:00.000Z",
                        "content": "<p>Tariffs!</p>",
                        "url": "https://example/1",
                    }
                )
            posts = load_archive(path)
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0].text, "Tariffs!")
            self.assertEqual(posts[0].ts.tzinfo, timezone.utc)


class KalshiParsingTests(unittest.TestCase):
    def test_probability_series_is_as_of(self):
        payload = {
            "candlesticks": [
                {
                    "end_period_ts": int(
                        datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp()
                    ),
                    "yes_bid": {"close_dollars": "0.40"},
                    "yes_ask": {"close_dollars": "0.44"},
                },
                {
                    "end_period_ts": int(
                        datetime(2026, 1, 3, tzinfo=timezone.utc).timestamp()
                    ),
                    "yes_bid": {"close_dollars": "0.50"},
                    "yes_ask": {"close_dollars": "0.54"},
                },
            ]
        }
        series = points_from_payload(payload)
        self.assertEqual(len(series.points), 2)
        self.assertIsNone(series.at(datetime(2026, 1, 1, tzinfo=timezone.utc)))
        self.assertAlmostEqual(
            series.at(datetime(2026, 1, 2, 12, tzinfo=timezone.utc)), 0.42
        )
        self.assertAlmostEqual(
            series.at(datetime(2026, 1, 4, tzinfo=timezone.utc)), 0.52
        )
        self.assertAlmostEqual(
            series.change(
                datetime(2026, 1, 2, tzinfo=timezone.utc),
                datetime(2026, 1, 3, tzinfo=timezone.utc),
            ),
            0.10,
        )


if __name__ == "__main__":
    unittest.main()
