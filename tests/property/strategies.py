"""Shared @composite strategies for tests/property/.

Centralizing generators here keeps each property test focused on the
invariant. Adding a new event kind means updating this file once.
"""
from __future__ import annotations

import string

from hypothesis import strategies as st

from loom.kernel import events as ev
from loom.kernel.events import Event


# ---------------------------------------------------------------------------
# Atoms
# ---------------------------------------------------------------------------

participant_alphabet = string.ascii_lowercase + string.digits + "_"
participant_ids = st.text(
    alphabet=participant_alphabet, min_size=1, max_size=12
).filter(lambda s: s and not s.isdigit())

# Channels are "main" or "dm:<participant>".
channels = st.one_of(
    st.just("main"),
    st.builds(lambda pid: f"dm:{pid}", participant_ids),
)

bodies_chat = st.text(min_size=0, max_size=80)
short_text = st.text(min_size=0, max_size=20)


# ---------------------------------------------------------------------------
# Event factories
# ---------------------------------------------------------------------------

@st.composite
def chat_events(draw):
    """A chat Event (id/ts left at zero — caller assigns)."""
    sender = draw(st.one_of(st.just("user"), participant_ids))
    body = draw(bodies_chat)
    addressees = draw(st.lists(participant_ids, max_size=4))
    channel = draw(channels)
    return ev.chat(
        sender=sender, body=body,
        addressees=addressees, channel=channel,
        room_epoch=draw(st.integers(min_value=0, max_value=100)),
    )


@st.composite
def control_events(draw):
    """Pick one of several known control_types and build it via the factory."""
    kind = draw(st.sampled_from(["topic_changed", "participant_added",
                                  "participant_removed", "user_turn_opened",
                                  "user_turn_closed", "obligation_recorded",
                                  "obligation_resolved", "dead_letter",
                                  "default_responder_changed",
                                  "roles_assigned", "floor_updated",
                                  "style_changed", "journal_error",
                                  "actor_error"]))
    if kind == "topic_changed":
        return ev.topic_changed(draw(st.one_of(st.none(), short_text)),
                                 draw(short_text))
    if kind == "participant_added":
        return ev.participant_added(draw(participant_ids))
    if kind == "participant_removed":
        return ev.participant_removed(draw(participant_ids))
    if kind == "user_turn_opened":
        return ev.user_turn_opened(
            user_turn_id=draw(st.integers(min_value=0, max_value=100)),
            routing_case=draw(st.sampled_from(
                ["direct_mention", "multi_opinion", "question",
                 "challenge", "followup", "none"])),
            required_participants=draw(
                st.lists(participant_ids, max_size=3)),
            optional_participants=draw(
                st.lists(participant_ids, max_size=3)),
            rationale=draw(short_text),
        )
    if kind == "user_turn_closed":
        return ev.user_turn_closed(
            user_turn_id=draw(st.integers(min_value=0, max_value=100)),
            reason=draw(st.sampled_from(
                ["completed", "idle_timeout", "new_user_post",
                 "cancelled", "topic_changed", "no_responder",
                 "obligation_unresolved"])),
        )
    if kind == "obligation_recorded":
        return ev.obligation_recorded(
            obligation_id=draw(st.integers(min_value=0, max_value=100)),
            participant_id=draw(participant_ids),
            level=draw(st.sampled_from(["may", "should", "must"])),
            target_event_ids=draw(
                st.lists(st.integers(min_value=0, max_value=50), max_size=3)),
            reason=draw(short_text),
        )
    if kind == "obligation_resolved":
        return ev.obligation_resolved(
            obligation_id=draw(st.integers(min_value=0, max_value=100)),
            participant_id=draw(participant_ids),
            resolved_by_event_id=draw(
                st.one_of(st.none(), st.integers(min_value=0, max_value=50))),
        )
    if kind == "dead_letter":
        return ev.dead_letter(
            original_mention_event_id=draw(st.integers(min_value=0, max_value=50)),
            reason=draw(short_text),
            reroute_to=draw(st.one_of(st.none(), participant_ids)),
        )
    if kind == "default_responder_changed":
        return ev.default_responder_changed(
            old_id=draw(st.one_of(st.none(), participant_ids)),
            new_id=draw(st.one_of(st.none(), participant_ids)),
        )
    if kind == "roles_assigned":
        return ev.roles_assigned(
            draw(st.dictionaries(participant_ids, short_text, max_size=3)),
        )
    if kind == "floor_updated":
        return ev.floor_updated(
            floor_owner=draw(
                st.one_of(st.none(), st.lists(participant_ids, max_size=3))),
            wait_for_user=draw(st.one_of(st.none(), st.booleans())),
        )
    if kind == "style_changed":
        return ev.style_changed(
            old=draw(st.sampled_from(["brief", "normal", "detailed"])),
            new=draw(st.sampled_from(["brief", "normal", "detailed"])),
        )
    if kind == "journal_error":
        return ev.journal_error(
            exception_class=draw(short_text),
            message=draw(short_text),
        )
    if kind == "actor_error":
        return ev.actor_error(
            participant_id=draw(participant_ids),
            exception_class=draw(short_text),
            message=draw(short_text),
        )
    raise AssertionError(f"unhandled control kind {kind}")


@st.composite
def stream_events(draw):
    """A stream_start/delta/end event."""
    kind = draw(st.sampled_from(["start", "delta", "end"]))
    lease_id = draw(st.integers(min_value=0, max_value=100))
    pid = draw(participant_ids)
    if kind == "start":
        return ev.stream_start(
            lease_id=lease_id, participant_id=pid,
            trigger_event_id=draw(st.integers(min_value=0, max_value=50)),
        )
    if kind == "delta":
        return ev.stream_delta(
            lease_id=lease_id, participant_id=pid,
            text=draw(bodies_chat),
        )
    return ev.stream_end(
        lease_id=lease_id, participant_id=pid,
        status=draw(st.sampled_from(["committed", "passed", "suppressed",
                                       "cancelled", "error", "lease_expired"])),
    )


events = st.one_of(chat_events(), control_events(), stream_events())


@st.composite
def event_streams(draw, min_size=1, max_size=50):
    """A list of events with id/ts assigned monotonically."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    out: list[Event] = []
    base_ts = 1_700_000_000.0
    for i in range(n):
        e = draw(events)
        e.id = i
        e.ts = base_ts + i * 0.001
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def equal_ignoring_id_ts(a: Event, b: Event) -> bool:
    """Compare two events ignoring the id/ts fields the bus assigns."""
    return (a.kind == b.kind and a.sender == b.sender
            and a.body == b.body and a.channel == b.channel
            and a.addressees == b.addressees
            and a.room_epoch == b.room_epoch
            and a.user_turn_id == b.user_turn_id
            and a.meta == b.meta)
