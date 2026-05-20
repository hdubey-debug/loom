"""Tests for ``loom.kernel.causality`` — v0.3 PR 4 typed causality + trace.

Doctrine: P11 (typed causal graph), P12 (trace metadata on every event),
§8 (causal refs & trace). Closes v0.2.1 audit deferral C3 typed form.

Three concerns:

- :class:`CausalityTypes` — EventRef / CausalRef / CausalRelation
  round-trip, equality, JSON shape.
- :class:`TraceContextLifecycle` — root span allocation, child span
  inheritance, JSON round-trip.
- :class:`EnvelopeIntegration` — :class:`loom.kernel.events.Event`
  carries typed ``causal_refs`` and ``trace``; round-trips through
  ``to_jsonl/from_jsonl``; legacy lines without the keys load with
  defaults.
"""

from __future__ import annotations

import json
import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.causality import (
    CausalRef,
    CausalRelation,
    EventRef,
    TraceContext,
    child_span,
    coerce_causal_refs,
    coerce_trace,
    new_trace,
)
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.events import Event, EventShapeError
from loom.kernel.room import RoomConfig, RoomState


class CausalityTypes(unittest.TestCase):
    def test_event_ref_equality(self):
        a = EventRef(room_id="r1", event_id=5, event_type="chat")
        b = EventRef(room_id="r1", event_id=5, event_type="chat")
        self.assertEqual(a, b)
        c = EventRef(room_id="r1", event_id=6, event_type="chat")
        self.assertNotEqual(a, c)

    def test_event_ref_round_trip(self):
        a = EventRef(room_id="r1", event_id=5, event_type="chat")
        self.assertEqual(EventRef.from_jsonable(a.to_jsonable()), a)

    def test_causal_relation_is_str_enum(self):
        # Subclassing str means equality interops with raw strings.
        self.assertEqual(CausalRelation.RESPONDS_TO, "responds_to")
        self.assertEqual(CausalRelation.CONTROL_ACTION_APPLIED.value, "control_action_applied")

    def test_causal_relation_has_seven_members(self):
        # PR 4 ships the seven predicates listed in doctrine §8.
        self.assertEqual(len(list(CausalRelation)), 7)

    def test_causal_ref_round_trip_with_note(self):
        r = CausalRef(
            ref=EventRef(room_id="r1", event_id=12, event_type="chat"),
            relation=CausalRelation.RESPONDS_TO,
            note="claude_code answers the user post",
        )
        d = r.to_jsonable()
        self.assertEqual(d["relation"], "responds_to")
        self.assertEqual(d["note"], "claude_code answers the user post")
        self.assertEqual(CausalRef.from_jsonable(d), r)

    def test_causal_ref_round_trip_without_note(self):
        r = CausalRef(
            ref=EventRef(room_id="r1", event_id=12, event_type="chat"),
            relation=CausalRelation.TOOL_RESULT_FOR,
        )
        d = r.to_jsonable()
        self.assertNotIn("note", d)
        self.assertEqual(CausalRef.from_jsonable(d), r)

    def test_causal_ref_unknown_relation_raises(self):
        with self.assertRaises(ValueError):
            CausalRef.from_jsonable(
                {
                    "ref": {"room_id": "r1", "event_id": 1, "event_type": "chat"},
                    "relation": "made_up_predicate",
                }
            )

    def test_coerce_causal_refs_passthrough_and_dict_list(self):
        ref_dict = {
            "ref": {"room_id": "r1", "event_id": 1, "event_type": "chat"},
            "relation": "responds_to",
        }
        typed = CausalRef(
            ref=EventRef(room_id="r1", event_id=1, event_type="chat"),
            relation=CausalRelation.RESPONDS_TO,
        )
        # Empty input.
        self.assertEqual(coerce_causal_refs(()), ())
        self.assertEqual(coerce_causal_refs(None), ())
        # Passthrough.
        self.assertEqual(coerce_causal_refs((typed,)), (typed,))
        # Dict list (JSON-loaded).
        self.assertEqual(coerce_causal_refs([ref_dict]), (typed,))


class TraceContextLifecycle(unittest.TestCase):
    def test_new_trace_allocates_distinct_ids(self):
        t = new_trace()
        self.assertEqual(len(t.trace_id), 32)
        self.assertEqual(len(t.span_id), 32)
        self.assertNotEqual(t.trace_id, t.span_id)
        self.assertIsNone(t.parent_span_id)

    def test_child_span_inherits_trace_and_links_parent(self):
        root = new_trace()
        child = child_span(root)
        self.assertEqual(child.trace_id, root.trace_id)
        self.assertNotEqual(child.span_id, root.span_id)
        self.assertEqual(child.parent_span_id, root.span_id)

    def test_trace_round_trip(self):
        root = new_trace()
        child = child_span(root)
        for tc in (root, child):
            self.assertEqual(TraceContext.from_jsonable(tc.to_jsonable()), tc)

    def test_trace_from_jsonable_rejects_missing_ids(self):
        with self.assertRaises(ValueError):
            TraceContext.from_jsonable({"span_id": "abc"})
        with self.assertRaises(ValueError):
            TraceContext.from_jsonable({"trace_id": "abc"})

    def test_coerce_trace_handles_none_dict_and_passthrough(self):
        self.assertIsNone(coerce_trace(None))
        root = new_trace()
        self.assertEqual(coerce_trace(root), root)
        self.assertEqual(coerce_trace(root.to_jsonable()), root)


class EnvelopeIntegration(unittest.TestCase):
    """``Event.causal_refs`` and ``Event.trace`` are first-class fields."""

    def test_event_carries_empty_causal_refs_by_default(self):
        e = ev.chat(sender="user", body="hi")
        self.assertEqual(e.causal_refs, ())
        self.assertIsNone(e.trace)

    def test_event_with_typed_causal_refs_round_trips(self):
        ref = CausalRef(
            ref=EventRef(room_id="r1", event_id=3, event_type="chat"),
            relation=CausalRelation.RESPONDS_TO,
        )
        e = ev.chat(sender="loom", body="answering")
        e.causal_refs = (ref,)
        e.id, e.ts = 1, 1.0
        line = e.to_jsonl()
        loaded = Event.from_jsonl(line)
        self.assertEqual(loaded.causal_refs, (ref,))

    def test_event_with_trace_round_trips(self):
        tc = new_trace()
        e = ev.chat(sender="loom", body="answering")
        e.trace = tc
        e.id, e.ts = 2, 2.0
        line = e.to_jsonl()
        loaded = Event.from_jsonl(line)
        self.assertEqual(loaded.trace, tc)

    def test_legacy_v021_line_without_trace_loads_with_none(self):
        # v0.2.1 events.jsonl lines: have schema_version + causal_refs
        # (PR 3 reservation) but no trace key. They must continue to
        # load with trace=None.
        line = json.dumps(
            {
                "kind": "chat",
                "sender": "user",
                "body": "hello",
                "channel": "main",
                "addressees": [],
                "room_epoch": 0,
                "user_turn_id": None,
                "meta": {},
                "id": 7,
                "ts": 12.5,
                "schema_version": 1,
                "causal_refs": [],
            }
        )
        loaded = Event.from_jsonl(line)
        self.assertIsNone(loaded.trace)
        self.assertEqual(loaded.causal_refs, ())

    def test_legacy_v020_line_without_either_field_still_loads(self):
        line = json.dumps(
            {
                "kind": "chat",
                "sender": "user",
                "body": "ancient",
                "channel": "main",
                "addressees": [],
                "room_epoch": 0,
                "user_turn_id": None,
                "meta": {},
                "id": 0,
                "ts": 1.0,
            }
        )
        loaded = Event.from_jsonl(line)
        self.assertEqual(loaded.causal_refs, ())
        self.assertIsNone(loaded.trace)
        self.assertEqual(loaded.schema_version, 1)

    def test_trace_must_be_dict_or_null(self):
        line = json.dumps(
            {
                "kind": "chat",
                "sender": "user",
                "body": "x",
                "channel": "main",
                "addressees": [],
                "room_epoch": 0,
                "user_turn_id": None,
                "meta": {},
                "id": 0,
                "ts": 1.0,
                "trace": "not a dict",
            }
        )
        with self.assertRaises(EventShapeError):
            Event.from_jsonl(line)

    def test_causal_refs_with_unknown_relation_rejected_at_load(self):
        # An on-disk line with an unrecognised relation must surface as
        # a load-time error (caught by replay's corruption path).
        line = json.dumps(
            {
                "kind": "chat",
                "sender": "user",
                "body": "x",
                "channel": "main",
                "addressees": [],
                "room_epoch": 0,
                "user_turn_id": None,
                "meta": {},
                "id": 0,
                "ts": 1.0,
                "causal_refs": [
                    {
                        "ref": {"room_id": "r1", "event_id": 1, "event_type": "chat"},
                        "relation": "made_up",
                    },
                ],
            }
        )
        with self.assertRaises(ValueError):
            Event.from_jsonl(line)


class CoordinatorTraceRoot(unittest.TestCase):
    """Coordinator allocates a trace root and exposes child-span helper."""

    def _coord(self) -> RoomCoordinator:
        bus = MessageBus()
        return RoomCoordinator(bus, RoomState(config=RoomConfig()))

    def test_trace_root_allocated_at_construction(self):
        coord = self._coord()
        self.assertIsInstance(coord.trace_root, TraceContext)
        self.assertIsNone(coord.trace_root.parent_span_id)

    def test_new_child_span_inherits_trace_id(self):
        coord = self._coord()
        child = coord.new_child_span()
        self.assertEqual(child.trace_id, coord.trace_root.trace_id)
        self.assertEqual(child.parent_span_id, coord.trace_root.span_id)
        self.assertNotEqual(child.span_id, coord.trace_root.span_id)

    def test_independent_coordinators_get_independent_traces(self):
        a = self._coord()
        b = self._coord()
        self.assertNotEqual(a.trace_root.trace_id, b.trace_root.trace_id)


if __name__ == "__main__":
    unittest.main()
