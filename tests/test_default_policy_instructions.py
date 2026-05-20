"""Direct unit tests for ``DefaultPolicy._instruction_for_*`` helpers.

These helpers shape the per-turn instruction string passed into actor
prompts. They reach actors only through ``plan_user_turn`` so an
output-shape regression (missing speaker name, dropped topic, broken
phrasing) ships unnoticed. The legacy ``test_kernel_default_policy``
suite imports the helpers but never asserts their return value; this
module fills that gap.

Each helper currently does ``del control`` on entry, so the tests pass
a typed ``None`` rather than constructing a full ``RoomControlStateView``.
If the helpers ever start reading ``control``, switch to
``RoomState().control_view()``.
"""

from __future__ import annotations

import unittest
from typing import cast

from loom.kernel.room import RoomControlStateView
from loom.policy.default import (
    _instruction_for_broadcast,
    _instruction_for_directed,
    _instruction_for_game_start,
    _instruction_for_round_robin,
)


# Helpers ``del control`` on entry; a typed ``None`` is enough.
_NULL_CONTROL = cast(RoomControlStateView, None)


class InstructionForDirectedTests(unittest.TestCase):
    def test_single_addressee_includes_speaker_name(self) -> None:
        out = _instruction_for_directed(["alice"], _NULL_CONTROL)
        self.assertIn("alice", out)
        self.assertIn("addressed", out)

    def test_multi_addressee_uses_plural_phrasing(self) -> None:
        out = _instruction_for_directed(["alice", "bob"], _NULL_CONTROL)
        self.assertIn("addressed participants", out)

    def test_topic_is_appended_when_present(self) -> None:
        out = _instruction_for_directed(["alice"], _NULL_CONTROL, topic="recursion")
        self.assertIn("Topic: recursion", out)

    def test_topic_omitted_when_none(self) -> None:
        out = _instruction_for_directed(["alice"], _NULL_CONTROL)
        self.assertNotIn("Topic", out)
        self.assertTrue(out)


class InstructionForBroadcastTests(unittest.TestCase):
    def test_mentions_pass_token(self) -> None:
        out = _instruction_for_broadcast(_NULL_CONTROL)
        self.assertIn("[PASS]", out)

    def test_topic_appended(self) -> None:
        out = _instruction_for_broadcast(_NULL_CONTROL, topic="kernel")
        self.assertIn("Topic: kernel", out)


class InstructionForRoundRobinTests(unittest.TestCase):
    def test_names_the_active_speaker(self) -> None:
        out = _instruction_for_round_robin("alice", _NULL_CONTROL)
        self.assertIn("alice", out)
        self.assertIn("Round-robin", out)

    def test_warns_other_agents_silent(self) -> None:
        out = _instruction_for_round_robin("alice", _NULL_CONTROL)
        self.assertIn("silent", out.lower())

    def test_topic_appended(self) -> None:
        out = _instruction_for_round_robin("alice", _NULL_CONTROL, topic="20 questions")
        self.assertIn("Topic: 20 questions", out)


class InstructionForGameStartTests(unittest.TestCase):
    def test_lists_active_participants(self) -> None:
        out = _instruction_for_game_start(["alice", "bob"], _NULL_CONTROL)
        self.assertIn("alice", out)
        self.assertIn("bob", out)
        self.assertIn("round-robin", out.lower())

    def test_empty_active_list_falls_back_to_room(self) -> None:
        out = _instruction_for_game_start([], _NULL_CONTROL)
        self.assertIn("the room", out)

    def test_topic_appended(self) -> None:
        out = _instruction_for_game_start(["alice", "bob"], _NULL_CONTROL, topic="trivia night")
        self.assertIn("Topic: trivia night", out)


if __name__ == "__main__":
    unittest.main()
