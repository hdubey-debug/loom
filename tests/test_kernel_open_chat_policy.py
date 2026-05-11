"""Tests for :class:`loom.policy.open_chat.OpenChatPolicy`.

The simplest broadcast policy — every active capable participant gets
a ``must`` obligation. Empty room ⇒ no-response plan.
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState
from loom.policy.open_chat import OpenChatPolicy


def _state(*pids_with_active: tuple[str, bool, bool]) -> RoomState:
    state = RoomState(config=RoomConfig())
    for pid, active, capable in pids_with_active:
        state.add_participant(ParticipantInfo(id=pid, active=active, capable=capable))
    return state


class OpenChatBroadcast(unittest.TestCase):
    """Typical case: every active capable participant is required."""

    def test_broadcast_to_all_active_capable(self):
        state = _state(("loom", True, True), ("claude", True, True))
        e = ev.chat(sender="user", body="hello room")
        plan = OpenChatPolicy().plan_user_turn(e, state)

        self.assertTrue(plan.requires_response)
        self.assertEqual(plan.routing_case, "broadcast")
        self.assertEqual(plan.required_participants, {"loom", "claude"})
        self.assertEqual(plan.allowed_speakers, {"loom", "claude"})
        self.assertEqual(plan.max_responses, 2)
        self.assertFalse(plan.wait_for_user_after)
        # Every required participant has an obligation.
        ids = sorted(o.participant_id for o in plan.obligations)
        self.assertEqual(ids, ["claude", "loom"])
        for o in plan.obligations:
            self.assertEqual(o.level, "must")

    def test_single_participant_room(self):
        state = _state(("loom", True, True))
        e = ev.chat(sender="user", body="solo")
        plan = OpenChatPolicy().plan_user_turn(e, state)
        self.assertTrue(plan.requires_response)
        self.assertEqual(plan.required_participants, {"loom"})
        self.assertEqual(plan.max_responses, 1)


class OpenChatEdgeCases(unittest.TestCase):
    def test_empty_room_no_response(self):
        state = _state()
        e = ev.chat(sender="user", body="hello?")
        plan = OpenChatPolicy().plan_user_turn(e, state)
        self.assertFalse(plan.requires_response)
        self.assertEqual(plan.routing_case, "acknowledgement")
        self.assertEqual(plan.required_participants, set())

    def test_event_id_zero_keeps_target_event_ids(self):
        # Bug repro: bus assigns ids starting at 0; the first user post
        # carries id=0 which used to be treated as falsy, dropping the
        # target_event_ids correlation.
        state = _state(("loom", True, True))
        e = ev.chat(sender="user", body="first post")
        e.id = 0
        plan = OpenChatPolicy().plan_user_turn(e, state)
        self.assertEqual(plan.target_event_ids, [0])

    def test_inactive_participants_excluded(self):
        state = _state(
            ("loom", True, True),
            ("ghost", False, True),  # inactive
            ("dumb", True, False),  # not capable
        )
        e = ev.chat(sender="user", body="who's home")
        plan = OpenChatPolicy().plan_user_turn(e, state)
        self.assertEqual(plan.required_participants, {"loom"})

    def test_all_inactive_no_response(self):
        state = _state(("a", False, True), ("b", False, True))
        e = ev.chat(sender="user", body="bueller?")
        plan = OpenChatPolicy().plan_user_turn(e, state)
        self.assertFalse(plan.requires_response)
        self.assertEqual(plan.required_participants, set())

    def test_name_attribute(self):
        self.assertEqual(OpenChatPolicy.name, "open_chat")

    def test_default_prompts_are_empty(self):
        p = OpenChatPolicy()
        state = _state(("loom", True, True))
        self.assertEqual(p.system_prompt("loom", state), "")
        self.assertEqual(p.role_prompt("loom", state), "")


if __name__ == "__main__":
    unittest.main()
