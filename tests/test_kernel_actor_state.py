"""Tests for v0.3 PR 13 — cursor persistence + per-policy threshold.

Closes v0.2.1 audit deferrals A3 (cursor persistence via
`cursor_advanced` semantic effect) and D3 (`policy_slow_threshold_ms`
moved to `RoomConfig`).
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.actor_state import (
    ActorStateRecord,
    CursorAdvancedEffect,
    cursor_for,
    register_cursor_advanced_reducer,
)
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.effects import build_kernel_registry
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState
from loom.kernel.state import new_kernel_state


def _coord(**config_overrides) -> RoomCoordinator:
    bus = MessageBus()
    cfg = RoomConfig(**config_overrides)
    state = RoomState(config=cfg)
    state.add_participant(ParticipantInfo(id="loom"))
    return RoomCoordinator(bus, state)


class CursorAdvancedEffectShape(unittest.TestCase):
    def test_effect_type_is_cursor_advanced(self):
        e = CursorAdvancedEffect(participant_id="loom")
        self.assertEqual(e.effect_type, "cursor_advanced")
        self.assertEqual(e.schema_version, 1)

    def test_default_fields(self):
        e = CursorAdvancedEffect()
        self.assertEqual(e.from_cursor, -1)
        self.assertEqual(e.to_cursor, -1)
        self.assertEqual(e.examined_event_ids, ())


class CursorReducer(unittest.TestCase):
    def test_reducer_creates_actor_record(self):
        reg = build_kernel_registry()
        register_cursor_advanced_reducer(reg)
        state = new_kernel_state(RoomState(config=RoomConfig()))
        reg.apply(state, CursorAdvancedEffect(
            participant_id="loom", from_cursor=-1, to_cursor=42,
        ))
        self.assertIsNotNone(state.actors)
        rec = state.actors["loom"]
        self.assertEqual(rec.cursor, 42)
        self.assertEqual(rec.participant_id, "loom")

    def test_reducer_overwrites_previous_record(self):
        reg = build_kernel_registry()
        register_cursor_advanced_reducer(reg)
        state = new_kernel_state(RoomState(config=RoomConfig()))
        reg.apply(state, CursorAdvancedEffect(
            participant_id="loom", from_cursor=-1, to_cursor=10,
        ))
        reg.apply(state, CursorAdvancedEffect(
            participant_id="loom", from_cursor=10, to_cursor=20,
        ))
        self.assertEqual(state.actors["loom"].cursor, 20)

    def test_cursor_for_helper_returns_none_when_no_record(self):
        state = new_kernel_state(RoomState(config=RoomConfig()))
        self.assertIsNone(cursor_for(state, "loom"))

    def test_cursor_for_helper_returns_persisted_value(self):
        reg = build_kernel_registry()
        register_cursor_advanced_reducer(reg)
        state = new_kernel_state(RoomState(config=RoomConfig()))
        reg.apply(state, CursorAdvancedEffect(
            participant_id="loom", from_cursor=-1, to_cursor=99,
        ))
        self.assertEqual(cursor_for(state, "loom"), 99)


class CoordinatorWiresCursorReducer(unittest.TestCase):
    def test_coordinator_registers_cursor_reducer(self):
        coord = _coord()
        self.assertTrue(
            coord._effect_registry.has("cursor_advanced", 1)
        )

    def test_apply_cursor_advanced_via_coordinator(self):
        coord = _coord()
        with coord._lock:
            coord._apply_effect(CursorAdvancedEffect(
                participant_id="loom", from_cursor=-1, to_cursor=7,
            ))
        self.assertEqual(cursor_for(coord.kernel_state, "loom"), 7)


class ReplayDeterminism(unittest.TestCase):
    def test_replay_of_sequence_reconstructs_same_state(self):
        reg = build_kernel_registry()
        register_cursor_advanced_reducer(reg)
        live = new_kernel_state(RoomState(config=RoomConfig()))
        replay = new_kernel_state(RoomState(config=RoomConfig()))
        seq = [
            CursorAdvancedEffect(
                participant_id="loom", from_cursor=-1, to_cursor=5),
            CursorAdvancedEffect(
                participant_id="claude_code", from_cursor=-1, to_cursor=3),
            CursorAdvancedEffect(
                participant_id="loom", from_cursor=5, to_cursor=12),
        ]
        for e in seq:
            reg.apply(live, e)
        for e in seq:
            reg.apply(replay, e)
        self.assertEqual(live.actors, replay.actors)


class PolicySlowThresholdFromConfig(unittest.TestCase):
    """v0.3 PR 13 closes audit D3 — threshold reads from RoomConfig."""

    def test_default_threshold_matches_v021_baseline(self):
        coord = _coord()
        self.assertEqual(coord.config.policy_slow_threshold_ms, 100.0)

    def test_custom_threshold_overrides_default(self):
        coord = _coord(policy_slow_threshold_ms=250.0)
        self.assertEqual(coord.config.policy_slow_threshold_ms, 250.0)

    def test_policy_slow_event_uses_configured_threshold(self):
        # Drive a slow classify_fn (sleep 50ms) under a 10ms threshold
        # → expect policy_slow event whose threshold_ms == 10.0.
        import time
        coord = _coord(policy_slow_threshold_ms=10.0)
        from loom.kernel.obligations import plan_for_default

        def slow_classify(e):
            time.sleep(0.05)
            return plan_for_default("loom", reason="t",
                                    target_event_ids=[e.id])

        user_event = ev.chat(sender="user", body="hi")
        coord.bus.post(user_event)
        coord.post_user_event_and_open_turn(user_event, slow_classify)
        slows = [x for x in coord.bus.snapshot()
                 if ev.control_type_of(x) == "policy_slow"]
        self.assertEqual(len(slows), 1)
        self.assertEqual(slows[0].body["threshold_ms"], 10.0)


if __name__ == "__main__":
    unittest.main()
