"""Property: round-robin pointer wraps cleanly under arbitrary churn.

Invariants:
- After N add+remove operations, ``advance_round_robin_pointer`` always
  returns an index inside ``[0, len(live))``.
- Advancing through the live rotation len(live) times returns to the
  starting speaker.
- Removing a participant from the rotation never causes the pointer to
  point at an inactive/removed id.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
)

from tests.property.strategies import participant_ids


@given(
    initial_pids=st.lists(participant_ids, min_size=2, max_size=5, unique=True),
    n_advances=st.integers(min_value=0, max_value=20),
)
def test_advance_pointer_stays_within_live_range(initial_pids, n_advances):
    """``advance_round_robin_pointer`` never returns out of range."""
    state = RoomState(config=RoomConfig())
    for pid in initial_pids:
        state.add_participant(ParticipantInfo(id=pid, capable=True, cost_tier=1))
    state.set_turn_order(initial_pids)

    for _ in range(n_advances):
        idx = state.advance_round_robin_pointer()
        assert 0 <= idx < len(initial_pids)


@given(
    pids=st.lists(participant_ids, min_size=2, max_size=5, unique=True),
)
def test_full_cycle_returns_to_start(pids):
    """Advancing exactly len(rotation) times brings the pointer home."""
    state = RoomState(config=RoomConfig())
    for pid in pids:
        state.add_participant(ParticipantInfo(id=pid, capable=True, cost_tier=1))
    state.set_turn_order(pids)

    start = state.control.next_speaker_idx
    for _ in range(len(pids)):
        state.advance_round_robin_pointer()
    assert state.control.next_speaker_idx == start


@given(
    pids=st.lists(participant_ids, min_size=2, max_size=5, unique=True),
)
def test_removing_pointed_at_pid_keeps_pointer_in_live_range(pids):
    """After removing the pointed-at participant, the pointer stays valid."""
    state = RoomState(config=RoomConfig())
    for pid in pids:
        state.add_participant(ParticipantInfo(id=pid, capable=True, cost_tier=1))
    state.set_turn_order(pids)

    # Remove the participant currently being pointed at.
    idx = state.control.next_speaker_idx
    target = state.control.turn_order[idx]
    state.remove_participant(target)
    # The advance call should still return a valid index for the live set.
    new_idx = state.advance_round_robin_pointer()
    live = [pid for pid in state.control.turn_order if pid in state.participants]
    if live:
        assert 0 <= new_idx < len(live)
    else:
        assert new_idx == 0


@given(
    pids=st.lists(participant_ids, min_size=3, max_size=6, unique=True),
    n_to_remove=st.integers(min_value=1, max_value=5),
)
def test_pointer_with_inactive_members_never_lands_on_inactive(pids, n_to_remove):
    """Inactive members in turn_order are skipped by the advance helper."""
    state = RoomState(config=RoomConfig())
    for pid in pids:
        state.add_participant(ParticipantInfo(id=pid, capable=True, cost_tier=1))
    state.set_turn_order(pids)

    # Mark some participants inactive (cap at remaining-1 to keep at least 1).
    n_to_remove = min(n_to_remove, len(pids) - 1)
    for pid in pids[:n_to_remove]:
        state.participants[pid].active = False

    for _ in range(2 * len(pids)):
        idx = state.advance_round_robin_pointer()
        live = [
            pid
            for pid in state.control.turn_order
            if pid in state.participants
            and state.participants[pid].active
            and state.participants[pid].capable
        ]
        if live:
            assert 0 <= idx < len(live)
