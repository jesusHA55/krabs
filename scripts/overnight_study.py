#!/usr/bin/env python3
"""Measure overnight (close-to-open) and intraday drift, net of costs.

This is a *study*, not a backtest: it uses daily opens and closes and assumes
you can transact at the close and the next open. Use it to decide whether a
strategy is worth implementing with intraday execution; it cannot prove that
the close or open auction is attainable.

    python3 scripts/overnight_study.py
    python3 scripts/overnight_study.py --csv data/samples/dual_momentum.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPREADS = {"SPY": 1.0, "VEU": 3.0, "AGG": 1.5, "BIL": 1.0}
FIELDS = (
    "symbol",
    "n",
    "overnight_bps",
    "overnight_ann_pct",
    "t_stat",
    "intraday_bps",
    "round_trip_cost_bps",
    "net_ann_pct",
)


def load_bars(path: Path) -> dict[str, list[tuple[datetime, float, float]]]:
    rows: dict[str, list[tuple[datetime, float, float]]] = defaultdict(list)
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            rows[row["symbol"]].append(
                (
                    datetime.fromisoformat(row["ts"]),
                    float(row["open"]),
                    float(row["close"]),
                )
            )
    for values in rows.values():
        values.sort()
    return rows


def load_spreads(path: Path | None, fallback: float) -> dict[str, float]:
    spreads: dict[str, float] = dict(DEFAULT_SPREADS)
    if path and path.is_file():
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    spreads[row["symbol"]] = float(
                        row.get("median_spread_bps") or row.get("spread_bps")
                    )
                except (TypeError, ValueError):
                    continue
    return {symbol: spreads.get(symbol, fallback) for symbol in spreads}


def study(
    bars: dict[str, list[tuple[datetime, float, float]]],
    spreads: dict[str, float],
    *,
    slippage_bps: float,
    default_spread_bps: float,
) -> list[dict]:
    result = []
    for symbol, values in sorted(bars.items()):
        overnight: list[float] = []
        intraday: list[float] = []
        for previous, current in zip(values, values[1:]):
            prev_close = previous[2]
            open_price = current[1]
            close_price = current[2]
            if prev_close <= 0 or open_price <= 0:
                continue
            overnight.append(open_price / prev_close - 1.0)
            intraday.append(close_price / open_price - 1.0)
        if len(overnight) < 100:
            continue
        mean = statistics.fmean(overnight)
        stdev = statistics.pstdev(overnight)
        spread = spreads.get(symbol, default_spread_bps)
        round_trip = 2.0 * (spread / 2.0 + slippage_bps)
        result.append(
            {
                "symbol": symbol,
                "n": len(overnight),
                "overnight_bps": mean * 10_000.0,
                "overnight_ann_pct": mean * 252.0 * 100.0,
                "t_stat": mean / (stdev / math.sqrt(len(overnight))) if stdev else 0.0,
                "intraday_bps": statistics.fmean(intraday) * 10_000.0,
                "round_trip_cost_bps": round_trip,
                "net_ann_pct": (mean - round_trip / 10_000.0) * 252.0 * 100.0,
            }
        )
    result.sort(key=lambda row: row["net_ann_pct"], reverse=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=str(ROOT / "data/samples/etf_sectors.csv"))
    parser.add_argument("--spreads", default=str(ROOT / "data/samples/etf_spreads.csv"))
    parser.add_argument("--symbols", help="comma-separated subset")
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--default-spread-bps", type=float, default=2.0)
    parser.add_argument("--out", default=str(ROOT / "reports/overnight-study"))
    args = parser.parse_args(argv)

    path = Path(args.csv)
    if not path.is_file():
        print(f"missing CSV: {path}", file=sys.stderr)
        return 2
    bars = load_bars(path)
    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(",")}
        bars = {symbol: values for symbol, values in bars.items() if symbol in wanted}
    spreads = load_spreads(Path(args.spreads), args.default_spread_bps)
    rows = study(
        bars,
        spreads,
        slippage_bps=args.slippage_bps,
        default_spread_bps=args.default_spread_bps,
    )
    if not rows:
        print("no symbols with enough observations", file=sys.stderr)
        return 1

    print(
        f"{'symbol':8} {'n':>5} {'overnight':>10} {'ann':>8} "
        f"{'t':>5} {'cost RT':>8} {'net ann':>8} {'intraday':>9}"
    )
    for row in rows:
        print(
            f"{row['symbol']:8} {row['n']:5d} {row['overnight_bps']:9.2f}b "
            f"{row['overnight_ann_pct']:7.2f}% {row['t_stat']:5.1f} "
            f"{row['round_trip_cost_bps']:7.1f}b {row['net_ann_pct']:7.2f}% "
            f"{row['intraday_bps']:8.2f}b"
        )
    med = statistics.median(row["net_ann_pct"] for row in rows)
    positive = sum(1 for row in rows if row["net_ann_pct"] > 0)
    print(f"\nmedian net annual {med:+.2f}%; positive {positive}/{len(rows)}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "study.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    with (out / "study.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"report: {(out / 'study.json').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
