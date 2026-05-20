"""Tests for ``loom.kernel.state`` — v0.3 PR 1 KernelState transactional root.

Doctrine: P5 (transactional KernelState root) + §1 KernelState architecture.

Two test classes:

- :class:`KernelStateBasics` exercises construction, ``bump_version``,
  ``view()`` immutability, and reserved-slot defaults.
- :class:`SnapshotMigration` exercises the v5→v6 envelope migration via
  :func:`loom.kernel.journal.restore_kernel_state`, the v6 round-trip,
  and the v6-shape sibling-slot serialization.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from loom.kernel.journal import (
    Journal,
    SNAPSHOT_VERSION,
    _migrate_v5_to_v6,
    restore_kernel_state,
    restore_state,
)
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
    RoomStateView,
)
from loom.kernel.state import (
    KERNEL_STATE_SCHEMA_VERSION,
    KernelState,
    KernelStateView,
    new_kernel_state,
)


def _fresh_room() -> RoomState:
    s = RoomState(config=RoomConfig())
    s.add_participant(ParticipantInfo(id="loom", cost_tier=0))
    s.add_participant(ParticipantInfo(id="claude_code", cost_tier=2))
    s.set_default_responder("claude_code")
    s.set_topic("derivatives lesson")
    return s


class KernelStateBasics(unittest.TestCase):
    def test_construct_wraps_room(self):
        room = _fresh_room()
        kernel = KernelState(room=room)
        self.assertIs(kernel.room, room)
        self.assertEqual(kernel.schema_version, KERNEL_STATE_SCHEMA_VERSION)
        self.assertEqual(kernel.version, 0)

    def test_factory_helper(self):
        room = _fresh_room()
        kernel = new_kernel_state(room)
        self.assertIs(kernel.room, room)
        self.assertEqual(kernel.version, 0)

    def test_reserved_subsystem_slots_default_to_none(self):
        kernel = new_kernel_state(_fresh_room())
        # PR 5/6/13 slots; PR 1 leaves them as None.
        self.assertIsNone(kernel.capabilities)
        self.assertIsNone(kernel.budget)
        self.assertIsNone(kernel.actors)
        # Post-v0.3 reserved slots.
        self.assertIsNone(kernel.workflow)
        self.assertIsNone(kernel.tools)

    def test_bump_version_increments_and_returns(self):
        kernel = new_kernel_state(_fresh_room())
        self.assertEqual(kernel.bump_version(), 1)
        self.assertEqual(kernel.bump_version(), 2)
        self.assertEqual(kernel.bump_version(), 3)
        self.assertEqual(kernel.version, 3)

    def test_view_freezes_room_into_room_state_view(self):
        kernel = new_kernel_state(_fresh_room())
        view = kernel.view()
        self.assertIsInstance(view, KernelStateView)
        self.assertIsInstance(view.room, RoomStateView)
        # KernelStateView is frozen — attribute reassignment must raise.
        with self.assertRaises(Exception):
            view.version = 999  # type: ignore[misc]

    def test_view_captures_version_at_call_time(self):
        kernel = new_kernel_state(_fresh_room())
        kernel.bump_version()
        kernel.bump_version()
        view_a = kernel.view()
        self.assertEqual(view_a.version, 2)
        kernel.bump_version()
        # The earlier view is frozen at 2; the next view reflects 3.
        self.assertEqual(view_a.version, 2)
        self.assertEqual(kernel.view().version, 3)

    def test_view_schema_version_round_trips(self):
        kernel = new_kernel_state(_fresh_room())
        self.assertEqual(kernel.view().schema_version, KERNEL_STATE_SCHEMA_VERSION)

    def test_room_view_participants_is_read_only_proxy(self):
        kernel = new_kernel_state(_fresh_room())
        view = kernel.view()
        self.assertIsInstance(view.room.participants, MappingProxyType)


class SnapshotMigration(unittest.TestCase):
    """v5→v6 envelope migration + v6 round-trip."""

    def _v5_dict(self) -> dict:
        return {
            "version": 5,
            "room_epoch": 7,
            "topic": "stale topic",
            "anchor_id": None,
            "chair_id": None,
            "default_responder_id": "loom",
            "default_summarizer_id": None,
            "current_user_turn_id": None,
            "last_compacted_event_id": 42,
            "participants": [
                {
                    "id": "loom",
                    "capable": True,
                    "cost_tier": 0,
                    "active": True,
                    "role_hints": {},
                },
            ],
            "control": {
                "roles": {"loom": "teacher"},
                "wait_for_user": True,
                "style": "brief",
                "turn_order": ["loom"],
                "next_speaker_idx": 0,
            },
        }

    def test_migrator_wraps_v5_under_room_key(self):
        v5 = self._v5_dict()
        v6 = _migrate_v5_to_v6(v5)
        # The v5→v6 migrator is a single step in the chain; it always
        # outputs a v6 dict (the v6→v7 migrator picks up from there).
        self.assertEqual(v6["version"], 6)
        # Inner v5 fields nest under "room".
        self.assertEqual(v6["room"]["room_epoch"], 7)
        self.assertEqual(v6["room"]["topic"], "stale topic")
        self.assertEqual(v6["room"]["default_responder_id"], "loom")
        # Sibling slots reserved as None.
        for slot in ("capabilities", "budget", "actors", "workflow", "tools"):
            self.assertIsNone(v6[slot])
        # kernel_version defaults to 0 on migration (v5 had no counter).
        self.assertEqual(v6["kernel_version"], 0)

    def test_migrator_is_idempotent_on_v6_input(self):
        v5 = self._v5_dict()
        v6 = _migrate_v5_to_v6(v5)
        v6_again = _migrate_v5_to_v6(v6)
        self.assertIs(v6, v6_again)

    def test_restore_kernel_state_from_v5_dict(self):
        kernel = restore_kernel_state(self._v5_dict(), RoomConfig())
        self.assertIsInstance(kernel, KernelState)
        self.assertEqual(kernel.room.topic, "stale topic")
        self.assertEqual(kernel.room.default_responder_id, "loom")
        self.assertIn("loom", kernel.room.participants)
        # v5 had no kernel_version; KernelState.version defaults to 0.
        self.assertEqual(kernel.version, 0)
        # Reserved subsystem slots stay None on restore (their owning
        # PRs hydrate them).
        self.assertIsNone(kernel.capabilities)
        self.assertIsNone(kernel.budget)

    def test_restore_kernel_state_from_v6_round_trip(self):
        room = _fresh_room()
        kernel = new_kernel_state(room)
        kernel.bump_version()
        kernel.bump_version()  # version == 2

        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir)
            j.snapshot(kernel)
            data = j.load_state()
            self.assertIsNotNone(data)
            # v0.3.x PR 2 bumped the envelope to v7 (adds "context").
            self.assertEqual(data["version"], SNAPSHOT_VERSION)
            self.assertEqual(data["version"], 7)
            # Sibling slots present in serialized form.
            for slot in ("capabilities", "budget", "actors", "workflow", "tools"):
                self.assertIn(slot, data)
                self.assertIsNone(data[slot])
            # ContextState slot is also present (empty in this test).
            self.assertIn("context", data)
            self.assertEqual(data["kernel_version"], 2)

            restored = restore_kernel_state(data, RoomConfig())
            self.assertEqual(restored.room.topic, room.topic)
            self.assertEqual(restored.room.default_responder_id, "claude_code")
            self.assertEqual(restored.version, 2)

    def test_restore_state_back_compat_consumes_v6(self):
        """v0.2-era ``restore_state`` callers continue to work post-PR 1."""
        room = _fresh_room()
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir)
            j.snapshot(new_kernel_state(room))
            data = j.load_state()
            restored = restore_state(data, RoomConfig())
            # restore_state still returns a RoomState; it transparently
            # unwraps the v6 envelope to find the room sub-dict.
            self.assertIsInstance(restored, RoomState)
            self.assertEqual(restored.topic, room.topic)
            self.assertEqual(restored.default_responder_id, "claude_code")

    def test_snapshot_accepts_bare_roomstate_back_compat(self):
        """Existing ``j.snapshot(roomstate)`` callers keep working."""
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir)
            j.snapshot(_fresh_room())  # passes RoomState, not KernelState
            data = json.loads((Path(tmpdir) / "room_state.json").read_text())
            self.assertEqual(data["version"], SNAPSHOT_VERSION)
            self.assertEqual(data["room"]["default_responder_id"], "claude_code")
            # Wrapped via new_kernel_state — kernel_version starts at 0.
            self.assertEqual(data["kernel_version"], 0)


if __name__ == "__main__":
    unittest.main()
