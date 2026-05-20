"""Loom default policy — deterministic floor-aware classifier.

The :class:`DefaultPolicy` reproduces v0.0 ``loom.chat.loom.interpreter``
behavior. Two top-level paths:

**A. Round-robin mode active** (``state.control.turn_order`` is
non-empty) — auto-enabled by the game-start detector below.

  R1. Game-end phrase ("good game", "let's stop", "new topic") —
      returns an acknowledgement-style plan whose
      ``set_turn_order=[]`` instructs the coordinator to drop back to
      broadcast on open. No turn opens.
  R2. Direct ``@-mention`` — same plan as broadcast Case 1, but
      ``advance_turn_pointer=False`` so the rotation slot is preserved.
  R3. Acknowledgement — same as broadcast Case 2 (no turn).
  R4. Vocative — same as broadcast Case 3, ``advance_turn_pointer=False``.
  R5. Rotation pick — single-speaker plan for
      ``turn_order[next_speaker_idx]`` (skipping inactive ids).
      ``advance_turn_pointer=True`` so the coordinator rotates on close.

**B. Broadcast mode** (default; cases run in order):

1. **Direct mention** — user writes ``@<id>`` for at least one active
   capable participant. ``allowed_speakers = required = mentioned set``;
   ``wait_for_user_after = True``. With ≥2 mentions the routing case is
   ``multi_opinion``; with one it is ``direct_mention``.
2. **Acknowledgement** — bare phrases like ``ok``, ``thanks``, ``got
   it`` (≤3 words, exact match against a small allow-list). Returns a
   no-response plan; the runtime skips ``open_user_turn``.
3. **Vocative addressing** — natural-language naming without ``@``.
   Recognized patterns (case-insensitive): ``<name>, ...`` or
   ``<name>: ...`` at start; ``..., <name>`` or ``... <name>`` at end
   (the name is the final token after stripping terminal punctuation).
   Aliases include the lowercase id and the first underscore segment
   (so ``claude_code`` is matched by ``claude``). Generic words
   (``you``, ``guys``, ``team``, ``ai``, etc.) are blacklisted to avoid
   false positives. Behaves like a direct mention.
4. **(Closed floor — removed in v0.2.)** Cross-turn floor narrowing
   is no longer a built-in DefaultPolicy case; the
   ``RoomControlState.floor_owner`` field is gone. Custom policies that
   want narrowing should override :meth:`plan_user_turn`.
5. **Game start (auto round-robin)** — phrase like "let's play a game",
   "20 questions", "take turns" with ≥2 active capable participants.
   The opening turn broadcasts (so each agent can propose / accept the
   game), but the plan carries
   ``set_turn_order=sorted(active_capable)`` so subsequent user posts
   route through path A.
6. **Broadcast (default)** — every other user message goes to every
   active capable participant as a ``must`` obligation.
   ``allowed_speakers = required = all active capable``;
   ``wait_for_user_after = False``.

This policy reads :class:`RoomState` and never mutates it. Mode flips
and rotation order propagate to the coordinator via UserTurnPlan
fields — declarative, the kernel applies them.
"""

from __future__ import annotations

import re
from typing import Optional, cast

from loom.contracts import ConversationPolicy
from loom.kernel import obligations as obl
from loom.kernel.addressees import parse_addressees
from loom.kernel.events import Event
from loom.kernel.obligations import RoutingCase
from loom.kernel.room import RoomControlStateView, RoomStateView


# ---------------------------------------------------------------------------
# ANCHOR_SYNTHESIS_INSTRUCTIONS — role-specific prompt addendum that lives
# alongside the policy that owns it. The kernel charter
# (LOOM_PROTOCOL_INSTRUCTIONS) is unchanged and stays in loom/kernel/prompt.py.
# ---------------------------------------------------------------------------

ANCHOR_SYNTHESIS_INSTRUCTIONS = """\
Anchor / default-responder role:
- You are the configured anchor (or default responder). When the
  TURN CARD selects you and other agents have already weighed in, you
  have an extra responsibility to synthesize: name the strongest take,
  merge non-duplicative points, call out the key correction or
  remaining uncertainty.
- You are not a chair or moderator. Do not grant the floor. Do not
  narrate protocol mechanics.
"""


# ---------------------------------------------------------------------------
# Regexes — module-level so callers can monkeypatch in tests.
# ---------------------------------------------------------------------------

# Participant id reserved for the user-side actor. Excluded from
# addressing scans so vocatives like "user, ..." or "@user" don't
# resolve back to the speaker themselves. Centralized here so the
# three call-sites stay in lockstep.
_USER_PSEUDO_ID = "user"

# Acknowledgement set — compared after lowercasing + stripping. Phrases
# longer than three words are excluded automatically by the word-count
# guard in ``_is_acknowledgement``.
_ACK_PHRASES: frozenset[str] = frozenset(
    {
        "ok",
        "okay",
        "k",
        "kk",
        "thanks",
        "thank you",
        "thx",
        "ty",
        "got it",
        "cool",
        "nice",
        "noted",
        "sounds good",
        "sgtg",
    }
)

# Words that look like names but are too generic to be reliable vocatives.
_VOCATIVE_BLACKLIST: frozenset[str] = frozenset(
    {
        "you",
        "i",
        "me",
        "we",
        "us",
        "they",
        "them",
        "all",
        "guys",
        "yall",
        "everyone",
        "everybody",
        "anyone",
        "anybody",
        "someone",
        "team",
        "folks",
        "ai",
        "bot",
        "model",
        "user",
        "assistant",
        "agent",
        "llm",
    }
)

_VOC_START_RE = re.compile(r"^([A-Za-z][\w-]{1,32})\s*[,:]\s")
_VOC_END_RE = re.compile(r"(?:^|[,\s])([A-Za-z][\w-]{1,32})\s*$")

_GAME_START_RE = re.compile(
    r"\b(let'?s play|play (a|the|some) game|playing a game|"
    r"taking turns|take turns|"
    r"round[- ]robin|one at a time|go in order|"
    r"twenty questions|20 questions|"
    r"would you rather|two truths and a lie|trivia|"
    r"riddle|charades|"
    r"story (round|together|game))\b",
    re.IGNORECASE,
)

_GAME_END_RE = re.compile(
    r"\b(good game|gg+|"
    r"end (the |this )?game|stop (the |this )?game|"
    r"thanks? for (playing|the game)|"
    r"new topic|moving on|let'?s stop|"
    r"i'?m done( playing)?|done with (the |this )?game|"
    r"that'?s enough( for now)?)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_acknowledgement(text: str) -> bool:
    cleaned = text.strip().lower()
    if not cleaned:
        return False
    if len(cleaned.split()) > 3:
        return False
    cleaned = cleaned.rstrip(".!?,")
    return cleaned in _ACK_PHRASES


def _aliases_for(participant_ids: list[str]) -> dict[str, str]:
    """Build ``{lowercase-alias: pid}`` for vocative detection."""
    aliases: dict[str, str] = {}
    for pid in participant_ids:
        lower = pid.lower()
        if len(lower) >= 2 and lower not in _VOCATIVE_BLACKLIST:
            aliases.setdefault(lower, pid)
        head = pid.split("_", 1)[0].lower()
        if head and head != lower and len(head) >= 3 and head not in _VOCATIVE_BLACKLIST:
            aliases.setdefault(head, pid)
    return aliases


def _detect_vocative(
    text: str,
    addressable: list[str],
    *,
    exclude: Optional[str] = None,
) -> list[str]:
    """Detect natural-language vocative addressing without ``@``."""
    if not text:
        return []
    pool = [pid for pid in addressable if pid != exclude]
    aliases = _aliases_for(pool)
    if not aliases:
        return []
    cleaned = text.strip().rstrip(".!?")
    if not cleaned:
        return []

    matched: list[str] = []
    seen: set[str] = set()

    sm = _VOC_START_RE.match(cleaned)
    if sm:
        pid = aliases.get(sm.group(1).lower())
        if pid and pid not in seen:
            matched.append(pid)
            seen.add(pid)

    em = _VOC_END_RE.search(cleaned)
    if em:
        pid = aliases.get(em.group(1).lower())
        if pid and pid not in seen:
            matched.append(pid)
            seen.add(pid)

    return matched


def _instruction_for_directed(
    addressed: list[str], control: RoomControlStateView, topic: Optional[str] = None
) -> str:
    del control  # signature stability; topic now lives on state
    parts: list[str] = []
    if len(addressed) == 1:
        parts.append(f"Respond directly — you ({addressed[0]}) were addressed.")
    else:
        parts.append("Respond directly — you were one of the addressed participants.")
    if topic:
        parts.append(f"Topic: {topic}")
    return " ".join(parts)


def _instruction_for_broadcast(control: RoomControlStateView, topic: Optional[str] = None) -> str:
    del control
    parts = ["Open group chat. Reply with substance or [PASS]."]
    if topic:
        parts.append(f"Topic: {topic}")
    return " ".join(parts)


def _instruction_for_round_robin(
    speaker: str, control: RoomControlStateView, topic: Optional[str] = None
) -> str:
    del control
    parts = [
        f"Round-robin mode: you ({speaker}) are up this turn. "
        "Other agents are silent until the next user post. "
        "Make one move (ask one question, answer once, then stop)."
    ]
    if topic:
        parts.append(f"Topic: {topic}")
    return " ".join(parts)


def _instruction_for_game_start(
    active_capable: list[str], control: RoomControlStateView, topic: Optional[str] = None
) -> str:
    del control
    others = ", ".join(active_capable) if active_capable else "the room"
    parts = [
        f"Game starting. Briefly propose or accept a game in one or "
        f"two sentences. After this turn, {others} will take turns "
        "round-robin (one speaker per user post)."
    ]
    if topic:
        parts.append(f"Topic: {topic}")
    return " ".join(parts)


def _pick_rotation_speaker(
    control: RoomControlStateView,
    active_capable: list[str],
) -> Optional[str]:
    # Walk forward in the configured turn_order starting at the
    # rotation pointer, returning the first live id. The earlier
    # ``live[idx % len(live)]`` shape collapsed indices and shifted
    # the visible rotation slot when ids ahead of the pointer went
    # offline; this preserves the slot.
    n = len(control.turn_order)
    if n == 0:
        return None
    active_set = set(active_capable)
    start = control.next_speaker_idx % n
    for offset in range(n):
        candidate = control.turn_order[(start + offset) % n]
        if candidate in active_set:
            return candidate
    return None


# ---------------------------------------------------------------------------
# DefaultPolicy
# ---------------------------------------------------------------------------


class DefaultPolicy(ConversationPolicy):
    """Deterministic floor-aware default policy (v0.0 behavior).

    The plan_user_turn body mirrors the legacy
    ``loom.chat.loom.interpreter.classify`` function — same input,
    same output. Wrapping it in a policy class lets third-party
    consumers subclass / replace it without forking the kernel.

    Subclasses :class:`ConversationPolicy` directly (not
    :class:`loom.policy.BasicPolicy`) because the per-case branching
    (mention / acknowledgement / vocative / floor / game-start /
    broadcast) does not fit the BasicPolicy "filter responders, emit
    one must-plan" template.
    """

    name = "default"

    def plan_user_turn(
        self,
        user_event: Event,
        state: RoomStateView,
    ) -> obl.UserTurnPlan:
        text = user_event.body if isinstance(user_event.body, str) else ""
        target_event_ids = [user_event.id] if user_event.id is not None else []
        active_capable = sorted(
            pid for pid, info in state.participants.items() if info.active and info.capable
        )
        control = state.control

        mentioned = parse_addressees(text, active_capable, exclude=_USER_PSEUDO_ID)

        # ==============================================================
        # Path A — Round-robin mode active (turn_order non-empty).
        # ==============================================================
        if control.turn_order:
            # R1: Game-end phrase — exit the mode and skip the turn.
            if _GAME_END_RE.search(text):
                plan = obl.plan_for_acknowledgement(
                    target_event_ids=target_event_ids,
                    rationale="game-end phrase — exit round-robin",
                )
                plan.set_turn_order = []
                return plan

            # R2: Direct @-mention — same plan shape as broadcast Case 1
            # but rotation pointer is preserved.
            if mentioned:
                case: RoutingCase = "multi_opinion" if len(mentioned) >= 2 else "direct_mention"
                return obl.plan_with_required(
                    mentioned,
                    routing_case=case,
                    target_event_ids=target_event_ids,
                    reason="mention",
                    rationale=(f"@-mentioned (round-robin): {', '.join(mentioned)}"),
                    allowed_speakers=set(mentioned),
                    max_responses=len(mentioned),
                    wait_for_user_after=True,
                    instruction=_instruction_for_directed(mentioned, control, state.topic),
                    advance_turn_pointer=False,
                )

            # R3: Acknowledgement — pass through, mode stays active.
            if _is_acknowledgement(text):
                return obl.plan_for_acknowledgement(
                    target_event_ids=target_event_ids,
                    rationale="ack phrase (round-robin)",
                )

            # R4: Vocative — same as direct mention.
            vocative = _detect_vocative(text, active_capable, exclude=_USER_PSEUDO_ID)
            if vocative:
                case = "multi_opinion" if len(vocative) >= 2 else "direct_mention"
                case = cast(RoutingCase, case)
                return obl.plan_with_required(
                    vocative,
                    routing_case=case,
                    target_event_ids=target_event_ids,
                    reason="vocative",
                    rationale=(f"vocative (round-robin): {', '.join(vocative)}"),
                    allowed_speakers=set(vocative),
                    max_responses=len(vocative),
                    wait_for_user_after=True,
                    instruction=_instruction_for_directed(vocative, control, state.topic),
                    advance_turn_pointer=False,
                )

            # R5: Rotation pick.
            speaker = _pick_rotation_speaker(control, active_capable)
            if speaker is not None:
                return obl.plan_with_required(
                    [speaker],
                    routing_case="direct_mention",
                    target_event_ids=target_event_ids,
                    reason="round_robin",
                    rationale=(f"round-robin: {speaker} (idx {control.next_speaker_idx})"),
                    allowed_speakers={speaker},
                    max_responses=1,
                    wait_for_user_after=True,
                    instruction=_instruction_for_round_robin(speaker, control, state.topic),
                    advance_turn_pointer=True,
                )
            # Rotation is empty — fall through to broadcast.

        # ==============================================================
        # Path B — Broadcast mode (default).
        # ==============================================================

        # Case 1: Direct @-mention overrides everything else.
        if mentioned:
            case = "multi_opinion" if len(mentioned) >= 2 else "direct_mention"
            case = cast(RoutingCase, case)
            return obl.plan_with_required(
                mentioned,
                routing_case=case,
                target_event_ids=target_event_ids,
                reason="mention",
                rationale=f"@-mentioned: {', '.join(mentioned)}",
                allowed_speakers=set(mentioned),
                max_responses=len(mentioned),
                wait_for_user_after=True,
                instruction=_instruction_for_directed(mentioned, control, state.topic),
            )

        # Case 2: Bare acknowledgement — no turn opens.
        if _is_acknowledgement(text):
            return obl.plan_for_acknowledgement(
                target_event_ids=target_event_ids,
                rationale="ack phrase",
            )

        # Case 3: Vocative addressing.
        vocative = _detect_vocative(text, active_capable, exclude=_USER_PSEUDO_ID)
        if vocative:
            case = "multi_opinion" if len(vocative) >= 2 else "direct_mention"
            case = cast(RoutingCase, case)
            return obl.plan_with_required(
                vocative,
                routing_case=case,
                target_event_ids=target_event_ids,
                reason="vocative",
                rationale=f"vocative addressing: {', '.join(vocative)}",
                allowed_speakers=set(vocative),
                max_responses=len(vocative),
                wait_for_user_after=True,
                instruction=_instruction_for_directed(vocative, control, state.topic),
            )

        # Case 4: (Closed floor — removed in v0.2 along with
        # ``RoomControlState.floor_owner``. Policies that need narrowing
        # across turns should subclass DefaultPolicy and override
        # ``plan_user_turn``; the kernel no longer carries a persistent
        # narrowed-speakers field.)

        # Case 5: Game start (auto round-robin).
        if len(active_capable) >= 2 and _GAME_START_RE.search(text):
            order = list(active_capable)
            return obl.plan_with_required(
                active_capable,
                routing_case="multi_opinion",
                target_event_ids=target_event_ids,
                reason="game_start",
                rationale=(f"game-start detected; round-robin enabled with order {order}"),
                confidence=0.95,
                allowed_speakers=set(active_capable),
                max_responses=len(active_capable),
                wait_for_user_after=True,
                instruction=_instruction_for_game_start(active_capable, control, state.topic),
                set_turn_order=order,
            )

        # Case 6: Broadcast (default).
        if active_capable:
            return obl.plan_with_required(
                active_capable,
                routing_case="multi_opinion",
                target_event_ids=target_event_ids,
                reason="broadcast",
                rationale=(f"broadcast to all {len(active_capable)} active participants"),
                confidence=0.9,
                allowed_speakers=set(active_capable),
                max_responses=len(active_capable),
                wait_for_user_after=False,
                instruction=_instruction_for_broadcast(control, state.topic),
            )

        # No active capable participants — no obligation possible.
        return obl.plan_for_default(
            None,
            reason="no active participants",
            target_event_ids=target_event_ids,
            rationale="no active capable participants",
        )

    def role_prompt(self, participant_id: str, state: RoomStateView) -> str:
        """Return ANCHOR_SYNTHESIS_INSTRUCTIONS to the anchor / default responder.

        Same condition as v0.0: the prompt addendum applies to the
        participant occupying the anchor or default-responder slot.
        """
        anchor_roles = {pid for pid in (state.anchor_id, state.default_responder_id) if pid}
        if participant_id in anchor_roles:
            return ANCHOR_SYNTHESIS_INSTRUCTIONS
        return ""
