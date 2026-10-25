#!/usr/bin/env python3
"""Run cost/execution stress scenarios for one or more Krabs configs.

Levers:
  * spread multiplier (applied to both the strategy's liquidity filter and the
    paper broker's per-symbol half-spread),
  * annual short-borrow rate (charged daily on short market value),
  * extra execution delay in bars,
  * volume participation cap.

Every scenario is run over the full sample and over the final 20% (with the
configured warm-up prefix), so a strategy has to survive both.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from krabs.backtest import BacktestEngine  # noqa: E402
from krabs.config import Config  # noqa: E402
from krabs.feeds.base import InMemoryFeed  # noqa: E402
from krabs.feeds.csv import CsvFeed  # noqa: E402
from krabs.strategy import build, get, load_builtin  # noqa: E402

SCENARIOS: dict[str, dict] = {
    "baseline": {},
    "spread_2x": {"spread_multiplier": 2.0},
    "spread_3x": {"spread_multiplier": 3.0},
    "borrow_1pct": {"borrow_bps_per_year": 100.0},
    "borrow_3pct": {"borrow_bps_per_year": 300.0},
    "delay_1bar": {"delay_bars": 1},
    "participation_0.01pct": {"max_participation": 0.0001},
    "combo_2x_borrow1_delay1": {
        "spread_multiplier": 2.0,
        "borrow_bps_per_year": 100.0,
        "delay_bars": 1,
    },
    "combo_3x_borrow3_delay1": {
        "spread_multiplier": 3.0,
        "borrow_bps_per_year": 300.0,
        "delay_bars": 1,
    },
    "capacity_1m": {
        "aum": 1_000_000.0,
        "max_participation": 0.001,
        "impact_coefficient_bps": 25.0,
    },
    "capacity_5m": {
        "aum": 5_000_000.0,
        "max_participation": 0.0005,
        "impact_coefficient_bps": 50.0,
    },
    "capacity_5m_combo": {
        "aum": 5_000_000.0,
        "max_participation": 0.0005,
        "impact_coefficient_bps": 50.0,
        "spread_multiplier": 2.0,
        "borrow_bps_per_year": 100.0,
        "delay_bars": 1,
    },
}
FIELDS = (
    "strategy",
    "scenario",
    "period",
    "return",
    "sharpe",
    "max_drawdown",
    "fills",
    "rejections",
    "trades",
    "turnover",
    "exposure",
    "borrow_cost",
    "final_equity",
)


def apply_scenario(cfg: Config, overrides: dict) -> Config:
    stressed = copy.deepcopy(cfg)
    execution = stressed.execution
    if "spread_multiplier" in overrides:
        execution.spread_multiplier = float(overrides["spread_multiplier"])
    if "borrow_bps_per_year" in overrides:
        execution.borrow_bps_per_year = float(overrides["borrow_bps_per_year"])
    if "delay_bars" in overrides:
        execution.delay_bars = int(overrides["delay_bars"])
    if "max_participation" in overrides:
        execution.max_participation = float(overrides["max_participation"])
    if "impact_coefficient_bps" in overrides:
        execution.impact_coefficient_bps = float(
            overrides["impact_coefficient_bps"]
        )
    if "aum" in overrides:
        aum = float(overrides["aum"])
        stressed.wallet.starting_cash = aum
        stressed.risk.max_order_notional = max(
            stressed.risk.max_order_notional, aum * 0.25
        )
        stressed.risk.max_open_orders = max(stressed.risk.max_open_orders, 100)
    return stressed


def run_once(cfg: Config, feed: InMemoryFeed, params: dict, *, start, end, warmup, run_id: str) -> dict:
    strategy = build(cfg.strategy.name, params)
    engine = BacktestEngine(
        cfg,
        strategy,
        feed,
        run_id=run_id,
        logger=logging.getLogger("krabs.stress.run"),
    )
    result = engine.run(start=start, end=end, warmup_bars=warmup)
    metrics = result.metrics

    def finite(value, default=0.0):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else None

    return {
        "return": finite(metrics.get("total_return")),
        "sharpe": finite(metrics.get("sharpe")),
        "max_drawdown": finite(metrics.get("max_drawdown")),
        "fills": len(result.fills),
        "rejections": len(result.rejections),
        "trades": metrics.get("trades", 0),
        "turnover": finite(metrics.get("turnover")),
        "exposure": finite(metrics.get("exposure")),
        "borrow_cost": finite(metrics.get("borrow_cost")),
        "final_equity": finite(metrics.get("final_equity")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("configs", nargs="+", help="config files to stress")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=sorted(SCENARIOS),
        default=None,
        help="subset of scenarios (default: all)",
    )
    parser.add_argument("--warmup-bars", type=int, default=None)
    parser.add_argument("--out", default="reports/stress")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    load_builtin()
    names = args.scenarios or list(SCENARIOS)
    rows: list[dict] = []
    started = time.perf_counter()

    for config_path in args.configs:
        cfg = Config.from_file(config_path)
        if cfg.feed.provider != "csv":
            print(f"skip {config_path}: stress harness needs feed.provider=csv", file=sys.stderr)
            continue
        bars = [
            bar
            for chunk in CsvFeed(cfg.feed.csv_path, cfg.market.timeframe).bars(
                cfg.market.symbols, cfg.market.timeframe
            )
            for bar in chunk
        ]
        if not bars:
            print(f"skip {config_path}: no bars", file=sys.stderr)
            continue
        feed = InMemoryFeed(bars)
        bounds = feed.bounds()
        assert bounds is not None
        raw_start, end = bounds
        cut = raw_start + (end - raw_start) * 0.8
        warmup = (
            args.warmup_bars
            if args.warmup_bars is not None
            else int(cfg.feed.warmup_bars)
        )
        print(
            f"\n{cfg.strategy.name} | {config_path} | "
            f"full {raw_start:%Y-%m-%d}..{end:%Y-%m-%d} | "
            f"oos {cut:%Y-%m-%d}..{end:%Y-%m-%d} | warmup {warmup}"
        )
        print(
            f"{'scenario':26} {'full ret':>9} {'full sh':>8} "
            f"{'oos ret':>9} {'oos sh':>8} {'fills':>7} {'rej':>5} {'borrow':>10}"
        )
        for name in names:
            overrides = SCENARIOS[name]
            stressed = apply_scenario(cfg, overrides)
            params = dict(stressed.strategy.params)
            defaults = get(cfg.strategy.name).defaults
            if "spread_multiplier" in overrides and "spread_multiplier" in defaults:
                params["spread_multiplier"] = float(overrides["spread_multiplier"])
            stamp = f"{cfg.strategy.name}-{name}"
            full = run_once(
                stressed,
                feed,
                params,
                start=None,
                end=None,
                warmup=0,
                run_id=f"{stamp}-full",
            )
            oos = run_once(
                stressed,
                feed,
                params,
                start=cut,
                end=end,
                warmup=warmup,
                run_id=f"{stamp}-oos",
            )
            for period, stats in (("full", full), ("oos", oos)):
                rows.append(
                    {
                        "strategy": cfg.strategy.name,
                        "scenario": name,
                        "period": period,
                        **stats,
                    }
                )
            def pct(value):
                return f"{value*100:+7.2f}%" if value is not None else "   n/a  "

            def num(value):
                return f"{value:+7.2f}" if value is not None else "   n/a "

            print(
                f"{name:26} {pct(full['return'])} {num(full['sharpe'])} "
                f"{pct(oos['return'])} {num(oos['sharpe'])} "
                f"{full['fills']:7d} {full['rejections']:5d} "
                f"{(full['borrow_cost'] if full['borrow_cost'] is not None else 0.0):10.2f}"
            )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / f"stress-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    with (out / "results.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in FIELDS})
    print(f"\n{len(rows)} runs in {time.perf_counter() - started:.1f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
