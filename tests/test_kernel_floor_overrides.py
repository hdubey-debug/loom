"""Tests for ``loom.kernel.floor_overrides`` — v0.3 PR 10.

Doctrine: §10 (policy/control precedence).
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.capabilities import CapabilityName
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.effects import CapabilityGrantedEffect, FloorOverrideEffect
from loom.kernel.floor_overrides import (
    ActiveOverride,
    FloorOverrideMode,
    FloorOverrideScope,
    compute_effective_speakers,
    prune_overrides_for_lease,
    prune_overrides_for_turn,
)
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState
from loom.kernel.state import new_kernel_state


def _coord() -> RoomCoordinator:
    bus = MessageBus()
    state = RoomState(config=RoomConfig())
    state.add_participant(ParticipantInfo(id="loom"))
    state.add_participant(ParticipantInfo(id="claude_code"))
    state.add_participant(ParticipantInfo(id="gemini"))
    return RoomCoordinator(bus, state)


def _grant(coord: RoomCoordinator, grantee: str, cap: CapabilityName) -> None:
    with coord._lock:
        coord._apply_effect(
            CapabilityGrantedEffect(
                grant_id=f"g-{grantee}-{cap.value}",
                grantee_id=grantee,
                capability=cap.value,
                grantor_id="user",
            )
        )


class CompositionRule(unittest.TestCase):
    """compute_effective_speakers applies the §10 rule."""

    def test_base_empty_returns_empty(self):
        self.assertEqual(compute_effective_speakers([], []), frozenset())

    def test_base_passthrough_no_overrides(self):
        self.assertEqual(
            compute_effective_speakers(["a", "b"], []),
            frozenset({"a", "b"}),
        )

    def test_add_extends_base(self):
        ov = ActiveOverride(
            mode=FloorOverrideMode.ADD,
            scope=FloorOverrideScope.ONE_LEASE,
            speakers=("c",),
        )
        eff = compute_effective_speakers(["a"], [ov])
        self.assertEqual(eff, frozenset({"a", "c"}))

    def test_block_strips(self):
        ov = ActiveOverride(
            mode=FloorOverrideMode.BLOCK,
            scope=FloorOverrideScope.CURRENT_TURN,
            speakers=("a",),
        )
        eff = compute_effective_speakers(["a", "b"], [ov])
        self.assertEqual(eff, frozenset({"b"}))

    def test_replace_wins(self):
        ov = ActiveOverride(
            mode=FloorOverrideMode.REPLACE,
            scope=FloorOverrideScope.UNTIL_CLEARED,
            speakers=("x",),
        )
        eff = compute_effective_speakers(["a", "b"], [ov])
        self.assertEqual(eff, frozenset({"x"}))

    def test_block_after_replace_strips_from_replace_set(self):
        ov1 = ActiveOverride(
            mode=FloorOverrideMode.REPLACE,
            scope=FloorOverrideScope.UNTIL_CLEARED,
            speakers=("x", "y"),
        )
        ov2 = ActiveOverride(
            mode=FloorOverrideMode.BLOCK,
            scope=FloorOverrideScope.CURRENT_TURN,
            speakers=("x",),
        )
        eff = compute_effective_speakers([], [ov1, ov2])
        self.assertEqual(eff, frozenset({"y"}))

    def test_multiple_adds_compose(self):
        ov1 = ActiveOverride(
            mode=FloorOverrideMode.ADD,
            scope=FloorOverrideScope.ONE_LEASE,
            speakers=("b",),
        )
        ov2 = ActiveOverride(
            mode=FloorOverrideMode.ADD,
            scope=FloorOverrideScope.ONE_LEASE,
            speakers=("c",),
        )
        eff = compute_effective_speakers(["a"], [ov1, ov2])
        self.assertEqual(eff, frozenset({"a", "b", "c"}))


class ReducerWiring(unittest.TestCase):
    def test_apply_floor_override_effect_appends_to_state(self):
        coord = _coord()
        with coord._lock:
            coord._apply_effect(
                FloorOverrideEffect(
                    mode=FloorOverrideMode.ADD.value,
                    scope=FloorOverrideScope.ONE_LEASE.value,
                    speakers=("loom",),
                )
            )
        overrides = coord.state.control.active_overrides
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0].mode, FloorOverrideMode.ADD)
        self.assertEqual(overrides[0].speakers, ("loom",))

    def test_unknown_mode_raises(self):
        coord = _coord()
        with self.assertRaises(ValueError):
            with coord._lock:
                coord._apply_effect(
                    FloorOverrideEffect(mode="MADE_UP", scope="ONE_LEASE")
                )


class PruningLifecycle(unittest.TestCase):
    def _state_with_overrides(self):
        coord = _coord()
        for spec in [
            (FloorOverrideMode.ADD, FloorOverrideScope.ONE_LEASE, 7, None),
            (FloorOverrideMode.ADD, FloorOverrideScope.CURRENT_TURN, None, 3),
            (FloorOverrideMode.ADD, FloorOverrideScope.UNTIL_CLEARED, None, None),
        ]:
            ov = ActiveOverride(
                mode=spec[0],
                scope=spec[1],
                speakers=("x",),
                lease_id=spec[2],
                turn_id=spec[3],
            )
            coord.state.control.__dict__.setdefault("active_overrides", []).append(ov)
        return coord

    def test_prune_for_lease_removes_one_lease_scope(self):
        coord = self._state_with_overrides()
        removed = prune_overrides_for_lease(coord.kernel_state, 7)
        self.assertEqual(removed, 1)
        self.assertEqual(len(coord.state.control.active_overrides), 2)

    def test_prune_for_turn_removes_current_turn_scope(self):
        coord = self._state_with_overrides()
        removed = prune_overrides_for_turn(coord.kernel_state, 3)
        self.assertEqual(removed, 1)

    def test_prune_keeps_until_cleared(self):
        coord = self._state_with_overrides()
        prune_overrides_for_lease(coord.kernel_state, 7)
        prune_overrides_for_turn(coord.kernel_state, 3)
        # UNTIL_CLEARED remains.
        remaining = coord.state.control.active_overrides
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].scope, FloorOverrideScope.UNTIL_CLEARED)


class ControlActionIntegration(unittest.TestCase):
    def test_grant_floor_action_appends_add_one_lease_override(self):
        coord = _coord()
        _grant(coord, "loom", CapabilityName.GRANT_FLOOR)
        result = coord.propose_control_action(
            "loom", "GRANT_FLOOR", {"speakers": ["claude_code"]}
        )
        self.assertTrue(result.granted)
        overrides = coord.state.control.active_overrides
        self.assertEqual(overrides[-1].mode, FloorOverrideMode.ADD)
        self.assertEqual(overrides[-1].scope, FloorOverrideScope.ONE_LEASE)
        self.assertEqual(overrides[-1].speakers, ("claude_code",))

    def test_block_floor_action_appends_block_current_turn_override(self):
        coord = _coord()
        _grant(coord, "loom", CapabilityName.UPDATE_ALLOWED_SPEAKERS)
        result = coord.propose_control_action(
            "loom", "BLOCK_FLOOR", {"speakers": ["gemini"]}
        )
        self.assertTrue(result.granted)
        overrides = coord.state.control.active_overrides
        self.assertEqual(overrides[-1].mode, FloorOverrideMode.BLOCK)
        self.assertEqual(overrides[-1].scope, FloorOverrideScope.CURRENT_TURN)

    def test_override_allowed_speakers_action_appends_replace_until_cleared(self):
        coord = _coord()
        _grant(coord, "loom", CapabilityName.UPDATE_ALLOWED_SPEAKERS)
        result = coord.propose_control_action(
            "loom", "OVERRIDE_ALLOWED_SPEAKERS", {"speakers": ["claude_code"]}
        )
        self.assertTrue(result.granted)
        overrides = coord.state.control.active_overrides
        self.assertEqual(overrides[-1].mode, FloorOverrideMode.REPLACE)
        self.assertEqual(overrides[-1].scope, FloorOverrideScope.UNTIL_CLEARED)


if __name__ == "__main__":
    unittest.main()
