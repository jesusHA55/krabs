"""PCA residual stat-arb on an ETF panel.

Every ``refit`` bars the strategy fits a correlation PCA on the trailing
``window`` of daily returns. For each asset it measures the return residual
against the top ``components`` factors and standardizes it by the residual's
in-sample volatility.

A trade holds one row of the residual projector ``I - V V^T``: for a positive
residual (``z > entry_z``) it shorts the asset's residual, which means shorting
the asset while holding a factor-mimicking hedge, and vice versa. Positions are
normalized to ``allocation`` gross per signal and capped at ``max_gross``.

The model is causal: the fit uses returns up to the current close, and orders
fill on the next bar. Existing positions keep the hedge vector from their entry
fit so a refit cannot silently redefine the trade; exits use that same fit.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from ...events import Bar
from ...pearl.estimators.panel import (
    PanelPCAFit,
    fit_panel_pca,
    residual_edge,
    residual_weights,
)
from ...pearl.screening import DEFAULT_ETF_CLUSTERS
from ...spreads import load_spreads
from ...strategy.base import Strategy, StrategyContext
from ..registry import register


@dataclass(slots=True)
class ResidualSignal:
    symbol: str
    index: int
    side: int
    entry_z: float
    entry_bar: int
    weights: dict[str, float]
    fit: PanelPCAFit
    last_z: float = 0.0


@register
class PCAResidual(Strategy):
    id = "pca_residual"
    description = (
        "Panel PCA stat-arb: trade ETF return residuals against the top "
        "principal-component hedge, with a causal rolling fit."
    )
    defaults = {
        "symbols": [],
        "window": 120,
        "refit": 20,
        "components": 5,
        "entry_z": 2.0,
        "exit_z": 0.5,
        "stop_z": 4.0,
        "friction_multiple": 1.0,
        "allocation": 0.10,
        "max_legs": 12,
        "max_positions": 6,
        "max_gross": 0.60,
        "max_hold_bars": 40,
        "min_hold_bars": 3,
        "min_resid_bps": 5.0,
        "max_bid_ask_bps": 5.0,
        "default_spread_bps": 2.0,
        "spreads_csv": "",
        "spread_multiplier": 1.0,
        "spread_bps": {},
        "slippage_bps": 1.0,
        "driver": "",
    }

    def __init__(self, **params: object) -> None:
        super().__init__(**params)
        configured = self.p("symbols") or []
        if configured:
            symbols = [str(symbol).upper() for symbol in configured]
        else:
            symbols = sorted(
                {symbol for group in DEFAULT_ETF_CLUSTERS.values() for symbol in group}
            )
        if len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be unique")
        if len(symbols) < 3:
            raise ValueError("pca_residual needs at least three symbols")
        self.symbols = symbols
        self._driver = str(self.p("driver")).upper() or symbols[0]
        if self._driver not in symbols:
            raise ValueError("driver must be one of symbols")
        if int(self.p("window")) < 40:
            raise ValueError("window must be >= 40")
        if int(self.p("refit")) < 1:
            raise ValueError("refit must be >= 1")
        if not 1 <= int(self.p("components")) < len(symbols):
            raise ValueError("components must be in [1, len(symbols))")
        if float(self.p("entry_z")) <= 0:
            raise ValueError("entry_z must be positive")
        if float(self.p("stop_z")) <= float(self.p("entry_z")):
            raise ValueError("stop_z must exceed entry_z")
        if not 0 < float(self.p("allocation")) <= 1:
            raise ValueError("allocation must be in (0, 1]")
        if float(self.p("max_gross")) <= 0:
            raise ValueError("max_gross must be positive")
        if int(self.p("max_positions")) < 1:
            raise ValueError("max_positions must be >= 1")
        if int(self.p("max_hold_bars")) < 0:
            raise ValueError("max_hold_bars must be >= 0")
        if int(self.p("min_hold_bars")) < 0:
            raise ValueError("min_hold_bars must be >= 0")
        if int(self.p("max_legs")) < 0:
            raise ValueError("max_legs must be >= 0")
        if float(self.p("spread_multiplier")) <= 0:
            raise ValueError("spread_multiplier must be positive")
        if float(self.p("min_resid_bps")) < 0:
            raise ValueError("min_resid_bps must be >= 0")
        self.log = logging.getLogger("krabs.strategy.pca_residual")
        self.spread_bps = self._load_spreads()
        self._bar_index = 0
        self._fit: PanelPCAFit | None = None
        self._fit_bar = -10**9
        self._signals: dict[str, ResidualSignal] = {}
        self._target: dict[str, float] = {}
        self._last_gross = 0.0
        self._fit_failures = 0
        self._entries = 0
        self._dirty = False

    # -- setup -----------------------------------------------------------
    def _load_spreads(self) -> dict[str, float]:
        """Per-symbol bid/ask in bps: params, then CSV, then the stress multiplier."""
        spreads = {
            str(symbol).upper(): float(value)
            for symbol, value in dict(self.p("spread_bps") or {}).items()
        }
        source = "params" if spreads else "default"
        spreads_csv = str(self.p("spreads_csv") or "").strip()
        if spreads_csv:
            loaded = load_spreads(spreads_csv, logger=self.log)
            if loaded:
                spreads.update(loaded)
                source = f"csv:{spreads_csv} ({len(loaded)} symbols)"
            else:
                self.log.warning(
                    "no spreads in %s; falling back to configured estimates",
                    spreads_csv,
                )
        multiplier = float(self.p("spread_multiplier"))
        if multiplier != 1.0:
            spreads = {
                symbol: value * multiplier for symbol, value in spreads.items()
            }
            source += f" x{multiplier:g}"
        self.spreads_source = source
        return spreads

    def on_start(self, ctx: StrategyContext) -> None:
        self._bar_index = 0
        self._fit = None
        self._fit_bar = -10**9
        self._signals.clear()
        self._target.clear()
        self._last_gross = 0.0
        self._fit_failures = 0
        self._entries = 0
        self._dirty = False

    # -- main loop -------------------------------------------------------
    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        if bar.symbol != self._driver:
            return
        self._bar_index += 1
        panel = self._panel_returns(ctx)
        if panel is None:
            return
        returns = panel

        changed = self._update_signals(returns)
        if self._bar_index - self._fit_bar >= int(self.p("refit")):
            self._refit(returns)
        if self._fit is not None:
            changed = self._open_signals(returns) or changed
        if changed:
            self._dirty = True
        if self._dirty and not ctx.open_orders():
            self._rebalance(ctx)

    def _update_signals(self, returns: Sequence[Sequence[float]]) -> bool:
        changed = False
        latest = returns[-1]
        for symbol, signal in list(self._signals.items()):
            x = signal.fit.standardize(latest)
            residual = sum(
                value * weight
                for value, weight in zip(x, signal.fit.residual_row(signal.index))
            )
            z = residual / signal.fit.residual_sd[signal.index]
            signal.last_z = z
            held = self._bar_index - signal.entry_bar
            reverted = z >= float(self.p("exit_z")) if signal.side > 0 else z <= float(
                self.p("exit_z")
            )
            if held < int(self.p("min_hold_bars")):
                reverted = False
            adverse = (
                z <= -float(self.p("stop_z"))
                if signal.side > 0
                else z >= float(self.p("stop_z"))
            )
            aged = int(self.p("max_hold_bars")) > 0 and (
                held >= int(self.p("max_hold_bars"))
            )
            if reverted or adverse or aged:
                self.log.info(
                    "exit %s side=%+d z=%+.2f after %d bars",
                    symbol,
                    signal.side,
                    z,
                    held,
                )
                del self._signals[symbol]
                changed = True
        return changed

    def _open_signals(self, returns: Sequence[Sequence[float]]) -> bool:
        fit = self._fit
        if fit is None:
            return False
        open_slots = int(self.p("max_positions")) - len(self._signals)
        if open_slots <= 0:
            return False
        latest = returns[-1]
        candidates: list[tuple[float, int, float]] = []
        for index, symbol in enumerate(self.symbols):
            if symbol in self._signals:
                continue
            if self.spread_bps.get(
                symbol,
                float(self.p("default_spread_bps")) * float(self.p("spread_multiplier")),
            ) > float(self.p("max_bid_ask_bps")):
                continue
            if fit.residual_sd[index] * fit.sigmas[index] < float(self.p("min_resid_bps")) / 10_000.0:
                continue
            z = fit.zscore(index, latest)
            if abs(z) < float(self.p("entry_z")):
                continue
            candidates.append((abs(z), index, z))
        candidates.sort(reverse=True)

        changed = False
        for _, index, z in candidates:
            if open_slots <= 0:
                break
            symbol = self.symbols[index]
            side = -1 if z > 0 else 1
            weights = residual_weights(
                fit,
                index,
                side,
                float(self.p("allocation")),
                int(self.p("max_legs")),
            )
            expected = residual_edge(fit, index, z, weights)
            cost = sum(
                abs(weight) * self._trade_cost_bps(other) / 10_000.0
                for other, weight in zip(self.symbols, weights)
                if weight
            )
            if expected < float(self.p("friction_multiple")) * 2.0 * cost:
                continue
            self._signals[symbol] = ResidualSignal(
                symbol=symbol,
                index=index,
                side=side,
                entry_z=z,
                entry_bar=self._bar_index,
                weights={
                    other: weight
                    for other, weight in zip(self.symbols, weights)
                    if abs(weight) > 1e-9
                },
                fit=fit,
                last_z=z,
            )
            self.log.info(
                "enter %s side=%+d z=%+.2f expected=%.2fbps cost=%.2fbps",
                symbol,
                side,
                z,
                expected * 10_000.0,
                cost * 10_000.0,
            )
            open_slots -= 1
            self._entries += 1
            changed = True
        return changed

    def _rebalance(self, ctx: StrategyContext) -> None:
        target = {symbol: 0.0 for symbol in self.symbols}
        for signal in self._signals.values():
            for symbol, weight in signal.weights.items():
                target[symbol] = target.get(symbol, 0.0) + weight
        gross = sum(abs(weight) for weight in target.values())
        if gross > float(self.p("max_gross")) > 0:
            scale = float(self.p("max_gross")) / gross
            target = {symbol: weight * scale for symbol, weight in target.items()}
            gross = float(self.p("max_gross"))
        for symbol in self.symbols:
            old = self._target.get(symbol, 0.0)
            new = target.get(symbol, 0.0)
            if abs(new - old) < 1e-4:
                continue
            ctx.target_weight(
                symbol,
                new,
                reason=(
                    f"pca_residual {symbol} target={new:+.3f} "
                    f"signals={len(self._signals)}"
                ),
            )
        self._target = target
        self._last_gross = gross
        self._dirty = False

    # -- data / model ----------------------------------------------------
    def _panel_returns(self, ctx: StrategyContext) -> list[list[float]] | None:
        need = int(self.p("window")) + 2
        closes: dict[str, dict[datetime, float]] = {}
        for symbol in self.symbols:
            rows = ctx.bars(symbol, need)
            if len(rows) < need:
                return None
            closes[symbol] = {row.ts: row.close for row in rows}
        timestamps = sorted(set.intersection(*(set(series) for series in closes.values())))
        if len(timestamps) < need:
            return None
        timestamps = timestamps[-need:]
        prices = {
            symbol: [closes[symbol][ts] for ts in timestamps] for symbol in self.symbols
        }
        rows: list[list[float]] = []
        for t in range(1, len(timestamps)):
            row = []
            for symbol in self.symbols:
                previous, current = prices[symbol][t - 1], prices[symbol][t]
                if previous <= 0 or current <= 0:
                    return None
                row.append(math.log(current / previous))
            rows.append(row)
        return rows

    def _refit(self, returns: Sequence[Sequence[float]]) -> None:
        try:
            fit = fit_panel_pca(
                returns,
                self.symbols,
                components=int(self.p("components")),
                window=int(self.p("window")),
            )
        except ValueError as exc:
            self._fit_failures += 1
            if self._fit_failures <= 3:
                self.log.warning("panel PCA fit failed: %s", exc)
            self._fit_bar = self._bar_index
            return
        self._fit = fit
        self._fit_bar = self._bar_index
        self._fit_failures = 0
        self.log.debug(
            "PCA refit at bar %d: %d components, %.1f%% variance",
            self._bar_index,
            fit.components,
            fit.explained * 100.0,
        )

    def _trade_cost_bps(self, symbol: str) -> float:
        spread = self.spread_bps.get(
            symbol.upper(),
            float(self.p("default_spread_bps")) * float(self.p("spread_multiplier")),
        )
        return spread / 2.0 + float(self.p("slippage_bps"))

    # -- observability ---------------------------------------------------
    def diagnostics(self) -> dict[str, Any]:
        fit = self._fit
        signals = {
            symbol: {
                "side": signal.side,
                "z": signal.last_z,
                "entry_z": signal.entry_z,
                "bars": self._bar_index - signal.entry_bar,
                "gross": sum(abs(value) for value in signal.weights.values()),
            }
            for symbol, signal in self._signals.items()
        }
        return {
            "ready": fit is not None,
            "model": "pca_residual",
            "components": fit.components if fit else 0,
            "explained": fit.explained if fit else 0.0,
            "window": int(self.p("window")),
            "max_legs": int(self.p("max_legs")),
            "fit_bar": self._fit_bar,
            "signals": signals,
            "active_signals": len(self._signals),
            "entries": self._entries,
            "gross": self._last_gross,
            "target_gross": sum(abs(value) for value in self._target.values()),
            "spreads_source": self.spreads_source,
        }
