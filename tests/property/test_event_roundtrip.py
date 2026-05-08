"""Property: every event survives a JSONL round-trip unchanged.

Invariant: ``Event.from_jsonl(e.to_jsonl()) == e`` for every event kind
and field combination, including the reserved ``id``/``ts`` slots.
"""
from __future__ import annotations

from hypothesis import given

from loom.kernel.events import Event, is_known_control

from tests.property.strategies import (
    chat_events,
    control_events,
    event_streams,
    stream_events,
)


@given(chat_events())
def test_chat_event_roundtrip(e: Event):
    """Chat events round-trip via to_jsonl/from_jsonl."""
    assert Event.from_jsonl(e.to_jsonl()) == e


@given(control_events())
def test_control_event_roundtrip(e: Event):
    """Control events round-trip via to_jsonl/from_jsonl."""
    assert Event.from_jsonl(e.to_jsonl()) == e


@given(stream_events())
def test_stream_event_roundtrip(e: Event):
    """Stream events round-trip via to_jsonl/from_jsonl."""
    assert Event.from_jsonl(e.to_jsonl()) == e


@given(event_streams(max_size=20))
def test_event_stream_roundtrip(events: list[Event]):
    """Every event in an event stream survives a round-trip."""
    for e in events:
        decoded = Event.from_jsonl(e.to_jsonl())
        assert decoded == e


@given(control_events())
def test_known_control_predicate_is_stable_across_roundtrip(e: Event):
    """``is_known_control`` agrees on the original and decoded event."""
    decoded = Event.from_jsonl(e.to_jsonl())
    assert is_known_control(e) == is_known_control(decoded)


@given(event_streams(max_size=15))
def test_decoded_jsonl_lines_each_parse_independently(events: list[Event]):
    """Concatenating to_jsonl outputs and decoding line-by-line is loss-less."""
    encoded = "\n".join(e.to_jsonl() for e in events)
    decoded = [Event.from_jsonl(line) for line in encoded.split("\n") if line]
    assert decoded == events
