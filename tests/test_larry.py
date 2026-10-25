from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest

from krabs.events import Bar, Fill, Side
from krabs.strategy.base import StrategyContext
from krabs.strategy.examples.larry import LarryTargets
from krabs.wallet import PaperWallet

TS = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)


def make_ctx(wallet: PaperWallet, bars: list[Bar], intents: list):
    def submit(intent):
        intents.append(intent)
        return None

    return StrategyContext(
        strategy_id="larry",
        wallet=wallet,
        submit=submit,
        bars=lambda symbol: [bar for bar in bars if bar.symbol == symbol],
        now=lambda: TS,
    )


class LarryStrategyTests(unittest.TestCase):
    def payload(self, **overrides):
        base = {
            "as_of": "2026-10-23",
            "targets": {"SSO": 0.5, "BIL": 0.25},
        }
        base.update(overrides)
        return base

    def test_applies_targets_and_closes_unlisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.json"
            path.write_text(json.dumps(self.payload()), encoding="utf-8")
            strategy = LarryTargets(source=str(path), drift_band=0.0)
            wallet = PaperWallet(1000)
            bars = [
                Bar("SSO", TS, 50, 51, 49, 50, 1000),
                Bar("BIL", TS, 100, 100, 100, 100, 1000),
                Bar("SPY", TS, 700, 701, 699, 700, 1000),
            ]
            intents = []
            wallet.apply_fill(Fill("seed", "SPY", Side.BUY, 0.5, 700, TS))
            ctx = make_ctx(wallet, bars, intents)
            strategy.on_start(ctx)
            strategy.on_day_start(ctx, date(2026, 10, 26))
        symbols = [(intent.symbol, intent.side.value) for intent in intents]
        self.assertIn(("SPY", "sell"), symbols)
        self.assertIn(("SSO", "buy"), symbols)
        self.assertIn(("BIL", "buy"), symbols)

    def test_stale_targets_flatten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.json"
            path.write_text(json.dumps(self.payload(as_of="2026-10-01")), encoding="utf-8")
            strategy = LarryTargets(source=str(path), max_age_days=1, drift_band=0.0)
            wallet = PaperWallet(1000)
            bars = [Bar("SPY", TS, 700, 701, 699, 700, 1000)]
            intents = []
            wallet.apply_fill(Fill("seed", "SPY", Side.BUY, 0.5, 700, TS))
            ctx = make_ctx(wallet, bars, intents)
            strategy.on_start(ctx)
            strategy.on_day_start(ctx, date(2026, 10, 26))
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].symbol, "SPY")
        self.assertEqual(intents[0].side, Side.SELL)

    def test_historical_rows_are_causal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.json"
            path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {"as_of": "2026-10-22", "targets": {"BIL": 0.5}},
                            {"as_of": "2026-10-26", "targets": {"SSO": 0.5}},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            strategy = LarryTargets(source=str(path), drift_band=0.0)
            wallet = PaperWallet(1000)
            bars = [
                Bar("BIL", TS, 100, 100, 100, 100, 1000),
                Bar("SSO", TS, 50, 51, 49, 50, 1000),
            ]
            intents = []
            ctx = make_ctx(wallet, bars, intents)
            strategy.on_start(ctx)
            strategy.on_day_start(ctx, date(2026, 10, 23))
        self.assertEqual([intent.symbol for intent in intents], ["BIL"])
        self.assertEqual(intents[0].side, Side.BUY)

    def test_reloads_file_before_each_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.json"
            path.write_text(json.dumps(self.payload(as_of="2026-10-23", targets={"BIL": 0.5})), encoding="utf-8")
            strategy = LarryTargets(source=str(path), drift_band=0.0)
            wallet = PaperWallet(1000)
            bars = [
                Bar("BIL", TS, 100, 100, 100, 100, 1000),
                Bar("SSO", TS, 50, 51, 49, 50, 1000),
            ]
            intents = []
            ctx = make_ctx(wallet, bars, intents)
            strategy.on_start(ctx)
            strategy.on_day_start(ctx, date(2026, 10, 26))
            path.write_text(json.dumps(self.payload(as_of="2026-10-26", targets={"SSO": 0.5})), encoding="utf-8")
            strategy.on_day_start(ctx, date(2026, 10, 27))
        self.assertEqual([intent.symbol for intent in intents], ["BIL", "SSO"])

    def test_requires_source_or_static(self) -> None:
        with self.assertRaises(ValueError):
            LarryTargets()


if __name__ == "__main__":
    unittest.main()
