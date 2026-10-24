from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from krabs.config import RiskConfig
from krabs.events import Fill, OrderIntent, OrderType, Side
from krabs.risk import KillSwitch, RiskEngine
from krabs.risk.rules import RiskContext
from krabs.wallet import PaperWallet

NOW = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)


def base_cfg(**overrides) -> RiskConfig:
    values = dict(
        max_order_notional=100_000.0,
        max_position_pct=1.0,
        max_gross_exposure_pct=1.0,
        max_daily_loss_pct=0.05,
        max_open_orders=10,
        max_data_age_s=60.0,
        price_collar_pct=0.05,
        duplicate_window_s=0.0,
        allowed_symbols=[],
    )
    values.update(overrides)
    return RiskConfig(**values)


def engine(wallet: PaperWallet, **overrides) -> RiskEngine:
    risk = RiskEngine(base_cfg(**overrides), wallet, symbols=["AAPL"])
    risk.note_price("AAPL", 100.0)
    return risk


def check(risk: RiskEngine, intent: OrderIntent, wallet: PaperWallet, open_orders=None):
    return risk.check(
        intent,
        now=NOW,
        open_orders=open_orders or [],
        last_event_ts=NOW,
    )


class RiskTests(unittest.TestCase):
    def test_clamps_order_size(self):
        wallet = PaperWallet(1_000_000)
        risk = engine(wallet, max_order_notional=500.0)
        intent = OrderIntent("AAPL", Side.BUY, qty=100)
        decision = check(risk, intent, wallet)
        self.assertTrue(decision.approved)
        self.assertTrue(decision.adjusted)
        self.assertAlmostEqual(decision.intent.qty, 5.0)

    def test_clamps_position_size(self):
        wallet = PaperWallet(10_000)
        risk = engine(wallet, max_position_pct=0.10)
        intent = OrderIntent("AAPL", Side.BUY, qty=50)  # $5,000 vs $1,000 cap
        decision = check(risk, intent, wallet)
        self.assertTrue(decision.approved)
        self.assertAlmostEqual(decision.intent.qty, 10.0)

    def test_rejects_gross_exposure(self):
        wallet = PaperWallet(10_000)
        risk = engine(wallet, max_gross_exposure_pct=0.10)
        decision = check(risk, OrderIntent("AAPL", Side.BUY, qty=20), wallet)
        self.assertFalse(decision.approved)
        self.assertIn("gross exposure", decision.reason)

    def test_rejects_short_when_disabled(self):
        wallet = PaperWallet(10_000)
        risk = engine(wallet)
        decision = check(risk, OrderIntent("AAPL", Side.SELL, qty=1), wallet)
        self.assertFalse(decision.approved)
        self.assertIn("short", decision.reason)

    def test_price_collar(self):
        wallet = PaperWallet(10_000)
        risk = engine(wallet)
        intent = OrderIntent("AAPL", Side.BUY, qty=1, type=OrderType.LIMIT, limit_price=120)
        decision = check(risk, intent, wallet)
        self.assertFalse(decision.approved)
        self.assertIn("away from", decision.reason)

    def test_duplicate_guard(self):
        wallet = PaperWallet(10_000)
        risk = engine(wallet, duplicate_window_s=60)
        intent = OrderIntent("AAPL", Side.BUY, qty=1)
        self.assertTrue(check(risk, intent, wallet).approved)
        second = check(risk, intent, wallet)
        self.assertFalse(second.approved)
        self.assertIn("duplicate", second.reason)

    def test_open_order_cap(self):
        wallet = PaperWallet(100_000)
        risk = engine(wallet, max_open_orders=1)
        from krabs.events import Order

        fake = Order(client_id="x", intent=OrderIntent("AAPL", Side.BUY, qty=1), qty=1)
        decision = check(risk, OrderIntent("AAPL", Side.BUY, qty=1), wallet, [fake])
        self.assertFalse(decision.approved)
        self.assertIn("open orders", decision.reason)

    def test_daily_loss_halts_and_resets(self):
        wallet = PaperWallet(10_000)
        wallet.apply_fill(Fill("o", "AAPL", Side.BUY, 100, 100.0, NOW))
        wallet.mark("AAPL", 90.0)
        risk = engine(wallet, max_daily_loss_pct=0.05)
        reason = risk.poll(NOW, last_event_ts=NOW)
        self.assertTrue(risk.halted)
        self.assertIn("daily loss", reason)
        decision = check(risk, OrderIntent("AAPL", Side.SELL, qty=1), wallet)
        self.assertFalse(decision.approved)
        self.assertTrue(decision.halted)

        wallet.reset_day(NOW)
        risk.reset_day(NOW)
        self.assertFalse(risk.halted)
        wallet.mark("AAPL", 100.0)
        self.assertTrue(check(risk, OrderIntent("AAPL", Side.SELL, qty=1), wallet).approved)

    def test_stale_data_halts(self):
        wallet = PaperWallet(10_000)
        risk = engine(wallet)
        stale_ts = NOW - timedelta(seconds=120)
        reason = risk.poll(NOW, last_event_ts=stale_ts)
        self.assertIn("stale", reason)
        decision = risk.check(
            OrderIntent("AAPL", Side.BUY, qty=1), now=NOW, open_orders=[], last_event_ts=stale_ts
        )
        self.assertFalse(decision.approved)

    def test_kill_switch_halts(self):
        with tempfile.TemporaryDirectory() as tmp:
            kill = KillSwitch(Path(tmp) / "KILL")
            kill.engage("test")
            wallet = PaperWallet(10_000)
            risk = RiskEngine(base_cfg(), wallet, symbols=["AAPL"], kill=kill)
            risk.note_price("AAPL", 100)
            reason = risk.poll(NOW, last_event_ts=NOW)
            self.assertIn("kill switch", reason)
            self.assertFalse(check(risk, OrderIntent("AAPL", Side.BUY, qty=1), wallet).approved)
            kill.clear()
            self.assertFalse(risk.poll(NOW, last_event_ts=NOW))

    def test_allowed_symbols(self):
        wallet = PaperWallet(10_000)
        risk = engine(wallet, allowed_symbols=["SPY"])
        decision = check(risk, OrderIntent("AAPL", Side.BUY, qty=1), wallet)
        self.assertFalse(decision.approved)
        self.assertIn("allowed_symbols", decision.reason)


if __name__ == "__main__":
    unittest.main()
