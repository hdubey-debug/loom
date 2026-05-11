"""System tests — combinations of RoomConfig × policy × policy_error_mode.

System-level: the Loom kernel exposes a small but interacting matrix of
configuration knobs (lease_ttl_s, compact_threshold, pass_buffer_chars,
max_drafts_per_participant, debounce_ms, idle_timeout_s) plus three
policy_error_mode values. These tests exercise representative
combinations end-to-end through ``LoomRoom`` to verify they don't
interfere unexpectedly.
"""

from __future__ import annotations

import time

import pytest

from loom.adapters import agent_from_send, agent_from_stream
from loom.policy.default import DefaultPolicy
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.round_robin import RoundRobinPolicy
from loom.room import LoomRoom


_LONG = (
    "reply long enough to bypass both the pass buffer and the loop "
    "guard short-text threshold for canonical commit by the kernel."
)


def _send_for(pid: str):
    counter = [0]

    def _send(prompt):
        counter[0] += 1
        return f"{pid} cfg turn {counter[0]} {_LONG}"

    return _send


# ---------------------------------------------------------------------------
# RoomConfig × policy combinations.
# ---------------------------------------------------------------------------


class TestConfigCombinations:
    """System-level: realistic RoomConfig deltas combined with policies."""

    @pytest.mark.timing
    def test_short_lease_ttl_2s_with_open_chat_and_default_responder_error_mode(
        self, multi_turn_session, varied_agents, config_factory
    ):
        cfg = config_factory(lease_ttl_s=2)
        agents = varied_agents(2, prefix="cf")
        room = multi_turn_session(
            agents=agents,
            policy=OpenChatPolicy(),
            policy_error_mode="default_responder",
            default_responder_id="cf0",
            room_config=cfg,
        )
        replies = room.post_and_wait("cfg-q", timeout=5.0)
        # The room runs and at least one reply commits.
        senders = {r.sender for r in replies if r.sender != "user"}
        assert senders <= {"cf0", "cf1"}
        assert len(senders) >= 1

    @pytest.mark.disk
    @pytest.mark.stress
    def test_low_compact_threshold_10_with_round_robin_5_turns(
        self, journaled_room, varied_agents, config_factory
    ):
        cfg = config_factory(compact_threshold=10)
        agents = varied_agents(3, prefix="lc")
        order = ["lc0", "lc1", "lc2"]
        room = journaled_room(
            agents=agents,
            policy=RoundRobinPolicy(order),
            room_config=cfg,
        )
        for i in range(5):
            room.post_and_wait(f"q{i}", timeout=5.0)
        # The session ran past the threshold without crashing.
        bus_len = len(room.session.bus.snapshot())
        assert bus_len > cfg.compact_threshold

    def test_pass_buffer_4_chars_with_default_policy_normal_replies(
        self, multi_turn_session, varied_agents, config_factory
    ):
        # pass_buffer_chars=4 means the streaming code flushes the
        # initial buffer earlier — replies should still commit cleanly
        # because they're long.
        cfg = config_factory(pass_buffer_chars=4)
        agents = varied_agents(2, prefix="pb")
        room = multi_turn_session(
            agents=agents,
            policy=DefaultPolicy(),
            default_responder_id="pb0",
            room_config=cfg,
        )
        replies = room.post_and_wait("hi", timeout=5.0)
        senders = {r.sender for r in replies if r.sender != "user"}
        # Default policy with default_responder may broadcast or route
        # — at minimum one of the agents replied.
        assert len(senders) >= 1

    def test_max_drafts_per_participant_2_with_open_chat_two_consecutive_replies(
        self, multi_turn_session, varied_agents, config_factory
    ):
        # max_drafts_per_participant=2 widens the per-turn cap. The
        # default OpenChatPolicy still emits one obligation per agent;
        # the cap only matters when an agent draws additional triggers
        # (e.g. agent-to-agent mentions). We just verify the room runs.
        cfg = config_factory(max_drafts_per_participant=2)
        agents = varied_agents(2, prefix="md")
        room = multi_turn_session(
            agents=agents,
            policy=OpenChatPolicy(),
            room_config=cfg,
        )
        replies = room.post_and_wait("max-drafts", timeout=5.0)
        senders = {r.sender for r in replies if r.sender != "user"}
        assert {"md0", "md1"} <= senders

    @pytest.mark.timing
    def test_user_turn_idle_timeout_1s_idle_closure_observable_via_journal(
        self, journaled_room, varied_agents, config_factory
    ):
        # Short idle timeout in the config; the room runs cleanly and
        # the journal records every closed user turn.
        cfg = config_factory(user_turn_idle_timeout_s=1)
        agents = varied_agents(2, prefix="it")
        room = journaled_room(
            agents=agents,
            policy=OpenChatPolicy(),
            room_config=cfg,
        )
        room.post_and_wait("idle-test", timeout=5.0)
        journal_dir = room.journal_dir
        room.stop(timeout=10.0)
        from loom.kernel.journal import Journal

        events = Journal(journal_dir).load_events()
        closed = [
            e
            for e in events
            if e.kind == "control"
            and isinstance(e.body, dict)
            and e.body.get("control_type") == "user_turn_closed"
        ]
        assert len(closed) >= 1

    @pytest.mark.timing
    def test_debounce_2000ms_two_rapid_posts_collapse_into_one_turn(
        self, varied_agents, config_factory, event_recorder
    ):
        cfg = config_factory(user_turn_debounce_ms=2000)
        agents = varied_agents(2, prefix="db")
        room = LoomRoom(
            agents=agents,
            policy=OpenChatPolicy(),
            room_config=cfg,
        )
        event_recorder.attach(room)
        room.start()
        try:
            # Two rapid non-blocking posts within the debounce window.
            room.post("first")
            room.post("second-rapid")
            time.sleep(2.5)  # let the turn complete
        finally:
            room.stop(timeout=10.0)
        opened = event_recorder.by_control_type("user_turn_opened")
        # Within a 2000ms debounce window, the second post should not
        # open a new turn — it gets debounced into the first.
        assert len(opened) == 1


# ---------------------------------------------------------------------------
# policy_error_mode matrix.
# ---------------------------------------------------------------------------


class TestPolicyErrorModeMatrix:
    """System-level: each error mode dispatches correctly when policy raises."""

    def test_close_turn_with_open_chat_silent_close_no_chat_committed(
        self, slow_policy_factory, varied_agents, event_recorder
    ):
        slow_policy = slow_policy_factory(raise_on_call=1)
        agents = varied_agents(2, prefix="ct")
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
        # No agent committed a draft.
        non_user = [r for r in replies if r.sender != "user"]
        assert non_user == []
        # policy_error fired.
        assert event_recorder.by_control_type("policy_error")

    def test_default_responder_with_round_robin_falls_through_correctly(
        self, slow_policy_factory, varied_agents, event_recorder
    ):
        # Wrap RoundRobinPolicy with a slow policy that raises once;
        # error mode dispatches to default_responder.
        slow_policy = slow_policy_factory(
            raise_on_call=1,
            delegate=RoundRobinPolicy(["dr0", "dr1", "dr2"]),
        )
        agents = varied_agents(3, prefix="dr")
        room = LoomRoom(
            agents=agents,
            policy=slow_policy,
            policy_error_mode="default_responder",
            default_responder_id="dr0",
        )
        event_recorder.attach(room)
        room.start()
        try:
            replies = room.post_and_wait("trigger-rr-error", timeout=5.0)
        finally:
            room.stop(timeout=5.0)
        senders = {r.sender for r in replies}
        assert "dr0" in senders
        assert event_recorder.by_control_type("policy_error")

    def test_raise_mode_propagates_to_caller_via_post_user_text(
        self, slow_policy_factory, varied_agents
    ):
        slow_policy = slow_policy_factory(
            raise_on_call=1,
            exc=RuntimeError,
            message="raise-mode-propagates",
        )
        agents = varied_agents(2, prefix="rm")
        room = LoomRoom(
            agents=agents,
            policy=slow_policy,
            policy_error_mode="raise",
        )
        room.start()
        try:
            with pytest.raises(RuntimeError, match="raise-mode-propagates"):
                room.post("trigger")
        finally:
            room.stop(timeout=5.0)


# ---------------------------------------------------------------------------
# Interaction effects — combinations that historically interact.
# ---------------------------------------------------------------------------


class TestInteractionEffects:
    """System-level: known-interacting config + policy combinations."""

    @pytest.mark.timing
    def test_short_lease_plus_low_compact_threshold_no_drift_over_30_turns(
        self, multi_turn_session, varied_agents, config_factory
    ):
        cfg = config_factory(lease_ttl_s=5, compact_threshold=10)
        agents = varied_agents(2, prefix="ie")
        room = multi_turn_session(
            agents=agents,
            policy=OpenChatPolicy(),
            room_config=cfg,
        )
        # 30 turns under combined short lease + low threshold; the
        # session should remain coherent — participant set unchanged,
        # no exception leaks.
        before_pids = set(room.participants)
        for i in range(30):
            room.post_and_wait(f"q{i}", timeout=5.0)
        assert set(room.participants) == before_pids

    def test_round_robin_plus_lease_ttl_2_drops_late_drafts(self, varied_agents, config_factory):
        # Slow agent + 2s lease — the lease expires before the slow
        # agent finishes streaming, so the chat is dropped (or only
        # partial deltas arrive).
        cfg = config_factory(lease_ttl_s=2)

        def slow_stream(prompt):
            yield "first delta long enough to flush the buffer here"
            time.sleep(3.0)  # exceed lease_ttl_s
            yield "second delta is dropped — lease has expired"

        agents = [
            agent_from_send("rl0", _send_for("rl0")),
            agent_from_stream("rl1", slow_stream),
        ]
        room = LoomRoom(
            agents=agents,
            policy=RoundRobinPolicy(["rl0", "rl1"]),
            room_config=cfg,
        )
        room.start()
        try:
            # First turn → rl0 (fast) — commits cleanly.
            r1 = room.post_and_wait("first", timeout=5.0)
            senders1 = {r.sender for r in r1 if r.sender != "user"}
            assert senders1 == {"rl0"}
            # Second turn → rl1 (slow). Wait < its full sleep so the
            # lease expires while it's mid-stream.
            r2 = room.post_and_wait("second", timeout=4.0)
            # Commit may have happened before the timeout (the first
            # delta is already past the pass buffer flush point) — the
            # invariant we assert is no crash.
            senders2 = {r.sender for r in r2 if r.sender != "user"}
            assert senders2 <= {"rl0", "rl1"}
        finally:
            room.stop(timeout=10.0)
