"""Tests for :class:`loom.policy.round_robin.RoundRobinPolicy`.

Covers:
- First post arms round-robin mode and routes to order[0].
- Subsequent posts use the kernel's rotation pointer.
- Removed-mid-rotation: an inactive id is skipped; live order is the
  filter set used for the modulo.
- All-inactive ⇒ no-response plan.
- Construction validates the order argument.
"""
from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState
from loom.policy.round_robin import RoundRobinPolicy


def _state_with(*pids_with_active: tuple[str, bool, bool]) -> RoomState:
    state = RoomState(config=RoomConfig())
    for pid, active, capable in pids_with_active:
        state.add_participant(
            ParticipantInfo(id=pid, active=active, capable=capable))
    return state


class RoundRobinFirstTurn(unittest.TestCase):
    """First post: turn_taking_mode is broadcast → arm round-robin."""

    def test_first_post_arms_mode_and_picks_order_zero(self):
        state = _state_with(("a", True, True), ("b", True, True),
                            ("c", True, True))
        e = ev.chat(sender="user", body="kickoff")
        plan = RoundRobinPolicy(["a", "b", "c"]).plan_user_turn(e, state)

        self.assertTrue(plan.requires_response)
        self.assertEqual(plan.required_participants, {"a"})
        self.assertEqual(plan.max_responses, 1)
        self.assertTrue(plan.wait_for_user_after)
        self.assertEqual(plan.set_turn_taking_mode, "round_robin")
        self.assertEqual(plan.set_turn_order, ["a", "b", "c"])
        self.assertTrue(plan.advance_turn_pointer)

    def test_event_id_zero_keeps_target_event_ids(self):
        # Bug repro (id=0 should NOT be treated as falsy).
        state = _state_with(("a", True, True), ("b", True, True))
        e = ev.chat(sender="user", body="kickoff")
        e.id = 0
        plan = RoundRobinPolicy(["a", "b"]).plan_user_turn(e, state)
        self.assertEqual(plan.target_event_ids, [0])

    def test_first_post_skips_inactive_in_pick(self):
        # ``a`` is inactive at the first post, so the first speaker
        # picked is ``b`` (still arms round-robin with the full order).
        state = _state_with(("a", False, True), ("b", True, True),
                            ("c", True, True))
        e = ev.chat(sender="user", body="kickoff")
        plan = RoundRobinPolicy(["a", "b", "c"]).plan_user_turn(e, state)
        self.assertEqual(plan.required_participants, {"b"})
        self.assertEqual(plan.set_turn_order, ["a", "b", "c"])


class RoundRobinSubsequent(unittest.TestCase):
    """Mode already armed → use the kernel's rotation pointer."""

    def _armed_state(self, idx: int = 0,
                     order: list[str] | None = None) -> RoomState:
        state = _state_with(("a", True, True), ("b", True, True),
                            ("c", True, True))
        state.control.turn_taking_mode = "round_robin"
        state.control.turn_order = list(order or ["a", "b", "c"])
        state.control.next_speaker_idx = idx
        return state

    def test_idx_zero_picks_first(self):
        plan = RoundRobinPolicy(["a", "b", "c"]).plan_user_turn(
            ev.chat(sender="user", body="next"),
            self._armed_state(idx=0),
        )
        self.assertEqual(plan.required_participants, {"a"})
        self.assertTrue(plan.advance_turn_pointer)
        # No state-mutation flags on subsequent turns (kernel keeps mode).
        self.assertIsNone(plan.set_turn_taking_mode)
        self.assertIsNone(plan.set_turn_order)

    def test_idx_one_picks_second(self):
        plan = RoundRobinPolicy(["a", "b", "c"]).plan_user_turn(
            ev.chat(sender="user", body="next"),
            self._armed_state(idx=1),
        )
        self.assertEqual(plan.required_participants, {"b"})

    def test_idx_wraps_via_modulo(self):
        plan = RoundRobinPolicy(["a", "b", "c"]).plan_user_turn(
            ev.chat(sender="user", body="next"),
            self._armed_state(idx=4),  # 4 % 3 = 1 → b
        )
        self.assertEqual(plan.required_participants, {"b"})

    def test_inactive_skipped_in_rotation(self):
        # turn_order is [a, b, c] but ``b`` went inactive — live order
        # becomes [a, c]. idx=1 mod 2 = 1 → c.
        state = _state_with(("a", True, True), ("b", False, True),
                            ("c", True, True))
        state.control.turn_taking_mode = "round_robin"
        state.control.turn_order = ["a", "b", "c"]
        state.control.next_speaker_idx = 1
        plan = RoundRobinPolicy(["a", "b", "c"]).plan_user_turn(
            ev.chat(sender="user", body="next"), state)
        self.assertEqual(plan.required_participants, {"c"})

    def test_all_inactive_returns_no_response(self):
        state = _state_with(("a", False, True), ("b", False, True))
        state.control.turn_taking_mode = "round_robin"
        state.control.turn_order = ["a", "b"]
        plan = RoundRobinPolicy(["a", "b"]).plan_user_turn(
            ev.chat(sender="user", body="anyone?"), state)
        self.assertFalse(plan.requires_response)


class RoundRobinConstruction(unittest.TestCase):

    def test_empty_order_rejected(self):
        with self.assertRaises(ValueError):
            RoundRobinPolicy([])

    def test_non_string_order_rejected(self):
        with self.assertRaises(ValueError):
            RoundRobinPolicy(["a", 7])  # type: ignore[list-item]

    def test_empty_string_in_order_rejected(self):
        with self.assertRaises(ValueError):
            RoundRobinPolicy(["a", ""])

    def test_dedupes_while_preserving_order(self):
        p = RoundRobinPolicy(["a", "b", "a", "c"])
        self.assertEqual(p.order, ["a", "b", "c"])

    def test_order_property_returns_copy(self):
        p = RoundRobinPolicy(["a", "b"])
        original = p.order
        original.append("c")
        self.assertEqual(p.order, ["a", "b"])

    def test_first_post_with_no_active_returns_no_response(self):
        state = _state_with(("a", False, True), ("b", False, True))
        plan = RoundRobinPolicy(["a", "b"]).plan_user_turn(
            ev.chat(sender="user", body="hi"), state)
        self.assertFalse(plan.requires_response)

    def test_name_attribute(self):
        self.assertEqual(RoundRobinPolicy.name, "round_robin")


if __name__ == "__main__":
    unittest.main()
