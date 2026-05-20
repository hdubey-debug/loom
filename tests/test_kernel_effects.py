"""Tests for ``loom.kernel.effects`` — v0.3 PR 3 effect vocabulary + registry.

Doctrine: P6 (event-sourced replay applies committed effects), P7
(applied events record versioned ``ControlEffect`` instances), §5
(effect vocabulary + registry). Closes v0.2.1 audit deferral C4.

Three test classes:

- :class:`EffectRegistryBasics` covers registry mechanics: register,
  lookup, collision, decorator, unknown raises.
- :class:`ReducerBehavior` covers each v0.2-backable reducer end to
  end against a fresh KernelState.
- :class:`CoordinatorRoutesThroughRegistry` covers the v0.2 slot
  setters after PR 3's refactor — they still emit their legacy events
  but now route the state mutation through ``_apply_effect``, so
  ``KernelState.version`` bumps in lockstep.
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.effects import (
    AnchorAssignedEffect,
    BudgetCommittedEffect,
    BudgetRefundedEffect,
    BudgetReservedEffect,
    CapabilityExpiredEffect,
    CapabilityGrantedEffect,
    CapabilityRevokedEffect,
    ChairAssignedEffect,
    ControlEffect,
    DefaultResponderSetEffect,
    DefaultSummarizerSetEffect,
    EffectRegistry,
    FloorOverrideEffect,
    LeaseCancelledEffect,
    PolicySwitchedEffect,
    RolesAssignedEffect,
    StyleChangedEffect,
    TopicChangedEffect,
    UnknownEffect,
    build_kernel_registry,
)
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState
from loom.kernel.state import KernelState, new_kernel_state


def _fresh_state() -> KernelState:
    s = RoomState(config=RoomConfig())
    s.add_participant(ParticipantInfo(id="loom", cost_tier=0))
    s.add_participant(ParticipantInfo(id="claude_code", cost_tier=2))
    return new_kernel_state(s)


def _fresh_coord() -> RoomCoordinator:
    bus = MessageBus()
    state = RoomState(config=RoomConfig())
    state.add_participant(ParticipantInfo(id="loom"))
    state.add_participant(ParticipantInfo(id="claude_code"))
    return RoomCoordinator(bus, state)


class EffectRegistryBasics(unittest.TestCase):
    def test_register_and_get(self):
        reg = EffectRegistry()
        marker = {"called": False}

        def reducer(state, effect):
            marker["called"] = True

        reg.register("topic_changed", 1, reducer)
        self.assertTrue(reg.has("topic_changed", 1))
        self.assertIs(reg.get("topic_changed", 1), reducer)

    def test_unknown_lookup_raises_unknown_effect(self):
        reg = EffectRegistry()
        with self.assertRaises(UnknownEffect):
            reg.get("does_not_exist", 1)

    def test_apply_routes_through_registered_reducer(self):
        reg = EffectRegistry()
        seen: list[tuple[KernelState, ControlEffect]] = []
        reg.register(
            "topic_changed", 1, lambda s, e: seen.append((s, e))
        )
        state = _fresh_state()
        effect = TopicChangedEffect(topic="hello")
        reg.apply(state, effect)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][1], effect)

    def test_apply_unknown_raises_unknown_effect(self):
        reg = EffectRegistry()
        state = _fresh_state()
        with self.assertRaises(UnknownEffect):
            reg.apply(state, TopicChangedEffect(topic="x"))

    def test_register_collision_raises_value_error(self):
        reg = EffectRegistry()
        reg.register("topic_changed", 1, lambda s, e: None)
        with self.assertRaises(ValueError):
            reg.register("topic_changed", 1, lambda s, e: None)

    def test_register_different_versions_coexist(self):
        reg = EffectRegistry()
        v1 = lambda s, e: None
        v2 = lambda s, e: None
        reg.register("topic_changed", 1, v1)
        reg.register("topic_changed", 2, v2)
        self.assertIs(reg.get("topic_changed", 1), v1)
        self.assertIs(reg.get("topic_changed", 2), v2)

    def test_decorator_form_registers(self):
        reg = EffectRegistry()

        @reg.register_reducer("topic_changed", 1)
        def my_reducer(state, effect):
            return None

        self.assertTrue(reg.has("topic_changed", 1))

    def test_kernel_registry_has_seven_v02_backed_reducers(self):
        reg = build_kernel_registry()
        for et in (
            "topic_changed",
            "anchor_assigned",
            "chair_assigned",
            "default_responder_set",
            "default_summarizer_set",
            "roles_assigned",
            "style_changed",
        ):
            self.assertTrue(reg.has(et, 1), f"missing reducer for {et!r}")

    def test_kernel_registry_missing_pr5_pr6_effects_until_their_pr_lands(self):
        # PR 3 declares the shape but does not register reducers for
        # capability / budget / floor / lease / policy effects — their
        # owning PRs (5, 6, 8, 9, 10) extend the registry on load.
        reg = build_kernel_registry()
        for et in (
            "floor_override",
            "lease_cancelled",
            "capability_granted",
            "capability_revoked",
            "capability_expired",
            "policy_switched",
            "budget_reserved",
            "budget_committed",
            "budget_refunded",
        ):
            self.assertFalse(reg.has(et, 1), f"unexpected reducer for {et!r}")

    def test_effect_subclasses_have_distinct_types(self):
        # The 13 doctrine effects each carry a distinct effect_type
        # string used as the registry key.
        effects = [
            FloorOverrideEffect(),
            TopicChangedEffect(),
            AnchorAssignedEffect(),
            ChairAssignedEffect(),
            DefaultResponderSetEffect(),
            DefaultSummarizerSetEffect(),
            RolesAssignedEffect(),
            StyleChangedEffect(),
            LeaseCancelledEffect(),
            CapabilityGrantedEffect(),
            CapabilityRevokedEffect(),
            CapabilityExpiredEffect(),
            PolicySwitchedEffect(),
            BudgetReservedEffect(),
            BudgetCommittedEffect(),
            BudgetRefundedEffect(),
        ]
        types = [e.effect_type for e in effects]
        self.assertEqual(len(types), len(set(types)))

    def test_applied_at_event_id_default_is_none(self):
        e = TopicChangedEffect(topic="x")
        self.assertIsNone(e.applied_at_event_id)
        e.applied_at_event_id = 42
        self.assertEqual(e.applied_at_event_id, 42)


class ReducerBehavior(unittest.TestCase):
    """Each v0.2-backable reducer produces the expected state delta."""

    def _apply(self, effect: ControlEffect) -> KernelState:
        state = _fresh_state()
        reg = build_kernel_registry()
        reg.apply(state, effect)
        return state

    def test_topic_changed_updates_topic(self):
        state = self._apply(TopicChangedEffect(topic="derivatives lesson"))
        self.assertEqual(state.room.topic, "derivatives lesson")

    def test_topic_changed_to_none_clears_topic(self):
        state = self._apply(TopicChangedEffect(topic=None))
        self.assertIsNone(state.room.topic)

    def test_anchor_assigned_updates_anchor(self):
        state = self._apply(AnchorAssignedEffect(anchor_id="loom"))
        self.assertEqual(state.room.anchor_id, "loom")

    def test_chair_assigned_updates_chair(self):
        state = self._apply(ChairAssignedEffect(chair_id="claude_code"))
        self.assertEqual(state.room.chair_id, "claude_code")

    def test_default_responder_set_updates_slot(self):
        state = self._apply(DefaultResponderSetEffect(participant_id="claude_code"))
        self.assertEqual(state.room.default_responder_id, "claude_code")

    def test_default_summarizer_set_updates_slot(self):
        state = self._apply(DefaultSummarizerSetEffect(participant_id="loom"))
        self.assertEqual(state.room.default_summarizer_id, "loom")

    def test_roles_assigned_replaces_role_map(self):
        state = self._apply(
            RolesAssignedEffect(roles={"loom": "teacher", "claude_code": "quizzer"})
        )
        self.assertEqual(
            state.room.control.roles,
            {"loom": "teacher", "claude_code": "quizzer"},
        )

    def test_roles_assigned_filters_unknown_participants(self):
        # state.room.set_roles silently drops ids not in participants.
        state = self._apply(
            RolesAssignedEffect(
                roles={"loom": "teacher", "ghost": "phantom"},
            )
        )
        self.assertEqual(state.room.control.roles, {"loom": "teacher"})

    def test_style_changed_updates_style(self):
        state = self._apply(StyleChangedEffect(style="brief"))
        self.assertEqual(state.room.control.style, "brief")

    def test_style_changed_unknown_raises_value_error(self):
        with self.assertRaises(ValueError):
            self._apply(StyleChangedEffect(style="grandiose"))

    def test_reducer_does_not_mutate_kernel_version(self):
        # The registry leaves version-bumping to the caller (coordinator).
        state = _fresh_state()
        reg = build_kernel_registry()
        before = state.version
        reg.apply(state, TopicChangedEffect(topic="x"))
        self.assertEqual(state.version, before)


class CoordinatorRoutesThroughRegistry(unittest.TestCase):
    """v0.2 slot setters now route through the registry post-PR 3."""

    def test_set_topic_emits_event_and_bumps_version(self):
        coord = _fresh_coord()
        before = coord.kernel_state.version
        coord.set_topic("hello")
        self.assertEqual(coord.state.topic, "hello")
        self.assertGreater(coord.kernel_state.version, before)
        topics = [
            x for x in coord.bus.snapshot() if ev.control_type_of(x) == "topic_changed"
        ]
        self.assertEqual(len(topics), 1)

    def test_set_anchor_routes_through_registry(self):
        coord = _fresh_coord()
        coord.register_participant(ParticipantInfo(id="alpha"))
        before = coord.kernel_state.version
        coord.set_anchor("alpha")
        self.assertEqual(coord.state.anchor_id, "alpha")
        self.assertGreater(coord.kernel_state.version, before)

    def test_set_chair_routes_through_registry(self):
        coord = _fresh_coord()
        coord.set_chair("loom")
        self.assertEqual(coord.state.chair_id, "loom")

    def test_set_default_summarizer_routes_through_registry(self):
        coord = _fresh_coord()
        coord.set_default_summarizer("loom")
        self.assertEqual(coord.state.default_summarizer_id, "loom")

    def test_set_roles_routes_through_registry(self):
        coord = _fresh_coord()
        coord.set_roles({"loom": "teacher"})
        self.assertEqual(coord.state.control.roles, {"loom": "teacher"})

    def test_set_style_routes_through_registry(self):
        coord = _fresh_coord()
        coord.set_style("brief")
        self.assertEqual(coord.state.control.style, "brief")

    def test_noop_topic_does_not_bump_version_or_emit(self):
        coord = _fresh_coord()
        coord.set_topic("hello")
        v_after_first = coord.kernel_state.version
        coord.set_topic("hello")  # same value — should short-circuit
        self.assertEqual(coord.kernel_state.version, v_after_first)
        topics = [
            x for x in coord.bus.snapshot() if ev.control_type_of(x) == "topic_changed"
        ]
        self.assertEqual(len(topics), 1)

    def test_apply_effect_raises_for_unregistered_effect_type(self):
        coord = _fresh_coord()

        class _Synthetic(ControlEffect):
            effect_type: str = "does_not_exist"  # type: ignore[assignment]

        with coord._lock:
            with self.assertRaises(UnknownEffect):
                coord._apply_effect(_Synthetic())


if __name__ == "__main__":
    unittest.main()
