"""Microbench — coordinator hot paths.

Today's hot paths:
- ``UserTurn.obligation_for`` — O(N) scan of ``obligations.values()``.
  Phase 3.1 replaces with a per-pid index.
- ``RoomCoordinator._find_recent_chat_event_id`` — O(E) reverse scan
  of ``bus.snapshot(channel='main', kinds=['chat'])`` per ``stream_end``.
  Phase 1.3 deletes this entirely (committed_event_id threading).
- Lease cap counter: ``sum(1 for l in self._leases.values() if ...)``
  — Phase 3.2 replaces with a counter when measurements warrant it.
"""
from __future__ import annotations

import pytest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.obligations import ResponseObligation, UserTurnPlan
from loom.kernel.user_turn import UserTurn


pytestmark = pytest.mark.perf


def _make_user_turn(n_obligations: int) -> UserTurn:
    required = {f"p{i}" for i in range(n_obligations)}
    plan = UserTurnPlan(
        requires_response=True,
        routing_case="direct_mention",
        required_participants=required,
        rationale="bench",
    )
    obs: dict[int, ResponseObligation] = {}
    for i in range(n_obligations):
        ob = ResponseObligation(
            id=i + 1, participant_id=f"p{i}", level="must",
            target_event_ids=[0], reason="bench",
        )
        obs[ob.id] = ob
    return UserTurn(
        id=1, user_event_id=0, started_at=0.0,
        frozen_plan=plan, obligations=obs,
    )


@pytest.mark.parametrize("n", [5, 25, 100])
def test_obligation_for_hit(bench, n):
    """Lookup that finds an obligation in the middle of the list."""
    ut = _make_user_turn(n)
    target = f"p{n // 2}"
    bench(lambda: ut.obligation_for(target),
          name=f"UserTurn.obligation_for hit N={n}",
          iters=500, inner=200)


@pytest.mark.parametrize("n", [5, 25, 100])
def test_obligation_for_miss(bench, n):
    """Lookup that finds nothing — worst case for linear scan."""
    ut = _make_user_turn(n)
    bench(lambda: ut.obligation_for("not-a-participant"),
          name=f"UserTurn.obligation_for miss N={n}",
          iters=500, inner=200)


@pytest.mark.parametrize("size", [1_000, 10_000])
def test_find_recent_chat_event_id(bench, size):
    """Reproduces coordinator._find_recent_chat_event_id today."""
    bus = MessageBus()
    for i in range(size):
        sender = "alice" if i % 2 == 0 else "bob"
        bus.post(ev.chat(sender=sender, body=f"line {i}"))

    def find():
        snap = bus.snapshot(channel="main", kinds=["chat"])
        for e in reversed(snap):
            if e.sender == "alice":
                return e.id
        return None

    bench(find, name=f"_find_recent_chat_event_id-style E={size}",
          iters=200, inner=5)


@pytest.mark.parametrize("n", [5, 25, 100])
def test_lease_cap_counter(bench, n):
    """Reproduces the per-grant ``sum(1 for l in self._leases.values() ...)``."""
    leases = {
        i: dict(holder=f"p{i % 5}", user_turn_id=1, valid=True)
        for i in range(n)
    }
    bench(
        lambda: sum(
            1 for lease in leases.values()
            if lease["user_turn_id"] == 1 and lease["valid"]
        ),
        name=f"lease cap-counter sum N={n}", iters=500, inner=200,
    )
