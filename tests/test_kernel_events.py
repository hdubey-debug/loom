"""Tests for ``loom.kernel.events`` — the v0 event schema.

Covers:
- Default Event construction.
- All control event factories produce well-formed events.
- Stream event factories.
- chat / system / summary.
- JSONL round-trip.
- control_type_of / stream_event_of / is_direct_mention / is_known_control helpers.
- Unknown control_type raises ValueError.
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev


class EventDefaults(unittest.TestCase):
    def test_chat_defaults(self):
        e = ev.chat(sender="claude_code", body="hello")
        self.assertEqual(e.kind, "chat")
        self.assertEqual(e.sender, "claude_code")
        self.assertEqual(e.body, "hello")
        self.assertEqual(e.channel, "main")
        self.assertEqual(e.addressees, [])
        self.assertEqual(e.room_epoch, 0)
        self.assertIsNone(e.user_turn_id)
        self.assertEqual(e.meta, {})
        self.assertEqual(e.id, 0)
        self.assertEqual(e.ts, 0.0)

    def test_chat_with_addressees(self):
        e = ev.chat(sender="user", body="hi @claude_code", addressees=["claude_code"])
        self.assertEqual(e.addressees, ["claude_code"])
        self.assertTrue(ev.is_direct_mention(e, "claude_code"))
        self.assertFalse(ev.is_direct_mention(e, "gemini_cli"))


class ControlEvents(unittest.TestCase):
    def test_topic_changed(self):
        e = ev.topic_changed(None, "is the moon real?")
        self.assertEqual(ev.control_type_of(e), "topic_changed")
        self.assertIsNone(e.body["old"])
        self.assertEqual(e.body["new"], "is the moon real?")

    def test_participant_added(self):
        e = ev.participant_added("claude_code", role_hints={"capable": True})
        self.assertEqual(ev.control_type_of(e), "participant_added")
        self.assertEqual(e.body["id"], "claude_code")
        self.assertTrue(e.body["role_hints"]["capable"])

    def test_participant_added_default_role_hints(self):
        e = ev.participant_added("claude_code")
        self.assertEqual(e.body["role_hints"], {})

    def test_participant_removed(self):
        e = ev.participant_removed("claude_code")
        self.assertEqual(ev.control_type_of(e), "participant_removed")
        self.assertEqual(e.body["id"], "claude_code")

    def test_user_turn_opened(self):
        e = ev.user_turn_opened(
            7,
            routing_case="direct_mention",
            required_participants=["claude_code", "gemini_cli"],
            optional_participants=[],
            rationale="user @-mentioned both",
        )
        self.assertEqual(ev.control_type_of(e), "user_turn_opened")
        self.assertEqual(e.body["user_turn_id"], 7)
        self.assertEqual(e.body["routing_case"], "direct_mention")
        self.assertEqual(e.body["required_participants"], ["claude_code", "gemini_cli"])
        self.assertEqual(e.body["optional_participants"], [])
        self.assertEqual(e.body["rationale"], "user @-mentioned both")

    def test_user_turn_opened_defaults_optional_to_empty(self):
        e = ev.user_turn_opened(1, routing_case="question", required_participants=["loom"])
        self.assertEqual(e.body["optional_participants"], [])
        self.assertEqual(e.body["rationale"], "")

    def test_user_turn_closed(self):
        e = ev.user_turn_closed(7, "completed")
        self.assertEqual(ev.control_type_of(e), "user_turn_closed")
        self.assertEqual(e.body["reason"], "completed")

    def test_user_turn_closed_obligation_unresolved(self):
        e = ev.user_turn_closed(7, "obligation_unresolved")
        self.assertEqual(e.body["reason"], "obligation_unresolved")

    def test_obligation_recorded(self):
        e = ev.obligation_recorded(
            obligation_id=3,
            participant_id="claude_code",
            level="must",
            target_event_ids=[42],
            reason="direct_mention",
        )
        self.assertEqual(ev.control_type_of(e), "obligation_recorded")
        self.assertEqual(e.body["obligation_id"], 3)
        self.assertEqual(e.body["participant_id"], "claude_code")
        self.assertEqual(e.body["level"], "must")
        self.assertEqual(e.body["target_event_ids"], [42])
        self.assertEqual(e.body["reason"], "direct_mention")

    def test_obligation_resolved_with_chat_event(self):
        e = ev.obligation_resolved(
            obligation_id=3,
            participant_id="claude_code",
            resolved_by_event_id=99,
        )
        self.assertEqual(ev.control_type_of(e), "obligation_resolved")
        self.assertEqual(e.body["resolved_by_event_id"], 99)

    def test_obligation_resolved_administrative(self):
        e = ev.obligation_resolved(3, "claude_code", resolved_by_event_id=None)
        self.assertIsNone(e.body["resolved_by_event_id"])

    def test_dead_letter(self):
        e = ev.dead_letter(42, reason="participant_removed", reroute_to="loom")
        self.assertEqual(ev.control_type_of(e), "dead_letter")
        self.assertEqual(e.body["original_mention_event_id"], 42)
        self.assertEqual(e.body["reroute_to"], "loom")

    def test_dead_letter_no_reroute(self):
        e = ev.dead_letter(42, reason="no_responder")
        self.assertIsNone(e.body["reroute_to"])

    def test_default_responder_changed(self):
        e = ev.default_responder_changed("loom", "claude_code")
        self.assertEqual(ev.control_type_of(e), "default_responder_changed")
        self.assertEqual(e.body["old_id"], "loom")
        self.assertEqual(e.body["new_id"], "claude_code")

    def test_default_responder_changed_to_none(self):
        e = ev.default_responder_changed("loom", None)
        self.assertIsNone(e.body["new_id"])

    def test_roles_assigned_copies_mapping(self):
        roles = {"loom": "teacher"}
        e = ev.roles_assigned(roles)
        roles["loom"] = "changed"
        self.assertEqual(ev.control_type_of(e), "roles_assigned")
        self.assertEqual(e.body["roles"], {"loom": "teacher"})

    def test_floor_updated_omits_unspecified_fields(self):
        # v0.2: ``floor_owner`` removed from kernel state; the
        # ``floor_updated`` event now carries only ``wait_for_user``.
        e = ev.floor_updated(wait_for_user=False)
        self.assertEqual(ev.control_type_of(e), "floor_updated")
        self.assertFalse(e.body["wait_for_user"])
        self.assertNotIn("floor_owner", e.body)
        self.assertNotIn("active_goal", e.body)

    def test_style_changed(self):
        e = ev.style_changed("normal", "brief")
        self.assertEqual(ev.control_type_of(e), "style_changed")
        self.assertEqual(e.body["old"], "normal")
        self.assertEqual(e.body["new"], "brief")

    def test_journal_error(self):
        e = ev.journal_error("OSError", "disk full")
        self.assertEqual(ev.control_type_of(e), "journal_error")
        self.assertEqual(e.body["exception_class"], "OSError")
        self.assertEqual(e.body["message"], "disk full")

    def test_actor_error(self):
        e = ev.actor_error("loom", "RuntimeError", "boom")
        self.assertEqual(ev.control_type_of(e), "actor_error")
        self.assertEqual(e.body["participant_id"], "loom")
        self.assertEqual(e.body["exception_class"], "RuntimeError")
        self.assertEqual(e.body["message"], "boom")

    def test_unknown_control_type_rejected(self):
        with self.assertRaises(ValueError):
            ev._control("not_a_real_type", foo=1)

    def test_retired_control_types_no_longer_in_set(self):
        # Sanity: the four retired control types must NOT be re-added.
        for retired in ("mode_changed", "debate_turn", "forfeit", "debate_end"):
            self.assertNotIn(retired, ev.CONTROL_TYPES)


class StreamEvents(unittest.TestCase):
    def test_stream_start(self):
        e = ev.stream_start(lease_id=5, participant_id="claude_code", trigger_event_id=12)
        self.assertEqual(e.kind, "stream")
        self.assertEqual(e.sender, "claude_code")
        self.assertEqual(ev.stream_event_of(e), "start")
        self.assertEqual(e.body["lease_id"], 5)
        self.assertEqual(e.body["trigger_event_id"], 12)

    def test_stream_delta(self):
        e = ev.stream_delta(lease_id=5, participant_id="claude_code", text="hel")
        self.assertEqual(ev.stream_event_of(e), "delta")
        self.assertEqual(e.body["text"], "hel")

    def test_stream_end_committed(self):
        e = ev.stream_end(lease_id=5, participant_id="claude_code", status="committed")
        self.assertEqual(ev.stream_event_of(e), "end")
        self.assertEqual(e.body["status"], "committed")
        self.assertNotIn("error", e.body)

    def test_stream_end_error_carries_message(self):
        e = ev.stream_end(
            lease_id=5, participant_id="claude_code", status="error", error="rate limited"
        )
        self.assertEqual(e.body["status"], "error")
        self.assertEqual(e.body["error"], "rate limited")

    def test_stream_end_terminal_statuses_and_commit_id(self):
        for status in ("committed", "suppressed", "cancelled", "error", "lease_expired", "passed"):
            e = ev.stream_end(
                lease_id=5,
                participant_id="claude_code",
                status=status,
                committed_event_id=99,
            )
            self.assertEqual(ev.stream_event_of(e), "end")
            self.assertEqual(e.body["status"], status)
            self.assertEqual(e.body["committed_event_id"], 99)


class SystemAndSummary(unittest.TestCase):
    def test_system(self):
        e = ev.system("session started")
        self.assertEqual(e.kind, "system")
        self.assertEqual(e.sender, "system")
        self.assertEqual(e.body, "session started")

    def test_summary(self):
        e = ev.summary(
            "user asked about cosmology; claude argued for, gemini against", room_epoch=3
        )
        self.assertEqual(e.kind, "summary")
        self.assertEqual(e.channel, "main")
        self.assertEqual(e.room_epoch, 3)


class JsonlRoundTrip(unittest.TestCase):
    def test_chat_roundtrip(self):
        original = ev.chat(
            sender="claude_code",
            body="that swap is missing the index — `str[i]` not `str`",
            addressees=["gemini_cli"],
            user_turn_id=4,
            room_epoch=1,
            meta={"cost_tokens": 87},
        )
        original.id = 99
        original.ts = 1714329600.5
        line = original.to_jsonl()
        decoded = ev.Event.from_jsonl(line)
        self.assertEqual(decoded, original)

    def test_control_roundtrip(self):
        original = ev.obligation_recorded(
            obligation_id=4,
            participant_id="gemini_cli",
            level="must",
            target_event_ids=[12, 13],
            reason="multi_opinion",
        )
        original.id = 100
        original.ts = 1714329600.5
        line = original.to_jsonl()
        decoded = ev.Event.from_jsonl(line)
        self.assertEqual(decoded, original)

    def test_stream_roundtrip(self):
        original = ev.stream_end(lease_id=7, participant_id="gemini_cli", status="suppressed")
        line = original.to_jsonl()
        decoded = ev.Event.from_jsonl(line)
        self.assertEqual(decoded, original)


class Helpers(unittest.TestCase):
    def test_control_type_of_non_control_returns_none(self):
        e = ev.chat(sender="user", body="hi")
        self.assertIsNone(ev.control_type_of(e))

    def test_control_type_of_malformed_body_returns_none(self):
        e = ev.Event(kind="control", sender="system", body="not a dict")
        self.assertIsNone(ev.control_type_of(e))
        self.assertFalse(ev.is_known_control(e))

    def test_stream_event_of_non_stream_returns_none(self):
        e = ev.chat(sender="user", body="hi")
        self.assertIsNone(ev.stream_event_of(e))

    def test_stream_event_of_malformed_body_returns_none(self):
        e = ev.Event(kind="stream", sender="loom", body="not a dict")
        self.assertIsNone(ev.stream_event_of(e))

    def test_is_known_control_for_current_type(self):
        e = ev.topic_changed(None, "x")
        self.assertTrue(ev.is_known_control(e))

    def test_is_known_control_false_for_chat(self):
        e = ev.chat(sender="user", body="hi")
        self.assertFalse(ev.is_known_control(e))

    def test_is_known_control_false_for_legacy_control(self):
        # Simulate a legacy ``mode_changed`` line read from a v0-pre
        # journal: kind="control" but control_type is no longer
        # registered. Construct directly to bypass _control()'s gate.
        legacy = ev.Event(
            kind="control",
            sender="system",
            body={"control_type": "mode_changed", "old": "normal", "new": "council"},
        )
        self.assertFalse(ev.is_known_control(legacy))


# ---------------------------------------------------------------------------
# Cross-factory invariants — properties every event factory should obey.
# ---------------------------------------------------------------------------


class EventInvariants(unittest.TestCase):
    """Properties that must hold across every event factory.

    The previous tests cover individual factories one by one; this class
    walks every factory at once and asserts cross-cutting invariants.
    """

    @staticmethod
    def _all_factories() -> list:
        """Build one example event per factory in :mod:`loom.kernel.events`."""
        return [
            ev.chat(sender="user", body="hi"),
            ev.chat(sender="loom", body="reply", addressees=["claude_code"]),
            ev.system(body="boot"),
            ev.summary(body="compaction summary"),
            ev.topic_changed("old", "new"),
            ev.participant_added("loom"),
            ev.participant_removed("claude_code"),
            ev.user_turn_opened(
                user_turn_id=1, routing_case="multi_opinion", required_participants=["loom"]
            ),
            ev.user_turn_closed(user_turn_id=1, reason="completed"),
            ev.obligation_recorded(
                obligation_id=1,
                participant_id="loom",
                level="must",
                target_event_ids=[0],
                reason="r",
            ),
            ev.obligation_resolved(obligation_id=1, participant_id="loom", resolved_by_event_id=2),
            ev.dead_letter(original_mention_event_id=0, reason="r"),
            ev.default_responder_changed("loom", "claude"),
            ev.roles_assigned({"loom": "teacher"}),
            ev.floor_updated(wait_for_user=True),
            ev.style_changed("normal", "brief"),
            ev.journal_error("OSError", "disk full"),
            ev.actor_error("loom", "RuntimeError", "boom"),
            ev.stream_start(lease_id=1, participant_id="loom", trigger_event_id=0),
            ev.stream_delta(lease_id=1, participant_id="loom", text="x"),
            ev.stream_end(
                lease_id=1, participant_id="loom", status="committed", committed_event_id=2
            ),
        ]

    def test_every_factory_round_trips_through_jsonl(self):
        # Each factory output must serialize via ``to_jsonl`` and
        # deserialize back into an equivalent Event. Round-trip is the
        # only persistence contract the journal relies on.
        for original in self._all_factories():
            line = original.to_jsonl()
            restored = ev.Event.from_jsonl(line)
            self.assertEqual(original.kind, restored.kind)
            self.assertEqual(original.sender, restored.sender)
            self.assertEqual(original.body, restored.body)
            self.assertEqual(original.channel, restored.channel)
            self.assertEqual(original.addressees, restored.addressees)

    def test_control_type_of_returns_known_or_none_for_every_event(self):
        # For control events the result must be a member of
        # CONTROL_TYPES (the registered set) or, in the legacy case, a
        # string at minimum. For non-control events it must be ``None``.
        for e in self._all_factories():
            ct = ev.control_type_of(e)
            if e.kind == "control":
                self.assertIsInstance(ct, str)
                self.assertIn(ct, ev.CONTROL_TYPES)
            else:
                self.assertIsNone(ct)

    def test_is_known_control_agrees_with_control_type_of(self):
        # By construction, every factory-emitted control event has a
        # registered control_type; non-control events fail both checks.
        for e in self._all_factories():
            ct = ev.control_type_of(e)
            if e.kind == "control":
                self.assertTrue(ev.is_known_control(e))
                self.assertIn(ct, ev.CONTROL_TYPES)
            else:
                self.assertFalse(ev.is_known_control(e))

    def test_chat_addressees_default_is_fresh_list_per_call(self):
        # Default-factory leakage bug guard: mutating one event's
        # ``addressees`` must not bleed into a sibling event built with
        # the same factory.
        a = ev.chat(sender="loom", body="one")
        b = ev.chat(sender="claude_code", body="two")
        a.addressees.append("loom")
        self.assertEqual(b.addressees, [])
        # And a third independent construction is also empty.
        c = ev.chat(sender="user", body="three")
        self.assertEqual(c.addressees, [])

    def test_unknown_control_type_factory_rejects_with_value_error(self):
        # The internal ``_control`` helper enforces the registered type
        # set. This is the structural-error surface for malformed
        # control events authored by callers.
        with self.assertRaises(ValueError):
            ev._control("not_a_real_control_type", foo=1)


if __name__ == "__main__":
    unittest.main()
