from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from krabs.backtest import BacktestEngine
from krabs.config import Config
from krabs.events import Bar, Side
from krabs.feeds.base import InMemoryFeed, group_bars
from krabs.feeds.synthetic import SyntheticFeed
from krabs.strategy.base import Strategy
from krabs.strategy.examples.buy_hold import BuyHold
from krabs.strategy.examples.sma_cross import SmaCross

T0 = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)


def make_bar(minute: int, o: float, h: float, l: float, c: float, v: float = 100_000) -> Bar:
    return Bar("SPY", T0 + timedelta(minutes=minute), o, h, l, c, v)


class ListFeed:
    def __init__(self, bars):
        self._bars = bars

    def bars(self, symbols, timeframe, start=None, end=None):
        yield from group_bars([b for b in self._bars if b.symbol in symbols], timeframe)


class RecordingStrategy(Strategy):
    id = "recording"
    defaults: dict = {}

    def __init__(self) -> None:
        super().__init__()
        self.submitted_at = None
        self.submitted_close = None
        self.fills = []

    def on_bar(self, ctx, bar):
        if self.submitted_at is None:
            ctx.buy(bar.symbol, qty=1)
            self.submitted_at = bar.ts
            self.submitted_close = bar.close

    def on_fill(self, ctx, fill):
        self.fills.append(fill)


def cfg(**overrides) -> Config:
    raw = {
        "app": {"mode": "backtest", "data_dir": "var"},
        "market": {"symbols": ["SPY"], "timeframe": "1Min"},
        "wallet": {"starting_cash": 10_000.0},
        "execution": {
            "slippage_bps": 0.0,
            "half_spread_bps": 0.0,
            "max_participation": 0.0,
        },
        "risk": {
            "max_order_notional": 100_000.0,
            "max_position_pct": 1.0,
            "max_gross_exposure_pct": 1.0,
            "max_daily_loss_pct": 0.9,
            "max_open_orders": 100,
            "duplicate_window_s": 0.0,
            "max_data_age_s": 0.0,
        },
        "feed": {"provider": "synthetic", "warmup_bars": 50},
        "storage": {"enabled": False},
        "strategy": {"name": "sma_cross", "params": {}},
    }
    raw.update(overrides)
    return Config.from_dict(raw)


class ShortHold(Strategy):
    id = "short_hold"
    defaults: dict = {}

    def __init__(self) -> None:
        super().__init__()
        self.done = False

    def on_bar(self, ctx, bar):
        if not self.done:
            ctx.target_weight(bar.symbol, -0.5)
            self.done = True


class BacktestTests(unittest.TestCase):
    def test_short_borrow_is_charged_daily(self):
        bars = [
            Bar(
                "SPY",
                T0 + timedelta(days=i),
                100.0,
                100.0,
                100.0,
                100.0,
                100_000,
                timeframe="1Day",
            )
            for i in range(12)
        ]
        cfg_borrow = cfg(
            market={"symbols": ["SPY"], "timeframe": "1Day"},
            wallet={"starting_cash": 100_000.0, "allow_short": True},
            execution={
                "slippage_bps": 0.0,
                "half_spread_bps": 0.0,
                "max_participation": 0.0,
                "borrow_bps_per_year": 1000.0,
            },
        )
        result = BacktestEngine(
            cfg_borrow, ShortHold(), InMemoryFeed(bars), run_id="borrow"
        ).run()
        borrow = result.metrics["borrow_cost"]
        self.assertGreater(borrow, 0.0)
        self.assertAlmostEqual(
            result.metrics["final_equity"],
            100_000.0 - borrow,
            places=2,
        )

    def test_per_symbol_borrow_rate(self):
        bars = [
            Bar(
                "SPY",
                T0 + timedelta(days=i),
                100.0,
                100.0,
                100.0,
                100.0,
                100_000,
                timeframe="1Day",
            )
            for i in range(12)
        ]
        borrow_cfg = cfg(
            market={"symbols": ["SPY"], "timeframe": "1Day"},
            wallet={"starting_cash": 100_000.0, "allow_short": True},
            execution={
                "slippage_bps": 0.0,
                "half_spread_bps": 0.0,
                "max_participation": 0.0,
                "borrow_bps_per_year": 0.0,
                "borrow_bps": {"SPY": 1000.0},
            },
        )
        result = BacktestEngine(
            borrow_cfg, ShortHold(), InMemoryFeed(bars), run_id="borrow-symbol"
        ).run()
        self.assertGreater(result.metrics["borrow_cost"], 0.0)

    def test_warmup_prefills_history_without_trading(self):
        bars = []
        for i in range(150):
            price = 100.0 + i * 0.1
            bars.append(
                Bar("SPY", T0 + timedelta(minutes=i), price, price, price, price, 100_000)
            )
        start, end = bars[100].ts, bars[108].ts

        warm = BacktestEngine(
            cfg(), SmaCross(fast=5, slow=10, allocation=0.25), InMemoryFeed(bars), run_id="warm"
        ).run(start=start, end=end, warmup_bars=30)
        self.assertTrue(warm.fills)
        self.assertEqual(warm.fills[0].ts, bars[101].ts)
        self.assertEqual(warm.equity_curve[0].ts, start)

        cold = BacktestEngine(
            cfg(), SmaCross(fast=5, slow=10, allocation=0.25), InMemoryFeed(bars), run_id="cold"
        ).run(start=start, end=end)
        self.assertEqual(len(cold.fills), 0)

    def test_no_lookahead_fills_at_next_open(self):
        bars = [
            make_bar(1, 100, 101, 99, 100),
            make_bar(2, 102, 103, 101, 102.5),
            make_bar(3, 104, 105, 103, 104.5),
        ]
        feed = ListFeed(bars)
        strategy = RecordingStrategy()
        result = BacktestEngine(cfg(), strategy, feed, run_id="test").run()

        self.assertEqual(len(strategy.fills), 1)
        fill = strategy.fills[0]
        self.assertEqual(fill.ts, bars[1].ts)
        self.assertAlmostEqual(fill.price, bars[1].open)
        self.assertEqual(fill.side, Side.BUY)
        self.assertEqual(len(result.equity_curve), 3)

    def test_buy_hold_tracks_market(self):
        bars = [make_bar(i, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(1, 6)]
        result = BacktestEngine(
            cfg(), BuyHold(allocation=0.5), ListFeed(bars), run_id="bh"
        ).run()
        self.assertEqual(len(result.fills), 1)
        self.assertGreater(result.final_equity, 10_000)
        self.assertEqual(result.rejections, [])

    def test_deterministic(self):
        config = cfg(market={"symbols": ["SPY", "QQQ"], "timeframe": "1Min"})
        results = []
        for _ in range(2):
            feed = SyntheticFeed(days=5, seed=123)
            engine = BacktestEngine(config, SmaCross(fast=5, slow=15, allocation=0.2), feed)
            results.append(engine.run())
        self.assertEqual(results[0].final_equity, results[1].final_equity)
        self.assertEqual(len(results[0].fills), len(results[1].fills))
        self.assertEqual(
            [p.equity for p in results[0].equity_curve],
            [p.equity for p in results[1].equity_curve],
        )

    def test_sma_cross_trades_and_reports(self):
        config = cfg(market={"symbols": ["SPY", "QQQ"], "timeframe": "5Min"})
        result = BacktestEngine(
            config, SmaCross(fast=5, slow=20, allocation=0.2), SyntheticFeed(days=10, seed=3)
        ).run(start=None, end=None)
        self.assertGreater(len(result.equity_curve), 0)
        self.assertGreater(len(result.fills), 0)
        self.assertIn("sharpe", result.metrics)
        self.assertLessEqual(result.metrics["max_drawdown"], 0.0)


if __name__ == "__main__":
    unittest.main()
