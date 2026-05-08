"""Tests for ``loom.kernel.user_turn`` — UserTurn + obligation bookkeeping."""
from __future__ import annotations

import unittest

from loom.kernel.obligations import (
    ResponseObligation,
    UserTurnPlan,
    plan_for_acknowledgement,
    plan_with_required,
)
from loom.kernel.user_turn import (
    UserTurn,
    is_user_turn_complete,
    make_user_turn,
    participant_is_eligible,
    should_open_new_user_turn,
)


def _plan_for(*ids: str, optional: tuple[str, ...] = (),
              routing_case: str = "direct_mention") -> UserTurnPlan:
    plan = plan_with_required(
        list(ids), routing_case=routing_case,  # type: ignore[arg-type]
        target_event_ids=[10], reason="test",
        optional=list(optional),
    )
    return plan


class MakeUserTurn(unittest.TestCase):
    def test_assigns_obligation_ids(self):
        plan = _plan_for("a", "b")
        turn, next_id = make_user_turn(turn_id=1, user_event_id=10,
                                       plan=plan,
                                       started_at=100.0,
                                       next_obligation_id=1)
        self.assertEqual(next_id, 3)
        self.assertEqual(set(turn.obligations.keys()), {1, 2})
        self.assertEqual(set(o.participant_id for o in turn.obligations.values()),
                         {"a", "b"})

    def test_required_optional_views(self):
        plan = _plan_for("a", optional=("b",))
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        self.assertEqual(turn.required_participants, {"a"})
        self.assertEqual(turn.optional_participants, {"b"})


class ObligationLookup(unittest.TestCase):
    def test_obligation_for_returns_open(self):
        plan = _plan_for("loom")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        ob = turn.obligation_for("loom")
        self.assertIsNotNone(ob)
        self.assertEqual(ob.participant_id, "loom")
        self.assertEqual(ob.level, "must")

    def test_obligation_for_returns_none_when_resolved(self):
        plan = _plan_for("loom")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        oid = next(iter(turn.obligations))
        turn.mark_obligation_resolved(oid, by_event_id=42, now=101.0)
        self.assertIsNone(turn.obligation_for("loom"))

    def test_obligation_for_unknown_participant(self):
        plan = _plan_for("loom")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        self.assertIsNone(turn.obligation_for("ghost"))


class MarkDrafted(unittest.TestCase):
    def test_basic(self):
        plan = _plan_for("loom")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        turn.mark_drafted("loom", now=101.0)
        self.assertEqual(turn.speaker_counts["loom"], 1)
        self.assertIn("loom", turn.drafted)
        self.assertEqual(turn.last_activity_at, 101.0)

    def test_no_count_toward_cap(self):
        plan = _plan_for("loom")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        turn.mark_drafted("loom", count_toward_cap=False, now=101.0)
        self.assertEqual(turn.speaker_counts.get("loom", 0), 0)
        self.assertIn("loom", turn.drafted)


class MarkObligationResolved(unittest.TestCase):
    def test_resolve_open_obligation(self):
        plan = _plan_for("loom")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        oid = next(iter(turn.obligations))
        ok = turn.mark_obligation_resolved(oid, by_event_id=99, now=101.0)
        self.assertTrue(ok)
        self.assertTrue(turn.obligations[oid].resolved)
        self.assertEqual(turn.obligations[oid].resolved_by_event_id, 99)
        self.assertEqual(turn.last_activity_at, 101.0)

    def test_resolve_unknown_obligation_returns_false(self):
        plan = _plan_for("loom")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        self.assertFalse(turn.mark_obligation_resolved(
            obligation_id=999, by_event_id=99, now=101.0,
        ))

    def test_resolve_already_resolved_returns_false(self):
        plan = _plan_for("loom")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        oid = next(iter(turn.obligations))
        turn.mark_obligation_resolved(oid, by_event_id=99, now=101.0)
        self.assertFalse(turn.mark_obligation_resolved(
            oid, by_event_id=100, now=102.0,
        ))


class UnresolvedRequired(unittest.TestCase):
    def test_starts_with_full_required_set(self):
        plan = _plan_for("a", "b", "c")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        self.assertEqual(turn.unresolved_required(), {"a", "b", "c"})

    def test_resolves_drop_from_set(self):
        plan = _plan_for("a", "b")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        oid_a = next(o.id for o in turn.obligations.values()
                     if o.participant_id == "a")
        turn.mark_obligation_resolved(oid_a, by_event_id=42, now=101.0)
        self.assertEqual(turn.unresolved_required(), {"b"})

    def test_acknowledgement_plan_has_no_required(self):
        plan = plan_for_acknowledgement(target_event_ids=[10])
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        self.assertEqual(turn.unresolved_required(), set())


class IsUserTurnComplete(unittest.TestCase):
    def test_complete_when_all_must_resolved(self):
        plan = _plan_for("a", "b")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        self.assertFalse(is_user_turn_complete(turn))
        for oid in list(turn.obligations):
            turn.mark_obligation_resolved(oid, by_event_id=42, now=101.0)
        self.assertTrue(is_user_turn_complete(turn))

    def test_incomplete_with_one_open(self):
        plan = _plan_for("a", "b")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        oid_a = next(o.id for o in turn.obligations.values()
                     if o.participant_id == "a")
        turn.mark_obligation_resolved(oid_a, by_event_id=42, now=101.0)
        self.assertFalse(is_user_turn_complete(turn))

    def test_acknowledgement_plan_trivially_complete(self):
        plan = plan_for_acknowledgement(target_event_ids=[10])
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        self.assertTrue(is_user_turn_complete(turn))


class ParticipantIsEligible(unittest.TestCase):
    def test_required_is_eligible(self):
        plan = _plan_for("a")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        self.assertTrue(participant_is_eligible(turn, "a"))

    def test_optional_is_eligible(self):
        plan = _plan_for("a", optional=("b",))
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        self.assertTrue(participant_is_eligible(turn, "b"))

    def test_outsider_is_not_eligible(self):
        plan = _plan_for("a")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        self.assertFalse(participant_is_eligible(turn, "outsider"))


class CloseAndIdle(unittest.TestCase):
    def test_close_records_reason(self):
        plan = _plan_for("a")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        turn.close("idle_timeout")
        self.assertEqual(turn.state, "closed")
        self.assertEqual(turn.closure_reason, "idle_timeout")

    def test_close_with_obligation_unresolved(self):
        plan = _plan_for("a")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        turn.close("obligation_unresolved")
        self.assertEqual(turn.closure_reason, "obligation_unresolved")

    def test_repeated_close_is_idempotent_last_reason_wins(self):
        plan = _plan_for("a")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        turn.close("idle_timeout")
        turn.close("cancelled")
        self.assertEqual(turn.state, "closed")
        self.assertEqual(turn.closure_reason, "cancelled")

    def test_is_idle_uses_last_activity(self):
        plan = _plan_for("a")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        turn.last_activity_at = 100.0
        self.assertFalse(turn.is_idle(idle_timeout_s=20, now=110.0))
        self.assertTrue(turn.is_idle(idle_timeout_s=20, now=121.0))

    def test_add_obligation_allocates_and_copies_targets(self):
        plan = _plan_for("a")
        turn, _ = make_user_turn(1, 10, plan, started_at=100.0)
        targets = [10]
        ob, next_id = turn.add_obligation(
            "b", "must", targets, "rerouted_from_a",
            next_obligation_id=7,
        )
        targets.append(11)
        self.assertEqual(next_id, 8)
        self.assertIs(turn.obligations[7], ob)
        self.assertEqual(ob.id, 7)
        self.assertEqual(ob.participant_id, "b")
        self.assertEqual(ob.target_event_ids, [10])
        self.assertIn("b", turn.unresolved_required())


class Debounce(unittest.TestCase):
    def test_first_post_always_opens_new_turn(self):
        self.assertTrue(should_open_new_user_turn(None, now=100.0,
                                                  debounce_ms=250))

    def test_within_debounce_appends(self):
        self.assertFalse(should_open_new_user_turn(prev_user_post_ts=100.0,
                                                   now=100.1,
                                                   debounce_ms=250))

    def test_past_debounce_opens_new(self):
        self.assertTrue(should_open_new_user_turn(prev_user_post_ts=100.0,
                                                  now=100.4,
                                                  debounce_ms=250))

    def test_exact_debounce_boundary_opens_new(self):
        self.assertTrue(should_open_new_user_turn(prev_user_post_ts=100.0,
                                                  now=100.25,
                                                  debounce_ms=250))


if __name__ == "__main__":
    unittest.main()
