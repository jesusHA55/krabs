#!/usr/bin/env python3
"""Download a small, sector-divided daily-bar sample for `krabs backtest --csv`.

    python3 scripts/fetch_sample_data.py
    python3 scripts/fetch_sample_data.py --start 2018-01-01 --end 2026-10-23
    python3 scripts/fetch_sample_data.py --raw          # unadjusted OHLC

Source: Yahoo Finance's public chart endpoint (unofficial, no SLA). Data is
adjusted for splits and dividends by default, which is what you want for
return-based research. It is for local testing only: review Yahoo's terms
before redistributing the generated files.

The universe is a set of *current* large caps, one file per GICS-like sector.
That introduces survivorship bias by construction; this sample is for testing
the plumbing and your strategy code, not for publishing performance claims.
For survivorship-bias-free research use CRSP/Compustat, Sharadar, Norgate or
point-in-time index constituents.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from krabs.market.calendar import session_for  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc
HEADER = ("ts", "symbol", "open", "high", "low", "close", "volume")

SECTORS: dict[str, tuple[str, list[str]]] = {
    "information_technology": ("Information Technology", ["AAPL", "MSFT", "NVDA"]),
    "financials": ("Financials", ["JPM", "BAC", "GS"]),
    "health_care": ("Health Care", ["JNJ", "UNH", "PFE"]),
    "consumer_discretionary": ("Consumer Discretionary", ["AMZN", "TSLA", "HD"]),
    "consumer_staples": ("Consumer Staples", ["PG", "KO", "WMT"]),
    "energy": ("Energy", ["XOM", "CVX", "COP"]),
    "industrials": ("Industrials", ["CAT", "UNP", "HON"]),
    "materials": ("Materials", ["LIN", "SHW", "FCX"]),
    "utilities": ("Utilities", ["NEE", "DUK", "SO"]),
    "real_estate": ("Real Estate", ["PLD", "AMT", "SPG"]),
    "communication_services": ("Communication Services", ["GOOGL", "META", "DIS"]),
}
BENCHMARK = ("benchmark", "S&P 500 ETF", ["SPY"])

ETF_CLUSTERS: dict[str, list[str]] = {
    "technology": ["XLK", "SMH", "IGV", "VGT"],
    "financials": ["XLF", "KBE", "KRE", "IAI"],
    "health_care": ["XLV", "XBI", "IHI", "XPH"],
    "energy": ["XLE", "XOP", "OIH", "XES"],
    "consumer": ["XLY", "XRT", "XLP"],
    "industrials": ["XLI", "XAR", "ITA"],
    "materials": ["XLB", "XME", "GDX"],
    "utilities": ["XLU", "VPU"],
    "real_estate": ["XLRE", "IYR", "VNQ"],
    "communication": ["XLC", "XTL"],
}
ETF_GROUP = ("etf_sectors", "Sector & industry ETFs", [s for group in ETF_CLUSTERS.values() for s in group])


def fetch(symbol: str, start: int, end: int, *, adjusted: bool) -> list[tuple]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={start}&period2={end}&interval=1d"
        "&events=div%2Csplits&includeAdjustedClose=true"
    )
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Krabs sample-data fetch)"}
    )
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            result = payload["chart"]["result"][0]
            return _rows(symbol, result, adjusted=adjusted)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, KeyError, TypeError) as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{symbol}: {last_error}")


def _rows(symbol: str, result: dict, *, adjusted: bool) -> list[tuple]:
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adjclose = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
    opens, highs, lows, closes, volumes = (
        quote.get(key) or [] for key in ("open", "high", "low", "close", "volume")
    )

    by_day: dict[date, tuple] = {}
    for i, stamp in enumerate(timestamps):
        try:
            o, h, low, c = opens[i], highs[i], lows[i], closes[i]
        except IndexError:
            continue
        if None in (o, h, low, c) or c <= 0 or o <= 0:
            continue
        ratio = 1.0
        if adjusted and i < len(adjclose) and adjclose[i]:
            ratio = adjclose[i] / c
        o, h, low, c = (float(v) * ratio for v in (o, h, low, c))
        high = max(h, o, c)
        flo = min(low, o, c)
        volume = volumes[i] if i < len(volumes) and volumes[i] is not None else 0.0

        day = datetime.fromtimestamp(stamp, tz=EASTERN).date()
        session = session_for(day)
        if session is not None:
            ts = session.closes
        else:
            ts = datetime.combine(day, dtime(16, 0), tzinfo=EASTERN).astimezone(UTC)
        # Yahoo may include an in-progress daily bar for today; never backtest it.
        if day == datetime.now(EASTERN).date() and datetime.now(UTC) < ts:
            continue
        by_day[day] = (
            ts,
            symbol,
            round(o, 6),
            round(high, 6),
            round(flo, 6),
            round(c, 6),
            round(float(volume), 2),
        )
    return [by_day[day] for day in sorted(by_day)]


def write_csv(path: Path, symbols: list[str], rows: list[tuple]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(",".join(HEADER) + "\n")
        for symbol in symbols:
            for row in rows:
                if row[1] == symbol:
                    fh.write(",".join(str(value) for value in row) + "\n")
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "data" / "samples"))
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=datetime.now(UTC).date().isoformat())
    parser.add_argument("--raw", action="store_true", help="unadjusted OHLC (no dividends/splits)")
    parser.add_argument("--delay", type=float, default=0.7, help="seconds between symbols")
    args = parser.parse_args(argv)

    start = int(datetime.fromisoformat(args.start).replace(tzinfo=UTC).timestamp())
    end = int(
        (datetime.fromisoformat(args.end) + timedelta(days=1))
        .replace(tzinfo=UTC)
        .timestamp()
    )
    out_dir = Path(args.out)
    adjusted = not args.raw

    manifest = {
        "source": "Yahoo Finance chart API (daily bars)",
        "adjusted": adjusted,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "range": {"start": args.start, "end": args.end},
        "timeframe": "1Day",
        "caveats": [
            "Current large caps only: survivorship bias.",
            "Yahoo data is unofficial and has no SLA.",
            "Adjusted prices include dividends/splits; volume is unadjusted.",
            "Verify licensing before redistributing the generated CSVs.",
        ],
        "sectors": {},
        "failures": [],
    }

    groups = dict(SECTORS)
    groups[BENCHMARK[0]] = (BENCHMARK[1], BENCHMARK[2])
    groups[ETF_GROUP[0]] = (ETF_GROUP[1], ETF_GROUP[2])
    total_rows = 0
    total_symbols = 0

    for key, (name, symbols) in groups.items():
        rows: list[tuple] = []
        loaded: list[str] = []
        for symbol in symbols:
            try:
                data = fetch(symbol, start, end, adjusted=adjusted)
            except RuntimeError as exc:
                print(f"  FAIL {symbol}: {exc}")
                manifest["failures"].append({"symbol": symbol, "error": str(exc)})
                continue
            print(f"  {symbol:<6} {len(data):>5} bars")
            rows.extend(data)
            loaded.append(symbol)
            time.sleep(args.delay)
        if not rows:
            continue
        path = out_dir / f"{key}.csv"
        count = write_csv(path, loaded, rows)
        total_rows += count
        total_symbols += len(loaded)
        manifest["sectors"][key] = {
            "name": name,
            "file": path.name,
            "symbols": loaded,
            "rows": count,
        }
        print(f"{name}: {path} ({count:,} rows)")

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\n{total_symbols} symbols, {total_rows:,} rows -> {out_dir}")
    if manifest["failures"]:
        print(f"{len(manifest['failures'])} symbol(s) failed; see manifest.json")
    return 1 if manifest["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
