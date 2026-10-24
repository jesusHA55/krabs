#!/usr/bin/env bash
# Online SQLite backup, gzip-compressed, with retention. Safe while the service
# is running (uses SQLite's backup API, not cp).
set -euo pipefail

DB="${KRABS_DB:-/opt/krabs/var/krabs.db}"
DEST="${KRABS_BACKUP_DIR:-/opt/krabs/backups}"
KEEP="${KRABS_BACKUP_KEEP:-14}"

python3 - "${DB}" "${DEST}" "${KEEP}" <<'PY'
import gzip
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

db = Path(sys.argv[1])
dest = Path(sys.argv[2])
keep = int(sys.argv[3])

if not db.exists():
    print(f"no database at {db}; nothing to back up")
    raise SystemExit(0)

dest.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
raw = dest / f"krabs-{stamp}.db"
out = dest / f"krabs-{stamp}.db.gz"

with sqlite3.connect(str(db), timeout=30) as source:
    with sqlite3.connect(str(raw)) as target:
        source.backup(target)

with raw.open("rb") as src, gzip.open(out, "wb", compresslevel=6) as dst:
    while chunk := src.read(1 << 20):
        dst.write(chunk)
raw.unlink()

backups = sorted(dest.glob("krabs-*.db.gz"))
for old in backups[:-keep]:
    old.unlink()
print(f"backup written: {out} ({out.stat().st_size / 1024:.0f} KiB), {len(backups)} kept")
PY
