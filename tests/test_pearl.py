from __future__ import annotations

import math
import random
import unittest
from datetime import datetime, timedelta, timezone

from krabs.backtest import BacktestEngine
from krabs.config import Config
from krabs.events import Bar
from krabs.feeds.base import group_bars
from krabs.pearl import (
    CUSUM,
    HaarTrend,
    Johansen,
    RollingPCA,
    pairs_model,
    trend_model,
    GARCH11,
    HPFilter,
    Hurst,
    Kalman,
    LogReturn,
    ModelBuilder,
    Observation,
    PairSpread,
    Series,
    Threshold,
    ZScore,
)
from krabs.pearl import linalg, stats
from krabs.strategy.examples.pearl_pairs import PearlPairs

START = datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc)


def make_bar(symbol: str, i: int, close: float, volume: float = 1_000_000) -> Bar:
    return Bar(
        symbol,
        START + timedelta(days=i),
        close,
        close * 1.002,
        close * 0.998,
        close,
        volume,
        timeframe="1Day",
    )


def obs_for(symbol: str, values: list[float]) -> list[Observation]:
    return [Observation.from_bar(make_bar(symbol, i, value)) for i, value in enumerate(values)]


def pair_bars(count: int = 260) -> list[Bar]:
    rng = random.Random(7)
    bars: list[Bar] = []
    for i in range(count):
        x = 100.0 + 0.12 * i + 1.5 * math.sin(i / 25.0)
        spread = rng.gauss(0.0, 1.5)
        y = 2.0 * x + spread
        bars.append(make_bar("AAA", i, x))
        bars.append(make_bar("BBB", i, y))
    return bars


class ObservationTests(unittest.TestCase):
    def test_from_bars_requires_same_timestamp(self):
        a = make_bar("AAA", 0, 10)
        b = make_bar("BBB", 1, 20)
        with self.assertRaises(ValueError):
            Observation.from_bars([a, b])

    def test_fields(self):
        obs = Observation.from_bar(make_bar("AAA", 0, 12.5))
        self.assertEqual(obs.close("AAA"), 12.5)
        self.assertEqual(obs.field("AAA", "high"), 12.5 * 1.002)


class BuilderTests(unittest.TestCase):
    def test_unknown_input_is_rejected(self):
        with self.assertRaises(ValueError):
            ModelBuilder().add("ret", LogReturn("missing"))

    def test_duplicate_name_is_rejected(self):
        builder = ModelBuilder().add("x", Series("AAA"))
        with self.assertRaises(ValueError):
            builder.add("x", Series("BBB"))

    def test_model_describe(self):
        model = (
            ModelBuilder()
            .add("px", Series("AAA"))
            .add("ret", LogReturn("px"))
            .build()
        )
        described = model.describe()
        self.assertEqual(described["nodes"][1]["type"], "log_return")


class FeatureTests(unittest.TestCase):
    def test_log_return_and_zscore(self):
        model = (
            ModelBuilder()
            .add("px", Series("AAA"))
            .add("ret", LogReturn("px"))
            .add("z", ZScore("ret", window=5))
            .build()
        )
        observations = obs_for("AAA", [100, 101, 102, 103, 104, 105, 106])
        snaps = model.transform(observations)
        self.assertAlmostEqual(snaps[-1].value("ret.value"), math.log(106 / 105))
        self.assertTrue(snaps[-1].ready)
        self.assertIsNotNone(snaps[-1].value("z.value"))

    def test_batch_and_streaming_are_identical(self):
        model = (
            ModelBuilder()
            .add("px", Series("AAA"))
            .add("ret", LogReturn("px"))
            .add("z", ZScore("ret", window=5))
            .build()
        )
        observations = obs_for("AAA", [100 + i + (i % 3) for i in range(30)])
        batch = model.transform(observations)

        stream_model = (
            ModelBuilder()
            .add("px", Series("AAA"))
            .add("ret", LogReturn("px"))
            .add("z", ZScore("ret", window=5))
            .build()
        )
        stream = [stream_model.update(obs) for obs in observations]
        for a, b in zip(batch, stream):
            self.assertEqual(a.ready, b.ready)
            for node in ("px", "ret", "z"):
                for field in ("value",):
                    self.assertAlmostEqual(
                        a.value(f"{node}.{field}"), b.value(f"{node}.{field}"), places=10
                    )

    def test_future_data_cannot_change_past_snapshots(self):
        def run(values):
            model = (
                ModelBuilder()
                .add("px", Series("AAA"))
                .add("ret", LogReturn("px"))
                .add("k", Kalman("ret", model="local_level", warmup=3))
                .build()
            )
            return [model.update(obs) for obs in obs_for("AAA", values)]

        values = [100 + i + (i % 4) * 0.3 for i in range(40)]
        early = run(values[:20])
        full = run(values)[:20]
        for a, b in zip(early, full):
            self.assertAlmostEqual(a.value("k.level"), b.value("k.level"), places=12)


class KalmanTests(unittest.TestCase):
    def test_local_level_converges(self):
        model = (
            ModelBuilder()
            .add("px", Series("AAA"))
            .add(
                "k",
                Kalman("px", model="local_level", level_var=1e-4, obs_var=0.01, warmup=5),
            )
            .build()
        )
        snaps = model.transform(obs_for("AAA", [10.0] * 40))
        self.assertTrue(snaps[-1].ready)
        self.assertAlmostEqual(snaps[-1].value("k.level"), 10.0, places=3)
        self.assertLess(snaps[-1].value("k.state_var"), 0.01)

    def test_local_trend_recovers_slope(self):
        model = (
            ModelBuilder()
            .add("px", Series("AAA"))
            .add(
                "k",
                Kalman(
                    "px",
                    model="local_trend",
                    level_var=1e-2,
                    slope_var=1e-2,
                    obs_var=1e-4,
                    warmup=10,
                ),
            )
            .build()
        )
        snaps = model.transform(obs_for("AAA", [100.0 + i for i in range(120)]))
        self.assertAlmostEqual(snaps[-1].value("k.slope"), 1.0, places=1)
        self.assertAlmostEqual(snaps[-1].value("k.level"), 219.0, delta=2.0)

    def test_regression_filter_recovers_coefficients(self):
        bars: list[Bar] = []
        for i in range(200):
            x = 1.0 + 0.01 * i
            y = 1.0 + 2.0 * x
            bars.extend([make_bar("AAA", i, x), make_bar("BBB", i, y)])
        model = (
            ModelBuilder()
            .add("x", Series("AAA"))
            .add("y", Series("BBB"))
            .add(
                "k",
                Kalman(
                    "y",
                    model="regression",
                    features=["x"],
                    coeff_var=1e-3,
                    obs_var=0.01,
                    warmup=10,
                ),
            )
            .build()
        )
        snap = None
        for i in range(0, len(bars), 2):
            snap = model.update(Observation.from_bars(bars[i : i + 2]))
        self.assertAlmostEqual(snap.value("k.coef0"), 1.0, delta=0.1)
        self.assertAlmostEqual(snap.value("k.coef1"), 2.0, delta=0.1)

    def test_ar1_filter_runs(self):
        model = (
            ModelBuilder()
            .add("px", Series("AAA"))
            .add("k", Kalman("px", model="ar1", phi=0.8, level_var=1e-3, warmup=3))
            .build()
        )
        values = [10.0]
        for i in range(1, 80):
            values.append(8.0 + 0.8 * (values[-1] - 8.0))
        snaps = model.transform(obs_for("AAA", values))
        self.assertTrue(snaps[-1].ready)
        self.assertTrue(math.isfinite(snaps[-1].value("k.level")))

    def test_state_round_trip(self):
        def build():
            return (
                ModelBuilder()
                .add("px", Series("AAA"))
                .add("k", Kalman("px", model="local_level", warmup=2))
                .build()
            )

        first = build()
        observations = obs_for("AAA", [10, 11, 12, 13, 14])
        for obs in observations:
            first.update(obs)
        state = first.state()

        second = build()
        second.load_state(state)
        next_obs = Observation.from_bar(make_bar("AAA", 5, 15))
        a = first.update(next_obs)
        b = second.update(next_obs)
        self.assertAlmostEqual(a.value("k.level"), b.value("k.level"), places=12)
        self.assertAlmostEqual(a.value("k.state_var"), b.value("k.state_var"), places=12)


class PresetTests(unittest.TestCase):
    def test_presets_build(self):
        self.assertEqual(len(pairs_model("AAA", "BBB").nodes), 4)
        self.assertEqual(len(trend_model("AAA").nodes), 3)


class PairTests(unittest.TestCase):
    def test_pair_spread_recovers_hedge_ratio(self):
        model = (
            ModelBuilder()
            .add("left", Series("BBB"))
            .add("right", Series("AAA"))
            .add(
                "spread",
                PairSpread("left", "right", window=60, min_obs=30, use_log=False),
            )
            .add("entry", Threshold("spread.z", entry=1.5, exit=0.3))
            .build()
        )
        bars = pair_bars(180)
        snapshots = []
        for chunk in group_bars([b for b in bars if b.symbol in ("AAA", "BBB")], "1Day"):
            snapshots.append(model.update(Observation.from_bars(chunk)))
        last = snapshots[-1]
        self.assertTrue(last.ready)
        self.assertAlmostEqual(last.value("spread.beta"), 2.0, delta=0.35)
        self.assertIsNotNone(last.value("spread.z"))
        self.assertGreaterEqual(last.value("spread.half_life"), 0.0)

    def test_johansen_pair_cointegration(self):
        rng = random.Random(19)
        x = 100.0
        bars: list[Bar] = []
        for i in range(400):
            x *= math.exp(rng.gauss(0.0002, 0.008))
            y = 2.0 * x + rng.gauss(0.0, 0.8)
            bars.extend([make_bar("AAA", i, x), make_bar("BBB", i, y)])
        model = (
            ModelBuilder()
            .add("x", Series("AAA"))
            .add("y", Series("BBB"))
            .add(
                "johansen",
                Johansen(["y", "x"], window=160, min_obs=60, use_log=False),
            )
            .build()
        )
        snap = None
        for i in range(0, len(bars), 2):
            snap = model.update(Observation.from_bars(bars[i : i + 2]))
        self.assertTrue(snap.ready)
        self.assertEqual(snap.value("johansen.rank"), 1)
        self.assertAlmostEqual(snap.value("johansen.beta"), 2.0, delta=0.15)
        self.assertGreater(snap.value("johansen.trace0"), snap.value("johansen.trace1"))

    def test_pair_state_round_trip(self):
        def build():
            return (
                ModelBuilder()
                .add("left", Series("BBB"))
                .add("right", Series("AAA"))
                .add("spread", PairSpread("left", "right", window=30, min_obs=15, use_log=False))
                .build()
            )

        first = build()
        for chunk in group_bars(pair_bars(80), "1Day"):
            first.update(Observation.from_bars(chunk))
        second = build()
        second.load_state(first.state())
        next_chunk = group_bars([make_bar("AAA", 80, 110), make_bar("BBB", 80, 225)], "1Day")
        obs = Observation.from_bars(next(iter(next_chunk)))
        a = first.update(obs)
        b = second.update(obs)
        self.assertAlmostEqual(a.value("spread.beta"), b.value("spread.beta"), places=12)
        self.assertAlmostEqual(a.value("spread.z"), b.value("spread.z"), places=12)


class StatisticsTests(unittest.TestCase):
    def test_hurst_on_returns(self):
        rng = random.Random(11)
        returns = [rng.gauss(0, 0.01) for _ in range(1024)]
        value = stats.hurst_rs(returns)
        self.assertGreater(value, 0.35)
        self.assertLess(value, 0.75)

    def test_hp_filter_removes_linear_trend(self):
        values = [100.0 + i for i in range(64)]
        trend = stats.hp_trend(values, lam=1600)
        self.assertAlmostEqual(trend[-1], values[-1], delta=1e-2)

    def test_cusum_detects_shift(self):
        model = (
            ModelBuilder()
            .add("px", Series("AAA"))
            .add("c", CUSUM("px", threshold=5, drift=0))
            .build()
        )
        snaps = model.transform(obs_for("AAA", [2.0] * 5))
        self.assertTrue(any(snapshot.value("c.alarm") == 1 for snapshot in snaps))

    def test_hurst_node(self):
        rng = random.Random(2)
        prices = [100.0]
        for _ in range(300):
            prices.append(prices[-1] * math.exp(rng.gauss(0, 0.01)))
        model = (
            ModelBuilder()
            .add("px", Series("AAA"))
            .add("ret", LogReturn("px"))
            .add("h", Hurst("ret", window=200))
            .build()
        )
        snaps = model.transform(obs_for("AAA", prices))
        self.assertTrue(snaps[-1].ready)
        self.assertGreater(snaps[-1].value("h.value"), 0.3)
        self.assertLess(snaps[-1].value("h.value"), 0.8)

    def test_hp_node(self):
        model = (
            ModelBuilder()
            .add("px", Series("AAA"))
            .add("hp", HPFilter("px", window=32, lam=1600))
            .build()
        )
        snaps = model.transform(obs_for("AAA", [100.0 + i for i in range(40)]))
        self.assertTrue(snaps[-1].ready)
        self.assertAlmostEqual(snaps[-1].value("hp.trend"), 139.0, delta=1.0)

    def test_haar_trend(self):
        model = (
            ModelBuilder()
            .add("px", Series("AAA"))
            .add("h", HaarTrend("px", level=3))
            .build()
        )
        snaps = model.transform(obs_for("AAA", [100.0 + i for i in range(20)]))
        self.assertTrue(snaps[-1].ready)
        self.assertAlmostEqual(snaps[-1].value("h.approximation"), 115.5, delta=2.0)
        self.assertGreater(snaps[-1].value("h.detail_energy"), 0.0)

    def test_rolling_pca_explains_correlated_pair(self):
        bars: list[Bar] = []
        for i in range(120):
            x = 100.0 + i * 0.1 + math.sin(i / 5.0)
            y = 2.0 * x + 0.5 * math.sin(i / 3.0)
            bars.extend([make_bar("AAA", i, x), make_bar("BBB", i, y)])
        model = (
            ModelBuilder()
            .add("a", Series("AAA"))
            .add("b", Series("BBB"))
            .add("pca", RollingPCA(["a", "b"], window=60, min_obs=30))
            .build()
        )
        snap = None
        for i in range(0, len(bars), 2):
            snap = model.update(Observation.from_bars(bars[i : i + 2]))
        self.assertTrue(snap.ready)
        self.assertGreater(snap.value("pca.explained"), 0.9)
        self.assertTrue(math.isfinite(snap.value("pca.value")))

    def test_jacobi_eigen(self):
        values, vectors = linalg.jacobi_eigen([[2.0, 1.0], [1.0, 2.0]])
        self.assertAlmostEqual(max(values), 3.0, places=8)
        self.assertAlmostEqual(min(values), 1.0, places=8)
        self.assertEqual(len(vectors), 2)

    def test_garch_variance_is_positive(self):
        model = (
            ModelBuilder()
            .add("px", Series("AAA"))
            .add("ret", LogReturn("px"))
            .add("g", GARCH11("ret", omega=1e-6, alpha=0.1, beta=0.85, warmup=10))
            .build()
        )
        prices = [100.0]
        rng = random.Random(4)
        for _ in range(80):
            prices.append(prices[-1] * math.exp(rng.gauss(0, 0.01)))
        snaps = model.transform(obs_for("AAA", prices))
        self.assertTrue(snaps[-1].ready)
        self.assertGreater(snaps[-1].value("g.variance"), 0.0)
        self.assertTrue(math.isfinite(snaps[-1].value("g.z")))


class PairsBacktestTests(unittest.TestCase):
    def test_pearl_pairs_backtest_trades(self):
        cfg = Config.from_dict(
            {
                "app": {"mode": "backtest"},
                "market": {"symbols": ["AAA", "BBB"], "timeframe": "1Day"},
                "wallet": {"starting_cash": 100_000.0, "allow_short": True},
                "execution": {"max_participation": 0.0},
                "risk": {
                    "max_order_notional": 100_000.0,
                    "max_position_pct": 0.45,
                    "max_gross_exposure_pct": 0.85,
                    "max_daily_loss_pct": 0.20,
                    "duplicate_window_s": 0.0,
                    "max_data_age_s": 0.0,
                },
                "feed": {"provider": "synthetic", "warmup_bars": 100},
                "storage": {"enabled": False},
                "strategy": {"name": "pearl_pairs", "params": {}},
            }
        )
        strategy = PearlPairs(
            left="AAA",
            right="BBB",
            window=40,
            min_obs=20,
            entry=1.5,
            exit=0.3,
            allocation=0.4,
            use_log=False,
        )
        bars = pair_bars(220)

        class Feed:
            def bars(self, symbols, timeframe, start=None, end=None):
                yield from group_bars([b for b in bars if b.symbol in symbols], timeframe)

        result = BacktestEngine(cfg, strategy, Feed(), run_id="pearl-backtest").run()
        self.assertGreater(len(result.fills), 0)
        self.assertEqual({f.symbol for f in result.fills}, {"AAA", "BBB"})
        self.assertLessEqual(result.metrics["max_drawdown"], 0.0)


if __name__ == "__main__":
    unittest.main()
