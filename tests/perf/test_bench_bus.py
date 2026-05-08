"""Microbench — :class:`MessageBus` operations.

Targets the operations the audit pinned as hot:
- :meth:`MessageBus.post`            (~1.8 μs target, single-subscriber)
- :meth:`MessageBus.snapshot`        full-log read
- :meth:`MessageBus.snapshot(since)` tail read (currently O(E))
- ``snapshot(since)`` deep-stale     cursor at 10% of log length
"""
from __future__ import annotations

import pytest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus


pytestmark = pytest.mark.perf


def _seed_bus(n: int) -> MessageBus:
    bus = MessageBus()
    for i in range(n):
        bus.post(ev.chat(sender="user", body=f"msg {i}"))
    return bus


def test_bus_post_hot(bench):
    bus = MessageBus()
    bench(lambda: bus.post(ev.chat(sender="user", body="x")),
          name="bus.post / no-subscribers", iters=500, inner=200)


def test_bus_post_with_subscriber(bench):
    bus = MessageBus()
    seen: list[int] = []
    bus.subscribe(lambda e: seen.append(e.id))
    bench(lambda: bus.post(ev.chat(sender="user", body="x")),
          name="bus.post / 1 subscriber", iters=500, inner=200)


@pytest.mark.parametrize("size", [100, 1_000, 10_000])
def test_bus_snapshot_full(bench, size):
    bus = _seed_bus(size)
    bench(lambda: bus.snapshot(),
          name=f"bus.snapshot full E={size}", iters=200, inner=10)


@pytest.mark.parametrize("size", [1_000, 10_000])
def test_bus_snapshot_tail_fresh(bench, size):
    """Cursor near the head — only the last N events are new."""
    bus = _seed_bus(size)
    cursor = size - 10
    bench(lambda: bus.snapshot(since=cursor),
          name=f"bus.snapshot since=E-10 E={size}", iters=200, inner=10)


@pytest.mark.parametrize("size", [1_000, 10_000])
def test_bus_snapshot_tail_stale(bench, size):
    """Cursor at 10% — actor was idle for ~90% of the log."""
    bus = _seed_bus(size)
    cursor = size // 10
    bench(lambda: bus.snapshot(since=cursor),
          name=f"bus.snapshot since=E*0.1 E={size}", iters=200, inner=10)


@pytest.mark.parametrize("size", [1_000, 10_000])
def test_bus_snapshot_audience(bench, size):
    """Audience filter without ``since`` — full-log walk + filter."""
    bus = _seed_bus(size)
    bench(lambda: bus.snapshot(audience="bob"),
          name=f"bus.snapshot audience=bob E={size}", iters=200, inner=10)
