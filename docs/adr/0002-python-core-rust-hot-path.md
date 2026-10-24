# ADR 0002 — Python core, optional Rust hot path

**Status:** accepted · **Date:** 2026-10

## Context

The Raspberry Pi has limited CPU and memory. Python gives the best quant
ecosystem and fast iteration, but per-bar indicator loops over hundreds of
points add up during parameter sweeps. A full Rust rewrite would sacrifice
readability and the ability to audit risk logic quickly.

## Decision

Keep orchestration, risk, I/O and strategies in dependency-free Python. Put
pure numeric kernels (SMA/EMA/RSI/ATR/rolling std, drawdown, realised vol) in a
PyO3 extension, called **once per bar with a window**, not per tick. Provide an
identical pure-Python fallback and load Rust only if importable.

## Consequences

- The platform always runs; Rust is an optimisation, not a requirement.
- One wheel per Python-ABI family (`abi3-py311`).
- Numeric contracts must stay in sync; both test suites assert the same values.
- FFI conversion cost is O(window); tiny windows stay in Python.

## Alternatives

- Pure NumPy: better than Python loops but adds a heavy dependency and still
  allocates per call.
- Whole-engine Rust: best raw speed, worst iteration speed and auditability.
- Cython: less safe, more build friction on ARM.
