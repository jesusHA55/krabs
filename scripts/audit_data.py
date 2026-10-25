#!/usr/bin/env python3
"""Audit the data behind a backtest: coverage, quality, PIT, spreads, borrow."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from krabs.borrow import load_borrow_table  # noqa: E402
from krabs.config import Config  # noqa: E402
from krabs.feeds.csv import CsvFeed  # noqa: E402
from krabs.market.calendar import EASTERN, session_days  # noqa: E402
from krabs.market.universe import PointInTimeUniverse  # noqa: E402
from krabs.spreads import SpreadTable, load_spread_table  # noqa: E402


def audit(config_path: str, *, universe_path: str | None, out: Path) -> dict:
    cfg = Config.from_file(config_path)
    if cfg.feed.provider != "csv":
        raise SystemExit(f"{config_path}: audit expects feed.provider=csv")
    bars = [
        bar
        for chunk in CsvFeed(cfg.feed.csv_path, cfg.market.timeframe).bars(
            cfg.market.symbols, cfg.market.timeframe
        )
        for bar in chunk
    ]
    if not bars:
        raise SystemExit(f"{config_path}: no bars")

    by_symbol: dict[str, list] = defaultdict(list)
    for bar in bars:
        by_symbol[bar.symbol].append(bar)
    timestamps = sorted({bar.ts for bar in bars})
    start, end = timestamps[0], timestamps[-1]
    expected_sessions = set(session_days(start.astimezone(EASTERN).date(), end.astimezone(EASTERN).date()))

    coverage = {}
    for symbol, rows in by_symbol.items():
        days = {row.ts.astimezone(EASTERN).date() for row in rows}
        coverage[symbol] = {
            "bars": len(rows),
            "sessions": len(days),
            "missing_sessions": len(expected_sessions - days),
            "duplicates": len(rows) - len(days),
        }
    missing = {
        symbol: value["missing_sessions"]
        for symbol, value in coverage.items()
        if value["missing_sessions"]
    }

    nonpositive = 0
    extreme = []
    for symbol, rows in by_symbol.items():
        previous = None
        for row in sorted(rows, key=lambda item: item.ts):
            if row.close <= 0 or row.open <= 0 or row.high <= 0 or row.low <= 0:
                nonpositive += 1
            if previous and previous > 0 and row.close > 0:
                change = abs(math.log(row.close / previous))
                if change > 0.5:
                    extreme.append((symbol, row.ts.date().isoformat(), round(change, 3)))
            previous = row.close

    warnings: list[str] = []
    if missing:
        warnings.append(f"{len(missing)} symbol(s) have missing sessions")
    if nonpositive:
        warnings.append(f"{nonpositive} bar(s) have non-positive prices")
    if extreme:
        warnings.append(f"{len(extreme)} daily move(s) exceed 50% (check adjustments)")

    if cfg.execution.spreads_csv:
        spread = load_spread_table(
            cfg.execution.spreads_csv,
            multiplier=cfg.execution.spread_multiplier,
            base=cfg.execution.spread_bps,
        )
    elif cfg.execution.spread_bps:
        spread = SpreadTable(
            static={str(k).upper(): float(v) for k, v in cfg.execution.spread_bps.items()},
            multiplier=cfg.execution.spread_multiplier,
            source="params",
        )
    else:
        spread = None
    spread_info = {"configured": bool(cfg.execution.spreads_csv or cfg.execution.spread_bps)}
    if spread is not None:
        series_dates = [point[0] for points in spread.series.values() for point in points]
        spread_info.update(
            {
                "static_symbols": len(spread.static),
                "series_symbols": len(spread.series),
                "series_start": min(series_dates).isoformat() if series_dates else None,
                "series_end": max(series_dates).isoformat() if series_dates else None,
                "multiplier": spread.multiplier,
                "missing_symbols": sorted(set(by_symbol) - set(spread.static) - set(spread.series)),
                "time_varying": bool(spread.series),
            }
        )
        if not spread.series:
            warnings.append("spreads are static per symbol; no historical time variation")
        if spread_info["missing_symbols"]:
            warnings.append(
                f"{len(spread_info['missing_symbols'])} feed symbol(s) have no spread row"
            )

    borrow_info: dict = {"configured": bool(cfg.execution.borrow_csv or cfg.execution.borrow_bps)}
    if cfg.execution.borrow_csv or cfg.execution.borrow_bps:
        borrow = load_borrow_table(
            cfg.execution.borrow_csv or "/nonexistent",
            base=cfg.execution.borrow_bps,
        )
        rates = [item.bps_per_year for item in borrow.values()]
        borrow_info.update(
            {
                "symbols": len(borrow),
                "unavailable": sum(1 for item in borrow.values() if not item.available),
                "median_bps": statistics.median(rates) if rates else 0.0,
                "max_bps": max(rates) if rates else 0.0,
                "missing_symbols": sorted(set(by_symbol) - set(borrow)),
            }
        )
        if not borrow_info["unavailable"]:
            warnings.append("no hard-to-borrow names in the borrow table")
    else:
        warnings.append("no borrow table; only execution.borrow_bps_per_year applies")

    universe_info: dict = {"configured": bool(universe_path)}
    if universe_path:
        try:
            universe = PointInTimeUniverse.from_file(universe_path)
        except FileNotFoundError:
            warnings.append(f"universe file not found: {universe_path}")
        else:
            info = universe.audit(list(by_symbol))
            info.update(
                {
                    "start_as_of": len(universe.as_of(start)),
                    "end_as_of": len(universe.as_of(end)),
                }
            )
            universe_info.update(info)
            if info["survivorship_warning"]:
                warnings.append(
                    "universe has no delisted funds; results remain survivorship-biased"
                )
            if info["missing_from_universe"]:
                warnings.append(
                    f"{len(info['missing_from_universe'])} feed symbol(s) have no listing row"
                )
    else:
        warnings.append("no point-in-time universe supplied")

    report = {
        "config": config_path,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bars": {
            "symbols": len(by_symbol),
            "rows": len(bars),
            "timestamps": len(timestamps),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "expected_sessions": len(expected_sessions),
            "missing_sessions": missing,
            "duplicates": sum(value["duplicates"] for value in coverage.values()),
            "nonpositive": nonpositive,
            "extreme_moves": extreme[:20],
        },
        "spreads": spread_info,
        "borrow": borrow_info,
        "universe": universe_info,
        "warnings": warnings,
    }

    out.mkdir(parents=True, exist_ok=True)
    (out / "audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"DATA AUDIT  {config_path}")
    print(
        f" bars      {len(by_symbol)} symbols, {len(bars):,} rows, "
        f"{len(timestamps):,} sessions, {start.date()}..{end.date()}"
    )
    print(
        f" coverage  expected {len(expected_sessions)} sessions; "
        f"missing={sum(missing.values())} across {len(missing)} symbol(s); "
        f"duplicates={report['bars']['duplicates']}"
    )
    print(
        f" quality   non-positive={nonpositive}, >50% moves={len(extreme)}"
    )
    if spread is not None:
        kind = "time series" if spread.series else "static"
        print(
            f" spreads   {kind}; static={spread_info.get('static_symbols', 0)}, "
            f"series={spread_info.get('series_symbols', 0)}, "
            f"missing={len(spread_info.get('missing_symbols', []))}"
        )
    else:
        print(" spreads   not configured")
    print(
        f" borrow    symbols={borrow_info.get('symbols', 0)}, "
        f"unavailable={borrow_info.get('unavailable', 0)}, "
        f"median={borrow_info.get('median_bps', 0):.0f} bps"
    )
    print(
        f" universe  listings={universe_info.get('listings', 0)}, "
        f"delisted={universe_info.get('delisted', 0)}, "
        f"as-of start/end={universe_info.get('start_as_of', 0)}/"
        f"{universe_info.get('end_as_of', 0)}"
    )
    print(f" warnings  {len(warnings)}")
    for warning in warnings:
        print(f"   - {warning}")
    print(f" report    {(out / 'audit.json').resolve()}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("--universe", default="data/universe.csv")
    parser.add_argument("--out", default="reports/data-audit")
    parser.add_argument("--strict", action="store_true", help="fail if warnings are present")
    args = parser.parse_args(argv)
    report = audit(args.config, universe_path=args.universe, out=Path(args.out))
    return 1 if args.strict and report["warnings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
