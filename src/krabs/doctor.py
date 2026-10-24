"""`krabs doctor`: pre-flight checks for a Raspberry Pi deployment.

Every check is read-only apart from touching a temp file in the data directory.
Exit code is the number of FAILs (capped at 127) so systemd/monitoring can alert.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .indicators import backend_name


@dataclass(slots=True)
class Check:
    name: str
    status: str  # OK | WARN | FAIL | INFO
    detail: str


def run_checks(cfg: Config) -> list[Check]:
    checks: list[Check] = []
    checks.append(
        Check(
            "python",
            "OK" if sys.version_info >= (3, 11) else "FAIL",
            f"{platform.python_version()} ({platform.machine()})",
        )
    )
    checks.append(Check("platform", "INFO", f"{platform.platform()}"))

    backend = backend_name()
    checks.append(
        Check(
            "indicator backend",
            "INFO" if backend == "rust" else "WARN",
            f"{backend}"
            + ("" if backend == "rust" else " (pure Python; build krabs_core for the hot path)"),
        )
    )

    checks.append(
        Check(
            "config",
            "OK",
            f"mode={cfg.app.mode} symbols={','.join(cfg.market.symbols)} "
            f"tf={cfg.market.timeframe} strategy={cfg.strategy.name}",
        )
    )

    if cfg.feed.offline:
        checks.append(Check("feed", "INFO", "offline simulation (synthetic bars, no broker)"))
        if cfg.app.mode in ("broker-paper", "live"):
            checks.append(
                Check("feed", "FAIL", f"{cfg.app.mode} cannot run with feed.offline = true")
            )
    elif cfg.app.mode in ("paper", "broker-paper", "live"):
        if cfg.feed.provider != "alpaca":
            checks.append(
                Check(
                    "feed",
                    "FAIL",
                    f"{cfg.app.mode} requires [feed].provider = 'alpaca' "
                    f"(got {cfg.feed.provider!r})",
                )
            )
        else:
            checks.append(Check("feed", "OK", "Alpaca REST polling"))
    if cfg.feed.provider == "csv" and not Path(cfg.feed.csv_path).is_file():
        checks.append(Check("feed", "FAIL", f"CSV not found: {cfg.feed.csv_path}"))

    if cfg.app.mode in ("paper", "broker-paper", "live") and not cfg.feed.offline:
        key = os.environ.get(cfg.broker.api_key_env, "")
        secret = os.environ.get(cfg.broker.api_secret_env, "")
        missing = [
            name
            for name, value in (
                (cfg.broker.api_key_env, key),
                (cfg.broker.api_secret_env, secret),
            )
            if not value
        ]
        if missing:
            checks.append(Check("credentials", "FAIL", f"missing env: {', '.join(missing)}"))
        else:
            checks.append(Check("credentials", "OK", "Alpaca key/secret present in environment"))
        if cfg.app.mode == "live" and cfg.broker.paper:
            checks.append(Check("live wiring", "FAIL", "[broker].paper must be false for live"))
        if cfg.app.mode == "live" and not cfg.storage.enabled:
            checks.append(Check("audit trail", "FAIL", "storage.enabled must be true for live"))
        if cfg.app.mode == "live":
            checks.append(
                Check(
                    "risk limits",
                    "INFO",
                    f"max order ${cfg.risk.max_order_notional:,.0f}, "
                    f"daily stop {cfg.risk.max_daily_loss_pct:.1%}, "
                    f"position {cfg.risk.max_position_pct:.0%}",
                )
            )

    data_dir = Path(cfg.app.data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(Check("data dir", "OK", f"{data_dir.resolve()} writable"))
    except OSError as exc:
        checks.append(Check("data dir", "FAIL", f"{data_dir}: {exc}"))

    kill = data_dir / "KILL"
    if kill.exists():
        checks.append(
            Check("kill switch", "WARN", f"ENGAGED: {kill.read_text(errors='replace').strip()}")
        )
    else:
        checks.append(Check("kill switch", "OK", "not engaged"))

    heartbeat = data_dir / "heartbeat.json"
    if heartbeat.exists():
        try:
            payload = json.loads(heartbeat.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(payload["ts"])
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            status = "OK" if age < 300 else "WARN"
            checks.append(
                Check(
                    "heartbeat",
                    status,
                    f"{age:.0f}s old, equity={payload.get('equity')}, "
                    f"halted={payload.get('halted')}",
                )
            )
        except (OSError, ValueError, KeyError) as exc:
            checks.append(Check("heartbeat", "WARN", f"unreadable: {exc}"))
    else:
        checks.append(Check("heartbeat", "INFO", "no heartbeat yet (service never started?)"))

    checks.extend(_system_checks())
    return checks


def _system_checks() -> list[Check]:
    checks: list[Check] = []

    try:
        usage = shutil.disk_usage(Path.cwd())
        free_gb = usage.free / 1e9
        status = "FAIL" if free_gb < 1 else ("WARN" if free_gb < 5 else "OK")
        checks.append(Check("disk free", status, f"{free_gb:.1f} GB on {Path.cwd()}"))
    except OSError as exc:
        checks.append(Check("disk free", "WARN", str(exc)))

    try:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            meminfo[key] = int(value.strip().split()[0])
        available_mb = meminfo.get("MemAvailable", 0) / 1024
        status = "WARN" if available_mb < 256 else "OK"
        checks.append(Check("memory", status, f"{available_mb:.0f} MB available"))
    except (OSError, ValueError):
        checks.append(Check("memory", "INFO", "not available on this platform"))

    for zone in ("/sys/class/thermal/thermal_zone0/temp", "/sys/class/hwmon/hwmon0/temp1_input"):
        try:
            celsius = int(Path(zone).read_text().strip()) / 1000.0
            status = "FAIL" if celsius >= 85 else ("WARN" if celsius >= 75 else "OK")
            checks.append(Check("temperature", status, f"{celsius:.1f} C"))
            break
        except (OSError, ValueError):
            continue
    else:
        checks.append(Check("temperature", "INFO", "no thermal sensor exposed"))

    attempts = 0
    total_ms = 0.0
    for host in ("api.alpaca.markets", "data.alpaca.markets"):
        started = time.perf_counter()
        try:
            with socket.create_connection((host, 443), timeout=4):
                total_ms += (time.perf_counter() - started) * 1000
                attempts += 1
        except OSError as exc:
            checks.append(Check("network", "WARN", f"{host}: {exc}"))
    if attempts:
        checks.append(
            Check(
                "network",
                "OK",
                f"{attempts}/2 broker hosts reachable, avg {total_ms / attempts:.0f} ms",
            )
        )

    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=3, check=False,
        ).stdout.strip()
        if out:
            checks.append(
                Check("clock", "OK" if out == "yes" else "WARN", f"NTP synchronized: {out}")
            )
    except (OSError, subprocess.SubprocessError):
        checks.append(Check("clock", "INFO", "timedatectl unavailable"))

    try:
        out = subprocess.run(
            ["systemctl", "is-active", "krabs.service"],
            capture_output=True, text=True, timeout=3, check=False,
        ).stdout.strip()
        if out:
            checks.append(
                Check("service", "OK" if out == "active" else "WARN", f"krabs.service: {out}")
            )
    except (OSError, subprocess.SubprocessError):
        pass

    return checks


def print_checks(checks: list[Check]) -> int:
    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"[{check.status:^4}] {check.name:<{width}}  {check.detail}")
    failures = sum(1 for c in checks if c.status == "FAIL")
    warnings = sum(1 for c in checks if c.status == "WARN")
    print()
    print(f"{len(checks)} checks, {warnings} warning(s), {failures} failure(s)")
    return min(failures, 127)
