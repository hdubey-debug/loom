"""Tests for ``loom.kernel.budgets`` — v0.3 PR 6 BudgetLedger.

Doctrine: P9 (three-way budget reservation/commit/refund), §9
(budget ledger).

Test classes:

- :class:`Ledger` — data structures (Scope, Reservation, Ledger;
  can_reserve, reserve, commit, refund, partial_commit_and_refund,
  outstanding, remaining).
- :class:`ThreeWayAccounting` — end-to-end flows:
  grant→commit (full); grant→denial pre-LLM (full refund);
  grant→denial post-LLM (commit LLM cost + refund remainder).
- :class:`CoordinatorWiresLedger` — coordinator hydrates the ledger;
  registry dispatches budget effects.
- :class:`BudgetEvents` — event constructors + validators.
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.budgets import (
    BudgetLedger,
    BudgetReservation,
    BudgetScope,
)
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.effects import (
    BudgetCommittedEffect,
    BudgetRefundedEffect,
    BudgetReservedEffect,
)
from loom.kernel.events import EventShapeError, Event
from loom.kernel.room import RoomConfig, RoomState


_ROOM_SCOPE = BudgetScope(room_id="r1")


class Ledger(unittest.TestCase):
    def test_empty_ledger_remaining_is_inf_for_unlimited_scopes(self):
        led = BudgetLedger()
        self.assertEqual(led.remaining(_ROOM_SCOPE), float("inf"))

    def test_remaining_subtracts_outstanding(self):
        led = BudgetLedger(limits={_ROOM_SCOPE: 100.0})
        led.reserve(1, _ROOM_SCOPE, 30.0)
        self.assertEqual(led.remaining(_ROOM_SCOPE), 70.0)

    def test_can_reserve_true_when_within_limit(self):
        led = BudgetLedger(limits={_ROOM_SCOPE: 100.0})
        self.assertTrue(led.can_reserve(_ROOM_SCOPE, 60.0))

    def test_can_reserve_false_when_over_limit(self):
        led = BudgetLedger(limits={_ROOM_SCOPE: 100.0})
        led.reserve(1, _ROOM_SCOPE, 60.0)
        self.assertFalse(led.can_reserve(_ROOM_SCOPE, 50.0))

    def test_reserve_records_reservation(self):
        led = BudgetLedger()
        r = led.reserve(7, _ROOM_SCOPE, 25.0)
        self.assertEqual(r.lease_id, 7)
        self.assertEqual(r.amount, 25.0)
        self.assertIn(7, led.reservations)

    def test_double_reserve_for_same_lease_raises(self):
        led = BudgetLedger()
        led.reserve(7, _ROOM_SCOPE, 10.0)
        with self.assertRaises(ValueError):
            led.reserve(7, _ROOM_SCOPE, 5.0)

    def test_reserve_over_limit_raises(self):
        led = BudgetLedger(limits={_ROOM_SCOPE: 5.0})
        with self.assertRaises(ValueError):
            led.reserve(1, _ROOM_SCOPE, 10.0)

    def test_commit_removes_reservation_and_records_actual(self):
        led = BudgetLedger()
        led.reserve(7, _ROOM_SCOPE, 50.0)
        r, actual = led.commit(7, 30.0)
        self.assertEqual(actual, 30.0)
        self.assertNotIn(7, led.reservations)
        self.assertEqual(led.commits[_ROOM_SCOPE], 30.0)

    def test_commit_unknown_lease_raises_key_error(self):
        led = BudgetLedger()
        with self.assertRaises(KeyError):
            led.commit(99, 1.0)

    def test_commit_more_than_reserved_raises(self):
        led = BudgetLedger()
        led.reserve(7, _ROOM_SCOPE, 10.0)
        with self.assertRaises(ValueError):
            led.commit(7, 20.0)

    def test_refund_releases_without_recording_committed(self):
        led = BudgetLedger()
        led.reserve(7, _ROOM_SCOPE, 50.0)
        r = led.refund(7, reason="denied")
        self.assertEqual(r.amount, 50.0)
        self.assertNotIn(7, led.reservations)
        # Pure refund (pre-commit) does NOT touch refunds[scope] —
        # there was no commit to offset.
        self.assertEqual(led.commits.get(_ROOM_SCOPE, 0.0), 0.0)
        self.assertEqual(led.refunds.get(_ROOM_SCOPE, 0.0), 0.0)

    def test_refund_unknown_lease_raises_key_error(self):
        led = BudgetLedger()
        with self.assertRaises(KeyError):
            led.refund(99, reason="cancelled")

    def test_partial_commit_and_refund_records_both(self):
        led = BudgetLedger()
        led.reserve(7, _ROOM_SCOPE, 100.0)
        r, committed, refunded = led.partial_commit_and_refund(7, actual_used=40.0)
        self.assertEqual(committed, 40.0)
        self.assertEqual(refunded, 60.0)
        self.assertEqual(led.commits[_ROOM_SCOPE], 40.0)
        self.assertEqual(led.refunds[_ROOM_SCOPE], 60.0)
        self.assertNotIn(7, led.reservations)

    def test_partial_commit_exceeding_reservation_raises(self):
        led = BudgetLedger()
        led.reserve(7, _ROOM_SCOPE, 10.0)
        with self.assertRaises(ValueError):
            led.partial_commit_and_refund(7, actual_used=20.0)

    def test_outstanding_includes_both_live_and_net_committed(self):
        led = BudgetLedger()
        led.reserve(1, _ROOM_SCOPE, 30.0)
        led.reserve(2, _ROOM_SCOPE, 20.0)
        # Commit one with 10 actual; the live remaining is 30 from
        # lease 1, plus 10 committed; outstanding = 40.
        led.commit(2, 10.0)
        self.assertEqual(led.outstanding(_ROOM_SCOPE), 30.0 + 10.0)

    def test_distinct_scopes_are_independent(self):
        led = BudgetLedger(
            limits={
                BudgetScope(room_id="r1"): 50.0,
                BudgetScope(room_id="r2"): 100.0,
            }
        )
        led.reserve(1, BudgetScope(room_id="r1"), 40.0)
        # r1 is at 40/50; r2 is independent.
        self.assertTrue(led.can_reserve(BudgetScope(room_id="r2"), 90.0))
        self.assertFalse(led.can_reserve(BudgetScope(room_id="r1"), 20.0))

    def test_reservation_dataclass_is_frozen(self):
        r = BudgetReservation(lease_id=1, scope=_ROOM_SCOPE, amount=5.0, reserved_at=0.0)
        with self.assertRaises(Exception):
            r.amount = 10.0  # type: ignore[misc]

    def test_scope_equality_by_tuple(self):
        a = BudgetScope(room_id="r1", participant_id="loom")
        b = BudgetScope(room_id="r1", participant_id="loom")
        self.assertEqual(a, b)
        self.assertNotEqual(a, BudgetScope(room_id="r2", participant_id="loom"))

    def test_remaining_with_no_limit_is_infinite(self):
        led = BudgetLedger()
        led.reserve(1, _ROOM_SCOPE, 999.0)
        # No limits[_ROOM_SCOPE]; remaining is +inf regardless of outstanding.
        self.assertEqual(led.remaining(_ROOM_SCOPE), float("inf"))


class ThreeWayAccounting(unittest.TestCase):
    def test_full_lifecycle_reserve_commit(self):
        led = BudgetLedger()
        led.reserve(1, _ROOM_SCOPE, 100.0)
        led.commit(1, 80.0)
        self.assertEqual(led.commits[_ROOM_SCOPE], 80.0)
        self.assertEqual(led.refunds.get(_ROOM_SCOPE, 0.0), 0.0)

    def test_lifecycle_reserve_refund_pre_llm(self):
        # Lease denied before LLM: pure refund — nothing recorded in
        # commits or refunds.
        led = BudgetLedger()
        led.reserve(1, _ROOM_SCOPE, 100.0)
        led.refund(1, reason="denied")
        self.assertEqual(led.commits.get(_ROOM_SCOPE, 0.0), 0.0)
        self.assertEqual(led.refunds.get(_ROOM_SCOPE, 0.0), 0.0)

    def test_lifecycle_reserve_partial_post_llm(self):
        # LLM call succeeded (40 tokens) but post-stream validation
        # suppressed: commit 40, refund 60.
        led = BudgetLedger()
        led.reserve(1, _ROOM_SCOPE, 100.0)
        led.partial_commit_and_refund(1, actual_used=40.0)
        self.assertEqual(led.commits[_ROOM_SCOPE], 40.0)
        self.assertEqual(led.refunds[_ROOM_SCOPE], 60.0)
        # Net committed = 40 - 60 = -20 (refunds exceed commits because
        # the post-LLM remainder was greater than the committed slice).
        self.assertEqual(led.commits[_ROOM_SCOPE] - led.refunds[_ROOM_SCOPE], -20.0)

    def test_invariant_sum_reservations_le_limits_after_each_step(self):
        led = BudgetLedger(limits={_ROOM_SCOPE: 100.0})
        # Reserve up to the limit across many leases.
        for i in range(10):
            led.reserve(i, _ROOM_SCOPE, 10.0)
            self.assertLessEqual(led.outstanding(_ROOM_SCOPE), 100.0)
        # 11th reservation must be rejected.
        self.assertFalse(led.can_reserve(_ROOM_SCOPE, 1.0))


class CoordinatorWiresLedger(unittest.TestCase):
    def _coord(self) -> RoomCoordinator:
        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        return RoomCoordinator(bus, state)

    def test_coordinator_init_populates_budget_ledger(self):
        coord = self._coord()
        self.assertIsInstance(coord.kernel_state.budget, BudgetLedger)
        self.assertEqual(len(coord.kernel_state.budget.reservations), 0)

    def test_apply_budget_reserved_effect_updates_ledger(self):
        coord = self._coord()
        effect = BudgetReservedEffect(
            lease_id=42,
            scope=BudgetScope(room_id="r1"),
            amount=15.5,
        )
        with coord._lock:
            coord._apply_effect(effect)
        self.assertIn(42, coord.kernel_state.budget.reservations)
        self.assertEqual(coord.kernel_state.budget.reservations[42].amount, 15.5)

    def test_apply_budget_committed_effect_records_actual(self):
        coord = self._coord()
        with coord._lock:
            coord._apply_effect(
                BudgetReservedEffect(
                    lease_id=42,
                    scope=BudgetScope(room_id="r1"),
                    amount=15.5,
                )
            )
            coord._apply_effect(
                BudgetCommittedEffect(
                    lease_id=42,
                    scope=BudgetScope(room_id="r1"),
                    actual=10.0,
                )
            )
        self.assertNotIn(42, coord.kernel_state.budget.reservations)
        self.assertEqual(coord.kernel_state.budget.commits[BudgetScope(room_id="r1")], 10.0)

    def test_apply_budget_refunded_effect_releases_reservation(self):
        coord = self._coord()
        with coord._lock:
            coord._apply_effect(
                BudgetReservedEffect(
                    lease_id=42,
                    scope=BudgetScope(room_id="r1"),
                    amount=15.5,
                )
            )
            coord._apply_effect(
                BudgetRefundedEffect(
                    lease_id=42,
                    scope=BudgetScope(room_id="r1"),
                    amount=15.5,
                    reason="denied",
                )
            )
        self.assertNotIn(42, coord.kernel_state.budget.reservations)


class BudgetEvents(unittest.TestCase):
    def test_budget_reserved_constructor(self):
        e = ev.budget_reserved(lease_id=1, amount=10.0)
        self.assertEqual(ev.control_type_of(e), "budget_reserved")
        self.assertEqual(e.body["amount"], 10.0)

    def test_budget_committed_round_trip(self):
        e = ev.budget_committed(lease_id=1, actual=5.5)
        e.id, e.ts = 0, 0.0
        loaded = Event.from_jsonl(e.to_jsonl())
        self.assertEqual(loaded.body["actual"], 5.5)

    def test_budget_refunded_carries_reason(self):
        e = ev.budget_refunded(lease_id=1, amount=10.0, reason="cancelled")
        self.assertEqual(e.body["reason"], "cancelled")

    def test_validator_rejects_missing_lease_id(self):
        import json

        line = json.dumps(
            {
                "kind": "control",
                "sender": "system",
                "body": {"control_type": "budget_reserved", "amount": 10.0},
                "channel": "main",
                "addressees": [],
                "room_epoch": 0,
                "user_turn_id": None,
                "meta": {},
                "id": 0,
                "ts": 1.0,
            }
        )
        with self.assertRaises(EventShapeError):
            Event.from_jsonl(line)


if __name__ == "__main__":
    unittest.main()
