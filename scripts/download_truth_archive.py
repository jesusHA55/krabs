#!/usr/bin/env python3
"""Download the public CNN Truth Social archive for event research.

    python3 scripts/download_truth_archive.py

This is a third-party public snapshot, not a Truth Social API client. It does
not bypass access controls and does not include non-public data.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "https://ix.cnn.io/data/truth-social/truth_archive.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "data/altdata/truth_archive.csv"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    target = Path(args.out)
    if target.is_file() and not args.force:
        print(f"already present: {target} ({target.stat().st_size:,} bytes)")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    print(f"downloading {URL}")
    request = urllib.request.Request(URL, headers={"User-Agent": "Krabs research"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            tmp.write_bytes(response.read())
    except Exception as exc:  # noqa: BLE001
        print(f"download failed: {exc}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return 1
    tmp.replace(target)
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
