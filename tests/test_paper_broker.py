from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from krabs.brokers.paper import FillModel, PaperBroker
from krabs.clock import SimulatedClock
from krabs.events import Bar, OrderIntent, OrderStatus, OrderType, Side, TimeInForce
from krabs.wallet import PaperWallet

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)


def bar(minute: int, o: float, h: float, l: float, c: float, v: float = 1_000_000) -> Bar:
    return Bar("AAPL", T0 + timedelta(minutes=minute), o, h, l, c, v)


class PaperBrokerTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimulatedClock(T0)
        self.wallet = PaperWallet(100_000)
        self.broker = PaperBroker(
            self.wallet,
            self.clock,
            FillModel(slippage_bps=0, half_spread_bps=0, max_participation=0),
        )

    def test_market_order_fills_on_next_bar_open(self):
        self.broker.on_event(bar(1, 100, 101, 99, 100))
        order = self.broker.submit(OrderIntent("AAPL", Side.BUY, qty=10))
        self.assertEqual(order.status, OrderStatus.NEW)
        self.assertEqual(order.filled_qty, 0.0)

        self.broker.on_event(bar(2, 102, 103, 101, 102.5))
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertAlmostEqual(order.avg_price, 102.0)
        self.assertAlmostEqual(self.wallet.position("AAPL").qty, 10)
        self.assertAlmostEqual(self.wallet.snapshot().cash, 100_000 - 1_020)

    def test_slippage_is_adverse(self):
        self.clock = SimulatedClock(T0)
        wallet = PaperWallet(100_000)
        broker = PaperBroker(
            wallet, self.clock, FillModel(slippage_bps=10, half_spread_bps=0, max_participation=0)
        )
        broker.on_event(bar(1, 100, 101, 99, 100))
        broker.submit(OrderIntent("AAPL", Side.BUY, qty=1))
        broker.on_event(bar(2, 100, 101, 99, 100))
        self.assertAlmostEqual(wallet.position("AAPL").avg_price, 100.10, places=4)

    def test_limit_only_fills_when_crossed(self):
        self.broker.on_event(bar(1, 100, 101, 99, 100))
        order = self.broker.submit(
            OrderIntent("AAPL", Side.BUY, qty=1, type=OrderType.LIMIT, limit_price=99.5)
        )
        self.broker.on_event(bar(2, 101, 102, 100.5, 101))
        self.assertEqual(order.status, OrderStatus.NEW)
        self.broker.on_event(bar(3, 101, 102, 99.0, 100))
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertAlmostEqual(order.avg_price, 99.5)

    def test_limit_gap_fills_at_better_open(self):
        self.broker.on_event(bar(1, 100, 101, 99, 100))
        order = self.broker.submit(
            OrderIntent("AAPL", Side.BUY, qty=1, type=OrderType.LIMIT, limit_price=99.5)
        )
        self.broker.on_event(bar(2, 98, 99, 97, 98.5))
        self.assertAlmostEqual(order.avg_price, 98.0)

    def test_partial_fill_respects_volume(self):
        self.wallet = PaperWallet(1_000_000)
        self.broker = PaperBroker(
            self.wallet,
            self.clock,
            FillModel(slippage_bps=0, half_spread_bps=0, max_participation=0.5),
        )
        self.broker.on_event(bar(1, 100, 101, 99, 100, v=1_000))
        order = self.broker.submit(OrderIntent("AAPL", Side.BUY, qty=100))
        self.broker.on_event(bar(2, 100, 101, 99, 100, v=100))
        self.assertEqual(order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(order.filled_qty, 50)
        self.broker.on_event(bar(3, 100, 101, 99, 100, v=100))
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertAlmostEqual(order.filled_qty, 100)

    def test_ioc_cancels_unfilled_remainder(self):
        broker = PaperBroker(
            self.wallet, self.clock, FillModel(max_participation=0.25), run_id="ioc"
        )
        broker.on_event(bar(1, 100, 101, 99, 100, v=0))
        order = broker.submit(OrderIntent("AAPL", Side.BUY, qty=10, tif=TimeInForce.IOC))
        broker.on_event(bar(2, 100, 101, 99, 100, v=0))
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertEqual(order.filled_qty, 0)

    def test_cancel(self):
        self.broker.on_event(bar(1, 100, 101, 99, 100))
        order = self.broker.submit(OrderIntent("AAPL", Side.BUY, qty=1))
        self.assertTrue(self.broker.cancel(order.client_id))
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.broker.on_event(bar(2, 100, 101, 99, 100))
        self.assertEqual(order.filled_qty, 0)

    def test_rejects_without_price(self):
        order = self.broker.submit(OrderIntent("AAPL", Side.BUY, qty=1))
        self.assertEqual(order.status, OrderStatus.REJECTED)

    def test_rejects_unaffordable_order(self):
        wallet = PaperWallet(1_000)
        broker = PaperBroker(wallet, self.clock, FillModel(max_participation=0))
        broker.on_event(bar(1, 100, 101, 99, 100))
        order = broker.submit(OrderIntent("AAPL", Side.BUY, qty=50))
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertIn("buying power", order.reject_reason)

    def test_short_rejected_when_disabled(self):
        self.broker.on_event(bar(1, 100, 101, 99, 100))
        order = self.broker.submit(OrderIntent("AAPL", Side.SELL, qty=1))
        self.assertEqual(order.status, OrderStatus.REJECTED)

    def test_notional_resolved_at_reference_price(self):
        self.broker.on_event(bar(1, 100, 101, 99, 100))
        order = self.broker.submit(OrderIntent("AAPL", Side.BUY, notional=1_000))
        self.assertAlmostEqual(order.qty, 10)
        self.broker.on_event(bar(2, 100, 101, 99, 100))
        self.assertAlmostEqual(self.wallet.position("AAPL").qty, 10)


if __name__ == "__main__":
    unittest.main()


class StopOrderTests(unittest.TestCase):
    def test_partial_stop_keeps_working(self):
        clock = SimulatedClock(T0)
        wallet = PaperWallet(1_000_000)
        broker = PaperBroker(
            wallet, clock, FillModel(slippage_bps=0, half_spread_bps=0, max_participation=0.5)
        )
        broker.on_event(bar(1, 100, 101, 99, 100, v=1_000))
        order = broker.submit(
            OrderIntent("AAPL", Side.BUY, qty=100, type=OrderType.STOP, stop_price=101)
        )
        broker.on_event(bar(2, 101, 102, 100, 101, v=100))
        self.assertEqual(order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(order.filled_qty, 50)
        broker.on_event(bar(3, 102, 103, 101, 102, v=100))
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertAlmostEqual(order.filled_qty, 100)
