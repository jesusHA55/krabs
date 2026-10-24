#!/usr/bin/env python3
"""Offline parameter sweep: train/compare variants of one strategy.

    PYTHONPATH=src python3 scripts/sweep.py \
        --strategy sma_cross --grid fast=5,10,20 --grid slow=30,60,90 \
        --days 30 --seed 7

Runs every combination through the deterministic synthetic feed, ranks by
Sharpe, and writes the full table to reports/sweep-<timestamp>.json.

This is deliberately in-memory and report-optional: a sweep on a Pi can run for
hours, and you do not want 100 report directories. Use `krabs backtest` for the
winner, with real data or a CSV export.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from krabs.backtest import BacktestEngine
from krabs.config import Config
from krabs.feeds.synthetic import SyntheticFeed
from krabs.strategy import build, coerce_params, load_builtin


def parse_grid(items: list[str]) -> dict[str, list[str]]:
    grid: dict[str, list[str]] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--grid expects KEY=v1,v2,..., got {item!r}")
        key, values = item.split("=", 1)
        grid[key.strip()] = [v.strip() for v in values.split(",") if v.strip()]
    return grid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default="configs/example.toml")
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--grid", action="append", default=[], metavar="KEY=V1,V2")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    load_builtin()
    cfg = Config.from_file(args.config)
    if args.timeframe:
        cfg.market.timeframe = args.timeframe
    cfg.storage.enabled = False

    name = args.strategy or cfg.strategy.name
    base = dict(cfg.strategy.params) if name == cfg.strategy.name else {}
    grid = parse_grid(args.grid)
    keys = list(grid)
    combinations = [
        dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))
    ] or [{}]

    rows = []
    for index, combo in enumerate(combinations, 1):
        params = {**base, **coerce_params(name, combo)}
        strategy = build(name, params)
        feed = SyntheticFeed(days=args.days, seed=args.seed,
                             volatility=cfg.feed.synthetic_volatility,
                             drift=cfg.feed.synthetic_drift)
        result = BacktestEngine(cfg, strategy, feed).run()
        m = result.metrics
        row = {
            "params": params,
            "sharpe": _num(m.get("sharpe")),
            "return": _num(m.get("total_return")),
            "max_drawdown": _num(m.get("max_drawdown")),
            "trades": m.get("trades"),
            "final_equity": _num(m.get("final_equity")),
        }
        rows.append(row)
        print(
            f"[{index:>3}/{len(combinations)}] {params}  "
            f"sharpe={_fmt(row['sharpe'])} return={_pct(row['return'])} "
            f"dd={_pct(row['max_drawdown'])} trades={row['trades']}",
            flush=True,
        )

    ranked = sorted(
        rows, key=lambda r: -1e9 if r["sharpe"] is None else r["sharpe"], reverse=True
    )
    print("\n=== top by Sharpe ===")
    for row in ranked[: args.top]:
        print(
            f"{_fmt(row['sharpe']):>8}  sharp  {_pct(row['return']):>8}  ret  "
            f"{_pct(row['max_drawdown']):>8}  dd  {row['trades']:>5} trades  {row['params']}"
        )

    out = Path(args.out or f"reports/sweep-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ranked, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


def _num(value):
    if value is None or (isinstance(value, float) and value != value):
        return None
    return value


def _fmt(value) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


if __name__ == "__main__":
    sys.exit(main())
