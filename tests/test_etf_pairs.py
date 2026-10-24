from __future__ import annotations

import math
import random
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from krabs.backtest import BacktestEngine
from krabs.config import Config
from krabs.events import Bar
from krabs.feeds.base import group_bars
from krabs.pearl import Observation
from krabs.pearl.screening import PairCandidate
from krabs.strategy.examples.etf_pairs import ActivePair, ETFPairs

START = datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc)


def make_bar(symbol: str, i: int, close: float) -> Bar:
    return Bar(
        symbol,
        START + timedelta(days=i),
        close,
        close * 1.002,
        close * 0.998,
        close,
        1_000_000,
        timeframe="1Day",
    )


class Feed:
    def __init__(self, bars):
        self._bars = bars

    def bars(self, symbols, timeframe, start=None, end=None):
        yield from group_bars([b for b in self._bars if b.symbol in symbols], timeframe)


class ETFPairsTests(unittest.TestCase):
    def test_spreads_csv_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spreads.csv"
            path.write_text(
                "symbol,samples,median_spread_bps,mean_spread_bps\n"
                "AAA,10,1.25,1.4\nBBB,10,2.50,2.7\n",
                encoding="utf-8",
            )
            strategy = ETFPairs(
                clusters={"test": ["AAA", "BBB"]},
                driver="AAA",
                spreads_csv=str(path),
            )
            self.assertAlmostEqual(strategy.spread_bps["AAA"], 1.25)
            self.assertAlmostEqual(strategy.spread_bps["BBB"], 2.5)
            self.assertIn("csv:", strategy.spreads_source)

    def test_missing_spreads_csv_falls_back_with_warning(self):
        with self.assertLogs("krabs.strategy.etf_pairs", level="WARNING"):
            strategy = ETFPairs(
                clusters={"test": ["AAA", "BBB"]},
                driver="AAA",
                spreads_csv="/nonexistent/spreads.csv",
                spread_bps={"AAA": 3.0},
            )
        self.assertAlmostEqual(strategy.spread_bps["AAA"], 3.0)
        self.assertEqual(strategy.spreads_source, "params")

    def test_time_stop_flattens_after_max_hold_bars(self):
        rng = random.Random(11)
        closes = []
        x = 100.0
        for _ in range(80):
            x *= math.exp(rng.gauss(0.0002, 0.004))
            closes.append(x)
        bars = {
            symbol: [make_bar(symbol, i, price) for i, price in enumerate(closes)]
            for symbol in ("AAA", "BBB")
        }

        targets: list[tuple[str, float]] = []

        class Ctx:
            def bar(self, symbol):
                return bars[symbol][-1]

            def position(self, symbol):
                return SimpleNamespace(is_flat=False)

            def target_weight(self, symbol, weight, reason=None):
                targets.append((symbol, round(weight, 6)))
                return object()

        strategy = ETFPairs(
            clusters={"test": ["AAA", "BBB"]},
            driver="AAA",
            max_hold_bars=5,
            kf_warmup=5,
        )
        candidate = PairCandidate(
            left="AAA",
            right="BBB",
            cluster="test",
            beta=1.0,
            half_life=5.0,
            crossings_per_year=20.0,
            adf_t=-3.0,
            rank=1,
            trace0=20.0,
            friction_bps=2.0,
            score=1.0,
        )
        model = strategy._build_model(candidate)
        for i in range(10):
            model.update(Observation.from_bars([bars["AAA"][i], bars["BBB"][i]]))
        active = ActivePair(candidate=candidate, model=model, side=1)
        active.opened_at_bar = 0
        strategy._active = {"test": active}
        strategy._bar_index = 5

        strategy._update_active(Ctx())

        self.assertEqual(active.side, 0)
        self.assertIn(("AAA", 0.0), targets)
        self.assertIn(("BBB", -0.0), targets)

    def test_screens_warms_kf_and_trades_both_legs(self):
        rng = random.Random(5)
        x = 100.0
        spread = 0.0
        bars: list[Bar] = []
        for i in range(400):
            x *= math.exp(rng.gauss(0.0001, 0.006))
            spread = 0.8 * spread + rng.gauss(0.0, 0.05)
            bars.extend([make_bar("AAA", i, x), make_bar("BBB", i, 2.0 * x * math.exp(spread))])

        cfg = Config.from_dict(
            {
                "app": {"mode": "backtest"},
                "market": {"symbols": ["AAA", "BBB"], "timeframe": "1Day"},
                "wallet": {"starting_cash": 100_000.0, "allow_short": True},
                "execution": {"max_participation": 0.0},
                "risk": {
                    "max_order_notional": 100_000.0,
                    "max_position_pct": 0.5,
                    "max_gross_exposure_pct": 0.9,
                    "max_daily_loss_pct": 0.2,
                    "duplicate_window_s": 0.0,
                    "max_data_age_s": 0.0,
                },
                "feed": {"provider": "synthetic"},
                "storage": {"enabled": False},
                "strategy": {"name": "etf_pairs", "params": {}},
            }
        )
        strategy = ETFPairs(
            clusters={"test": ["AAA", "BBB"]},
            driver="AAA",
            formation_days=150,
            rescreen_days=20,
            min_half_life=1,
            max_half_life=20,
            min_crossings_per_year=5,
            max_bid_ask_bps=5,
            default_spread_bps=2,
            entry_z=1.5,
            friction_multiple=0,
            allocation=0.2,
            max_pairs=1,
            kf_warmup=10,
        )
        result = BacktestEngine(cfg, strategy, Feed(bars), run_id="etf-test").run()

        self.assertGreater(len(result.fills), 0)
        self.assertEqual({fill.symbol for fill in result.fills}, {"AAA", "BBB"})
        diagnostics = strategy.diagnostics()
        self.assertIn("selected_pairs", diagnostics)
        self.assertGreaterEqual(len(diagnostics["selected_pairs"]), 1)

    def test_model_variants_trade(self):
        rng = random.Random(5)
        x, spread = 100.0, 0.0
        bars: list[Bar] = []
        for i in range(400):
            x *= math.exp(rng.gauss(0.0001, 0.006))
            spread = 0.8 * spread + rng.gauss(0.0, 0.05)
            bars.extend(
                [make_bar("AAA", i, x), make_bar("BBB", i, 2.0 * x * math.exp(spread))]
            )
        cfg = Config.from_dict(
            {
                "app": {"mode": "backtest"},
                "market": {"symbols": ["AAA", "BBB"], "timeframe": "1Day"},
                "wallet": {"starting_cash": 100_000.0, "allow_short": True},
                "execution": {"max_participation": 0.0},
                "risk": {
                    "max_order_notional": 100_000.0,
                    "max_position_pct": 0.5,
                    "max_gross_exposure_pct": 0.9,
                    "max_daily_loss_pct": 0.2,
                    "duplicate_window_s": 0.0,
                    "max_data_age_s": 0.0,
                },
                "feed": {"provider": "synthetic"},
                "storage": {"enabled": False},
                "strategy": {"name": "etf_pairs", "params": {}},
            }
        )
        variants = [
            ("local_level", {}),
            ("ou", {"ou_window": 60, "ou_refit": 10}),
            ("ou_dynamic", {"ou_window": 60, "ou_refit": 10, "beta_var": 1e-7}),
        ]
        for model, extra in variants:
            with self.subTest(model=model):
                strategy = ETFPairs(
                    clusters={"test": ["AAA", "BBB"]},
                    driver="AAA",
                    formation_days=150,
                    rescreen_days=20,
                    min_half_life=1,
                    max_half_life=20,
                    min_crossings_per_year=5,
                    max_bid_ask_bps=5,
                    default_spread_bps=2,
                    entry_z=1.5,
                    friction_multiple=0,
                    allocation=0.2,
                    max_pairs=1,
                    kf_warmup=10,
                    model=model,
                    **extra,
                )
                result = BacktestEngine(cfg, strategy, Feed(bars), run_id=model).run()
                self.assertGreater(len(result.fills), 0)
                self.assertEqual(strategy.diagnostics()["model"], model)

    def test_model_validation(self):
        common = {"clusters": {"test": ["AAA", "BBB"]}, "driver": "AAA"}
        with self.assertRaises(ValueError):
            ETFPairs(**common, model="ekf")
        with self.assertRaises(ValueError):
            ETFPairs(**common, model="ou", formation_days=150, ou_window=200)


if __name__ == "__main__":
    unittest.main()
