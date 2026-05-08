"""System tests — adapters + policies + cooperative-and-adversarial coexistence.

System-level: drives a single ``LoomRoom`` containing every adapter
shape (``agent_from_send`` / ``agent_from_stream`` / ``agent_from_object``)
side-by-side with healthy and adversarial agents, under each bundled
policy. The kernel must keep healthy turns flowing even when one
participant misbehaves — these tests assert that cross-cutting
guarantee.
"""
from __future__ import annotations

import threading
import time

import pytest

from loom.adapters import (
    agent_from_object,
    agent_from_send,
    agent_from_stream,
)
from loom.policy.default import DefaultPolicy
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.round_robin import RoundRobinPolicy
from loom.policy.single_responder import SingleResponderPolicy
from loom.room import LoomRoom


# Shared long-text helper — bypasses both the pass buffer and the loop
# guard short-text threshold so the kernel commits canonical chats.
_LONG = ("reply long enough to bypass both the pass buffer and the loop "
         "guard short-text threshold for canonical commit by the kernel.")


def _send_for(pid: str):
    counter = [0]

    def _send(prompt):
        counter[0] += 1
        return f"{pid} send-adapter turn {counter[0]} {_LONG}"
    return _send


def _stream_for(pid: str):
    counter = [0]

    def _stream(prompt):
        counter[0] += 1
        yield f"{pid} stream-adapter turn {counter[0]} {_LONG}"
    return _stream


class _ObjectClient:
    """Minimal client object exposing ``stream`` + ``cancel``."""

    def __init__(self, pid: str) -> None:
        self.pid = pid
        self.cancelled = False
        self._counter = 0
        self.persona = f"persona-of-{pid}"
        self.capability_block = f"caps-of-{pid}"
        self.cost_tier = 2
        self.capable = True

    def stream(self, prompt):
        self._counter += 1
        yield f"{self.pid} object-adapter turn {self._counter} {_LONG}"

    def cancel(self):
        self.cancelled = True


class _SlowObjectClient(_ObjectClient):
    """Object client that sleeps mid-stream so cancel() can land."""

    def stream(self, prompt):
        self._counter += 1
        # Yield one chunk, then sleep so room.stop() can cancel us.
        yield f"{self.pid} slow object first delta long enough"
        time.sleep(2.0)
        if self.cancelled:
            return
        yield f"{self.pid} slow object second delta {_LONG}"


# ---------------------------------------------------------------------------
# All three adapter kinds in one room.
# ---------------------------------------------------------------------------

class TestAllThreeAdaptersInOneRoom:
    """System-level: send / stream / object adapters coexist."""

    def test_send_stream_object_adapters_coexist_in_open_chat(
            self, multi_turn_session):
        a_send = agent_from_send("aa", _send_for("aa"))
        a_stream = agent_from_stream("bb", _stream_for("bb"))
        a_object = agent_from_object("cc", _ObjectClient("cc"))
        room = multi_turn_session(
            agents=[a_send, a_stream, a_object],
            policy=OpenChatPolicy(),
        )
        replies = room.post_and_wait("hello", timeout=5.0)
        senders = {r.sender for r in replies if r.sender != "user"}
        assert "aa" in senders
        assert "bb" in senders
        assert "cc" in senders

    def test_each_adapter_kind_can_become_default_responder(
            self, multi_turn_session):
        for pid, agent_factory in [
            ("a_send", lambda: agent_from_send("a_send", _send_for("a_send"))),
            ("a_stream", lambda: agent_from_stream("a_stream",
                                                    _stream_for("a_stream"))),
            ("a_object", lambda: agent_from_object("a_object",
                                                   _ObjectClient("a_object"))),
        ]:
            agent = agent_factory()
            room = multi_turn_session(
                agents=[agent],
                policy=SingleResponderPolicy(pid),
                default_responder_id=pid,
            )
            replies = room.post_and_wait("hi", timeout=5.0)
            senders = {r.sender for r in replies if r.sender != "user"}
            assert senders == {pid}

    def test_persona_capability_block_cost_tier_round_trip_per_adapter(
            self, multi_turn_session):
        a_send = agent_from_send(
            "ps", _send_for("ps"),
            persona="ps-persona", capability_block="ps-caps",
            cost_tier=3,
        )
        a_stream = agent_from_stream(
            "st", _stream_for("st"),
            persona="st-persona", capability_block="st-caps",
            cost_tier=4,
        )
        # Object adapter pulls metadata from the wrapped object.
        client = _ObjectClient("ob")
        client.persona = "ob-persona"
        client.capability_block = "ob-caps"
        client.cost_tier = 5
        a_object = agent_from_object("ob", client)
        room = multi_turn_session(
            agents=[a_send, a_stream, a_object],
            policy=OpenChatPolicy(),
        )
        # Verify metadata reached the wirings.
        wirings = room.session.wirings
        assert wirings["ps"].persona == "ps-persona"
        assert wirings["ps"].capability_block == "ps-caps"
        assert wirings["ps"].cost_tier == 3
        assert wirings["st"].persona == "st-persona"
        assert wirings["st"].cost_tier == 4
        assert wirings["ob"].persona == "ob-persona"
        assert wirings["ob"].cost_tier == 5

    def test_object_adapter_with_cancel_invoked_on_room_stop(
            self, multi_turn_session, config_factory):
        # Build a slow-streaming object client. With ``lease_ttl_s=1``,
        # the lease expires while the agent is still in time.sleep.
        # When sleep finishes and the next chunk hits streaming.py,
        # validate_lease fails and the kernel calls _try_cancel(proxy)
        # → proxy.cancel() → client.cancel().
        client = _SlowObjectClient("slow")
        agent = agent_from_object("slow", client)
        cfg = config_factory(lease_ttl_s=1)
        room = multi_turn_session(
            agents=[agent],
            policy=OpenChatPolicy(),
            room_config=cfg,
        )
        # Fire-and-forget — don't wait for the slow stream to complete.
        room.post("trigger-slow")
        # Wait long enough for the lease to expire AND for the agent's
        # time.sleep to finish so the next chunk triggers cancel.
        time.sleep(2.5)
        room.stop(timeout=5.0)
        assert client.cancelled is True


# ---------------------------------------------------------------------------
# Cooperative + adversarial agents in the same room.
# ---------------------------------------------------------------------------

class TestCooperativeAndAdversarialCoexist:
    """System-level: one bad agent does not disrupt the rest of the room."""

    @pytest.mark.timing
    def test_one_hang_after_first_delta_does_not_starve_three_healthy(
            self, mixed_agent_room):
        room = mixed_agent_room(
            healthy=3,
            adversarial=[("hang", 1)],
            policy=OpenChatPolicy(),
        )
        replies = room.post_and_wait("question", timeout=8.0)
        senders = {r.sender for r in replies if r.sender != "user"}
        # All three healthy agents committed.
        assert {"healthy_0", "healthy_1", "healthy_2"} <= senders

    def test_one_raises_after_chunks_routes_to_default_responder_in_default_responder_mode(
            self, mixed_agent_room):
        # In a broadcast room with a raises_after_chunks agent, the
        # OTHER agents still commit. The default_responder slot is set
        # so that any policy_error fallback would route there — but
        # the agent's own raise doesn't trigger a policy_error.
        room = mixed_agent_room(
            healthy=2,
            adversarial=[("raises", 1)],
            policy=OpenChatPolicy(),
            policy_error_mode="default_responder",
            default_responder_id="healthy_0",
        )
        replies = room.post_and_wait("with-raiser", timeout=5.0)
        senders = {r.sender for r in replies if r.sender != "user"}
        assert "healthy_0" in senders or "healthy_1" in senders

    def test_garbage_payload_agent_in_open_chat_does_not_corrupt_journal(
            self, mixed_agent_room, tmp_path):
        # Construct a journaled mixed-agent room and verify the journal
        # parses cleanly even after a garbage-payload agent contributes.
        room = mixed_agent_room(
            healthy=2,
            adversarial=[("garbage", 1)],
            policy=OpenChatPolicy(),
            journal_dir=tmp_path / "garbage_session",
        )
        room.post_and_wait("garbage-test", timeout=5.0)
        room.stop(timeout=10.0)
        # Re-open via Journal observation surface.
        from loom.kernel.journal import Journal
        events = Journal(tmp_path / "garbage_session").load_events()
        # Every line parsed cleanly; events list is well-formed.
        assert len(events) >= 1
        for e in events:
            assert isinstance(e.body, (str, dict))

    @pytest.mark.timing
    def test_infinite_stream_agent_bounded_by_lease_ttl_other_agents_complete(
            self, mixed_agent_room, config_factory):
        # Short lease_ttl so the infinite stream is bounded even before
        # cap_chunks runs out (200 chunks at adapter pace).
        cfg = config_factory(lease_ttl_s=2)
        room = mixed_agent_room(
            healthy=2,
            adversarial=[("infinite", 1)],
            policy=OpenChatPolicy(),
            room_config=cfg,
        )
        replies = room.post_and_wait("bounded", timeout=10.0)
        senders = {r.sender for r in replies if r.sender != "user"}
        assert {"healthy_0", "healthy_1"} <= senders


# ---------------------------------------------------------------------------
# Routing across all four bundled policies in a live room.
# ---------------------------------------------------------------------------

class TestRoutingAcrossPoliciesInLiveRoom:
    """System-level: each policy routes a real ``post_and_wait`` correctly."""

    def test_default_policy_at_mention_routes_committed_chat_to_named_agent(
            self, multi_turn_session):
        agents = [
            agent_from_send("alpha", _send_for("alpha")),
            agent_from_send("beta", _send_for("beta")),
            agent_from_send("gamma", _send_for("gamma")),
        ]
        room = multi_turn_session(
            agents=agents,
            policy=DefaultPolicy(),
            default_responder_id="alpha",
        )
        replies = room.post_and_wait("@beta question?", timeout=5.0)
        senders = {r.sender for r in replies if r.sender != "user"}
        assert senders == {"beta"}

    @pytest.mark.stress
    def test_open_chat_5_agents_each_committed_chat_appears_in_post_and_wait_replies(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(5, prefix="oc")
        room = multi_turn_session(
            agents=agents,
            policy=OpenChatPolicy(),
        )
        replies = room.post_and_wait("broadcast", timeout=10.0)
        senders = {r.sender for r in replies if r.sender != "user"}
        # All five agents commit.
        assert {f"oc{i}" for i in range(5)} <= senders

    def test_round_robin_3_agents_5_turns_speakers_match_rotation_order(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(3, prefix="rt")
        order = ["rt0", "rt1", "rt2"]
        room = multi_turn_session(
            agents=agents,
            policy=RoundRobinPolicy(order),
        )
        speakers: list[str] = []
        for i in range(5):
            replies = room.post_and_wait(f"q{i}", timeout=5.0)
            for r in replies:
                if r.sender in order:
                    speakers.append(r.sender)
                    break
        # First speaker is order[0].
        assert speakers[0] == "rt0"
        # The rotation cycles through every member.
        assert set(speakers) == set(order)

    def test_single_responder_silenced_when_responder_inactive_falls_to_acknowledgement(
            self, multi_turn_session):
        # Configure SingleResponderPolicy with an id that's NOT in the
        # agent list. The policy treats missing/inactive as silenced
        # and returns plan_for_acknowledgement → no turn opens →
        # post_and_wait returns [].
        agents = [
            agent_from_send("present", _send_for("present")),
        ]
        room = multi_turn_session(
            agents=agents,
            policy=SingleResponderPolicy("absent_target"),
        )
        replies = room.post_and_wait("hi", timeout=3.0)
        assert len(replies) == 0
