"""Loom v0 — UserTurn (obligation-based).

A :class:`UserTurn` is the unit between consecutive user posts. It scopes:

- The frozen :class:`UserTurnPlan` — the interpreter's classification of
  the triggering user message.
- The set of :class:`ResponseObligation`\\ s for required (and any
  explicit optional) participants.
- Closure semantics: the room ``settles`` and becomes idle when all
  ``must`` obligations resolve, OR a closure event fires
  (idle, new user post, ``/cancel``, ``/topic``, ``obligation_unresolved``).

The coordinator owns all state mutation. UserTurn here is a pure
dataclass + helpers; closure-detection helpers below return booleans
the coordinator interprets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional
import time

from loom.kernel.obligations import (
    ObligationLevel,
    ResponseObligation,
    UserTurnPlan,
)


UserTurnState = Literal["open", "closing", "closed"]
ClosureReason = Literal[
    "completed", "idle_timeout", "new_user_post", "cancelled",
    "topic_changed", "no_responder", "obligation_unresolved",
]


@dataclass
class UserTurn:
    id: int
    user_event_id: int
    started_at: float
    frozen_plan: UserTurnPlan
    obligations: dict[int, ResponseObligation] = field(default_factory=dict)
    speaker_counts: dict[str, int] = field(default_factory=dict)
    drafted: set[str] = field(default_factory=set)
    state: UserTurnState = "open"
    closure_reason: Optional[ClosureReason] = None
    last_activity_at: float = 0.0
    # Additional user-event ids that were debounced into this turn
    # (within ``user_turn_debounce_ms`` of the prior post). Actors treat
    # these as additional obligation triggers so the second of two
    # rapid-fire posts still wakes any required participant.
    debounced_event_ids: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.last_activity_at == 0.0:
            self.last_activity_at = self.started_at

    # ------------------------------------------------------------------
    # Convenience views
    # ------------------------------------------------------------------

    @property
    def required_participants(self) -> set[str]:
        return set(self.frozen_plan.required_participants)

    @property
    def optional_participants(self) -> set[str]:
        return set(self.frozen_plan.optional_participants)

    @property
    def routing_case(self) -> str:
        return self.frozen_plan.routing_case

    def obligation_for(self, participant_id: str) -> Optional[ResponseObligation]:
        """Return ``participant_id``'s open obligation in this turn, if any.

        Multiple obligations per participant are not produced by the v0
        deterministic interpreter (it emits one per id), so this helper
        returns the first match.
        """
        for ob in self.obligations.values():
            if ob.participant_id == participant_id and not ob.resolved:
                return ob
        return None

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------

    def mark_drafted(self, participant_id: str,
                     *, count_toward_cap: bool = True,
                     now: Optional[float] = None) -> None:
        """Record that ``participant_id`` drafted in this UserTurn.

        ``count_toward_cap=False`` is for direct-mention replies that
        bypass the cap. The draft still counts as activity (extends idle
        timeout), it just doesn't burn cap.

        Both branches add to :attr:`drafted` — that set is the
        authoritative "did they reply at all" signal.
        """
        if count_toward_cap:
            self.speaker_counts[participant_id] = (
                self.speaker_counts.get(participant_id, 0) + 1
            )
        self.drafted.add(participant_id)
        self.last_activity_at = now if now is not None else time.monotonic()

    def mark_obligation_resolved(self, obligation_id: int,
                                 *, by_event_id: Optional[int],
                                 now: Optional[float] = None) -> bool:
        """Mark an obligation resolved. Returns True if an open obligation was found.

        ``by_event_id`` references the committed chat event that
        discharged the obligation; pass ``None`` for administrative
        resolutions (``/cancel``).
        """
        ob = self.obligations.get(obligation_id)
        if ob is None or ob.resolved:
            return False
        ob.resolve(by_event_id=by_event_id)
        self.last_activity_at = now if now is not None else time.monotonic()
        return True

    def unresolved_required(self) -> set[str]:
        """Required participants whose ``must`` obligation is still open."""
        out: set[str] = set()
        for ob in self.obligations.values():
            if ob.level == "must" and not ob.resolved:
                out.add(ob.participant_id)
        return out

    def can_draft(self, participant_id: str) -> bool:
        """Cap-respecting draft eligibility (no obligation/eligibility check).

        Used by the coordinator when granting leases. Direct mentions
        bypass this (handled in coordinator).
        """
        cap = self._cap()
        if cap is None:
            return True
        return self.speaker_counts.get(participant_id, 0) < cap

    def _cap(self) -> Optional[int]:
        # ``frozen_plan`` does not carry a cap; the coordinator passes
        # the room config max at lease time. We expose this hook for
        # future per-plan caps without touching call sites.
        return None  # cap is enforced by the coordinator at lease time

    # ------------------------------------------------------------------
    # Closure
    # ------------------------------------------------------------------

    def close(self, reason: ClosureReason) -> None:
        self.state = "closed"
        self.closure_reason = reason

    def is_idle(self, *, idle_timeout_s: float,
                now: Optional[float] = None) -> bool:
        now = now if now is not None else time.monotonic()
        return (now - self.last_activity_at) >= idle_timeout_s

    def add_obligation(
        self,
        participant_id: str,
        level: ObligationLevel,
        target_event_ids: list[int],
        reason: str,
        *,
        next_obligation_id: int,
    ) -> tuple[ResponseObligation, int]:
        """Append a new obligation to an open turn at runtime.

        Used by :meth:`RoomCoordinator.unregister_participant` to
        transfer a removed participant's required obligation onto a
        live fallback so the turn doesn't close prematurely. Mirrors
        the allocation pattern in :func:`make_user_turn`.
        """
        ob = ResponseObligation(
            id=next_obligation_id,
            participant_id=participant_id,
            level=level,
            target_event_ids=list(target_event_ids),
            reason=reason,
        )
        self.obligations[next_obligation_id] = ob
        return ob, next_obligation_id + 1


def is_user_turn_complete(turn: UserTurn) -> bool:
    """Return True when every ``must`` obligation has been resolved.

    Optional obligations and ``should``-level obligations do not gate
    closure in v0. A turn with no required participants (e.g.
    ``plan_for_acknowledgement`` was somehow opened) is trivially
    complete.
    """
    return not turn.unresolved_required()


def should_open_new_user_turn(prev_user_post_ts: Optional[float],
                              now: float, debounce_ms: int) -> bool:
    """Debounce decision for a new user post.

    Returns True if the new post is far enough past the previous user
    post to warrant opening a fresh UserTurn. Posts within the debounce
    window append to the previous UserTurn (caller updates
    ``last_activity_at`` and treats them as a follow-up trigger).
    """
    if prev_user_post_ts is None:
        return True
    return (now - prev_user_post_ts) * 1000 >= debounce_ms


def make_user_turn(turn_id: int, user_event_id: int,
                   plan: UserTurnPlan,
                   *, started_at: Optional[float] = None,
                   next_obligation_id: Optional[int] = None,
                   ) -> tuple[UserTurn, int]:
    """Build a new :class:`UserTurn` from a plan, allocating obligation ids.

    Returns ``(turn, next_id_after_allocation)`` so the coordinator
    can advance its monotonic obligation counter.
    """
    started = started_at if started_at is not None else time.monotonic()
    if next_obligation_id is None:
        next_obligation_id = 1

    obligations: dict[int, ResponseObligation] = {}
    for ob in plan.obligations:
        # The plan emits id=0 placeholders; the coordinator owns id
        # allocation, so we copy (dataclass mutation is fine here since
        # the plan is the only owner up to this point).
        ob.id = next_obligation_id
        obligations[next_obligation_id] = ob
        next_obligation_id += 1

    turn = UserTurn(
        id=turn_id,
        user_event_id=user_event_id,
        started_at=started,
        frozen_plan=plan,
        obligations=obligations,
    )
    return turn, next_obligation_id


# Eligibility check used by the coordinator at lease-grant time.
def participant_is_eligible(turn: UserTurn, participant_id: str) -> bool:
    """A participant may draft in this turn if they hold any obligation
    or are listed in optional participants.
    """
    if participant_id in turn.required_participants:
        return True
    if participant_id in turn.optional_participants:
        return True
    return False
