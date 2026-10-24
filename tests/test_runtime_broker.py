from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from krabs.brokers.alpaca import AlpacaBroker
from krabs.config import Config
from krabs.events import Bar
from krabs.risk import KillSwitch, RiskEngine
from krabs.runtime import TradingRuntime
from krabs.strategy.examples.buy_hold import BuyHold
from krabs.wallet import BrokerWallet


class FakeAlpacaClient:
    """Just enough Alpaca to exercise the broker-backed runtime path."""

    def __init__(self, state):
        self.state = state

    def request(self, method, path, *, params=None, body=None):
        params = params or {}
        if method == "POST" and path == "/v2/orders":
            payload = {
                "id": f"broker-{len(self.state['orders']) + 1}",
                "client_order_id": body["client_order_id"],
                "symbol": body["symbol"],
                "side": body["side"],
                "qty": body["qty"],
                "type": body["type"],
                "time_in_force": body["time_in_force"],
                "status": "filled",
                "filled_qty": body["qty"],
                "filled_avg_price": "400.50",
                "submitted_at": "2024-01-02T15:00:00Z",
                "updated_at": "2024-01-02T15:00:05Z",
            }
            self.state["orders"].append(payload)
            return payload
        if method == "GET" and path == "/v2/orders":
            return list(self.state["orders"])
        if method == "GET" and path == "/v2/account":
            return {
                "cash": "10000",
                "equity": "10000",
                "buying_power": "10000",
                "long_market_value": "0",
                "short_market_value": "0",
                "last_equity": "10000",
                "currency": "USD",
            }
        if method == "GET" and path == "/v2/positions":
            if not self.state["orders"]:
                return []
            return [
                {
                    "symbol": "SPY",
                    "qty": "12.5",
                    "avg_entry_price": "400.50",
                    "current_price": "401",
                }
            ]
        if method == "DELETE" and path in ("/v2/orders", "/v2/positions"):
            self.state["orders"].clear()
            return None
        raise AssertionError(f"unexpected request: {method} {path} {params}")


class FakeFeed:
    def __init__(self, bars):
        self.bars = bars

    def history(self, symbols, timeframe, start, end=None):
        return self.bars[:2]

    def latest_bars(self, symbols, timeframe, lookback=10):
        return self.bars


class BrokerRuntimeTests(unittest.TestCase):
    def test_broker_backed_cycle_detects_fill(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        bars = [
            Bar("SPY", now - timedelta(minutes=5 - i), 400 + i, 401 + i, 399 + i, 400 + i, 1e6)
            for i in range(5)
        ]
        state = {"orders": []}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_dict(
                {
                    "app": {"mode": "broker-paper", "data_dir": tmp},
                    "market": {
                        "symbols": ["SPY"],
                        "timeframe": "1Min",
                        "enforce_market_hours": False,
                    },
                    "wallet": {"starting_cash": 10_000.0, "allow_short": False},
                    "risk": {
                        "max_order_notional": 100_000.0,
                        "max_position_pct": 1.0,
                        "max_gross_exposure_pct": 1.0,
                        "max_daily_loss_pct": 0.5,
                        "max_open_orders": 10,
                        "max_data_age_s": 600.0,
                        "duplicate_window_s": 0.0,
                    },
                    "feed": {"provider": "alpaca", "poll_seconds": 0.01},
                    "storage": {"enabled": False},
                }
            )
            wallet = BrokerWallet(10_000)
            kill = KillSwitch(Path(tmp) / "KILL")
            risk = RiskEngine(cfg.risk, wallet, symbols=["SPY"], kill=kill)
            broker = AlpacaBroker(
                FakeAlpacaClient(state),
                run_id="broker-runtime-test",
                price_fn=lambda symbol: risk.prices().get(symbol, 0.0),
            )
            runtime = TradingRuntime(
                cfg,
                BuyHold(allocation=0.5),
                FakeFeed(bars),
                broker,
                wallet,
                risk,
                run_id="broker-runtime-test",
                mode="broker-paper",
                kill=kill,
            )
            stats = asyncio.run(runtime.run(max_cycles=3))

            self.assertEqual(stats.bars, 3)
            self.assertEqual(stats.orders, 1)
            self.assertEqual(stats.fills, 1)
            self.assertAlmostEqual(wallet.position("SPY").qty, 12.5)
            # Shutdown cancels working orders (the fake had one "filled" order,
            # so cleanup emptied the list: that is the intended behaviour).


if __name__ == "__main__":
    unittest.main()
