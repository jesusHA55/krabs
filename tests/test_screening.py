from __future__ import annotations

import math
import random
import unittest

from krabs.pearl.screening import screen_pairs, zero_crossings


def series(seed: int = 5, n: int = 400):
    rng = random.Random(seed)
    x = 100.0
    spread = 0.0
    left, right = [], []
    for _ in range(n):
        x *= math.exp(rng.gauss(0.0001, 0.006))
        spread = 0.8 * spread + rng.gauss(0.0, 0.05)
        left.append(x)
        right.append(2.0 * x * math.exp(spread))
    return {"AAA": left, "BBB": right}


class ScreeningTests(unittest.TestCase):
    def test_zero_crossings(self):
        self.assertEqual(zero_crossings([-1, 1, -1, 1, 1, -1]), 4)
        self.assertEqual(zero_crossings([1, 1, 1]), 0)

    def test_selects_cointegrated_pair(self):
        selected = screen_pairs(
            series(),
            {"test": ["AAA", "BBB"]},
            min_half_life=1,
            max_half_life=20,
            min_crossings_per_year=5,
            max_bid_ask_bps=5,
            default_spread_bps=2,
            max_pairs=1,
            min_observations=100,
        )
        self.assertEqual(len(selected), 1)
        candidate = selected[0]
        self.assertEqual(candidate.key, ("AAA", "BBB"))
        self.assertGreater(candidate.half_life, 0)
        self.assertGreater(candidate.crossings_per_year, 5)
        self.assertGreater(abs(candidate.beta), 0.2)
        self.assertLess(abs(candidate.beta), 2.0)

    def test_filters_reject_wide_or_slow_spreads(self):
        data = series()
        wide = screen_pairs(
            data,
            {"test": ["AAA", "BBB"]},
            min_half_life=1,
            max_half_life=20,
            min_crossings_per_year=5,
            max_bid_ask_bps=5,
            default_spread_bps=10,
            max_pairs=1,
            min_observations=100,
        )
        self.assertEqual(wide, [])
        slow = screen_pairs(
            data,
            {"test": ["AAA", "BBB"]},
            min_half_life=1,
            max_half_life=0.5,
            min_crossings_per_year=5,
            max_bid_ask_bps=5,
            default_spread_bps=2,
            max_pairs=1,
            min_observations=100,
        )
        self.assertEqual(slow, [])

    def test_one_pair_per_cluster_and_no_shared_legs(self):
        data = series()
        data["CCC"] = [value * 1.001 for value in data["BBB"]]
        selected = screen_pairs(
            data,
            {"one": ["AAA", "BBB"], "two": ["BBB", "CCC"]},
            min_half_life=1,
            max_half_life=20,
            min_crossings_per_year=5,
            max_bid_ask_bps=5,
            default_spread_bps=2,
            max_pairs=5,
            min_observations=100,
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].cluster, "one")


if __name__ == "__main__":
    unittest.main()
