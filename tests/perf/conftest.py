"""Shared infra for ``tests/perf/`` — microbench tier.

Goal: run a small callable many times and report p50/p99/mean with
enough statistical care that we can compare across phases on the same
box. We intentionally avoid the ``pytest-benchmark`` dependency: this
tier ships with the kernel and we want zero new deps for it.

The :func:`_bench` helper is the single entry point. It returns a
:class:`BenchResult` and *also* attaches it to the active node's
``user_properties`` so a pytest plugin / CLI hook can collect them.
The default-suite ``make test`` excludes these tests via the ``perf``
marker; ``make bench`` runs only them.
"""
from __future__ import annotations

import gc
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "perf: opt-in microbenchmark; excluded from default suite, run via make bench",
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

    def per_op_ns(self) -> float:
        return self.ns_p50 / self.inner if self.inner else 0.0


def _bench(
    fn: Callable[[], Any],
    *,
    name: str,
    iters: int = 200,
    inner: int = 1,
    warmup: int = 10,
    record: Any = None,
) -> BenchResult:
    """Time ``fn`` ``iters`` times; ``inner`` repeats per call.

    GC is disabled for the duration so a stop-the-world collection
    doesn't pollute the p99. We re-enable + collect afterwards so test
    isolation is preserved.

    ``record`` is the active pytest node (``request.node``); when set
    the result is attached to ``user_properties`` for later collection.
    """
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
    p50 = statistics.median(samples)
    p99 = samples[int(0.99 * (len(samples) - 1))]
    mean = statistics.fmean(samples)
    res = BenchResult(
        name=name,
        iters=iters,
        inner=inner,
        ns_p50=float(p50),
        ns_p99=float(p99),
        ns_mean=mean,
        ns_min=float(samples[0]),
    )
    if record is not None:
        record.user_properties.append(("bench", res))
    return res


@pytest.fixture
def bench(request: pytest.FixtureRequest) -> Callable[..., BenchResult]:
    """Return a curried :func:`_bench` that records onto ``request.node``."""
    def runner(fn: Callable[[], Any], *, name: str, **kw: Any) -> BenchResult:
        return _bench(fn, name=name, record=request.node, **kw)
    return runner


# ---------------------------------------------------------------------------
# Optional: a session-end hook that prints a summary table of every bench
# result collected during the run. Quick-look only — the canonical record
# is JSON written by ``benchmarks/perf.py``.
# ---------------------------------------------------------------------------

_collected: list[BenchResult] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    outcome = yield
    rep = outcome.get_result()
    if rep.when != "call":
        return
    for k, v in getattr(rep, "user_properties", ()):
        if k == "bench" and isinstance(v, BenchResult):
            _collected.append(v)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter,
                            exitstatus: int,
                            config: pytest.Config) -> None:
    if not _collected:
        return
    tr = terminalreporter
    tr.section("perf microbench results")
    tr.write_line(f"{'name':<60} {'iters':>6} {'p50_ns':>12} {'p99_ns':>12} {'mean_ns':>12}")
    for r in _collected:
        tr.write_line(
            f"{r.name:<60} {r.iters:>6} {r.ns_p50:>12.0f} {r.ns_p99:>12.0f} {r.ns_mean:>12.0f}"
        )
