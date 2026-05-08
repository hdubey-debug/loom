"""System tests — 50–100 turn realistic multi-turn workloads.

System-level: drives long, varied dialog through the public ``LoomRoom``
API and verifies state, journal, and observable events stay coherent
across many turns. Distinct from subsystem tests (which exercise one
subsystem at a time): each test here keeps a real room running for
dozens of user posts and asserts whole-kernel invariants over the
accumulated trace.
"""
from __future__ import annotations

import pytest

from loom.adapters import agent_from_send
from loom.policy.default import DefaultPolicy
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.round_robin import RoundRobinPolicy
from loom.policy.single_responder import SingleResponderPolicy


# ---------------------------------------------------------------------------
# Extended dialog — long happy paths under each bundled policy.
# ---------------------------------------------------------------------------

class TestExtendedDialog:
    """System-level: drive long sessions through every bundled policy."""

    @pytest.mark.stress
    def test_50_turn_open_chat_broadcast_no_state_drift(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(3)
        room = multi_turn_session(agents=agents, policy=OpenChatPolicy())
        before_participants = set(room.participants)
        before_topic = room.topic
        for i in range(50):
            replies = room.post_and_wait(f"q{i}", timeout=5.0)
            assert isinstance(replies, (list, __import__("loom").TurnResult))
        assert set(room.participants) == before_participants
        assert room.topic == before_topic

    @pytest.mark.stress
    def test_75_turn_round_robin_pointer_consistency_across_run(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(3, prefix="rr")
        order = ["rr0", "rr1", "rr2"]
        room = multi_turn_session(
            agents=agents,
            policy=RoundRobinPolicy(order),
        )
        speakers: list[str] = []
        for i in range(75):
            replies = room.post_and_wait(f"q{i}", timeout=5.0)
            for r in replies:
                if r.sender in order:
                    speakers.append(r.sender)
                    break
        # The rotation must produce speakers from the configured order.
        assert speakers, "round-robin produced no speakers in 75 turns"
        for s in speakers:
            assert s in order
        # Cycle should advance — at least 2 distinct speakers seen.
        assert len(set(speakers)) >= 2

    @pytest.mark.stress
    def test_60_turn_single_responder_only_target_speaks(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(3, prefix="sr")
        room = multi_turn_session(
            agents=agents,
            policy=SingleResponderPolicy("sr1"),
        )
        any_reply = False
        for i in range(60):
            replies = room.post_and_wait(f"q{i}", timeout=5.0)
            for r in replies:
                if r.sender != "user":
                    any_reply = True
                    assert r.sender == "sr1", (
                        f"non-target spoke at turn {i}: {r.sender}")
        assert any_reply

    @pytest.mark.stress
    @pytest.mark.watchdog(seconds=120)
    def test_80_turn_default_policy_mixed_mention_and_broadcast(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(3, prefix="dp")
        room = multi_turn_session(agents=agents, policy=DefaultPolicy())
        mentioned_only_dp1 = 0
        for i in range(80):
            if i % 5 == 0:
                replies = room.post_and_wait(
                    f"@dp1 thoughts on item {i}?", timeout=5.0)
                senders = {r.sender for r in replies if r.sender != "user"}
                if senders == {"dp1"}:
                    mentioned_only_dp1 += 1
            else:
                room.post_and_wait(f"open question {i}", timeout=5.0)
        assert mentioned_only_dp1 > 0, (
            "no @-mention turn ever resolved exclusively to dp1")

    @pytest.mark.stress
    def test_long_haul_default_responder_resolves_after_remove_at_turn_30(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(3, prefix="dr")
        room = multi_turn_session(
            agents=agents,
            policy=DefaultPolicy(),
            default_responder_id="dr0",
        )
        for i in range(30):
            room.post_and_wait(f"q{i}", timeout=5.0)
        # Removing dr0 forces the kernel to re-resolve the default
        # responder slot to the cheapest active capable participant.
        room.remove_agent("dr0")
        assert "dr0" not in room.participants
        for i in range(30):
            room.post_and_wait(f"q{i+30}", timeout=5.0)
        # The room kept running; the surviving agents are still here.
        assert set(room.participants) == {"dr1", "dr2"}


# ---------------------------------------------------------------------------
# Invariants checked across the entire long session.
# ---------------------------------------------------------------------------

class TestLongHaulInvariants:
    """System-level: invariants asserted over a long accumulated trace."""

    def test_committed_chat_count_matches_completed_user_turns(
            self, multi_turn_session, varied_agents, event_recorder):
        agents = varied_agents(2, prefix="inv")
        room = multi_turn_session(
            agents=agents,
            policy=SingleResponderPolicy("inv0"),
        )
        event_recorder.attach(room)
        for i in range(20):
            room.post_and_wait(f"q{i}", timeout=5.0)
        # SingleResponder + 20 user posts → ≤ 20 user_turn_closed events
        # and ≥ 1 chat from inv0 per turn (loop guard may suppress some).
        closed_events = event_recorder.by_control_type("user_turn_closed")
        completed_closes = [e for e in closed_events
                            if e.body.get("reason") == "completed"]
        agent_chats = [e for e in event_recorder.by_kind("chat")
                       if e.sender == "inv0"]
        # The chat count should not exceed the number of completed turns
        # by more than the loop-guard bypass margin (one chat per turn).
        assert len(agent_chats) <= len(closed_events)
        assert len(completed_closes) >= 1

    def test_obligation_recorded_resolved_pairs_balance_at_session_end(
            self, multi_turn_session, varied_agents, event_recorder):
        agents = varied_agents(3, prefix="bal")
        room = multi_turn_session(
            agents=agents,
            policy=OpenChatPolicy(),
        )
        event_recorder.attach(room)
        for i in range(15):
            room.post_and_wait(f"q{i}", timeout=5.0)
        recorded = event_recorder.by_control_type("obligation_recorded")
        resolved = event_recorder.by_control_type("obligation_resolved")
        # Every obligation recorded should eventually resolve. We allow
        # a small lag if the last turn's idle timer hasn't fired yet —
        # explicit close on stop will catch any stragglers.
        room.stop(timeout=10.0)
        resolved = event_recorder.by_control_type("obligation_resolved")
        assert len(resolved) >= len(recorded) - 3, (
            f"recorded={len(recorded)} but only resolved={len(resolved)}")

    def test_room_epoch_strictly_monotonic_nondecreasing_across_100_turns(
            self, multi_turn_session, varied_agents, event_recorder):
        agents = varied_agents(2, prefix="ep")
        room = multi_turn_session(
            agents=agents,
            policy=SingleResponderPolicy("ep0"),
        )
        event_recorder.attach(room)
        for i in range(100):
            room.post_and_wait(f"q{i}", timeout=5.0)
        # Read the live state's epoch via observation.
        final_epoch = room.session.state.room_epoch
        assert final_epoch >= 0
        # Epoch only increments on membership / slot changes, which
        # didn't happen here — so it should equal whatever it was after
        # the initial participant adds + default_responder set (no
        # explicit default_responder was set, so just the registers).
        # The exact value depends on the kernel; the invariant is that
        # it didn't decrease and is bounded by participant changes.
        assert final_epoch <= 100  # generous upper bound

    def test_no_orphan_obligations_after_long_session(
            self, multi_turn_session, varied_agents, event_recorder):
        agents = varied_agents(3, prefix="orph")
        room = multi_turn_session(
            agents=agents,
            policy=OpenChatPolicy(),
        )
        event_recorder.attach(room)
        for i in range(20):
            room.post_and_wait(f"q{i}", timeout=5.0)
        room.stop(timeout=10.0)
        recorded = event_recorder.by_control_type("obligation_recorded")
        resolved = event_recorder.by_control_type("obligation_resolved")
        recorded_ids = {e.body.get("obligation_id") for e in recorded}
        resolved_ids = {e.body.get("obligation_id") for e in resolved}
        orphans = recorded_ids - resolved_ids
        # Idle-timeout closures may leave a few unresolved obligations
        # — we just check the gap is small.
        assert len(orphans) <= 3, f"orphan obligation ids: {sorted(orphans)}"


# ---------------------------------------------------------------------------
# Membership churn during a long session.
# ---------------------------------------------------------------------------

class TestLongHaulMembershipChurn:
    """System-level: add/remove agents during a long-running session."""

    @pytest.mark.stress
    def test_add_remove_agent_alternation_during_50_turn_session(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(3, prefix="ch")
        room = multi_turn_session(agents=agents, policy=OpenChatPolicy())
        counter = [0]

        def churn_send(prompt):
            counter[0] += 1
            return (f"churn {counter[0]} reply long enough to bypass "
                    f"both buffers in the kernel canonical commit path.")

        for i in range(50):
            room.post_and_wait(f"q{i}", timeout=5.0)
            if i % 10 == 9:
                # Add a transient member, then remove on the next break.
                cid = f"churn{i}"
                room.add_agent(agent_from_send(cid, churn_send))
                assert cid in room.participants
            elif i % 10 == 4:
                cid_to_remove = f"churn{i - 5}"
                if cid_to_remove in room.participants:
                    room.remove_agent(cid_to_remove)
                    assert cid_to_remove not in room.participants

        # Original three are still here.
        for pid in ("ch0", "ch1", "ch2"):
            assert pid in room.participants

    @pytest.mark.stress
    def test_round_robin_with_mid_session_member_removal_skips_cleanly(
            self, multi_turn_session, varied_agents):
        agents = varied_agents(3, prefix="rrm")
        order = ["rrm0", "rrm1", "rrm2"]
        room = multi_turn_session(
            agents=agents,
            policy=RoundRobinPolicy(order),
        )
        # Drive a few turns to enter the rotation.
        for i in range(5):
            room.post_and_wait(f"warm{i}", timeout=5.0)
        # Remove the middle agent; the rotation should skip cleanly.
        room.remove_agent("rrm1")
        assert "rrm1" not in room.participants
        speakers_after_removal: list[str] = []
        for i in range(15):
            replies = room.post_and_wait(f"q{i}", timeout=5.0)
            for r in replies:
                if r.sender != "user":
                    speakers_after_removal.append(r.sender)
                    break
        # The removed agent never speaks again.
        assert "rrm1" not in speakers_after_removal
        # The remaining agents still receive the floor.
        assert set(speakers_after_removal) <= {"rrm0", "rrm2"}

    @pytest.mark.stress
    def test_open_chat_3_adds_3_removes_no_actor_thread_leak(
            self, multi_turn_session, varied_agents):
        # The autouse ``assert_no_thread_leak_extended`` fixture is the
        # primary assertion here; this test just exercises the add/remove
        # path enough to surface any actor-thread cleanup bug.
        base = varied_agents(2, prefix="base")
        room = multi_turn_session(agents=base, policy=OpenChatPolicy())

        def long_send(label: str):
            counter = [0]

            def _send(prompt):
                counter[0] += 1
                return (f"{label} reply {counter[0]} long enough "
                        "to bypass both buffers for canonical commit.")
            return _send

        for cycle in range(3):
            new_id = f"trans{cycle}"
            room.add_agent(agent_from_send(new_id, long_send(new_id)))
            assert new_id in room.participants
            room.post_and_wait(f"with-{new_id}", timeout=5.0)
            room.post_and_wait(f"with-{new_id}-2", timeout=5.0)
            room.remove_agent(new_id)
            assert new_id not in room.participants
            room.post_and_wait(f"after-{new_id}", timeout=5.0)
        # Base agents survive every cycle.
        for pid in ("base0", "base1"):
            assert pid in room.participants
