#!/usr/bin/env python3
"""Fetch a short 5-minute sample for the overnight-drift prototype.

Yahoo limits 5-minute history to about 60 days. This is enough to validate
execution logic, not to validate the strategy. For a real test use a vendor
with multi-year minute bars.

    python3 scripts/fetch_intraday_sample.py
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from krabs.market.calendar import EASTERN, session_for  # noqa: E402

HEADER = ("ts", "symbol", "open", "high", "low", "close", "volume")
SYMBOLS = ("SPY", "SMH", "XME", "XOP")


def fetch(symbol: str, interval: str, span: str) -> list[tuple]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval={interval}&range={span}&includePrePost=false"
    )
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Krabs overnight prototype)"}
    )
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            result = payload["chart"]["result"][0]
            timestamps = result.get("timestamp") or []
            quote = (result.get("indicators") or {}).get("quote", [{}])[0]
            rows = []
            step = timedelta(minutes=5 if interval == "5m" else 1)
            for index, stamp in enumerate(timestamps):
                try:
                    o = quote["open"][index]
                    h = quote["high"][index]
                    low = quote["low"][index]
                    c = quote["close"][index]
                    volume = quote["volume"][index] or 0
                except (KeyError, IndexError, TypeError):
                    continue
                if None in (o, h, low, c) or c <= 0:
                    continue
                # Yahoo timestamps mark the start of the interval; Krabs bars
                # are stamped at the close.
                ts = datetime.fromtimestamp(stamp, tz=timezone.utc) + step
                session = session_for(ts.astimezone(EASTERN).date())
                if session is None or not (session.opens < ts <= session.closes):
                    continue
                rows.append(
                    (
                        ts,
                        symbol,
                        round(float(o), 6),
                        round(float(h), 6),
                        round(float(low), 6),
                        round(float(c), 6),
                        round(float(volume), 2),
                    )
                )
            return rows
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, KeyError, TypeError) as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{symbol}: {last_error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--interval", default="5m", choices=("1m", "5m"))
    parser.add_argument("--range", dest="span", default="60d")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--out", default=str(ROOT / "data" / "samples" / "overnight_5m.csv"))
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    all_rows: list[tuple] = []
    for symbol in symbols:
        try:
            rows = fetch(symbol, args.interval, args.span)
        except RuntimeError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1
        print(f"{symbol:<5} {len(rows):>6} bars")
        all_rows.extend(rows)
        time.sleep(args.delay)
    if not all_rows:
        print("no bars", file=sys.stderr)
        return 1
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for symbol in symbols:
            for row in all_rows:
                if row[1] == symbol:
                    writer.writerow(row)
    print(f"wrote {target} ({len(all_rows):,} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
