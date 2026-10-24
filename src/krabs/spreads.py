"""Per-symbol quoted-spread tables shared by strategies and cost models.

Spread tables are deliberately static: one value per symbol for the whole run.
They are used in two places:

* the strategy's per-leg liquidity filter, and
* the paper fill model's half-spread cost.

The CSV format is produced by ``scripts/fetch_etf_spreads.py``:

    symbol,samples,median_spread_bps,mean_spread_bps,...
    XLK,81,1.2,1.4,...

``spread_bps`` is accepted as an alternative column name.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

LOG = logging.getLogger("krabs.spreads")

_COLUMNS = ("median_spread_bps", "spread_bps")


def load_spreads(path: str | Path, *, logger: logging.Logger | None = None) -> dict[str, float]:
    """Load a symbol -> full bid/ask spread (bps) table.

    A missing file is not fatal: it returns an empty table and logs a warning,
    so a checkout without captured quotes still runs on configured estimates.
    """
    log = logger or LOG
    table_path = Path(path)
    if not table_path.is_file():
        log.warning("spread table not found: %s", table_path)
        return {}
    spreads: dict[str, float] = {}
    with table_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            for column in _COLUMNS:
                value = row.get(column)
                if value not in (None, ""):
                    try:
                        bps = float(value)
                    except ValueError:
                        log.warning("invalid %s for %s in %s: %r", column, symbol, table_path, value)
                        break
                    if bps >= 0:
                        spreads[symbol] = bps
                    break
    return spreads
