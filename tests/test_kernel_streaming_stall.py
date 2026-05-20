"""Tests for v0.3 PR 12 — streaming-stall watchdog (closes audit D2).

Doctrine: P4 (no long-running I/O under coordinator lock — the
watchdog is the safety net when something gets stuck despite that).
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.events import EventShapeError, Event
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState


def _coord(stall_threshold: float = 30.0) -> RoomCoordinator:
    bus = MessageBus()
    state = RoomState(config=RoomConfig(stream_stall_threshold_s=stall_threshold))
    state.add_participant(ParticipantInfo(id="loom"))
    return RoomCoordinator(bus, state)


def _open_turn_with_lease(coord: RoomCoordinator):
    from loom.kernel.obligations import plan_for_default

    user_event = ev.chat(sender="user", body="hi")
    coord.bus.post(user_event)
    plan = plan_for_default("loom", reason="t", target_event_ids=[user_event.id])
    coord.open_user_turn(user_event, plan)
    return coord.acquire_lease(holder="loom", trigger_event_id=user_event.id)


class StreamStalledEvent(unittest.TestCase):
    def test_constructor_round_trip(self):
        e = ev.stream_stalled(lease_id=1, holder="loom", seconds_silent=42.0)
        e.id, e.ts = 0, 0.0
        loaded = Event.from_jsonl(e.to_jsonl())
        self.assertEqual(loaded.body["holder"], "loom")
        self.assertEqual(loaded.body["seconds_silent"], 42.0)

    def test_validator_rejects_missing_holder(self):
        import json

        line = json.dumps(
            {
                "kind": "control",
                "sender": "system",
                "body": {"control_type": "stream_stalled", "lease_id": 1, "seconds_silent": 1.0},
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


class StreamingStallWatchdog(unittest.TestCase):
    def test_no_stall_when_no_active_lease(self):
        coord = _coord()
        self.assertEqual(coord.check_streaming_stall(), 0)

    def test_no_stall_when_lease_has_recent_chunk(self):
        coord = _coord(stall_threshold=30.0)
        lease = _open_turn_with_lease(coord)
        self.assertIsNotNone(lease)
        # Record a chunk at t=100.
        coord.on_stream_chunk(lease, now=100.0)
        # Check at t=110 (10s elapsed, threshold 30s) → no stall.
        self.assertEqual(coord.check_streaming_stall(now=110.0), 0)

    def test_stall_fires_after_threshold(self):
        coord = _coord(stall_threshold=5.0)
        lease = _open_turn_with_lease(coord)
        coord.on_stream_chunk(lease, now=100.0)
        # 10s elapsed > 5s threshold.
        n = coord.check_streaming_stall(now=110.0)
        self.assertEqual(n, 1)
        # Event sequence: stream_stalled + lease_closed(aborted).
        types_seen = [ev.control_type_of(x) for x in coord.bus.snapshot()]
        self.assertIn("stream_stalled", types_seen)
        closed = [x for x in coord.bus.snapshot() if ev.control_type_of(x) == "lease_closed"]
        # At least one lease_closed with reason "aborted".
        self.assertTrue(any(x.body["reason"] == "aborted" for x in closed))

    def test_stall_marks_lease_invalid(self):
        coord = _coord(stall_threshold=5.0)
        lease = _open_turn_with_lease(coord)
        coord.on_stream_chunk(lease, now=100.0)
        coord.check_streaming_stall(now=110.0)
        # Lease is no longer in the active set; validate_lease returns False.
        self.assertFalse(coord.validate_lease(lease))

    def test_chunk_arrival_resets_silence_timer(self):
        coord = _coord(stall_threshold=5.0)
        lease = _open_turn_with_lease(coord)
        coord.on_stream_chunk(lease, now=100.0)
        # At t=103 (3s silent) — no stall.
        self.assertEqual(coord.check_streaming_stall(now=103.0), 0)
        # Fresh chunk at t=104 — timer resets.
        coord.on_stream_chunk(lease, now=104.0)
        # At t=108 (4s silent since last chunk) — still no stall.
        self.assertEqual(coord.check_streaming_stall(now=108.0), 0)

    def test_release_lease_clears_last_chunk_at(self):
        coord = _coord(stall_threshold=5.0)
        lease = _open_turn_with_lease(coord)
        coord.on_stream_chunk(lease, now=100.0)
        coord.release_lease(lease)
        # Watchdog should not find anything stale; the entry is cleared.
        self.assertEqual(coord.check_streaming_stall(now=200.0), 0)

    def test_uninitialised_lease_uses_acquired_at_as_fallback(self):
        coord = _coord(stall_threshold=5.0)
        lease = _open_turn_with_lease(coord)
        # Never call on_stream_chunk; lease.acquired_at is t0 (now).
        # Wait far past acquired_at + threshold.
        n = coord.check_streaming_stall(now=lease.acquired_at + 100.0)
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
