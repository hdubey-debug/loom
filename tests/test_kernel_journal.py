"""Tests for ``loom.kernel.journal`` — events.jsonl + room_state.json."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.journal import (
    SNAPSHOT_VERSION,
    Journal,
    restore_state,
)
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
)


class JournalLifecycle(unittest.TestCase):
    def test_open_creates_dir_and_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session = Path(tmpdir) / "sess1"
            j = Journal(session)
            self.assertTrue(session.exists())
            j.open()
            j.close()

    def test_drops_events_when_not_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir)
            j.on_event(ev.chat(sender="user", body="hi"))
            self.assertFalse(j.events_path.exists())


class EventAppend(unittest.TestCase):
    def test_each_event_one_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with Journal(tmpdir) as j:
                e1 = ev.chat(sender="user", body="one")
                e2 = ev.chat(sender="user", body="two")
                e1.id, e1.ts = 0, 1.0
                e2.id, e2.ts = 1, 2.0
                j.on_event(e1)
                j.on_event(e2)
            lines = (Path(tmpdir) / "events.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["body"], "one")
            self.assertEqual(json.loads(lines[1])["body"], "two")

    def test_subscribed_to_bus(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bus = MessageBus()
            with Journal(tmpdir) as j:
                bus.subscribe(j.on_event)
                bus.post(ev.chat(sender="user", body="hi"))
                bus.post(ev.system("started"))
            lines = (Path(tmpdir) / "events.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 2)


class StateSnapshot(unittest.TestCase):
    def _state(self):
        s = RoomState(config=RoomConfig())
        s.add_participant(ParticipantInfo(id="loom", cost_tier=0))
        s.add_participant(ParticipantInfo(id="claude_code", cost_tier=2))
        s.set_default_responder("claude_code")
        s.set_topic("god's existence")
        s.last_compacted_event_id = 42
        return s

    def test_snapshot_writes_json_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir)
            j.snapshot(self._state())
            data = json.loads((Path(tmpdir) / "room_state.json").read_text())
            self.assertEqual(data["version"], SNAPSHOT_VERSION)
            self.assertEqual(data["default_responder_id"], "claude_code")
            self.assertEqual(data["topic"], "god's existence")
            self.assertEqual(data["last_compacted_event_id"], 42)
            self.assertEqual({p["id"] for p in data["participants"]}, {"loom", "claude_code"})
            # Mode/debate keys must NOT appear in v2 snapshots.
            self.assertNotIn("mode", data)
            self.assertNotIn("debate", data)

    def test_snapshot_atomic_no_partial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir)
            s1 = self._state()
            s1.set_topic("first")
            j.snapshot(s1)
            s2 = self._state()
            s2.set_topic("second")
            j.snapshot(s2)
            data = json.loads((Path(tmpdir) / "room_state.json").read_text())
            self.assertEqual(data["topic"], "second")
            tmp = Path(tmpdir) / "room_state.json.tmp"
            self.assertFalse(tmp.exists())

    def test_load_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir)
            self.assertIsNone(j.load_state())

    def test_load_returns_none_on_corrupt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir)
            (Path(tmpdir) / "room_state.json").write_text("{ not json")
            self.assertIsNone(j.load_state())

    def test_load_returns_none_on_far_future_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir)
            (Path(tmpdir) / "room_state.json").write_text('{"version": 999}')
            self.assertIsNone(j.load_state())

    def test_load_accepts_v1_snapshot(self):
        """v1 snapshots (legacy mode/debate world) are still readable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "room_state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mode": "council",  # legacy: silently ignored on restore
                        "room_epoch": 4,
                        "topic": "stale topic",
                        "anchor_id": None,
                        "chair_id": None,
                        "default_responder_id": "loom",
                        "default_summarizer_id": None,
                        "current_user_turn_id": None,
                        "last_compacted_event_id": 12,
                        "participants": [
                            {
                                "id": "loom",
                                "capable": True,
                                "cost_tier": 0,
                                "active": True,
                                "role_hints": {},
                            },
                        ],
                        "debate": {
                            "next_side": "pro",
                            "round": 1,
                            "max_rounds": 6,
                            "pro_side": [],
                            "con_side": [],
                            "consecutive_forfeits": 0,
                            "spoke_this_round": [],
                        },
                    }
                )
            )
            j = Journal(tmpdir)
            data = j.load_state()
            self.assertIsNotNone(data)
            restored = restore_state(data, RoomConfig())
            # Mode key is silently dropped — RoomState has no mode field.
            self.assertFalse(hasattr(restored, "mode"))
            self.assertEqual(restored.topic, "stale topic")
            self.assertEqual(restored.default_responder_id, "loom")
            self.assertIn("loom", restored.participants)

    def test_round_trip_via_restore_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir)
            original = self._state()
            j.snapshot(original)
            data = j.load_state()
            self.assertIsNotNone(data)
            restored = restore_state(data, RoomConfig())
            self.assertEqual(restored.topic, original.topic)
            self.assertEqual(restored.default_responder_id, original.default_responder_id)
            self.assertEqual(set(restored.participants.keys()), set(original.participants.keys()))


class LoadEvents(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with Journal(tmpdir) as j:
                e = ev.chat(sender="user", body="hi", addressees=["claude_code"])
                e.id, e.ts = 0, 1.0
                j.on_event(e)
                ce = ev.topic_changed(None, "weather")
                ce.id, ce.ts = 1, 2.0
                j.on_event(ce)
            j2 = Journal(tmpdir)
            evs = j2.load_events()
            self.assertEqual(len(evs), 2)
            self.assertEqual(evs[0].body, "hi")
            self.assertEqual(evs[0].addressees, ["claude_code"])
            self.assertEqual(evs[1].kind, "control")

    def test_skips_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "events.jsonl").write_text(
                '{"kind":"chat","sender":"user","body":"good","channel":"main",'
                '"addressees":[],"room_epoch":0,"user_turn_id":null,"meta":{},'
                '"id":0,"ts":1.0}\n'
                "not json\n"
                '{"kind":"chat","sender":"user","body":"good2","channel":"main",'
                '"addressees":[],"room_epoch":0,"user_turn_id":null,"meta":{},'
                '"id":1,"ts":2.0}\n'
            )
            j = Journal(tmpdir)
            evs = j.load_events()
            self.assertEqual(len(evs), 2)
            self.assertEqual(evs[0].body, "good")
            self.assertEqual(evs[1].body, "good2")

    def test_legacy_control_events_load_but_can_be_filtered(self):
        """Old v1 sessions wrote ``mode_changed`` / ``debate_turn`` lines.

        Those lines deserialize cleanly into Event() — they're just
        control events with retired control_types. The journal returns
        them; downstream filters via ``is_known_control``.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "events.jsonl").write_text(
                # A retired control_type.
                '{"kind":"control","sender":"system",'
                '"body":{"control_type":"mode_changed","old":"normal",'
                '"new":"council"},"channel":"main","addressees":[],'
                '"room_epoch":0,"user_turn_id":null,"meta":{},'
                '"id":0,"ts":1.0}\n'
                # A current control_type.
                '{"kind":"control","sender":"system",'
                '"body":{"control_type":"topic_changed","old":null,'
                '"new":"x"},"channel":"main","addressees":[],'
                '"room_epoch":0,"user_turn_id":null,"meta":{},'
                '"id":1,"ts":2.0}\n'
            )
            j = Journal(tmpdir)
            evs = j.load_events()
            self.assertEqual(len(evs), 2)
            # The retired one is_known_control() = False.
            self.assertFalse(ev.is_known_control(evs[0]))
            self.assertTrue(ev.is_known_control(evs[1]))


class RoomControlStateRoundTrip(unittest.TestCase):
    """RoomControlState (roles/floor/wait/style/goal) survives snapshot."""

    def test_full_control_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir)
            cfg = RoomConfig()
            original = RoomState(config=cfg)
            original.add_participant(ParticipantInfo(id="loom"))
            original.add_participant(ParticipantInfo(id="claude_code"))
            original.set_roles({"loom": "teacher", "claude_code": "quizzer"})
            original.set_wait_for_user(True)
            original.set_style("brief")
            original.set_topic("teach derivatives")
            j.snapshot(original)

            data = j.load_state()
            restored = restore_state(data, cfg)
            self.assertEqual(restored.control.roles, {"loom": "teacher", "claude_code": "quizzer"})
            self.assertTrue(restored.control.wait_for_user)
            self.assertEqual(restored.control.style, "brief")
            self.assertEqual(restored.topic, "teach derivatives")

    def test_legacy_v1_snapshot_without_control_loads_with_defaults(self):
        """v1 snapshots predate ``control``; they must still load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "room_state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "room_epoch": 0,
                        "topic": None,
                        "anchor_id": None,
                        "chair_id": None,
                        "default_responder_id": None,
                        "default_summarizer_id": None,
                        "current_user_turn_id": None,
                        "last_compacted_event_id": -1,
                        "participants": [],
                    }
                )
            )
            j = Journal(tmpdir)
            data = j.load_state()
            restored = restore_state(data, RoomConfig())
            self.assertEqual(restored.control.roles, {})
            self.assertEqual(restored.control.style, "normal")

    def test_round_robin_state_roundtrip(self):
        """Round-robin turn order + pointer survive snapshot."""
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir)
            cfg = RoomConfig()
            original = RoomState(config=cfg)
            original.add_participant(ParticipantInfo(id="loom"))
            original.add_participant(ParticipantInfo(id="claude_code"))
            original.set_turn_order(["loom", "claude_code"])
            original.advance_round_robin_pointer()  # idx -> 1
            j.snapshot(original)

            restored = restore_state(j.load_state(), cfg)
            # v5 schema: a non-empty turn_order is the round-robin signal.
            self.assertEqual(restored.control.turn_order, ["loom", "claude_code"])
            self.assertEqual(restored.control.next_speaker_idx, 1)

    def test_v2_snapshot_loads_round_robin_defaults(self):
        """v2 snapshots predate the round-robin fields — defaults apply."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "room_state.json").write_text(
                json.dumps(
                    {
                        "version": 2,
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
                            "floor_owner": None,
                            "wait_for_user": False,
                            "style": "normal",
                            "active_goal": None,
                        },
                    }
                )
            )
            j = Journal(tmpdir)
            restored = restore_state(j.load_state(), RoomConfig())
            self.assertEqual(restored.control.turn_order, [])
            self.assertEqual(restored.control.next_speaker_idx, 0)

    def test_corrupt_round_robin_fields_fall_back_to_defaults(self):
        """Malformed round-robin fields must not crash restore."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "room_state.json").write_text(
                json.dumps(
                    {
                        "version": 3,
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
                            "floor_owner": None,
                            "wait_for_user": False,
                            "style": "normal",
                            "active_goal": None,
                            "turn_taking_mode": "garbage",
                            "turn_order": "not a list",
                            "next_speaker_idx": "not an int",
                        },
                    }
                )
            )
            j = Journal(tmpdir)
            restored = restore_state(j.load_state(), RoomConfig())
            # v3/v4 snapshots carrying the retired turn_taking_mode field
            # are tolerated; malformed turn_order / next_speaker_idx
            # default to empty / 0.
            self.assertEqual(restored.control.turn_order, [])
            self.assertEqual(restored.control.next_speaker_idx, 0)

    def test_v3_snapshot_with_round_robin_mode_carries_turn_order(self):
        """v3 snapshots with `turn_taking_mode=round_robin` restore via turn_order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "room_state.json").write_text(
                json.dumps(
                    {
                        "version": 3,
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
                            "floor_owner": None,
                            "wait_for_user": False,
                            "style": "normal",
                            "turn_taking_mode": "round_robin",
                            "turn_order": ["a", "b"],
                            "next_speaker_idx": 1,
                        },
                    }
                )
            )
            j = Journal(tmpdir)
            restored = restore_state(j.load_state(), RoomConfig())
            # The mode field is discarded; turn_order alone signals RR.
            self.assertEqual(restored.control.turn_order, ["a", "b"])
            self.assertEqual(restored.control.next_speaker_idx, 1)

    def test_corrupt_control_dict_falls_back_to_defaults(self):
        """If ``control`` is malformed, restore_state must not crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "room_state.json").write_text(
                json.dumps(
                    {
                        "version": 2,
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
                            "roles": "not a dict",
                            "floor_owner": "not a list",
                            "style": "ultra-brief",  # invalid
                            "wait_for_user": True,
                            "active_goal": None,
                        },
                    }
                )
            )
            j = Journal(tmpdir)
            data = j.load_state()
            restored = restore_state(data, RoomConfig())
            self.assertEqual(restored.control.roles, {})
            self.assertEqual(restored.control.style, "normal")


class SnapshotCallback(unittest.TestCase):
    def test_callback_fires_after_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls = []
            with Journal(tmpdir, snapshot_every_events=3) as j:
                j.set_snapshot_due_callback(lambda: calls.append(True))
                for i in range(5):
                    e = ev.chat(sender="user", body=f"m{i}")
                    e.id, e.ts = i, float(i)
                    j.on_event(e)
            self.assertGreaterEqual(len(calls), 1)

    def test_dict_payload_written_via_background_thread(self):
        # When the callback returns a dict, the journal queues it for
        # the background writer; on close() the queue drains and the
        # snapshot lands on disk.
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = {
                "version": SNAPSHOT_VERSION,
                "room_epoch": 7,
                "topic": "design",
                "anchor_id": None,
                "chair_id": None,
                "default_responder_id": None,
                "default_summarizer_id": None,
                "current_user_turn_id": None,
                "last_compacted_event_id": -1,
                "participants": [],
                "control": {
                    "roles": {},
                    "floor_owner": None,
                    "wait_for_user": False,
                    "style": "normal",
                    "active_goal": None,
                    "turn_taking_mode": "broadcast",
                    "turn_order": [],
                    "next_speaker_idx": 0,
                },
            }
            with Journal(tmpdir, snapshot_every_events=2) as j:
                j.set_snapshot_due_callback(lambda: payload)
                for i in range(2):
                    e = ev.chat(sender="user", body=f"m{i}")
                    e.id, e.ts = i, float(i)
                    j.on_event(e)
            # close() drained the queue, so the snapshot is on disk.
            on_disk = json.loads((Path(tmpdir) / "room_state.json").read_text())
            self.assertEqual(on_disk["room_epoch"], 7)
            self.assertEqual(on_disk["topic"], "design")

    def test_callback_returning_non_dict_skips_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with Journal(tmpdir, snapshot_every_events=2) as j:
                j.set_snapshot_due_callback(lambda: None)
                for i in range(2):
                    e = ev.chat(sender="user", body=f"m{i}")
                    e.id, e.ts = i, float(i)
                    j.on_event(e)
            self.assertFalse((Path(tmpdir) / "room_state.json").exists())

    def test_post_path_does_not_block_on_slow_disk(self):
        # The background writer absorbs disk latency. Even if the
        # snapshot writer is sleeping for hundreds of ms, on_event
        # returns near-instantly.
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            payload = {
                "version": SNAPSHOT_VERSION,
                "room_epoch": 1,
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
                    "floor_owner": None,
                    "wait_for_user": False,
                    "style": "normal",
                    "active_goal": None,
                    "turn_taking_mode": "broadcast",
                    "turn_order": [],
                    "next_speaker_idx": 0,
                },
            }
            with Journal(tmpdir, snapshot_every_events=1) as j:
                # Patch the writer to simulate a slow disk.
                original = j._write_snapshot_dict

                def slow_write(p):
                    time.sleep(0.5)
                    return original(p)

                j._write_snapshot_dict = slow_write  # type: ignore[assignment]
                j.set_snapshot_due_callback(lambda: payload)

                # Trigger the threshold and time the call.
                e = ev.chat(sender="user", body="trigger")
                e.id, e.ts = 0, 0.0
                t0 = time.monotonic()
                j.on_event(e)
                elapsed = time.monotonic() - t0
                self.assertLess(
                    elapsed,
                    0.1,
                    f"on_event blocked for {elapsed:.3f}s — snapshot write should be off-thread",
                )

    def test_close_drains_pending_snapshots(self):
        # Multiple snapshots queued, close() must wait for all to land.
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            payload = {
                "version": SNAPSHOT_VERSION,
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
                    "floor_owner": None,
                    "wait_for_user": False,
                    "style": "normal",
                    "active_goal": None,
                    "turn_taking_mode": "broadcast",
                    "turn_order": [],
                    "next_speaker_idx": 0,
                },
            }
            j = Journal(tmpdir, snapshot_every_events=1)
            j.open()
            try:
                writes = []
                original = j._write_snapshot_dict

                def slow_write(p):
                    time.sleep(0.05)
                    writes.append(p["room_epoch"])
                    return original(p)

                j._write_snapshot_dict = slow_write  # type: ignore[assignment]
                j.set_snapshot_due_callback(lambda: dict(payload, room_epoch=len(writes) + 1))

                # Queue a few snapshots in quick succession.
                for i in range(3):
                    e = ev.chat(sender="user", body=f"m{i}")
                    e.id, e.ts = i, float(i)
                    j.on_event(e)
            finally:
                j.close()
            # close() drained the queue: every snapshot wrote.
            self.assertEqual(len(writes), 3)


class _RaisingFile:
    """Stand-in events_file that raises on every write."""

    def __init__(self) -> None:
        self.write_count = 0

    def write(self, _line: str) -> int:
        self.write_count += 1
        raise OSError("disk full")

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class JournalFailureSurface(unittest.TestCase):
    def test_write_failure_marks_degraded_and_fires_callback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with Journal(tmpdir) as j:
                seen: list[Exception] = []
                j.set_failure_callback(seen.append)
                j._events_file = _RaisingFile()  # type: ignore[assignment]
                j.on_event(ev.chat(sender="user", body="boom"))
            self.assertTrue(j.degraded)
            self.assertEqual(len(seen), 1)
            self.assertIsInstance(seen[0], OSError)

    def test_failure_callback_recursion_guarded(self):
        # The callback itself triggers another bus.post → on_event. The
        # recursion guard must prevent the callback from firing twice
        # (or infinitely) for the same write.
        with tempfile.TemporaryDirectory() as tmpdir:
            with Journal(tmpdir) as j:
                fired = [0]
                raising = _RaisingFile()
                j._events_file = raising  # type: ignore[assignment]

                def cb(exc: Exception) -> None:
                    fired[0] += 1
                    # Simulate the failure-event re-entering on_event.
                    j.on_event(ev.system("recursive write"))

                j.set_failure_callback(cb)
                j.on_event(ev.chat(sender="user", body="boom"))
            self.assertTrue(j.degraded)
            self.assertEqual(fired[0], 1)
            # Both writes hit the file, but the callback only fired once.
            self.assertEqual(raising.write_count, 2)

    def test_session_emits_journal_error_event_on_failure(self):
        # End-to-end via build_loom_session: a write failure surfaces as
        # a ``journal_error`` control event on the bus.
        from loom.runtime import ParticipantWiring, build_loom_session

        class FakeProxy:
            def stream(self, prompt):
                yield "ok"

        with tempfile.TemporaryDirectory() as tmpdir:
            wirings = [ParticipantWiring(id="loom", proxy=FakeProxy(), cost_tier=0)]
            session = build_loom_session(
                wirings,
                journal_dir=tmpdir,
                auto_start=False,
                default_responder_id="loom",
            )
            try:
                # Replace the journal's open file with a raising one.
                session.journal._events_file = _RaisingFile()  # type: ignore[union-attr,assignment]
                session.bus.post(ev.chat(sender="user", body="hi"))
                errs = [
                    e
                    for e in session.bus.snapshot()
                    if e.kind == "control"
                    and isinstance(e.body, dict)
                    and e.body.get("control_type") == "journal_error"
                ]
                self.assertEqual(len(errs), 1)
                self.assertEqual(errs[0].body["exception_class"], "OSError")
                self.assertIn("disk full", errs[0].body["message"])
                self.assertTrue(session.journal.degraded)  # type: ignore[union-attr]
            finally:
                # The journal's file is broken; close() will try to
                # flush+close. Replace with a benign sentinel first.
                session.journal._events_file = None  # type: ignore[union-attr,assignment]
                session.stop()


class RestoreState(unittest.TestCase):
    """Hardening tests for the load + replay path.

    These exercise the boundary conditions where the on-disk journal is
    incomplete (truncated final line), carries retired control_types
    (legacy v1 sessions), or omits keys entirely (older snapshot
    versions).
    """

    def test_truncated_final_line_dropped(self):
        # An events.jsonl whose tail line is partially written (no
        # closing brace, no trailing newline). load_events must skip it
        # silently and return everything before the truncation.
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "events.jsonl").write_text(
                '{"kind":"chat","sender":"user","body":"a","channel":"main",'
                '"addressees":[],"room_epoch":0,"user_turn_id":null,"meta":{},'
                '"id":0,"ts":1.0}\n'
                '{"kind":"chat","sender":"user","body":"b","channel":"main",'
                '"addressees":[],"room_epoch":0,"user_turn_id":null,"meta":{},'
                '"id":1,"ts":2.0}\n'
                # Truncated mid-event — no closing brace, no newline.
                '{"kind":"chat","sender":"user","body":"c","ch'
            )
            j = Journal(tmpdir)
            evs = j.load_events()
            self.assertEqual(len(evs), 2)
            self.assertEqual([e.body for e in evs], ["a", "b"])

    def test_replay_into_skips_retired_control_types(self):
        # ``replay_into`` filters control events whose ``control_type``
        # is no longer in :data:`CONTROL_TYPES`. The body deserializes
        # cleanly (it's just a dict) but should not feed coordinator
        # state.
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "events.jsonl").write_text(
                '{"kind":"control","sender":"system",'
                '"body":{"control_type":"mode_changed","old":"a","new":"b"},'
                '"channel":"main","addressees":[],"room_epoch":0,'
                '"user_turn_id":null,"meta":{},"id":0,"ts":1.0}\n'
                '{"kind":"control","sender":"system",'
                '"body":{"control_type":"topic_changed","old":null,"new":"x"},'
                '"channel":"main","addressees":[],"room_epoch":0,'
                '"user_turn_id":null,"meta":{},"id":1,"ts":2.0}\n'
            )
            j = Journal(tmpdir)
            mock_coord = MagicMock()
            mock_coord.bus = MagicMock()
            posted = j.replay_into(mock_coord)
            # Only the topic_changed event was posted (legacy
            # mode_changed dropped).
            self.assertEqual(posted, 1)
            # P1: replay re-injects via bus.post_internal so that
            # tampered ``sender="user"`` lines on disk go through the
            # privileged path (replay is a documented bypass; see
            # security-model.md).
            self.assertEqual(mock_coord.bus.post_internal.call_count, 1)
            posted_event = mock_coord.bus.post_internal.call_args[0][0]
            self.assertEqual(posted_event.body.get("control_type"), "topic_changed")

    def test_restore_state_missing_room_epoch_defaults_to_zero(self):
        # A snapshot dict with no ``room_epoch`` key (older format or
        # corrupted partial write) must restore to a state with
        # room_epoch == 0 — never a Python error.
        cfg = RoomConfig()
        # Minimal v2-shaped dict missing the room_epoch key.
        state_data = {
            "version": SNAPSHOT_VERSION,
            "topic": "design review",
            "participants": [],
        }
        restored = restore_state(state_data, cfg)
        self.assertEqual(restored.room_epoch, 0)
        self.assertEqual(restored.topic, "design review")


class SnapshotQueueBoundedDropOldest(unittest.TestCase):
    """P2.3 / audit RES3 — bounded snapshot queue with drop-oldest."""

    def test_overflow_drops_oldest_and_invokes_callback(self):
        # The journal is constructed with ``snapshot_queue_maxsize=2``
        # and is NOT opened, so the writer thread does not run and the
        # queue cannot drain. We then enqueue 5 payloads directly via
        # the producer-side helper. The first two fit; the next three
        # each evict the oldest, leaving exactly two payloads on the
        # queue. The drop callback fires three times.
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir, snapshot_queue_maxsize=2)
            calls: list[tuple[int, int]] = []
            j.set_snapshot_drop_callback(lambda total, depth: calls.append((total, depth)))
            for i in range(5):
                j._enqueue_snapshot({"version": 2, "tick": i})
            self.assertEqual(j._snapshot_queue.qsize(), 2)
            self.assertEqual(len(calls), 3)
            # ``total`` is monotonically increasing; ``depth`` reports
            # the configured cap (not the current qsize).
            self.assertEqual([t for t, _ in calls], [1, 2, 3])
            self.assertTrue(all(d == 2 for _, d in calls))
            # Newest-wins: the first two payloads were dropped.
            remaining = []
            try:
                while True:
                    remaining.append(j._snapshot_queue.get_nowait())
            except Exception:
                pass
            self.assertEqual([p["tick"] for p in remaining], [3, 4])

    def test_buggy_drop_callback_is_swallowed(self):
        # A drop callback that raises must not break the snapshot path —
        # drops are observability, not load-bearing.
        with tempfile.TemporaryDirectory() as tmpdir:
            j = Journal(tmpdir, snapshot_queue_maxsize=1)

            def explode(_total, _depth):
                raise RuntimeError("observer boom")

            j.set_snapshot_drop_callback(explode)
            # Two payloads — second one would invoke the buggy callback.
            j._enqueue_snapshot({"version": 2, "tick": 0})
            j._enqueue_snapshot({"version": 2, "tick": 1})
            # Did not raise; queue retains the newest payload.
            self.assertEqual(j._snapshot_queue.qsize(), 1)


if __name__ == "__main__":
    unittest.main()
