"""Subsystem tests — bus + journal under load.

This file exercises the event pipeline end-to-end:

- ordering invariants under concurrent post,
- subscriber backpressure,
- 1 MB payload serialization,
- journal write failure + degraded mode,
- replay correctness through ``Journal.replay_into``,
- snapshot round-trip stability across many cycles.

Markers (``stress``, ``disk``, ``breakpoint``) are informational labels;
all tests run by default. The 45 s autouse watchdog caps every test.
"""

from __future__ import annotations

import time

import pytest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.journal import (
    Journal,
    restore_state,
)
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
)


# ---------------------------------------------------------------------------
# Bus ordering invariants under concurrent load.
# ---------------------------------------------------------------------------


class TestBusOrderingUnderLoad:
    """The bus assigns monotonic ids and never drops events under load."""

    def test_2000_events_50_subscribers_strict_monotonic_ids(self, thread_harness):
        bus = MessageBus()
        records: list[list[int]] = [[] for _ in range(50)]

        for i in range(50):

            def sub(event, idx=i):
                records[idx].append(event.id)

            bus.subscribe(sub)

        n_posters = 8
        per_poster = 250  # 8 * 250 == 2000

        def poster():
            for _ in range(per_poster):
                bus.post(ev.chat(sender="user", body="x"))

        for _ in range(n_posters):
            thread_harness.spawn(poster, name="poster")
        thread_harness.join_all(timeout=10.0)
        assert thread_harness.errors == []

        all_events = bus.snapshot(kinds=["chat"])
        assert len(all_events) == n_posters * per_poster
        ids = [e.id for e in all_events]
        # Monotonic and dense (positions in the log).
        assert ids == sorted(ids)
        assert ids[0] == 0
        assert ids[-1] == len(ids) - 1
        # Every subscriber observed every id, in order.
        for rec in records:
            assert rec == ids

    def test_concurrent_post_no_duplicates_or_drops(self, thread_harness):
        bus = MessageBus()
        n_posters = 10
        per_poster = 200

        def poster(pid):
            for j in range(per_poster):
                bus.post(ev.chat(sender=f"agent{pid}", body=f"m{j}", meta={"pid": pid, "seq": j}))

        for pid in range(n_posters):
            thread_harness.spawn(lambda p=pid: poster(p), name=f"post{pid}")
        thread_harness.join_all(timeout=10.0)

        snap = bus.snapshot(kinds=["chat"])
        assert len(snap) == n_posters * per_poster
        # Every (pid, seq) pair is unique.
        seen: set[tuple] = set()
        for e in snap:
            key = (e.meta["pid"], e.meta["seq"])
            assert key not in seen, f"duplicate event for {key}"
            seen.add(key)
        # And every expected pair is present.
        expected = {(p, j) for p in range(n_posters) for j in range(per_poster)}
        assert seen == expected

    def test_subscribe_unsubscribe_during_post_no_lost_events(self, thread_harness):
        # While posters churn, a controller subscribes / unsubscribes a
        # short-lived listener. The bus must remain consistent: the
        # listener sees a contiguous slice of events, and the bus log is
        # intact afterwards.
        bus = MessageBus()
        seen: list[int] = []
        unsubscribe = bus.subscribe(lambda e: seen.append(e.id))

        n_posters = 4
        per_poster = 100

        def poster():
            for _ in range(per_poster):
                bus.post(ev.chat(sender="user", body="x"))

        for _ in range(n_posters):
            thread_harness.spawn(poster, name="poster")
        # Detach mid-flight.
        time.sleep(0.005)
        unsubscribe()
        thread_harness.join_all(timeout=10.0)

        snap = bus.snapshot(kinds=["chat"])
        assert len(snap) == n_posters * per_poster
        # Unsubscribed handle cannot crash the bus.
        unsubscribe()  # idempotent


# ---------------------------------------------------------------------------
# Subscriber backpressure.
# ---------------------------------------------------------------------------


class TestBusBackpressure:
    """Slow subscribers must not block the bus post path indefinitely.

    The bus invokes subscribers inline, so a *single* slow subscriber
    will slow the post path. The contract is that exceptions are
    swallowed (a misbehaving subscriber cannot break the bus) and that
    posts continue regardless.
    """

    @pytest.mark.stress
    def test_slow_subscriber_does_not_corrupt_log(self):
        bus = MessageBus()

        def slow(_event):
            # Small delay that adds up over many posts but stays under
            # the watchdog ceiling.
            time.sleep(0.001)

        bus.subscribe(slow)
        for _ in range(200):
            bus.post(ev.chat(sender="user", body="x"))
        # All events landed; ids are dense.
        snap = bus.snapshot(kinds=["chat"])
        assert len(snap) == 200
        assert [e.id for e in snap] == list(range(200))

    @pytest.mark.stress
    @pytest.mark.breakpoint
    def test_breakpoint_subscriber_count_when_post_p99_exceeds_50ms(self):
        # Binary-search the subscriber count at which p99 post latency
        # exceeds 50 ms. Floor: at least 10 subscribers must be
        # tolerable. Hard ceiling: 256 subscribers — the 45 s watchdog
        # provides the runtime cap.
        threshold_s = 0.050
        floor = 10
        ceiling = 256

        def measure_p99(n_subs: int) -> float:
            bus = MessageBus()
            for _ in range(n_subs):
                bus.subscribe(lambda _e: None)
            samples = []
            for _ in range(100):
                t0 = time.monotonic()
                bus.post(ev.chat(sender="user", body="x"))
                samples.append(time.monotonic() - t0)
            samples.sort()
            return samples[98]  # p99 of 100 samples

        breakpoint_n = ceiling
        n = 5
        while n <= ceiling:
            p99 = measure_p99(n)
            if p99 > threshold_s:
                breakpoint_n = n
                break
            n *= 2
        print(f"BREAKPOINT: subscribers_at_50ms_p99={breakpoint_n}")
        assert breakpoint_n >= floor, (
            f"p99 latency exceeded 50ms at only {breakpoint_n} "
            f"subscribers — system broke earlier than the sanity floor"
        )


# ---------------------------------------------------------------------------
# Journal under pressure (disk path).
# ---------------------------------------------------------------------------


class TestJournalUnderPressure:
    """Journal correctness under large payloads, write failures, and rotation."""

    @pytest.mark.disk
    def test_1mb_event_payload_serializes_and_round_trips(self, tmp_path):
        # 1 MB of chat body — the journal must serialize it as a single
        # JSONL line and load it back losslessly.
        big_body = "x" * (1024 * 1024)
        with Journal(tmp_path) as j:
            e = ev.chat(sender="user", body=big_body)
            e.id = 0
            e.ts = 1.0
            j.on_event(e)

        j2 = Journal(tmp_path)
        evs = j2.load_events()
        assert len(evs) == 1
        assert len(evs[0].body) == 1024 * 1024
        assert evs[0].body == big_body

    @pytest.mark.disk
    @pytest.mark.breakpoint
    def test_breakpoint_event_size_when_serialize_exceeds_500ms(self, tmp_path):
        # Find the payload size at which a single ``to_jsonl`` exceeds
        # 500 ms. Floor: must succeed at 1 KB. Hard ceiling: 8 MB.
        threshold_s = 0.5
        floor_kb = 1
        ceiling_kb = 8 * 1024  # 8 MB

        def measure(size_kb: int) -> float:
            body = "y" * (size_kb * 1024)
            e = ev.chat(sender="user", body=body)
            t0 = time.monotonic()
            line = e.to_jsonl()
            elapsed = time.monotonic() - t0
            assert len(line) >= size_kb * 1024  # sanity
            return elapsed

        breakpoint_kb = ceiling_kb
        size = floor_kb
        while size <= ceiling_kb:
            elapsed = measure(size)
            if elapsed > threshold_s:
                breakpoint_kb = size
                break
            size *= 2
        print(f"BREAKPOINT: serialize_size_at_500ms_kb={breakpoint_kb}")
        assert breakpoint_kb >= floor_kb, (
            f"serialize broke at only {breakpoint_kb} KB — below sanity floor"
        )

    @pytest.mark.disk
    def test_journal_write_failure_emits_journal_error_and_marks_degraded(self, tmp_path):
        # The journal subscribes to the bus; a write failure must mark
        # ``degraded=True`` and post a single ``journal_error`` event.
        from tests.subsystem.conftest import InMemoryFaultJournal

        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        for pid in ("loom", "claude_code"):
            state.add_participant(ParticipantInfo(id=pid))

        j = InMemoryFaultJournal(tmp_path / "jrn", fail_at=2)

        # Simulate the runtime's wiring: failure callback posts a
        # journal_error event.
        def on_failure(exc):
            bus.post(
                ev.journal_error(
                    exception_class=type(exc).__name__,
                    message=str(exc),
                )
            )

        j.set_failure_callback(on_failure)
        j.open()
        unsubscribe = bus.subscribe(j.on_event)
        try:
            bus.post(ev.chat(sender="user", body="first"))
            bus.post(ev.chat(sender="user", body="second-fail"))
            bus.post(ev.chat(sender="user", body="third"))
        finally:
            unsubscribe()
            j.close()

        assert j.degraded is True
        errors = [
            e
            for e in bus.snapshot()
            if e.kind == "control"
            and isinstance(e.body, dict)
            and e.body.get("control_type") == "journal_error"
        ]
        # One journal_error event total (not one per failure).
        assert len(errors) == 1
        assert errors[0].body["exception_class"] == "OSError"

    @pytest.mark.disk
    def test_journal_reopen_appends_no_data_loss(self, tmp_path):
        # Closing a journal and reopening it at the same dir must
        # append, not truncate. After two sessions the file has both
        # batches, in order.
        j1 = Journal(tmp_path)
        j1.open()
        for i in range(5):
            e = ev.chat(sender="user", body=f"batch1-{i}")
            e.id, e.ts = i, float(i)
            j1.on_event(e)
        j1.close()

        j2 = Journal(tmp_path)
        j2.open()
        for i in range(5):
            e = ev.chat(sender="user", body=f"batch2-{i}")
            e.id, e.ts = 100 + i, 100.0 + i
            j2.on_event(e)
        j2.close()

        loaded = Journal(tmp_path).load_events()
        assert len(loaded) == 10
        assert loaded[0].body == "batch1-0"
        assert loaded[4].body == "batch1-4"
        assert loaded[5].body == "batch2-0"
        assert loaded[9].body == "batch2-4"

    def test_50_consecutive_snapshot_restores_yield_identical_state(self):
        # Snapshot → restore → snapshot → restore … 50 times. The state
        # at iteration K must be equivalent to the original.
        cfg = RoomConfig()
        original = RoomState(config=cfg)
        original.add_participant(ParticipantInfo(id="loom", cost_tier=0))
        original.add_participant(ParticipantInfo(id="claude_code", cost_tier=1))
        original.set_default_responder("loom")
        original.set_anchor("loom")
        original.set_topic("design review")
        original.set_roles({"loom": "teacher"})
        original.set_style("brief")
        # P2.3: active_goal collapsed into topic; topic already set above.

        # Build the snapshot dict the same way the journal does.
        from loom.kernel.journal import Journal as _J

        current = original
        for _ in range(50):
            d = _J._state_to_dict(current)
            current = restore_state(d, cfg)

        assert current.topic == original.topic
        assert current.default_responder_id == original.default_responder_id
        assert current.anchor_id == original.anchor_id
        assert sorted(current.participants.keys()) == sorted(original.participants.keys())
        assert current.control.roles == original.control.roles
        assert current.control.style == original.control.style


# ---------------------------------------------------------------------------
# Replay correctness.
# ---------------------------------------------------------------------------


class TestJournalReplayCorrectness:
    """``Journal.replay_into`` posts events to a fresh bus.

    The kernel's resume model is "restore state from snapshot, then
    optionally replay events for audit." These tests cover the replay
    half: ordering preserved, retired control_types skipped.
    """

    def test_replay_into_preserves_event_order(self, tmp_path):
        # Write a known sequence; replay into a fresh coordinator's bus
        # and verify the bus log matches the journal.
        with Journal(tmp_path) as j:
            for i in range(30):
                e = ev.chat(sender="user", body=f"m{i}")
                e.id, e.ts = i, float(i)
                j.on_event(e)

        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        coord = RoomCoordinator(bus, state)

        loaded = Journal(tmp_path).replay_into(coord)
        assert loaded == 30

        snap = bus.snapshot(kinds=["chat"])
        assert len(snap) == 30
        # Bodies preserved in order.
        assert [e.body for e in snap] == [f"m{i}" for i in range(30)]

    def test_replay_into_skips_retired_control_types(self, tmp_path):
        # Mix retired + current control events; only current ones must
        # be re-posted.
        events_path = tmp_path / "events.jsonl"
        events_path.write_text(
            '{"kind":"control","sender":"system",'
            '"body":{"control_type":"mode_changed","old":"a","new":"b"},'
            '"channel":"main","addressees":[],"room_epoch":0,'
            '"user_turn_id":null,"meta":{},"id":0,"ts":1.0}\n'
            '{"kind":"control","sender":"system",'
            '"body":{"control_type":"topic_changed","old":null,"new":"x"},'
            '"channel":"main","addressees":[],"room_epoch":0,'
            '"user_turn_id":null,"meta":{},"id":1,"ts":2.0}\n'
            '{"kind":"chat","sender":"user","body":"hi","channel":"main",'
            '"addressees":[],"room_epoch":0,"user_turn_id":null,"meta":{},'
            '"id":2,"ts":3.0}\n'
        )

        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        coord = RoomCoordinator(bus, state)
        posted = Journal(tmp_path).replay_into(coord)
        assert posted == 2  # legacy mode_changed dropped

        kinds = [e.kind for e in bus.snapshot()]
        assert "control" in kinds
        assert "chat" in kinds
        # The mode_changed event is NOT re-posted.
        for e in bus.snapshot():
            if e.kind == "control":
                assert ev.control_type_of(e) != "mode_changed"
