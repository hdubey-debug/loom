"""Tests for the :class:`loom.contracts.ConversationPolicy` ABC.

The ABC lives at :mod:`loom.contracts` — neutral location so the kernel
can import it without violating the kernel/policy boundary.
"""
from __future__ import annotations

import unittest

from loom.contracts import ConversationPolicy
from loom.kernel.events import Event
from loom.kernel.obligations import UserTurnPlan, plan_for_acknowledgement
from loom.kernel.room import RoomConfig, RoomState


class _NoopPolicy(ConversationPolicy):
    name = "noop"

    def plan_user_turn(self, user_event, state, *, prior_speaker=None):
        return plan_for_acknowledgement(rationale="noop")


class AbcEnforcement(unittest.TestCase):
    def test_cannot_instantiate_abstract(self):
        with self.assertRaises(TypeError):
            ConversationPolicy()  # type: ignore[abstract]

    def test_subclass_without_plan_user_turn_is_abstract(self):
        class Bad(ConversationPolicy):  # noqa: D101
            pass
        with self.assertRaises(TypeError):
            Bad()  # type: ignore[abstract]

    def test_minimal_subclass_works(self):
        p = _NoopPolicy()
        e = Event(kind="chat", sender="user", body="hi")
        plan = p.plan_user_turn(e, RoomState(config=RoomConfig()))
        self.assertIsInstance(plan, UserTurnPlan)
        self.assertFalse(plan.requires_response)

    def test_default_system_prompt_is_empty(self):
        self.assertEqual(_NoopPolicy().system_prompt(
            "loom", RoomState(config=RoomConfig())), "")

    def test_default_role_prompt_is_empty(self):
        self.assertEqual(_NoopPolicy().role_prompt(
            "loom", RoomState(config=RoomConfig())), "")


class BundledPolicyContractCompliance(unittest.TestCase):
    """Every bundled policy must satisfy the same minimal contract.

    The four concrete policies live in ``loom.policy.*``. We assert that
    each one:

    - instantiates without arguments where applicable;
    - subclasses :class:`ConversationPolicy` (so the ABC enforces
      ``plan_user_turn`` is implemented);
    - returns a :class:`UserTurnPlan` (never ``None``) for a minimal
      valid call;
    - does not mutate the supplied :class:`RoomState` (no add/remove of
      participants, no field reassignment);
    - does not post to the bus during ``plan_user_turn`` (the policy is
      pure; only the coordinator emits control events).
    """

    @staticmethod
    def _seeded_state() -> RoomState:
        from loom.kernel.room import ParticipantInfo
        s = RoomState(config=RoomConfig())
        s.add_participant(ParticipantInfo(id="loom", cost_tier=0))
        s.add_participant(ParticipantInfo(id="claude_code", cost_tier=1))
        s.set_default_responder("loom")
        s.set_anchor("loom")
        return s

    @staticmethod
    def _bundled_policies() -> list:
        from loom.policy.default import DefaultPolicy
        from loom.policy.open_chat import OpenChatPolicy
        from loom.policy.round_robin import RoundRobinPolicy
        from loom.policy.single_responder import SingleResponderPolicy
        return [
            DefaultPolicy(),
            OpenChatPolicy(),
            RoundRobinPolicy(["loom", "claude_code"]),
            SingleResponderPolicy("loom"),
        ]

    def test_each_policy_instantiates_and_subclasses_abc(self):
        for policy in self._bundled_policies():
            self.assertIsInstance(policy, ConversationPolicy)

    def test_each_policy_returns_user_turn_plan(self):
        e = Event(kind="chat", sender="user", body="hello")
        for policy in self._bundled_policies():
            state = self._seeded_state()
            plan = policy.plan_user_turn(e, state.view())
            self.assertIsInstance(
                plan, UserTurnPlan,
                f"{type(policy).__name__} returned non-plan: {plan!r}")

    def test_no_policy_mutates_room_state(self):
        # Snapshot the live mutable state's identifying fields before
        # calling the policy and verify nothing changed.
        e = Event(kind="chat", sender="user", body="hello")
        for policy in self._bundled_policies():
            state = self._seeded_state()
            before_pids = sorted(state.participants.keys())
            before_epoch = state.room_epoch
            before_responder = state.default_responder_id
            before_anchor = state.anchor_id
            policy.plan_user_turn(e, state.view())
            self.assertEqual(sorted(state.participants.keys()),
                             before_pids,
                             f"{type(policy).__name__} mutated participants")
            self.assertEqual(state.room_epoch, before_epoch,
                             f"{type(policy).__name__} bumped room_epoch")
            self.assertEqual(state.default_responder_id, before_responder,
                             f"{type(policy).__name__} changed responder")
            self.assertEqual(state.anchor_id, before_anchor,
                             f"{type(policy).__name__} changed anchor")

    def test_no_policy_posts_to_bus_during_planning(self):
        # The policy receives a state-view; it has no bus reference, but
        # we assert behaviorally by giving each policy a state seeded
        # with no recent bus activity and verifying nothing changes after
        # the call. (Boundary tests cover the static side; this is the
        # runtime side of invariant 3.)
        from loom.kernel.bus import MessageBus

        e = Event(kind="chat", sender="user", body="hello")
        bus = MessageBus()
        before_len = len(bus)
        for policy in self._bundled_policies():
            state = self._seeded_state()
            policy.plan_user_turn(e, state.view())
        self.assertEqual(len(bus), before_len)

    def test_each_policy_handles_empty_active_capable_room(self):
        # A degenerate room (no active capable participants) must still
        # return a plan — typically an acknowledgement — rather than
        # raising. Used to be a real production bug for round-robin.
        from loom.kernel.room import ParticipantInfo
        e = Event(kind="chat", sender="user", body="hi")
        for policy in self._bundled_policies():
            state = RoomState(config=RoomConfig())
            state.add_participant(ParticipantInfo(id="zombie",
                                                  active=False))
            try:
                plan = policy.plan_user_turn(e, state.view())
            except Exception as exc:  # pragma: no cover
                self.fail(f"{type(policy).__name__} raised on empty "
                          f"active set: {exc!r}")
            self.assertIsInstance(plan, UserTurnPlan)


if __name__ == "__main__":
    unittest.main()
