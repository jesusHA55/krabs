"""Pair screening for ETF/statistical arbitrage.

Selection is intentionally a separate, causal step from trading:

1. At a rebalance date, take the trailing formation window.
2. For every pair inside a sector cluster, run a Johansen test on log prices.
3. Keep pairs with a cointegrating rank, a half-life of 2–15 bars (days on
   daily data), at least N zero crossings per year, and liquid legs.
4. Pick the best pair per cluster, then the top ``max_pairs`` overall.

The screen only uses bars up to the decision timestamp; orders still fill on the
next bar.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from . import stats
from .estimators.statistics import johansen_test

DEFAULT_ETF_CLUSTERS: dict[str, list[str]] = {
    "technology": ["XLK", "SMH", "IGV", "VGT"],
    "financials": ["XLF", "KBE", "KRE", "IAI"],
    "health_care": ["XLV", "XBI", "IHI", "XPH"],
    "energy": ["XLE", "XOP", "OIH", "XES"],
    "consumer": ["XLY", "XRT", "XLP"],
    "industrials": ["XLI", "XAR", "ITA"],
    "materials": ["XLB", "XME", "GDX"],
    "utilities": ["XLU", "VPU"],
    "real_estate": ["XLRE", "IYR", "VNQ"],
    "communication": ["XLC", "XTL"],
}


@dataclass(frozen=True, slots=True)
class PairCandidate:
    left: str
    right: str
    cluster: str
    beta: float
    half_life: float
    crossings_per_year: float
    adf_t: float
    rank: int
    trace0: float
    friction_bps: float
    score: float

    @property
    def key(self) -> tuple[str, str]:
        return self.left, self.right

    def to_dict(self) -> dict:
        return {
            "left": self.left,
            "right": self.right,
            "cluster": self.cluster,
            "beta": self.beta,
            "half_life": self.half_life,
            "crossings_per_year": self.crossings_per_year,
            "adf_t": self.adf_t,
            "rank": self.rank,
            "trace0": self.trace0,
            "friction_bps": self.friction_bps,
            "score": self.score,
        }


def zero_crossings(values: Sequence[float]) -> int:
    """Number of times a demeaned series changes sign."""
    if len(values) < 2:
        return 0
    avg = stats.mean(values)
    signs = [1 if value > avg else -1 for value in values]
    return sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])


def screen_pairs(
    closes: Mapping[str, Sequence[float]],
    clusters: Mapping[str, Sequence[str]],
    *,
    min_half_life: float = 2.0,
    max_half_life: float = 15.0,
    min_crossings_per_year: float = 12.0,
    max_bid_ask_bps: float = 5.0,
    spread_bps: Mapping[str, float] | None = None,
    default_spread_bps: float = 2.0,
    max_pairs: int = 5,
    min_observations: int = 80,
    annualization: float = 252.0,
    min_abs_beta: float = 0.2,
    max_abs_beta: float = 5.0,
) -> list[PairCandidate]:
    spreads = dict(spread_bps or {})
    candidates: list[PairCandidate] = []

    for cluster, symbols in clusters.items():
        available = [symbol.upper() for symbol in symbols if symbol.upper() in closes]
        for left, right in itertools.combinations(available, 2):
            left_values = [float(v) for v in closes[left]]
            right_values = [float(v) for v in closes[right]]
            n = min(len(left_values), len(right_values))
            if n < min_observations:
                continue
            left_values = left_values[-n:]
            right_values = right_values[-n:]
            if any(value <= 0 for value in left_values) or any(
                value <= 0 for value in right_values
            ):
                continue
            levels = [
                [math.log(left_values[i]), math.log(right_values[i])] for i in range(n)
            ]
            result = johansen_test(levels)
            if not result or int(result.get("rank", 0)) < 1:
                continue
            beta = float(result.get("beta", 0.0))
            if (
                not math.isfinite(beta)
                or abs(beta) < min_abs_beta
                or abs(beta) > max_abs_beta
            ):
                continue
            half_life = float(result.get("half_life", 0.0))
            if not min_half_life <= half_life <= max_half_life:
                continue
            spread_series = result["spread_series"]
            crossings = zero_crossings(spread_series) * annualization / max(1, n - 1)
            if crossings < min_crossings_per_year:
                continue
            left_bps = float(spreads.get(left, default_spread_bps))
            right_bps = float(spreads.get(right, default_spread_bps))
            # The requirement applies to each leg: a wide ETF is not liquid
            # enough for this strategy, even if the pair average is narrow.
            if max(left_bps, right_bps) >= max_bid_ask_bps:
                continue
            friction_bps = 0.5 * (left_bps + abs(beta) * right_bps)
            adf_t = stats.adf_tstat(spread_series)
            score = crossings / max(half_life, 0.5)
            candidates.append(
                PairCandidate(
                    left=left,
                    right=right,
                    cluster=cluster,
                    beta=beta,
                    half_life=half_life,
                    crossings_per_year=crossings,
                    adf_t=adf_t,
                    rank=int(result.get("rank", 0)),
                    trace0=float(result.get("trace0", 0.0)),
                    friction_bps=friction_bps,
                    score=score,
                )
            )

    # Best pair per cluster first, then cap the portfolio and avoid shared legs.
    candidates.sort(key=lambda c: c.score, reverse=True)
    selected: list[PairCandidate] = []
    used_clusters: set[str] = set()
    used_symbols: set[str] = set()
    for candidate in candidates:
        if candidate.cluster in used_clusters:
            continue
        if candidate.left in used_symbols or candidate.right in used_symbols:
            continue
        selected.append(candidate)
        used_clusters.add(candidate.cluster)
        used_symbols.update((candidate.left, candidate.right))
        if len(selected) >= max_pairs:
            break
    return selected
