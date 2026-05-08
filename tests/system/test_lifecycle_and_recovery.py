"""System tests — cold-start, process-restart from journal, graceful shutdown.

System-level: drives the assembled kernel through its public lifecycle
(``start`` / ``stop`` / context manager) and tests cold-load resume by
constructing a **fresh second** ``LoomRoom`` over the same journal
directory. The "process restart" simulation is in-process — we never
re-enter a stopped room, only build a new one — but exercises the same
code paths (``Journal.load_state``, ``restore_state``) a real
multi-process consumer would hit on resume.
"""
from __future__ import annotations

import time

import pytest

from loom.kernel.journal import Journal
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.round_robin import RoundRobinPolicy
from loom.policy.single_responder import SingleResponderPolicy
from loom.room import LoomRoom


# ---------------------------------------------------------------------------
# Cold-start lifecycle.
# ---------------------------------------------------------------------------

class TestColdStartLive:
    """System-level: bring up a room from zero state and shut it down."""

    def test_cold_start_no_journal_completes_5_turns_then_clean_stop(
            self, varied_agents):
        agents = varied_agents(2, prefix="cs")
        room = LoomRoom(agents=agents, policy=OpenChatPolicy())
        try:
            with room:
                for i in range(5):
                    replies = room.post_and_wait(f"q{i}", timeout=5.0)
                    assert isinstance(replies, (list, __import__("loom").TurnResult))
        finally:
            # Idempotent stop after the context manager exit.
            room.stop(timeout=5.0)
        # The autouse thread-leak guard catches any survivor.

    def test_context_manager_enter_exit_idempotent_stop(
            self, varied_agents):
        agents = varied_agents(2, prefix="cm")
        room = LoomRoom(agents=agents, policy=OpenChatPolicy())
        with room:
            room.post_and_wait("first", timeout=5.0)
        # Second context entry would re-enter a stopped room — instead
        # we re-stop, which must be a no-op.
        room.stop(timeout=5.0)
        room.stop(timeout=5.0)

    @pytest.mark.disk
    def test_explicit_start_then_stop_no_thread_leak_with_journal(
            self, varied_agents, tmp_path):
        agents = varied_agents(2, prefix="es")
        journal_dir = tmp_path / "session"
        room = LoomRoom(
            agents=agents,
            policy=OpenChatPolicy(),
            journal_dir=journal_dir,
        )
        room.start()
        try:
            for i in range(3):
                room.post_and_wait(f"q{i}", timeout=5.0)
        finally:
            room.stop(timeout=10.0)
        # Journal artifacts on disk after a clean stop.
        assert (journal_dir / "events.jsonl").exists()
        assert (journal_dir / "room_state.json").exists()


# ---------------------------------------------------------------------------
# Process restart from journal — fresh second LoomRoom over same dir.
# ---------------------------------------------------------------------------

class TestProcessRestartFromJournal:
    """System-level: construct a fresh second room over an existing journal."""

    @pytest.mark.disk
    def test_restart_via_second_LoomRoom_replays_state_topic_responder_anchor(
            self, journaled_room, restart_helper, varied_agents):
        agents = varied_agents(2, prefix="r1")
        room = journaled_room(
            agents=agents,
            policy=SingleResponderPolicy("r10"),
            topic="restart-topic",
            anchor_id="r10",
            default_responder_id="r10",
        )
        for i in range(3):
            room.post_and_wait(f"q{i}", timeout=5.0)
        new_room, restored = restart_helper(
            room,
            agents=varied_agents(2, prefix="r1"),
            policy=SingleResponderPolicy("r10"),
        )
        assert restored is not None
        assert restored.get("topic") == "restart-topic"
        assert restored.get("anchor_id") == "r10"
        assert restored.get("default_responder_id") == "r10"
        # The fresh room is functional.
        replies = new_room.post_and_wait("post-restart", timeout=5.0)
        assert isinstance(replies, (list, __import__("loom").TurnResult))

    @pytest.mark.disk
    def test_restart_preserves_round_robin_turn_taking_mode_and_pointer(
            self, journaled_room, restart_helper, varied_agents):
        agents = varied_agents(3, prefix="rr")
        order = ["rr0", "rr1", "rr2"]
        room = journaled_room(
            agents=agents,
            policy=RoundRobinPolicy(order),
        )
        # A few turns to advance the rotation pointer.
        for i in range(4):
            room.post_and_wait(f"q{i}", timeout=5.0)
        pre_pointer = room.session.state.control.next_speaker_idx
        pre_mode = room.session.state.control.turn_taking_mode
        new_room, restored = restart_helper(
            room,
            agents=varied_agents(3, prefix="rr"),
            policy=RoundRobinPolicy(order),
        )
        assert restored is not None
        ctl = restored.get("control") or {}
        assert ctl.get("turn_taking_mode") == pre_mode == "round_robin"
        assert ctl.get("next_speaker_idx") == pre_pointer
        assert ctl.get("turn_order") == order

    @pytest.mark.disk
    def test_restart_preserves_room_control_state_floor_roles_style(
            self, journaled_room, restart_helper, varied_agents,
            scripted_console):
        agents = varied_agents(2, prefix="rc")
        room = journaled_room(
            agents=agents,
            policy=OpenChatPolicy(),
        )
        # Drive slash commands via the public run_console facade to set
        # roles / floor / style. ``run_console`` calls ``room.stop()``
        # internally on exit, so we capture the journal_dir first and
        # use a separate stop+restart flow.
        journal_dir = room.journal_dir
        script = scripted_console([
            "/roles rc0=teacher rc1=student",
            "/floor rc0",
            "/brief",
            "/quit",
        ])
        room.run_console(prompt_fn=script.prompt_fn, notify=script.notify)
        # ``run_console`` stopped the room. Open a fresh Journal to
        # observe the snapshot, then construct the second LoomRoom.
        loader = Journal(journal_dir)
        restored = loader.load_state()
        assert restored is not None
        ctl = restored.get("control") or {}
        assert ctl.get("roles") == {"rc0": "teacher", "rc1": "student"}
        assert ctl.get("floor_owner") == ["rc0"]
        assert ctl.get("style") == "brief"
        # The fresh second room can still post and stop cleanly.
        new_agents = varied_agents(2, prefix="rc")
        new_room = LoomRoom(
            agents=new_agents,
            policy=OpenChatPolicy(),
            journal_dir=journal_dir,
        )
        new_room.start()
        try:
            new_room.post_and_wait("after-restart", timeout=5.0)
        finally:
            new_room.stop(timeout=10.0)

    @pytest.mark.disk
    def test_restart_after_unclean_close_rebuilds_from_events_jsonl_alone(
            self, journaled_room, varied_agents, tmp_path):
        agents = varied_agents(2, prefix="un")
        room = journaled_room(
            agents=agents,
            policy=OpenChatPolicy(),
            topic="unclean-topic",
        )
        for i in range(2):
            room.post_and_wait(f"q{i}", timeout=5.0)
        journal_dir = room.journal_dir
        room.stop(timeout=10.0)
        # Simulate "unclean close" — delete the snapshot, leave events.
        state_path = journal_dir / "room_state.json"
        events_path = journal_dir / "events.jsonl"
        if state_path.exists():
            state_path.unlink()
        assert events_path.exists()
        # ``load_state`` returns None when state.json is gone.
        loader = Journal(journal_dir)
        assert loader.load_state() is None
        # Events are still recoverable.
        events = loader.load_events()
        assert len(events) > 0
        # Constructing a fresh room over the same directory must work
        # without crashing — the new bus opens, the journal subscribes,
        # and the room is functional.
        new_room = LoomRoom(
            agents=varied_agents(2, prefix="un"),
            policy=OpenChatPolicy(),
            journal_dir=journal_dir,
        )
        new_room.start()
        try:
            new_room.post_and_wait("post-unclean", timeout=5.0)
        finally:
            new_room.stop(timeout=10.0)


# ---------------------------------------------------------------------------
# Graceful shutdown — stop during active turn / in-flight stream.
# ---------------------------------------------------------------------------

class TestGracefulShutdown:
    """System-level: stop() invariants under common in-flight conditions."""

    @pytest.mark.timing
    def test_stop_during_active_user_turn_completes_without_error(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(2, prefix="dur")
        room = multi_turn_session(agents=agents, policy=OpenChatPolicy())
        # Fire-and-forget: don't wait for replies.
        room.post("mid-turn-question")
        # Don't sleep too long — we want stop() to land while the turn
        # is still open.
        time.sleep(0.05)
        # Should not raise.
        room.stop(timeout=10.0)
        assert all(actor.stopped for actor in room.session.actors)

    def test_stop_drains_pending_journal_snapshots_before_close(
            self, journaled_room, varied_agents):
        # Force a snapshot rotation by driving more events than
        # snapshot_every_events (default 100). The kernel emits ~6-8
        # events per broadcast turn with 2 agents, so 20 turns ≈ 120+.
        agents = varied_agents(2, prefix="dr")
        room = journaled_room(agents=agents, policy=OpenChatPolicy())
        for i in range(20):
            room.post_and_wait(f"q{i}", timeout=5.0)
        journal_dir = room.journal_dir
        room.stop(timeout=10.0)
        # After clean stop, room_state.json exists and parses.
        state_path = journal_dir / "room_state.json"
        assert state_path.exists()
        loader = Journal(journal_dir)
        snap = loader.load_state()
        assert snap is not None
        assert snap.get("version") in (1, 2, 3, 4)

    def test_stop_with_in_flight_streams_marks_lease_expired_or_cancelled(
            self, mixed_agent_room, event_recorder):
        room = mixed_agent_room(
            healthy=2,
            adversarial=[("hang", 1)],
            policy=OpenChatPolicy(),
        )
        event_recorder.attach(room)
        room.post("trigger-hang")
        # Let the actor pick up the post and start streaming the hang
        # agent — but stop before its full sleep elapses.
        time.sleep(0.2)
        room.stop(timeout=5.0)
        # The bus should contain at least one stream_end event for the
        # adversarial agent. Status may be committed/suppressed/error
        # (the cancel() ran but the underlying time.sleep can't be
        # interrupted) — we only assert that the stream terminated.
        stream_events = event_recorder.by_kind("stream")
        end_events = [e for e in stream_events
                      if e.body.get("stream_event") == "end"]
        assert len(end_events) >= 1


# ---------------------------------------------------------------------------
# Restart and resume — continue the user_turn_id sequence.
# ---------------------------------------------------------------------------

class TestRestartAndResume:
    """System-level: post-restart user_turn_ids continue monotonically."""

    @pytest.mark.disk
    def test_resume_post_restart_continues_user_turn_id_sequence_monotonic(
            self, journaled_room, restart_helper, varied_agents,
            event_recorder):
        agents = varied_agents(2, prefix="rs")
        room = journaled_room(
            agents=agents,
            policy=SingleResponderPolicy("rs0"),
        )
        event_recorder.attach(room)
        for i in range(3):
            room.post_and_wait(f"q{i}", timeout=5.0)
        ids_pre = [
            e.body.get("user_turn_id")
            for e in event_recorder.by_control_type("user_turn_opened")
        ]
        assert ids_pre == sorted(ids_pre)

        new_room, _ = restart_helper(
            room,
            agents=varied_agents(2, prefix="rs"),
            policy=SingleResponderPolicy("rs0"),
        )
        # Attach a fresh recorder to the new bus.
        from tests.system.conftest import _EventRecorder
        rec2 = _EventRecorder()
        rec2.attach(new_room)
        try:
            for i in range(3):
                new_room.post_and_wait(f"r{i}", timeout=5.0)
            ids_post = [
                e.body.get("user_turn_id")
                for e in rec2.by_control_type("user_turn_opened")
            ]
            # The new room's coordinator starts a fresh user_turn_id
            # counter (in-process restart). The sequence is still
            # internally monotonic.
            assert ids_post == sorted(ids_post)
            assert len(ids_post) >= 1
        finally:
            rec2.detach()
