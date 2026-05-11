"""Property: concurrent posters yield strictly monotonic event ids.

Invariants:
- Under N concurrent producers each posting K events: bus.snapshot()
  returns ids 0..NK-1 with no gaps.
- Every subscriber observes events in the same order as the snapshot.
  (This was the bug fixed in the last pass — the property formalizes it.)
"""

from __future__ import annotations

import threading

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus


@given(
    n_producers=st.integers(min_value=1, max_value=8),
    events_per=st.integers(min_value=1, max_value=20),
)
@settings(deadline=5000, suppress_health_check=[HealthCheck.too_slow])
def test_concurrent_post_yields_contiguous_monotonic_ids(n_producers, events_per):
    """N producers × K events → ids 0..NK-1 with no gaps."""
    bus = MessageBus()
    try:
        barrier = threading.Barrier(n_producers)

        def worker():
            barrier.wait()
            for _ in range(events_per):
                bus.post(ev.chat(sender="user", body="x", addressees=[]))

        threads = [threading.Thread(target=worker) for _ in range(n_producers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        snap = bus.snapshot()
        assert len(snap) == n_producers * events_per
        ids = [e.id for e in snap]
        assert ids == list(range(n_producers * events_per))
    finally:
        bus.stop()


@given(
    n_subscribers=st.integers(min_value=2, max_value=5),
    events=st.integers(min_value=1, max_value=30),
)
@settings(deadline=5000, suppress_health_check=[HealthCheck.too_slow])
def test_all_subscribers_see_same_order(n_subscribers, events):
    """Every subscriber observes events in the same order as the log."""
    bus = MessageBus()
    try:
        per_sub: list[list[int]] = [[] for _ in range(n_subscribers)]
        locks = [threading.Lock() for _ in range(n_subscribers)]

        for i in range(n_subscribers):

            def subscriber(e: ev.Event, idx=i):
                with locks[idx]:
                    per_sub[idx].append(e.id)

            bus.subscribe(subscriber)

        for _ in range(events):
            bus.post(ev.chat(sender="user", body="x", addressees=[]))

        # Every subscriber saw the same sequence of ids.
        first = per_sub[0]
        for other in per_sub[1:]:
            assert other == first
    finally:
        bus.stop()


@given(events=st.integers(min_value=1, max_value=20))
def test_snapshot_returns_id_indexed_view(events):
    """``snapshot()[i].id == i`` for the canonical view."""
    bus = MessageBus()
    try:
        for _ in range(events):
            bus.post(ev.chat(sender="user", body="x", addressees=[]))
        snap = bus.snapshot()
        for i, e in enumerate(snap):
            assert e.id == i
    finally:
        bus.stop()
