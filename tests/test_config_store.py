from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from krabs.config import Config, ConfigError
from krabs.events import Fill, Order, OrderIntent, Side
from krabs.store import SqliteStore

NOW = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)


class ConfigTests(unittest.TestCase):
    def test_example_config_loads(self):
        cfg = Config.from_file("configs/example.toml")
        self.assertEqual(cfg.market.symbols, ["SPY", "QQQ", "AAPL"])
        self.assertEqual(cfg.risk.max_daily_loss_pct, 0.03)

    def test_unknown_key_is_an_error(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"risk": {"max_daily_loss_pct": 0.01, "typo": 1}})

    def test_invalid_values(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"market": {"symbols": []}})
        with self.assertRaises(ConfigError):
            Config.from_dict({"wallet": {"starting_cash": -1}})
        with self.assertRaises(ConfigError):
            Config.from_dict({"feed": {"provider": "csv"}})

    def test_env_credentials(self):
        cfg = Config.from_dict(
            {"broker": {"api_key_env": "KRABS_TEST_KEY", "api_secret_env": "KRABS_TEST_SECRET"}}
        )
        import os

        os.environ.pop("KRABS_TEST_KEY", None)
        with self.assertRaises(ConfigError):
            cfg.credentials()
        os.environ["KRABS_TEST_KEY"] = "k"
        os.environ["KRABS_TEST_SECRET"] = "s"
        self.assertEqual(cfg.credentials(), ("k", "s"))
        del os.environ["KRABS_TEST_KEY"], os.environ["KRABS_TEST_SECRET"]


class StoreTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "krabs.db"
            store = SqliteStore(path)
            store.start_run(
                run_id="run-1",
                mode="backtest",
                strategy="buy_hold",
                params={"allocation": 0.5},
                config={"a": 1},
                started_at=NOW,
                initial_cash=10_000.0,
            )
            order = Order(
                client_id="paper-1",
                intent=OrderIntent("SPY", Side.BUY, qty=1, strategy_id="buy_hold"),
                qty=1.0,
            )
            store.record_order("run-1", order)
            store.record_fill(
                "run-1",
                Fill("paper-1", "SPY", Side.BUY, 1.0, 400.0, NOW, strategy_id="buy_hold"),
            )
            from krabs.events import AccountState

            store.record_equity(
                "run-1",
                AccountState(ts=NOW, cash=9_600.0, equity=10_000.0, buying_power=9_600.0),
            )
            store.log_event("run-1", "INFO", "test", {"ok": True})
            store.finish_run("run-1", "finished", 10_000.0)

            counts = store.counts("run-1")
            self.assertEqual(counts["orders"], 1)
            self.assertEqual(counts["fills"], 1)
            self.assertEqual(counts["equity"], 1)
            self.assertEqual(len(store.load_orders("run-1")), 1)
            store.close()

            with SqliteStore(path) as reopened:
                last = reopened.last_run()
                self.assertIsNotNone(last)
                self.assertEqual(last["status"], "finished")
                self.assertEqual(last["strategy"], "buy_hold")

    def test_abort_stale_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "krabs.db"
            with SqliteStore(path) as store:
                store.start_run(
                    run_id="old",
                    mode="paper",
                    strategy="larry",
                    params={},
                    config={},
                    started_at=NOW,
                    initial_cash=1000.0,
                )
                store.start_run(
                    run_id="current",
                    mode="paper",
                    strategy="larry",
                    params={},
                    config={},
                    started_at=NOW,
                    initial_cash=1000.0,
                )
                aborted = store.abort_stale_runs(exclude_run_id="current")
                self.assertEqual(aborted, 1)
                rows = {
                    row["run_id"]: row["status"]
                    for row in store._conn.execute("SELECT run_id, status FROM runs")
                }
                self.assertEqual(rows["old"], "aborted")
                self.assertEqual(rows["current"], "running")


if __name__ == "__main__":
    unittest.main()
