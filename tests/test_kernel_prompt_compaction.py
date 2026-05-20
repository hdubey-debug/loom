"""Tests for v0.3.x PR 4 — build_prompt reads ContextState.

Doctrine: §6 / §10 (`docs/internal/study/14-context-compaction-doctrine.md`).
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.context import (
    ContextScope,
    SummaryRecord,
)
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.prompt import build_prompt
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState


def _coord_with_chats(n: int = 12, room_id: str = "main") -> RoomCoordinator:
    bus = MessageBus()
    state = RoomState(config=RoomConfig(room_id=room_id))
    state.add_participant(ParticipantInfo(id="loom"))
    state.add_participant(ParticipantInfo(id="claude_code"))
    coord = RoomCoordinator(bus, state)
    for i in range(n):
        bus.post(ev.chat(sender="loom", body=f"m{i}"))
    return coord


class RendersActiveSummary(unittest.TestCase):
    def test_active_summary_renders_in_prompt(self):
        coord = _coord_with_chats(20)
        rec = SummaryRecord(
            summary_id="s1",
            scope=ContextScope(room_id="main"),
            covers_event_range=(0, 9),
            text="this is the canonical summary text",
            input_event_ranges=((0, 9),),
            summarizer_id="loom",
        )
        result = coord.submit_summary_proposed(rec)
        self.assertTrue(result.committed, msg=f"reason={result.reason!r}")
        prompt = build_prompt("claude_code", trigger_event=None, coordinator=coord)
        self.assertIn("this is the canonical summary text", prompt)
        self.assertIn("<<<PRIOR ROOM SUMMARY (canonical compaction)>>>", prompt)

    def test_no_active_summary_omits_block(self):
        coord = _coord_with_chats(5)
        prompt = build_prompt("claude_code", trigger_event=None, coordinator=coord)
        self.assertNotIn("PRIOR ROOM SUMMARY", prompt)

    def test_legacy_summary_event_renders_when_no_active(self):
        # No ContextState entry; falls back to a posted summary event.
        coord = _coord_with_chats(5)
        coord.bus.post(ev.summary("legacy compaction text"))
        prompt = build_prompt("claude_code", trigger_event=None, coordinator=coord)
        self.assertIn("legacy compaction text", prompt)

    def test_active_summary_wins_over_legacy_event(self):
        # When both exist, ContextState takes precedence (doctrine §6).
        coord = _coord_with_chats(20)
        coord.bus.post(ev.summary("legacy text"))
        rec = SummaryRecord(
            summary_id="s1",
            scope=ContextScope(room_id="main"),
            covers_event_range=(0, 9),
            text="canonical text",
            input_event_ranges=((0, 9),),
            summarizer_id="loom",
        )
        coord.submit_summary_proposed(rec)
        prompt = build_prompt("claude_code", trigger_event=None, coordinator=coord)
        self.assertIn("canonical text", prompt)
        self.assertNotIn("legacy text", prompt)


class RoomConfigCompactionFields(unittest.TestCase):
    def test_room_id_defaults_to_main(self):
        self.assertEqual(RoomConfig().room_id, "main")

    def test_pressure_threshold_default(self):
        self.assertEqual(RoomConfig().context_pressure_threshold_ratio, 0.7)

    def test_pressure_check_interval_default(self):
        self.assertEqual(RoomConfig().context_pressure_check_interval_events, 10)

    def test_max_consecutive_failures_default(self):
        self.assertEqual(RoomConfig().summarizer_max_consecutive_failures, 3)

    def test_overrides_propagate(self):
        cfg = RoomConfig(
            room_id="research",
            context_pressure_threshold_ratio=0.5,
            context_pressure_check_interval_events=25,
            summarizer_max_consecutive_failures=5,
        )
        self.assertEqual(cfg.room_id, "research")
        self.assertEqual(cfg.context_pressure_threshold_ratio, 0.5)
        self.assertEqual(cfg.context_pressure_check_interval_events, 25)
        self.assertEqual(cfg.summarizer_max_consecutive_failures, 5)


if __name__ == "__main__":
    unittest.main()
