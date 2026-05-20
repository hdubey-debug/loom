"""Tests for v0.3.x PR 2 — ContextState + SummaryRecord + validators.

Doctrine: P17 (view-layer compaction), §3.2 / §3.3 / §3.4 / §6.

Five test groups:

- :class:`SummaryRecordShape` — construction defaults, frozen-ness.
- :class:`ContextStateBasics` — empty defaults, mutation, equality.
- :class:`LineageValidation` — :func:`validate_lineage` invariants.
- :class:`StructuralValidator` — :func:`validate_summary_record`
  produces each :class:`SummaryFailureReason` for the right input.
- :class:`ContextStateJsonRoundTrip` — serialise + restore.
"""

from __future__ import annotations

import unittest

from loom.kernel.context import (
    ContextScope,
    ContextState,
    SUMMARY_RECORD_SCHEMA_VERSION,
    SummaryFailureReason,
    SummaryRecord,
    context_state_from_jsonable,
    context_state_to_jsonable,
    new_context_state,
    summary_record_from_jsonable,
    summary_record_to_jsonable,
    validate_lineage,
    validate_summary_record,
)


def _rec(**overrides) -> SummaryRecord:
    base = dict(
        summary_id="s1",
        scope=ContextScope(room_id="r1"),
        covers_event_range=(0, 9),
        text="summary",
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


class SummaryRecordShape(unittest.TestCase):
    def test_default_schema_version_is_1(self):
        self.assertEqual(_rec().schema_version, SUMMARY_RECORD_SCHEMA_VERSION)
        self.assertEqual(_rec().schema_version, 1)

    def test_committed_at_event_id_defaults_none(self):
        self.assertIsNone(_rec().committed_at_event_id)

    def test_record_is_frozen(self):
        rec = _rec()
        with self.assertRaises(Exception):
            rec.text = "mutated"  # type: ignore[misc]


class ContextStateBasics(unittest.TestCase):
    def test_empty_default(self):
        st = new_context_state()
        self.assertEqual(st.summaries, {})
        self.assertEqual(st.active_summary_by_scope, {})
        self.assertEqual(st.supersession_edges, {})
        self.assertEqual(st.failure_count, {})

    def test_mutate_summaries(self):
        st = new_context_state()
        rec = _rec()
        st.summaries[rec.summary_id] = rec
        scope = rec.scope
        st.active_summary_by_scope[scope] = rec.summary_id
        self.assertIs(st.summaries[rec.summary_id], rec)
        self.assertEqual(st.active_summary_by_scope[scope], "s1")

    def test_failure_count_keyed_by_summarizer_and_scope_tuple(self):
        st = new_context_state()
        key = ("loom", ContextScope(room_id="r1").as_tuple())
        st.failure_count[key] = 2
        self.assertEqual(st.failure_count[key], 2)


class LineageValidation(unittest.TestCase):
    def test_single_range_equal_covers_passes(self):
        rec = _rec(input_event_ranges=((0, 9),), input_summary_ids=())
        ok, reason, _ = validate_lineage(rec)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_two_contiguous_ranges_passes(self):
        rec = _rec(input_event_ranges=((0, 4), (5, 9)), input_summary_ids=())
        ok, reason, _ = validate_lineage(rec)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_gap_between_ranges_classified_as_lineage_gap(self):
        rec = _rec(input_event_ranges=((0, 3), (5, 9)))
        ok, reason, _ = validate_lineage(rec)
        self.assertFalse(ok)
        self.assertEqual(reason, SummaryFailureReason.LINEAGE_GAP)

    def test_overlapping_ranges_classified_as_lineage_overlap(self):
        rec = _rec(input_event_ranges=((0, 5), (4, 9)))
        ok, reason, _ = validate_lineage(rec)
        self.assertFalse(ok)
        self.assertEqual(reason, SummaryFailureReason.LINEAGE_OVERLAP)

    def test_range_union_too_small_classified_as_mismatch(self):
        rec = _rec(input_event_ranges=((0, 5),))
        ok, reason, _ = validate_lineage(rec)
        self.assertFalse(ok)
        self.assertEqual(reason, SummaryFailureReason.COVERS_RANGE_MISMATCH)

    def test_invalid_covers_range_schema_error(self):
        rec = _rec(covers_event_range=(5, 2))  # hi < lo
        ok, reason, _ = validate_lineage(rec)
        self.assertFalse(ok)
        self.assertEqual(reason, SummaryFailureReason.SCHEMA_ERROR)

    def test_input_summary_unknown_when_lookup_missing_id(self):
        rec = _rec(input_summary_ids=("ghost",), input_event_ranges=())
        ok, reason, _ = validate_lineage(rec, input_summary_lookup={})
        self.assertFalse(ok)
        self.assertEqual(reason, SummaryFailureReason.INPUT_SUMMARY_UNKNOWN)

    def test_cross_scope_input_rejected(self):
        parent = _rec(
            summary_id="p1",
            scope=ContextScope(room_id="r-other"),
            covers_event_range=(0, 4),
            input_event_ranges=((0, 4),),
        )
        child = _rec(
            summary_id="c1",
            scope=ContextScope(room_id="r1"),
            covers_event_range=(0, 9),
            input_summary_ids=("p1",),
            input_event_ranges=((5, 9),),
        )
        ok, reason, _ = validate_lineage(
            child, input_summary_lookup={"p1": parent}
        )
        self.assertFalse(ok)
        self.assertEqual(reason, SummaryFailureReason.CROSS_SCOPE)

    def test_rolling_compaction_with_input_summary_passes(self):
        parent = _rec(
            summary_id="p1",
            covers_event_range=(0, 4),
            input_event_ranges=((0, 4),),
        )
        child = _rec(
            summary_id="c1",
            covers_event_range=(0, 9),
            input_summary_ids=("p1",),
            input_event_ranges=((5, 9),),
        )
        ok, reason, _ = validate_lineage(
            child, input_summary_lookup={"p1": parent}
        )
        self.assertTrue(ok, msg=f"reason={reason!r}")


class StructuralValidator(unittest.TestCase):
    def test_happy_path_passes(self):
        rec = _rec()
        ok, reason, _ = validate_summary_record(rec, bus_length=20)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_bus_out_of_range_when_hi_at_or_past_length(self):
        rec = _rec(covers_event_range=(0, 9), input_event_ranges=((0, 9),))
        ok, reason, _ = validate_summary_record(rec, bus_length=9)
        self.assertFalse(ok)
        self.assertEqual(reason, SummaryFailureReason.BUS_OUT_OF_RANGE)

    def test_retained_out_of_range_below_lo(self):
        rec = _rec(retained_event_ids=(0, 5, 11))
        ok, reason, _ = validate_summary_record(rec, bus_length=20)
        self.assertFalse(ok)
        self.assertEqual(reason, SummaryFailureReason.RETAINED_OUT_OF_RANGE)

    def test_lineage_failure_propagates_through_full_validator(self):
        rec = _rec(input_event_ranges=((0, 3), (5, 9)))
        ok, reason, _ = validate_summary_record(rec, bus_length=20)
        self.assertFalse(ok)
        self.assertEqual(reason, SummaryFailureReason.LINEAGE_GAP)


class ContextStateJsonRoundTrip(unittest.TestCase):
    def test_empty_round_trip(self):
        st = new_context_state()
        rt = context_state_from_jsonable(context_state_to_jsonable(st))
        self.assertEqual(rt.summaries, {})
        self.assertEqual(rt.active_summary_by_scope, {})
        self.assertEqual(rt.supersession_edges, {})
        self.assertEqual(rt.failure_count, {})

    def test_populated_round_trip_preserves_summaries(self):
        st = new_context_state()
        rec = _rec()
        st.summaries[rec.summary_id] = rec
        st.active_summary_by_scope[rec.scope] = rec.summary_id
        st.supersession_edges["old_id"] = rec.summary_id
        key = ("loom", rec.scope.as_tuple())
        st.failure_count[key] = 3
        rt = context_state_from_jsonable(context_state_to_jsonable(st))
        self.assertEqual(list(rt.summaries.keys()), [rec.summary_id])
        self.assertEqual(
            rt.summaries[rec.summary_id].covers_event_range, (0, 9)
        )
        self.assertEqual(rt.active_summary_by_scope[rec.scope], rec.summary_id)
        self.assertEqual(rt.supersession_edges["old_id"], rec.summary_id)
        self.assertEqual(rt.failure_count[key], 3)

    def test_summary_record_round_trip_preserves_lineage(self):
        rec = _rec(
            input_summary_ids=("p1",),
            input_event_ranges=((5, 9),),
            covers_event_range=(0, 9),
        )
        rt = summary_record_from_jsonable(summary_record_to_jsonable(rec))
        self.assertEqual(rt.input_summary_ids, ("p1",))
        self.assertEqual(rt.input_event_ranges, ((5, 9),))


if __name__ == "__main__":
    unittest.main()
