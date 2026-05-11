"""Targeted coverage of loom/kernel/actor.py defensive paths.

Covers actor-error post-failure branch, pending direct-mention replay,
and the linear-scan branch of `_lookup_event`.
"""

from __future__ import annotations


import pytest

from loom.kernel import events as ev
from loom.kernel.actor import ParticipantActor
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.obligations import plan_for_default
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState


@pytest.fixture
def bus_and_coord():
    """Bring up a minimal bus + coordinator + state for actor tests."""
    bus = MessageBus()
    state = RoomState(config=RoomConfig())
    state.add_participant(ParticipantInfo(id="alice", capable=True, cost_tier=1))
    state.add_participant(ParticipantInfo(id="bob", capable=True, cost_tier=1))
    coord = RoomCoordinator(bus, state)
    coord.register_participant.__self__  # noqa: B018 - sanity check
    yield bus, coord, state
    bus.stop()


def test_actor_error_post_failure_swallowed(bus_and_coord):
    """Covers: actor.py:292-293 — bus.post failure inside actor_error swallowed."""
    bus, coord, _state = bus_and_coord

    def boom_handler(actor, trigger, lease):
        raise RuntimeError("draft handler exploded")

    actor = ParticipantActor("alice", bus, coord, boom_handler)

    # Stub bus.post to fail when posting actor_error so we cover the
    # inner except branch on line 292-293.
    real_post = bus.post

    def selective_post(event):
        if event.kind == "control" and event.control_type == "actor_error":
            raise RuntimeError("bus refusing actor_error too")
        return real_post(event)

    bus.post = selective_post  # type: ignore[method-assign]

    # Trigger _step_with_error_handling: simulate by directly calling step
    # via a wrapper (the actor doesn't auto-run; that's controlled by tests).
    actor._step_with_error_handling()  # must not raise


def test_lookup_event_linear_scan_branch(bus_and_coord):
    """Covers: actor.py:387-390 — id-mismatch fast path falls through to linear scan.

    When ``0 <= event_id < len(snap)`` is true but ``snap[event_id].id !=
    event_id`` (because subscriber-visible snapshots may have been
    filtered or reordered for some audience), the linear scan finds it.
    """
    bus, coord, _state = bus_and_coord
    actor = ParticipantActor("alice", bus, coord, lambda *a, **k: None)

    # Post an event the actor will see.
    e = ev.chat(sender="bob", body="hi", addressees=["alice"])
    eid = bus.post(e)

    # Patch snapshot() to return a list where index doesn't match id.
    real_snapshot = bus.snapshot

    def reordered_snapshot(*args, **kwargs):
        snap = real_snapshot(*args, **kwargs)
        # Insert a sentinel at position 0 so snap[eid].id != eid.
        if snap:
            sentinel = ev.chat(sender="user", body="x", addressees=[])
            sentinel.id = 99999
            sentinel.ts = 0.0
            return [sentinel] + snap
        return snap

    bus.snapshot = reordered_snapshot  # type: ignore[method-assign]

    found = actor._lookup_event(eid)
    assert found is not None
    assert found.id == eid


def _open_turn_with_alice_addressed(bus, coord):
    """Open a user turn that adds an obligation for alice."""
    e = ev.chat(sender="user", body="@alice hi", addressees=["alice"])
    coord.post_user_event_and_open_turn(
        e,
        lambda posted: plan_for_default(
            "alice", reason="direct_mention", target_event_ids=[posted.id], rationale="@alice"
        ),
    )
    return e.id


def test_pending_direct_mention_replay(bus_and_coord):
    """Covers: actor.py:303-317 — pending direct mentions are re-injected on next step."""
    bus, coord, state = bus_and_coord
    actor = ParticipantActor("alice", bus, coord, lambda *a, **k: None)

    # Open a user turn so decide() can return DRAFT.
    eid = _open_turn_with_alice_addressed(bus, coord)
    actor._pending_direct_mentions.append(eid)

    # When _decide_once runs, the pending event should be replayed and
    # decide() should select it as the trigger.
    decision = actor._decide_once()
    assert decision.trigger_event_id == eid


def test_pending_direct_mention_purged_when_event_vanished(bus_and_coord):
    """Covers: actor.py:309-315 — pending mention to vanished event is dropped."""
    bus, coord, _state = bus_and_coord
    actor = ParticipantActor("alice", bus, coord, lambda *a, **k: None)

    # Seed a pending mention id that doesn't exist on the bus.
    actor._pending_direct_mentions.append(999_999)

    # Run decide; the pending mention should be silently removed because
    # _lookup_event returns None.
    actor._decide_once()
    assert 999_999 not in actor._pending_direct_mentions


def test_pending_direct_mention_already_in_snap_skipped(bus_and_coord):
    """Covers: actor.py:307-308 — pending mention already in snap is not duplicated."""
    bus, coord, _state = bus_and_coord
    actor = ParticipantActor("alice", bus, coord, lambda *a, **k: None)

    eid = _open_turn_with_alice_addressed(bus, coord)
    actor._pending_direct_mentions.append(eid)

    decision = actor._decide_once()
    assert decision.trigger_event_id == eid


def test_update_pending_mentions_adds_new_user_mentions(bus_and_coord):
    """Covers: actor.py:338-339 — newly-seen user mentions added to pending LRU."""
    bus, coord, _state = bus_and_coord
    actor = ParticipantActor("alice", bus, coord, lambda *a, **k: None)

    # Open a turn (adds obligation for alice on this trigger).
    id1 = _open_turn_with_alice_addressed(bus, coord)
    # Post a second user mention to alice in the same turn.
    e2 = ev.chat(sender="user", body="@alice second", addressees=["alice"])
    id2 = bus.post(e2)

    decision = actor._decide_once()
    # Whichever event was the trigger, the OTHER user mention should be in
    # the pending LRU.
    other = id2 if decision.trigger_event_id == id1 else id1
    assert other in actor._pending_direct_mentions
