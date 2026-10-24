from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from krabs.config import Config
from krabs.dashboard import DashboardData, DashboardHTTPServer
from krabs.events import AccountState, Fill, Order, OrderIntent, OrderStatus, Side
from krabs.store import SqliteStore


class DashboardDataTests(unittest.TestCase):
    def test_snapshot_aggregates_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp)
            _seed(tmp, cfg)
            data = DashboardData(cfg)
            snap = data.snapshot()

            self.assertEqual(snap["version"], "0.1.0")
            self.assertIsNotNone(snap["heartbeat"])
            self.assertFalse(snap["heartbeat_stale"])
            self.assertEqual(snap["config"]["symbols"], ["SPY"])
            self.assertNotIn("secret", json.dumps(snap["config"]).lower())
            self.assertNotIn("api_key", json.dumps(snap["config"]).lower())

            self.assertAlmostEqual(snap["account"]["equity"], 10050.0)
            self.assertAlmostEqual(snap["account"]["total_pnl"], 50.0)
            self.assertEqual(len(snap["positions"]), 1)
            self.assertEqual(snap["heartbeat"]["strategy_diagnostics"]["signals"]["entry"], 1)
            self.assertEqual(snap["positions"][0]["symbol"], "SPY")
            self.assertGreaterEqual(len(snap["equity"]["points"]), 1)
            self.assertEqual(len(snap["orders"]), 1)
            self.assertEqual(len(snap["fills"]), 1)
            self.assertTrue(any(s["id"] == "sma_cross" for s in snap["strategies"]))
            types = {r["type"] for r in snap["reports"]}
            self.assertEqual(types, {"backtest", "optimize"})

    def test_report_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp)
            _seed(tmp, cfg)
            data = DashboardData(cfg)
            with self.assertRaises(ValueError):
                data.read_report("../../etc/passwd")


class DashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _config(self.tmp.name)
        _seed(self.tmp.name, self.cfg)
        self.data = DashboardData(self.cfg)
        self.server = DashboardHTTPServer(
            ("127.0.0.1", 0),
            self.data,
            token="secret",
            no_auth=False,
            allow_controls=True,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, method, path, *, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            payload = response.read()
            return response, payload
        finally:
            conn.close()

    def _login(self):
        response, _ = self._request("GET", "/?token=secret")
        self.assertEqual(response.status, 303)
        cookies = {}
        for key, value in response.getheaders():
            if key.lower() == "set-cookie":
                name, rest = value.split(";", 1)
                cookie_name, cookie_value = name.split("=", 1)
                cookies[cookie_name] = cookie_value
        self.assertIn("krabs_session", cookies)
        self.assertIn("krabs_csrf", cookies)
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return cookies, cookie_header

    def test_requires_auth_and_serves_snapshot(self):
        response, _ = self._request("GET", "/api/snapshot")
        self.assertEqual(response.status, 401)

        cookies, cookie_header = self._login()
        response, payload = self._request(
            "GET", "/api/snapshot", headers={"Cookie": cookie_header}
        )
        self.assertEqual(response.status, 200)
        snap = json.loads(payload)
        self.assertEqual(snap["config"]["symbols"], ["SPY"])
        self.assertTrue(snap["controls"]["enabled"])

        # Bearer token works for scripts and monitoring.
        response, _ = self._request(
            "GET", "/api/snapshot", headers={"Authorization": "Bearer secret"}
        )
        self.assertEqual(response.status, 200)

        # Static assets are public (needed by the login page).
        response, _ = self._request("GET", "/static/app.js")
        self.assertEqual(response.status, 200)
        response, _ = self._request("GET", "/static/manifest.webmanifest")
        self.assertEqual(response.status, 200)

    def test_kill_switch_requires_csrf_and_controls(self):
        cookies, cookie_header = self._login()
        body = json.dumps({"action": "engage", "reason": "test"})
        response, _ = self._request(
            "POST",
            "/api/kill",
            headers={"Cookie": cookie_header, "Content-Type": "application/json"},
            body=body,
        )
        self.assertEqual(response.status, 403)

        response, payload = self._request(
            "POST",
            "/api/kill",
            headers={
                "Cookie": cookie_header,
                "X-Krabs-CSRF": cookies["krabs_csrf"],
                "Content-Type": "application/json",
            },
            body=body,
        )
        self.assertEqual(response.status, 200)
        self.assertTrue((Path(self.tmp.name) / "KILL").is_file())
        self.assertTrue(json.loads(payload)["kill"]["engaged"])

        response, _ = self._request(
            "POST",
            "/api/kill",
            headers={
                "Cookie": cookie_header,
                "X-Krabs-CSRF": cookies["krabs_csrf"],
                "Content-Type": "application/json",
            },
            body=json.dumps({"action": "clear"}),
        )
        self.assertEqual(response.status, 200)
        self.assertFalse((Path(self.tmp.name) / "KILL").exists())

    def test_login_post_and_reports(self):
        body = urlencode({"token": "secret"})
        response, _ = self._request(
            "POST",
            "/login",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
        )
        self.assertEqual(response.status, 303)
        cookies = {}
        for key, value in response.getheaders():
            if key.lower() == "set-cookie":
                name = value.split(";", 1)[0]
                k, v = name.split("=", 1)
                cookies[k] = v
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

        response, payload = self._request(
            "GET",
            "/api/report?path=bt-test/result.json",
            headers={"Cookie": cookie_header},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload)["strategy"], "sma_cross")

        response, _ = self._request(
            "GET",
            "/api/report?path=../../etc/passwd",
            headers={"Cookie": cookie_header},
        )
        self.assertEqual(response.status, 400)

    def test_login_rejects_bad_token(self):
        body = urlencode({"token": "wrong"})
        response, _ = self._request(
            "POST",
            "/login",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
        )
        self.assertEqual(response.status, 303)
        self.assertIn("error=1", response.getheader("Location") or "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config(tmp: str) -> Config:
    return Config.from_dict(
        {
            "app": {"mode": "paper", "data_dir": tmp},
            "market": {"symbols": ["SPY"], "timeframe": "1Min"},
            "wallet": {"starting_cash": 10_000.0},
            "risk": {"max_data_age_s": 600},
            "feed": {"provider": "synthetic", "poll_seconds": 1.0},
            "storage": {"enabled": True, "path": str(Path(tmp) / "krabs.db")},
            "backtest": {"report_dir": str(Path(tmp) / "reports")},
        }
    )


def _seed(tmp: str, cfg: Config) -> None:
    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    heartbeat = {
        "ts": ts,
        "mode": "paper",
        "run_id": "run-1",
        "strategy": "sma_cross",
        "pid": 1234,
        "cycles": 3,
        "bars": 2,
        "orders": 1,
        "fills": 1,
        "errors": 0,
        "halted": False,
        "halt_reason": "",
        "kill_switch": False,
        "equity": 10050.0,
        "cash": 9000.0,
        "buying_power": 9000.0,
        "start_equity": 10000.0,
        "day_start_equity": 10000.0,
        "day_pnl": 50.0,
        "day_return": 0.005,
        "gross_exposure": 1050.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 50.0,
        "long_value": 1050.0,
        "short_value": 0.0,
        "positions": {
            "SPY": {"qty": 10, "avg": 100.0, "last": 105.0, "value": 1050.0, "unrealized": 50.0}
        },
        "last_bar": ts,
        "strategy_diagnostics": {
            "ready": True,
            "signals": {"entry": 1},
            "values": {"spread": {"beta": 2.0, "z": -2.1}},
        },
        "stopped": False,
    }
    (Path(tmp) / "heartbeat.json").write_text(json.dumps(heartbeat), encoding="utf-8")

    store = SqliteStore(cfg.storage.path)
    store.start_run(
        run_id="run-1",
        mode="paper",
        strategy="sma_cross",
        params={"fast": 20, "slow": 60},
        config={"test": True},
        started_at=now,
        initial_cash=10_000.0,
    )
    order = Order(
        client_id="paper-1",
        intent=OrderIntent("SPY", Side.BUY, qty=10, strategy_id="sma_cross"),
        qty=10,
        avg_price=100.0,
        status=OrderStatus.FILLED,
    )
    store.record_order("run-1", order)
    store.record_fill("run-1", Fill("paper-1", "SPY", Side.BUY, 10, 100.0, now))
    store.record_equity(
        "run-1",
        AccountState(
            ts=now,
            cash=9000.0,
            equity=10050.0,
            buying_power=9000.0,
            long_value=1050.0,
            unrealized_pnl=50.0,
            day_start_equity=10000.0,
        ),
    )
    store.finish_run("run-1", "finished", 10050.0)
    store.close()

    reports = Path(cfg.backtest.report_dir)
    bt = reports / "bt-test"
    bt.mkdir(parents=True)
    (bt / "result.json").write_text(
        json.dumps(
            {
                "run_id": "bt-test",
                "strategy": "sma_cross",
                "params": {"fast": 20, "slow": 60},
                "metrics": {
                    "total_return": 0.12,
                    "sharpe": 1.1,
                    "max_drawdown": -0.08,
                    "trades": 12,
                    "final_equity": 11200.0,
                },
                "counts": {"bars": 100},
            }
        ),
        encoding="utf-8",
    )
    opt = reports / "opt-test"
    opt.mkdir(parents=True)
    (opt / "result.json").write_text(
        json.dumps(
            {
                "run_id": "opt-test",
                "strategy": "sma_cross",
                "method": "de",
                "objective": "sharpe",
                "best": {"score": 1.2, "params": {"fast": 8, "slow": 30}},
                "holdout": {"sharpe": 0.4, "total_return": 0.05},
                "evaluations": [{"score": 1.2}],
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
