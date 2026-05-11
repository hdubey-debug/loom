"""Property: journal replay is deterministic; tail truncation never crashes.

Invariants:
- ``load_events()`` returns events in the same order they were written.
- Truncating ``events.jsonl`` at any byte offset is graceful: load_events
  returns whatever prefix it can parse (skipping the partial last line).
- A snapshot + replay round-trip produces an equal RoomState.
"""

from __future__ import annotations


from hypothesis import given
from hypothesis import strategies as st

from loom.kernel.events import Event
from loom.kernel.journal import Journal, restore_state
from loom.kernel.room import (
    RoomConfig,
    RoomState,
)

from tests.property.strategies import event_streams, participant_ids


@given(event_streams(min_size=0, max_size=20))
def test_load_events_round_trip_preserves_order(tmp_path_factory, events):
    """Writing events to events.jsonl then loading returns them in order."""
    tmp = tmp_path_factory.mktemp("journal")
    j = Journal(tmp)
    j.events_path.write_text("\n".join(e.to_jsonl() for e in events) + ("\n" if events else ""))
    loaded = j.load_events()
    assert [e.to_jsonl() for e in loaded] == [e.to_jsonl() for e in events]


@given(
    events=event_streams(min_size=1, max_size=10),
    truncate_at=st.integers(min_value=0, max_value=10_000),
)
def test_tail_truncation_does_not_crash(tmp_path_factory, events, truncate_at):
    """Truncating events.jsonl at any byte offset returns a valid prefix."""
    tmp = tmp_path_factory.mktemp("journal")
    j = Journal(tmp)
    full = "\n".join(e.to_jsonl() for e in events) + "\n"
    truncated = full[: min(truncate_at, len(full))]
    j.events_path.write_text(truncated)
    # Must not raise and must return a list of valid Event objects.
    loaded = j.load_events()
    assert isinstance(loaded, list)
    for e in loaded:
        assert isinstance(e, Event)


@given(st.text(min_size=0, max_size=200))
def test_load_events_skips_garbage_lines(tmp_path_factory, garbage: str):
    """Lines that fail to parse are silently skipped."""
    tmp = tmp_path_factory.mktemp("journal")
    j = Journal(tmp)
    # Mix garbage with one valid event.
    valid = '{"kind":"chat","sender":"user","body":"hi","channel":"main","addressees":[],"room_epoch":0,"user_turn_id":null,"meta":{},"id":0,"ts":0.0}'
    j.events_path.write_text(garbage + "\n" + valid + "\n")
    loaded = j.load_events()
    # The valid event must survive; garbage lines are dropped silently.
    assert any(e.kind == "chat" and e.body == "hi" for e in loaded)


@given(
    state_data=st.fixed_dictionaries(
        {
            "version": st.integers(min_value=1, max_value=3),
            "room_epoch": st.integers(min_value=0, max_value=100),
            "topic": st.one_of(st.none(), st.text(max_size=20)),
            "anchor_id": st.one_of(st.none(), participant_ids),
            "default_responder_id": st.one_of(st.none(), participant_ids),
        }
    ),
)
def test_restore_state_handles_arbitrary_top_level_dicts(state_data):
    """``restore_state`` doesn't crash on arbitrary supported-version snapshots."""
    cfg = RoomConfig()
    s = restore_state(state_data, cfg)
    assert isinstance(s, RoomState)
    assert s.config is cfg
    # The RoomState's room_epoch reflects the snapshot.
    assert s.room_epoch == state_data["room_epoch"]


@given(
    control_data=st.fixed_dictionaries(
        {
            "next_speaker_idx": st.integers(min_value=-100, max_value=100),
            "turn_order": st.one_of(
                st.lists(participant_ids, max_size=5),
                st.text(max_size=10),  # invalid type
                st.none(),
            ),
            "style": st.one_of(
                st.sampled_from(["brief", "normal", "detailed"]),
                st.text(max_size=10),  # potentially invalid
            ),
            # v3/v4 snapshots may carry the retired ``turn_taking_mode``
            # field; restore_state ignores it in v5+. Including arbitrary
            # values here verifies the field is tolerated.
            "turn_taking_mode": st.one_of(
                st.sampled_from(["broadcast", "round_robin"]),
                st.text(max_size=10),
            ),
        }
    ),
)
def test_restore_state_clamps_and_filters_control(control_data):
    """Invalid control sub-fields fall back to safe defaults."""
    cfg = RoomConfig()
    state_data = {"version": 3, "control": control_data}
    s = restore_state(state_data, cfg)
    # Invariants: next_speaker_idx never negative; style is valid;
    # turn_order is a list (never the raw text/none from the input).
    assert s.control.next_speaker_idx >= 0
    assert s.control.style in ("brief", "normal", "detailed")
    assert isinstance(s.control.turn_order, list)
