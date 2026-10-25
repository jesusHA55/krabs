from __future__ import annotations

import unittest
from datetime import datetime, timezone

from krabs.events import Bar, Fill, Order, OrderIntent, Side
from krabs.strategy import build, coerce_params, describe, load_builtin
from krabs.strategy.base import StrategyContext, StrategyError
from krabs.strategy.examples.sma_cross import SmaCross
from krabs.wallet import PaperWallet

TS = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)


def make_ctx(wallet: PaperWallet, bars: list[Bar], intents: list):
    def submit(intent):
        intents.append(intent)
        return None

    return StrategyContext(
        strategy_id="test",
        wallet=wallet,
        submit=submit,
        bars=lambda symbol: [b for b in bars if b.symbol == symbol],
        now=lambda: TS,
    )


class StrategyTests(unittest.TestCase):
    def setUp(self):
        load_builtin()

    def test_registry(self):
        ids = {info.id for info in describe()}
        self.assertIn("sma_cross", ids)
        self.assertIn("buy_hold", ids)
        with self.assertRaises(KeyError):
            build("does_not_exist")

    def test_param_coercion(self):
        params = coerce_params("sma_cross", {"fast": "10", "allocation": "0.3"})
        self.assertEqual(params, {"fast": 10, "allocation": 0.3})
        with self.assertRaises(KeyError):
            coerce_params("sma_cross", {"nope": "1"})

    def test_sma_cross_validation(self):
        with self.assertRaises(ValueError):
            SmaCross(fast=10, slow=5)

    def test_open_order_visibility(self):
        wallet = PaperWallet(10_000)
        order = Order(
            client_id="test-1",
            intent=OrderIntent("AAPL", Side.BUY, qty=1),
            qty=1,
        )
        ctx = make_ctx(wallet, [], [])
        ctx._open_orders = lambda: [order]
        self.assertTrue(ctx.has_open_order("AAPL"))
        self.assertTrue(ctx.has_open_order("aapl"))
        self.assertFalse(ctx.has_open_order("MSFT"))
        self.assertTrue(ctx.has_open_order())
        self.assertEqual(ctx.open_orders(), [order])

    def test_target_weight_buys_and_sells(self):
        wallet = PaperWallet(10_000)
        bars = [Bar("SPY", TS, 100, 101, 99, 100, 1_000)]
        intents = []
        ctx = make_ctx(wallet, bars, intents)

        ctx.target_weight("SPY", 0.5)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].side, Side.BUY)
        self.assertAlmostEqual(intents[0].qty, 50.0)

        wallet.apply_fill(Fill("o", "SPY", Side.BUY, 50, 100, TS))
        ctx.target_weight("SPY", 0.0)
        self.assertEqual(intents[-1].side, Side.SELL)
        self.assertAlmostEqual(intents[-1].qty, 50.0)

    def test_nested_parameter_paths(self):
        strategy = build(
            "pearl_pairs",
            {
                "left": "AAA",
                "right": "BBB",
                "model.window": 30,
                "model.entry": 1.5,
                "model.method": "johansen",
            },
        )
        self.assertEqual(strategy.p("model.window"), 30)
        self.assertEqual(strategy.p("model.entry"), 1.5)
        self.assertEqual(strategy.p("model.method"), "johansen")

        coerced = coerce_params("pearl_pairs", {"model.window": "40"})
        self.assertEqual(coerced, {"model": {"window": 40}})
        with self.assertRaises(ValueError):
            build("pearl_pairs", {"model.nope": 1})

    def test_negative_weight_requires_shorting(self):
        wallet = PaperWallet(10_000)
        ctx = make_ctx(wallet, [Bar("SPY", TS, 100, 101, 99, 100, 1_000)], [])
        with self.assertRaises(StrategyError):
            ctx.target_weight("SPY", -0.2)

    def test_fractional_disabled_rounds_down(self):
        wallet = PaperWallet(10_000)
        bars = [Bar("SPY", TS, 300, 301, 299, 300, 1_000)]
        intents = []
        ctx = make_ctx(wallet, bars, intents)
        ctx.fractional = False
        ctx.target_weight("SPY", 0.5)  # $5,000 / $300 = 16.66 -> 16
        self.assertAlmostEqual(intents[0].qty, 16.0)

    def test_history_field(self):
        wallet = PaperWallet(10_000)
        bars = [Bar("SPY", TS, 100, 110, 90, 105, 1_000)]
        ctx = make_ctx(wallet, bars, [])
        self.assertEqual(ctx.history("SPY"), [105.0])
        self.assertEqual(ctx.history("SPY", field="high"), [110.0])
        with self.assertRaises(StrategyError):
            ctx.history("SPY", field="vwap")


if __name__ == "__main__":
    unittest.main()
