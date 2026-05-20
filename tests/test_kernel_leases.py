"""Tests for ``loom.kernel.leases`` — v0.3 PR 7 unified Lease abstraction.

Doctrine: P8 (one Lease abstraction with kind discriminator),
§3 (lease abstraction).

Test classes:

- :class:`LeaseShape` — LeaseKind, LeaseContext tagged union, Lease
  invariant (kind matches context type).
- :class:`AppliesToFiltering` — every default check declares
  ``applies_to``; lookup helper returns the expected sets.
- :class:`AcquireTypedLeaseUserTurn` — kind=USER_TURN goes through
  the v0.2 path; existing tests should still pass.
- :class:`AcquireTypedLeaseReactive` — kind=REACTIVE skips USER_TURN-only
  checks (open turn, allowed speaker, throttle, budget, etc.).
- :class:`AcquireTypedLeaseControlAction` — kind=CONTROL_ACTION
  exercises the capability check; granting the capability flips the
  deny→grant outcome.
"""

from __future__ import annotations

import time
import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.capabilities import CapabilityName
from loom.kernel.coordinator import (
    DEFAULT_LEASE_CHECKS,
    RoomCoordinator,
    _CapabilityCheck,
)
from loom.kernel.effects import CapabilityGrantedEffect
from loom.kernel.leases import (
    ALL_LEASE_KINDS,
    ControlActionContext,
    Lease,
    LeaseKind,
    ReactiveContext,
    ToolInvocationContext,
    UserTurnContext,
    WorkflowStepContext,
    check_applies_to,
)
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState


def _open_coord(participant_id: str = "loom") -> RoomCoordinator:
    bus = MessageBus()
    state = RoomState(config=RoomConfig())
    state.add_participant(ParticipantInfo(id=participant_id))
    return RoomCoordinator(bus, state)


class LeaseShape(unittest.TestCase):
    def test_lease_kind_has_six_members(self):
        # v0.3.x PR 5 added SUMMARIZATION.
        self.assertEqual(len(list(LeaseKind)), 6)

    def test_lease_kind_to_string(self):
        self.assertEqual(LeaseKind.USER_TURN.value, "user_turn")
        self.assertEqual(LeaseKind.CONTROL_ACTION.value, "control_action")

    def test_context_dataclasses_are_frozen(self):
        ctx = UserTurnContext(user_turn_id=1, trigger_event_id=0, room_epoch=0)
        with self.assertRaises(Exception):
            ctx.user_turn_id = 99  # type: ignore[misc]

    def test_lease_post_init_enforces_kind_context_match(self):
        # USER_TURN with ReactiveContext → TypeError.
        with self.assertRaises(TypeError):
            Lease(
                id=1,
                kind=LeaseKind.USER_TURN,
                holder="loom",
                context=ReactiveContext(reason="x"),  # mismatched
                acquired_at=0.0,
                expires_at=1.0,
            )

    def test_lease_post_init_accepts_matching_kind_context(self):
        # All five (kind, context) pairs construct cleanly.
        pairs = [
            (LeaseKind.USER_TURN, UserTurnContext(user_turn_id=1, trigger_event_id=0, room_epoch=0)),
            (LeaseKind.CONTROL_ACTION, ControlActionContext(action_name="SET_TOPIC")),
            (LeaseKind.TOOL_INVOCATION, ToolInvocationContext(tool_name="x")),
            (LeaseKind.WORKFLOW_STEP, WorkflowStepContext(workflow_id="w", step_id="s")),
            (LeaseKind.REACTIVE, ReactiveContext(reason="dead_letter")),
        ]
        for kind, ctx in pairs:
            Lease(id=1, kind=kind, holder="loom", context=ctx,
                  acquired_at=0.0, expires_at=1.0)


class AppliesToFiltering(unittest.TestCase):
    def test_check_applies_to_lookup_handles_missing_attr(self):
        class _CustomCheck:
            name = "custom"

            def check(self, **_kw):
                from loom.contracts import PASSED
                return PASSED

        # No applies_to attribute → defaults to ALL_LEASE_KINDS.
        self.assertEqual(check_applies_to(_CustomCheck()), ALL_LEASE_KINDS)

    def test_default_checks_have_applies_to(self):
        # Every default check has applies_to (either {USER_TURN} or
        # ALL_LEASE_KINDS or some subset).
        for chk in DEFAULT_LEASE_CHECKS:
            applies = check_applies_to(chk)
            self.assertIsInstance(applies, frozenset)
            self.assertTrue(len(applies) >= 1)

    def test_user_turn_only_checks(self):
        names = {
            chk.name for chk in DEFAULT_LEASE_CHECKS
            if check_applies_to(chk) == frozenset({LeaseKind.USER_TURN})
        }
        # The v0.2 chat-specific checks.
        self.assertIn("open_turn", names)
        self.assertIn("allowed_speaker", names)
        self.assertIn("per_participant_cap", names)
        self.assertIn("max_responses", names)
        self.assertIn("throttle", names)
        self.assertIn("budget", names)

    def test_universal_checks(self):
        names = {
            chk.name for chk in DEFAULT_LEASE_CHECKS
            if check_applies_to(chk) == ALL_LEASE_KINDS
        }
        # Participant registration / activity gates apply to every kind.
        self.assertIn("participant_registered", names)
        self.assertIn("participant_active", names)

    def test_capability_check_applies_to_control_and_summarization(self):
        # v0.3.x PR 5: SUMMARIZATION leases share the capability gate.
        cap_check = next(
            chk for chk in DEFAULT_LEASE_CHECKS if chk.name == "capability"
        )
        self.assertEqual(
            check_applies_to(cap_check),
            frozenset({LeaseKind.CONTROL_ACTION, LeaseKind.SUMMARIZATION}),
        )


class AcquireTypedLeaseReactive(unittest.TestCase):
    """REACTIVE leases skip the USER_TURN-only checks."""

    def test_reactive_lease_granted_with_no_open_user_turn(self):
        coord = _open_coord()
        # No UserTurn open; the USER_TURN _OpenTurnCheck would reject a
        # USER_TURN request — but REACTIVE skips it.
        ctx = ReactiveContext(reason="dead_letter_reroute")
        lease = coord.acquire_typed_lease(LeaseKind.REACTIVE, "loom", ctx)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.kind, LeaseKind.REACTIVE)
        self.assertEqual(lease.holder, "loom")
        self.assertIs(lease.context, ctx)

    def test_reactive_lease_requires_registered_participant(self):
        coord = _open_coord()
        ctx = ReactiveContext(reason="x")
        lease = coord.acquire_typed_lease(LeaseKind.REACTIVE, "ghost", ctx)
        self.assertIsNone(lease)
        denials = [
            x for x in coord.bus.snapshot()
            if ev.control_type_of(x) == "lease_denied"
        ]
        self.assertEqual(len(denials), 1)
        self.assertEqual(denials[0].body["deny_reason"], "unknown_participant")

    def test_reactive_lease_carries_trace_span(self):
        coord = _open_coord()
        ctx = ReactiveContext(reason="x")
        lease = coord.acquire_typed_lease(LeaseKind.REACTIVE, "loom", ctx)
        self.assertIsNotNone(lease.trace_span_id)


class AcquireTypedLeaseControlAction(unittest.TestCase):
    """CONTROL_ACTION lease requires the holder's capability."""

    def test_control_action_denied_without_capability(self):
        coord = _open_coord()
        ctx = ControlActionContext(action_name="SET_TOPIC")
        lease = coord.acquire_typed_lease(LeaseKind.CONTROL_ACTION, "loom", ctx)
        self.assertIsNone(lease)
        denials = [
            x for x in coord.bus.snapshot()
            if ev.control_type_of(x) == "lease_denied"
        ]
        self.assertEqual(len(denials), 1)
        self.assertEqual(denials[0].body["deny_reason"], "insufficient_capability")
        self.assertEqual(denials[0].body["check_name"], "capability")

    def test_control_action_granted_after_capability_grant(self):
        coord = _open_coord()
        # Grant SET_TOPIC via the registry first.
        with coord._lock:
            coord._apply_effect(
                CapabilityGrantedEffect(
                    grant_id="g1",
                    grantee_id="loom",
                    capability=CapabilityName.SET_TOPIC.value,
                    grantor_id="user",
                )
            )
        ctx = ControlActionContext(action_name="SET_TOPIC")
        lease = coord.acquire_typed_lease(LeaseKind.CONTROL_ACTION, "loom", ctx)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.kind, LeaseKind.CONTROL_ACTION)

    def test_user_bypasses_capability_check(self):
        coord = _open_coord()
        # User must still be a registered participant for the universal
        # check; register them.
        coord.register_participant(ParticipantInfo(id="user"))
        ctx = ControlActionContext(action_name="SET_TOPIC")
        lease = coord.acquire_typed_lease(LeaseKind.CONTROL_ACTION, "user", ctx)
        self.assertIsNotNone(lease)

    def test_unknown_action_name_denied(self):
        coord = _open_coord()
        ctx = ControlActionContext(action_name="NOT_A_REAL_ACTION")
        lease = coord.acquire_typed_lease(LeaseKind.CONTROL_ACTION, "loom", ctx)
        self.assertIsNone(lease)
        denials = [
            x for x in coord.bus.snapshot()
            if ev.control_type_of(x) == "lease_denied"
        ]
        self.assertEqual(denials[0].body["deny_reason"], "unknown_control_action")


class AcquireTypedLeaseUserTurn(unittest.TestCase):
    """USER_TURN delegates to the v0.2 path."""

    def _coord_with_open_turn(self) -> RoomCoordinator:
        coord = _open_coord()
        # Open a turn so the OpenTurnCheck passes. Use the same helper
        # pattern existing coordinator tests use (plan_for_default
        # against the participant + open_user_turn directly).
        from loom.kernel.obligations import plan_for_default
        user_event = ev.chat(sender="user", body="hello")
        coord.bus.post(user_event)
        plan = plan_for_default("loom", reason="test", target_event_ids=[user_event.id])
        coord.open_user_turn(user_event, plan)
        return coord

    def test_user_turn_typed_lease_grants_under_open_turn(self):
        coord = self._coord_with_open_turn()
        ut = coord.user_turn
        self.assertIsNotNone(ut)
        ctx = UserTurnContext(
            user_turn_id=ut.id,
            trigger_event_id=0,
            room_epoch=coord.state.room_epoch,
        )
        lease = coord.acquire_typed_lease(LeaseKind.USER_TURN, "loom", ctx)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.kind, LeaseKind.USER_TURN)
        self.assertEqual(lease.holder, "loom")

    def test_user_turn_typed_requires_matching_context_type(self):
        coord = _open_coord()
        with self.assertRaises(TypeError):
            coord.acquire_typed_lease(
                LeaseKind.USER_TURN,
                "loom",
                ReactiveContext(reason="x"),  # wrong context
            )


if __name__ == "__main__":
    unittest.main()
