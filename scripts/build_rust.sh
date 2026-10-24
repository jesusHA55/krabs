#!/usr/bin/env bash
# Build the optional Rust hot path into the active Python environment.
#
#   ./scripts/build_rust.sh          # release build (recommended)
#   ./scripts/build_rust.sh --debug  # faster compile, slower runtime
#
# On a Raspberry Pi, building takes a few minutes. Use the native CPU to get
# the most from the little cores:
#   RUSTFLAGS="-C target-cpu=native" ./scripts/build_rust.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

MANIFEST="crates/krabs_core/Cargo.toml"
PROFILE="--release"
if [[ "${1:-}" == "--debug" ]]; then
  PROFILE=""
fi

if ! command -v maturin >/dev/null 2>&1; then
  echo "maturin not found; installing into the active environment"
  python3 -m pip install --upgrade maturin
fi

if [[ -n "${VIRTUAL_ENV:-}" ]]; then
  echo "building into venv: ${VIRTUAL_ENV}"
else
  echo "warning: no virtualenv active; maturin may refuse to install system-wide"
fi

maturin develop ${PROFILE} --manifest-path "${MANIFEST}"
python3 - <<'PY'
import krabs_core
from krabs.indicators import backend_name, sma

assert backend_name() == "rust", backend_name()
series = [float(i) for i in range(10)]
out = sma(series, 3)
assert abs(out[-1] - 8.0) < 1e-12, out
print(f"krabs_core {krabs_core.__version__} loaded; indicators backend={backend_name()}")
PY
