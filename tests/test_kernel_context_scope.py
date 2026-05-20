"""Tests for v0.3.x PR 1 — ContextScope dataclass + module home.

Doctrine: P21 / §3.1. ContextScope is the addressing key for
compaction state (PR 2 hangs ContextState off it).
"""

from __future__ import annotations

import json
import unittest

from loom.kernel.context import ContextScope


class ContextScopeBasics(unittest.TestCase):
    def test_default_thread_id_is_main(self):
        scope = ContextScope(room_id="r1")
        self.assertEqual(scope.thread_id, "main")
        self.assertIsNone(scope.actor_id)

    def test_explicit_thread_and_actor(self):
        scope = ContextScope(room_id="r1", thread_id="t9", actor_id="a")
        self.assertEqual(scope.room_id, "r1")
        self.assertEqual(scope.thread_id, "t9")
        self.assertEqual(scope.actor_id, "a")

    def test_is_hashable(self):
        scope = ContextScope(room_id="r1", thread_id="t9")
        d: dict = {scope: 1}
        self.assertEqual(d[scope], 1)

    def test_equality_uses_all_fields(self):
        a = ContextScope(room_id="r1", thread_id="main")
        b = ContextScope(room_id="r1", thread_id="main")
        c = ContextScope(room_id="r1", thread_id="t1")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_frozen_cannot_mutate(self):
        scope = ContextScope(room_id="r1")
        with self.assertRaises(Exception):
            scope.thread_id = "other"  # type: ignore[misc]

    def test_as_tuple_roundtrips_through_json(self):
        scope = ContextScope(room_id="r1", thread_id="t1", actor_id="a")
        s = json.dumps(list(scope.as_tuple()))
        rt = json.loads(s)
        self.assertEqual(rt, ["r1", "t1", "a"])


if __name__ == "__main__":
    unittest.main()
