"""Tests for ``loom.kernel.addressees`` — the kernel-level addressee parser.

These tests exercise the helpers at their canonical location. Equivalent
behavior is also covered (with more edge cases) by
``test_kernel_interpreter.py`` for as long as ``interpreter.parse_addressees``
remains a back-compat alias.
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.addressees import (
    _MENTION_RE,
    last_responsible_speaker,
    parse_addressees,
)
from loom.kernel.bus import MessageBus


class ParseAddressees(unittest.TestCase):
    def test_picks_known_ids_in_order(self):
        out = parse_addressees(
            "hi @loom and @claude_code",
            addressable=["loom", "claude_code", "gemini"],
        )
        self.assertEqual(out, ["loom", "claude_code"])

    def test_drops_unknown_ids(self):
        out = parse_addressees(
            "@nobody @loom @stranger",
            addressable=["loom", "claude_code"],
        )
        self.assertEqual(out, ["loom"])

    def test_dedups_repeated_ids(self):
        out = parse_addressees(
            "@a @b @a hi @c @b",
            addressable=["a", "b", "c"],
        )
        self.assertEqual(out, ["a", "b", "c"])

    def test_excludes_self(self):
        out = parse_addressees(
            "@a @b",
            addressable=["a", "b"],
            exclude="a",
        )
        self.assertEqual(out, ["b"])

    def test_no_mentions_returns_empty(self):
        self.assertEqual(parse_addressees("hi", ["a", "b"]), [])

    def test_mention_regex_matches_basic_id(self):
        self.assertEqual(_MENTION_RE.findall("ping @loom"), ["loom"])


class LastResponsibleSpeaker(unittest.TestCase):
    def test_empty_bus_returns_none(self):
        bus = MessageBus()
        self.assertIsNone(last_responsible_speaker(bus))

    def test_picks_last_non_user_chat(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="user", body="hi"))
        bus.post(ev.chat(sender="loom", body="hello"))
        bus.post(ev.chat(sender="user", body="ping"))
        self.assertEqual(last_responsible_speaker(bus), "loom")

    def test_skips_system_chats(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="loom", body="hello"))
        bus.post(ev.chat(sender="system", body="boot"))
        self.assertEqual(last_responsible_speaker(bus), "loom")

    def test_respects_channel_filter(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="loom", body="public"))
        bus.post(ev.chat(sender="claude_code", body="dm", channel="dm:user"))
        self.assertEqual(
            last_responsible_speaker(bus, channel="main"),
            "loom",
        )
        self.assertEqual(
            last_responsible_speaker(bus, channel="dm:user"),
            "claude_code",
        )

    def test_walks_past_consecutive_user_posts(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="loom", body="hello"))
        for _ in range(5):
            bus.post(ev.chat(sender="user", body="ping"))
        self.assertEqual(last_responsible_speaker(bus), "loom")

    def test_multi_speaker_returns_most_recent_non_user(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="alice", body="first"))
        bus.post(ev.chat(sender="bob", body="second"))
        bus.post(ev.chat(sender="carol", body="third"))
        bus.post(ev.chat(sender="user", body="ping"))
        self.assertEqual(last_responsible_speaker(bus), "carol")

    def test_exclude_user_false_returns_user_if_most_recent(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="loom", body="hello"))
        bus.post(ev.chat(sender="user", body="ping"))
        self.assertEqual(
            last_responsible_speaker(bus, exclude_user=False),
            "user",
        )

    def test_exclude_user_false_still_skips_system(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="loom", body="hello"))
        bus.post(ev.chat(sender="system", body="boot"))
        self.assertEqual(
            last_responsible_speaker(bus, exclude_user=False),
            "loom",
        )

    def test_non_chat_events_do_not_affect_result(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="loom", body="hello"))
        bus.post(
            ev.user_turn_opened(
                user_turn_id=1,
                routing_case="multi_opinion",
                required_participants=["claude_code"],
            )
        )
        bus.post(ev.stream_start(lease_id=1, participant_id="claude_code", trigger_event_id=0))
        bus.post(ev.stream_delta(lease_id=1, participant_id="claude_code", text="hi"))
        bus.post(ev.system(body="something"))
        self.assertEqual(last_responsible_speaker(bus), "loom")


if __name__ == "__main__":
    unittest.main()
