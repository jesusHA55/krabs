from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from krabs.cli import main


class CliTests(unittest.TestCase):
    def test_strategies_lists_builtins(self):
        self.assertEqual(main(["strategies"]), 0)

    def test_backtest_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main(
                [
                    "backtest",
                    "-c",
                    "configs/example.toml",
                    "--days",
                    "2",
                    "--quiet",
                    "--report-dir",
                    tmp,
                ]
            )
            self.assertEqual(code, 0)
            runs = list(Path(tmp).iterdir())
            self.assertEqual(len(runs), 1)
            self.assertTrue((runs[0] / "result.json").is_file())
            self.assertTrue((runs[0] / "equity.csv").is_file())

    def test_switch_strategy_ignores_other_strategy_params(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main(
                [
                    "backtest",
                    "-c",
                    "configs/example.toml",
                    "--strategy",
                    "rsi_reversion",
                    "--days",
                    "2",
                    "--quiet",
                    "--report-dir",
                    tmp,
                ]
            )
            self.assertEqual(code, 0)

    def test_global_config_flag_after_subcommand(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                main(
                    [
                        "-c",
                        "configs/example.toml",
                        "backtest",
                        "--days",
                        "1",
                        "--quiet",
                        "--report-dir",
                        tmp,
                    ]
                ),
                0,
            )

    def test_offline_paper_runtime_without_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "offline.toml"
            config.write_text(
                f"""
[app]
mode = "paper"
data_dir = "{tmp}"

[market]
symbols = ["SPY"]
timeframe = "1Min"
history_days = 1
enforce_market_hours = false

[wallet]
starting_cash = 10000.0

[execution]
max_participation = 0.0

[risk]
max_daily_loss_pct = 0.5
max_data_age_s = 600
duplicate_window_s = 0

[feed]
provider = "synthetic"
poll_seconds = 0.01
warmup_bars = 50

[storage]
enabled = false

[strategy]
name = "buy_hold"
params = {{ allocation = 0.2 }}
"""
            )
            code = main(
                ["paper", "--offline", "-c", str(config), "--max-cycles", "3"]
            )
            self.assertEqual(code, 0)
            heartbeat = Path(tmp) / "heartbeat.json"
            self.assertTrue(heartbeat.is_file())
            self.assertIn('"stopped": true', heartbeat.read_text())

    def test_unknown_config_key_fails_cleanly(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write("[risk]\nmax_daily_loss_pct = 0.01\ntypo = 1\n")
            path = fh.name
        try:
            self.assertEqual(main(["backtest", "-c", path]), 2)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_bad_parameter_fails_cleanly(self):
        self.assertEqual(
            main(
                [
                    "backtest",
                    "-c",
                    "configs/example.toml",
                    "--strategy",
                    "sma_cross",
                    "--param",
                    "nope=1",
                ]
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
