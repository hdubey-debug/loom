"""Loom v0 — Room state and configuration.

``RoomConfig`` is immutable boot-time configuration (timeouts, thresholds).

``RoomState`` is the live mutable state owned by :class:`RoomCoordinator`.
It tracks ``room_epoch``, participants, slot occupants (anchor / chair /
default_responder / default_summarizer), the current ``UserTurn`` id,
and the last compacted event id (for compaction trigger). Mutation
increments ``room_epoch`` on membership / slot changes so in-flight
:class:`TurnLease` instances can self-invalidate.

The coordinator wraps these primitives with bus emission of control
events (``participant_added/removed``, ``default_responder_changed``);
this module performs only the state transition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping, Optional, Tuple


StyleLevel = Literal["brief", "normal", "detailed"]

TurnTakingMode = Literal["broadcast", "round_robin"]


@dataclass(frozen=True)
class RoomConfig:
    """Immutable boot-time configuration.

    Slot occupants (anchor/chair/default_responder/default_summarizer) are
    NOT here — they live on :class:`RoomState` because they can change at
    runtime (notably via ``/remove`` re-resolution).

    ``max_drafts_per_participant`` caps how many real (non-suppressed)
    drafts a single participant may emit during one UserTurn. Default
    ``1`` covers the v0 group-chat behavior: one substantive reply per
    speaker per turn.
    """
    compact_threshold: int = 50
    user_turn_idle_timeout_s: int = 20
    user_turn_debounce_ms: int = 250
    pass_buffer_chars: int = 16
    lease_ttl_s: int = 60
    max_drafts_per_participant: int = 1


@dataclass
class RoomControlState:
    """Canonical, coordinator-owned, replay-rebuildable room control state.

    These are the persistent across-turn knobs that govern who may speak
    and how. Per-turn decisions live in the :class:`UserTurnPlan` on the
    open :class:`UserTurn`; ``RoomControlState`` is what the interpreter
    consults *between* user posts.

    Fields:

    - ``roles``         current task-role assignments (e.g.
      ``{"gemini": "teacher", "claude_code": "quizzer"}``). Empty when
      nobody has a role. Rendered into every selected speaker's
      TurnCard so a role assignment sticks across turns without each
      agent needing to re-derive it from the transcript.
    - ``floor_owner``   ``None`` means the floor is open (broadcast
      default). A non-empty list narrows ``allowed_speakers`` to that
      set on every subsequent classification, until cleared.
    - ``wait_for_user`` set to ``True`` after a UserTurn closes with
      ``wait_for_user_after``; the next user post is required before
      any agent draft. Cleared automatically when the user posts.
    - ``style``         ``"brief"`` / ``"normal"`` / ``"detailed"`` — a
      brevity preference that persists across turns. Renders as a max-
      length hint into every TurnCard.
    - ``turn_taking_mode`` ``"broadcast"`` (default) means every active
      capable participant is eligible per turn; ``"round_robin"`` means
      the interpreter routes each non-mention user post to a single
      rotating speaker. Auto-enabled when the interpreter detects a
      game-start phrase ("let's play a game", "20 questions", etc.) and
      auto-disabled by an explicit end phrase ("good game", "let's
      stop", "new topic"). The user never toggles this directly.
    - ``turn_order``    ordered list of participant ids consulted in
      ``round_robin`` mode. Set when the mode is entered (typically
      ``sorted(active_capable)``). Stale ids (removed participants) are
      filtered at read time, not pruned eagerly.
    - ``next_speaker_idx`` rotation pointer into ``turn_order``;
      advanced on UserTurn close when the closed plan came from the
      rotation (not from an @-mention or vocative override).
    """
    roles: dict[str, str] = field(default_factory=dict)
    floor_owner: Optional[list[str]] = None
    wait_for_user: bool = False
    style: StyleLevel = "normal"
    turn_taking_mode: TurnTakingMode = "broadcast"
    turn_order: list[str] = field(default_factory=list)
    next_speaker_idx: int = 0


@dataclass
class ParticipantInfo:
    """Per-participant metadata held in the room registry.

    The actual proxy / streaming machinery lives in
    :class:`ParticipantActor`; this struct is only what
    :class:`RoomState` needs to make routing decisions:

    - ``capable`` whether this participant can serve as a fallback in
      slot re-resolution. Set to ``False`` for observer-only members.
    - ``cost_tier`` lower = preferred fallback. ``0`` = free / local
      (Gemma), ``1`` = cheap API, ``2`` = expensive frontier model.
    - ``active`` toggled to ``False`` when a participant is paused or
      error-backoff-pending; excluded from fallback selection.
    - ``role_hints`` opaque metadata published with ``participant_added``.
    """
    id: str
    capable: bool = True
    cost_tier: int = 0
    active: bool = True
    role_hints: dict = field(default_factory=dict)


@dataclass
class RoomState:
    """Live, mutable room state. Single-writer (the coordinator)."""
    config: RoomConfig
    room_epoch: int = 0
    topic: Optional[str] = None
    participants: dict[str, ParticipantInfo] = field(default_factory=dict)

    # Slot occupants — change at runtime (re-resolved on /remove).
    anchor_id: Optional[str] = None
    chair_id: Optional[str] = None
    default_responder_id: Optional[str] = None
    default_summarizer_id: Optional[str] = None

    # Lifecycle markers.
    current_user_turn_id: Optional[int] = None
    last_compacted_event_id: int = -1

    # Coordinator-owned control state — replaces the prompt's "default to
    # responding" framing with explicit, replay-rebuildable knobs that
    # govern who may speak and how. See :class:`RoomControlState`.
    control: RoomControlState = field(default_factory=RoomControlState)

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def add_participant(self, info: ParticipantInfo) -> None:
        if info.id in self.participants:
            raise ValueError(f"participant {info.id!r} already registered")
        self.participants[info.id] = info
        self.room_epoch += 1

    def remove_participant(self, pid: str) -> dict[str, Optional[str]]:
        """Remove participant ``pid``. Returns dict of slot changes.

        Each entry is ``{slot_name: new_value}``; only slots that
        previously pointed to ``pid`` appear. Caller (coordinator) emits
        the corresponding ``*_changed`` control events.
        """
        if pid not in self.participants:
            raise ValueError(f"no such participant: {pid!r}")
        del self.participants[pid]
        self.room_epoch += 1

        slot_changes: dict[str, Optional[str]] = {}
        for slot in ("anchor_id", "chair_id",
                     "default_responder_id", "default_summarizer_id"):
            if getattr(self, slot) == pid:
                new_id = self.cheapest_active_capable()
                slot_changes[slot] = new_id
                setattr(self, slot, new_id)
        return slot_changes

    def set_active(self, pid: str, active: bool) -> None:
        if pid not in self.participants:
            raise ValueError(f"no such participant: {pid!r}")
        self.participants[pid].active = active
        # Activity changes don't bump epoch; only membership/slots do.

    # ------------------------------------------------------------------
    # Topic / slots
    # ------------------------------------------------------------------

    def set_topic(self, new_topic: Optional[str]) -> Optional[str]:
        old = self.topic
        self.topic = new_topic
        # Topic doesn't change membership — no epoch bump.
        return old

    def set_default_responder(self, pid: Optional[str]) -> Optional[str]:
        if pid is not None and pid not in self.participants:
            raise ValueError(f"no such participant: {pid!r}")
        old = self.default_responder_id
        self.default_responder_id = pid
        self.room_epoch += 1
        return old

    def set_anchor(self, pid: Optional[str]) -> Optional[str]:
        if pid is not None and pid not in self.participants:
            raise ValueError(f"no such participant: {pid!r}")
        old = self.anchor_id
        self.anchor_id = pid
        self.room_epoch += 1
        return old

    def set_chair(self, pid: Optional[str]) -> Optional[str]:
        if pid is not None and pid not in self.participants:
            raise ValueError(f"no such participant: {pid!r}")
        old = self.chair_id
        self.chair_id = pid
        # Chair has no protocol privilege — UI default only — but we still
        # bump the epoch so any leases tied to the prior chair invalidate
        # if anything ever conditions on it.
        self.room_epoch += 1
        return old

    def set_default_summarizer(self, pid: Optional[str]) -> Optional[str]:
        if pid is not None and pid not in self.participants:
            raise ValueError(f"no such participant: {pid!r}")
        old = self.default_summarizer_id
        self.default_summarizer_id = pid
        # Doesn't affect leases; bumping is consistent with the rest.
        self.room_epoch += 1
        return old

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------

    def cheapest_active_capable(self) -> Optional[str]:
        """Lowest ``cost_tier`` active+capable participant; tie-break by id.

        Used as fallback for any unset / removed slot occupant, and as
        the default summarizer if none is configured.
        """
        candidates = [p for p in self.participants.values()
                      if p.active and p.capable]
        if not candidates:
            return None
        candidates.sort(key=lambda p: (p.cost_tier, p.id))
        return candidates[0].id

    def resolve_default_responder(self) -> Optional[str]:
        """Currently-valid default responder.

        Returns the configured ``default_responder_id`` if it points to an
        active+capable participant; else falls back to
        :meth:`cheapest_active_capable`.
        """
        pid = self.default_responder_id
        if pid and pid in self.participants:
            p = self.participants[pid]
            if p.active and p.capable:
                return pid
        return self.cheapest_active_capable()

    def resolve_default_summarizer(self) -> Optional[str]:
        pid = self.default_summarizer_id
        if pid and pid in self.participants:
            p = self.participants[pid]
            if p.active and p.capable:
                return pid
        return self.cheapest_active_capable()

    # ------------------------------------------------------------------
    # Room control state setters
    # ------------------------------------------------------------------

    def set_roles(self, roles: dict[str, str]) -> dict[str, str]:
        """Replace the role-assignment map. Returns the previous mapping.

        Unknown participant ids are filtered silently — callers should
        validate before calling. An empty dict clears all roles.
        """
        old = dict(self.control.roles)
        clean = {pid: role for pid, role in roles.items()
                 if pid in self.participants}
        self.control.roles = clean
        return old

    def set_floor_owner(self,
                        floor_owner: Optional[list[str]]) -> Optional[list[str]]:
        """Set the floor owner list. Returns the previous value.

        ``None`` opens the floor (broadcast default). An empty list also
        opens the floor — for simplicity an empty floor is treated as
        no-floor. A non-empty list narrows ``allowed_speakers`` for
        every subsequent user post until cleared.
        """
        old = (list(self.control.floor_owner)
               if self.control.floor_owner is not None else None)
        if not floor_owner:
            self.control.floor_owner = None
        else:
            self.control.floor_owner = [pid for pid in floor_owner
                                        if pid in self.participants]
            if not self.control.floor_owner:
                self.control.floor_owner = None
        return old

    def set_wait_for_user(self, wait: bool) -> bool:
        old = self.control.wait_for_user
        self.control.wait_for_user = bool(wait)
        return old

    def set_style(self, style: StyleLevel) -> StyleLevel:
        old = self.control.style
        if style not in ("brief", "normal", "detailed"):
            raise ValueError(f"unknown style: {style!r}")
        self.control.style = style
        return old

    def set_turn_taking_mode(self, mode: TurnTakingMode) -> TurnTakingMode:
        """Switch ``"broadcast"`` ↔ ``"round_robin"``. Returns prior mode.

        Switching out of ``round_robin`` clears the rotation pointer and
        the turn order so a re-entry starts fresh. The interpreter is
        what decides *when* to switch (game-start / game-end phrases);
        this setter just performs the transition.
        """
        if mode not in ("broadcast", "round_robin"):
            raise ValueError(f"unknown turn_taking_mode: {mode!r}")
        old = self.control.turn_taking_mode
        self.control.turn_taking_mode = mode
        if mode == "broadcast":
            self.control.turn_order = []
            self.control.next_speaker_idx = 0
        return old

    def set_turn_order(self, order: list[str]) -> list[str]:
        """Set the round-robin participant order. Returns the previous list.

        Unknown ids are filtered silently. Setting a new order resets the
        rotation pointer to 0 — caller-friendly default for "we just
        entered round-robin, start from the top".
        """
        old = list(self.control.turn_order)
        self.control.turn_order = [pid for pid in order
                                   if pid in self.participants]
        self.control.next_speaker_idx = 0
        return old

    def advance_round_robin_pointer(self) -> int:
        """Advance the rotation pointer by one (modulo active turn order).

        Returns the new pointer value. Inactive / removed ids in
        ``turn_order`` are filtered before the modulo so the pointer
        stays within the live set. Returns ``0`` when the rotation has
        no live members (the next read will fall back to broadcast).
        """
        live = [pid for pid in self.control.turn_order
                if pid in self.participants
                and self.participants[pid].active
                and self.participants[pid].capable]
        if not live:
            self.control.next_speaker_idx = 0
            return 0
        self.control.next_speaker_idx = (
            (self.control.next_speaker_idx + 1) % len(live)
        )
        return self.control.next_speaker_idx

    # ------------------------------------------------------------------
    # Read-only view (for policy callbacks)
    # ------------------------------------------------------------------

    def view(self) -> "RoomStateView":
        """Return a read-only snapshot view of this state.

        Used by the coordinator to hand state to policy methods
        (``plan_user_turn`` / ``system_prompt`` / ``role_prompt``)
        without exposing setters or the live mutable container.

        Cheap to call — wraps the existing dicts/lists in immutable
        proxies; does not deep-copy. Mutations to the underlying
        ``RoomState`` after the view is taken *are* visible through
        the view (it's a live read-only window, not a snapshot copy).
        Callers that need a frozen point-in-time copy should call
        ``view()`` and serialize/copy what they need.
        """
        return RoomStateView(
            room_epoch=self.room_epoch,
            topic=self.topic,
            participants=MappingProxyType(self.participants),
            anchor_id=self.anchor_id,
            chair_id=self.chair_id,
            default_responder_id=self.default_responder_id,
            default_summarizer_id=self.default_summarizer_id,
            current_user_turn_id=self.current_user_turn_id,
            last_compacted_event_id=self.last_compacted_event_id,
            control=RoomControlStateView(
                roles=MappingProxyType(self.control.roles),
                floor_owner=(tuple(self.control.floor_owner)
                             if self.control.floor_owner is not None
                             else None),
                wait_for_user=self.control.wait_for_user,
                style=self.control.style,
                turn_taking_mode=self.control.turn_taking_mode,
                turn_order=tuple(self.control.turn_order),
                next_speaker_idx=self.control.next_speaker_idx,
            ),
        )


# ---------------------------------------------------------------------------
# Read-only views — passed to policies in lieu of the live RoomState
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoomControlStateView:
    """Read-only view of :class:`RoomControlState`.

    Lists become tuples; the roles mapping is wrapped in
    :class:`MappingProxyType`. Setters do not exist. Construct via
    :meth:`RoomState.view`, never directly.
    """
    roles: Mapping[str, str]
    floor_owner: Optional[Tuple[str, ...]]
    wait_for_user: bool
    style: StyleLevel
    turn_taking_mode: TurnTakingMode
    turn_order: Tuple[str, ...]
    next_speaker_idx: int


@dataclass(frozen=True)
class RoomStateView:
    """Read-only view of :class:`RoomState` for policy callbacks.

    What's read-only:

    - The view itself (``frozen=True``) cannot have its top-level fields
      reassigned.
    - ``participants`` is a :class:`MappingProxyType`; ``view.participants["x"] = ...``
      raises ``TypeError``.
    - ``control.roles`` is a :class:`MappingProxyType`; same protection.
    - ``control.turn_order`` and ``control.floor_owner`` are tuples;
      ``.append`` / index-assignment raise.

    What's *not* read-only (known soft leak, full deep freeze deferred
    to v0.2 with ``ParticipantInfoView``):

    - The :class:`ParticipantInfo` values inside ``participants`` are
      still mutable dataclasses. A policy that captures one and writes
      ``info.active = False`` will mutate live state. The boundary
      grep + import-asymmetry test catches this in practice; the deep
      view is on the v0.2 list.

    Read access (``view.participants["loom"].cost_tier``,
    ``view.control.turn_order``) is the supported path. Anything beyond
    reading is a policy bug.
    """
    room_epoch: int
    topic: Optional[str]
    participants: Mapping[str, ParticipantInfo]
    anchor_id: Optional[str]
    chair_id: Optional[str]
    default_responder_id: Optional[str]
    default_summarizer_id: Optional[str]
    current_user_turn_id: Optional[int]
    last_compacted_event_id: int
    control: RoomControlStateView
