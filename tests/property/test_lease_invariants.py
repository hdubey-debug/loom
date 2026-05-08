"""Property: lease lifecycle invariants under arbitrary action sequences.

Invariants:
- A released lease never validates again.
- An expired lease never validates.
- After a fresh acquisition the lease validates immediately (within TTL).
- ``release_lease`` is idempotent (calling twice doesn't crash).
"""
from __future__ import annotations

import time

from hypothesis import given
from hypothesis import strategies as st

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.obligations import plan_for_default
from loom.kernel.room import (
    ParticipantInfo, RoomConfig, RoomState,
)


def _open_turn_for(coord: RoomCoordinator, holder: str):
    """Open a turn that gives ``holder`` a must-obligation."""
    e = ev.chat(sender="user", body=f"@{holder} hi", addressees=[holder])
    coord.post_user_event_and_open_turn(
        e,
        lambda posted: plan_for_default(
            holder, reason="direct_mention",
            target_event_ids=[posted.id],
            rationale=f"@{holder}"),
    )
    return e


def _make_coord(pid: str = "alice"):
    bus = MessageBus()
    state = RoomState(config=RoomConfig())
    state.add_participant(ParticipantInfo(id=pid, capable=True, cost_tier=1))
    coord = RoomCoordinator(bus, state)
    return bus, coord


@given(n_acquire_release=st.integers(min_value=1, max_value=10))
def test_released_lease_never_validates(n_acquire_release):
    """A released lease remains invalid forever."""
    bus, coord = _make_coord()
    try:
        for _ in range(n_acquire_release):
            e = _open_turn_for(coord, "alice")
            lease = coord.acquire_lease("alice", e.id, is_direct_mention=True)
            assert lease is not None
            assert coord.validate_lease(lease) is True
            coord.release_lease(lease)
            assert coord.validate_lease(lease) is False
            coord.close_user_turn("cancelled")
    finally:
        bus.stop()


@given(ttl_seconds=st.floats(min_value=0.001, max_value=0.1))
def test_expired_lease_never_validates(ttl_seconds):
    """A lease past its expires_at never validates."""
    bus = MessageBus()
    state = RoomState(config=RoomConfig(lease_ttl_s=ttl_seconds))
    state.add_participant(ParticipantInfo(id="alice", capable=True, cost_tier=1))
    coord = RoomCoordinator(bus, state)
    try:
        e = _open_turn_for(coord, "alice")
        lease = coord.acquire_lease("alice", e.id, is_direct_mention=True)
        assert lease is not None

        # Advance time past expiry.
        time.sleep(ttl_seconds + 0.01)
        assert coord.validate_lease(lease) is False
    finally:
        bus.stop()


@given(_n=st.integers(min_value=1, max_value=5))
def test_release_lease_is_idempotent(_n):
    """Calling release_lease twice is a no-op the second time."""
    bus, coord = _make_coord()
    try:
        e = _open_turn_for(coord, "alice")
        lease = coord.acquire_lease("alice", e.id, is_direct_mention=True)
        assert lease is not None
        coord.release_lease(lease)
        # Second call must not raise.
        coord.release_lease(lease)
        # The lease remains invalid.
        assert coord.validate_lease(lease) is False
    finally:
        bus.stop()


@given(_n=st.integers(min_value=1, max_value=5))
def test_freshly_acquired_lease_validates(_n):
    """A lease just returned by acquire_lease validates within TTL."""
    bus, coord = _make_coord()
    try:
        e = _open_turn_for(coord, "alice")
        lease = coord.acquire_lease("alice", e.id, is_direct_mention=True)
        assert lease is not None
        assert coord.validate_lease(lease) is True
    finally:
        bus.stop()


@given(epoch_bumps=st.integers(min_value=1, max_value=5))
def test_epoch_bump_invalidates_existing_leases(epoch_bumps):
    """Adding/removing participants bumps the epoch; leases lose validity."""
    bus, coord = _make_coord()
    try:
        e = _open_turn_for(coord, "alice")
        lease = coord.acquire_lease("alice", e.id, is_direct_mention=True)
        assert lease is not None
        prior_epoch = coord.state.room_epoch

        # Bump the epoch by add+remove.
        for i in range(epoch_bumps):
            tmp_id = f"tmp{i}"
            coord.register_participant(
                ParticipantInfo(id=tmp_id, capable=True, cost_tier=1))
            coord.unregister_participant(tmp_id)

        assert coord.state.room_epoch != prior_epoch
        # The lease's room_epoch is stale → validate_lease returns False.
        assert coord.validate_lease(lease) is False
    finally:
        bus.stop()
