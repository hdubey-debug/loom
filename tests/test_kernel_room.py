"""Tests for ``loom.kernel.room`` — RoomConfig / RoomState / slot logic.

Covers:
- Defaults and basic mutation
- ``room_epoch`` increments on membership / slot changes (NOT on
  topic or activity flips — those are runtime-only and don't invalidate
  leases).
- Removing a slot occupant re-resolves to cheapest active capable
  participant; emits the slot-change in the return value.
- Cheapest-active-capable picks lowest cost_tier, ties broken by id.
- Inactive or non-capable participants are excluded from fallback.
"""
from __future__ import annotations

import unittest

from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomControlState,
    RoomState,
)


def _state(**overrides) -> RoomState:
    cfg = RoomConfig(**overrides) if overrides else RoomConfig()
    return RoomState(config=cfg)


class Defaults(unittest.TestCase):
    def test_room_config_defaults(self):
        cfg = RoomConfig()
        self.assertEqual(cfg.compact_threshold, 50)
        self.assertEqual(cfg.pass_buffer_chars, 16)
        self.assertEqual(cfg.lease_ttl_s, 60)
        self.assertEqual(cfg.max_drafts_per_participant, 1)

    def test_room_state_defaults(self):
        s = _state()
        self.assertEqual(s.room_epoch, 0)
        self.assertIsNone(s.topic)
        self.assertEqual(s.participants, {})
        self.assertIsNone(s.anchor_id)
        self.assertIsNone(s.default_responder_id)
        self.assertEqual(s.last_compacted_event_id, -1)


class MembershipAndEpoch(unittest.TestCase):
    def test_add_increments_epoch(self):
        s = _state()
        self.assertEqual(s.room_epoch, 0)
        s.add_participant(ParticipantInfo(id="claude_code"))
        self.assertEqual(s.room_epoch, 1)
        s.add_participant(ParticipantInfo(id="gemini_cli"))
        self.assertEqual(s.room_epoch, 2)

    def test_add_duplicate_raises(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="claude_code"))
        with self.assertRaises(ValueError):
            s.add_participant(ParticipantInfo(id="claude_code"))

    def test_remove_increments_epoch(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="claude_code"))
        epoch_before = s.room_epoch
        s.remove_participant("claude_code")
        self.assertEqual(s.room_epoch, epoch_before + 1)
        self.assertNotIn("claude_code", s.participants)

    def test_remove_unknown_raises(self):
        s = _state()
        with self.assertRaises(ValueError):
            s.remove_participant("nobody")

    def test_set_active_does_not_bump_epoch(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="claude_code"))
        epoch = s.room_epoch
        s.set_active("claude_code", False)
        self.assertEqual(s.room_epoch, epoch)
        self.assertFalse(s.participants["claude_code"].active)

    def test_set_active_unknown_pid_raises(self):
        s = _state()
        with self.assertRaises(ValueError):
            s.set_active("ghost", False)
        with self.assertRaises(ValueError):
            s.set_active("ghost", True)

    def test_set_active_after_remove_raises(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="claude_code"))
        s.remove_participant("claude_code")
        with self.assertRaises(ValueError):
            s.set_active("claude_code", False)


class RoomConfigNonDefaults(unittest.TestCase):
    """RoomConfig accepts non-default values and round-trips them onto state."""

    def test_compact_threshold_override(self):
        cfg = RoomConfig(compact_threshold=5)
        self.assertEqual(cfg.compact_threshold, 5)
        s = RoomState(config=cfg)
        self.assertEqual(s.config.compact_threshold, 5)

    def test_pass_buffer_chars_override(self):
        cfg = RoomConfig(pass_buffer_chars=4)
        self.assertEqual(cfg.pass_buffer_chars, 4)
        s = RoomState(config=cfg)
        self.assertEqual(s.config.pass_buffer_chars, 4)

    def test_max_drafts_per_participant_override(self):
        cfg = RoomConfig(max_drafts_per_participant=2)
        self.assertEqual(cfg.max_drafts_per_participant, 2)
        s = RoomState(config=cfg)
        self.assertEqual(s.config.max_drafts_per_participant, 2)

    def test_user_turn_debounce_ms_zero_override(self):
        cfg = RoomConfig(user_turn_debounce_ms=0)
        self.assertEqual(cfg.user_turn_debounce_ms, 0)
        s = RoomState(config=cfg)
        self.assertEqual(s.config.user_turn_debounce_ms, 0)


class TopicAndSlots(unittest.TestCase):
    def test_set_topic_does_not_bump_epoch(self):
        s = _state()
        epoch = s.room_epoch
        s.set_topic("god's existence")
        self.assertEqual(s.topic, "god's existence")
        self.assertEqual(s.room_epoch, epoch)


class CheapestActiveCapable(unittest.TestCase):
    def test_picks_lowest_cost_tier(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="loom", cost_tier=0))
        s.add_participant(ParticipantInfo(id="claude_code", cost_tier=2))
        s.add_participant(ParticipantInfo(id="gpt5_mini", cost_tier=1))
        self.assertEqual(s.cheapest_active_capable(), "loom")

    def test_alphabetic_tiebreak(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="zeta", cost_tier=1))
        s.add_participant(ParticipantInfo(id="alpha", cost_tier=1))
        self.assertEqual(s.cheapest_active_capable(), "alpha")

    def test_skips_inactive(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="loom", cost_tier=0,
                                          active=False))
        s.add_participant(ParticipantInfo(id="claude_code", cost_tier=2))
        self.assertEqual(s.cheapest_active_capable(), "claude_code")

    def test_skips_incapable(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="observer", cost_tier=0,
                                          capable=False))
        s.add_participant(ParticipantInfo(id="claude_code", cost_tier=2))
        self.assertEqual(s.cheapest_active_capable(), "claude_code")

    def test_returns_none_when_empty(self):
        s = _state()
        self.assertIsNone(s.cheapest_active_capable())

    def test_returns_none_when_all_inactive(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="loom", active=False))
        self.assertIsNone(s.cheapest_active_capable())


class SlotResolution(unittest.TestCase):
    def setUp(self):
        self.s = _state()
        self.s.add_participant(ParticipantInfo(id="loom", cost_tier=0))
        self.s.add_participant(ParticipantInfo(id="claude_code",
                                               cost_tier=2))
        self.s.add_participant(ParticipantInfo(id="gemini_cli",
                                               cost_tier=1))
        self.s.set_default_responder("claude_code")
        self.s.set_anchor("claude_code")

    def test_resolve_returns_configured_when_valid(self):
        self.assertEqual(self.s.resolve_default_responder(), "claude_code")

    def test_resolve_falls_back_when_inactive(self):
        self.s.set_active("claude_code", False)
        # Configured default is still claude_code, but inactive so falls
        # back to cheapest active capable = loom.
        self.assertEqual(self.s.resolve_default_responder(), "loom")

    def test_remove_default_responder_re_resolves_to_cheapest(self):
        changes = self.s.remove_participant("claude_code")
        # Both default_responder_id and anchor_id pointed to claude_code.
        self.assertEqual(changes["default_responder_id"], "loom")
        self.assertEqual(changes["anchor_id"], "loom")
        self.assertEqual(self.s.default_responder_id, "loom")
        self.assertEqual(self.s.anchor_id, "loom")

    def test_remove_non_slot_holder_does_not_change_slots(self):
        changes = self.s.remove_participant("gemini_cli")
        self.assertEqual(changes, {})
        self.assertEqual(self.s.default_responder_id, "claude_code")

    def test_remove_last_capable_drops_slot_to_none(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="onlyone"))
        s.set_default_responder("onlyone")
        changes = s.remove_participant("onlyone")
        self.assertIn("default_responder_id", changes)
        self.assertIsNone(changes["default_responder_id"])
        self.assertIsNone(s.default_responder_id)


class SlotSetters(unittest.TestCase):
    def test_set_slot_to_unknown_id_raises(self):
        s = _state()
        with self.assertRaises(ValueError):
            s.set_default_responder("ghost")
        with self.assertRaises(ValueError):
            s.set_anchor("ghost")
        with self.assertRaises(ValueError):
            s.set_chair("ghost")
        with self.assertRaises(ValueError):
            s.set_default_summarizer("ghost")

    def test_set_slot_to_none_allowed(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="loom"))
        s.set_default_responder("loom")
        s.set_default_responder(None)
        self.assertIsNone(s.default_responder_id)


class RoomControlStateTests(unittest.TestCase):
    """Coordinator-owned room control state — roles / floor / wait / style."""

    def test_defaults(self):
        ctl = RoomControlState()
        self.assertEqual(ctl.roles, {})
        self.assertIsNone(ctl.floor_owner)
        self.assertFalse(ctl.wait_for_user)
        self.assertEqual(ctl.style, "normal")

    def test_room_state_owns_control_with_defaults(self):
        s = _state()
        self.assertIsInstance(s.control, RoomControlState)
        self.assertEqual(s.control.style, "normal")
        self.assertIsNone(s.control.floor_owner)

    def test_set_roles_filters_unknown_pids(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="loom"))
        s.set_roles({"loom": "teacher", "ghost": "quizzer"})
        self.assertEqual(s.control.roles, {"loom": "teacher"})

    def test_set_roles_returns_old_mapping(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="loom"))
        s.set_roles({"loom": "teacher"})
        old = s.set_roles({"loom": "grader"})
        self.assertEqual(old, {"loom": "teacher"})
        self.assertEqual(s.control.roles, {"loom": "grader"})

    def test_set_roles_empty_clears(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="loom"))
        s.set_roles({"loom": "teacher"})
        s.set_roles({})
        self.assertEqual(s.control.roles, {})

    def test_set_floor_owner(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="loom"))
        s.add_participant(ParticipantInfo(id="claude_code"))
        s.set_floor_owner(["loom"])
        self.assertEqual(s.control.floor_owner, ["loom"])

    def test_set_floor_owner_filters_unknown(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="loom"))
        s.set_floor_owner(["loom", "ghost"])
        self.assertEqual(s.control.floor_owner, ["loom"])

    def test_set_floor_owner_empty_list_opens_floor(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="loom"))
        s.set_floor_owner(["loom"])
        s.set_floor_owner([])
        self.assertIsNone(s.control.floor_owner)

    def test_set_floor_owner_none_opens_floor(self):
        s = _state()
        s.add_participant(ParticipantInfo(id="loom"))
        s.set_floor_owner(["loom"])
        s.set_floor_owner(None)
        self.assertIsNone(s.control.floor_owner)

    def test_set_wait_for_user_toggles(self):
        s = _state()
        self.assertFalse(s.control.wait_for_user)
        s.set_wait_for_user(True)
        self.assertTrue(s.control.wait_for_user)
        s.set_wait_for_user(False)
        self.assertFalse(s.control.wait_for_user)

    def test_set_style_validates(self):
        s = _state()
        s.set_style("brief")
        self.assertEqual(s.control.style, "brief")
        with self.assertRaises(ValueError):
            s.set_style("ultra-brief")

    def test_set_topic_via_state(self):
        # P2.3: ``active_goal`` collapsed into ``state.topic``.
        s = _state()
        s.set_topic("teach derivatives")
        self.assertEqual(s.topic, "teach derivatives")
        s.set_topic(None)
        self.assertIsNone(s.topic)

    def test_control_state_does_not_bump_epoch(self):
        # Control-state changes are runtime preferences; they should
        # not invalidate in-flight leases the way slot/membership
        # changes do.
        s = _state()
        s.add_participant(ParticipantInfo(id="loom"))
        before = s.room_epoch
        s.set_roles({"loom": "teacher"})
        s.set_floor_owner(["loom"])
        s.set_style("brief")
        s.set_wait_for_user(True)
        s.set_topic("X")
        s.set_turn_taking_mode("round_robin")
        s.set_turn_order(["loom"])
        s.advance_round_robin_pointer()
        # Only the participant add should have bumped epoch — the
        # control mutations are pure preference updates.
        self.assertEqual(s.room_epoch, before)


class TurnTakingModeTests(unittest.TestCase):
    """Round-robin mode setter, turn order, rotation pointer."""

    def _state_with(self, *ids: str) -> RoomState:
        s = _state()
        for pid in ids:
            s.add_participant(ParticipantInfo(id=pid))
        return s

    def test_default_is_broadcast(self):
        s = self._state_with("a", "b")
        self.assertEqual(s.control.turn_taking_mode, "broadcast")
        self.assertEqual(s.control.turn_order, [])
        self.assertEqual(s.control.next_speaker_idx, 0)

    def test_set_turn_taking_mode_round_robin(self):
        s = self._state_with("a", "b")
        old = s.set_turn_taking_mode("round_robin")
        self.assertEqual(old, "broadcast")
        self.assertEqual(s.control.turn_taking_mode, "round_robin")

    def test_set_turn_taking_mode_back_to_broadcast_clears_order(self):
        s = self._state_with("a", "b")
        s.set_turn_taking_mode("round_robin")
        s.set_turn_order(["a", "b"])
        s.control.next_speaker_idx = 1
        s.set_turn_taking_mode("broadcast")
        self.assertEqual(s.control.turn_order, [])
        self.assertEqual(s.control.next_speaker_idx, 0)

    def test_set_turn_taking_mode_invalid_raises(self):
        s = self._state_with("a")
        with self.assertRaises(ValueError):
            s.set_turn_taking_mode("debate")

    def test_set_turn_order_filters_unknown(self):
        s = self._state_with("a", "b")
        old = s.set_turn_order(["a", "ghost", "b"])
        self.assertEqual(old, [])
        self.assertEqual(s.control.turn_order, ["a", "b"])

    def test_set_turn_order_resets_pointer(self):
        s = self._state_with("a", "b")
        s.set_turn_order(["a", "b"])
        s.control.next_speaker_idx = 1
        s.set_turn_order(["a", "b"])
        self.assertEqual(s.control.next_speaker_idx, 0)

    def test_advance_pointer_wraps(self):
        s = self._state_with("a", "b", "c")
        s.set_turn_order(["a", "b", "c"])
        self.assertEqual(s.advance_round_robin_pointer(), 1)
        self.assertEqual(s.advance_round_robin_pointer(), 2)
        self.assertEqual(s.advance_round_robin_pointer(), 0)

    def test_advance_pointer_skips_inactive(self):
        s = self._state_with("a", "b", "c")
        s.set_turn_order(["a", "b", "c"])
        s.set_active("c", False)
        # Live = [a, b], wraps over 2.
        s.advance_round_robin_pointer()
        s.advance_round_robin_pointer()
        self.assertEqual(s.control.next_speaker_idx, 0)

    def test_advance_pointer_zeroes_when_no_live(self):
        s = self._state_with("a")
        s.set_turn_order(["a"])
        s.set_active("a", False)
        self.assertEqual(s.advance_round_robin_pointer(), 0)


if __name__ == "__main__":
    unittest.main()
