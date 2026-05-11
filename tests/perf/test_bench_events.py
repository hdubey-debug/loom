"""Microbench — :class:`Event` allocation + JSONL serialize.

Phase 2 will rewrite ``to_jsonl`` (drop the ``asdict`` deepcopy) and
add ``slots=True``. These benchmarks are the before/after.
"""

from __future__ import annotations

import pytest

from loom.kernel import events as ev


pytestmark = pytest.mark.perf


def test_event_chat_alloc(bench):
    bench(
        lambda: ev.chat(sender="alice", body="hello"), name="Event chat alloc", iters=500, inner=500
    )


def test_event_chat_alloc_with_addressees(bench):
    bench(
        lambda: ev.chat(sender="alice", body="hi @bob", addressees=["bob"]),
        name="Event chat alloc + addressees",
        iters=500,
        inner=500,
    )


def test_event_control_alloc(bench):
    bench(
        lambda: ev.user_turn_opened(
            user_turn_id=1,
            routing_case="direct_mention",
            required_participants=["bob"],
            rationale="@bob",
        ),
        name="Event control user_turn_opened",
        iters=500,
        inner=200,
    )


def test_to_jsonl_chat(bench):
    e = ev.chat(sender="alice", body="hello world from alice", addressees=["bob"], room_epoch=5)
    e.id = 7
    e.ts = 1_700_000_000.0
    bench(lambda: e.to_jsonl(), name="Event.to_jsonl chat", iters=500, inner=200)


def test_to_jsonl_control(bench):
    e = ev.user_turn_opened(
        user_turn_id=1,
        routing_case="direct_mention",
        required_participants=["bob", "carol"],
        optional_participants=["dave"],
        rationale="@bob @carol",
    )
    e.id = 9
    e.ts = 1_700_000_000.0
    bench(lambda: e.to_jsonl(), name="Event.to_jsonl control", iters=500, inner=200)


def test_to_jsonl_stream_delta(bench):
    e = ev.stream_delta(lease_id=1, participant_id="alice", text="Lorem ipsum dolor sit amet" * 4)
    e.id = 11
    e.ts = 1_700_000_000.0
    bench(lambda: e.to_jsonl(), name="Event.to_jsonl stream_delta", iters=500, inner=200)


def test_from_jsonl_chat(bench):
    e = ev.chat(sender="alice", body="hello world from alice", addressees=["bob"], room_epoch=5)
    e.id = 7
    e.ts = 1_700_000_000.0
    line = e.to_jsonl()
    bench(lambda: type(e).from_jsonl(line), name="Event.from_jsonl chat", iters=500, inner=200)
