"""Cross-module Loom kernel integration tests.

These scenarios use only fake in-process agents/proxies. They exercise
runtime wiring, policies, actor decisions, streaming commits, turn
closure, room control, and journaling together without any network calls.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loom.kernel.room import RoomConfig
from loom.policy.default import DefaultPolicy
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.round_robin import RoundRobinPolicy
from loom.policy.single_responder import SingleResponderPolicy
from loom.runtime import (
    ParticipantWiring,
    SendProxyAdapter,
    build_loom_session,
    post_user_text,
)


class FakeSendProxy:
    """Deterministic ``send`` proxy used behind ``SendProxyAdapter``."""

    def __init__(self, *responses: str):
        self.responses = list(responses) or ["ok"]
        self.calls = 0
        self.prompts = []

    def send(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        idx = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[idx]


def _long_reply(pid: str, marker: str = "reply") -> str:
    return (
        f"{pid} {marker}: this deterministic integration response is "
        "long enough to avoid the short duplicate loop guard."
    )


def _wiring(pid: str, *responses: str, cost_tier: int = 1):
    proxy = FakeSendProxy(*(responses or (_long_reply(pid),)))
    return ParticipantWiring(
        id=pid,
        proxy=SendProxyAdapter(proxy),
        cost_tier=cost_tier,
    )


def _drive_until_closed(case: unittest.TestCase, session, *,
                        rounds: int = 10):
    turn = session.coordinator.user_turn
    case.assertIsNotNone(turn)
    target_turn_id = turn.id
    for _ in range(rounds):
        for actor in list(session.actors):
            actor.step()
        turn = session.coordinator.user_turn
        if turn is not None and turn.id == target_turn_id \
                and turn.state == "closed":
            return turn
    case.fail(f"user turn {target_turn_id} did not close")


def _agent_chats(session, *, turn_id: int | None = None):
    chats = [
        e for e in session.bus.snapshot()
        if e.kind == "chat" and e.sender != "user"
    ]
    if turn_id is not None:
        chats = [e for e in chats if e.user_turn_id == turn_id]
    return chats


class OpenChatIntegration(unittest.TestCase):
    def test_ten_fake_agents_each_reply_once_and_turn_completes(self):
        ids = [f"a{i}" for i in range(10)]
        session = build_loom_session(
            [_wiring(pid, _long_reply(pid), cost_tier=i)
             for i, pid in enumerate(ids)],
            policy=OpenChatPolicy(),
            auto_start=False,
        )
        try:
            post_user_text(session, "everyone weigh in")
            turn = _drive_until_closed(self, session)
            self.assertEqual(turn.closure_reason, "completed")
            chats = _agent_chats(session, turn_id=turn.id)
            self.assertEqual({e.sender for e in chats}, set(ids))
            counts = {pid: 0 for pid in ids}
            for chat in chats:
                counts[chat.sender] += 1
            self.assertTrue(all(count == 1 for count in counts.values()))
        finally:
            session.stop()


class DefaultPolicyIntegration(unittest.TestCase):
    def test_direct_mention_routes_only_addressed_agent_and_waits_for_user(self):
        session = build_loom_session(
            [_wiring("alpha"), _wiring("bravo"), _wiring("charlie")],
            policy=DefaultPolicy(),
            auto_start=False,
        )
        try:
            post_user_text(session, "@bravo can you take this one?")
            turn = _drive_until_closed(self, session)
            chats = _agent_chats(session, turn_id=turn.id)
            self.assertEqual([e.sender for e in chats], ["bravo"])
            self.assertTrue(session.state.control.wait_for_user)
        finally:
            session.stop()

    def test_debounced_followup_remains_same_turn_and_wakes_actor(self):
        session = build_loom_session(
            [_wiring("alpha", _long_reply("alpha", "debounced"))],
            config=RoomConfig(user_turn_debounce_ms=1000),
            policy=SingleResponderPolicy("alpha"),
            auto_start=False,
        )
        try:
            first = post_user_text(session, "first question")
            original_turn = session.coordinator.user_turn
            second = post_user_text(session, "follow-up detail")
            turn = session.coordinator.user_turn

            self.assertIs(turn, original_turn)
            self.assertEqual(turn.user_event_id, first.id)
            self.assertIn(second.id, turn.debounced_event_ids)

            closed = _drive_until_closed(self, session)
            starts = [
                e for e in session.bus.snapshot()
                if e.kind == "stream" and e.body.get("stream_event") == "start"
            ]
            self.assertEqual(closed.closure_reason, "completed")
            self.assertEqual(starts[-1].body["trigger_event_id"], second.id)
        finally:
            session.stop()


class IdleTimeoutIntegration(unittest.TestCase):
    def test_required_non_reply_closes_on_idle_window_not_lease_ttl(self):
        session = build_loom_session(
            [_wiring("alpha")],
            config=RoomConfig(user_turn_idle_timeout_s=0.1,
                              lease_ttl_s=60.0),
            policy=SingleResponderPolicy("alpha"),
            auto_start=False,
        )
        try:
            post_user_text(session, "please respond")
            turn = session.coordinator.user_turn
            self.assertEqual(turn.state, "open")
            session.coordinator.check_idle_timeout(
                now=turn.last_activity_at + 0.101)
            self.assertEqual(turn.state, "closed")
            self.assertEqual(turn.closure_reason, "obligation_unresolved")
        finally:
            session.stop()


class RoundRobinIntegration(unittest.TestCase):
    def test_round_robin_rotates_one_speaker_per_user_post(self):
        session = build_loom_session(
            [_wiring("alpha"), _wiring("bravo"), _wiring("charlie")],
            policy=RoundRobinPolicy(["alpha", "bravo", "charlie"]),
            auto_start=False,
        )
        try:
            observed = []
            for text in ("turn one", "turn two", "turn three"):
                post_user_text(session, text)
                turn = _drive_until_closed(self, session)
                chats = _agent_chats(session, turn_id=turn.id)
                self.assertEqual(len(chats), 1)
                observed.append(chats[0].sender)
            self.assertEqual(observed, ["alpha", "bravo", "charlie"])
            self.assertEqual(session.state.control.next_speaker_idx, 0)
            self.assertEqual(
                session.state.control.turn_taking_mode, "round_robin")
        finally:
            session.stop()


class SingleResponderIntegration(unittest.TestCase):
    def test_single_responder_policy_suppresses_other_live_agents(self):
        session = build_loom_session(
            [_wiring("alpha"), _wiring("bravo")],
            policy=SingleResponderPolicy("bravo"),
            auto_start=False,
        )
        try:
            post_user_text(session, "who is the configured responder?")
            turn = _drive_until_closed(self, session)
            chats = _agent_chats(session, turn_id=turn.id)
            self.assertEqual([e.sender for e in chats], ["bravo"])
            self.assertEqual(turn.closure_reason, "completed")
        finally:
            session.stop()


class JournaledSessionIntegration(unittest.TestCase):
    def test_events_and_snapshot_are_written_on_stop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session = build_loom_session(
                [_wiring("alpha")],
                policy=SingleResponderPolicy("alpha"),
                journal_dir=tmpdir,
                auto_start=False,
            )
            try:
                post_user_text(session, "persist this")
                turn = _drive_until_closed(self, session)
            finally:
                session.stop()

            root = Path(tmpdir)
            events_path = root / "events.jsonl"
            snapshot_path = root / "room_state.json"
            self.assertTrue(events_path.exists())
            self.assertTrue(snapshot_path.exists())

            lines = [line for line in events_path.read_text().splitlines()
                     if line.strip()]
            self.assertGreater(len(lines), 0)
            decoded = [json.loads(line) for line in lines]
            self.assertTrue(any(e["kind"] == "chat"
                                and e["sender"] == "alpha"
                                for e in decoded))

            snapshot = json.loads(snapshot_path.read_text())
            self.assertEqual(snapshot["current_user_turn_id"], None)
            self.assertEqual(snapshot["participants"][0]["id"], "alpha")
            self.assertEqual(turn.closure_reason, "completed")


if __name__ == "__main__":
    unittest.main()
