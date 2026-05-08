"""Tests for :class:`loom.policy.default.DefaultPolicy` — broadcast-by-default classifier.

The v0 default policy has three branches:

  1. ``@-mention`` → required = mentioned set (``direct_mention`` for one
     mention, ``multi_opinion`` for ≥2).
  2. Bare acknowledgement (``ok`` / ``thanks`` / etc.) → no-response plan;
     runtime skips ``open_user_turn``.
  3. Anything else → broadcast to every active capable participant.

Agents self-route via prompt; the policy is intentionally minimal.

Test bodies are unchanged from the legacy ``test_kernel_interpreter.py``;
only the import surface is updated. The local ``it`` alias preserves
the call shape (``it.classify(...)``, ``it.parse_addressees(...)``,
``it._GAME_END_RE``) so the 700+ assertions read identically.
"""
from __future__ import annotations

import types
import unittest

from loom.kernel import events as ev
from loom.kernel.addressees import (
    _MENTION_RE,
    last_responsible_speaker,
    parse_addressees,
)
from loom.kernel.bus import MessageBus
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState
from loom.policy.default import (
    DefaultPolicy,
    _ACK_PHRASES,
    _GAME_END_RE,
    _GAME_START_RE,
    _VOC_END_RE,
    _VOC_START_RE,
    _VOCATIVE_BLACKLIST,
    _aliases_for,
    _detect_vocative,
    _instruction_for_broadcast,
    _instruction_for_directed,
    _instruction_for_floor,
    _instruction_for_game_start,
    _instruction_for_round_robin,
    _is_acknowledgement,
    _pick_rotation_speaker,
)


def _classify(user_event, state, *, prior_speaker=None):
    """Module-level helper — instantiate a fresh policy each call.

    The legacy module-level ``classify`` function expected positional
    ``(event, state, prior_speaker)``; we accept and silently drop
    ``prior_speaker`` (P2.7 removed the kwarg from the policy
    contract; tests were never coupled to its semantics in any case).
    """
    del prior_speaker
    return DefaultPolicy().plan_user_turn(user_event, state)


# ``it`` namespace — a SimpleNamespace shim so the legacy ``it.X``
# call-shape used throughout this file resolves to the canonical
# kernel/policy locations. Adding new tests should prefer importing
# the symbols directly.
it = types.SimpleNamespace(
    classify=_classify,
    parse_addressees=parse_addressees,
    last_responsible_speaker=last_responsible_speaker,
    _MENTION_RE=_MENTION_RE,
    _ACK_PHRASES=_ACK_PHRASES,
    _GAME_END_RE=_GAME_END_RE,
    _GAME_START_RE=_GAME_START_RE,
    _VOC_END_RE=_VOC_END_RE,
    _VOC_START_RE=_VOC_START_RE,
    _VOCATIVE_BLACKLIST=_VOCATIVE_BLACKLIST,
    _aliases_for=_aliases_for,
    _detect_vocative=_detect_vocative,
    _instruction_for_broadcast=_instruction_for_broadcast,
    _instruction_for_directed=_instruction_for_directed,
    _instruction_for_floor=_instruction_for_floor,
    _instruction_for_game_start=_instruction_for_game_start,
    _instruction_for_round_robin=_instruction_for_round_robin,
    _is_acknowledgement=_is_acknowledgement,
    _pick_rotation_speaker=_pick_rotation_speaker,
)


def _state_with(*ids: str,
                default_responder: str = "loom") -> RoomState:
    """Build a tiny RoomState with the given participant ids active+capable."""
    state = RoomState(config=RoomConfig())
    for pid in ids:
        state.add_participant(ParticipantInfo(id=pid, capable=True,
                                              active=True))
    if default_responder in ids:
        state.set_default_responder(default_responder)
    return state


def _user_chat(text: str, *, addressees=None) -> ev.Event:
    return ev.chat(sender="user", body=text,
                   addressees=list(addressees or []))


class ParseAddressees(unittest.TestCase):
    def test_basic(self):
        out = it.parse_addressees("hi @loom please reply",
                                  addressable=["loom", "claude_code"])
        self.assertEqual(out, ["loom"])

    def test_filters_unknown(self):
        out = it.parse_addressees("@nope @loom @other",
                                  addressable=["loom"])
        self.assertEqual(out, ["loom"])

    def test_dedup_preserves_order(self):
        out = it.parse_addressees("@a @b @a hi @c @b",
                                  addressable=["a", "b", "c"])
        self.assertEqual(out, ["a", "b", "c"])

    def test_excludes_self(self):
        out = it.parse_addressees("@a @b", addressable=["a", "b"],
                                  exclude="a")
        self.assertEqual(out, ["b"])


class LastResponsibleSpeaker(unittest.TestCase):
    def test_empty_bus_returns_none(self):
        bus = MessageBus()
        self.assertIsNone(it.last_responsible_speaker(bus))

    def test_returns_most_recent_non_user(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="user", body="hi"))
        bus.post(ev.chat(sender="loom", body="hello"))
        bus.post(ev.chat(sender="user", body="thx"))
        self.assertEqual(it.last_responsible_speaker(bus), "loom")

    def test_skips_system(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="loom", body="hi"))
        bus.post(ev.system("session note"))
        self.assertEqual(it.last_responsible_speaker(bus), "loom")

    def test_filters_by_channel(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="loom", body="main hi"))
        bus.post(ev.chat(sender="claude_code", body="dm hi",
                         channel="dm:user"))
        self.assertEqual(it.last_responsible_speaker(bus,
                                                     channel="main"),
                         "loom")
        self.assertEqual(it.last_responsible_speaker(bus,
                                                     channel="dm:user"),
                         "claude_code")


class DirectMention(unittest.TestCase):
    def test_single_mention(self):
        state = _state_with("loom", "claude_code")
        e = _user_chat("@claude_code what do you think?")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.routing_case, "direct_mention")
        self.assertEqual(plan.required_participants, {"claude_code"})
        self.assertEqual(plan.obligations[0].level, "must")

    def test_multiple_mentions_route_multi_opinion(self):
        state = _state_with("a", "b")
        e = _user_chat("@a @b reply")
        plan = it.classify(e, state, prior_speaker=None)
        # ≥2 @-mentions always classify as multi_opinion regardless of
        # connector words — the user explicitly named multiple agents.
        self.assertEqual(plan.routing_case, "multi_opinion")
        self.assertEqual(plan.required_participants, {"a", "b"})
        self.assertEqual(len(plan.obligations), 2)

    def test_inactive_mention_falls_through_to_broadcast(self):
        state = _state_with("loom", "claude_code")
        state.set_active("claude_code", False)
        e = _user_chat("@claude_code hi")
        plan = it.classify(e, state, prior_speaker=None)
        # The mention is filtered (not active); broadcast fallback
        # reaches the remaining active participant ("loom").
        self.assertEqual(plan.routing_case, "multi_opinion")
        self.assertEqual(plan.required_participants, {"loom"})


class Acknowledgement(unittest.TestCase):
    def test_thanks_no_response(self):
        state = _state_with("loom")
        e = _user_chat("thanks")
        plan = it.classify(e, state, prior_speaker="loom")
        self.assertEqual(plan.routing_case, "acknowledgement")
        self.assertFalse(plan.requires_response)
        self.assertEqual(plan.required_participants, set())

    def test_ok_no_response(self):
        state = _state_with("loom")
        e = _user_chat("ok")
        plan = it.classify(e, state, prior_speaker="loom")
        self.assertFalse(plan.requires_response)

    def test_got_it_two_words(self):
        state = _state_with("loom")
        e = _user_chat("got it")
        plan = it.classify(e, state, prior_speaker="loom")
        self.assertEqual(plan.routing_case, "acknowledgement")

    def test_thanks_with_punctuation(self):
        state = _state_with("loom")
        e = _user_chat("thanks!")
        plan = it.classify(e, state, prior_speaker="loom")
        self.assertEqual(plan.routing_case, "acknowledgement")

    def test_long_message_not_ack(self):
        state = _state_with("loom")
        e = _user_chat("ok so what about the edge case?")
        plan = it.classify(e, state, prior_speaker="loom")
        self.assertNotEqual(plan.routing_case, "acknowledgement")

    def test_thanks_with_mention_routes_to_mention(self):
        state = _state_with("loom", "claude_code")
        e = _user_chat("thanks @claude_code")
        plan = it.classify(e, state, prior_speaker="loom")
        # @-mentions override ack classification.
        self.assertEqual(plan.routing_case, "direct_mention")
        self.assertEqual(plan.required_participants, {"claude_code"})


class BroadcastByDefault(unittest.TestCase):
    """Anything that isn't @-mention and isn't bare-ack reaches everyone."""

    def test_plain_hello_broadcasts(self):
        state = _state_with("a", "b", "c", default_responder="a")
        e = _user_chat("hello")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.routing_case, "multi_opinion")
        self.assertEqual(plan.required_participants, {"a", "b", "c"})
        self.assertEqual(len(plan.obligations), 3)

    def test_question_broadcasts_not_routed_to_prior(self):
        state = _state_with("a", "b", "c", default_responder="a")
        e = _user_chat("does that hold for n>1?")
        plan = it.classify(e, state, prior_speaker="b")
        # Old design routed to prior_speaker; broadcast reaches all.
        self.assertEqual(plan.routing_case, "multi_opinion")
        self.assertEqual(plan.required_participants, {"a", "b", "c"})

    def test_challenge_phrase_broadcasts_not_routed(self):
        state = _state_with("a", "b", default_responder="a")
        e = _user_chat("I disagree with that")
        plan = it.classify(e, state, prior_speaker="b")
        self.assertEqual(plan.routing_case, "multi_opinion")
        self.assertEqual(plan.required_participants, {"a", "b"})

    def test_followup_connector_broadcasts(self):
        state = _state_with("a", "b", default_responder="a")
        e = _user_chat("interesting take.")
        plan = it.classify(e, state, prior_speaker="b")
        self.assertEqual(plan.routing_case, "multi_opinion")
        self.assertEqual(plan.required_participants, {"a", "b"})

    def test_say_something_broadcasts(self):
        state = _state_with("a", "b", "c", default_responder="a")
        e = _user_chat("all of you say something interesting")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.routing_case, "multi_opinion")
        self.assertEqual(plan.required_participants, {"a", "b", "c"})

    def test_one_active_still_broadcasts(self):
        # With a single active capable participant the "broadcast" set
        # is just that one — still routed via the multi_opinion case
        # rather than falling out as no-response.
        state = _state_with("a", default_responder="a")
        e = _user_chat("hello")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.routing_case, "multi_opinion")
        self.assertEqual(plan.required_participants, {"a"})

    def test_user_id_excluded_from_mentions_falls_to_broadcast(self):
        state = _state_with("loom")
        e = _user_chat("@user reply")
        plan = it.classify(e, state, prior_speaker=None)
        # @user is not a participant; no mentions resolved → broadcast.
        self.assertEqual(plan.routing_case, "multi_opinion")
        self.assertEqual(plan.required_participants, {"loom"})


class NoActiveParticipants(unittest.TestCase):
    def test_empty_room_returns_no_response(self):
        state = RoomState(config=RoomConfig())
        e = _user_chat("hi")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertFalse(plan.requires_response)
        self.assertEqual(plan.required_participants, set())

    def test_all_inactive_returns_no_response(self):
        state = _state_with("a", "b")
        state.set_active("a", False)
        state.set_active("b", False)
        e = _user_chat("hello")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertFalse(plan.requires_response)


class FloorNarrowed(unittest.TestCase):
    """RoomControlState.floor_owner narrows ``allowed_speakers``."""

    def test_floor_owner_replaces_broadcast(self):
        state = _state_with("a", "b", "c")
        state.set_floor_owner(["a"])
        e = _user_chat("hello room")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"a"})
        self.assertEqual(plan.allowed_speakers, {"a"})
        self.assertEqual(plan.max_responses, 1)
        self.assertTrue(plan.wait_for_user_after)

    def test_floor_owner_with_multiple_pids(self):
        state = _state_with("a", "b", "c")
        state.set_floor_owner(["a", "b"])
        e = _user_chat("continue")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"a", "b"})
        self.assertEqual(plan.allowed_speakers, {"a", "b"})
        self.assertEqual(plan.max_responses, 2)

    def test_floor_falls_back_to_broadcast_when_floor_inactive(self):
        state = _state_with("a", "b", "c")
        state.set_floor_owner(["a"])
        state.set_active("a", False)
        e = _user_chat("hello")
        plan = it.classify(e, state, prior_speaker=None)
        # Floor owner is inactive → broadcast to remaining active.
        self.assertEqual(plan.required_participants, {"b", "c"})
        self.assertFalse(plan.wait_for_user_after)

    def test_user_mention_overrides_floor(self):
        state = _state_with("a", "b", "c")
        state.set_floor_owner(["a"])
        e = _user_chat("@b hello")
        plan = it.classify(e, state, prior_speaker=None)
        # Direct @-mention always wins, even when floor is set.
        self.assertEqual(plan.required_participants, {"b"})
        self.assertEqual(plan.allowed_speakers, {"b"})


class BroadcastDefaults(unittest.TestCase):
    """Broadcast (no floor, no mention) defaults."""

    def test_broadcast_allowed_speakers_equals_active_capable(self):
        state = _state_with("a", "b", "c")
        e = _user_chat("hello")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.allowed_speakers, {"a", "b", "c"})
        self.assertEqual(plan.max_responses, 3)
        self.assertFalse(plan.wait_for_user_after)

    def test_direct_mention_wait_for_user_after_true(self):
        state = _state_with("a", "b")
        e = _user_chat("@a hello")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertTrue(plan.wait_for_user_after)
        self.assertEqual(plan.allowed_speakers, {"a"})

    def test_event_id_zero_keeps_target_event_ids(self):
        # Bug repro: bus assigns ids starting at 0; the first user post
        # carries id=0 which used to be treated as falsy, dropping the
        # target_event_ids correlation.
        state = _state_with("a", "b")
        e = _user_chat("hello")
        e.id = 0
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.target_event_ids, [0])


class PriorSpeakerIgnored(unittest.TestCase):
    """v0 broadcast classifier accepts ``prior_speaker`` for signature
    stability but does not use it — passing different values must not
    change the resulting plan."""

    def test_prior_speaker_does_not_change_plan(self):
        state = _state_with("a", "b", "c")
        e = _user_chat("anyone?")
        p_none = it.classify(e, state, prior_speaker=None)
        p_a = it.classify(e, state, prior_speaker="a")
        p_b = it.classify(e, state, prior_speaker="b")
        self.assertEqual(p_none.required_participants,
                         p_a.required_participants)
        self.assertEqual(p_none.required_participants,
                         p_b.required_participants)
        self.assertEqual(p_none.routing_case, "multi_opinion")


class VocativeAddressing(unittest.TestCase):
    """Natural-language vocative addressing without ``@``."""

    def test_end_of_message_vocative_narrows(self):
        # "thats too much claude" — the actual transcript bug.
        state = _state_with("claude_code", "OAI", "gemini")
        e = _user_chat("you cannot read 1m, thats too much claude")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.routing_case, "direct_mention")
        self.assertEqual(plan.required_participants, {"claude_code"})
        self.assertEqual(plan.allowed_speakers, {"claude_code"})
        self.assertEqual(plan.max_responses, 1)
        self.assertTrue(plan.wait_for_user_after)

    def test_end_of_message_with_comma(self):
        state = _state_with("claude_code", "OAI", "gemini")
        e = _user_chat("please explain that, claude")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"claude_code"})

    def test_end_of_message_with_punctuation(self):
        state = _state_with("claude_code", "OAI", "gemini")
        e = _user_chat("are you there claude?")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"claude_code"})

    def test_start_of_message_vocative(self):
        state = _state_with("claude_code", "OAI", "gemini")
        e = _user_chat("claude, what do you think?")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"claude_code"})

    def test_start_of_message_colon(self):
        state = _state_with("claude_code", "OAI", "gemini")
        e = _user_chat("OAI: explain this")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"OAI"})

    def test_full_id_vocative(self):
        state = _state_with("claude_code", "OAI", "gemini")
        e = _user_chat("claude_code, run the tests")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"claude_code"})

    def test_case_insensitive(self):
        state = _state_with("claude_code", "OAI", "gemini")
        e = _user_chat("Claude, hi")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"claude_code"})

    def test_name_in_middle_does_not_match(self):
        # Talking ABOUT claude, not TO claude — must broadcast.
        state = _state_with("claude_code", "OAI", "gemini")
        e = _user_chat("I told claude to fix it but he refused")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants,
                         {"claude_code", "OAI", "gemini"})

    def test_name_as_subject_does_not_match(self):
        # Start-of-sentence subject (no comma/colon) is not vocative.
        state = _state_with("claude_code", "OAI", "gemini")
        e = _user_chat("claude knows about this stuff")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants,
                         {"claude_code", "OAI", "gemini"})

    def test_explicit_mention_overrides_vocative(self):
        # @-mention case wins even when text also has a vocative.
        state = _state_with("claude_code", "OAI", "gemini")
        e = _user_chat("@OAI please respond claude")
        plan = it.classify(e, state, prior_speaker=None)
        # @-mention takes case 1 — only OAI required.
        self.assertEqual(plan.required_participants, {"OAI"})

    def test_blacklisted_word_not_vocative(self):
        # "you" / "guys" / "team" / "all" never narrow even though they
        # could look like names.
        state = _state_with("claude_code", "OAI", "gemini")
        for text in ("guys, hi", "team, please look", "everyone, hello",
                     "all, ok"):
            with self.subTest(text=text):
                e = _user_chat(text)
                plan = it.classify(e, state, prior_speaker=None)
                self.assertEqual(plan.required_participants,
                                 {"claude_code", "OAI", "gemini"})

    def test_unknown_name_does_not_match(self):
        state = _state_with("claude_code", "OAI", "gemini")
        e = _user_chat("nobody, please answer")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants,
                         {"claude_code", "OAI", "gemini"})

    def test_vocative_overrides_floor(self):
        # User explicitly named someone — that wins over a closed floor.
        state = _state_with("claude_code", "OAI", "gemini")
        state.set_floor_owner(["OAI"])
        e = _user_chat("gemini, what do you think?")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"gemini"})

    def test_vocative_skipped_for_inactive(self):
        state = _state_with("claude_code", "OAI", "gemini")
        state.set_active("gemini", False)
        e = _user_chat("gemini, hello")
        plan = it.classify(e, state, prior_speaker=None)
        # gemini inactive → vocative drops out → broadcast to remaining.
        self.assertEqual(plan.required_participants,
                         {"claude_code", "OAI"})

    def test_ack_takes_priority_over_vocative(self):
        # "thanks" is a bare ack — never opens a turn even with a name
        # we could match. Plain "thanks" is the test case; "thanks claude"
        # is two words and still passes the vocative branch separately.
        state = _state_with("claude_code", "OAI", "gemini")
        plan_ack = it.classify(_user_chat("thanks"), state,
                               prior_speaker=None)
        self.assertFalse(plan_ack.requires_response)
        plan_voc = it.classify(_user_chat("thanks claude"), state,
                               prior_speaker=None)
        self.assertTrue(plan_voc.requires_response)
        self.assertEqual(plan_voc.required_participants, {"claude_code"})

    def test_two_distinct_vocatives_at_start_and_end(self):
        # "claude, ... gemini" — start AND end both name a participant.
        state = _state_with("claude_code", "OAI", "gemini")
        e = _user_chat("claude, what do you think gemini")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants,
                         {"claude_code", "gemini"})
        self.assertEqual(plan.routing_case, "multi_opinion")

    def test_helper_aliases_for_uses_underscore_segment(self):
        aliases = it._aliases_for(["claude_code", "OAI", "gemini"])
        self.assertEqual(aliases.get("claude_code"), "claude_code")
        self.assertEqual(aliases.get("claude"), "claude_code")
        self.assertEqual(aliases.get("oai"), "OAI")
        self.assertEqual(aliases.get("gemini"), "gemini")

    def test_helper_aliases_skips_blacklist(self):
        # A participant whose name is blacklisted is still callable via
        # @-mention but not by vocative.
        aliases = it._aliases_for(["team_lead", "user_proxy"])
        self.assertNotIn("team", aliases)
        self.assertNotIn("user", aliases)
        # The full id is still mapped (long enough; not blacklisted).
        self.assertEqual(aliases.get("team_lead"), "team_lead")

    def test_helper_detect_vocative_returns_empty_for_no_addressable(self):
        self.assertEqual(it._detect_vocative("claude, hi", []), [])

    def test_helper_detect_vocative_excludes_self(self):
        out = it._detect_vocative("claude, hi", ["claude_code"],
                                  exclude="claude_code")
        self.assertEqual(out, [])


# ---------------------------------------------------------------------------
# Auto round-robin (game-start / game-end / rotation)
# ---------------------------------------------------------------------------


class GameStartDetection(unittest.TestCase):
    """Phrases like ``"lets play 20 questions"`` enable round-robin."""

    def test_lets_play_enables_round_robin(self):
        state = _state_with("a", "b", "c")
        e = _user_chat("lets play a game guys")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.set_turn_taking_mode, "round_robin")
        self.assertEqual(plan.set_turn_order, ["a", "b", "c"])
        # Opening turn still broadcasts so each agent can propose.
        self.assertEqual(plan.allowed_speakers, {"a", "b", "c"})
        self.assertEqual(plan.routing_case, "multi_opinion")
        self.assertTrue(plan.wait_for_user_after)

    def test_twenty_questions_phrase(self):
        state = _state_with("a", "b")
        e = _user_chat("how about 20 questions?")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.set_turn_taking_mode, "round_robin")

    def test_take_turns_phrase(self):
        state = _state_with("a", "b")
        e = _user_chat("lets take turns answering")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.set_turn_taking_mode, "round_robin")

    def test_would_you_rather(self):
        state = _state_with("a", "b")
        e = _user_chat("would you rather have x or y")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.set_turn_taking_mode, "round_robin")

    def test_two_truths_and_a_lie(self):
        state = _state_with("a", "b", "c")
        e = _user_chat("two truths and a lie — drop three statements")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.set_turn_taking_mode, "round_robin")

    def test_single_participant_does_not_enable(self):
        state = _state_with("a")
        e = _user_chat("lets play a game")
        plan = it.classify(e, state, prior_speaker=None)
        # Need ≥2 active capable; falls through to broadcast.
        self.assertIsNone(plan.set_turn_taking_mode)
        self.assertIsNone(plan.set_turn_order)

    def test_unrelated_text_does_not_enable(self):
        state = _state_with("a", "b")
        e = _user_chat("can you explain quicksort to me?")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertIsNone(plan.set_turn_taking_mode)

    def test_explicit_mention_overrides_game_start(self):
        # Direct mention (Case 1) wins over game-start (Case 5).
        state = _state_with("a", "b")
        e = _user_chat("@a lets play 20 questions")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"a"})
        # Game-start side-effect skipped because directed turn won.
        self.assertIsNone(plan.set_turn_taking_mode)


class RoundRobinRotation(unittest.TestCase):
    """When mode is round_robin, classify routes to the rotation pointer."""

    def _round_robin_state(self, *ids: str, idx: int = 0) -> RoomState:
        s = _state_with(*ids)
        s.set_turn_taking_mode("round_robin")
        s.set_turn_order(list(ids))
        s.control.next_speaker_idx = idx
        return s

    def test_first_message_routes_to_first_speaker(self):
        state = self._round_robin_state("a", "b", "c", idx=0)
        e = _user_chat("ok lets start, ask me something")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"a"})
        self.assertEqual(plan.allowed_speakers, {"a"})
        self.assertEqual(plan.max_responses, 1)
        self.assertTrue(plan.advance_turn_pointer)
        self.assertTrue(plan.wait_for_user_after)

    def test_second_message_routes_to_second_speaker(self):
        state = self._round_robin_state("a", "b", "c", idx=1)
        e = _user_chat("yes, exactly")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"b"})

    def test_pointer_wraps(self):
        state = self._round_robin_state("a", "b", "c", idx=0)
        # idx 3 % 3 == 0 → speaker is a
        state.control.next_speaker_idx = 3
        e = _user_chat("alright next")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"a"})

    def test_inactive_speaker_skipped(self):
        state = self._round_robin_state("a", "b", "c", idx=0)
        state.set_active("a", False)
        e = _user_chat("alright next")
        plan = it.classify(e, state, prior_speaker=None)
        # Live = [b, c], idx 0 → b
        self.assertEqual(plan.required_participants, {"b"})

    def test_at_mention_during_round_robin_does_not_advance(self):
        state = self._round_robin_state("a", "b", "c", idx=1)
        e = _user_chat("@c quick side question")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"c"})
        self.assertFalse(plan.advance_turn_pointer)

    def test_vocative_during_round_robin_does_not_advance(self):
        state = self._round_robin_state("alpha", "bravo", "charlie", idx=1)
        e = _user_chat("charlie, quick side question")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.required_participants, {"charlie"})
        self.assertFalse(plan.advance_turn_pointer)

    def test_ack_during_round_robin_passes_through(self):
        state = self._round_robin_state("a", "b", "c", idx=0)
        e = _user_chat("ok")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertFalse(plan.requires_response)
        # Mode stays active — no flip back to broadcast on plain ack.
        self.assertIsNone(plan.set_turn_taking_mode)

    def test_rotation_falls_back_to_broadcast_when_all_inactive(self):
        state = self._round_robin_state("a", "b", "c", idx=0)
        for pid in ("a", "b", "c"):
            state.set_active(pid, False)
        # Re-add an active participant outside the order.
        state.add_participant(ParticipantInfo(id="d", capable=True,
                                              active=True))
        e = _user_chat("anyone there?")
        plan = it.classify(e, state, prior_speaker=None)
        # Falls through to broadcast (Case 6) since rotation has no live.
        self.assertEqual(plan.allowed_speakers, {"d"})


class GameEndDetection(unittest.TestCase):
    """Game-end phrases exit round-robin and skip the turn."""

    def _round_robin_state(self) -> RoomState:
        s = _state_with("a", "b", "c")
        s.set_turn_taking_mode("round_robin")
        s.set_turn_order(["a", "b", "c"])
        return s

    def test_good_game_exits(self):
        state = self._round_robin_state()
        e = _user_chat("good game everyone")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertFalse(plan.requires_response)
        self.assertEqual(plan.set_turn_taking_mode, "broadcast")
        self.assertEqual(plan.set_turn_order, [])

    def test_thanks_for_playing_exits(self):
        state = self._round_robin_state()
        e = _user_chat("thanks for playing")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.set_turn_taking_mode, "broadcast")

    def test_lets_stop_exits(self):
        state = self._round_robin_state()
        e = _user_chat("lets stop and try something else")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.set_turn_taking_mode, "broadcast")

    def test_new_topic_exits(self):
        state = self._round_robin_state()
        e = _user_chat("new topic — help me debug this code")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.set_turn_taking_mode, "broadcast")

    def test_im_done_exits(self):
        state = self._round_robin_state()
        e = _user_chat("i'm done playing")
        plan = it.classify(e, state, prior_speaker=None)
        self.assertEqual(plan.set_turn_taking_mode, "broadcast")

    def test_game_end_only_in_round_robin(self):
        # In broadcast mode, "good game" is just a message.
        state = _state_with("a", "b")
        e = _user_chat("good game (we should have one)")
        plan = it.classify(e, state, prior_speaker=None)
        # Falls through to broadcast (no game-end exit signaled).
        self.assertIsNone(plan.set_turn_taking_mode)
        self.assertEqual(plan.allowed_speakers, {"a", "b"})


class GameSimulation(unittest.TestCase):
    """End-to-end: simulate the actual transcript flow with rotation."""

    def test_full_game_flow(self):
        state = _state_with("OAI", "claude_code", "gemini")

        # 1. User: "lets play a game guys" → broadcast for opening,
        # round-robin enabled with sorted order.
        plan1 = it.classify(_user_chat("lets play a game guys"),
                            state, prior_speaker=None)
        self.assertEqual(plan1.set_turn_taking_mode, "round_robin")
        self.assertEqual(plan1.set_turn_order,
                         ["OAI", "claude_code", "gemini"])
        # Apply the plan-driven changes (the coordinator does this in
        # production via _apply_plan_state_changes_locked).
        state.set_turn_taking_mode(plan1.set_turn_taking_mode)
        state.set_turn_order(plan1.set_turn_order)

        # 2. User: "person" — round-robin pick (idx 0 → OAI).
        plan2 = it.classify(_user_chat("person"), state, prior_speaker=None)
        self.assertEqual(plan2.required_participants, {"OAI"})
        self.assertTrue(plan2.advance_turn_pointer)
        # Coordinator advances at close.
        state.advance_round_robin_pointer()

        # 3. User: "yes, alive" — idx 1 → claude_code.
        plan3 = it.classify(_user_chat("yes, alive"), state,
                            prior_speaker=None)
        self.assertEqual(plan3.required_participants, {"claude_code"})
        state.advance_round_robin_pointer()

        # 4. User: "no" — idx 2 → gemini.
        plan4 = it.classify(_user_chat("no"), state, prior_speaker=None)
        self.assertEqual(plan4.required_participants, {"gemini"})
        state.advance_round_robin_pointer()

        # 5. Wraps back to OAI.
        plan5 = it.classify(_user_chat("yes"), state, prior_speaker=None)
        self.assertEqual(plan5.required_participants, {"OAI"})

        # 6. User: "good game" — exit mode, no turn opens.
        plan6 = it.classify(_user_chat("good game"), state,
                            prior_speaker=None)
        self.assertFalse(plan6.requires_response)
        self.assertEqual(plan6.set_turn_taking_mode, "broadcast")
        # After applying, mode is back to broadcast.
        state.set_turn_taking_mode(plan6.set_turn_taking_mode)
        self.assertEqual(state.control.turn_taking_mode, "broadcast")
        self.assertEqual(state.control.turn_order, [])

        # 7. Next message — broadcast resumes.
        plan7 = it.classify(_user_chat("hello"), state, prior_speaker=None)
        self.assertEqual(plan7.allowed_speakers,
                         {"OAI", "claude_code", "gemini"})


if __name__ == "__main__":
    unittest.main()
