from __future__ import annotations

import math
import random
import unittest

from krabs.pearl.estimators.panel import (
    fit_panel_pca,
    residual_edge,
    residual_weights,
)


def synthetic_panel(
    assets: int = 8, days: int = 400, seed: int = 7
) -> tuple[list[str], list[list[float]]]:
    rng = random.Random(seed)
    symbols = [f"E{i}" for i in range(assets)]
    loadings = [0.6 + 0.1 * i for i in range(assets)]
    idio = [0.0] * assets
    rows: list[list[float]] = []
    for _ in range(days):
        factor = rng.gauss(0.0, 0.01)
        row = []
        for i in range(assets):
            idio[i] = 0.6 * idio[i] + rng.gauss(0.0, 0.004)
            row.append(loadings[i] * factor + idio[i])
        rows.append(row)
    return symbols, rows


class PanelPCATests(unittest.TestCase):
    def test_fits_factors_and_standardizes_residuals(self):
        symbols, rows = synthetic_panel()
        fit = fit_panel_pca(rows, symbols, components=2, window=120)

        self.assertEqual(fit.symbols, tuple(symbols))
        self.assertEqual(fit.components, 2)
        self.assertGreater(fit.explained, 0.6)
        self.assertEqual(len(fit.residual_sd), len(symbols))
        self.assertTrue(all(0 < sd < 1.5 for sd in fit.residual_sd))

        latest = rows[-1]
        z = fit.zscore(1, latest)
        standardized = fit.standardize(latest)
        manual = sum(
            value * weight
            for value, weight in zip(standardized, fit.residual_row(1))
        ) / fit.residual_sd[1]
        self.assertAlmostEqual(z, manual, places=10)

    def test_residual_weights_are_gross_normalized_and_neutral(self):
        symbols, rows = synthetic_panel()
        fit = fit_panel_pca(rows, symbols, components=2, window=120)
        weights = residual_weights(fit, 3, side=+1, gross=0.10)

        self.assertAlmostEqual(sum(abs(value) for value in weights), 0.10, places=10)
        self.assertGreater(weights[3], 0)
        # The standardized factor exposure of the hedge is ~zero.
        for k in range(fit.components):
            exposure = sum(
                weight * fit.sigmas[i] * fit.loadings[i][k]
                for i, weight in enumerate(weights)
            )
            self.assertAlmostEqual(exposure, 0.0, places=12)

    def test_max_legs_keeps_the_asset_and_the_gross_budget(self):
        symbols, rows = synthetic_panel()
        fit = fit_panel_pca(rows, symbols, components=2, window=120)
        weights = residual_weights(fit, 3, side=1, gross=0.10, max_legs=4)
        self.assertAlmostEqual(sum(abs(value) for value in weights), 0.10, places=10)
        self.assertLessEqual(sum(1 for value in weights if abs(value) > 1e-12), 4)
        self.assertGreater(weights[3], 0)

    def test_expected_edge_is_positive_for_a_displaced_residual(self):
        symbols, rows = synthetic_panel()
        fit = fit_panel_pca(rows, symbols, components=2, window=120)
        weights = residual_weights(fit, 2, side=-1, gross=0.10)
        self.assertGreater(residual_edge(fit, 2, z=2.5, weights=weights), 0.0)

    def test_rejects_degenerate_inputs(self):
        symbols, rows = synthetic_panel()
        with self.assertRaises(ValueError):
            fit_panel_pca(rows[:10], symbols, components=2, window=120)
        with self.assertRaises(ValueError):
            fit_panel_pca(rows, symbols[:2], components=1, window=120)
        flat = [[0.0] * len(symbols) for _ in range(200)]
        with self.assertRaises(ValueError):
            fit_panel_pca(flat, symbols, components=2, window=120)


if __name__ == "__main__":
    unittest.main()
