"""ETF pairs trading with sector-cluster screening and a Kalman z-score.

Pipeline
--------
1. At a rebalance date, screen every pair inside a sector cluster over a trailing
   formation window using Johansen cointegration.
2. Keep pairs with half-life 2–15 bars, at least 12 zero crossings/year, and
   liquid legs (default: both bid/ask spreads < 5 bps).
3. Pick the best pair per cluster (no shared legs), cap the portfolio, and warm
   a Kalman local-level filter on the pair spread.
4. Enter when |z| > entry_z and |innovation| > friction_multiple × friction;
   hold until the z-score crosses zero.

The model is causal: screening uses bars up to the current close, and orders
fill on the next bar. Bid/ask spreads come from ``spreads_csv`` (captured
quotes), then per-symbol parameters, then ``default_spread_bps``.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from ...events import Bar
from ...pearl import (
    DEFAULT_ETF_CLUSTERS,
    Kalman,
    LogSeries,
    OU,
    ModelBuilder,
    Observation,
    PairCandidate,
    PearlModel,
    Series,
    Spread,
    screen_pairs,
)
from ...spreads import load_spreads
from ...strategy.base import Strategy, StrategyContext
from ..registry import register


@dataclass(slots=True)
class ActivePair:
    candidate: PairCandidate
    model: PearlModel
    side: int = 0
    last_z: float = 0.0
    last_innovation: float = 0.0
    entries: int = 0
    closing: bool = False
    repair_attempts: int = 0
    opened_at_bar: int = 0
    beta: float = 0.0
    phi: float = 0.0
    half_life: float = 0.0


@register
class ETFPairs(Strategy):
    id = "etf_pairs"
    description = (
        "Sector ETF pairs: Johansen screening, half-life/zero-crossing/liquidity "
        "filters, then a Kalman z-score entry with a friction threshold."
    )
    defaults = {
        "clusters": {},
        "formation_days": 252,
        "rescreen_days": 21,
        "min_half_life": 2.0,
        "max_half_life": 15.0,
        "min_crossings_per_year": 12.0,
        "periods_per_year": 252.0,
        "max_bid_ask_bps": 5.0,
        "default_spread_bps": 2.0,
        "spreads_csv": "",
        "min_abs_beta": 0.20,
        "max_abs_beta": 5.0,
        "spread_bps": {},
        "entry_z": 2.0,
        "friction_multiple": 2.5,
        "exit_z": 0.0,
        "max_hold_bars": 0,
        "allocation": 0.10,
        "max_pairs": 5,
        "model": "local_level",
        "kf_level_var": 1e-6,
        "kf_obs_var": 1e-4,
        "kf_warmup": 20,
        "ou_window": 120,
        "ou_refit": 20,
        "ou_obs_ratio": 0.5,
        "ou_warmup": 5,
        "beta_var": 1e-5,
        "driver": "",
    }

    def __init__(self, **params: object) -> None:
        super().__init__(**params)
        clusters = self.p("clusters") or DEFAULT_ETF_CLUSTERS
        normalized: dict[str, list[str]] = {}
        seen: set[str] = set()
        for cluster, symbols in clusters.items():
            normalized[str(cluster)] = [str(s).upper() for s in symbols]
            for symbol in normalized[str(cluster)]:
                if symbol in seen:
                    raise ValueError(f"symbol {symbol} appears in more than one cluster")
                seen.add(symbol)
        self.clusters = normalized
        self._driver = str(self.p("driver")).upper() or next(iter(normalized.values()))[0]
        if self._driver not in seen:
            raise ValueError("driver must be one of the cluster symbols")
        if int(self.p("formation_days")) < 40:
            raise ValueError("formation_days must be >= 40")
        if int(self.p("rescreen_days")) < 1:
            raise ValueError("rescreen_days must be >= 1")
        if float(self.p("allocation")) <= 0 or float(self.p("allocation")) > 1:
            raise ValueError("allocation must be in (0, 1]")
        if int(self.p("max_pairs")) < 1:
            raise ValueError("max_pairs must be >= 1")
        if float(self.p("entry_z")) <= 0 or float(self.p("friction_multiple")) < 0:
            raise ValueError("invalid entry thresholds")
        if float(self.p("exit_z")) != 0:
            raise ValueError("this example exits exactly at z=0; exit_z must be 0")
        if int(self.p("max_hold_bars")) < 0:
            raise ValueError("max_hold_bars must be >= 0 (0 disables the time stop)")
        self.model_name = str(self.p("model")).strip().lower()
        if self.model_name not in ("local_level", "ou", "ou_dynamic"):
            raise ValueError("model must be local_level, ou or ou_dynamic")
        if int(self.p("ou_window")) < 20:
            raise ValueError("ou_window must be >= 20")
        if self.model_name != "local_level" and int(self.p("ou_window")) >= int(
            self.p("formation_days")
        ):
            raise ValueError("ou_window must be smaller than formation_days")
        if int(self.p("beta_var")) < 0:
            raise ValueError("beta_var must be >= 0")
        self.log = logging.getLogger("krabs.strategy.etf_pairs")
        self.spread_bps = self._load_spreads()
        self._bar_index = 0
        self._last_screen = -10**9
        self._active: dict[str, ActivePair] = {}
        self._pending: dict[str, PairCandidate] = {}
        self._last_selection: list[PairCandidate] = []

    def _load_spreads(self) -> dict[str, float]:
        """Per-symbol bid/ask in bps: explicit params, then CSV overrides."""
        spreads = {
            str(symbol).upper(): float(value)
            for symbol, value in dict(self.p("spread_bps") or {}).items()
        }
        self.spreads_source = "params" if spreads else "default"
        spreads_csv = str(self.p("spreads_csv") or "").strip()
        if not spreads_csv:
            return spreads
        loaded = load_spreads(spreads_csv, logger=self.log)
        if not loaded:
            self.log.warning(
                "no spreads in %s; falling back to configured/estimated spreads",
                spreads_csv,
            )
            return spreads
        spreads.update(loaded)
        self.spreads_source = f"csv:{spreads_csv} ({len(loaded)} symbols)"
        return spreads

    def _signal_refs(self) -> tuple[str, str, str]:
        if self.model_name == "local_level":
            return "kf.innovation_z", "kf.innovation", ""
        if self.model_name == "ou_dynamic":
            return "ou.z", "ou.innovation", "beta.coef1"
        return "ou.z", "ou.innovation", ""

    def _friction_bps(self, left: str, right: str, beta: float) -> float:
        default = float(self.p("default_spread_bps"))
        left_bps = self.spread_bps.get(left.upper(), default)
        right_bps = self.spread_bps.get(right.upper(), default)
        return 0.5 * (left_bps + abs(beta) * right_bps)

    # -- lifecycle -------------------------------------------------------
    def on_start(self, ctx: StrategyContext) -> None:
        self.log = logging.getLogger("krabs.strategy.etf_pairs")
        self._bar_index = 0
        self._last_screen = -10**9
        self._active.clear()
        self._pending.clear()
        self._last_selection = []

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        if bar.symbol != self._driver:
            return
        self._bar_index += 1

        if self._pending:
            self._open_pending(ctx)
        if self._bar_index - self._last_screen >= int(self.p("rescreen_days")):
            self._screen(ctx)
        self._update_active(ctx)

    def diagnostics(self) -> dict[str, Any]:
        signals = {
            f"{active.candidate.left}/{active.candidate.right}": active.side
            for active in self._active.values()
        }
        values = {
            f"{active.candidate.left}/{active.candidate.right}": {
                "z": active.last_z,
                "innovation": active.last_innovation,
                "beta": active.beta,
                "half_life": active.half_life or active.candidate.half_life,
                "phi": active.phi,
                "crossings": active.candidate.crossings_per_year,
                "friction_bps": active.candidate.friction_bps,
            }
            for active in self._active.values()
        }
        return {
            "ready": bool(self._active),
            "model": self.model_name,
            "spreads_source": self.spreads_source,
            "signals": signals,
            "values": values,
            "active_pairs": len(self._active),
            "selected_pairs": [c.to_dict() for c in self._last_selection],
        }

    # -- screening -------------------------------------------------------
    def _screen(self, ctx: StrategyContext) -> None:
        symbols = sorted({s for values in self.clusters.values() for s in values})
        bars_by_symbol = {symbol: ctx.bars(symbol, int(self.p("formation_days"))) for symbol in symbols}
        lengths = {symbol: len(rows) for symbol, rows in bars_by_symbol.items()}
        if not lengths or min(lengths.values()) < 40:
            return
        maps = {symbol: {b.ts: b for b in rows} for symbol, rows in bars_by_symbol.items()}
        common = set(maps[symbols[0]])
        for symbol in symbols[1:]:
            common &= set(maps[symbol])
        if len(common) < 40:
            return
        timestamps = sorted(common)[-int(self.p("formation_days")) :]
        closes = {
            symbol: [maps[symbol][ts].close for ts in timestamps] for symbol in symbols
        }

        selected = screen_pairs(
            closes,
            self.clusters,
            min_half_life=float(self.p("min_half_life")),
            max_half_life=float(self.p("max_half_life")),
            min_crossings_per_year=float(self.p("min_crossings_per_year")),
            max_bid_ask_bps=float(self.p("max_bid_ask_bps")),
            spread_bps=self.spread_bps,
            default_spread_bps=float(self.p("default_spread_bps")),
            max_pairs=int(self.p("max_pairs")),
            min_observations=max(60, int(self.p("formation_days")) // 2),
            annualization=float(self.p("periods_per_year")),
            min_abs_beta=float(self.p("min_abs_beta")),
            max_abs_beta=float(self.p("max_abs_beta")),
        )
        self._last_selection = selected
        selected_by_cluster = {candidate.cluster: candidate for candidate in selected}

        # Close pairs that disappeared or whose hedge ratio moved materially.
        for cluster, active in list(self._active.items()):
            replacement = selected_by_cluster.get(cluster)
            keep = (
                replacement is not None
                and replacement.key == active.candidate.key
                and abs(replacement.beta - active.beta)
                / max(0.1, abs(active.beta))
                < 0.20
            )
            if keep and not active.closing:
                # A static-beta model was built with the old hedge; rebuilding
                # mid-flight would lose filter state, so only refresh metadata.
                if self.model_name == "ou_dynamic" or abs(
                    replacement.beta - active.beta
                ) < 1e-9:
                    active.candidate = replacement
                continue
            self._flatten(ctx, active)
            active.closing = True

        # Schedule new pairs for the next bar; positions from closed pairs may
        # still need one bar to settle.
        self._pending = {
            candidate.cluster: candidate
            for candidate in selected
            if candidate.cluster not in self._active
        }
        self._last_screen = self._bar_index
        self.log.info(
            "screen: %d candidate(s), %d active, %d pending",
            len(selected),
            len(self._active),
            len(self._pending),
        )

    # -- opening / updating ---------------------------------------------
    def _open_pending(self, ctx: StrategyContext) -> None:
        for cluster, candidate in list(self._pending.items()):
            if cluster in self._active:
                del self._pending[cluster]
                continue
            left_position = ctx.position(candidate.left)
            right_position = ctx.position(candidate.right)
            if not left_position.is_flat or not right_position.is_flat:
                continue
            model = self._build_model(candidate)
            if not self._warm_model(ctx, model, candidate):
                continue
            self._active[cluster] = ActivePair(
                candidate=candidate, model=model, beta=candidate.beta
            )
            del self._pending[cluster]
            self.log.info(
                "enabled %s/%s hl=%.1f crossings=%.1f beta=%.2f friction=%.2fbps",
                candidate.left,
                candidate.right,
                candidate.half_life,
                candidate.crossings_per_year,
                candidate.beta,
                candidate.friction_bps,
            )

    def _build_model(self, candidate: PairCandidate) -> PearlModel:
        builder = ModelBuilder()
        if self.model_name == "local_level":
            return (
                builder.add("left", Series(candidate.left))
                .add("right", Series(candidate.right))
                .add(
                    "spread",
                    Spread("left", "right", hedge=candidate.beta, log=True),
                )
                .add(
                    "kf",
                    Kalman(
                        "spread",
                        model="local_level",
                        level_var=float(self.p("kf_level_var")),
                        obs_var=float(self.p("kf_obs_var")),
                        warmup=int(self.p("kf_warmup")),
                    ),
                )
                .build()
            )
        if self.model_name == "ou":
            builder.add("left", Series(candidate.left))
            builder.add("right", Series(candidate.right))
            builder.add("spread", Spread("left", "right", hedge=candidate.beta, log=True))
            builder.add("ou", self._ou_node("spread"))
            return builder.build()
        # ou_dynamic: log prices, random-walk [alpha, beta], OU on the residual.
        builder.add("left", LogSeries(candidate.left))
        builder.add("right", LogSeries(candidate.right))
        builder.add(
            "beta",
            Kalman(
                "left",
                model="regression",
                features=["right"],
                intercept=True,
                coeff_var=float(self.p("beta_var")),
                obs_var=float(self.p("kf_obs_var")),
                warmup=int(self.p("kf_warmup")),
            ),
        )
        builder.add("ou", self._ou_node("beta.residual"))
        return builder.build()

    def _ou_node(self, source: str) -> OU:
        return OU(
            source,
            window=int(self.p("ou_window")),
            refit=int(self.p("ou_refit")),
            obs_ratio=float(self.p("ou_obs_ratio")),
            warmup=int(self.p("ou_warmup")),
        )

    def _warm_model(
        self, ctx: StrategyContext, model: PearlModel, candidate: PairCandidate
    ) -> bool:
        rows = self._pair_bars(ctx, candidate.left, candidate.right, int(self.p("formation_days")))
        need = int(self.p("kf_warmup"))
        if self.model_name != "local_level":
            need = max(need, int(self.p("ou_window")) + int(self.p("ou_warmup")) + 5)
        if len(rows) < need:
            return False
        # Warm on history before the current bar; _update_active will feed the
        # current bar exactly once.
        for left_bar, right_bar in rows[:-1]:
            model.update(Observation.from_bars([left_bar, right_bar]))
        snapshot = model.last_snapshot
        return bool(snapshot and snapshot.ready)

    def _pair_bars(
        self, ctx: StrategyContext, left: str, right: str, n: int
    ) -> list[tuple[Bar, Bar]]:
        left_rows = {bar.ts: bar for bar in ctx.bars(left, n)}
        right_rows = {bar.ts: bar for bar in ctx.bars(right, n)}
        timestamps = sorted(set(left_rows) & set(right_rows))
        return [(left_rows[ts], right_rows[ts]) for ts in timestamps]

    def _update_active(self, ctx: StrategyContext) -> None:
        entry_z = float(self.p("entry_z"))
        exit_z = float(self.p("exit_z"))
        friction_multiple = float(self.p("friction_multiple"))
        for cluster, active in list(self._active.items()):
            if active.closing:
                if ctx.position(active.candidate.left).is_flat and ctx.position(
                    active.candidate.right
                ).is_flat:
                    del self._active[cluster]
                else:
                    self._set_targets(ctx, active, 0)
                continue
            try:
                left_bar = ctx.bar(active.candidate.left)
                right_bar = ctx.bar(active.candidate.right)
            except Exception:  # noqa: BLE001
                continue
            if left_bar.ts != right_bar.ts:
                continue
            snapshot = active.model.update(Observation.from_bars([left_bar, right_bar]))
            z_ref, innovation_ref, beta_ref = self._signal_refs()
            if not snapshot.ready:
                if active.side != 0:
                    self.log.warning(
                        "flattening %s/%s: %s model not ready",
                        active.candidate.left,
                        active.candidate.right,
                        self.model_name,
                    )
                    self._flatten(ctx, active)
                continue
            if beta_ref:
                beta = snapshot.value(beta_ref, active.beta)
                if beta is not None and math.isfinite(beta):
                    lower = float(self.p("min_abs_beta"))
                    upper = float(self.p("max_abs_beta"))
                    if lower <= abs(beta) <= upper:
                        active.beta = float(beta)
            z = snapshot.value(z_ref, 0.0) or 0.0
            innovation = snapshot.value(innovation_ref, 0.0) or 0.0
            active.last_z = z
            active.last_innovation = innovation
            if self.model_name != "local_level":
                active.phi = snapshot.value("ou.phi", active.phi) or active.phi
                active.half_life = snapshot.value("ou.half_life", active.half_life) or active.half_life

            left_position = ctx.position(active.candidate.left)
            right_position = ctx.position(active.candidate.right)
            both_flat = left_position.is_flat and right_position.is_flat

            if active.side == 0:
                if not both_flat:
                    # A previous exit was partially rejected; keep flattening.
                    self._set_targets(ctx, active, 0)
                    continue
                active.repair_attempts = 0
                friction = self._friction_bps(
                    active.candidate.left, active.candidate.right, active.beta
                ) / 10_000.0
                if z >= entry_z and abs(innovation) >= friction_multiple * friction:
                    if self._set_targets(ctx, active, -1):
                        active.side = -1
                        active.entries += 1
                        active.opened_at_bar = self._bar_index
                elif z <= -entry_z and abs(innovation) >= friction_multiple * friction:
                    if self._set_targets(ctx, active, 1):
                        active.side = 1
                        active.entries += 1
                        active.opened_at_bar = self._bar_index
                continue

            # One leg filled and the other did not: keep the hedge intact by
            # re-sending the target, then give up and flatten if it stays broken.
            if left_position.is_flat != right_position.is_flat:
                active.repair_attempts += 1
                if active.repair_attempts <= 2:
                    self._set_targets(ctx, active, active.side)
                else:
                    self.log.warning(
                        "flattening %s/%s: one-legged position after %d repair attempts",
                        active.candidate.left,
                        active.candidate.right,
                        active.repair_attempts,
                    )
                    self._flatten(ctx, active)
                continue
            active.repair_attempts = 0

            max_hold = int(self.p("max_hold_bars"))
            if max_hold > 0 and self._bar_index - active.opened_at_bar >= max_hold:
                self.log.info(
                    "time stop %s/%s after %d bars (z=%+.2f)",
                    active.candidate.left,
                    active.candidate.right,
                    self._bar_index - active.opened_at_bar,
                    z,
                )
                self._flatten(ctx, active)
                continue

            # Exit when the z-score crosses zero. Treat the exit as done as
            # soon as it is submitted: fills happen next bar, and any rejected
            # leg is flattened by the side == 0 branch above.
            if active.side > 0 and z >= exit_z:
                self._set_targets(ctx, active, 0)
                active.side = 0
            elif active.side < 0 and z <= exit_z:
                self._set_targets(ctx, active, 0)
                active.side = 0

    def _flatten(self, ctx: StrategyContext, active: ActivePair) -> None:
        self._set_targets(ctx, active, 0)
        active.side = 0

    def _set_targets(self, ctx: StrategyContext, active: ActivePair, side: int) -> bool:
        candidate = active.candidate
        gross = float(self.p("allocation"))
        beta = active.beta if active.beta else candidate.beta
        denominator = 1.0 + abs(beta)
        left_weight = side * gross / denominator
        right_weight = -side * beta * gross / denominator
        reason = (
            f"etf_pairs {candidate.left}/{candidate.right} side={side:+d} "
            f"z={active.last_z:+.2f} beta={beta:.2f}"
        )
        left_order = ctx.target_weight(candidate.left, left_weight, reason=reason)
        right_order = ctx.target_weight(candidate.right, right_weight, reason=reason)
        if side == 0:
            return ctx.position(candidate.left).is_flat and ctx.position(candidate.right).is_flat
        return left_order is not None and right_order is not None
