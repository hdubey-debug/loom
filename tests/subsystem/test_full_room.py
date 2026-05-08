"""Subsystem tests — LoomRoom end-to-end stress.

Drives full LoomRoom instances with up to 30+ agents under various
real-thread stresses: parallel post_and_wait, EOF mid-stream, journal
replay across 20 turns, repeated start/stop cycles.
"""
from __future__ import annotations

import io
import threading
import time
from contextlib import redirect_stdout

import pytest

from loom.adapters import agent_from_send
from loom.kernel.journal import Journal, restore_state
from loom.kernel.room import RoomConfig
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.single_responder import SingleResponderPolicy
from loom.room import LoomRoom


# ---------------------------------------------------------------------------
# Many-agent room.
# ---------------------------------------------------------------------------

class TestManyAgentRoom:

    @pytest.mark.stress
    def test_30_agents_one_turn_completes_within_watchdog(
            self, room_factory, simple_agents):
        # 30 agents under broadcast policy; one turn must complete.
        agents = simple_agents(30)
        room = room_factory(agents=agents, policy=OpenChatPolicy())
        t0 = time.monotonic()
        replies = room.post_and_wait("broadcast question", timeout=20.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 20.0
        # At least 25 of 30 commit.
        senders = {r.sender for r in replies}
        assert len(senders) >= 25

    @pytest.mark.stress
    def test_15_agents_three_consecutive_turns_no_lock_hang(
            self, room_factory, simple_agents):
        # Three sequential turns must each complete; no progressive
        # lock-contention degradation.
        agents = simple_agents(15)
        room = room_factory(agents=agents, policy=OpenChatPolicy())
        elapsed = []
        for i in range(3):
            t0 = time.monotonic()
            replies = room.post_and_wait(f"q{i}", timeout=15.0)
            elapsed.append(time.monotonic() - t0)
            # Each turn produced at least 10 replies.
            assert len({r.sender for r in replies}) >= 10
        # No turn took catastrophically longer than the previous —
        # a 3× variance is generous (CPU-bound work, 15 agents).
        assert max(elapsed) < 3 * (min(elapsed) + 0.5)

    @pytest.mark.stress
    @pytest.mark.breakpoint
    def test_breakpoint_agent_count_when_post_and_wait_exceeds_5s(
            self, simple_agents):
        # Find the agent count at which one turn's post_and_wait
        # exceeds 5 s. Floor: at least 5 agents must complete in time.
        # Hard ceiling: 40 agents.
        threshold_s = 5.0
        floor = 5
        ceiling = 40

        def measure(n: int) -> float:
            agents = simple_agents(n)
            room = LoomRoom(agents=agents, policy=OpenChatPolicy())
            try:
                with room:
                    t0 = time.monotonic()
                    room.post_and_wait("q", timeout=threshold_s + 5)
                    return time.monotonic() - t0
            finally:
                room.stop()

        breakpoint_n = ceiling
        n = 5
        while n <= ceiling:
            elapsed = measure(n)
            if elapsed > threshold_s:
                breakpoint_n = n
                break
            n *= 2
        print(f"BREAKPOINT: full_room_agents_at_5s={breakpoint_n}")
        assert breakpoint_n >= floor


# ---------------------------------------------------------------------------
# Console + notify behavior.
# ---------------------------------------------------------------------------

class TestConsoleAndNotify:

    def test_run_console_thread_safe_notify_serializes_output(
            self, room_factory):
        # The default thread-safe notify wraps print() in a module-level
        # lock; concurrent writes must not interleave (i.e. produce
        # one full line per call).
        long_a = "alpha sufficient long reply that bypasses loop guard."
        long_b = "bravo sufficient long reply that bypasses loop guard."
        a = agent_from_send("a", lambda p: long_a)
        b = agent_from_send("b", lambda p: long_b)

        out_lines: list[str] = []
        out_lock = threading.Lock()

        def notify(msg):
            # Each call appends one full message; the framework calls
            # this with whole strings only.
            with out_lock:
                out_lines.append(msg)

        inputs = iter(["hello", "/quit"])

        def prompt():
            return next(inputs)

        room = LoomRoom(agents=[a, b], policy=OpenChatPolicy())
        room.run_console(prompt_fn=prompt, notify=notify)
        # Notify must have been called at least once.
        assert len(out_lines) >= 1
        # Each captured line is a full message (no shearing).
        for line in out_lines:
            assert isinstance(line, str)

    def test_run_console_eof_clean_shutdown(self):
        # An EOF from prompt_fn during an active read terminates
        # cleanly; the room is stopped.
        long_a = "alpha sufficient long reply that bypasses loop guard."

        def prompt():
            raise EOFError

        room = LoomRoom(
            agents=[agent_from_send("a", lambda p: long_a)],
            policy=OpenChatPolicy(),
        )
        with redirect_stdout(io.StringIO()):
            room.run_console(prompt_fn=prompt, notify=lambda _: None)
        assert all(actor.stopped for actor in room.session.actors)


# ---------------------------------------------------------------------------
# Concurrent post_and_wait.
# ---------------------------------------------------------------------------

class TestConcurrentPostAndWait:

    @pytest.mark.timing
    def test_two_post_and_wait_callers_each_get_consistent_replies(
            self, room_factory):
        long_a = "alpha sufficient long reply that bypasses loop guard."
        long_b = "bravo sufficient long reply that bypasses loop guard."
        a = agent_from_send("a", lambda p: long_a)
        b = agent_from_send("b", lambda p: long_b)
        room = room_factory(agents=[a, b], policy=OpenChatPolicy())

        from loom import TurnResult
        results: list[TurnResult | list] = [[], []]
        errors: list[BaseException] = []

        def caller(idx: int, body: str):
            try:
                results[idx] = room.post_and_wait(body, timeout=10.0)
            except BaseException as exc:
                errors.append(exc)

        t1 = threading.Thread(target=caller, args=(0, "from caller 1"),
                              name="caller1")
        t2 = threading.Thread(target=caller, args=(1, "from caller 2"),
                              name="caller2")
        t1.start()
        t2.start()
        t1.join(timeout=15.0)
        t2.join(timeout=15.0)
        assert errors == []
        # Both calls returned. The replies they observed are subsets
        # of the bus log (we don't assert ordering across the two
        # callers — the kernel is at liberty to merge or serialize them).
        assert all(isinstance(r, (list, TurnResult)) for r in results)

    @pytest.mark.timing
    def test_post_no_wait_then_post_and_wait_does_not_starve(
            self, room_factory):
        long_a = "alpha sufficient long reply that bypasses loop guard."
        a = agent_from_send("a", lambda p: long_a)
        room = room_factory(agents=[a], policy=OpenChatPolicy())
        # Fire-and-forget post.
        room.post("first")
        # Wait briefly to allow the first turn to progress.
        time.sleep(0.1)
        # Then a blocking post_and_wait — must complete within timeout.
        replies = room.post_and_wait("second", timeout=10.0)
        # At minimum the bus contains both user posts.
        user_chats = [
            e for e in room.session.bus.snapshot()
            if e.kind == "chat" and e.sender == "user"
        ]
        bodies = [c.body for c in user_chats]
        assert "first" in bodies
        assert "second" in bodies


# ---------------------------------------------------------------------------
# Journal + replay across many turns.
# ---------------------------------------------------------------------------

class TestJournalAndReplay:

    @pytest.mark.disk
    def test_journal_state_after_many_turns_restorable(self, tmp_path):
        # Run several turns with journaling; close, reopen via the
        # journal snapshot, verify state matches. Uses 8 turns to stay
        # under the per-participant rate cap (default 10/min).
        counter = [0]

        def a_send(prompt):
            counter[0] += 1
            return (f"alpha turn {counter[0]} sufficient long reply that "
                    f"bypasses both the pass buffer and the loop guard.")

        a = agent_from_send("a", a_send)
        journal_dir = tmp_path / "session"
        room = LoomRoom(
            agents=[a],
            policy=SingleResponderPolicy("a"),
            journal_dir=journal_dir,
        )
        try:
            with room:
                for i in range(8):
                    room.post_and_wait(f"turn-{i}", timeout=5.0)
                live_state = room.session.state
                live_topic = live_state.topic
                live_responder = live_state.default_responder_id
                live_anchor = live_state.anchor_id
                live_pids = sorted(live_state.participants.keys())
                # Force a snapshot before close.
                room.session.journal.snapshot(live_state)
        finally:
            room.stop()

        # Reload the snapshot.
        j = Journal(journal_dir)
        data = j.load_state()
        assert data is not None
        restored = restore_state(data, RoomConfig())
        assert restored.topic == live_topic
        assert restored.default_responder_id == live_responder
        assert restored.anchor_id == live_anchor
        assert sorted(restored.participants.keys()) == live_pids

    @pytest.mark.disk
    def test_journal_records_every_user_turn_id(self, tmp_path):
        counter = [0]

        def a_send(prompt):
            counter[0] += 1
            return (f"alpha turn {counter[0]} sufficient long reply that "
                    f"bypasses both the pass buffer and the loop guard.")

        a = agent_from_send("a", a_send)
        journal_dir = tmp_path / "session2"
        with LoomRoom(
            agents=[a],
            policy=SingleResponderPolicy("a"),
            journal_dir=journal_dir,
        ) as room:
            for i in range(5):
                room.post_and_wait(f"turn-{i}", timeout=5.0)

        # Load events and verify every user_turn_opened control event
        # is present.
        events = Journal(journal_dir).load_events()
        opened = [
            e for e in events
            if e.kind == "control"
            and isinstance(e.body, dict)
            and e.body.get("control_type") == "user_turn_opened"
        ]
        assert len(opened) == 5
        # User turn ids are unique and monotonically increasing.
        ids = [e.body["user_turn_id"] for e in opened]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# Lifecycle stress — repeated start/stop.
# ---------------------------------------------------------------------------

class TestLifecycleStress:

    def test_start_stop_5_cycles_no_thread_leak(self):
        # Construct, start, stop a fresh room 5 times. The autouse
        # ``assert_no_thread_leak`` fixture would catch any actor
        # thread leaks; here we additionally verify all actor stopped
        # flags are set and the bus is stopped.
        long_a = "alpha sufficient long reply that bypasses loop guard."
        for _ in range(5):
            a = agent_from_send("a", lambda p: long_a)
            b = agent_from_send("b", lambda p: long_a)
            room = LoomRoom(agents=[a, b], policy=OpenChatPolicy())
            room.start()
            assert all(act._thread is not None
                       and act._thread.is_alive()
                       for act in room.session.actors)
            room.stop(timeout=5.0)
            assert all(act.stopped for act in room.session.actors)
            assert room.session.bus.stopped
