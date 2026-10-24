"""Operator kill switch.

Deliberately a file, not an API: it works over SSH, survives process crashes,
can be triggered by a watchdog or a mobile shortcut, and needs no service.

    krabs kill            # writes var/KILL
    krabs status          # reports it
    rm var/KILL           # clears it (or `krabs kill --clear`)

The runtime polls it every cycle and blocks all new orders while it exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


class KillSwitch:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @property
    def engaged(self) -> bool:
        return self.path.exists()

    def reason(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
        except OSError:
            return "kill switch engaged (unreadable reason)"

    def engage(self, reason: str = "manual") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(f"{stamp} {reason}\n", encoding="utf-8")
        tmp.replace(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
