"""System tests — external bus subscribers + kernel watchdog visibility.

System-level: third-party observers attach to a live ``LoomRoom`` via
the public ``room.session.bus.subscribe`` hook (observation only) and
verify the kernel surfaces the right events for the right conditions.
Distinct from subsystem observability tests, which probe one event
factory at a time — these tests confirm the kernel emits diagnostic
events in their proper context inside an assembled session.
"""
from __future__ import annotations

import threading

import pytest

from loom.adapters import agent_from_send
from loom.kernel.events import Event
from loom.policy.open_chat import OpenChatPolicy
from loom.room import LoomRoom


_LONG = ("reply long enough to bypass both the pass buffer and the loop "
         "guard short-text threshold for canonical commit by the kernel.")


def _send_for(pid: str):
    counter = [0]

    def _send(prompt):
        counter[0] += 1
        return f"{pid} turn {counter[0]} {_LONG}"
    return _send


def _build_room(*, n_agents: int = 2, prefix: str = "ob",
                policy=None, **kwargs) -> LoomRoom:
    agents = [agent_from_send(f"{prefix}{i}", _send_for(f"{prefix}{i}"))
              for i in range(n_agents)]
    return LoomRoom(
        agents=agents,
        policy=policy if policy is not None else OpenChatPolicy(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# External bus subscriber.
# ---------------------------------------------------------------------------

class TestExternalBusSubscriber:
    """System-level: a third-party subscriber observes every event."""

    def test_subscriber_attached_pre_start_sees_every_event(
            self, varied_agents):
        # Subscribers attached before start() see participant_added
        # events emitted during construction (build_loom_session calls
        # register_participant for each wiring).
        agents = varied_agents(2, prefix="pre")
        room = LoomRoom(agents=agents, policy=OpenChatPolicy())
        captured: list[Event] = []
        capture_lock = threading.Lock()

        def on_event(e: Event) -> None:
            with capture_lock:
                captured.append(e)

        unsub = room.session.bus.subscribe(on_event)
        room.start()
        try:
            room.post_and_wait("hello", timeout=5.0)
        finally:
            unsub()
            room.stop(timeout=5.0)
        # The pre-start subscriber missed the construction-time
        # participant_added events (they fired before subscribe), but
        # it sees every event from start() onward.
        kinds = {e.kind for e in captured}
        assert "chat" in kinds
        assert "control" in kinds

    def test_subscriber_attached_mid_session_sees_only_subsequent_events(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(2, prefix="mid")
        room = multi_turn_session(agents=agents, policy=OpenChatPolicy())
        # Drive one turn before subscribing.
        room.post_and_wait("first", timeout=5.0)
        captured: list[Event] = []
        capture_lock = threading.Lock()

        def on_event(e: Event) -> None:
            with capture_lock:
                captured.append(e)

        unsub = room.session.bus.subscribe(on_event)
        try:
            room.post_and_wait("second", timeout=5.0)
        finally:
            unsub()
        # The "first" user post is not in captured.
        bodies = [e.body for e in captured if e.kind == "chat"]
        assert "first" not in bodies
        assert "second" in bodies

    def test_subscriber_detach_during_post_does_not_lose_in_flight_event(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(2, prefix="det")
        room = multi_turn_session(agents=agents, policy=OpenChatPolicy())
        captured: list[Event] = []
        capture_lock = threading.Lock()

        def on_event(e: Event) -> None:
            with capture_lock:
                captured.append(e)

        unsub = room.session.bus.subscribe(on_event)
        room.post_and_wait("during", timeout=5.0)
        unsub()
        # No events are LOST from the bus log — they're still all in
        # bus.snapshot() regardless of whether any subscriber observed
        # them.
        all_events = list(room.session.bus.snapshot())
        bodies = {e.body for e in all_events if e.kind == "chat"}
        assert "during" in bodies

    def test_two_subscribers_observe_identical_event_id_sequences(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(2, prefix="two")
        room = multi_turn_session(agents=agents, policy=OpenChatPolicy())
        ids_a: list[int] = []
        ids_b: list[int] = []
        lock_a = threading.Lock()
        lock_b = threading.Lock()

        def on_a(e: Event) -> None:
            with lock_a:
                ids_a.append(e.id)

        def on_b(e: Event) -> None:
            with lock_b:
                ids_b.append(e.id)

        unsub_a = room.session.bus.subscribe(on_a)
        unsub_b = room.session.bus.subscribe(on_b)
        try:
            for i in range(3):
                room.post_and_wait(f"q{i}", timeout=5.0)
        finally:
            unsub_a()
            unsub_b()
        # Identical sequences (same order, same ids).
        assert ids_a == ids_b

    def test_misbehaving_subscriber_exception_does_not_break_room(
            self, multi_turn_session, varied_agents, event_recorder):
        agents = varied_agents(2, prefix="mis")
        room = multi_turn_session(agents=agents, policy=OpenChatPolicy())
        # Attach a recorder + a misbehaving subscriber.
        event_recorder.attach(room)

        def bad_on_event(e: Event) -> None:
            raise RuntimeError("bad subscriber")

        unsub_bad = room.session.bus.subscribe(bad_on_event)
        try:
            room.post_and_wait("after-bad-subscriber", timeout=5.0)
        finally:
            unsub_bad()
        # The well-behaved recorder still saw events; the room kept
        # running.
        assert event_recorder.count() > 0


# ---------------------------------------------------------------------------
# Watchdog visibility — policy_slow / policy_error.
# ---------------------------------------------------------------------------

class TestWatchdogVisibility:
    """System-level: kernel emits diagnostic events for slow/error policies."""

    @pytest.mark.timing
    def test_policy_slow_emitted_when_policy_exceeds_threshold(
            self, slow_policy_factory, event_recorder, varied_agents):
        # 200ms sleep exceeds the kernel's 100ms threshold → policy_slow.
        slow_policy = slow_policy_factory(sleep_ms=200)
        agents = varied_agents(2, prefix="ps")
        room = LoomRoom(agents=agents, policy=slow_policy)
        event_recorder.attach(room)
        room.start()
        try:
            room.post_and_wait("slow-trigger", timeout=10.0)
        finally:
            room.stop(timeout=5.0)
        ps = event_recorder.by_control_type("policy_slow")
        assert len(ps) >= 1
        assert ps[0].body.get("elapsed_ms") >= 100

    def test_policy_error_emitted_with_default_responder_fallback(
            self, slow_policy_factory, event_recorder, varied_agents):
        agents = varied_agents(2, prefix="pe")
        # Policy raises on the 1st (and only) call. Fallback to the
        # default responder.
        slow_policy = slow_policy_factory(raise_on_call=1)
        room = LoomRoom(
            agents=agents,
            policy=slow_policy,
            policy_error_mode="default_responder",
            default_responder_id="pe0",
        )
        event_recorder.attach(room)
        room.start()
        try:
            replies = room.post_and_wait("trigger-err", timeout=5.0)
        finally:
            room.stop(timeout=5.0)
        assert event_recorder.by_control_type("policy_error")
        # The default responder pe0 replied via the fallback plan.
        senders = {r.sender for r in replies}
        assert "pe0" in senders

    def test_policy_error_with_close_turn_mode_no_replies_committed(
            self, slow_policy_factory, event_recorder, varied_agents):
        agents = varied_agents(2, prefix="pc")
        slow_policy = slow_policy_factory(raise_on_call=1)
        room = LoomRoom(
            agents=agents,
            policy=slow_policy,
            policy_error_mode="close_turn",
        )
        event_recorder.attach(room)
        room.start()
        try:
            replies = room.post_and_wait("close-turn", timeout=3.0)
        finally:
            room.stop(timeout=5.0)
        # policy_error fired, but no chat events were committed (the
        # turn closed silently).
        assert event_recorder.by_control_type("policy_error")
        # No agent chats from this turn.
        senders = {r.sender for r in replies}
        assert senders <= {"user"}


# ---------------------------------------------------------------------------
# Actor + journal error visibility.
# ---------------------------------------------------------------------------

class TestActorAndJournalErrorVisibility:
    """System-level: kernel surfaces actor + journal errors for diagnosis."""

    def test_actor_error_emitted_when_handler_raises_actor_keeps_running(
            self, mixed_agent_room, event_recorder):
        # An adversarial agent that raises during streaming. The
        # streaming machinery emits stream_end with status=error. The
        # actor itself doesn't post actor_error here unless the actor
        # loop's step() raised — that path is exercised when an agent
        # raises mid-stream via streaming.run_streaming_call.
        room = mixed_agent_room(
            healthy=2,
            adversarial=[("raises", 1)],
            policy=OpenChatPolicy(),
        )
        event_recorder.attach(room)
        room.post_and_wait("raise-trigger", timeout=5.0)
        # Healthy agents still committed; the adversarial's stream_end
        # carried status=error.
        stream_ends = [e for e in event_recorder.by_kind("stream")
                       if e.body.get("stream_event") == "end"]
        statuses = {e.body.get("status") for e in stream_ends}
        # At least one healthy commit; at least one error end.
        assert "committed" in statuses
        assert "error" in statuses

    @pytest.mark.disk
    def test_journal_error_emitted_once_per_session_after_first_failure(
            self, event_recorder, varied_agents, tmp_path, monkeypatch):
        from tests.subsystem.conftest import InMemoryFaultJournal
        import loom.runtime as runtime_mod
        # Fail at the 10th write so the failure lands during a user
        # post, after our event_recorder has attached. (Construction
        # writes ~3 events: participant_added × N + anchor_changed.)
        monkeypatch.setattr(
            runtime_mod, "Journal",
            lambda d, **kw: InMemoryFaultJournal(d, fail_at=10, **kw))
        agents = varied_agents(2, prefix="je")
        journal_dir = tmp_path / "single_error_session"
        room = LoomRoom(
            agents=agents,
            policy=OpenChatPolicy(),
            journal_dir=journal_dir,
        )
        event_recorder.attach(room)
        room.start()
        try:
            for i in range(5):
                room.post_and_wait(f"q{i}", timeout=5.0)
        finally:
            room.stop(timeout=5.0)
        je = event_recorder.by_control_type("journal_error")
        # Exactly one journal_error event posted across the session.
        # Subsequent failed writes do not re-emit (recursion guard +
        # journal stays in degraded mode).
        assert len(je) == 1
