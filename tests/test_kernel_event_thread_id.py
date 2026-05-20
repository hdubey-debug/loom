"""Tests for v0.3.x PR 1 — Event.thread_id envelope field.

Doctrine: P21 — thread membership is a first-class envelope field;
agents never set it directly. Defaults to ``"main"``; old journals
without the field load as ``"main"``. The bus rejects any event
with empty / non-str ``thread_id``.
"""

from __future__ import annotations

import json
import unittest

from loom.kernel import events as ev
from loom.kernel.bus import _KERNEL_AUTH, MessageBus
from loom.kernel.events import Event, EventShapeError


class EventThreadIdDefault(unittest.TestCase):
    def test_chat_default_thread_id_is_main(self):
        e = ev.chat(sender="claude_code", body="hello")
        self.assertEqual(e.thread_id, "main")

    def test_system_default_thread_id_is_main(self):
        e = ev.system("boot")
        self.assertEqual(e.thread_id, "main")

    def test_summary_default_thread_id_is_main(self):
        e = ev.summary("rolling")
        self.assertEqual(e.thread_id, "main")

    def test_control_default_thread_id_is_main(self):
        e = ev.topic_changed(None, "first topic")
        self.assertEqual(e.thread_id, "main")

    def test_explicit_thread_id_propagates(self):
        e = Event(kind="chat", sender="u", body="hi", thread_id="debate-1")
        self.assertEqual(e.thread_id, "debate-1")


class EventThreadIdRoundTrip(unittest.TestCase):
    def test_to_jsonl_emits_thread_id(self):
        e = Event(kind="chat", sender="u", body="hi", thread_id="t9")
        d = json.loads(e.to_jsonl())
        self.assertEqual(d["thread_id"], "t9")

    def test_from_jsonl_round_trip(self):
        e = Event(kind="chat", sender="u", body="hi", thread_id="t9")
        rt = Event.from_jsonl(e.to_jsonl())
        self.assertEqual(rt.thread_id, "t9")

    def test_legacy_line_without_thread_id_loads_as_main(self):
        # Construct a legacy v0.3 line: serialize, strip thread_id,
        # reload — should default to "main".
        e = Event(kind="chat", sender="u", body="hi")
        d = json.loads(e.to_jsonl())
        d.pop("thread_id")
        rt = Event.from_jsonl(json.dumps(d))
        self.assertEqual(rt.thread_id, "main")

    def test_empty_thread_id_in_jsonl_is_rejected(self):
        e = Event(kind="chat", sender="u", body="hi")
        d = json.loads(e.to_jsonl())
        d["thread_id"] = ""
        with self.assertRaises(EventShapeError):
            Event.from_jsonl(json.dumps(d))

    def test_nonstring_thread_id_in_jsonl_is_rejected(self):
        e = Event(kind="chat", sender="u", body="hi")
        d = json.loads(e.to_jsonl())
        d["thread_id"] = 123
        with self.assertRaises(EventShapeError):
            Event.from_jsonl(json.dumps(d))


class BusThreadIdAssertion(unittest.TestCase):
    def _bus(self) -> MessageBus:
        return MessageBus()

    def test_default_event_posts_cleanly(self):
        b = self._bus()
        e = ev.chat(sender="u", body="hi")
        b.post_internal(e, auth=_KERNEL_AUTH)
        self.assertEqual(len(b.snapshot()), 1)

    def test_explicit_thread_id_posts_cleanly(self):
        b = self._bus()
        e = Event(kind="chat", sender="u", body="hi", thread_id="t9")
        b.post_internal(e, auth=_KERNEL_AUTH)
        self.assertEqual(b.snapshot()[0].thread_id, "t9")

    def test_empty_thread_id_rejected_by_bus(self):
        b = self._bus()
        e = Event(kind="chat", sender="u", body="hi", thread_id="")
        with self.assertRaises(ValueError):
            b.post_internal(e, auth=_KERNEL_AUTH)

    def test_none_thread_id_rejected_by_bus(self):
        b = self._bus()
        e = Event(kind="chat", sender="u", body="hi")
        e.thread_id = None  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            b.post_internal(e, auth=_KERNEL_AUTH)


if __name__ == "__main__":
    unittest.main()
