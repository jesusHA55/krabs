"""Point-in-time listing metadata for an as-of universe.

A backtest should only trade symbols that were listed on the bar date. This
module loads that metadata from a CSV:

    symbol,name,listed,delisted,cluster,source
    XLK,Technology Select Sector SPDR,1998-12-16,,technology,approximate
    OLD,Delisted Sector ETF,2005-01-03,2021-06-30,energy,vendor

Dates are optional. Empty ``delisted`` means still listed as of the data end.
The sample file in ``data/universe.csv`` uses approximate inception dates and
contains **no dead funds**, so it cannot remove survivorship bias; it is a
schema and audit aid, not a vendor PIT dataset.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Listing:
    symbol: str
    name: str = ""
    listed: date = date.min
    delisted: date | None = None
    cluster: str = ""
    source: str = ""

    def active_on(self, day: date) -> bool:
        return self.listed <= day and (self.delisted is None or day < self.delisted)


def _parse_day(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


@dataclass(frozen=True)
class PointInTimeUniverse:
    listings: dict[str, Listing]

    @classmethod
    def from_file(cls, path: str | Path) -> PointInTimeUniverse:
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(f"universe file not found: {target}")
        listings: dict[str, Listing] = {}
        with target.open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                symbol = (row.get("symbol") or "").strip().upper()
                if not symbol:
                    continue
                listed = _parse_day(row.get("listed")) or date.min
                listings[symbol] = Listing(
                    symbol=symbol,
                    name=(row.get("name") or "").strip(),
                    listed=listed,
                    delisted=_parse_day(row.get("delisted")),
                    cluster=(row.get("cluster") or "").strip(),
                    source=(row.get("source") or "").strip(),
                )
        return cls(listings)

    def as_of(self, day: date | datetime) -> set[str]:
        target = _as_date(day)
        return {
            symbol
            for symbol, listing in self.listings.items()
            if listing.active_on(target)
        }

    def audit(self, symbols: list[str] | set[str]) -> dict:
        wanted = {symbol.upper() for symbol in symbols}
        listed = set(self.listings)
        dead = [listing for listing in self.listings.values() if listing.delisted]
        missing = sorted(wanted - listed)
        extra = sorted(listed - wanted)
        return {
            "listings": len(self.listings),
            "delisted": len(dead),
            "missing_from_universe": missing,
            "universe_not_in_feed": extra,
            "survivorship_warning": len(dead) == 0,
        }
