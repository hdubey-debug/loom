"""Tests for v0.3.x PR 2 — KernelState.context slot + v6 → v7 migration.

Covers:

- :class:`KernelStateContextSlot` — slot wired, default empty, view
  exposes it, snapshot_version is 7.
- :class:`SnapshotV6ToV7Migration` — :func:`_migrate_v6_to_v7` is
  idempotent; v6 dicts gain an empty ``context``; v7 round-trips
  through :class:`Journal._state_to_dict` /
  :func:`restore_kernel_state` preserve populated state.
- :class:`SnapshotBackwardCompat` — a v6-shape snapshot still loads
  cleanly through :func:`restore_kernel_state` with an empty
  :class:`ContextState`.
"""

from __future__ import annotations

import unittest

from loom.kernel.context import (
    ContextScope,
    SummaryRecord,
    new_context_state,
)
from loom.kernel.journal import (
    Journal,
    SNAPSHOT_VERSION,
    _migrate_v5_to_v6,
    _migrate_v6_to_v7,
    restore_kernel_state,
)
from loom.kernel.room import RoomConfig, RoomState
from loom.kernel.state import (
    KERNEL_STATE_SCHEMA_VERSION,
    KernelState,
    new_kernel_state,
)


class KernelStateContextSlot(unittest.TestCase):
    def test_snapshot_version_is_7(self):
        self.assertEqual(KERNEL_STATE_SCHEMA_VERSION, 7)
        self.assertEqual(SNAPSHOT_VERSION, 7)

    def test_new_kernel_state_has_empty_context(self):
        st = new_kernel_state(RoomState(config=RoomConfig()))
        self.assertIsNotNone(st.context)
        self.assertEqual(st.context.summaries, {})
        self.assertEqual(st.context.active_summary_by_scope, {})

    def test_view_exposes_context_reference(self):
        st = new_kernel_state(RoomState(config=RoomConfig()))
        view = st.view()
        self.assertIs(view.context, st.context)


class SnapshotV6ToV7Migration(unittest.TestCase):
    def _v6_dict(self) -> dict:
        return {
            "version": 6,
            "room": {
                "room_epoch": 0,
                "topic": None,
                "anchor_id": None,
                "chair_id": None,
                "default_responder_id": None,
                "default_summarizer_id": None,
                "current_user_turn_id": None,
                "last_compacted_event_id": -1,
                "participants": [],
                "control": {
                    "roles": {},
                    "wait_for_user": False,
                    "style": "normal",
                    "turn_order": [],
                    "next_speaker_idx": 0,
                },
            },
            "capabilities": None,
            "budget": None,
            "actors": None,
            "workflow": None,
            "tools": None,
            "kernel_version": 3,
        }

    def test_v6_to_v7_is_additive(self):
        v6 = self._v6_dict()
        v7 = _migrate_v6_to_v7(v6)
        self.assertEqual(v7["version"], 7)
        self.assertIn("context", v7)
        self.assertEqual(v7["context"]["summaries"], {})

    def test_v6_to_v7_is_idempotent(self):
        v7 = _migrate_v6_to_v7(self._v6_dict())
        again = _migrate_v6_to_v7(v7)
        self.assertEqual(again, v7)

    def test_v6_snapshot_restores_to_empty_context(self):
        cfg = RoomConfig()
        # Note: v6 snapshot lacks the "context" key entirely. Both the
        # explicit migration AND the load path itself default to an
        # empty ContextState in that case.
        restored = restore_kernel_state(self._v6_dict(), cfg)
        self.assertEqual(restored.context.summaries, {})
        self.assertEqual(restored.context.active_summary_by_scope, {})


class V7RoundTripPreservesContext(unittest.TestCase):
    def test_round_trip_through_state_to_dict_and_restore(self):
        cfg = RoomConfig()
        st = new_kernel_state(RoomState(config=cfg))
        scope = ContextScope(room_id="r1")
        rec = SummaryRecord(
            summary_id="s1",
            scope=scope,
            covers_event_range=(0, 9),
            text="hi",
            input_event_ranges=((0, 9),),
            model_id="m",
            prompt_hash="h",
            summarizer_id="loom",
            proposed_at_event_id=10,
            committed_at_event_id=11,
        )
        st.context.summaries[rec.summary_id] = rec
        st.context.active_summary_by_scope[scope] = rec.summary_id
        st.context.supersession_edges["s0"] = "s1"
        st.context.failure_count[("loom", scope.as_tuple())] = 2

        d = Journal._state_to_dict(st)
        self.assertEqual(d["version"], 7)
        rt = restore_kernel_state(d, cfg)
        self.assertEqual(rt.context.summaries["s1"].covers_event_range, (0, 9))
        self.assertEqual(rt.context.active_summary_by_scope[scope], "s1")
        self.assertEqual(rt.context.supersession_edges["s0"], "s1")
        self.assertEqual(
            rt.context.failure_count[("loom", scope.as_tuple())], 2
        )


if __name__ == "__main__":
    unittest.main()
