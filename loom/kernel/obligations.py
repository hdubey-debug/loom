"""Loom v0 — Response obligations.

The interpreter classifies each user message into a :class:`UserTurnPlan`
that names which participants are *required* to respond and which are
merely *optional*. The plan also carries one :class:`ResponseObligation`
per required (and any explicit optional) participant.

Obligation levels:

- ``"must"``   — required participant; the room will not close cleanly
  until this is resolved (or `/cancel` runs).
- ``"should"`` — strong preference; logged but does not gate UserTurn
  closure in v0. Reserved for future LLM-classified plans.
- ``"may"``    — optional; participant may :pep:`PASS` without
  consequence. Used to advertise eligibility without compulsion.

The dataclasses here are pure data; mutation belongs to the
:class:`RoomCoordinator`. Module-level helpers build a few canonical
plans the runtime needs (acknowledgement, fallback default).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


ObligationLevel = Literal["may", "should", "must"]


RoutingCase = Literal[
    "direct_mention",
    "question",
    "challenge",
    "followup",
    "acknowledgement",
    "multi_opinion",
    "single_responder",
    "round_robin",
    "floor",
    "broadcast",
    "dm",
    "none",
]


# Runtime-validated set used by :class:`UserTurnPlan` and the plan-builder
# helpers. Mirrors the :data:`RoutingCase` :class:`Literal` exactly. The
# Literal is for static analysis; the set is for the runtime guard that
# rejects unknown routing cases at plan construction (P2.6).
_VALID_ROUTING_CASES: frozenset[str] = frozenset({
    "direct_mention",
    "question",
    "challenge",
    "followup",
    "acknowledgement",
    "multi_opinion",
    "single_responder",
    "round_robin",
    "floor",
    "broadcast",
    "dm",
    "none",
})


def _validate_routing_case(value: str) -> None:
    if value not in _VALID_ROUTING_CASES:
        raise ValueError(
            f"unknown routing_case: {value!r}; valid values are "
            f"{sorted(_VALID_ROUTING_CASES)}")


@dataclass
class ResponseObligation:
    """One participant's response obligation for the current UserTurn.

    ``id`` is assigned by the coordinator on record so journal events
    can reference it. ``target_event_ids`` lists the user-event ids this
    obligation answers — typically a single user message, but
    multi-mention plans may span more than one.
    """
    id: int
    participant_id: str
    level: ObligationLevel
    target_event_ids: list[int]
    reason: str
    resolved: bool = False
    resolved_by_event_id: Optional[int] = None

    def resolve(self, *, by_event_id: Optional[int]) -> None:
        self.resolved = True
        self.resolved_by_event_id = by_event_id


@dataclass
class UserTurnPlan:
    """Output of the :func:`loom.chat.loom.interpreter.classify` pass.

    Frozen at UserTurn open and stored on the :class:`UserTurn`. The
    coordinator allocates :class:`ResponseObligation` ids; the
    interpreter sets ``id`` to ``0`` (placeholder) on each obligation.

    The plan is the per-turn contract between the interpreter and the
    coordinator. Floor-control fields (``allowed_speakers``,
    ``max_responses``, ``wait_for_user_after``, ``instruction``) gate
    lease acquisition and turn closure inside the coordinator; required
    obligations gate clean-completion.

    Field semantics:

    - ``required_participants`` ids whose ``must`` obligation gates
      closure (broadcast plans put every active capable id here).
    - ``optional_participants`` ids that may but need not respond.
    - ``allowed_speakers``     ids permitted to acquire a draft lease
      this turn. Defaults to ``required ∪ optional`` if not set
      explicitly. The coordinator denies leases for any id not in this
      set (direct-mention triggers still bypass via the existing
      ``is_direct_mention`` carve-out).
    - ``max_responses``        cap on total committed drafts in this
      turn. ``0`` defaults to ``len(allowed_speakers)``. The coordinator
      closes the turn early once this many drafts commit.
    - ``wait_for_user_after``  if ``True``, the coordinator sets
      :attr:`RoomControlState.wait_for_user` after this turn closes —
      keeping subsequent agent wakeups silent until the next user post.
      Used for directed turns (mentions, role-driven, floor-narrowed).
    - ``instruction``          short natural-language hint rendered into
      the selected speaker's TurnCard (e.g. "Teach the next small step
      slowly"). ``None`` means no extra hint beyond the trigger label.
    - ``set_turn_taking_mode`` if not ``None``, the coordinator switches
      :attr:`RoomControlState.turn_taking_mode` to this value when
      opening the turn. Used by the interpreter to enable round-robin
      on game-start phrases or restore broadcast on game-end phrases.
    - ``set_turn_order``       if not ``None``, the coordinator replaces
      :attr:`RoomControlState.turn_order` with this list (ignoring
      unknown ids) and resets the rotation pointer. Pair with
      ``set_turn_taking_mode="round_robin"`` on first entry.
    - ``advance_turn_pointer`` if ``True`` AND mode is still
      ``round_robin`` at close, the coordinator advances the rotation
      pointer by one. Set on plans whose chosen speaker came from the
      rotation; left ``False`` for @-mention / vocative overrides so
      the rotation slot is preserved across the side-question.
    """
    requires_response: bool
    routing_case: RoutingCase
    required_participants: set[str] = field(default_factory=set)
    optional_participants: set[str] = field(default_factory=set)
    obligations: list[ResponseObligation] = field(default_factory=list)
    target_event_ids: list[int] = field(default_factory=list)
    rationale: str = ""
    confidence: float = 1.0
    allowed_speakers: set[str] = field(default_factory=set)
    max_responses: int = 0
    wait_for_user_after: bool = False
    instruction: Optional[str] = None
    set_turn_taking_mode: Optional[str] = None
    set_turn_order: Optional[list[str]] = None
    advance_turn_pointer: bool = False

    def __post_init__(self) -> None:
        # P2.6: validate routing_case against the documented Literal set.
        # Free-form strings used to slip through silently and break
        # downstream consumers (analytics, TurnResult.routing_case).
        _validate_routing_case(self.routing_case)
        # Defensive: a plan that flags requires_response=True must name at
        # least one required participant. Callers that mean "no response"
        # should use ``plan_for_acknowledgement()`` or set the flag False.
        if self.requires_response and not self.required_participants:
            raise ValueError(
                "UserTurnPlan.requires_response=True with empty "
                "required_participants — use plan_for_acknowledgement() "
                "or fall back to default responder")
        # Default allowed_speakers to the union of required + optional.
        # Interpreters can override (e.g., narrow further or include
        # observers); the default matches the "everyone with an
        # obligation may speak" expectation.
        if not self.allowed_speakers:
            self.allowed_speakers = (
                set(self.required_participants)
                | set(self.optional_participants)
            )
        # Default max_responses to len(allowed_speakers); 0 means
        # "unlimited" only when there are no allowed speakers (no-response
        # plans). Callers wanting a hard cap pass it explicitly.
        if self.max_responses <= 0:
            self.max_responses = len(self.allowed_speakers)


def plan_for_acknowledgement(
    *, target_event_ids: Optional[list[int]] = None,
    rationale: str = "user message classified as acknowledgement",
) -> UserTurnPlan:
    """Plan for messages that need no response (e.g. ``thanks``, ``ok``).

    Runtime should skip ``open_user_turn`` entirely for these — the
    plan exists so the interpreter has a uniform return type.
    """
    return UserTurnPlan(
        requires_response=False,
        routing_case="acknowledgement",
        required_participants=set(),
        optional_participants=set(),
        obligations=[],
        target_event_ids=list(target_event_ids or []),
        rationale=rationale,
        confidence=1.0,
        allowed_speakers=set(),
        max_responses=0,
        wait_for_user_after=False,
        instruction=None,
    )


def plan_for_default(
    default_responder: Optional[str],
    *, reason: str,
    target_event_ids: Optional[list[int]] = None,
    rationale: str = "fallback to default responder",
    instruction: Optional[str] = None,
    wait_for_user_after: bool = True,
) -> UserTurnPlan:
    """Plan that routes to the configured default responder.

    Returns a plan with ``requires_response=False`` if no responder is
    available — the coordinator can then close the turn as
    ``no_responder``.

    ``wait_for_user_after`` defaults to ``True`` because a single-
    responder fallback is a directed turn: once the default responder
    has spoken, no other agent should chime in until the user posts.
    """
    if default_responder is None:
        return UserTurnPlan(
            requires_response=False,
            routing_case="none",
            required_participants=set(),
            optional_participants=set(),
            obligations=[],
            target_event_ids=list(target_event_ids or []),
            rationale=rationale + " (none configured)",
            confidence=0.5,
            allowed_speakers=set(),
            max_responses=0,
            wait_for_user_after=False,
            instruction=instruction,
        )
    return UserTurnPlan(
        requires_response=True,
        routing_case="none",
        required_participants={default_responder},
        optional_participants=set(),
        obligations=[
            ResponseObligation(
                id=0,
                participant_id=default_responder,
                level="must",
                target_event_ids=list(target_event_ids or []),
                reason=reason,
            ),
        ],
        target_event_ids=list(target_event_ids or []),
        rationale=rationale,
        confidence=0.6,
        allowed_speakers={default_responder},
        max_responses=1,
        wait_for_user_after=wait_for_user_after,
        instruction=instruction,
    )


def plan_with_required(
    required: list[str],
    *, routing_case: RoutingCase,
    target_event_ids: list[int],
    reason: str,
    rationale: str = "",
    confidence: float = 1.0,
    optional: Optional[list[str]] = None,
    allowed_speakers: Optional[set[str]] = None,
    max_responses: Optional[int] = None,
    wait_for_user_after: bool = False,
    instruction: Optional[str] = None,
    set_turn_taking_mode: Optional[str] = None,
    set_turn_order: Optional[list[str]] = None,
    advance_turn_pointer: bool = False,
) -> UserTurnPlan:
    """Build a ``must``-obligation plan from an ordered required list.

    Convenience for the interpreter cases (direct_mention, broadcast,
    floor-narrowed). Order is preserved in the obligations list to
    mirror the interpreter's chosen priority.

    Floor-control parameters (``allowed_speakers`` / ``max_responses`` /
    ``wait_for_user_after`` / ``instruction``) are forwarded to the
    plan; defaults follow :class:`UserTurnPlan.__post_init__`
    (``allowed_speakers = required ∪ optional``;
    ``max_responses = len(allowed_speakers)``).
    """
    if not required:
        raise ValueError("plan_with_required requires at least one id")
    obligations = [
        ResponseObligation(
            id=0,
            participant_id=pid,
            level="must",
            target_event_ids=list(target_event_ids),
            reason=reason,
        )
        for pid in required
    ]
    return UserTurnPlan(
        requires_response=True,
        routing_case=routing_case,
        required_participants=set(required),
        optional_participants=set(optional or []),
        obligations=obligations,
        target_event_ids=list(target_event_ids),
        rationale=rationale,
        confidence=confidence,
        allowed_speakers=allowed_speakers or set(),
        max_responses=max_responses if max_responses is not None else 0,
        wait_for_user_after=wait_for_user_after,
        instruction=instruction,
        set_turn_taking_mode=set_turn_taking_mode,
        set_turn_order=list(set_turn_order) if set_turn_order is not None else None,
        advance_turn_pointer=advance_turn_pointer,
    )
