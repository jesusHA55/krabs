#!/usr/bin/env python3
"""Event study: Trump Truth Social posts -> sector ETF abnormal returns.

This is the first gate for a "Trump index": does a public post predict a
sector move at a tradable horizon? It is not a strategy.

    python3 scripts/trump_event_study.py

Data:
  * public archive: data/altdata/truth_archive.csv
    (download: https://ix.cnn.io/data/truth-social/truth_archive.csv)
  * daily ETF bars: data/samples/etf_sectors.csv
  * SPY benchmark: data/samples/benchmark.csv
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

from krabs.altdata.truth import Post, classify, load_archive  # noqa: E402
from krabs.market.calendar import EASTERN  # noqa: E402

TOPIC_ETFS: dict[str, list[str]] = {
    "tariffs_trade": ["XLI", "XLB", "XLF", "XLE", "XRT", "XLP"],
    "energy": ["XLE", "XOP", "OIH", "XES"],
    "defense": ["ITA", "XAR"],
    "pharma": ["XLV", "XBI", "IHI", "XPH"],
    "banks_fed": ["XLF", "KRE", "KBE", "IAI"],
    "tech_china": ["XLK", "SMH", "IGV", "VGT"],
    "immigration": ["XLI", "XLP", "XRT"],
}
OPEN = time(9, 30)


def load_daily(path: Path, symbols: set[str]) -> dict[str, dict[date, tuple[float, float]]]:
    data: dict[str, dict[date, tuple[float, float]]] = defaultdict(dict)
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            symbol = row["symbol"].upper()
            if symbol not in symbols:
                continue
            ts = datetime.fromisoformat(row["ts"]).astimezone(EASTERN)
            data[symbol][ts.date()] = (float(row["open"]), float(row["close"]))
    return data


def event_day(post: Post, sessions: list[date]) -> date | None:
    local = post.ts.astimezone(EASTERN)
    day = local.date()
    index = bisect.bisect_left(sessions, day)
    if index < len(sessions) and sessions[index] == day and local.time() < OPEN:
        return day
    # During or after the session, or on a non-session day: next session.
    return sessions[index] if index < len(sessions) else None


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean_bps": None, "t": None, "hit": None}
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values)
    return {
        "n": len(values),
        "mean_bps": mean * 10_000.0,
        "t": mean / (stdev / math.sqrt(len(values))) if stdev else None,
        "hit": sum(1 for value in values if value > 0) / len(values),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", default=str(ROOT / "data/altdata/truth_archive.csv"))
    parser.add_argument("--bars", default=str(ROOT / "data/samples/etf_sectors.csv"))
    parser.add_argument("--benchmark", default=str(ROOT / "data/samples/benchmark.csv"))
    parser.add_argument("--split", default="2025-01-01")
    parser.add_argument("--min-events", type=int, default=100)
    parser.add_argument("--out", default=str(ROOT / "reports/trump-event-study"))
    args = parser.parse_args(argv)

    archive = Path(args.archive)
    if not archive.is_file():
        print(f"missing archive: {archive}", file=sys.stderr)
        return 2
    posts = load_archive(archive)
    posts = [post for post in posts if not post.text.lower().startswith("rt @")]
    print(f"posts {len(posts):,}: {posts[0].ts.date()} → {posts[-1].ts.date()}")

    symbols = {symbol for group in TOPIC_ETFS.values() for symbol in group} | {"SPY"}
    bars = load_daily(Path(args.bars), symbols)
    bench = load_daily(Path(args.benchmark), {"SPY"}).get("SPY", {})
    sessions = sorted(bench)
    if not sessions:
        print("no benchmark sessions", file=sys.stderr)
        return 1
    split = date.fromisoformat(args.split)

    # Deduplicate to one event per topic per session.
    events: dict[str, list[tuple[date, Post]]] = defaultdict(list)
    seen: dict[str, set[date]] = defaultdict(set)
    for post in posts:
        topics = classify(post.content)
        if not topics:
            continue
        day = event_day(post, sessions)
        if day is None:
            continue
        for topic in topics:
            if day in seen[topic]:
                continue
            seen[topic].add(day)
            events[topic].append((day, post))

    rows = []
    for topic, topic_events in sorted(events.items()):
        for symbol in TOPIC_ETFS.get(topic, []):
            etf = bars.get(symbol, {})
            one_day: list[float] = []
            five_day: list[float] = []
            for day, _post in topic_events:
                index = bisect.bisect_left(sessions, day)
                if index >= len(sessions) or sessions[index] != day:
                    continue
                if day not in etf or day not in bench:
                    continue
                etf_open, etf_close = etf[day]
                spy_open, spy_close = bench[day]
                if etf_open <= 0 or spy_open <= 0:
                    continue
                one_day.append((etf_close / etf_open - 1.0) - (spy_close / spy_open - 1.0))
                exit_index = index + 5
                if exit_index < len(sessions):
                    exit_day = sessions[exit_index]
                    if exit_day in etf and exit_day in bench:
                        five_day.append(
                            (etf[exit_day][1] / etf_close - 1.0)
                            - (bench[exit_day][1] / spy_close - 1.0)
                        )
            if len(one_day) < args.min_events:
                continue
            train = []
            test = []
            for day, _post in topic_events:
                if day not in etf or day not in bench:
                    continue
                value = (etf[day][1] / etf[day][0] - 1.0) - (bench[day][1] / bench[day][0] - 1.0)
                (train if day < split else test).append(value)
            rows.append(
                {
                    "topic": topic,
                    "symbol": symbol,
                    **{f"one_{key}": value for key, value in stats(one_day).items()},
                    **{f"five_{key}": value for key, value in stats(five_day).items()},
                    **{f"train_{key}": value for key, value in stats(train).items()},
                    **{f"test_{key}": value for key, value in stats(test).items()},
                }
            )

    rows.sort(key=lambda row: abs(row["one_t"] or 0), reverse=True)
    print(
        f"\n{'topic':16} {'etf':5} {'n':>5} {'1d bps':>8} {'1d t':>6} "
        f"{'5d bps':>8} {'train':>8} {'test':>8}"
    )
    for row in rows:
        print(
            f"{row['topic']:16} {row['symbol']:5} {row['one_n']:5d} "
            f"{(row['one_mean_bps'] if row['one_mean_bps'] is not None else 0):+8.2f} "
            f"{(row['one_t'] if row['one_t'] is not None else 0):+6.2f} "
            f"{(row['five_mean_bps'] if row['five_mean_bps'] is not None else 0):+8.2f} "
            f"{(row['train_mean_bps'] if row['train_mean_bps'] is not None else 0):+8.2f} "
            f"{(row['test_mean_bps'] if row['test_mean_bps'] is not None else 0):+8.2f}"
        )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "study.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    fields = list(rows[0]) if rows else ["topic", "symbol"]
    with (out / "study.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nreport: {(out / 'study.json').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
