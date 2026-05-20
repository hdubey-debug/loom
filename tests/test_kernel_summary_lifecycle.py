"""Tests for v0.3.x PR 3 — summary event constructors + commit lifecycle.

Doctrine: P18, P19, §3, §6 (`docs/internal/study/14-context-compaction-doctrine.md`).

Covers:

- :class:`SummaryEventConstructors` — the three control-event
  factories (proposed / committed / failed) and their payload shapes.
- :class:`SummaryReducers` — the three reducers mutate
  :class:`ContextState` correctly via the registry.
- :class:`SubmitSummaryProposedHappyPath` — off-lock pre-validation
  passes, under-lock anchor check passes, ``active_summary_by_scope``
  advances.
- :class:`SubmitSummaryProposedFailure` — structural failures emit
  ``summary_failed`` and increment ``failure_count``.
- :class:`SubmitSummaryProposedAnchorConflict` — race-condition
  produces ``ANCHOR_CONFLICT`` without bumping ``failure_count``.
- :class:`SubmitSummaryProposedRollingCompaction` — chained summaries
  produce the supersession edge and update ``active_summary_by_scope``.
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.context import (
    ContextScope,
    ContextState,
    SummaryFailureReason,
    SummaryRecord,
    new_context_state,
)
from loom.kernel.coordinator import RoomCoordinator, SummaryCommitResult
from loom.kernel.effects import (
    SummaryCommittedEffect,
    SummaryFailedEffect,
    SummaryProposedEffect,
    build_kernel_registry,
)
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState
from loom.kernel.state import new_kernel_state


def _coord_with_events(n: int = 12) -> RoomCoordinator:
    bus = MessageBus()
    state = RoomState(config=RoomConfig())
    state.add_participant(ParticipantInfo(id="loom"))
    coord = RoomCoordinator(bus, state)
    # Pre-populate the bus with N chat events so covers_event_range
    # validators pass.
    for i in range(n):
        bus.post(ev.chat(sender="loom", body=f"m{i}"))
    return coord


def _rec(**overrides) -> SummaryRecord:
    base = dict(
        summary_id="s1",
        scope=ContextScope(room_id="r1"),
        covers_event_range=(0, 9),
        text="summary text",
        retained_event_ids=(0, 5, 9),
        input_summary_ids=(),
        input_event_ranges=((0, 9),),
        model_id="m",
        prompt_hash="h",
        summarizer_id="loom",
        proposed_at_event_id=10,
    )
    base.update(overrides)
    return SummaryRecord(**base)


class SummaryEventConstructors(unittest.TestCase):
    def test_summary_proposed_carries_full_payload(self):
        scope = ContextScope(room_id="r1", thread_id="t1")
        e = ev.summary_proposed(
            summary_id="s1",
            scope=scope,
            covers_event_range=(0, 9),
            proposed_text="hello",
            summarizer_id="loom",
            input_event_ranges=((0, 9),),
        )
        self.assertEqual(ev.control_type_of(e), "summary_proposed")
        self.assertEqual(e.body["summary_id"], "s1")
        self.assertEqual(e.body["scope"]["room_id"], "r1")
        self.assertEqual(e.body["scope"]["thread_id"], "t1")
        self.assertEqual(e.body["covers_event_range"], [0, 9])
        self.assertEqual(e.thread_id, "t1")

    def test_summary_committed_carries_supersedes(self):
        e = ev.summary_committed(
            summary_id="s2",
            scope=ContextScope(room_id="r1"),
            covers_event_range=(0, 19),
            proposed_text="rolling",
            summarizer_id="loom",
            input_summary_ids=("s1",),
            input_event_ranges=((10, 19),),
            supersedes_summary_ids=("s1",),
            committed_at_event_id=42,
        )
        self.assertEqual(ev.control_type_of(e), "summary_committed")
        self.assertEqual(e.body["supersedes_summary_ids"], ["s1"])
        self.assertEqual(e.body["committed_at_event_id"], 42)

    def test_summary_failed_carries_reason(self):
        e = ev.summary_failed(
            proposed_summary_id="sX",
            scope=ContextScope(room_id="r1"),
            reason=SummaryFailureReason.LINEAGE_GAP.value,
            details="gap detected",
            summarizer_id="loom",
        )
        self.assertEqual(ev.control_type_of(e), "summary_failed")
        self.assertEqual(e.body["reason"], "lineage_gap")

    def test_summary_proposed_round_trip_through_jsonl(self):
        e = ev.summary_proposed(
            summary_id="s1",
            scope=ContextScope(room_id="r1"),
            covers_event_range=(0, 9),
            proposed_text="hi",
            summarizer_id="loom",
            input_event_ranges=((0, 9),),
        )
        rt = ev.Event.from_jsonl(e.to_jsonl())
        self.assertEqual(rt.body["summary_id"], "s1")


class SummaryReducers(unittest.TestCase):
    def test_committed_reducer_installs_record(self):
        reg = build_kernel_registry()
        state = new_kernel_state(RoomState(config=RoomConfig()))
        rec = _rec()
        reg.apply(state, SummaryCommittedEffect(record=rec))
        self.assertIn(rec.summary_id, state.context.summaries)
        self.assertEqual(
            state.context.active_summary_by_scope[rec.scope], rec.summary_id
        )

    def test_committed_reducer_adds_supersession_edge(self):
        reg = build_kernel_registry()
        state = new_kernel_state(RoomState(config=RoomConfig()))
        rec = _rec(input_summary_ids=("s0",))
        reg.apply(
            state,
            SummaryCommittedEffect(record=rec, supersedes_summary_ids=("s0",)),
        )
        self.assertEqual(state.context.supersession_edges["s0"], rec.summary_id)

    def test_proposed_reducer_is_audit_only(self):
        reg = build_kernel_registry()
        state = new_kernel_state(RoomState(config=RoomConfig()))
        rec = _rec()
        reg.apply(state, SummaryProposedEffect(record=rec))
        self.assertEqual(state.context.summaries, {})
        self.assertEqual(state.context.active_summary_by_scope, {})

    def test_failed_reducer_bumps_failure_count(self):
        reg = build_kernel_registry()
        state = new_kernel_state(RoomState(config=RoomConfig()))
        scope = ContextScope(room_id="r1")
        reg.apply(
            state,
            SummaryFailedEffect(
                summarizer_id="loom",
                scope=scope,
                reason=SummaryFailureReason.SCHEMA_ERROR,
            ),
        )
        key = ("loom", scope.as_tuple())
        self.assertEqual(state.context.failure_count[key], 1)

    def test_failed_reducer_skips_anchor_conflict(self):
        # Anchor races are benign — they do not count toward backoff.
        reg = build_kernel_registry()
        state = new_kernel_state(RoomState(config=RoomConfig()))
        scope = ContextScope(room_id="r1")
        reg.apply(
            state,
            SummaryFailedEffect(
                summarizer_id="loom",
                scope=scope,
                reason=SummaryFailureReason.ANCHOR_CONFLICT,
            ),
        )
        self.assertEqual(state.context.failure_count, {})


class SubmitSummaryProposedHappyPath(unittest.TestCase):
    def test_commit_advances_active_summary(self):
        coord = _coord_with_events(12)
        rec = _rec()
        result = coord.submit_summary_proposed(rec)
        self.assertTrue(result.committed, msg=f"reason={result.reason!r}")
        self.assertEqual(
            coord.kernel_state.context.active_summary_by_scope[rec.scope],
            rec.summary_id,
        )

    def test_commit_emits_proposed_then_committed_event(self):
        coord = _coord_with_events(12)
        before = len(coord.bus.snapshot())
        coord.submit_summary_proposed(_rec())
        after = coord.bus.snapshot()
        new_kinds = [
            ev.control_type_of(e)
            for e in after[before:]
            if ev.control_type_of(e) is not None
        ]
        self.assertEqual(
            new_kinds, ["summary_proposed", "summary_committed"]
        )

    def test_result_carries_committed_at_event_id(self):
        coord = _coord_with_events(12)
        result = coord.submit_summary_proposed(_rec())
        self.assertIsNotNone(result.committed_at_event_id)
        self.assertGreater(result.committed_at_event_id, 0)


class SubmitSummaryProposedFailure(unittest.TestCase):
    def test_bus_out_of_range_fails_off_lock(self):
        coord = _coord_with_events(5)  # only 5 events on bus
        rec = _rec()  # covers (0, 9) — out of range
        result = coord.submit_summary_proposed(rec)
        self.assertFalse(result.committed)
        self.assertEqual(result.reason, SummaryFailureReason.BUS_OUT_OF_RANGE)
        self.assertEqual(result.failed_validator, "structural")

    def test_failed_validator_increments_failure_count(self):
        coord = _coord_with_events(5)
        rec = _rec()
        coord.submit_summary_proposed(rec)
        key = (rec.summarizer_id, rec.scope.as_tuple())
        self.assertEqual(coord.kernel_state.context.failure_count[key], 1)

    def test_failed_validator_does_not_emit_committed_event(self):
        coord = _coord_with_events(5)
        before = len(coord.bus.snapshot())
        coord.submit_summary_proposed(_rec())
        emitted = [
            ev.control_type_of(e) for e in coord.bus.snapshot()[before:]
        ]
        self.assertNotIn("summary_committed", emitted)
        self.assertIn("summary_failed", emitted)

    def test_lineage_gap_classified(self):
        coord = _coord_with_events(20)
        rec = _rec(input_event_ranges=((0, 3), (5, 9)))  # gap
        result = coord.submit_summary_proposed(rec)
        self.assertFalse(result.committed)
        self.assertEqual(result.reason, SummaryFailureReason.LINEAGE_GAP)


class SubmitSummaryProposedAnchorConflict(unittest.TestCase):
    def test_anchor_conflict_when_active_already_set(self):
        # Pre-commit one summary to set active_summary_by_scope[scope] = s1.
        coord = _coord_with_events(20)
        rec1 = _rec(summary_id="s1", covers_event_range=(0, 9))
        result1 = coord.submit_summary_proposed(rec1)
        self.assertTrue(result1.committed)

        # Now another summariser proposes a *first-gen* summary for the
        # same scope; active is s1, this record has no input_summary_ids
        # → anchor conflict.
        rec2 = _rec(
            summary_id="s2",
            covers_event_range=(0, 9),
            input_summary_ids=(),
            input_event_ranges=((0, 9),),
        )
        result2 = coord.submit_summary_proposed(rec2)
        self.assertFalse(result2.committed)
        self.assertEqual(result2.reason, SummaryFailureReason.ANCHOR_CONFLICT)

    def test_anchor_conflict_does_not_count_toward_backoff(self):
        coord = _coord_with_events(20)
        rec1 = _rec(summary_id="s1")
        coord.submit_summary_proposed(rec1)
        rec2 = _rec(summary_id="s2", input_summary_ids=())
        coord.submit_summary_proposed(rec2)
        key = (rec2.summarizer_id, rec2.scope.as_tuple())
        # Pre-validator passed; only the under-lock anchor check
        # rejected. ANCHOR_CONFLICT is explicitly skipped by the
        # failed-reducer.
        self.assertEqual(
            coord.kernel_state.context.failure_count.get(key, 0), 0
        )


class SubmitSummaryProposedRollingCompaction(unittest.TestCase):
    def test_chained_summary_records_supersession_edge(self):
        coord = _coord_with_events(20)
        # First summary covers (0, 9).
        rec1 = _rec(summary_id="s1", covers_event_range=(0, 9))
        self.assertTrue(coord.submit_summary_proposed(rec1).committed)

        # Rolling summary covers (0, 19), with s1 as input + (10, 19)
        # as the new bare range.
        rec2 = _rec(
            summary_id="s2",
            covers_event_range=(0, 19),
            input_summary_ids=("s1",),
            input_event_ranges=((10, 19),),
        )
        result = coord.submit_summary_proposed(rec2)
        self.assertTrue(result.committed, msg=f"reason={result.reason!r}")
        ctx = coord.kernel_state.context
        self.assertEqual(ctx.active_summary_by_scope[rec2.scope], "s2")
        self.assertEqual(ctx.supersession_edges["s1"], "s2")


if __name__ == "__main__":
    unittest.main()
