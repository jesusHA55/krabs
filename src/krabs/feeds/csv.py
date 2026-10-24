"""CSV feed.

Expected columns (header required, order irrelevant):

    ts,symbol,open,high,low,close,volume

``ts`` is the bar close time in ISO-8601. Naive timestamps are treated as UTC.
If the requested timeframe is larger than ``1Min``, the file must contain
1-minute bars; aggregation assumes a 1-minute source.

This is intentionally boring: export from your data vendor once, keep the file
on the Pi's SSD, and the backtest is reproducible forever.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence

from ..events import Bar, ensure_utc
from .base import group_bars, resample

REQUIRED = ("ts", "symbol", "open", "high", "low", "close", "volume")


class CsvFeed:
    def __init__(self, path: str | Path, timeframe: str = "1Min") -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"CSV feed not found: {self.path}")
        self.timeframe = timeframe

    def symbols(self) -> list[str]:
        """Unique symbols in the file, in first-seen order (used by --csv)."""
        seen: list[str] = []
        with self.path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if "symbol" not in (reader.fieldnames or ()):
                raise ValueError(f"{self.path}: missing column: symbol")
            for row in reader:
                symbol = (row["symbol"] or "").strip().upper()
                if symbol and symbol not in seen:
                    seen.append(symbol)
        return seen

    def bars(
        self,
        symbols: Sequence[str] | None = None,
        timeframe: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[list[Bar]]:
        wanted = {s.upper() for s in symbols} if symbols else None
        tf = timeframe or self.timeframe
        rows: list[Bar] = []
        with self.path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = set(REQUIRED) - set(reader.fieldnames or ())
            if missing:
                raise ValueError(f"{self.path}: missing column(s): {sorted(missing)}")
            for line, row in enumerate(reader, start=2):
                symbol = (row["symbol"] or "").strip().upper()
                if wanted is not None and symbol not in wanted:
                    continue
                try:
                    ts = ensure_utc(datetime.fromisoformat(row["ts"].strip().replace("Z", "+00:00")))
                    bar = Bar(
                        symbol=symbol,
                        ts=ts,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        timeframe=tf,
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{self.path}:{line}: {exc}") from exc
                if start and bar.ts < start:
                    continue
                if end and bar.ts > end:
                    continue
                rows.append(bar)
        if tf != "1Min":
            rows = resample(rows, tf)
        yield from group_bars(rows, tf)
