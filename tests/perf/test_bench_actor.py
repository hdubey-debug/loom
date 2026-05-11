"""Microbench — :class:`ParticipantActor` helpers.

Currently :meth:`ParticipantActor._lookup_event` does ``bus.snapshot()``
+ linear scan to fetch a single event by id — Phase 1.2 replaces this
with O(1) ``bus.get(id)``.
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


@pytest.mark.parametrize("size", [1_000, 10_000])
def test_lookup_event_via_snapshot(bench, size):
    """Reproduces actor._lookup_event today: snapshot + indexed access."""
    bus = _seed_bus(size)
    target = size // 2

    def fetch():
        snap = bus.snapshot()
        if 0 <= target < len(snap) and snap[target].id == target:
            return snap[target]
        for e in snap:
            if e.id == target:
                return e
        return None

    bench(fetch, name=f"actor._lookup_event-style E={size}", iters=200, inner=10)
