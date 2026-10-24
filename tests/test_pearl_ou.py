from __future__ import annotations

import math
import random
import unittest
from datetime import datetime, timedelta, timezone

from krabs.events import Bar
from krabs.pearl import Kalman, LogSeries, ModelBuilder, Observation, OU, Series, Spread

START = datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc)


def make_bar(symbol: str, i: int, close: float) -> Bar:
    return Bar(symbol, START + timedelta(days=i), close, close, close, close, 1_000_000)


def feed(node: OU, values: list[float]) -> dict:
    out = {}
    for value in values:
        out = node.update(None, {"spread": {"value": value}})
    return out


class OUTests(unittest.TestCase):
    def test_estimates_mean_reversion_on_stationary_series(self):
        rng = random.Random(4)
        phi = 0.93
        x, values = 0.0, []
        for _ in range(600):
            x = phi * x + rng.gauss(0.0, 0.02)
            values.append(x)

        node = OU("spread", window=120, refit=20, warmup=5)
        last = feed(node, values)

        self.assertTrue(node.ready)
        self.assertGreater(last["phi"], 0.82)
        self.assertLess(last["phi"], 0.97)
        expected_hl = -math.log(2.0) / math.log(last["phi"])
        self.assertAlmostEqual(last["half_life"], expected_hl, places=6)
        self.assertLess(abs(last["z"]), 4.0)
        self.assertGreater(last["sd"], 0.0)

    def test_refuses_a_trending_series(self):
        node = OU("spread", window=120, refit=20, warmup=5)
        feed(node, [100.0 + 0.5 * i for i in range(400)])
        self.assertFalse(node.ready)

    def test_state_roundtrip_continues_identically(self):
        rng = random.Random(9)
        x, values = 0.0, []
        for _ in range(400):
            x = 0.9 * x + rng.gauss(0.0, 0.01)
            values.append(x)

        a = OU("spread", window=120, refit=20)
        feed(a, values[:-30])
        b = OU("spread", window=120, refit=20)
        b.load_state(a.state())

        for value in values[-30:]:
            out_a = a.update(None, {"spread": {"value": value}})
            out_b = b.update(None, {"spread": {"value": value}})
            self.assertAlmostEqual(out_a["z"], out_b["z"], places=10)

    def test_model_builder_accepts_ou(self):
        model = (
            ModelBuilder()
            .add("left", Series("AAA"))
            .add("right", Series("BBB"))
            .add("spread", Spread("left", "right", hedge=2.0, log=True))
            .add("ou", OU("spread", window=60))
            .build()
        )
        snap = None
        for i in range(200):
            left = 100.0 * math.exp(0.0002 * i)
            right = 50.0 * math.exp(0.0001 * i) * math.exp(0.01 * math.sin(i / 5.0))
            snap = model.update(
                Observation.from_bars([make_bar("AAA", i, left), make_bar("BBB", i, right)])
            )
        self.assertIsNotNone(snap)
        self.assertTrue(snap.ready)
        self.assertIsNotNone(snap.value("ou.z"))
        self.assertGreater(snap.value("ou.sd", 0.0), 0.0)


class DynamicBetaTests(unittest.TestCase):
    def test_regression_kalman_recovers_hedge_ratio(self):
        rng = random.Random(2)
        x = 100.0
        points = []
        for _ in range(400):
            x *= math.exp(rng.gauss(0.0002, 0.01))
            y = math.exp(0.5) * (x ** 1.5) * math.exp(rng.gauss(0.0, 0.001))
            points.append((x, y))

        model = (
            ModelBuilder()
            .add("left", LogSeries("AAA"))
            .add("right", LogSeries("BBB"))
            .add(
                "beta",
                Kalman(
                    "left",
                    model="regression",
                    features=["right"],
                    intercept=True,
                    coeff_var=1e-6,
                    obs_var=1e-5,
                    warmup=20,
                ),
            )
            .build()
        )
        snap = None
        for i, (x, y) in enumerate(points):
            snap = model.update(
                Observation.from_bars([make_bar("AAA", i, y), make_bar("BBB", i, x)])
            )
        self.assertTrue(snap.ready)
        self.assertAlmostEqual(snap.value("beta.coef1", 0.0), 1.5, delta=0.05)
        self.assertAlmostEqual(snap.value("beta.coef0", 0.0), 0.5, delta=0.05)


if __name__ == "__main__":
    unittest.main()
