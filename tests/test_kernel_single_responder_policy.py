"""Tests for :class:`loom.policy.single_responder.SingleResponderPolicy`."""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState
from loom.policy.single_responder import SingleResponderPolicy


def _state(*pids_with_active: tuple[str, bool, bool]) -> RoomState:
    state = RoomState(config=RoomConfig())
    for pid, active, capable in pids_with_active:
        state.add_participant(ParticipantInfo(id=pid, active=active, capable=capable))
    return state


class SingleResponderRouting(unittest.TestCase):
    def test_routes_to_configured_responder(self):
        state = _state(("loom", True, True), ("claude", True, True))
        e = ev.chat(sender="user", body="hi everyone")
        plan = SingleResponderPolicy("loom").plan_user_turn(e, state)

        self.assertTrue(plan.requires_response)
        self.assertEqual(plan.routing_case, "single_responder")
        self.assertEqual(plan.required_participants, {"loom"})
        self.assertEqual(plan.allowed_speakers, {"loom"})
        self.assertEqual(plan.max_responses, 1)
        self.assertTrue(plan.wait_for_user_after)
        self.assertEqual(len(plan.obligations), 1)
        self.assertEqual(plan.obligations[0].participant_id, "loom")
        self.assertEqual(plan.obligations[0].level, "must")

    def test_other_agents_not_required_or_allowed(self):
        state = _state(("loom", True, True), ("claude", True, True))
        e = ev.chat(sender="user", body="anything")
        plan = SingleResponderPolicy("claude").plan_user_turn(e, state)
        self.assertNotIn("loom", plan.required_participants)
        self.assertNotIn("loom", plan.allowed_speakers)

    def test_event_id_zero_keeps_target_event_ids(self):
        # Bug repro (id=0 should NOT be treated as falsy).
        state = _state(("loom", True, True))
        e = ev.chat(sender="user", body="first")
        e.id = 0
        plan = SingleResponderPolicy("loom").plan_user_turn(e, state)
        self.assertEqual(plan.target_event_ids, [0])


class SingleResponderEdgeCases(unittest.TestCase):
    def test_responder_not_in_room_returns_no_response(self):
        state = _state(("claude", True, True))
        e = ev.chat(sender="user", body="hi")
        plan = SingleResponderPolicy("loom").plan_user_turn(e, state)
        self.assertFalse(plan.requires_response)
        self.assertEqual(plan.routing_case, "acknowledgement")

    def test_responder_inactive_returns_no_response(self):
        state = _state(("loom", False, True))
        e = ev.chat(sender="user", body="hi")
        plan = SingleResponderPolicy("loom").plan_user_turn(e, state)
        self.assertFalse(plan.requires_response)

    def test_responder_not_capable_returns_no_response(self):
        state = _state(("loom", True, False))
        e = ev.chat(sender="user", body="hi")
        plan = SingleResponderPolicy("loom").plan_user_turn(e, state)
        self.assertFalse(plan.requires_response)

    def test_construction_rejects_empty_id(self):
        with self.assertRaises(ValueError):
            SingleResponderPolicy("")

    def test_construction_rejects_none(self):
        with self.assertRaises(ValueError):
            SingleResponderPolicy(None)  # type: ignore[arg-type]

    def test_name_attribute(self):
        self.assertEqual(SingleResponderPolicy.name, "single_responder")

    def test_default_prompts_are_empty(self):
        p = SingleResponderPolicy("loom")
        state = _state(("loom", True, True))
        self.assertEqual(p.system_prompt("loom", state), "")
        self.assertEqual(p.role_prompt("loom", state), "")


if __name__ == "__main__":
    unittest.main()
