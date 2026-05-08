"""Loom v0 — TurnLease + RoomCoordinator.

The coordinator is the single owner of mutable room state. All actors
call into it; nothing else mutates :class:`RoomState`. Internally it
composes:

- ``Floor``       lease arbitration (eligibility-aware parallelism)
- ``ThrottleConfig``    per-participant + per-channel rate buckets
- ``LoopGuardConfig``   bag-of-words IoU duplicate detector
- ``BudgetConfig``      cumulative token tracker per UserTurn
- ``UserTurn``    the current obligations, drafts, idle timer

The coordinator is thread-safe via a single internal lock. All public
methods that mutate state hold the lock for the duration; readers
(``validate_lease``, ``user_turn``) are also lock-protected so in-flight
epoch updates can't tear.

It emits control events to the bus (``user_turn_*``,
``obligation_recorded``, ``obligation_resolved``,
``default_responder_changed``, ``dead_letter``,
``participant_added/removed``) but does not interpret chat content.
Chat-content interpretation (decisions, prompts, streaming) lives in
:mod:`actor` / :mod:`streaming` / :mod:`prompt`. Routing decisions
(who must respond) live in :mod:`interpreter`.

Sender authentication (P1, audit C1): every bus emission inside this
module uses :meth:`MessageBus.post_internal` rather than
:meth:`MessageBus.post`. The coordinator is privileged kernel code —
it posts events with sender ``"system"`` or ``"user"`` regardless of
which thread called the public method (the call may originate on an
actor's bound thread via e.g. :meth:`handle_skip` /
:meth:`on_stream_end`). ``post_internal`` is the documented bypass
for the thread-actor binding check.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, cast
import threading
import time

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.events import Event
from loom.kernel.obligations import (
    ResponseObligation,
    UserTurnPlan,
)
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
    StyleLevel,
    TurnTakingMode,
)
from loom.kernel.obligations import plan_for_default
from loom.kernel.user_turn import (
    ClosureReason,
    UserTurn,
    is_user_turn_complete,
    make_user_turn,
    should_open_new_user_turn,
)


# Watchdog threshold: log a ``policy_slow`` control event when a
# policy's ``plan_user_turn`` exceeds this many milliseconds. The
# coordinator holds its lock across the call, so a slow policy blocks
# every actor thread.
_POLICY_SLOW_THRESHOLD_MS = 100.0


PolicyErrorMode = str  # Literal["close_turn", "default_responder", "raise"]
"""Coordinator behavior when ``classify_fn`` raises.

- ``"close_turn"`` (default, fail-closed): emit ``policy_error`` then
  close the turn with no response. Library-default because "default
  responder" is a Loom-specific concept that breaks for debate /
  classroom / 20-questions policies.
- ``"default_responder"``: emit ``policy_error`` then fall back to
  :func:`plan_for_default` against ``state.default_responder_id``.
  Loom can opt into this for v0.0 behavioral compat.
- ``"raise"``: emit ``policy_error`` then re-raise the exception.
  Useful in dev mode to surface stack traces; do not use in prod.
"""


# ---------------------------------------------------------------------------
# TurnLease
# ---------------------------------------------------------------------------

@dataclass
class TurnLease:
    """One lease per granted draft slot. The bookkeeping fields
    ``acquired_at`` / ``expires_at`` are :func:`time.monotonic` values
    (P3.3 / audit TIME1) — wall-clock would let an NTP step backward
    widen the validity window or forward shrink it (causing stream-mid
    expiry); ``time.monotonic`` is unaffected by clock adjustments and
    is the right primitive for "did N seconds elapse since acquire".
    """
    id: int
    holder: str
    user_turn_id: int
    trigger_event_id: int
    room_epoch: int
    acquired_at: float                          # time.monotonic
    expires_at: float                           # time.monotonic
    valid: bool = True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoopGuardConfig:
    """Bag-of-words IoU duplicate detector.

    Suppresses near-duplicate short replies (e.g. "standing by",
    "waiting for argument") that would otherwise form idle chains.
    Returns True from :meth:`is_idle_dup` if the new reply should be
    dropped.

    Frozen dataclass: ``iou_threshold`` and ``short_text_chars`` are
    fixed at construction. The internal per-participant ``_last`` dict
    is mutable in-place — frozen prevents attribute reassignment, not
    mutation of dict contents (audit F4.4 / P2.2).
    """

    iou_threshold: float = 0.8
    short_text_chars: int = 50
    _last: dict[str, str] = field(
        default_factory=dict, init=False, compare=False, repr=False)

    def is_idle_dup(self, participant_id: str, new_text: str) -> bool:
        prev = self._last.get(participant_id)
        if not prev:
            return False
        if len(new_text) >= self.short_text_chars:
            return False
        return self._iou(prev, new_text) > self.iou_threshold

    def record(self, participant_id: str, text: str) -> None:
        self._last[participant_id] = text

    @staticmethod
    def _iou(a: str, b: str) -> float:
        sa = set(a.lower().split())
        sb = set(b.lower().split())
        if not sa and not sb:
            return 1.0
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)


@dataclass(frozen=True)
class ThrottleConfig:
    """Per-participant + per-channel rate buckets, sliding 60-second window.

    Frozen: limits are immutable after construction; the per-participant
    and per-channel history dicts mutate in place.
    """

    per_participant_per_min: int = 10
    per_channel_per_min: int = 60
    _participant_hist: dict[str, list[float]] = field(
        default_factory=dict, init=False, compare=False, repr=False)
    _channel_hist: dict[str, list[float]] = field(
        default_factory=dict, init=False, compare=False, repr=False)

    def try_consume(self, participant_id: str, channel: str,
                    now: Optional[float] = None) -> bool:
        now = now if now is not None else time.monotonic()
        cutoff = now - 60.0
        ph = self._participant_hist.setdefault(participant_id, [])
        ch = self._channel_hist.setdefault(channel, [])
        ph[:] = [t for t in ph if t >= cutoff]
        ch[:] = [t for t in ch if t >= cutoff]
        if len(ph) >= self.per_participant_per_min:
            return False
        if len(ch) >= self.per_channel_per_min:
            return False
        ph.append(now)
        ch.append(now)
        return True


@dataclass(frozen=True)
class BudgetConfig:
    """Cumulative-cost tracker, scoped per UserTurn.

    Frozen: ``max_tokens_per_user_turn`` is fixed at construction; the
    per-turn usage map mutates in place.
    """

    max_tokens_per_user_turn: int = 200_000
    _per_turn: dict[int, int] = field(
        default_factory=dict, init=False, compare=False, repr=False)

    def can_acquire(self, user_turn_id: Optional[int],
                    estimated_cost: int = 0) -> bool:
        if user_turn_id is None:
            return True
        used = self._per_turn.get(user_turn_id, 0)
        return (used + estimated_cost) <= self.max_tokens_per_user_turn

    def record(self, user_turn_id: Optional[int], cost: int) -> None:
        if user_turn_id is None:
            return
        self._per_turn[user_turn_id] = (
            self._per_turn.get(user_turn_id, 0) + cost
        )

    def used(self, user_turn_id: int) -> int:
        return self._per_turn.get(user_turn_id, 0)


# ---------------------------------------------------------------------------
# RoomCoordinator
# ---------------------------------------------------------------------------

class RoomCoordinator:
    """Single mutator of :class:`RoomState`. Thread-safe.

    Public methods either succeed and emit at-most-one matching control
    event, or are no-ops. They never raise on legal-but-no-op cases
    (e.g. setting the same default responder); they raise only on
    programmer errors (unknown participant id).
    """

    def __init__(self, bus: MessageBus, state: RoomState,
                 *,
                 policy_error_mode: PolicyErrorMode = "close_turn") -> None:
        self.bus = bus
        self.state = state
        self.config: RoomConfig = state.config
        self._lock = threading.RLock()

        if policy_error_mode not in ("close_turn", "default_responder",
                                     "raise"):
            raise ValueError(
                f"unknown policy_error_mode: {policy_error_mode!r}")
        self.policy_error_mode: PolicyErrorMode = policy_error_mode

        self._leases: dict[int, TurnLease] = {}
        self._next_lease_id = 0

        self._user_turn: Optional[UserTurn] = None
        self._next_user_turn_id = 0
        self._next_obligation_id = 1
        self._last_user_post_ts: Optional[float] = None

        self._loop_guard = LoopGuardConfig()
        self._throttle = ThrottleConfig()
        self._budget = BudgetConfig()

        self._compaction_in_flight = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def user_turn(self) -> Optional[UserTurn]:
        with self._lock:
            return self._user_turn

    @property
    def loop_guard(self) -> LoopGuardConfig:
        return self._loop_guard

    @property
    def budget(self) -> BudgetConfig:
        return self._budget

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def register_participant(self, info: ParticipantInfo) -> None:
        with self._lock:
            self.state.add_participant(info)
            self.bus.post_internal(ev.participant_added(info.id, info.role_hints))

    def unregister_participant(self, pid: str) -> None:
        """Remove participant, re-resolve slots, dead-letter pending mentions.

        Also marks any open obligation held by ``pid`` as resolved-by-
        removal so the UserTurn can still close cleanly.

        The caller (slash-command handler) is responsible for stopping
        the participant's actor thread separately.
        """
        with self._lock:
            slot_changes = self.state.remove_participant(pid)
            self.bus.post_internal(ev.participant_removed(pid))

            # Slot-change events. Only default_responder_changed is
            # required v0; anchor/chair/summarizer get analogues if any
            # were defined for the room.
            if "default_responder_id" in slot_changes:
                self.bus.post_internal(ev.default_responder_changed(
                    pid, slot_changes["default_responder_id"]))
            for slot in ("anchor_id", "chair_id", "default_summarizer_id"):
                if slot in slot_changes:
                    factory = {
                        "anchor_id": "anchor_changed",
                        "chair_id": "chair_changed",
                        "default_summarizer_id": "default_summarizer_changed",
                    }[slot]
                    self.bus.post_internal(ev._control(
                        factory, old_id=pid, new_id=slot_changes[slot]))

            # Invalidate any of pid's in-flight leases.
            for lease in list(self._leases.values()):
                if lease.holder == pid:
                    lease.valid = False

            # Transfer any required (must/should) obligations held by pid
            # to a live fallback BEFORE resolving the originals. This
            # keeps the turn open across the removal so the rerouted
            # agent has a real obligation to drive a draft. If no
            # fallback is available, fall through to the resolve-only
            # path below (turn will close cleanly via the trailing
            # ``_maybe_close_user_turn_locked``).
            self._transfer_required_obligations_locked(pid, slot_changes)

            # Mark pid's open obligations resolved-administratively so a
            # closure check later won't hang the turn.
            if self._user_turn:
                for ob in list(self._user_turn.obligations.values()):
                    if ob.participant_id == pid and not ob.resolved:
                        self._resolve_obligation_locked(
                            ob.id, by_event_id=None)

            # Dead-letter pending direct mentions to pid.
            self._dead_letter_pending_mentions(pid, slot_changes)

            # The removal may have closed out the last unresolved required
            # obligation; re-check for completion.
            self._maybe_close_user_turn_locked()

    def _transfer_required_obligations_locked(
        self, removed_pid: str,
        slot_changes: dict[str, Optional[str]],
    ) -> None:
        """Re-route required obligations held by a removed participant.

        For each unresolved ``must``/``should`` obligation held by
        ``removed_pid``, if a fallback (default responder slot, then
        cheapest active capable) is available and doesn't already hold
        an obligation in this turn, allocate a new obligation on the
        fallback and emit ``obligation_recorded``. The original
        obligation is resolved by the caller immediately after.

        Skips silently when no fallback exists or the candidate already
        holds an obligation — callers fall back to the
        resolve-only path.
        """
        ut = self._user_turn
        if ut is None or ut.state != "open":
            return
        reroute_to = slot_changes.get(
            "default_responder_id",
            self.state.default_responder_id,
        )
        if reroute_to is None or reroute_to == removed_pid:
            reroute_to = self.state.cheapest_active_capable()
        if reroute_to is None or reroute_to == removed_pid:
            return
        # Skip if the candidate has already drafted in this turn (no
        # need to obligate them a second time) or already holds an
        # unresolved obligation here.
        if reroute_to in ut.drafted:
            return
        if ut.obligation_for(reroute_to) is not None:
            return

        for ob in list(ut.obligations.values()):
            if ob.participant_id != removed_pid or ob.resolved:
                continue
            if ob.level not in ("must", "should"):
                continue
            new_ob, next_oid = ut.add_obligation(
                participant_id=reroute_to,
                level=ob.level,
                target_event_ids=list(ob.target_event_ids),
                reason=f"rerouted_from_{removed_pid}",
                next_obligation_id=self._next_obligation_id,
            )
            self._next_obligation_id = next_oid
            # Allow the rerouted speaker to acquire a draft lease — the
            # frozen plan's ``allowed_speakers`` was scoped before the
            # removal and would otherwise reject them.
            ut.frozen_plan.allowed_speakers.add(reroute_to)
            self.bus.post_internal(ev.obligation_recorded(
                obligation_id=new_ob.id,
                participant_id=new_ob.participant_id,
                level=new_ob.level,
                target_event_ids=list(new_ob.target_event_ids),
                reason=new_ob.reason,
            ))
            # Only transfer once — additional must/should obligations
            # from the removed participant collapse onto the same
            # fallback rather than producing N duplicate obligations.
            return

    def _dead_letter_pending_mentions(
        self, removed_pid: str,
        slot_changes: dict[str, Optional[str]],
    ) -> None:
        """Re-route or dead-letter outstanding @mentions to ``removed_pid``."""
        if not self._user_turn:
            return
        ut = self._user_turn
        snap = self.bus.snapshot(channel="main", kinds=["chat"],
                                 since=ut.user_event_id - 1)
        last_response_id = -1
        for e in snap:
            if e.sender == removed_pid:
                last_response_id = e.id
        for e in snap:
            if removed_pid in e.addressees and e.id > last_response_id:
                reroute_to = slot_changes.get("default_responder_id",
                                              self.state.default_responder_id)
                if reroute_to is None:
                    reroute_to = self.state.cheapest_active_capable()
                self.bus.post_internal(ev.dead_letter(
                    original_mention_event_id=e.id,
                    reroute_to=reroute_to,
                    reason="participant_removed",
                ))

    # ------------------------------------------------------------------
    # Topic / slots
    # ------------------------------------------------------------------

    def set_topic(self, new_topic: Optional[str]) -> None:
        with self._lock:
            old = self.state.topic
            if old == new_topic:
                return
            if self._user_turn and self._user_turn.state == "open":
                self._close_user_turn_locked("topic_changed")
            self.state.set_topic(new_topic)
            self.bus.post_internal(ev.topic_changed(old, new_topic or ""))

    def set_default_responder(self, pid: Optional[str]) -> None:
        with self._lock:
            old = self.state.default_responder_id
            if old == pid:
                return
            self.state.set_default_responder(pid)
            for lease in self._leases.values():
                lease.valid = False
            self.bus.post_internal(ev.default_responder_changed(old, pid))

    def set_anchor(self, pid: Optional[str]) -> None:
        with self._lock:
            old = self.state.anchor_id
            if old == pid:
                return
            self.state.set_anchor(pid)
            self.bus.post_internal(ev._control("anchor_changed",
                                      old_id=old, new_id=pid))

    def set_chair(self, pid: Optional[str]) -> None:
        with self._lock:
            old = self.state.chair_id
            if old == pid:
                return
            self.state.set_chair(pid)
            self.bus.post_internal(ev._control("chair_changed",
                                      old_id=old, new_id=pid))

    def set_default_summarizer(self, pid: Optional[str]) -> None:
        with self._lock:
            old = self.state.default_summarizer_id
            if old == pid:
                return
            self.state.set_default_summarizer(pid)
            self.bus.post_internal(ev._control("default_summarizer_changed",
                                      old_id=old, new_id=pid))

    # ------------------------------------------------------------------
    # Room control state — roles / floor / wait_for_user / style
    # ------------------------------------------------------------------

    def set_roles(self, roles: dict[str, str]) -> None:
        """Replace the role-assignment map and emit ``roles_assigned``.

        Unknown participant ids are filtered silently. Pass ``{}`` to
        clear all roles. Roles drive every selected speaker's TurnCard
        until reassigned.
        """
        with self._lock:
            old = self.state.set_roles(roles)
            new = dict(self.state.control.roles)
            if new == old:
                return
            self.bus.post_internal(ev.roles_assigned(new))

    def set_floor_owner(self, floor_owner: Optional[list[str]],
                        *,
                        wait_for_user: Optional[bool] = None) -> None:
        """Set or clear the floor owner.

        Pass ``None`` (or empty list) to open the floor. Emits
        ``floor_updated`` with whatever fields changed.

        P2.3: ``active_goal`` parameter removed; topic now flows
        through :meth:`set_topic` (single source of truth in
        :attr:`RoomState.topic`).
        """
        with self._lock:
            old = self.state.set_floor_owner(floor_owner)
            payload: dict = {}
            new_floor = self.state.control.floor_owner
            if old != new_floor:
                payload["floor_owner"] = (
                    list(new_floor) if new_floor is not None else [])
            if wait_for_user is not None:
                old_wait = self.state.set_wait_for_user(wait_for_user)
                if old_wait != self.state.control.wait_for_user:
                    payload["wait_for_user"] = (
                        self.state.control.wait_for_user)
            if not payload:
                return
            # ``floor_updated`` accepts only the fields that changed.
            self.bus.post_internal(ev.floor_updated(
                floor_owner=payload.get("floor_owner"),
                wait_for_user=payload.get("wait_for_user"),
            ))

    def set_style(self, style: StyleLevel) -> None:
        """Update the brevity preference."""
        with self._lock:
            old = self.state.set_style(style)
            new = self.state.control.style
            if old == new:
                return
            self.bus.post_internal(ev.style_changed(old, new))

    # ------------------------------------------------------------------
    # UserTurn lifecycle
    # ------------------------------------------------------------------

    def post_user_event_and_open_turn(
        self, user_event: Event,
        classify_fn: Callable[[Event], UserTurnPlan],
    ) -> UserTurnPlan:
        """Atomically post a user chat event and open its UserTurn.

        Holds the coordinator lock across:

        1. ``bus.post(user_event)``  (assigns event.id, notifies actors)
        2. ``classify_fn(user_event)``  (runs the interpreter)
        3. ``_apply_plan_state_changes_locked(plan)``  (rotation /
           mode-flip side effects from the plan)
        4. ``open_user_turn(user_event, plan)``  (unless plan is an
           acknowledgement — no turn opens for those)

        Without this guard there is a real race: ``bus.post`` notifies
        actor threads, which then read ``coordinator.user_turn`` BEFORE
        the caller has opened a turn for the new event. The actor sees
        ``user_turn=None``, decides SKIP, and advances its cursor past
        the user event — so when the turn does open, the user event is
        no longer in the actor's window and no trigger fires.

        Holding ``self._lock`` here forces actor threads to block on
        ``coordinator.user_turn`` until this method returns; by then the
        turn is open and the actor sees the correct trigger.
        """
        with self._lock:
            self.bus.post_internal(user_event)
            plan = self._run_policy_under_lock(classify_fn, user_event)
            # Apply plan-driven state changes (turn_taking_mode /
            # turn_order) BEFORE the open check so acknowledgement plans
            # carrying ``set_turn_taking_mode="broadcast"`` (game-end
            # phrase exit) still flip the mode even though no turn opens.
            self._apply_plan_state_changes_locked(plan)
            if plan.routing_case != "acknowledgement":
                self.open_user_turn(user_event, plan)
        return plan

    def _run_policy_under_lock(
        self,
        classify_fn: Callable[[Event], UserTurnPlan],
        user_event: Event,
    ) -> UserTurnPlan:
        """Run ``classify_fn`` with a watchdog (timing + error handling).

        Caller holds ``self._lock``. We measure wall time around the
        call; if it exceeds ``_POLICY_SLOW_THRESHOLD_MS`` we emit a
        ``policy_slow`` control event for observability (no
        interruption — Python can't safely cancel arbitrary code).

        On exception we emit ``policy_error`` and dispatch on
        ``self.policy_error_mode``:

        - ``"close_turn"`` returns an acknowledgement-shaped plan so the
          caller skips ``open_user_turn`` (turn closes silently).
        - ``"default_responder"`` falls back to :func:`plan_for_default`.
        - ``"raise"`` re-raises after the ``policy_error`` event has
          been recorded.
        """
        t0 = time.monotonic()
        try:
            plan = classify_fn(user_event)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            self.bus.post_internal(ev._control(
                "policy_error",
                exception_class=type(exc).__name__,
                message=str(exc)[:500],
                elapsed_ms=round(elapsed_ms, 3),
                user_event_id=user_event.id,
            ))
            if self.policy_error_mode == "raise":
                raise
            if self.policy_error_mode == "default_responder":
                return plan_for_default(
                    self.state.resolve_default_responder(),
                    reason="policy_error",
                    target_event_ids=[user_event.id],
                    rationale="policy raised; falling back to default responder",
                )
            # ``close_turn`` (default, fail-closed): return an
            # acknowledgement-shaped plan so the outer caller's
            # ``routing_case != "acknowledgement"`` guard skips the open.
            from loom.kernel.obligations import plan_for_acknowledgement
            return plan_for_acknowledgement(
                target_event_ids=[user_event.id],
                rationale="policy raised; turn closed (fail-closed)",
            )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if elapsed_ms > _POLICY_SLOW_THRESHOLD_MS:
            self.bus.post_internal(ev._control(
                "policy_slow",
                elapsed_ms=round(elapsed_ms, 3),
                threshold_ms=_POLICY_SLOW_THRESHOLD_MS,
                user_event_id=user_event.id,
            ))
        return plan

    def _apply_plan_state_changes_locked(self, plan: UserTurnPlan) -> None:
        """Apply :class:`UserTurnPlan` state-mutation directives to ``state.control``.

        The interpreter is pure; it returns a plan with ``set_turn_taking_mode``
        and / or ``set_turn_order`` set when an external state change is
        warranted (e.g. auto-enabling round-robin on a game-start phrase).
        The coordinator is the only mutator, so we apply them here under
        the lock.

        ``advance_turn_pointer`` is read at turn-close time (see
        :meth:`_close_user_turn_locked`), not here.
        """
        if plan.set_turn_taking_mode is not None:
            self.state.set_turn_taking_mode(
                cast(TurnTakingMode, plan.set_turn_taking_mode))
        if plan.set_turn_order is not None:
            self.state.set_turn_order(plan.set_turn_order)

    def open_user_turn(self, user_event: Event,
                       plan: UserTurnPlan) -> UserTurn:
        """Open a new UserTurn for ``user_event`` with the interpreter's plan.

        Caller (input loop) is responsible for posting ``user_event`` to
        the bus first. This method:

        - debounces (returns the existing UserTurn and bumps activity
          if within ``user_turn_debounce_ms`` of last user post),
        - or closes any open UserTurn (with reason ``"new_user_post"``),
        - opens a fresh UserTurn carrying ``plan``,
        - emits ``user_turn_opened`` and ``obligation_recorded`` events,
        - returns the (possibly preexisting) UserTurn.
        """
        with self._lock:
            # P2.5: duration math (debounce, last_activity_at) uses
            # ``time.monotonic`` — wall-clock subtraction is unsafe when
            # the system clock steps. ``user_event.ts`` (wall-clock) is
            # for replay correlation only.
            now = time.monotonic()
            if not should_open_new_user_turn(
                self._last_user_post_ts, now,
                self.config.user_turn_debounce_ms,
            ) and self._user_turn and self._user_turn.state == "open":
                # Record the debounced event id so actors with open
                # obligations on this turn still wake on the new post.
                if user_event.id != self._user_turn.user_event_id:
                    self._user_turn.debounced_event_ids.add(user_event.id)
                self._user_turn.last_activity_at = now
                self._last_user_post_ts = now
                return self._user_turn

            if self._user_turn and self._user_turn.state == "open":
                self._close_user_turn_locked("new_user_post")

            # A user post implicitly clears any prior wait_for_user
            # gate — the user has spoken, so the room may resume.
            if self.state.control.wait_for_user:
                self.state.set_wait_for_user(False)
                self.bus.post_internal(ev.floor_updated(wait_for_user=False))

            turn, next_oid = make_user_turn(
                turn_id=self._next_user_turn_id,
                user_event_id=user_event.id,
                plan=plan,
                started_at=now,
                next_obligation_id=self._next_obligation_id,
            )
            self._next_user_turn_id += 1
            self._next_obligation_id = next_oid
            self._user_turn = turn
            self.state.current_user_turn_id = turn.id
            self._last_user_post_ts = now

            self.bus.post_internal(ev.user_turn_opened(
                user_turn_id=turn.id,
                routing_case=plan.routing_case,
                required_participants=sorted(plan.required_participants),
                optional_participants=sorted(plan.optional_participants),
                rationale=plan.rationale,
            ))
            for ob in turn.obligations.values():
                self.bus.post_internal(ev.obligation_recorded(
                    obligation_id=ob.id,
                    participant_id=ob.participant_id,
                    level=ob.level,
                    target_event_ids=list(ob.target_event_ids),
                    reason=ob.reason,
                ))

            # If the plan declared no required participants and no
            # optional participants, there's nothing to wait for —
            # close immediately as ``no_responder``.
            if (not plan.required_participants
                    and not plan.optional_participants):
                self._close_user_turn_locked("no_responder")
            return turn

    def close_user_turn(self, reason: ClosureReason = "cancelled") -> None:
        """Public closure entry point. Marks all open obligations resolved.

        The ``cancelled`` reason resolves obligations administratively so
        downstream closure checks see a clean turn. Other reasons leave
        obligation state intact (for later analysis), but the turn is
        still closed.
        """
        with self._lock:
            if reason == "cancelled" and self._user_turn:
                for ob in list(self._user_turn.obligations.values()):
                    if not ob.resolved:
                        self._resolve_obligation_locked(
                            ob.id, by_event_id=None)
            self._close_user_turn_locked(reason)

    def _close_user_turn_locked(self, reason: ClosureReason) -> None:
        if not self._user_turn or self._user_turn.state == "closed":
            return
        plan = self._user_turn.frozen_plan
        self._user_turn.close(reason)
        self.state.current_user_turn_id = None
        self.bus.post_internal(ev.user_turn_closed(
            user_turn_id=self._user_turn.id,
            reason=reason,
        ))
        # Apply ``wait_for_user_after`` from the plan now that the turn
        # is over. The flag stays set until the next user post (which
        # clears it inside ``open_user_turn``). It also fires for
        # cancelled turns — the user explicitly stopped the floor.
        if plan.wait_for_user_after and not self.state.control.wait_for_user:
            self.state.set_wait_for_user(True)
            self.bus.post_internal(ev.floor_updated(wait_for_user=True))
        # Advance the round-robin rotation pointer if the closed plan
        # came from the rotation (``advance_turn_pointer=True``) and the
        # mode is still active. ``@-mention`` / vocative overrides leave
        # this flag False so the rotation slot is preserved.
        if (plan.advance_turn_pointer
                and self.state.control.turn_taking_mode == "round_robin"):
            self.state.advance_round_robin_pointer()

    def _maybe_close_user_turn_locked(self) -> None:
        """Auto-close when all required obligations resolve OR the
        committed-reply count has reached ``max_responses``.

        ``max_responses`` is the per-turn cap declared by the
        :class:`UserTurnPlan`; once enough drafts commit the turn closes
        early even if some optional/observer participants haven't
        spoken. The cap is what enforces "max_responses=1" for directed
        turns (mention, floor) without depending on every other
        participant emitting a clean ``[PASS]``.
        """
        if not self._user_turn or self._user_turn.state != "open":
            return
        ut = self._user_turn
        committed_count = len(ut.drafted)
        cap = ut.frozen_plan.max_responses
        cap_reached = cap > 0 and committed_count >= cap
        if cap_reached or is_user_turn_complete(ut):
            # The turn ran to natural completion — every required
            # obligation was resolved (by a committed draft or by PASS)
            # and/or the response cap was hit. ``no_responder`` is
            # reserved for empty-plan turns, handled at open-time.
            self._close_user_turn_locked("completed")

    def check_idle_timeout(self, *, now: Optional[float] = None) -> None:
        """Called from a poller / timer to close idle UserTurns.

        Closes with ``obligation_unresolved`` if any required
        obligation is still open; otherwise plain ``idle_timeout``.
        """
        with self._lock:
            if not self._user_turn or self._user_turn.state != "open":
                return
            if self._user_turn.is_idle(
                idle_timeout_s=self.config.user_turn_idle_timeout_s,
                now=now,
            ):
                if self._user_turn.unresolved_required():
                    # TODO(v0.1): retry or synthesize a fallback for the
                    # un-replied required participant before closing.
                    self._close_user_turn_locked("obligation_unresolved")
                else:
                    self._close_user_turn_locked("idle_timeout")

    # ------------------------------------------------------------------
    # Obligation helpers
    # ------------------------------------------------------------------

    def obligation_for(self, holder: str,
                       trigger_event_id: Optional[int] = None,
                       ) -> Optional[ResponseObligation]:
        """Return ``holder``'s open obligation in the current UserTurn, if any.

        ``trigger_event_id`` is reserved for future disambiguation (e.g.
        per-mention obligations); the v0 deterministic interpreter emits
        one obligation per participant per turn so the parameter is
        informational.
        """
        with self._lock:
            if not self._user_turn:
                return None
            return self._user_turn.obligation_for(holder)

    def _resolve_obligation_locked(
            self, obligation_id: int, *,
            by_event_id: Optional[int],
            expected_holder: Optional[str] = None) -> None:
        """Mark an obligation resolved.

        P3.2 / audit C2: the optional ``expected_holder`` parameter is
        a defensive guard for future callers that derive
        ``obligation_id`` from data they do not fully trust. Today
        the only public path through ``on_stream_end`` already gates
        on ``lease.holder`` before reaching this helper, so today the
        check is a no-op assertion. A future caller that loses the
        holder check would otherwise resolve obligations for arbitrary
        participants — passing ``expected_holder`` makes that mismatch
        loud rather than silent.
        """
        if not self._user_turn:
            return
        ut = self._user_turn
        if expected_holder is not None:
            ob_pre = ut.obligations.get(obligation_id)
            if ob_pre is not None and ob_pre.participant_id != expected_holder:
                raise ValueError(
                    f"obligation {obligation_id} belongs to "
                    f"{ob_pre.participant_id!r}, not the expected "
                    f"holder {expected_holder!r}")
        if ut.mark_obligation_resolved(obligation_id,
                                       by_event_id=by_event_id):
            ob = ut.obligations.get(obligation_id)
            if ob is not None:
                self.bus.post_internal(ev.obligation_resolved(
                    obligation_id=obligation_id,
                    participant_id=ob.participant_id,
                    resolved_by_event_id=by_event_id,
                ))

    # ------------------------------------------------------------------
    # Leases
    # ------------------------------------------------------------------

    def acquire_lease(
        self,
        holder: str,
        trigger_event_id: int,
        *,
        is_direct_mention: bool = False,
    ) -> Optional[TurnLease]:
        """Try to acquire a lease for ``holder``. Returns ``None`` on rejection.

        Rejection reasons (non-exhaustive):
        - no open UserTurn
        - holder unknown or inactive
        - holder is not in the plan's ``allowed_speakers`` (and not
          user-direct-mentioned, which still bypasses the gate)
        - speaker cap reached for unprompted draft
        - throttle / budget exceeded

        ``is_direct_mention`` is reserved for the actor's user-direct-
        mention path (trigger event sender == "user" AND holder in
        addressees). Agent-to-agent ``@``-mentions do NOT pass this
        flag — they go through the standard ``allowed_speakers`` gate
        so chains close at ``max_responses``.
        """
        with self._lock:
            if not self._user_turn or self._user_turn.state != "open":
                return None
            if holder not in self.state.participants:
                return None
            p = self.state.participants[holder]
            if not p.active:
                return None

            ut = self._user_turn
            plan = ut.frozen_plan

            # Allowed-speakers gate. Direct user mention bypasses (the
            # user explicitly addressed them; the floor narrowing in the
            # plan was implicit). Plans with empty ``allowed_speakers``
            # (e.g. legacy callers) fall back to the old check.
            if plan.allowed_speakers:
                if holder not in plan.allowed_speakers and not is_direct_mention:
                    return None
            else:
                has_obligation = ut.obligation_for(holder) is not None
                is_optional = holder in ut.optional_participants
                if not (has_obligation or is_optional or is_direct_mention):
                    return None

            if not is_direct_mention:
                cap = self.config.max_drafts_per_participant
                if ut.speaker_counts.get(holder, 0) >= cap:
                    return None

            # ``max_responses`` cap — concurrency-safe gate. Counts
            # already-committed drafts plus outstanding valid leases for
            # this turn whose holder hasn't yet committed (a committed
            # lease is already accounted for via ``ut.drafted``). Direct
            # user mention bypasses the cap.
            if not is_direct_mention:
                cap_max = plan.max_responses
                if cap_max > 0:
                    committed = len(ut.drafted)
                    outstanding = sum(
                        1 for lease in self._leases.values()
                        if lease.user_turn_id == ut.id and lease.valid
                        and lease.holder not in ut.drafted
                    )
                    if committed + outstanding >= cap_max:
                        return None

            if not self._throttle.try_consume(holder, "main"):
                return None

            if not self._budget.can_acquire(ut.id):
                return None

            # P3.3 / audit TIME1: lease bookkeeping uses time.monotonic
            # so an NTP step cannot widen or shrink the validity window.
            now = time.monotonic()
            lease = TurnLease(
                id=self._next_lease_id,
                holder=holder,
                user_turn_id=ut.id,
                trigger_event_id=trigger_event_id,
                room_epoch=self.state.room_epoch,
                acquired_at=now,
                expires_at=now + self.config.lease_ttl_s,
            )
            self._next_lease_id += 1
            self._leases[lease.id] = lease
            return lease

    def validate_lease(self, lease: TurnLease) -> bool:
        with self._lock:
            if not lease.valid:
                return False
            if lease.id not in self._leases:
                return False
            if lease.room_epoch != self.state.room_epoch:
                lease.valid = False
                return False
            # P3.3 / audit TIME1: monotonic compare against the
            # monotonic-stamped expires_at; wall-clock skew cannot
            # bypass or accelerate expiry.
            if time.monotonic() > lease.expires_at:
                lease.valid = False
                return False
            return True

    def release_lease(self, lease: TurnLease) -> None:
        with self._lock:
            self._leases.pop(lease.id, None)
            lease.valid = False

    # ------------------------------------------------------------------
    # Stream / decision callbacks (called by streaming.py & actor.py)
    # ------------------------------------------------------------------

    def on_stream_end(
        self,
        lease: TurnLease,
        status: str,
        *,
        committed_text: Optional[str] = None,
        cost_tokens: int = 0,
        committed_event_id: Optional[int] = None,
    ) -> None:
        """Record terminal stream_end and fire the right control events.

        Called by ``streaming.run_streaming_call`` after it emits
        ``stream_end(...)`` on the bus.

        - ``committed`` → mark drafted, charge budget, record loop-guard,
          resolve the holder's obligation (if any). The caller threads
          ``committed_event_id`` directly from the ``bus.post(chat_event)``
          return value; we never re-derive it via a snapshot scan.
        - ``passed`` → the agent emitted ``[PASS]``. Resolve the holder's
          obligation administratively (no chat, no draft mark) so the
          turn closes cleanly instead of idle-timing-out on a required
          participant who deliberately declined the floor.
        - ``suppressed`` → post-stream filter rejected the body. Leave
          obligation intact (idle timeout will close as
          ``obligation_unresolved`` if the holder was required).
        - ``cancelled`` / ``error`` / ``lease_expired`` → same as
          suppressed: leave obligation intact.
        """
        with self._lock:
            self._budget.record(lease.user_turn_id, cost_tokens)
            ut = self._user_turn
            if not ut:
                return

            triggering = self._lookup_event(lease.trigger_event_id)
            is_direct = bool(
                triggering and lease.holder in triggering.addressees
            )

            if status == "committed":
                ut.mark_drafted(
                    lease.holder,
                    count_toward_cap=not is_direct,
                )
                if committed_text:
                    self._loop_guard.record(lease.holder, committed_text)
                ob = ut.obligation_for(lease.holder)
                if ob is not None:
                    # P3.2: pass expected_holder so the helper rejects
                    # any future caller that derives obligation_id from
                    # untrusted data without first checking holder.
                    self._resolve_obligation_locked(
                        ob.id, by_event_id=committed_event_id,
                        expected_holder=lease.holder)
            elif status == "passed":
                # PASS is a valid completion: the agent took the floor
                # and chose silence. Resolve the obligation but do not
                # mark a draft (no chat was committed) so speaker_count
                # caps are unaffected.
                ob = ut.obligation_for(lease.holder)
                if ob is not None:
                    self._resolve_obligation_locked(
                        ob.id, by_event_id=None,
                        expected_holder=lease.holder)

            self._maybe_close_user_turn_locked()

    def handle_skip(self, holder: str,
                    trigger_event: Optional[Event] = None) -> None:
        """Record a SKIP decision for an actor that did not draft.

        v0 has no debate path — SKIP is a soft no-op for state. The
        coordinator just bumps the turn's last activity so empty-batch
        wakeups don't immediately re-fire idle.
        """
        with self._lock:
            ut = self._user_turn
            if not ut or ut.state != "open":
                return
            ut.last_activity_at = time.monotonic()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _lookup_event(self, event_id: int) -> Optional[Event]:
        if event_id is None:
            return None
        return self.bus.get(event_id)

    def in_flight_lease_count(self) -> int:
        with self._lock:
            return len(self._leases)
