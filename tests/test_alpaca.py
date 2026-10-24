from __future__ import annotations

import unittest
from datetime import datetime, timezone

from krabs.brokers.alpaca import AlpacaBroker, BrokerRejected
from krabs.brokers.base import client_order_id
from krabs.events import OrderIntent, OrderStatus, OrderType, Side
from krabs.feeds.alpaca import AlpacaFeed


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, method, path, *, params=None, body=None):
        self.calls.append((method, path, params, body))
        response = self.responses.get((method, path))
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(params, body)
        return response


def order_payload(**overrides):
    payload = {
        "id": "broker-1",
        "client_order_id": "krabs-abc-000001",
        "symbol": "SPY",
        "side": "buy",
        "qty": "10",
        "type": "market",
        "time_in_force": "day",
        "status": "accepted",
        "filled_qty": "0",
        "filled_avg_price": None,
        "submitted_at": "2024-01-02T15:00:00Z",
        "updated_at": "2024-01-02T15:00:01Z",
    }
    payload.update(overrides)
    return payload


class AlpacaBrokerTests(unittest.TestCase):
    def test_submit_sends_expected_payload(self):
        def respond(params, body):
            return {
                **order_payload(),
                "client_order_id": body["client_order_id"],
                "qty": body["qty"],
            }

        client = FakeClient({("POST", "/v2/orders"): respond})
        broker = AlpacaBroker(client, run_id="run-1234", price_fn=lambda s: 400.0)
        order = broker.submit(OrderIntent("SPY", Side.BUY, notional=4_000, reason="test"))
        self.assertEqual(order.status, OrderStatus.NEW)
        self.assertEqual(order.broker_id, "broker-1")
        self.assertAlmostEqual(order.qty, 10.0)
        method, path, _, body = client.calls[0]
        self.assertEqual((method, path), ("POST", "/v2/orders"))
        self.assertEqual(body["symbol"], "SPY")
        self.assertEqual(body["side"], "buy")
        self.assertEqual(body["qty"], "10")
        self.assertEqual(body["type"], "market")
        cid = body["client_order_id"]
        self.assertTrue(cid.startswith("krabs-"))
        self.assertTrue(cid.endswith("000001"))
        self.assertLessEqual(len(cid), 48)

    def test_limit_order_includes_prices(self):
        client = FakeClient({("POST", "/v2/orders"): order_payload()})
        broker = AlpacaBroker(client, run_id="r", price_fn=lambda s: 400.0)
        broker.submit(OrderIntent("SPY", Side.SELL, qty=1, type=OrderType.LIMIT, limit_price=410))
        body = client.calls[0][3]
        self.assertEqual(body["limit_price"], "410")

    def test_4xx_becomes_rejected_order(self):
        client = FakeClient({("POST", "/v2/orders"): BrokerRejected("insufficient qty")})
        broker = AlpacaBroker(client, run_id="r", price_fn=lambda s: 1.0)
        order = broker.submit(OrderIntent("SPY", Side.BUY, qty=1))
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertIn("insufficient qty", order.reject_reason)

    def test_account_mapping(self):
        payload = {
            "cash": "1000.50",
            "equity": "2500.75",
            "buying_power": "2000",
            "long_market_value": "1500.25",
            "short_market_value": "0",
            "last_equity": "2400",
            "currency": "USD",
        }
        client = FakeClient({("GET", "/v2/account"): payload})
        broker = AlpacaBroker(client, run_id="r", price_fn=lambda s: 1.0)
        account = broker.account()
        self.assertAlmostEqual(account.cash, 1000.50)
        self.assertAlmostEqual(account.equity, 2500.75)
        self.assertAlmostEqual(account.day_start_equity, 2400.0)
        self.assertAlmostEqual(account.long_value, 1500.25)

    def test_positions_mapping(self):
        payload = [
            {"symbol": "SPY", "qty": "3", "avg_entry_price": "400", "current_price": "410"},
            {"symbol": "QQQ", "qty": "-2", "avg_entry_price": "300", "current_price": "290"},
        ]
        client = FakeClient({("GET", "/v2/positions"): payload})
        broker = AlpacaBroker(client, run_id="r", price_fn=lambda s: 1.0)
        positions = broker.positions()
        self.assertAlmostEqual(positions["SPY"].market_value, 1230.0)
        self.assertAlmostEqual(positions["QQQ"].unrealized_pnl, 20.0)

    def test_open_orders_parse_status(self):
        client = FakeClient(
            {("GET", "/v2/orders"): [order_payload(status="partially_filled", filled_qty="4")]}
        )
        broker = AlpacaBroker(client, run_id="r", price_fn=lambda s: 1.0)
        orders = broker.open_orders()
        self.assertEqual(orders[0].status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(orders[0].filled_qty, 4.0)

    def test_client_order_id_is_safe_short_and_unique_per_run(self):
        cid = client_order_id("live-20260101-120000-abcd", 42)
        self.assertLessEqual(len(cid), 48)
        self.assertTrue(all(c.isalnum() or c in "-_" for c in cid))
        self.assertTrue(cid.endswith("000042"))
        self.assertEqual(cid, client_order_id("live-20260101-120000-abcd", 42))
        self.assertNotEqual(cid, client_order_id("live-20260101-120001-abcd", 42))


def raw_bar(t: str, **overrides):
    row = {"t": t, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1234}
    row.update(overrides)
    return row


class AlpacaFeedTests(unittest.TestCase):
    def test_history_paginates_and_converts_to_close_time(self):
        pages = {
            None: {"bars": {"SPY": [raw_bar("2024-01-02T14:31:00Z")]}, "next_page_token": "p2"},
            "p2": {"bars": {"SPY": [raw_bar("2024-01-02T14:32:00Z")]}},
        }

        def handler(params, body):
            return pages[params.get("page_token")]

        client = FakeClient({("GET", "/v2/stocks/bars"): handler})
        feed = AlpacaFeed(client, feed="iex")
        bars = feed.history(
            ["SPY"], "1Min", datetime(2024, 1, 2, 14, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].ts, datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc))
        self.assertEqual(bars[1].ts, datetime(2024, 1, 2, 14, 33, tzinfo=timezone.utc))
        self.assertEqual(bars[0].timeframe, "1Min")

    def test_daily_bar_uses_session_close(self):
        client = FakeClient(
            {
                ("GET", "/v2/stocks/bars"): {
                    "bars": {"SPY": [raw_bar("2024-01-02T05:00:00Z")]},
                }
            }
        )
        feed = AlpacaFeed(client)
        bars = feed.history(["SPY"], "1Day", datetime(2024, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(bars[0].ts, datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc))

    def test_empty_symbols_short_circuits(self):
        client = FakeClient({})
        feed = AlpacaFeed(client)
        self.assertEqual(
            feed.history([], "1Min", datetime(2024, 1, 1, tzinfo=timezone.utc)), []
        )
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
