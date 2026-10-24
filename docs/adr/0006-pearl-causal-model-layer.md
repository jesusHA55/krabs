# ADR 0006 — Pearl as a causal model layer

**Status:** accepted · **Date:** 2026-10

## Context

Krabs strategies need more than scalar indicators: state-space filtering,
rolling regressions, pairs statistics, volatility models and change detection.
These algorithms are stateful, have warm-up periods, and are easy to leak future
information into if fitted in the wrong place.

Putting them inside `on_bar` ad hoc would duplicate lifecycle logic and make
restart/serialization impossible. A separate ML framework would add dependencies
and a training loop that does not match bar-driven execution.

## Decision

Add `krabs.pearl`, a code-first DAG of stateful nodes:

- `Observation` carries one or more synchronized bars and an explicit `dt`.
- Nodes expose `update`, `reset`, `ready`, `state`, `load_state`, `params`.
- `ModelBuilder` enforces that nodes only reference earlier nodes.
- `PearlModel` executes the DAG in insertion order and returns an immutable
  `PearlSnapshot` with all outputs and a global `ready`.
- `PearlStrategy` updates a model once per timestamp and only acts when ready.
- Core maths are pure Python; optional NumPy/SciPy/PyWavelets modules can be
  added as lazy adapters.

Pairs trading is a first-class use case: `Observation.from_bars([left, right])`
and `PairSpread` are part of the initial release.

## Consequences

- No look-ahead by construction; a node cannot reference a future output.
- The same model runs in backtest and live because it is only driven by
  `update(observation)`.
- State can be saved/loaded without serializing the graph.
- Numerical code is auditable and dependency-free on a Pi.
- More code than a single indicator library; every node must own its lifecycle.
- Nonlinear filters and advanced multivariate estimators are deferred.

## Alternatives

- **Single-file indicator functions:** cannot carry covariance/buffers or warm-up
  state cleanly.
- **statsmodels/scikit-learn:** excellent references, but heavy, partially
  non-causal, and awkward for per-bar online state.
- **A separate model service:** network and deployment overhead with no benefit
  for a single-process Pi.
