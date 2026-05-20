"""Tests for v0.3 PR 8 — event taxonomy (planes) + lease_closed.

Doctrine: P2 (three event planes — Conversation, Control, Execution),
§4 (event taxonomy).

Test classes:

- :class:`EventPlaneClassification` — every kind maps to a plane;
  per-control_type override slot exists for v0.4 tool events.
- :class:`ControlActionEvents` — proposed / applied / denied
  constructors + validators round-trip cleanly.
- :class:`LeaseClosedUnification` — release_lease and check_lease_ttl
  emit `lease_closed` alongside the v0.2 legacy events; each reason
  produces the expected payload shape; legacy events still load.
"""

from __future__ import annotations

import json
import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.events import (
    EventPlane,
    EventShapeError,
    Event,
    plane_of,
)
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState


class EventPlaneClassification(unittest.TestCase):
    def test_plane_enum_has_three_members(self):
        self.assertEqual(len(list(EventPlane)), 3)
        self.assertEqual(EventPlane.CONVERSATION.value, "conversation")
        self.assertEqual(EventPlane.CONTROL.value, "control")
        self.assertEqual(EventPlane.EXECUTION.value, "execution")

    def test_chat_event_is_conversation_plane(self):
        e = ev.chat(sender="user", body="hi")
        self.assertEqual(plane_of(e), EventPlane.CONVERSATION)

    def test_stream_event_is_conversation_plane(self):
        e = ev.stream_start(lease_id=1, participant_id="loom", trigger_event_id=0)
        self.assertEqual(plane_of(e), EventPlane.CONVERSATION)

    def test_control_event_is_control_plane(self):
        e = ev.topic_changed(None, "new")
        self.assertEqual(plane_of(e), EventPlane.CONTROL)

    def test_capability_granted_is_control_plane(self):
        e = ev.capability_granted(
            grant_id="g1",
            grantor_id="user",
            grantee_id="loom",
            capability="SET_TOPIC",
            source_event_id=1,
        )
        self.assertEqual(plane_of(e), EventPlane.CONTROL)

    def test_summary_event_is_conversation_plane(self):
        # Summary is content, not control.
        from loom.kernel.events import Event as _Event

        e = _Event(kind="summary", sender="system", body="recap")
        self.assertEqual(plane_of(e), EventPlane.CONVERSATION)


class ControlActionEvents(unittest.TestCase):
    def test_control_action_proposed_constructor(self):
        e = ev.control_action_proposed(
            action_name="SET_TOPIC",
            proposer_id="loom",
            params={"new": "rebase strategy"},
            target_event_id=42,
        )
        self.assertEqual(ev.control_type_of(e), "control_action_proposed")
        self.assertEqual(e.body["action_name"], "SET_TOPIC")
        self.assertEqual(e.body["proposer_id"], "loom")
        self.assertEqual(e.body["params"], {"new": "rebase strategy"})
        self.assertEqual(e.body["target_event_id"], 42)

    def test_control_action_applied_round_trip(self):
        e = ev.control_action_applied(
            action_name="SET_TOPIC",
            applier_id="loom",
            effects=[{"effect_type": "topic_changed", "schema_version": 1}],
            applied_at_event_id=15,
        )
        e.id, e.ts = 0, 0.0
        loaded = Event.from_jsonl(e.to_jsonl())
        self.assertEqual(loaded.body["applier_id"], "loom")
        self.assertEqual(loaded.body["effects"][0]["effect_type"], "topic_changed")

    def test_control_action_denied_carries_reason(self):
        e = ev.control_action_denied(
            action_name="SET_TOPIC",
            proposer_id="loom",
            reason="INSUFFICIENT_CAPABILITY",
            check_name="capability",
        )
        self.assertEqual(e.body["reason"], "INSUFFICIENT_CAPABILITY")
        self.assertEqual(e.body["check_name"], "capability")

    def test_validator_rejects_missing_action_name(self):
        line = json.dumps(
            {
                "kind": "control",
                "sender": "system",
                "body": {"control_type": "control_action_proposed", "proposer_id": "loom"},
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


class LeaseClosedUnification(unittest.TestCase):
    """release_lease + check_lease_ttl emit lease_closed alongside legacy."""

    def _coord(self) -> RoomCoordinator:
        bus = MessageBus()
        return RoomCoordinator(bus, RoomState(config=RoomConfig()))

    def test_lease_closed_constructor_round_trip(self):
        e = ev.lease_closed(
            lease_id=1,
            holder="loom",
            kind="user_turn",
            reason="released",
            span_id="abc",
        )
        e.id, e.ts = 0, 0.0
        loaded = Event.from_jsonl(e.to_jsonl())
        self.assertEqual(loaded.body["lease_id"], 1)
        self.assertEqual(loaded.body["reason"], "released")
        self.assertEqual(loaded.body["span_id"], "abc")

    def test_lease_closed_each_reason_validates(self):
        for reason in (
            "released",
            "denied",
            "expired",
            "cancelled",
            "aborted",
            "aborted_validation",
        ):
            e = ev.lease_closed(lease_id=1, holder="loom", kind="user_turn", reason=reason)
            e.id, e.ts = 0, 0.0
            Event.from_jsonl(e.to_jsonl())  # must not raise

    def test_legacy_lease_denied_still_loads(self):
        # v0.2 journal lines must still deserialize even after PR 8.
        e = ev.lease_denied(
            holder="loom",
            check_name="open_turn",
            deny_reason="no_open_user_turn",
            trigger_event_id=0,
        )
        e.id, e.ts = 0, 0.0
        loaded = Event.from_jsonl(e.to_jsonl())
        self.assertEqual(ev.control_type_of(loaded), "lease_denied")

    def test_legacy_lease_expired_still_loads(self):
        e = ev.lease_expired(holder="loom", lease_id=1, trigger_event_id=0)
        e.id, e.ts = 0, 0.0
        loaded = Event.from_jsonl(e.to_jsonl())
        self.assertEqual(ev.control_type_of(loaded), "lease_expired")

    def test_release_lease_emits_lease_closed_released(self):
        coord = self._coord()
        coord.register_participant(ParticipantInfo(id="loom"))
        # Open a user turn so acquire_lease can succeed.
        from loom.kernel.obligations import plan_for_default

        user_event = ev.chat(sender="user", body="hello")
        coord.bus.post(user_event)
        plan = plan_for_default("loom", reason="test", target_event_ids=[user_event.id])
        coord.open_user_turn(user_event, plan)
        lease = coord.acquire_lease(holder="loom", trigger_event_id=user_event.id)
        self.assertIsNotNone(lease)
        coord.release_lease(lease)
        closed = [x for x in coord.bus.snapshot() if ev.control_type_of(x) == "lease_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].body["reason"], "released")
        self.assertEqual(closed[0].body["holder"], "loom")
        self.assertEqual(closed[0].body["kind"], "user_turn")

    def test_check_lease_ttl_emits_lease_closed_expired(self):
        coord = self._coord()
        coord.register_participant(ParticipantInfo(id="loom"))
        from loom.kernel.obligations import plan_for_default

        user_event = ev.chat(sender="user", body="hello")
        coord.bus.post(user_event)
        plan = plan_for_default("loom", reason="test", target_event_ids=[user_event.id])
        coord.open_user_turn(user_event, plan)
        lease = coord.acquire_lease(holder="loom", trigger_event_id=user_event.id)
        self.assertIsNotNone(lease)
        # Force expiry by passing a far-future cutoff.
        n = coord.check_lease_ttl(now=lease.expires_at + 1.0)
        self.assertEqual(n, 1)
        closed = [x for x in coord.bus.snapshot() if ev.control_type_of(x) == "lease_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].body["reason"], "expired")
        # Both legacy and new fire.
        expired = [x for x in coord.bus.snapshot() if ev.control_type_of(x) == "lease_expired"]
        self.assertEqual(len(expired), 1)


if __name__ == "__main__":
    unittest.main()
