"""Read-only data aggregation for the Krabs dashboard.

The dashboard is a separate process: it never touches the trading runtime's
memory. It reads the heartbeat file, the SQLite audit trail, the kill switch and
the report directories, so it works even when the bot is stopped or crashed.
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import Config
from ..risk.kill import KillSwitch
from ..strategy import describe, load_builtin

MAX_EQUITY_POINTS = 600
MAX_RUNS = 30
MAX_ROWS = 50
MAX_REPORTS = 50


class DashboardData:
    def __init__(
        self,
        cfg: Config,
        *,
        reports_dir: str | Path | None = None,
    ) -> None:
        load_builtin()
        self.cfg = cfg
        self.data_dir = Path(cfg.app.data_dir)
        self.heartbeat_path = self.data_dir / "heartbeat.json"
        self.kill = KillSwitch(self.data_dir / "KILL")
        self.db_path = Path(cfg.storage.path)
        self.reports_dir = Path(reports_dir or cfg.backtest.report_dir)
        self._reports_cache: list[dict[str, Any]] = []
        self._reports_cache_time = 0.0
        self._reports_lock = threading.Lock()

    # -- public API ------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        warnings: list[str] = []
        heartbeat, age = self._read_heartbeat()
        stale = bool(
            age is not None
            and age > max(30.0, 3.0 * max(1.0, self.cfg.feed.poll_seconds))
        )
        if stale:
            warnings.append(f"heartbeat is {age:.0f}s old")
        if heartbeat and heartbeat.get("stopped"):
            warnings.append("runtime is stopped")

        kill = self._kill_state()
        if kill["engaged"]:
            warnings.append("kill switch is engaged")

        db = self._read_db(heartbeat, warnings)
        reports = self._read_reports(warnings)
        account = self._account(heartbeat, db)
        positions = self._positions(heartbeat, account)
        health = self._health(age, stale, db, warnings)

        return {
            "generated_at": _now_iso(),
            "version": __version__,
            "config": self._config_summary(),
            "heartbeat": heartbeat,
            "heartbeat_age_s": age,
            "heartbeat_stale": stale,
            "kill": kill,
            "account": account,
            "positions": positions,
            "equity": {
                "run_id": db["run_id"],
                "initial_cash": db["initial_cash"],
                "points": db["equity_points"],
            },
            "orders": db["orders"],
            "fills": db["fills"],
            "runs": db["runs"],
            "events": db["events"],
            "reports": reports,
            "strategies": [
                {"id": item.id, "description": item.description, "defaults": item.defaults}
                for item in describe()
            ],
            "health": health,
            "warnings": warnings,
        }

    # -- heartbeat / kill ------------------------------------------------
    def _read_heartbeat(self) -> tuple[dict[str, Any] | None, float | None]:
        try:
            payload = json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None, None
            ts = _parse_ts(payload.get("ts"))
            age = (datetime.now(timezone.utc) - ts).total_seconds() if ts else None
            return payload, age
        except (OSError, ValueError):
            return None, None

    def _kill_state(self) -> dict[str, Any]:
        engaged = self.kill.engaged
        modified = None
        try:
            modified = datetime.fromtimestamp(
                self.kill.path.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except OSError:
            pass
        return {"engaged": engaged, "reason": self.kill.reason(), "modified": modified}

    # -- database --------------------------------------------------------
    def _connect(self) -> sqlite3.Connection | None:
        if not self.cfg.storage.enabled or not self.db_path.is_file():
            return None
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _read_db(
        self, heartbeat: dict[str, Any] | None, warnings: list[str]
    ) -> dict[str, Any]:
        empty = {
            "db_state": "disabled" if not self.cfg.storage.enabled else "missing",
            "db_error": "",
            "runs": [],
            "run_id": None,
            "initial_cash": None,
            "equity_points": [],
            "orders": [],
            "fills": [],
            "events": [],
        }
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            warnings.append(f"cannot open audit database: {exc}")
            return {**empty, "db_state": "error", "db_error": str(exc)}
        if conn is None:
            return empty

        try:
            runs = [
                self._run_row(row)
                for row in conn.execute(
                    "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (MAX_RUNS,)
                )
            ]
            run_ids = {run["run_id"] for run in runs}
            heartbeat_run = (heartbeat or {}).get("run_id")
            selected = heartbeat_run if heartbeat_run in run_ids else None

            equity_points: list[dict[str, Any]] = []
            chart_run = selected
            if chart_run is None:
                for run in runs:
                    points = self._equity(conn, run["run_id"])
                    if points:
                        chart_run = run["run_id"]
                        equity_points = points
                        break
            else:
                equity_points = self._equity(conn, chart_run)
                if not equity_points:
                    for run in runs:
                        points = self._equity(conn, run["run_id"])
                        if points:
                            chart_run = run["run_id"]
                            equity_points = points
                            break
            if chart_run is None and runs:
                chart_run = runs[0]["run_id"]

            selected_run = next((r for r in runs if r["run_id"] == chart_run), None)
            initial_cash = selected_run["initial_cash"] if selected_run else None

            orders = self._orders(conn, chart_run) if chart_run else []
            fills = self._fills(conn, chart_run) if chart_run else []
            events = self._events(conn, chart_run) if chart_run else []

            return {
                "db_state": "ok",
                "db_error": "",
                "runs": runs,
                "run_id": chart_run,
                "initial_cash": initial_cash,
                "equity_points": _downsample(equity_points),
                "orders": orders,
                "fills": fills,
                "events": events,
            }
        except sqlite3.Error as exc:
            warnings.append(f"audit database error: {exc}")
            return {**empty, "db_state": "error", "db_error": str(exc)}
        finally:
            conn.close()

    def _run_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            params = json.loads(row["params"])
        except (TypeError, ValueError):
            params = {}
        return {
            "run_id": row["run_id"],
            "mode": row["mode"],
            "strategy": row["strategy"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "initial_cash": _number(row["initial_cash"]),
            "final_equity": _number(row["final_equity"]),
            "params": params,
        }

    def _equity(self, conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM ("
            "  SELECT ts, equity, cash, gross, buying_power FROM equity"
            "  WHERE run_id = ? ORDER BY ts DESC LIMIT 4000"
            ") ORDER BY ts ASC",
            (run_id,),
        ).fetchall()
        return [
            {
                "ts": row["ts"],
                "equity": _number(row["equity"], 0.0),
                "cash": _number(row["cash"], 0.0),
                "gross": _number(row["gross"], 0.0),
                "buying_power": _number(row["buying_power"], 0.0),
            }
            for row in rows
        ]

    def _orders(self, conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT json, ts FROM orders WHERE run_id = ? ORDER BY ts DESC LIMIT ?",
            (run_id, MAX_ROWS),
        ).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row["json"])
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "client_id": payload.get("client_id", ""),
                    "ts": row["ts"],
                    "symbol": payload.get("symbol", ""),
                    "side": payload.get("side", ""),
                    "qty": _number(payload.get("qty"), 0.0),
                    "type": payload.get("type", ""),
                    "status": payload.get("status", ""),
                    "filled_qty": _number(payload.get("filled_qty"), 0.0),
                    "avg_price": _number(payload.get("avg_price"), 0.0),
                    "commission": _number(payload.get("commission"), 0.0),
                    "reason": payload.get("reason", ""),
                    "tag": payload.get("tag", ""),
                }
            )
        return out

    def _fills(self, conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM fills WHERE run_id = ? ORDER BY ts DESC LIMIT ?",
            (run_id, MAX_ROWS),
        ).fetchall()
        return [
            {
                "order_id": row["order_id"],
                "ts": row["ts"],
                "symbol": row["symbol"],
                "side": row["side"],
                "qty": _number(row["qty"], 0.0),
                "price": _number(row["price"], 0.0),
                "commission": _number(row["commission"], 0.0),
            }
            for row in rows
        ]

    def _events(self, conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY ts DESC LIMIT 30",
            (run_id,),
        ).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                payload = {}
            out.append(
                {
                    "ts": row["ts"],
                    "level": row["level"],
                    "topic": row["topic"],
                    "payload": payload,
                }
            )
        return out

    # -- account / positions ---------------------------------------------
    def _account(
        self, heartbeat: dict[str, Any] | None, db: dict[str, Any]
    ) -> dict[str, Any] | None:
        initial = db.get("initial_cash") or self.cfg.wallet.starting_cash
        if heartbeat and heartbeat.get("equity") is not None:
            equity = _number(heartbeat.get("equity"), 0.0)
            start = _number(heartbeat.get("start_equity"), initial)
            return {
                "source": "heartbeat",
                "equity": equity,
                "cash": _number(heartbeat.get("cash"), 0.0),
                "buying_power": _number(heartbeat.get("buying_power"), 0.0),
                "start_equity": start,
                "total_pnl": equity - start,
                "total_return": (equity / start - 1.0) if start else None,
                "day_start_equity": _nullable(heartbeat.get("day_start_equity")),
                "day_pnl": _nullable(heartbeat.get("day_pnl")),
                "day_return": _nullable(heartbeat.get("day_return")),
                "gross_exposure": _number(heartbeat.get("gross_exposure"), 0.0),
                "realized_pnl": _nullable(heartbeat.get("realized_pnl")),
                "unrealized_pnl": _nullable(heartbeat.get("unrealized_pnl")),
                "long_value": _nullable(heartbeat.get("long_value")),
                "short_value": _nullable(heartbeat.get("short_value")),
                "ts": heartbeat.get("ts"),
            }
        points = db.get("equity_points") or []
        if not points:
            return None
        last = points[-1]
        start = initial
        return {
            "source": "database",
            "equity": last["equity"],
            "cash": last["cash"],
            "buying_power": last.get("buying_power", 0.0),
            "start_equity": start,
            "total_pnl": last["equity"] - start if start else None,
            "total_return": (last["equity"] / start - 1.0) if start else None,
            "day_start_equity": None,
            "day_pnl": None,
            "day_return": None,
            "gross_exposure": last.get("gross", 0.0),
            "realized_pnl": None,
            "unrealized_pnl": None,
            "long_value": None,
            "short_value": None,
            "ts": last["ts"],
        }

    def _positions(
        self, heartbeat: dict[str, Any] | None, account: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        if not heartbeat:
            return []
        equity = (account or {}).get("equity") or 0.0
        out = []
        for symbol, raw in (heartbeat.get("positions") or {}).items():
            qty = _number(raw.get("qty"), 0.0)
            avg = _number(raw.get("avg"), 0.0)
            last = _number(raw.get("last"), avg) or avg
            value = qty * last
            out.append(
                {
                    "symbol": symbol,
                    "qty": qty,
                    "avg": avg,
                    "last": last,
                    "value": value,
                    "unrealized": (last - avg) * qty,
                    "weight": value / equity if equity else 0.0,
                }
            )
        out.sort(key=lambda p: abs(p["value"]), reverse=True)
        return out

    # -- reports ---------------------------------------------------------
    def _read_reports(self, warnings: list[str]) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._reports_lock:
            if now - self._reports_cache_time < 5.0:
                return list(self._reports_cache)
            reports: list[dict[str, Any]] = []
            if self.reports_dir.is_dir():
                candidates = list(self.reports_dir.glob("*/result.json"))
                candidates += list(self.reports_dir.glob("sweep-*.json"))
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                for path in candidates[:MAX_REPORTS]:
                    try:
                        if path.stat().st_size > 5_000_000:
                            continue
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        summary = self._report_summary(path, payload)
                        if summary:
                            reports.append(summary)
                    except (OSError, ValueError) as exc:
                        warnings.append(f"report {path.name}: {exc}")
            self._reports_cache = reports
            self._reports_cache_time = now
            return list(reports)

    def _report_summary(self, path: Path, payload: Any) -> dict[str, Any] | None:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        base = {
            "id": path.parent.name if path.name == "result.json" else path.stem,
            "path": str(path.relative_to(self.reports_dir)),
            "mtime": mtime,
        }
        if isinstance(payload, list):
            return {
                **base,
                "type": "sweep",
                "rows": len(payload),
                "top": payload[:5],
            }
        if not isinstance(payload, dict):
            return None
        if "best" in payload and "evaluations" in payload:
            best = payload.get("best") or {}
            return {
                **base,
                "type": "optimize",
                "strategy": payload.get("strategy"),
                "method": payload.get("method"),
                "objective": payload.get("objective"),
                "score": best.get("score"),
                "params": best.get("params"),
                "holdout": payload.get("holdout"),
                "trials": len(payload.get("evaluations") or []),
            }
        if "metrics" in payload:
            metrics = payload.get("metrics") or {}
            return {
                **base,
                "type": "backtest",
                "strategy": payload.get("strategy"),
                "params": payload.get("params"),
                "return": metrics.get("total_return"),
                "sharpe": metrics.get("sharpe"),
                "max_drawdown": metrics.get("max_drawdown"),
                "trades": metrics.get("trades"),
                "final_equity": metrics.get("final_equity"),
                "counts": payload.get("counts"),
            }
        return None

    def read_report(self, relative: str) -> Any:
        """Read one report JSON, restricted to the reports directory."""
        root = self.reports_dir.resolve()
        candidate = (root / relative).resolve()
        if root != candidate and root not in candidate.parents:
            raise ValueError("path escapes the reports directory")
        if not candidate.is_file():
            raise FileNotFoundError(relative)
        if candidate.stat().st_size > 5_000_000:
            raise ValueError("report is too large")
        return json.loads(candidate.read_text(encoding="utf-8"))

    # -- config / health -------------------------------------------------
    def _config_summary(self) -> dict[str, Any]:
        cfg = self.cfg
        return {
            "name": cfg.app.name,
            "mode": cfg.app.mode,
            "symbols": list(cfg.market.symbols),
            "timeframe": cfg.market.timeframe,
            "history_days": cfg.market.history_days,
            "strategy": {"name": cfg.strategy.name, "params": cfg.strategy.params},
            "wallet": {
                "starting_cash": cfg.wallet.starting_cash,
                "currency": cfg.wallet.currency,
                "allow_short": cfg.wallet.allow_short,
                "margin_multiplier": cfg.wallet.margin_multiplier,
            },
            "broker": {"provider": cfg.broker.provider, "paper": cfg.broker.paper},
            "feed": {
                "provider": cfg.feed.provider,
                "offline": cfg.feed.offline,
                "poll_seconds": cfg.feed.poll_seconds,
                "timeframe": cfg.market.timeframe,
            },
            "execution": {
                "slippage_bps": cfg.execution.slippage_bps,
                "half_spread_bps": cfg.execution.half_spread_bps,
                "spread_bps": dict(cfg.execution.spread_bps),
                "spreads_csv": cfg.execution.spreads_csv,
                "commission_per_share": cfg.execution.commission_per_share,
                "min_commission": cfg.execution.min_commission,
                "max_participation": cfg.execution.max_participation,
            },
            "risk": {
                "max_order_notional": cfg.risk.max_order_notional,
                "max_position_pct": cfg.risk.max_position_pct,
                "max_gross_exposure_pct": cfg.risk.max_gross_exposure_pct,
                "max_daily_loss_pct": cfg.risk.max_daily_loss_pct,
                "max_open_orders": cfg.risk.max_open_orders,
                "max_data_age_s": cfg.risk.max_data_age_s,
                "price_collar_pct": cfg.risk.price_collar_pct,
                "duplicate_window_s": cfg.risk.duplicate_window_s,
            },
            "storage": {"enabled": cfg.storage.enabled, "path": cfg.storage.path},
            "data_dir": cfg.app.data_dir,
            "reports_dir": str(self.reports_dir),
        }

    def _health(
        self,
        age: float | None,
        stale: bool,
        db: dict[str, Any],
        warnings: list[str],
    ) -> dict[str, Any]:
        disk_free = None
        try:
            target = self.data_dir if self.data_dir.exists() else Path.cwd()
            disk_free = shutil.disk_usage(target).free / 1e9
            if disk_free < 1.0:
                warnings.append(f"low disk space: {disk_free:.1f} GB")
            elif disk_free < 5.0:
                warnings.append(f"disk space is getting low: {disk_free:.1f} GB")
        except OSError:
            pass

        temperature = None
        for zone in (
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/hwmon/hwmon0/temp1_input",
        ):
            try:
                temperature = int(Path(zone).read_text().strip()) / 1000.0
                break
            except (OSError, ValueError):
                continue
        if temperature is not None and temperature >= 80.0:
            warnings.append(f"high SoC temperature: {temperature:.1f} C")

        return {
            "db_state": db["db_state"],
            "db_error": db["db_error"],
            "disk_free_gb": disk_free,
            "temperature_c": temperature,
            "heartbeat_age_s": age,
            "heartbeat_stale": stale,
            "warnings": list(warnings),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _downsample(points: list[dict[str, Any]], limit: int = MAX_EQUITY_POINTS) -> list[dict[str, Any]]:
    if len(points) <= limit:
        return points
    step = len(points) / limit
    sampled = [points[int(i * step)] for i in range(limit)]
    if sampled[-1] is not points[-1]:
        sampled[-1] = points[-1]
    return sampled


def _number(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _nullable(value: Any) -> float | None:
    return _number(value, None)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
