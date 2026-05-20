"""Tests for v0.3.x PR 4 — ContextPressure estimator + cache key.

Doctrine: §10 (`docs/internal/study/14-context-compaction-doctrine.md`).
"""

from __future__ import annotations

import unittest

from loom.kernel.context import (
    ContextPressure,
    ContextScope,
    ContextState,
    SummaryRecord,
    _PRESSURE_CACHE,
    estimate_context_pressure,
    select_compaction_range,
)


class EstimatorBasics(unittest.TestCase):
    def setUp(self):
        _PRESSURE_CACHE.clear()

    def test_returns_context_pressure(self):
        out = estimate_context_pressure(
            participant_id="loom",
            scope=ContextScope(room_id="main"),
            kernel_state_version=1,
            prompt_template_hash="h",
            estimated_prompt_chars=400,
        )
        self.assertIsInstance(out, ContextPressure)
        self.assertEqual(out.estimated_tokens, 100)  # 400 / 4

    def test_zero_chars_zero_pressure(self):
        out = estimate_context_pressure(
            participant_id="loom",
            scope=ContextScope(room_id="main"),
            kernel_state_version=1,
            prompt_template_hash="h",
            estimated_prompt_chars=0,
        )
        self.assertEqual(out.estimated_tokens, 0)
        self.assertEqual(out.pressure_ratio, 0.0)
        self.assertFalse(out.needs_compaction)

    def test_above_threshold_triggers_needs_compaction(self):
        out = estimate_context_pressure(
            participant_id="loom",
            scope=ContextScope(room_id="main"),
            kernel_state_version=1,
            prompt_template_hash="h",
            estimated_prompt_chars=4 * 200_000,  # 200k tokens
            threshold_ratio=0.5,
        )
        self.assertTrue(out.needs_compaction)
        self.assertGreater(out.pressure_ratio, 0.5)

    def test_below_threshold_does_not_trigger(self):
        out = estimate_context_pressure(
            participant_id="loom",
            scope=ContextScope(room_id="main"),
            kernel_state_version=1,
            prompt_template_hash="h",
            estimated_prompt_chars=4 * 1_000,
            threshold_ratio=0.7,
        )
        self.assertFalse(out.needs_compaction)


class EstimatorCacheKey(unittest.TestCase):
    def setUp(self):
        _PRESSURE_CACHE.clear()

    def test_same_inputs_return_cached_object_identity(self):
        a = estimate_context_pressure(
            participant_id="loom",
            scope=ContextScope(room_id="main"),
            kernel_state_version=1,
            prompt_template_hash="h",
            estimated_prompt_chars=100,
        )
        b = estimate_context_pressure(
            participant_id="loom",
            scope=ContextScope(room_id="main"),
            kernel_state_version=1,
            prompt_template_hash="h",
            estimated_prompt_chars=100,
        )
        self.assertIs(a, b)

    def test_version_bump_invalidates_cache(self):
        a = estimate_context_pressure(
            participant_id="loom",
            scope=ContextScope(room_id="main"),
            kernel_state_version=1,
            prompt_template_hash="h",
            estimated_prompt_chars=100,
        )
        b = estimate_context_pressure(
            participant_id="loom",
            scope=ContextScope(room_id="main"),
            kernel_state_version=2,  # different version
            prompt_template_hash="h",
            estimated_prompt_chars=100,
        )
        self.assertIsNot(a, b)

    def test_participant_change_invalidates_cache(self):
        a = estimate_context_pressure(
            participant_id="loom",
            scope=ContextScope(room_id="main"),
            kernel_state_version=1,
            prompt_template_hash="h",
            estimated_prompt_chars=100,
        )
        b = estimate_context_pressure(
            participant_id="claude_code",
            scope=ContextScope(room_id="main"),
            kernel_state_version=1,
            prompt_template_hash="h",
            estimated_prompt_chars=100,
        )
        self.assertIsNot(a, b)

    def test_template_change_invalidates_cache(self):
        a = estimate_context_pressure(
            participant_id="loom",
            scope=ContextScope(room_id="main"),
            kernel_state_version=1,
            prompt_template_hash="h-v1",
            estimated_prompt_chars=100,
        )
        b = estimate_context_pressure(
            participant_id="loom",
            scope=ContextScope(room_id="main"),
            kernel_state_version=1,
            prompt_template_hash="h-v2",
            estimated_prompt_chars=100,
        )
        self.assertIsNot(a, b)


class SelectCompactionRange(unittest.TestCase):
    def test_no_active_summary_recommends_full_bus(self):
        st = ContextState()
        scope = ContextScope(room_id="main")
        lo, hi = select_compaction_range(st, scope, bus_length=100, min_events=1)
        self.assertEqual((lo, hi), (0, 99))

    def test_active_summary_recommends_tail(self):
        st = ContextState()
        scope = ContextScope(room_id="main")
        rec = SummaryRecord(
            summary_id="s1",
            scope=scope,
            covers_event_range=(0, 49),
            text="x",
            input_event_ranges=((0, 49),),
        )
        st.summaries["s1"] = rec
        st.active_summary_by_scope[scope] = "s1"
        lo, hi = select_compaction_range(st, scope, bus_length=100, min_events=1)
        self.assertEqual((lo, hi), (50, 99))

    def test_too_few_new_events_returns_degenerate_range(self):
        st = ContextState()
        scope = ContextScope(room_id="main")
        rec = SummaryRecord(
            summary_id="s1",
            scope=scope,
            covers_event_range=(0, 95),
            text="x",
            input_event_ranges=((0, 95),),
        )
        st.summaries["s1"] = rec
        st.active_summary_by_scope[scope] = "s1"
        lo, hi = select_compaction_range(
            st, scope, bus_length=100, min_events=10
        )
        # 96..99 = 4 events, below threshold 10 → degenerate
        self.assertLess(hi, lo)

    def test_pressure_suggests_range_when_state_supplied(self):
        st = ContextState()
        scope = ContextScope(room_id="main")
        out = estimate_context_pressure(
            participant_id="loom",
            scope=scope,
            kernel_state_version=1,
            prompt_template_hash="h-new",
            estimated_prompt_chars=4 * 200_000,
            threshold_ratio=0.5,
            context_state=st,
            bus_length=100,
        )
        self.assertTrue(out.needs_compaction)
        self.assertEqual(out.suggested_compaction_range, (0, 99))


if __name__ == "__main__":
    unittest.main()
