#!/usr/bin/env python3
"""Tariff posts + Kalshi probability changes -> sector abnormal returns.

This is a case study, not a strategy. It asks whether a public post day and the
associated Kalshi probability move explain next-session sector moves.

Kalshi market: KXTARIFFBILL-27JAN01 ("Will any legislation imposing new or
increased tariffs become law before Jan 1, 2027?"). Daily candles.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from krabs.altdata.kalshi import KalshiClient  # noqa: E402
from krabs.altdata.truth import Post, classify, load_archive  # noqa: E402
from krabs.market.calendar import EASTERN  # noqa: E402

OPEN = time(9, 30)
SYMBOLS = ("XLI", "XLB", "XLF")


def load_bars(path: Path, symbols: set[str]) -> dict[str, dict[date, tuple[float, float]]]:
    data: dict[str, dict[date, tuple[float, float]]] = defaultdict(dict)
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            symbol = row["symbol"].upper()
            if symbol not in symbols:
                continue
            stamp = datetime.fromisoformat(row["ts"]).astimezone(EASTERN).date()
            data[symbol][stamp] = (float(row["open"]), float(row["close"]))
    return data


def event_day(post: Post, sessions: list[date]) -> date | None:
    local = post.ts.astimezone(EASTERN)
    index = bisect.bisect_left(sessions, local.date())
    if (
        index < len(sessions)
        and sessions[index] == local.date()
        and local.time() < OPEN
    ):
        return local.date()
    return sessions[index] if index < len(sessions) else None


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean_bps": None, "t": None}
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values)
    return {
        "n": len(values),
        "mean_bps": mean * 10_000.0,
        "t": mean / (stdev / math.sqrt(len(values))) if stdev else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", default=str(ROOT / "data/altdata/truth_archive.csv"))
    parser.add_argument("--bars", default=str(ROOT / "data/samples/etf_sectors.csv"))
    parser.add_argument("--benchmark", default=str(ROOT / "data/samples/benchmark.csv"))
    parser.add_argument("--series", default="KXTARIFFBILL")
    parser.add_argument("--market", default="KXTARIFFBILL-27JAN01")
    parser.add_argument("--start", default="2026-02-01")
    parser.add_argument("--end", default="2026-10-24")
    parser.add_argument("--out", default=str(ROOT / "reports/trump-prediction-study"))
    args = parser.parse_args(argv)

    posts = load_archive(Path(args.archive))
    posts = [
        post
        for post in posts
        if post.ts.year == 2026
        and not post.text.lower().startswith("rt @")
        and "tariffs_trade" in classify(post.content)
    ]
    bars = load_bars(Path(args.bars), set(SYMBOLS))
    bench = load_bars(Path(args.benchmark), {"SPY"}).get("SPY", {})
    sessions = sorted(bench)
    by_day: dict[date, Post] = {}
    for post in posts:
        day = event_day(post, sessions)
        if day is not None:
            by_day.setdefault(day, post)

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    series = KalshiClient().series(
        args.series, args.market, start, end, period_interval=1440
    )
    if not series.points:
        print("no Kalshi points", file=sys.stderr)
        return 1
    probabilities = {
        point.ts.astimezone(EASTERN).date(): point.probability
        for point in series.points
    }
    last_probability_day = max(probabilities)

    rows = []
    for day in sorted(by_day):
        if day > last_probability_day:
            continue
        index = bisect.bisect_left(sessions, day)
        if index < 2 or sessions[index] != day:
            continue
        previous, before = sessions[index - 1], sessions[index - 2]
        if (
            day not in probabilities
            or previous not in probabilities
            or before not in probabilities
        ):
            continue
        pre_change = probabilities[previous] - probabilities[before]
        post_change = probabilities[day] - probabilities[previous]
        for symbol in SYMBOLS:
            if day not in bars.get(symbol, {}) or day not in bench:
                continue
            etf_open, etf_close = bars[symbol][day]
            spy_open, spy_close = bench[day]
            abnormal = (etf_close / etf_open - 1.0) - (spy_close / spy_open - 1.0)
            rows.append(
                {
                    "date": day.isoformat(),
                    "symbol": symbol,
                    "pre_change": pre_change,
                    "post_change": post_change,
                    "abnormal": abnormal,
                }
            )

    if not rows:
        print("no usable events", file=sys.stderr)
        return 1
    print(
        f"tariff post-days {len(by_day)}, usable {len(rows) // len(SYMBOLS)}, "
        f"Kalshi {args.market} ({len(series.points)} points, "
        f"{min(probabilities)} → {max(probabilities)})"
    )
    print(f"{'etf':5} {'n':>4} {'abn bps':>9} {'pre corr':>9} {'post corr':>9} {'sharp n':>8} {'sharp bps':>10}")
    summary = {}
    for symbol in SYMBOLS:
        subset = [row for row in rows if row["symbol"] == symbol]
        if len(subset) < 3:
            continue
        abnormal = [row["abnormal"] for row in subset]
        pre = [row["pre_change"] for row in subset]
        post = [row["post_change"] for row in subset]
        sharp = [row for row in subset if abs(row["post_change"]) >= 0.05]
        pre_corr = statistics.correlation(pre, abnormal) if len(set(pre)) > 1 else None
        post_corr = statistics.correlation(post, abnormal) if len(set(post)) > 1 else None
        summary[symbol] = {
            **stats(abnormal),
            "pre_corr": pre_corr,
            "post_corr": post_corr,
            "sharp_n": len(sharp),
            "sharp_mean_bps": statistics.fmean(row["abnormal"] for row in sharp) * 10_000.0
            if sharp
            else None,
        }
        print(
            f"{symbol:5} {len(subset):4d} {summary[symbol]['mean_bps']:+9.2f} "
            f"{(pre_corr if pre_corr is not None else 0):+9.2f} "
            f"{(post_corr if post_corr is not None else 0):+9.2f} "
            f"{len(sharp):8d} "
            f"{(summary[symbol]['sharp_mean_bps'] if sharp else 0):+10.2f}"
        )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(
            {
                "market": args.market,
                "series": args.series,
                "rows": rows,
                "summary": summary,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "events.csv").write_text(
        "\n".join(
            [
                "date,symbol,pre_change,post_change,abnormal",
                *[
                    f"{row['date']},{row['symbol']},{row['pre_change']:.6f},"
                    f"{row['post_change']:.6f},{row['abnormal']:.8f}"
                    for row in rows
                ],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nreport: {(out / 'summary.json').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
