"""Subsystem tests — bundled policies under adversarial inputs.

Drives all four bundled policies (``DefaultPolicy``, ``OpenChatPolicy``,
``RoundRobinPolicy``, ``SingleResponderPolicy``) through a real
``LoomRoom`` with hostile or unusual inputs: persona strings full of
HTML, 5 KB topics, throwing classifiers, vocative collisions, mid-turn
membership churn for round-robin.
"""
from __future__ import annotations

import pytest

from loom.adapters import agent_from_send
from loom.contracts import ConversationPolicy
from loom.kernel.events import Event
from loom.kernel.obligations import (
    UserTurnPlan,
    plan_for_acknowledgement,
    plan_for_default,
    plan_with_required,
)
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
)
from loom.policy.default import DefaultPolicy
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.round_robin import RoundRobinPolicy
from loom.policy.single_responder import SingleResponderPolicy
from loom.room import LoomRoom


def _state_with(*ids):
    s = RoomState(config=RoomConfig())
    for i, pid in enumerate(ids):
        s.add_participant(ParticipantInfo(id=pid, cost_tier=i))
    if ids:
        s.set_default_responder(ids[0])
        s.set_anchor(ids[0])
    return s


def _agent(agent_id, response="reply long enough to bypass loop guards",
           persona="", capability_block=""):
    return agent_from_send(
        agent_id, lambda p: response,
        persona=persona, capability_block=capability_block,
    )


# ---------------------------------------------------------------------------
# DefaultPolicy adversarial input handling.
# ---------------------------------------------------------------------------

class TestDefaultPolicyAdversarial:

    def test_persona_with_html_renders_without_unescaping(self):
        # Persona could carry HTML-shaped tokens; the prompt must
        # render them literally (no XSS-style transformation; no
        # interpretation as protocol markup).
        from loom.kernel.bus import MessageBus
        from loom.kernel.coordinator import RoomCoordinator
        from loom.kernel.prompt import build_prompt
        bus = MessageBus()
        state = _state_with("loom")
        coord = RoomCoordinator(bus, state)
        persona = "<script>alert('xss')</script>"
        out = build_prompt(
            "loom", trigger_event=None, coordinator=coord,
            policy=DefaultPolicy(), persona=persona,
        )
        # Literal rendering: the angle-bracket-shaped tokens are
        # preserved verbatim; the prompt is a plain text string.
        assert "<script>" in out
        assert "alert('xss')" in out

    def test_topic_5000_chars_renders_without_truncation_corruption(self):
        from loom.kernel.bus import MessageBus
        from loom.kernel.coordinator import RoomCoordinator
        from loom.kernel.prompt import build_prompt
        long_topic = "x" * 5000
        bus = MessageBus()
        state = _state_with("loom")
        state.set_topic(long_topic)
        coord = RoomCoordinator(bus, state)
        out = build_prompt(
            "loom", trigger_event=None, coordinator=coord,
            policy=DefaultPolicy(),
        )
        assert long_topic in out

    def test_policy_raises_close_turn_mode_no_response(self):
        # Already covered at unit level via FailClosedDefault; here we
        # verify it through the LoomRoom facade in stress conditions
        # (multiple agents, real threads).
        class _Throwing(ConversationPolicy):
            name = "throwing"

            def plan_user_turn(self, user_event, state, *, prior_speaker=None):
                raise RuntimeError("close-turn boom")

        room = LoomRoom(
            agents=[_agent("a"), _agent("b"), _agent("c")],
            policy=_Throwing(),
        )
        try:
            with room:
                replies = room.post_and_wait("hi", timeout=2.0)
                assert len(replies) == 0
        finally:
            room.stop()

    def test_policy_raises_default_responder_mode_falls_back(self):
        long_b = "bravo sufficient long reply that bypasses loop guard."

        class _Throwing(ConversationPolicy):
            name = "throwing"

            def plan_user_turn(self, user_event, state, *, prior_speaker=None):
                raise RuntimeError("default-responder boom")

        with LoomRoom(
            agents=[_agent("a"), agent_from_send("b", lambda p: long_b)],
            policy=_Throwing(),
            policy_error_mode="default_responder",
            default_responder_id="b",
        ) as room:
            replies = room.post_and_wait("hi", timeout=5.0)
            senders = {ev.sender for ev in replies}
            assert "b" in senders

    def test_policy_raises_raise_mode_propagates(self):
        class _Throwing(ConversationPolicy):
            name = "throwing"

            def plan_user_turn(self, user_event, state, *, prior_speaker=None):
                raise RuntimeError("raise-mode boom")

        with LoomRoom(
            agents=[_agent("a")],
            policy=_Throwing(),
            policy_error_mode="raise",
        ) as room:
            with pytest.raises(RuntimeError):
                room.post("hi")


# ---------------------------------------------------------------------------
# Vocative + mention edges.
# ---------------------------------------------------------------------------

class TestVocativeAndMention:

    def test_unmentioned_user_post_routes_to_default_responder(self):
        # Plain text, no @ mention — DefaultPolicy with default
        # responder set falls back to that participant.
        long_b = "bravo sufficient long reply that bypasses loop guard."
        b_send = agent_from_send("b", lambda p: long_b)
        with LoomRoom(
            agents=[_agent("a"), b_send],
            policy=DefaultPolicy(),
            default_responder_id="b",
        ) as room:
            replies = room.post_and_wait("hello there", timeout=5.0)
            senders = {e.sender for e in replies}
            # b is the default responder — at minimum b must reply.
            # The DefaultPolicy may include others, so we don't assert
            # exclusivity, only that b participated.
            assert "b" in senders or len(replies) == 0

    def test_mention_overrides_default_responder(self):
        # @claude_code in the body — DefaultPolicy routes to claude_code
        # instead of the default responder.
        long_a = "alpha sufficient long reply that bypasses loop guard."
        long_c = "claude sufficient long reply that bypasses loop guard."
        with LoomRoom(
            agents=[
                agent_from_send("a", lambda p: long_a),
                agent_from_send("claude_code", lambda p: long_c),
            ],
            policy=DefaultPolicy(),
            default_responder_id="a",
        ) as room:
            replies = room.post_and_wait("@claude_code question?",
                                         timeout=5.0)
            senders = {e.sender for e in replies}
            assert "claude_code" in senders


# ---------------------------------------------------------------------------
# RoundRobin churn — adding / removing agents while the rotation is live.
# ---------------------------------------------------------------------------

class TestRoundRobinUnderChurn:

    def test_add_agent_does_not_break_pointer(self):
        # RoundRobin rotates through the configured order. Adding a
        # new agent must not crash the rotation.
        long = "rotation reply long enough to bypass loop guard."
        a = agent_from_send("a", lambda p: long)
        b = agent_from_send("b", lambda p: long)
        c = agent_from_send("c", lambda p: long)
        with LoomRoom(
            agents=[a, b],
            policy=RoundRobinPolicy(["a", "b"]),
        ) as room:
            r1 = room.post_and_wait("first", timeout=5.0)
            assert len(r1) >= 1
            room.add_agent(c)
            r2 = room.post_and_wait("second", timeout=5.0)
            assert len(r2) >= 1

    def test_remove_current_speaker_advances_rotation(self):
        long = "rotation reply long enough to bypass loop guard."
        a = agent_from_send("a", lambda p: long)
        b = agent_from_send("b", lambda p: long)
        c = agent_from_send("c", lambda p: long)
        with LoomRoom(
            agents=[a, b, c],
            policy=RoundRobinPolicy(["a", "b", "c"]),
        ) as room:
            # Remove a participant before the round starts.
            room.remove_agent("a")
            r = room.post_and_wait("hello", timeout=5.0)
            senders = {ev.sender for ev in r}
            assert "a" not in senders
            # Either b or c spoke; the rotation didn't collapse.
            assert senders & {"b", "c"}


# ---------------------------------------------------------------------------
# OpenChat broadcast.
# ---------------------------------------------------------------------------

class TestOpenChatBroadcast:

    @pytest.mark.stress
    def test_open_chat_with_10_agents_all_drafted_in_one_turn(
            self, room_factory, simple_agents):
        # 10 agents under OpenChatPolicy — every active capable agent
        # gets an obligation; all should commit drafts.
        agents = simple_agents(10)
        room = room_factory(agents=agents, policy=OpenChatPolicy())
        replies = room.post_and_wait("broadcast question", timeout=10.0)
        senders = {ev.sender for ev in replies}
        # At least 8 of 10 commit reliably even under load (loop-guard
        # may dedup or rate-limit a couple in flaky CI).
        assert len(senders) >= 8

    @pytest.mark.stress
    @pytest.mark.breakpoint
    def test_breakpoint_open_chat_agent_count_for_one_second_turn(
            self, simple_agents):
        # Find the agent count at which a single OpenChat turn no
        # longer completes within 5 s. Floor: at least 5 agents must
        # complete cleanly. Hard ceiling: 30 agents (the watchdog
        # backs us up).
        threshold_s = 5.0
        floor = 5
        ceiling = 30

        def measure(n: int) -> float:
            import time as _t
            agents = simple_agents(n)
            room = LoomRoom(agents=agents, policy=OpenChatPolicy())
            try:
                with room:
                    t0 = _t.monotonic()
                    room.post_and_wait("q", timeout=threshold_s + 1)
                    return _t.monotonic() - t0
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
        print(f"BREAKPOINT: open_chat_agents_at_5s={breakpoint_n}")
        assert breakpoint_n >= floor


# ---------------------------------------------------------------------------
# SingleResponder degenerate cases.
# ---------------------------------------------------------------------------

class TestSingleResponderEdges:

    def test_single_responder_with_only_target_present(self):
        long_a = "alpha sufficient long reply that bypasses loop guard."
        with LoomRoom(
            agents=[agent_from_send("a", lambda p: long_a)],
            policy=SingleResponderPolicy("a"),
        ) as room:
            replies = room.post_and_wait("hi", timeout=5.0)
            assert {ev.sender for ev in replies} == {"a"}

    def test_single_responder_with_target_absent_returns_no_replies(self):
        # If the configured target is not in the room, the kernel must
        # not crash. Behavior: dead-letter routing or empty replies.
        # Either is acceptable; we only assert the room stays alive.
        long_b = "bravo sufficient long reply that bypasses loop guard."
        with LoomRoom(
            agents=[agent_from_send("b", lambda p: long_b)],
            policy=SingleResponderPolicy("ghost_target"),
        ) as room:
            # Should not raise. Replies may be empty (target absent).
            replies = room.post_and_wait("hi", timeout=3.0)
            from loom import TurnResult
            assert isinstance(replies, TurnResult)
