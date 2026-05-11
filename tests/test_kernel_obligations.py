"""Tests for ``loom.kernel.obligations`` — pure dataclasses + helpers."""

from __future__ import annotations

import unittest

from loom.kernel import obligations as obl


class ResponseObligationTests(unittest.TestCase):
    def test_defaults(self):
        o = obl.ResponseObligation(
            id=1,
            participant_id="loom",
            level="must",
            target_event_ids=[10],
            reason="direct_mention",
        )
        self.assertFalse(o.resolved)
        self.assertIsNone(o.resolved_by_event_id)

    def test_resolve_by_event(self):
        o = obl.ResponseObligation(
            id=1,
            participant_id="loom",
            level="must",
            target_event_ids=[10],
            reason="x",
        )
        o.resolve(by_event_id=42)
        self.assertTrue(o.resolved)
        self.assertEqual(o.resolved_by_event_id, 42)

    def test_resolve_administrative(self):
        o = obl.ResponseObligation(
            id=1,
            participant_id="loom",
            level="must",
            target_event_ids=[10],
            reason="x",
        )
        o.resolve(by_event_id=None)
        self.assertTrue(o.resolved)
        self.assertIsNone(o.resolved_by_event_id)


class UserTurnPlanTests(unittest.TestCase):
    def test_requires_response_with_no_required_rejects(self):
        with self.assertRaises(ValueError):
            obl.UserTurnPlan(
                requires_response=True,
                routing_case="question",
                required_participants=set(),
            )

    def test_plain_no_response_is_valid(self):
        plan = obl.UserTurnPlan(
            requires_response=False,
            routing_case="acknowledgement",
        )
        self.assertEqual(plan.required_participants, set())
        self.assertEqual(plan.obligations, [])

    def test_full_plan(self):
        ob = obl.ResponseObligation(
            id=0,
            participant_id="loom",
            level="must",
            target_event_ids=[12],
            reason="direct_mention",
        )
        plan = obl.UserTurnPlan(
            requires_response=True,
            routing_case="direct_mention",
            required_participants={"loom"},
            obligations=[ob],
            target_event_ids=[12],
            rationale="user @loom",
        )
        self.assertEqual(plan.required_participants, {"loom"})
        self.assertEqual(plan.obligations[0].participant_id, "loom")


class PlanForAcknowledgementTests(unittest.TestCase):
    def test_default_shape(self):
        plan = obl.plan_for_acknowledgement(target_event_ids=[7])
        self.assertFalse(plan.requires_response)
        self.assertEqual(plan.routing_case, "acknowledgement")
        self.assertEqual(plan.required_participants, set())
        self.assertEqual(plan.target_event_ids, [7])
        self.assertEqual(plan.confidence, 1.0)


class PlanForDefaultTests(unittest.TestCase):
    def test_with_responder(self):
        plan = obl.plan_for_default("loom", reason="fallback", target_event_ids=[3])
        self.assertTrue(plan.requires_response)
        self.assertEqual(plan.required_participants, {"loom"})
        self.assertEqual(len(plan.obligations), 1)
        self.assertEqual(plan.obligations[0].level, "must")
        self.assertEqual(plan.obligations[0].target_event_ids, [3])

    def test_with_no_responder_returns_no_response_plan(self):
        plan = obl.plan_for_default(None, reason="fallback")
        self.assertFalse(plan.requires_response)
        self.assertEqual(plan.routing_case, "none")
        self.assertEqual(plan.required_participants, set())
        # confidence is below the 0.5 floor used by callers that gate.
        self.assertLessEqual(plan.confidence, 0.5)


class PlanWithRequiredTests(unittest.TestCase):
    def test_single(self):
        plan = obl.plan_with_required(
            ["loom"],
            routing_case="question",
            target_event_ids=[10],
            reason="ends with ?",
            rationale="plain question",
        )
        self.assertEqual(plan.required_participants, {"loom"})
        self.assertEqual(plan.obligations[0].reason, "ends with ?")

    def test_multi(self):
        plan = obl.plan_with_required(
            ["a", "b"],
            routing_case="multi_opinion",
            target_event_ids=[10],
            reason="multi-opinion",
        )
        self.assertEqual(plan.required_participants, {"a", "b"})
        self.assertEqual(len(plan.obligations), 2)
        self.assertEqual({o.participant_id for o in plan.obligations}, {"a", "b"})

    def test_empty_list_rejected(self):
        with self.assertRaises(ValueError):
            obl.plan_with_required(
                [],
                routing_case="question",
                target_event_ids=[1],
                reason="x",
            )


class TurnPlanFloorControlFields(unittest.TestCase):
    """v0 floor-control fields on UserTurnPlan."""

    def test_allowed_speakers_defaults_to_required(self):
        plan = obl.plan_with_required(
            ["a", "b"],
            routing_case="multi_opinion",
            target_event_ids=[1],
            reason="x",
        )
        self.assertEqual(plan.allowed_speakers, {"a", "b"})

    def test_allowed_speakers_explicit_overrides(self):
        plan = obl.plan_with_required(
            ["a"],
            routing_case="direct_mention",
            target_event_ids=[1],
            reason="x",
            allowed_speakers={"a", "b"},
        )
        self.assertEqual(plan.allowed_speakers, {"a", "b"})

    def test_max_responses_defaults_to_allowed_count(self):
        plan = obl.plan_with_required(
            ["a", "b", "c"],
            routing_case="multi_opinion",
            target_event_ids=[1],
            reason="x",
        )
        self.assertEqual(plan.max_responses, 3)

    def test_max_responses_explicit(self):
        plan = obl.plan_with_required(
            ["a", "b", "c"],
            routing_case="multi_opinion",
            target_event_ids=[1],
            reason="x",
            max_responses=1,
        )
        self.assertEqual(plan.max_responses, 1)

    def test_wait_for_user_after_default_false(self):
        plan = obl.plan_with_required(
            ["a"],
            routing_case="multi_opinion",
            target_event_ids=[1],
            reason="x",
        )
        self.assertFalse(plan.wait_for_user_after)

    def test_wait_for_user_after_true_for_default_responder(self):
        plan = obl.plan_for_default("a", reason="fallback")
        self.assertTrue(plan.wait_for_user_after)
        self.assertEqual(plan.max_responses, 1)

    def test_instruction_passed_through(self):
        plan = obl.plan_with_required(
            ["a"],
            routing_case="direct_mention",
            target_event_ids=[1],
            reason="x",
            instruction="Teach the next step.",
        )
        self.assertEqual(plan.instruction, "Teach the next step.")

    def test_acknowledgement_has_zero_max_responses(self):
        plan = obl.plan_for_acknowledgement(target_event_ids=[1])
        self.assertEqual(plan.allowed_speakers, set())
        self.assertEqual(plan.max_responses, 0)


if __name__ == "__main__":
    unittest.main()
