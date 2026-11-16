from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from krabs.events import Bar, Fill, Order, Side
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

    def test_rejected_targets_are_retried(self) -> None:
        """Regression: rejected orders used to mark the day as applied, and the
        unchanged targets were skipped on every later day."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.json"
            path.write_text(
                json.dumps(self.payload(as_of="2026-10-23", targets={"BIL": 0.5})),
                encoding="utf-8",
            )
            strategy = LarryTargets(source=str(path), drift_band=0.0)
            wallet = PaperWallet(1000)
            bars = [Bar("BIL", TS, 100, 100, 100, 100, 1000)]
            intents = []
            accept = {"value": False}

            def submit(intent):
                intents.append(intent)
                if not accept["value"]:
                    return None  # risk rejection
                return Order(client_id=f"o{len(intents)}", intent=intent, qty=intent.qty or 0.0)

            ctx = StrategyContext(
                strategy_id="larry",
                wallet=wallet,
                submit=submit,
                bars=lambda symbol: [bar for bar in bars if bar.symbol == symbol],
                now=lambda: TS,
            )
            strategy.on_start(ctx)
            strategy.on_day_start(ctx, date(2026, 10, 26))
            self.assertEqual(len(intents), 1)
            self.assertEqual(strategy.diagnostics()["pending_day"], "2026-10-26")

            # Same bar timestamp: no retry storm within one chunk.
            strategy.on_bar(ctx, bars[0])
            self.assertEqual(len(intents), 1)

            # Next day, unchanged targets: still sent again.
            strategy.on_day_start(ctx, date(2026, 10, 27))
            self.assertEqual(len(intents), 2)

            # Accepted on a later bar: the day is marked applied.
            accept["value"] = True
            later = Bar("BIL", TS + timedelta(minutes=5), 100, 100, 100, 100, 1000)
            strategy.on_bar(ctx, later)
            self.assertEqual(len(intents), 3)
            self.assertIsNone(strategy.diagnostics()["pending_day"])
            self.assertEqual(strategy.diagnostics()["last_applied_day"], "2026-10-27")

    def _fast_setup(self, directory, *, weight=0.1, min_price=600.0, stop=0.01):
        path = Path(directory) / "targets.json"
        path.write_text(
            json.dumps(
                {
                    "as_of": "2026-11-16",
                    "targets": {"BIL": 0.5},
                    "fast_overnight": {
                        "session": "2026-11-17",
                        "symbol": "SPY",
                        "weight": weight,
                        "min_price": min_price,
                        "stop_pct": stop,
                        "entry_after": "15:55",
                        "exit_after": "09:35",
                    },
                }
            ),
            encoding="utf-8",
        )
        strategy = LarryTargets(source=str(path), drift_band=0.01, fast_overnight=True)
        wallet = PaperWallet(1000)
        bars: list[Bar] = []
        intents = []

        def submit(intent):
            intents.append(intent)
            price = [b for b in bars if b.symbol == intent.symbol][-1].close
            wallet.apply_fill(Fill(f"f{len(intents)}", intent.symbol, intent.side, intent.qty, price, bars[-1].ts))
            return Order(client_id=f"o{len(intents)}", intent=intent, qty=intent.qty or 0.0)

        ctx = StrategyContext(
            strategy_id="larry",
            wallet=wallet,
            submit=submit,
            bars=lambda symbol: [bar for bar in bars if bar.symbol == symbol],
            now=lambda: bars[-1].ts if bars else TS,
        )
        return strategy, wallet, bars, intents, ctx, path

    def test_fast_overnight_enters_before_close_and_exits_next_morning(self) -> None:
        def et(day, hh, mm):  # November: ET = UTC-5
            return datetime(2026, 11, day, hh + 5, mm, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as directory:
            strategy, wallet, bars, intents, ctx, path = self._fast_setup(directory)
            strategy.on_start(ctx)
            bars += [Bar("BIL", et(17, 9, 35), 100, 100, 100, 100, 1e5),
                     Bar("SPY", et(17, 9, 35), 650, 651, 649, 650, 1e6)]
            strategy.on_day_start(ctx, date(2026, 11, 17))
            self.assertEqual([i.symbol for i in intents], ["BIL"])

            bars.append(Bar("SPY", et(17, 15, 50), 650, 651, 649, 650, 1e6))
            strategy.on_bar(ctx, bars[-1])  # before entry_after: nothing
            self.assertEqual(len(intents), 1)

            bars.append(Bar("SPY", et(17, 15, 55), 650, 651, 649, 650, 1e6))
            strategy.on_bar(ctx, bars[-1])
            self.assertEqual((intents[-1].symbol, intents[-1].side), ("SPY", Side.BUY))
            self.assertAlmostEqual(wallet.position("SPY").qty * 650 / ctx.equity, 0.1, places=2)
            strategy.on_bar(ctx, bars[-1])  # one decision per session
            self.assertEqual(len(intents), 2)

            # Next morning: Larry's new file has no SPY in the base targets.
            path.write_text(json.dumps({"as_of": "2026-11-17", "targets": {"BIL": 0.5}}))
            bars.append(Bar("SPY", et(18, 9, 35), 652, 653, 651, 652, 1e6))
            strategy.on_day_start(ctx, date(2026, 11, 18))
            self.assertEqual((intents[-1].symbol, intents[-1].side), ("SPY", Side.SELL))
            self.assertTrue(wallet.position("SPY").is_flat)
            self.assertIn("entered 2026-11-17", strategy.diagnostics()["fast_overnight"]["status"])

    def test_fast_overnight_respects_price_gate_and_permission(self) -> None:
        def et(day, hh, mm):
            return datetime(2026, 11, day, hh + 5, mm, tzinfo=timezone.utc)

        for kwargs, close in (({"min_price": 700.0}, 650), ({"weight": 0.0}, 650)):
            with tempfile.TemporaryDirectory() as directory:
                strategy, wallet, bars, intents, ctx, _ = self._fast_setup(directory, **kwargs)
                strategy.on_start(ctx)
                bars += [Bar("BIL", et(17, 9, 35), 100, 100, 100, 100, 1e5)]
                strategy.on_day_start(ctx, date(2026, 11, 17))
                bars.append(Bar("SPY", et(17, 15, 55), close, close, close, close, 1e6))
                strategy.on_bar(ctx, bars[-1])
                self.assertEqual([i.symbol for i in intents], ["BIL"], kwargs)
                self.assertIn("skipped", strategy.diagnostics()["fast_overnight"]["status"])

    def test_fast_overnight_gap_stop_blocks_next_entry(self) -> None:
        def et(day, hh, mm):
            return datetime(2026, 11, day, hh + 5, mm, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as directory:
            strategy, wallet, bars, intents, ctx, path = self._fast_setup(directory)
            strategy.on_start(ctx)
            bars += [Bar("BIL", et(17, 9, 35), 100, 100, 100, 100, 1e5),
                     Bar("SPY", et(17, 15, 55), 650, 651, 649, 650, 1e6)]
            strategy.on_day_start(ctx, date(2026, 11, 17))
            strategy.on_bar(ctx, bars[-1])
            self.assertEqual(intents[-1].symbol, "SPY")

            # Permission for the 18th, but SPY opens 2% lower than the entry.
            payload = json.loads(path.read_text())
            payload["as_of"] = "2026-11-17"
            payload["fast_overnight"]["session"] = "2026-11-18"
            path.write_text(json.dumps(payload))
            bars.append(Bar("SPY", et(18, 9, 35), 637, 640, 636, 638, 1e6))
            strategy.on_day_start(ctx, date(2026, 11, 18))
            sells = len(intents)
            bars.append(Bar("SPY", et(18, 15, 55), 640, 641, 639, 640, 1e6))
            strategy.on_bar(ctx, bars[-1])
            self.assertEqual(len(intents), sells)
            self.assertIn("overnight stop", strategy.diagnostics()["fast_overnight"]["status"])

    def test_requires_source_or_static(self) -> None:
        with self.assertRaises(ValueError):
            LarryTargets()


if __name__ == "__main__":
    unittest.main()
