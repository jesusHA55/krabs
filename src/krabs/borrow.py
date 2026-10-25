"""Short-borrow rate and availability tables.

File format (all values are annual):

    symbol,borrow_bps_per_year,available
    XLK,40,true
    XTL,350,true
    TINY,2500,false

``available=false`` is reported by the data audit. The simulated engine still
charges the rate (so an unavailable name is expensive), but it does not yet
block a short entry; that belongs in a live borrow/locate check.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("krabs.borrow")


@dataclass(frozen=True, slots=True)
class BorrowRate:
    bps_per_year: float
    available: bool = True


def load_borrow_table(
    path: str | Path,
    *,
    base: dict[str, float] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, BorrowRate]:
    log = logger or LOG
    table: dict[str, BorrowRate] = {
        str(symbol).upper(): BorrowRate(float(bps)) for symbol, bps in (base or {}).items()
    }
    target = Path(path)
    if not target.is_file():
        log.warning("borrow table not found: %s", target)
        return table
    with target.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            raw = row.get("borrow_bps_per_year") or row.get("borrow_bps")
            try:
                bps = float(raw) if raw not in (None, "") else 0.0
            except ValueError:
                log.warning("invalid borrow rate for %s: %r", symbol, raw)
                continue
            available = str(row.get("available", "true")).strip().lower() not in (
                "0",
                "false",
                "no",
                "n",
            )
            table[symbol] = BorrowRate(max(0.0, bps), available)
    return table
