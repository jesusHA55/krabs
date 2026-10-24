#!/usr/bin/env python3
"""Capture observed ETF bid/ask spreads from Alpaca and write a spread table.

This is the data step for the ETF-pairs liquidity filter. It samples one second
around the close of the last trading session of each month, using Alpaca's
historical SIP quotes where permitted, and writes the median spread per symbol.

    export ALPACA_API_KEY=...
    export ALPACA_SECRET_KEY=...
    python3 scripts/fetch_etf_spreads.py --start 2020-01-01

The generated CSV is a *static* per-symbol spread estimate for backtesting. It
is not a historical spread time series. For live trading, use live quotes.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from krabs.market.calendar import session_days, session_for  # noqa: E402
from krabs.pearl.screening import DEFAULT_ETF_CLUSTERS  # noqa: E402

UTC = timezone.utc
DATA_URL = "https://data.alpaca.markets/v2/stocks/quotes"
HEADERS = ("symbol", "samples", "median_spread_bps", "mean_spread_bps", "last_bid", "last_ask", "last_spread_bps")


def months(start: date, end: date):
    current = date(start.year, start.month, 1)
    while current <= end:
        if current.month == 12:
            nxt = date(current.year + 1, 1, 1)
        else:
            nxt = date(current.year, current.month + 1, 1)
        yield current, nxt - timedelta(days=1)
        current = nxt


def api_get(url: str, key: str, secret: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        },
    )
    last_error = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 429 or exc.code >= 500:
                last_error = f"HTTP {exc.code}: {body}"
                time.sleep(2.0 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"request failed after retries: {last_error}")


def sample_spreads(
    key: str,
    secret: str,
    *,
    symbols: list[str],
    start: date,
    end: date,
    feed: str,
    sleep: float,
) -> tuple[list[dict], list[str]]:
    per_symbol: dict[str, list[float]] = {symbol: [] for symbol in symbols}
    last_quote: dict[str, tuple[float, float, float]] = {}
    failures: list[str] = []
    total = 0

    for first, last in months(start, end):
        candidates = session_days(first, last)[-5:]
        if not candidates:
            continue
        quotes = {}
        chosen = None
        for day in reversed(candidates):
            session = session_for(day)
            if session is None:
                continue
            # One second around the close. Some symbols will not print in this
            # tiny window; try up to five seconds, then move to the prior day.
            for width in (1, 5):
                end_ts = session.closes
                start_ts = end_ts - timedelta(seconds=width)
                for attempt_feed in (feed, "iex"):
                    params = {
                        "symbols": ",".join(symbols),
                        "start": start_ts.isoformat().replace("+00:00", "Z"),
                        "end": end_ts.isoformat().replace("+00:00", "Z"),
                        "limit": "10000",
                        "feed": attempt_feed,
                        "sort": "asc",
                    }
                    try:
                        payload = api_get(
                            f"{DATA_URL}?{urllib.parse.urlencode(params)}", key, secret
                        )
                    except RuntimeError as exc:
                        # Free plans can query old SIP history but not the most
                        # recent SIP window; IEX is the real-time fallback.
                        if attempt_feed != "iex" and "403" in str(exc):
                            continue
                        failures.append(f"{first:%Y-%m} {attempt_feed}: {exc}")
                        payload = {}
                    quotes = payload.get("quotes") or {}
                    if quotes:
                        chosen = day
                        break
                    time.sleep(sleep)
                if chosen is not None:
                    break
            if chosen is not None:
                break
        if chosen is None:
            failures.append(f"{first:%Y-%m}: no quotes")
            continue

        month_values = 0
        for symbol, rows in quotes.items():
            spreads = []
            for row in rows:
                try:
                    bid = float(row["bp"])
                    ask = float(row["ap"])
                except (KeyError, TypeError, ValueError):
                    continue
                if bid <= 0 or ask <= bid:
                    continue
                mid = (bid + ask) / 2.0
                spreads.append((ask - bid) / mid * 10_000.0)
            if not spreads:
                continue
            value = statistics.median(spreads)
            per_symbol.setdefault(symbol, []).append(value)
            last = rows[-1]
            last_quote[symbol] = (
                float(last["bp"]),
                float(last["ap"]),
                value,
            )
            month_values += 1
        total += 1
        print(f"{first:%Y-%m}: {month_values} symbol(s), day={chosen}", flush=True)
        time.sleep(sleep)

    rows = []
    for symbol, values in sorted(per_symbol.items()):
        if not values:
            continue
        last_bid, last_ask, last_spread = last_quote.get(symbol, (0.0, 0.0, 0.0))
        rows.append(
            {
                "symbol": symbol,
                "samples": len(values),
                "median_spread_bps": statistics.median(values),
                "mean_spread_bps": statistics.fmean(values),
                "last_bid": last_bid,
                "last_ask": last_ask,
                "last_spread_bps": last_spread,
            }
        )
    print(f"sampled {total} month(s); {len(rows)} symbol(s)")
    return rows, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--out", default=str(ROOT / "data" / "samples" / "etf_spreads.csv"))
    parser.add_argument("--feed", default="sip")
    parser.add_argument("--sleep", type=float, default=0.35)
    args = parser.parse_args(argv)

    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        print("set ALPACA_API_KEY and ALPACA_SECRET_KEY", file=sys.stderr)
        return 2
    symbols = sorted({s for group in DEFAULT_ETF_CLUSTERS.values() for s in group})
    rows, failures = sample_spreads(
        key,
        secret,
        symbols=symbols,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        feed=args.feed,
        sleep=args.sleep,
    )
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key_: (f"{value:.4f}" if isinstance(value, float) else value)
                    for key_, value in row.items()
                }
            )
    print(f"wrote {target}")
    for failure in failures:
        print(f"WARN {failure}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
