"""Adversarial-bench harness (P4.3).

Each test in this directory exercises a known abuse vector under load.
Reuses the microbench helper from ``tests/perf/conftest.py`` so the
output format is identical to the perf baseline.

Invocation: ``pytest bench/adversarial/ -v`` (also wired as
``make security-bench``). These tests are NOT part of the default
``testpaths`` (``tests/``) so they don't run on every commit; they
are opt-in like ``bench-soak``.

Scenarios shipped:

- ``test_large_body.py`` — RES4 / P2.1: 1 MB body must be rejected by
  ``bus.post`` in <1 ms, with the bus log unchanged.
- ``test_tampered_replay.py`` — T1 / P0.1+P0.2: a 1k-line journal
  with 5% tampered lines replays to completion, surfacing
  ``journal_corruption`` events for each bad line.

Future scenarios (not yet implemented; tracked in the security-model
plan):

- ``test_slow_subscriber.py`` — CON1: latency under a slow callback.
- ``test_slow_disk.py`` — RES3: snapshot-queue depth at sustained
  100 ms write latency.
- ``test_token_flood.py`` — RES6: bus.post rate at 10 k tokens/s.
- ``test_subscriber_exceptions.py`` — F: throughput at 50 % failing
  callbacks.
- ``test_lease_burst.py`` — C3: 16-actor concurrent acquire under
  ``cap=1`` (property + tail latency).
"""
from __future__ import annotations

import gc
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

# Reuse the microbench helper from tests/perf/conftest.py without
# importing test files (which would re-collect them). The helper is
# pure, so we copy a minimal version inline.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "adversarial: opt-in security adversarial bench (run via make security-bench)",
    )
    config.addinivalue_line(
        "markers",
        "perf: opt-in microbench (shared marker with tests/perf/)",
    )


@dataclass
class BenchResult:
    name: str
    iters: int
    inner: int
    ns_p50: float
    ns_p99: float
    ns_mean: float
    ns_min: float
    extras: dict[str, Any] = field(default_factory=dict)


def _bench(fn: Callable[[], Any], *, name: str,
           iters: int = 100, inner: int = 1, warmup: int = 5) -> BenchResult:
    samples: list[int] = []
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(warmup):
            fn()
        for _ in range(iters):
            t0 = time.perf_counter_ns()
            for _ in range(inner):
                fn()
            samples.append(time.perf_counter_ns() - t0)
    finally:
        if gc_was_enabled:
            gc.enable()
        gc.collect()
    samples.sort()
    return BenchResult(
        name=name, iters=iters, inner=inner,
        ns_p50=float(statistics.median(samples)),
        ns_p99=float(samples[int(0.99 * (len(samples) - 1))]),
        ns_mean=statistics.fmean(samples),
        ns_min=float(samples[0]),
    )


@pytest.fixture
def bench():
    """Return :func:`_bench` with default settings."""
    return _bench
