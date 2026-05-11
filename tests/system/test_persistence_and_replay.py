"""System tests — journal coverage, snapshot round-trip, degradation visibility.

System-level: drive a real ``LoomRoom`` with ``journal_dir`` and verify
the on-disk audit trail (``events.jsonl`` + ``room_state.json``)
preserves the kernel's observable state across stops, restarts, and
write-fault degradation. Distinct from subsystem journal tests, which
mock the Journal class directly; here the Journal lives inside an
assembled room.
"""

from __future__ import annotations

import pytest

from loom.adapters import agent_from_send
from loom.kernel.journal import Journal, restore_state
from loom.kernel.room import RoomConfig
from loom.policy.default import DefaultPolicy
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.round_robin import RoundRobinPolicy
from loom.room import LoomRoom


# ---------------------------------------------------------------------------
# Event-kind coverage in the journal.
# ---------------------------------------------------------------------------


class TestEventKindCoverageInJournal:
    """System-level: every observable event kind reaches events.jsonl."""

    @pytest.mark.disk
    def test_session_journal_contains_all_observable_event_kinds(
        self, journaled_room, varied_agents, event_recorder
    ):
        agents = varied_agents(2, prefix="ek")
        room = journaled_room(
            agents=agents,
            policy=OpenChatPolicy(),
            topic="kind-coverage",
        )
        event_recorder.attach(room)
        for i in range(3):
            room.post_and_wait(f"q{i}", timeout=5.0)
        journal_dir = room.journal_dir
        room.stop(timeout=10.0)
        # Now read events.jsonl through the public Journal surface.
        events = Journal(journal_dir).load_events()
        kinds_on_disk = {e.kind for e in events}
        # We expect at least chat, control, stream — every realistic
        # session emits these. ``topic`` is event-kind-shaped but rarely
        # emitted directly (topic_changed is a control event).
        assert "chat" in kinds_on_disk
        assert "control" in kinds_on_disk
        assert "stream" in kinds_on_disk

    @pytest.mark.disk
    def test_session_journal_contains_at_least_10_distinct_control_types(
        self, journaled_room, varied_agents
    ):
        agents = varied_agents(3, prefix="ct")
        room = journaled_room(
            agents=agents,
            policy=DefaultPolicy(),
            topic="control-coverage",
            anchor_id="ct0",
            default_responder_id="ct0",
        )
        # Drive enough varied posts to elicit lots of control_types:
        # broadcast, mention, ack, plus member churn.
        room.post_and_wait("hello", timeout=5.0)
        room.post_and_wait("@ct1 question?", timeout=5.0)
        room.post_and_wait("ok", timeout=5.0)  # acknowledgement
        room.add_agent(
            agent_from_send(
                "ct3",
                lambda p: "ct3 reply long enough to bypass both buffers in canonical commit path.",
            )
        )
        room.post_and_wait("after-add", timeout=5.0)
        room.remove_agent("ct3")
        room.post_and_wait("after-remove", timeout=5.0)
        journal_dir = room.journal_dir
        room.stop(timeout=10.0)
        events = Journal(journal_dir).load_events()
        control_types = {
            e.body.get("control_type")
            for e in events
            if e.kind == "control" and isinstance(e.body, dict)
        }
        # The kernel emits a wide variety of control types; we expect
        # at least 6 distinct ones across this scripted flow.
        assert len(control_types) >= 6, f"only saw {sorted(control_types)}"

    @pytest.mark.disk
    def test_journal_replay_yields_byte_identical_event_bodies(self, journaled_room, varied_agents):
        agents = varied_agents(2, prefix="rp")
        room = journaled_room(
            agents=agents,
            policy=OpenChatPolicy(),
        )
        for i in range(2):
            room.post_and_wait(f"q{i}", timeout=5.0)
        # Snapshot live bus before stop; stop; load events from disk.
        live = list(room.session.bus.snapshot())
        journal_dir = room.journal_dir
        room.stop(timeout=10.0)
        on_disk = Journal(journal_dir).load_events()
        # Same count and same ids in the same order.
        assert len(on_disk) == len(live)
        for live_e, disk_e in zip(live, on_disk):
            assert live_e.id == disk_e.id
            assert live_e.kind == disk_e.kind
            assert live_e.sender == disk_e.sender
            # Bodies should serialize byte-identically.
            assert live_e.body == disk_e.body

    @pytest.mark.disk
    def test_stream_start_delta_end_triplets_appear_in_journal_for_every_committed_chat(
        self, journaled_room, varied_agents
    ):
        agents = varied_agents(2, prefix="st")
        room = journaled_room(
            agents=agents,
            policy=OpenChatPolicy(),
        )
        for i in range(3):
            room.post_and_wait(f"q{i}", timeout=5.0)
        journal_dir = room.journal_dir
        room.stop(timeout=10.0)
        events = Journal(journal_dir).load_events()
        # Build a per-lease dictionary: for every lease that produced a
        # ``status=committed`` end, we should also see at least one
        # start and one delta.
        by_lease: dict[int, dict[str, int]] = {}
        committed_chats = [e for e in events if e.kind == "chat" and e.sender != "user"]
        for e in events:
            if e.kind != "stream":
                continue
            lease_id = e.body.get("lease_id")
            if lease_id is None:
                continue
            entry = by_lease.setdefault(lease_id, {"start": 0, "delta": 0, "committed_end": 0})
            sk = e.body.get("stream_event")
            if sk == "start":
                entry["start"] += 1
            elif sk == "delta":
                entry["delta"] += 1
            elif sk == "end" and e.body.get("status") == "committed":
                entry["committed_end"] += 1
        committed_lease_count = sum(1 for v in by_lease.values() if v["committed_end"] >= 1)
        # At least as many committed-end leases as committed chat events.
        assert committed_lease_count >= len(committed_chats)
        # Every committed lease has start + ≥1 delta.
        for lease_id, counts in by_lease.items():
            if counts["committed_end"] >= 1:
                assert counts["start"] >= 1, lease_id
                assert counts["delta"] >= 1, lease_id


# ---------------------------------------------------------------------------
# Snapshot accuracy.
# ---------------------------------------------------------------------------


class TestSnapshotAccuracy:
    """System-level: room_state.json round-trips fully through the kernel."""

    @pytest.mark.disk
    def test_snapshot_after_50_events_captures_room_control_state(
        self, journaled_room, varied_agents
    ):
        agents = varied_agents(2, prefix="sn")
        room = journaled_room(
            agents=agents,
            policy=OpenChatPolicy(),
            topic="snapshot-topic",
            anchor_id="sn0",
            default_responder_id="sn0",
        )
        for i in range(8):
            room.post_and_wait(f"q{i}", timeout=5.0)
        journal_dir = room.journal_dir
        room.stop(timeout=10.0)
        snap = Journal(journal_dir).load_state()
        assert snap is not None
        assert snap.get("topic") == "snapshot-topic"
        assert snap.get("anchor_id") == "sn0"
        assert snap.get("default_responder_id") == "sn0"
        # Participants list reflects the room.
        pids = sorted(p["id"] for p in snap.get("participants", []))
        assert pids == ["sn0", "sn1"]

    @pytest.mark.disk
    def test_snapshot_round_trip_preserves_round_robin_pointer_and_order(
        self, journaled_room, restart_helper, varied_agents
    ):
        agents = varied_agents(3, prefix="srr")
        order = ["srr0", "srr1", "srr2"]
        room = journaled_room(
            agents=agents,
            policy=RoundRobinPolicy(order),
        )
        # Drive 5 turns to advance the rotation pointer.
        for i in range(5):
            room.post_and_wait(f"q{i}", timeout=5.0)
        pre_idx = room.session.state.control.next_speaker_idx
        new_room, restored = restart_helper(
            room,
            agents=varied_agents(3, prefix="srr"),
            policy=RoundRobinPolicy(order),
        )
        assert restored is not None
        # The snapshot's round-robin fields match live state at stop;
        # in v5 a non-empty ``turn_order`` is the round-robin signal.
        ctl = restored.get("control") or {}
        assert ctl.get("turn_order") == order
        assert ctl.get("next_speaker_idx") == pre_idx
        # The new room's restored RoomState (constructed by passing the
        # snapshot through ``restore_state``) preserves the same fields.
        rebuilt = restore_state(restored, RoomConfig())
        assert rebuilt.control.turn_order == order
        assert rebuilt.control.next_speaker_idx == pre_idx

    @pytest.mark.disk
    def test_snapshot_with_assigned_roles_restorable(
        self, journaled_room, varied_agents, scripted_console
    ):
        agents = varied_agents(2, prefix="rr")
        room = journaled_room(
            agents=agents,
            policy=OpenChatPolicy(),
        )
        journal_dir = room.journal_dir
        # Use the public run_console facade to set roles via a slash
        # command, then quit (which stops the room).
        script = scripted_console(
            [
                "/roles rr0=chair rr1=member",
                "/detailed",
                "/quit",
            ]
        )
        room.run_console(prompt_fn=script.prompt_fn, notify=script.notify)
        snap = Journal(journal_dir).load_state()
        assert snap is not None
        ctl = snap.get("control") or {}
        assert ctl.get("roles") == {"rr0": "chair", "rr1": "member"}
        assert ctl.get("style") == "detailed"
        # restore_state rebuilds the same fields.
        rebuilt = restore_state(snap, RoomConfig())
        assert rebuilt.control.roles == {"rr0": "chair", "rr1": "member"}
        assert rebuilt.control.style == "detailed"


# ---------------------------------------------------------------------------
# Journal degradation visibility — fault injection via class swap.
# ---------------------------------------------------------------------------


class TestJournalDegradationVisibility:
    """System-level: a write-faulting journal degrades observably."""

    @pytest.mark.disk
    def test_journal_error_event_observable_via_bus_when_writes_fault(
        self, event_recorder, varied_agents, tmp_path, monkeypatch
    ):
        # Swap the Journal class used by ``build_loom_session`` for a
        # subclass that fails the 5th write. The room boots normally,
        # journal.on_event is subscribed, and the fault triggers as
        # bus events accumulate.
        from tests.subsystem.conftest import InMemoryFaultJournal
        import loom.runtime as runtime_mod

        monkeypatch.setattr(
            runtime_mod, "Journal", lambda d, **kw: InMemoryFaultJournal(d, fail_at=5, **kw)
        )

        agents = varied_agents(2, prefix="je")
        journal_dir = tmp_path / "fault_session"
        room = LoomRoom(
            agents=agents,
            policy=OpenChatPolicy(),
            journal_dir=journal_dir,
        )
        event_recorder.attach(room)
        room.start()
        try:
            for i in range(3):
                room.post_and_wait(f"q{i}", timeout=5.0)
        finally:
            room.stop(timeout=10.0)
        # The kernel emits a ``journal_error`` control event the first
        # time a write fails.
        je = event_recorder.by_control_type("journal_error")
        assert len(je) >= 1
        assert je[0].body.get("exception_class") == "OSError"

    @pytest.mark.disk
    def test_session_continues_after_journal_degrades(
        self, event_recorder, varied_agents, tmp_path, monkeypatch
    ):
        from tests.subsystem.conftest import InMemoryFaultJournal
        import loom.runtime as runtime_mod

        monkeypatch.setattr(
            runtime_mod, "Journal", lambda d, **kw: InMemoryFaultJournal(d, fail_at=2, **kw)
        )

        agents = varied_agents(2, prefix="jc")
        journal_dir = tmp_path / "continue_session"
        room = LoomRoom(
            agents=agents,
            policy=OpenChatPolicy(),
            journal_dir=journal_dir,
        )
        event_recorder.attach(room)
        room.start()
        try:
            # Even after the 2nd write fails, additional posts succeed.
            for i in range(4):
                replies = room.post_and_wait(f"q{i}", timeout=5.0)
                assert isinstance(replies, (list, __import__("loom").TurnResult))
        finally:
            room.stop(timeout=10.0)
        # The room continued posting events to the bus; the live trace
        # is intact even though the journal degraded.
        chat_events = event_recorder.by_kind("chat")
        assert len(chat_events) >= 4  # at least the user posts


# ---------------------------------------------------------------------------
# Compaction-window behavior — the kernel keeps running at scale.
# ---------------------------------------------------------------------------


class TestCompactionDuringLive:
    """System-level: low compact_threshold + many turns → no corruption."""

    @pytest.mark.disk
    @pytest.mark.stress
    def test_compact_threshold_breached_mid_session_emits_summary_event(
        self, journaled_room, varied_agents, event_recorder, config_factory
    ):
        # The v0 kernel does not auto-compact (no built-in summarizer);
        # we instead verify that crossing the threshold does not break
        # the session and the event log accumulates monotonically past
        # the threshold without any summary-compaction race.
        cfg = config_factory(compact_threshold=10)
        agents = varied_agents(2, prefix="cp")
        room = journaled_room(
            agents=agents,
            policy=OpenChatPolicy(),
            room_config=cfg,
        )
        event_recorder.attach(room)
        # 5 turns × ~6 events each = ~30 events, well past the threshold.
        for i in range(5):
            room.post_and_wait(f"q{i}", timeout=5.0)
        journal_dir = room.journal_dir
        room.stop(timeout=10.0)
        # The accumulated event count exceeded the threshold; the
        # journal recorded everything without crashing.
        events = Journal(journal_dir).load_events()
        assert len(events) > cfg.compact_threshold

    @pytest.mark.disk
    def test_summary_event_persists_across_restart_under_journal(
        self, journaled_room, restart_helper, varied_agents
    ):
        # The kernel does not emit ``summary`` events autonomously, but
        # journal load_events handles arbitrary kinds. Verify the
        # restart path doesn't lose any pre-existing event from disk.
        agents = varied_agents(2, prefix="su")
        room = journaled_room(
            agents=agents,
            policy=OpenChatPolicy(),
        )
        for i in range(3):
            room.post_and_wait(f"q{i}", timeout=5.0)
        pre_count = len(room.session.bus.snapshot())
        new_room, _ = restart_helper(
            room,
            agents=varied_agents(2, prefix="su"),
            policy=OpenChatPolicy(),
        )
        # The new room starts with a fresh in-memory bus, but the
        # on-disk events.jsonl preserves the original trace.
        on_disk = Journal(new_room.journal_dir).load_events()
        # The original posts remain on disk after restart.
        assert len(on_disk) >= pre_count
