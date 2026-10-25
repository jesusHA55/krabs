"""SQLite audit trail.

Why SQLite: zero-ops, single file, WAL mode is crash-safe, and an RPi can write
it for years on an SSD. Every order, fill, equity mark and risk event is
persisted so a post-mortem never depends on log files surviving log rotation.

Notes for SD-card installs: put this file on the SSD, keep ``synchronous=NORMAL``
and never commit during a market session from the same device.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import AccountState, Fill, Order, OrderIntent, OrderStatus, OrderType, Side, TimeInForce

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    strategy TEXT NOT NULL,
    params TEXT NOT NULL,
    config TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    initial_cash REAL NOT NULL,
    final_equity REAL
);
CREATE TABLE IF NOT EXISTS orders (
    run_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    status TEXT NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (run_id, client_id)
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS equity (
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    cash REAL NOT NULL,
    equity REAL NOT NULL,
    gross REAL NOT NULL,
    buying_power REAL NOT NULL,
    PRIMARY KEY (run_id, ts)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    topic TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_run ON fills(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, ts);
"""


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Side | OrderType | TimeInForce | OrderStatus):
        return value.value
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


class SqliteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._equity_since_commit = 0

    # -- lifecycle -------------------------------------------------------
    def start_run(
        self,
        *,
        run_id: str,
        mode: str,
        strategy: str,
        params: dict,
        config: dict,
        started_at: datetime,
        initial_cash: float,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO runs "
            "(run_id, mode, strategy, params, config, started_at, status, initial_cash) "
            "VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
            (
                run_id,
                mode,
                strategy,
                json.dumps(params, default=_json_default),
                json.dumps(config, default=_json_default),
                _iso(started_at),
                initial_cash,
            ),
        )
        self._conn.commit()

    def finish_run(self, run_id: str, status: str, final_equity: float) -> None:
        self._conn.commit()
        self._conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, final_equity = ? WHERE run_id = ?",
            (status, _iso(datetime.now(timezone.utc)), final_equity, run_id),
        )
        self._conn.commit()

    def abort_stale_runs(self, *, exclude_run_id: str) -> int:
        """Mark orphaned ``running`` rows aborted before a new run starts.

        A process killed by an operator or systemd cannot run its shutdown
        handler. Without this reconciliation the dashboard would keep showing
        old runs as running forever.
        """
        self._conn.commit()
        cursor = self._conn.execute(
            "UPDATE runs SET status = 'aborted', finished_at = ? "
            "WHERE status = 'running' AND run_id != ?",
            (_iso(datetime.now(timezone.utc)), exclude_run_id),
        )
        self._conn.commit()
        return int(cursor.rowcount)

    # -- records ---------------------------------------------------------
    def record_order(self, run_id: str, order: Order) -> None:
        payload = json.dumps(
            {
                "client_id": order.client_id,
                "symbol": order.symbol,
                "side": order.side.value,
                "qty": order.qty,
                "type": order.intent.type.value,
                "limit_price": order.intent.limit_price,
                "stop_price": order.intent.stop_price,
                "tif": order.intent.tif.value,
                "status": order.status.value,
                "filled_qty": order.filled_qty,
                "avg_price": order.avg_price,
                "commission": order.commission,
                "broker_id": order.broker_id,
                "reject_reason": order.reject_reason,
                "strategy_id": order.intent.strategy_id,
                "reason": order.intent.reason,
                "tag": order.intent.tag,
                "created_at": _iso(order.created_at),
                "updated_at": _iso(order.updated_at),
            },
            default=_json_default,
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO orders (run_id, client_id, ts, status, json) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, order.client_id, _iso(order.updated_at), order.status.value, payload),
        )
        self._conn.commit()

    def record_fill(self, run_id: str, fill: Fill) -> None:
        self._conn.execute(
            "INSERT INTO fills (run_id, order_id, ts, symbol, side, qty, price, commission) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                fill.order_id,
                _iso(fill.ts),
                fill.symbol,
                fill.side.value,
                fill.qty,
                fill.price,
                fill.commission,
            ),
        )
        self._conn.commit()

    def record_equity(self, run_id: str, account: AccountState) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO equity (run_id, ts, cash, equity, gross, buying_power) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                _iso(account.ts),
                account.cash,
                account.equity,
                account.gross_exposure,
                account.buying_power,
            ),
        )
        # Equity marks are high-frequency; orders/fills are committed immediately
        # for auditability. Batch the marks and always commit on finish/close.
        self._equity_since_commit += 1
        if self._equity_since_commit >= 100:
            self._conn.commit()
            self._equity_since_commit = 0

    def log_event(self, run_id: str, level: str, topic: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO events (run_id, ts, level, topic, payload) VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                _iso(datetime.now(timezone.utc)),
                level,
                topic,
                json.dumps(payload, default=_json_default),
            ),
        )
        self._conn.commit()

    # -- queries ---------------------------------------------------------
    def last_run(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def counts(self, run_id: str) -> dict[str, int]:
        out = {}
        for table in ("orders", "fills", "equity", "events"):
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE run_id = ?", (run_id,)
            ).fetchone()
            out[table] = int(row["n"])
        return out

    def load_orders(self, run_id: str, *, open_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT json FROM orders WHERE run_id = ?"
        if open_only:
            sql += " AND status IN ('new', 'partially_filled')"
        rows = self._conn.execute(sql + " ORDER BY ts", (run_id,)).fetchall()
        return [json.loads(row["json"]) for row in rows]

    def close(self) -> None:
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def parse_order_intent(payload: dict[str, Any]) -> OrderIntent:
    """Best-effort reconstruction for reconciliation/debugging."""
    return OrderIntent(
        symbol=payload["symbol"],
        side=Side(payload["side"]),
        qty=payload.get("qty"),
        type=OrderType(payload.get("type", "market")),
        limit_price=payload.get("limit_price"),
        stop_price=payload.get("stop_price"),
        tif=TimeInForce(payload.get("tif", "day")),
        strategy_id=payload.get("strategy_id", "manual"),
        reason=payload.get("reason", ""),
        tag=payload.get("tag", ""),
    )
