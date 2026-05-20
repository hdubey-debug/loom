"""Tests for ``loom.kernel.control_actions`` — v0.3 PR 9 dispatch.

Doctrine: P3 (extended), P13 (extended), P14 (custom actions return
built-in effects only), §7 (control action spec).

Test classes:

- :class:`ActionRegistry` — kernel built-ins + custom registration +
  name collision.
- :class:`ProposalLifecycle` — propose_control_action end-to-end for
  each kernel action; verifies `_proposed` / `_applied` events fire
  and the state mutation happened.
- :class:`DenialPath` — each :class:`DenialReason` exercised:
  UNKNOWN_ACTION, INVALID_PARAMS, INSUFFICIENT_CAPABILITY,
  CHECK_RAISED.
- :class:`CustomAction` — custom action returning a built-in effect
  passes; one that returns an unregistered effect type triggers
  CHECK_RAISED.
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.capabilities import CapabilityName
from loom.kernel.control_actions import (
    ControlAction,
    ControlActionRegistry,
    DenialReason,
    KERNEL_BUILTIN_ACTIONS,
    SetTopicAction,
    build_kernel_action_registry,
)
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.effects import (
    CapabilityGrantedEffect,
    ControlEffect,
    TopicChangedEffect,
)
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState


def _coord(participant_id: str = "loom") -> RoomCoordinator:
    bus = MessageBus()
    state = RoomState(config=RoomConfig())
    state.add_participant(ParticipantInfo(id=participant_id))
    return RoomCoordinator(bus, state)


def _grant_capability(coord: RoomCoordinator, grantee: str, cap: CapabilityName) -> None:
    with coord._lock:
        coord._apply_effect(
            CapabilityGrantedEffect(
                grant_id=f"g-{grantee}-{cap.value}",
                grantee_id=grantee,
                capability=cap.value,
                grantor_id="user",
            )
        )


class ActionRegistry(unittest.TestCase):
    def test_kernel_builtin_set_includes_doctrine_actions(self):
        reg = build_kernel_action_registry()
        names = reg.names()
        for n in ("SET_TOPIC", "SET_ANCHOR", "SET_DEFAULT_RESPONDER",
                  "SET_ROLES", "SET_STYLE"):
            self.assertIn(n, names)

    def test_get_returns_registered_action(self):
        reg = build_kernel_action_registry()
        action = reg.get("SET_TOPIC")
        self.assertIsNotNone(action)
        self.assertEqual(action.required_capability, CapabilityName.SET_TOPIC)

    def test_get_returns_none_for_unknown(self):
        reg = build_kernel_action_registry()
        self.assertIsNone(reg.get("NOPE"))

    def test_custom_action_registration(self):
        class _CustomNoop:
            name = "NOOP"
            required_capability = CapabilityName.SET_TOPIC

            def validate_params(self, params):
                return True, None

            def propose_effect(self, params, state_view):
                return ()

        reg = build_kernel_action_registry(customs=(_CustomNoop(),))
        self.assertIn("NOOP", reg.names())

    def test_name_collision_raises(self):
        with self.assertRaises(ValueError):
            build_kernel_action_registry(customs=(SetTopicAction(),))


class ProposalLifecycle(unittest.TestCase):
    def test_set_topic_full_lifecycle(self):
        coord = _coord()
        _grant_capability(coord, "loom", CapabilityName.SET_TOPIC)
        result = coord.propose_control_action(
            proposer_id="loom",
            action_name="SET_TOPIC",
            params={"topic": "derivatives lesson"},
        )
        self.assertTrue(result.granted)
        self.assertEqual(coord.state.topic, "derivatives lesson")
        # Proposed + applied events both fire.
        proposed = [x for x in coord.bus.snapshot()
                    if ev.control_type_of(x) == "control_action_proposed"]
        applied = [x for x in coord.bus.snapshot()
                   if ev.control_type_of(x) == "control_action_applied"]
        self.assertEqual(len(proposed), 1)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].body["action_name"], "SET_TOPIC")
        self.assertEqual(applied[0].body["effects"][0]["effect_type"], "topic_changed")

    def test_set_anchor_full_lifecycle(self):
        coord = _coord()
        coord.register_participant(ParticipantInfo(id="anchor_target"))
        _grant_capability(coord, "loom", CapabilityName.SET_ANCHOR)
        result = coord.propose_control_action(
            "loom", "SET_ANCHOR", {"participant_id": "anchor_target"}
        )
        self.assertTrue(result.granted)
        self.assertEqual(coord.state.anchor_id, "anchor_target")

    def test_set_default_responder_full_lifecycle(self):
        coord = _coord()
        coord.register_participant(ParticipantInfo(id="bob"))
        _grant_capability(coord, "loom", CapabilityName.SET_DEFAULT_RESPONDER)
        result = coord.propose_control_action(
            "loom", "SET_DEFAULT_RESPONDER", {"participant_id": "bob"}
        )
        self.assertTrue(result.granted)
        self.assertEqual(coord.state.default_responder_id, "bob")

    def test_set_roles_full_lifecycle(self):
        coord = _coord()
        _grant_capability(coord, "loom", CapabilityName.SET_ROLES)
        result = coord.propose_control_action(
            "loom", "SET_ROLES",
            {"roles": {"loom": "teacher"}},
        )
        self.assertTrue(result.granted)
        self.assertEqual(coord.state.control.roles, {"loom": "teacher"})

    def test_set_style_full_lifecycle(self):
        coord = _coord()
        _grant_capability(coord, "loom", CapabilityName.SET_TOPIC)  # proxy
        result = coord.propose_control_action(
            "loom", "SET_STYLE", {"style": "brief"}
        )
        self.assertTrue(result.granted)
        self.assertEqual(coord.state.control.style, "brief")

    def test_lease_closed_emitted_after_action_applies(self):
        coord = _coord()
        _grant_capability(coord, "loom", CapabilityName.SET_TOPIC)
        coord.propose_control_action("loom", "SET_TOPIC", {"topic": "x"})
        closed = [x for x in coord.bus.snapshot()
                  if ev.control_type_of(x) == "lease_closed"]
        # One lease_closed for the CONTROL_ACTION lease released
        # after apply.
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].body["kind"], "control_action")
        self.assertEqual(closed[0].body["reason"], "released")


class DenialPath(unittest.TestCase):
    def test_unknown_action_denied(self):
        coord = _coord()
        result = coord.propose_control_action("loom", "NOT_REAL", {})
        self.assertFalse(result.granted)
        self.assertEqual(result.reason, DenialReason.UNKNOWN_ACTION)
        denied = [x for x in coord.bus.snapshot()
                  if ev.control_type_of(x) == "control_action_denied"]
        self.assertEqual(denied[0].body["reason"], "UNKNOWN_ACTION")

    def test_invalid_params_denied(self):
        coord = _coord()
        _grant_capability(coord, "loom", CapabilityName.SET_TOPIC)
        result = coord.propose_control_action(
            "loom", "SET_TOPIC", {"topic": 123}  # not a str
        )
        self.assertFalse(result.granted)
        self.assertEqual(result.reason, DenialReason.INVALID_PARAMS)

    def test_insufficient_capability_denied(self):
        coord = _coord()
        # No capability granted.
        result = coord.propose_control_action(
            "loom", "SET_TOPIC", {"topic": "x"}
        )
        self.assertFalse(result.granted)
        self.assertEqual(result.reason, DenialReason.INSUFFICIENT_CAPABILITY)

    def test_check_raised_via_propose_effect(self):
        # Custom action whose propose_effect throws.
        class _Boom:
            name = "BOOM"
            required_capability = CapabilityName.SET_TOPIC

            def validate_params(self, params):
                return True, None

            def propose_effect(self, params, state_view):
                raise RuntimeError("explode")

        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="loom"))
        # Inject the custom action via RoomConfig.custom_control_actions
        # (which doesn't exist yet — verify the coordinator tolerates
        # its absence by passing via test-only registry mutation).
        coord = RoomCoordinator(bus, state)
        coord._action_registry.register(_Boom())
        _grant_capability(coord, "loom", CapabilityName.SET_TOPIC)
        result = coord.propose_control_action("loom", "BOOM", {})
        self.assertFalse(result.granted)
        self.assertEqual(result.reason, DenialReason.CHECK_RAISED)


class CustomAction(unittest.TestCase):
    def test_custom_action_returning_builtin_effect_passes(self):
        # P14: custom action returning a built-in TopicChangedEffect.
        class _CustomTopic:
            name = "CUSTOM_TOPIC"
            required_capability = CapabilityName.SET_TOPIC

            def validate_params(self, params):
                return True, None

            def propose_effect(self, params, state_view):
                return (TopicChangedEffect(topic="custom"),)

        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="loom"))
        coord = RoomCoordinator(bus, state)
        coord._action_registry.register(_CustomTopic())
        _grant_capability(coord, "loom", CapabilityName.SET_TOPIC)
        result = coord.propose_control_action("loom", "CUSTOM_TOPIC", {})
        self.assertTrue(result.granted)
        self.assertEqual(coord.state.topic, "custom")

    def test_custom_action_returning_unregistered_effect_is_denied(self):
        # P14 enforced via registry: an unknown effect_type raises
        # UnknownEffect which the coordinator surfaces as CHECK_RAISED.
        class _ExoticEffect(ControlEffect):
            effect_type: str = "exotic_custom"  # type: ignore[assignment]

        class _UsesExotic:
            name = "EXOTIC"
            required_capability = CapabilityName.SET_TOPIC

            def validate_params(self, params):
                return True, None

            def propose_effect(self, params, state_view):
                return (_ExoticEffect(),)

        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="loom"))
        coord = RoomCoordinator(bus, state)
        coord._action_registry.register(_UsesExotic())
        _grant_capability(coord, "loom", CapabilityName.SET_TOPIC)
        result = coord.propose_control_action("loom", "EXOTIC", {})
        self.assertFalse(result.granted)
        self.assertEqual(result.reason, DenialReason.CHECK_RAISED)


if __name__ == "__main__":
    unittest.main()
