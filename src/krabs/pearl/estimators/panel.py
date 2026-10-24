"""Rolling PCA on a return panel and residual-portfolio construction.

The panel is standardized per asset (correlation PCA), so high-volatility ETFs
do not dominate the factors. For the top ``components`` principal components
``V`` and standardized returns ``X``:

    F = X V                     (factor scores)
    E = X - F V^T               (idiosyncratic residuals)

A residual signal on asset ``i`` holds the ``i``-th row of the projector
``M = I - V V^T``: long the asset's residual, short the factor exposure. This
keeps the portfolio (approximately) neutral to the fitted factors, including
the market component.

Everything here is causal when called with returns up to the current close.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence

from .. import linalg


@dataclass(frozen=True, slots=True)
class PanelPCAFit:
    symbols: tuple[str, ...]
    components: int
    means: tuple[float, ...]
    sigmas: tuple[float, ...]
    loadings: tuple[tuple[float, ...], ...]  # N x K
    eigenvalues: tuple[float, ...]
    residual_sd: tuple[float, ...]
    explained: float

    def standardize(self, returns: Sequence[float]) -> list[float]:
        return [
            (float(value) - mean) / sigma
            for value, mean, sigma in zip(returns, self.means, self.sigmas)
        ]

    def residual_row(self, index: int) -> list[float]:
        """Row ``index`` of ``I - V V^T`` in standardized return space."""
        n = len(self.symbols)
        row = [0.0] * n
        loadings = self.loadings[index]
        for j in range(n):
            projection = sum(loadings[k] * self.loadings[j][k] for k in range(self.components))
            row[j] = (1.0 if j == index else 0.0) - projection
        return row

    def zscore(self, index: int, returns: Sequence[float]) -> float:
        residual = sum(
            value * weight for value, weight in zip(self.standardize(returns), self.residual_row(index))
        )
        sd = self.residual_sd[index]
        return residual / sd if sd > 0 else 0.0


def fit_panel_pca(
    returns: Sequence[Sequence[float]],
    symbols: Sequence[str],
    *,
    components: int = 5,
    window: int = 120,
) -> PanelPCAFit:
    """Fit the top ``components`` PCs on the last ``window`` return rows.

    ``returns`` is a time-ordered sequence of rows, one value per symbol.
    Raises ``ValueError`` when there is not enough data or an asset is flat.
    """
    n = len(symbols)
    if n < 3:
        raise ValueError("panel PCA needs at least three symbols")
    if not 1 <= components < n:
        raise ValueError("components must be in [1, symbols)")
    rows = [list(row) for row in returns if len(row) == n]
    if len(rows) < max(20, components + 3):
        raise ValueError("not enough return rows for panel PCA")
    rows = rows[-window:]
    samples = len(rows)

    means = [statistics.fmean(row[i] for row in rows) for i in range(n)]
    sigmas = [statistics.pstdev(row[i] for row in rows) for i in range(n)]
    if any(not math.isfinite(sigma) or sigma <= 1e-12 for sigma in sigmas):
        raise ValueError("a panel asset has zero or invalid volatility")

    standardized = [
        [(row[i] - means[i]) / sigmas[i] for i in range(n)] for row in rows
    ]
    correlation = linalg.zeros(n, n)
    for i in range(n):
        for j in range(i, n):
            value = sum(row[i] * row[j] for row in standardized) / max(1, samples - 1)
            correlation[i][j] = correlation[j][i] = value

    top_values, top_vectors = linalg.top_eigenvectors(correlation, components)
    loadings: list[tuple[float, ...]] = [
        tuple(top_vectors[i][k] for k in range(components)) for i in range(n)
    ]
    # Fix each component's sign for stable hedge weights across refits.
    for k in range(components):
        if sum(loadings[i][k] for i in range(n)) < 0:
            for i in range(n):
                vector = list(loadings[i])
                vector[k] = -vector[k]
                loadings[i] = tuple(vector)

    projected = [
        [sum(row[i] * loadings[i][k] for i in range(n)) for k in range(components)]
        for row in standardized
    ]
    residual_sd = []
    for i in range(n):
        residuals = [
            standardized[t][i]
            - sum(projected[t][k] * loadings[i][k] for k in range(components))
            for t in range(samples)
        ]
        residual_sd.append(statistics.pstdev(residuals))
    if any(sd <= 1e-12 for sd in residual_sd):
        raise ValueError("a panel asset has no idiosyncratic variance left")

    total = sum(correlation[i][i] for i in range(n))
    explained = sum(max(0.0, value) for value in top_values) / total if total > 0 else 0.0
    return PanelPCAFit(
        symbols=tuple(symbols),
        components=components,
        means=tuple(means),
        sigmas=tuple(sigmas),
        loadings=tuple(loadings),
        eigenvalues=tuple(top_values),
        residual_sd=tuple(residual_sd),
        explained=explained,
    )


def residual_weights(
    fit: PanelPCAFit,
    index: int,
    side: int,
    gross: float,
    max_legs: int = 0,
) -> list[float]:
    """Actual asset weights for a unit-residual position, normalized to ``gross``.

    ``side=+1`` is long the residual (buy the asset, short the factor hedge).
    ``max_legs`` keeps only the largest hedge legs (the traded asset is always
    kept) and renormalizes; this cuts turnover at some cost in factor
    neutrality.
    """
    if gross <= 0:
        raise ValueError("gross must be positive")
    if max_legs < 0:
        raise ValueError("max_legs must be >= 0")
    row = fit.residual_row(index)
    raw = [side * value / fit.sigmas[j] for j, value in enumerate(row)]
    if 0 < max_legs < len(raw):
        keep = set(
            sorted(range(len(raw)), key=lambda j: abs(raw[j]), reverse=True)[:max_legs]
        )
        keep.add(index)
        raw = [value if j in keep else 0.0 for j, value in enumerate(raw)]
    total = sum(abs(value) for value in raw)
    if total <= 1e-12:
        return [0.0] * len(raw)
    scale = gross / total
    return [value * scale for value in raw]


def residual_edge(fit: PanelPCAFit, index: int, z: float, weights: Sequence[float]) -> float:
    """Expected return if the residual reverts from ``z`` to zero (fraction of equity)."""
    exposure = abs(weights[index]) * fit.sigmas[index]
    return exposure * abs(z) * fit.residual_sd[index]
