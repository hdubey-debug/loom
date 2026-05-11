"""Loom v0 — ParticipantActor + decision policy.

A :class:`ParticipantActor` is one daemon thread per participant. On
each bus wakeup it:

1. Reads new events visible to it (``bus.snapshot(audience=self.id,
   since=cursor)``), filtering out events it sent.
2. Picks the highest-priority trigger from the batch, calls
   :func:`decide` to produce an :class:`AgentDecision`.
3. Prunes its cursor: events in ``considered_event_ids`` are not
   reconsidered; direct mentions to self that were NOT selected as the
   trigger remain pending in a bounded LRU.
4. Dispatches the decision:
   - ``SKIP`` → ``coordinator.handle_skip(self.id, trigger)``
   - ``DRAFT`` → ``coordinator.acquire_lease(...)`` then run streaming
     via the configured ``draft_handler`` callback. Streaming is a
     separate module (Section 7) and is supplied here so this module
     stays pure for unit tests.

Trigger priority (highest first):

1. Direct ``@mention`` to this participant.
2. Rerouted ``dead_letter`` trigger assigned to this participant.
3. The user message that opened the current UserTurn — *if* this
   participant holds an unresolved obligation for it.

Tie-break: newest event wins (highest id) within the same priority class.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Literal, Optional
import threading

from loom.kernel import events as ev
from loom.kernel.bus import _KERNEL_AUTH, MessageBus
from loom.kernel.coordinator import RoomCoordinator, TurnLease
from loom.kernel.events import Event, control_type_of
from loom.kernel.user_turn import UserTurn


DecisionAction = Literal["SKIP", "DRAFT"]


@dataclass
class AgentDecision:
    """Output of :func:`decide` — what an actor wants to do this wakeup.

    - ``action="SKIP"`` — the actor saw events but elected not to draft;
      the coordinator updates per-actor bookkeeping (``handle_skip``).
    - ``action="DRAFT"`` — the actor wants to draft a reply for
      ``trigger_event_id``; the coordinator grants a lease (or denies).

    ``considered_event_ids`` is the actor's "I have processed these,
    do not redeliver" cursor advance.
    """

    action: DecisionAction
    trigger_event_id: Optional[int]
    considered_event_ids: list[int] = field(default_factory=list)
    reason: str = ""


# ---------------------------------------------------------------------------
# Trigger priority
# ---------------------------------------------------------------------------


def _trigger_priority(event: Event, my_id: str, user_turn: Optional[UserTurn]) -> Optional[int]:
    """Lower number = higher priority. ``None`` = not actionable.

    Priority 1 (direct mention) is gated on ``event.sender == "user"``:
    only the human's direct addresses are treated as a top-priority
    trigger. Agent-to-agent ``@``-mentions are intentionally NOT
    actionable here; the room's allowed-speakers gate (in
    :meth:`RoomCoordinator.acquire_lease`) handles inter-agent
    addressing through the obligation/allowed path.

    v0.2: this function is the kernel default; advanced users can
    override the priority hook via
    :attr:`loom.kernel.room.RoomConfig.trigger_priority` (which
    defaults to :data:`DEFAULT_TRIGGER_PRIORITY`, a re-export of
    this function under the public name).
    """
    if event.kind == "chat" and event.sender == "user" and my_id in event.addressees:
        return 1  # user direct @mention
    if (
        event.kind == "control"
        and isinstance(event.body, dict)
        and control_type_of(event) == "dead_letter"
        and event.body.get("reroute_to") == my_id
    ):
        return 2  # rerouted to me
    # Required obligation transferred to me on participant removal —
    # ``rerouted_from_<pid>`` is the canonical reason set by
    # :meth:`RoomCoordinator._transfer_required_obligations_locked`.
    if (
        event.kind == "control"
        and isinstance(event.body, dict)
        and control_type_of(event) == "obligation_recorded"
        and event.body.get("participant_id") == my_id
        and isinstance(event.body.get("reason"), str)
        and event.body["reason"].startswith("rerouted_from_")
    ):
        return 2  # transferred to me
    # The user post that opened the current UserTurn is a trigger only
    # if I hold an unresolved obligation in that turn. This replaces the
    # legacy "user broadcast" priority — non-required participants no
    # longer have an actionable user-post trigger.
    if (
        event.kind == "chat"
        and event.sender == "user"
        and user_turn is not None
        and (event.id == user_turn.user_event_id or event.id in user_turn.debounced_event_ids)
        and user_turn.obligation_for(my_id) is not None
    ):
        return 3  # required for current turn
    return None


# v0.2: public re-export of the kernel default. Custom callables with
# the same shape can replace it via ``RoomConfig.trigger_priority``.
DEFAULT_TRIGGER_PRIORITY = _trigger_priority


def pick_priority_trigger(
    events: Iterable[Event],
    my_id: str,
    user_turn: Optional[UserTurn],
    *,
    priority_fn: Optional[Callable[[Event, str, Optional[UserTurn]], Optional[int]]] = None,
) -> Optional[Event]:
    """Pick the highest-priority event from ``events``, or ``None``.

    Sort key: ``(priority_class_asc, -event.id)``. Newest event wins
    inside a priority class.

    ``priority_fn`` defaults to :data:`DEFAULT_TRIGGER_PRIORITY`
    (the kernel's three-class scheme); pass a custom callable with
    the same shape to override per-call. When invoked from the
    actor loop the caller forwards
    :attr:`RoomConfig.trigger_priority` here.
    """
    fn = priority_fn or DEFAULT_TRIGGER_PRIORITY
    candidates: list[tuple[int, int, Event]] = []
    for e in events:
        prio = fn(e, my_id, user_turn)
        if prio is None:
            continue
        candidates.append((prio, -e.id, e))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


# ---------------------------------------------------------------------------
# Decision policy
# ---------------------------------------------------------------------------


def decide(
    events: list[Event],
    my_id: str,
    user_turn: Optional[UserTurn],
    *,
    priority_fn: Optional[Callable[[Event, str, Optional[UserTurn]], Optional[int]]] = None,
) -> AgentDecision:
    """Pure decision function. No mutation, no I/O.

    ``events`` is the per-wakeup batch (already audience-filtered and
    self-filtered). ``user_turn`` is the current open UserTurn; ``None``
    means the room is idle and there is nothing to draft.

    DRAFT iff the highest-priority trigger is one of:
    - a direct @-mention to me, OR
    - a dead_letter rerouted to me, OR
    - the user post that opened the current turn AND I hold an
      unresolved obligation for it.

    ``priority_fn`` overrides the kernel's :data:`DEFAULT_TRIGGER_PRIORITY`
    classifier. The actor loop forwards
    :attr:`RoomConfig.trigger_priority`.
    """
    considered = [e.id for e in events]
    if not events or user_turn is None:
        return AgentDecision(
            action="SKIP",
            trigger_event_id=None,
            considered_event_ids=considered,
            reason="empty batch" if not events else "no open user_turn",
        )

    trigger = pick_priority_trigger(events, my_id, user_turn, priority_fn=priority_fn)
    if trigger is None:
        return AgentDecision(
            action="SKIP",
            trigger_event_id=None,
            considered_event_ids=considered,
            reason="no actionable trigger",
        )

    is_direct = trigger.kind == "chat" and trigger.sender == "user" and my_id in trigger.addressees
    is_dead_letter = (
        trigger.kind == "control"
        and isinstance(trigger.body, dict)
        and control_type_of(trigger) == "dead_letter"
    )
    has_obligation = user_turn.obligation_for(my_id) is not None

    if is_direct:
        return AgentDecision(
            action="DRAFT",
            trigger_event_id=trigger.id,
            considered_event_ids=considered,
            reason="direct_mention",
        )
    if is_dead_letter:
        return AgentDecision(
            action="DRAFT",
            trigger_event_id=trigger.id,
            considered_event_ids=considered,
            reason="dead_letter_rerouted",
        )
    if has_obligation:
        return AgentDecision(
            action="DRAFT",
            trigger_event_id=trigger.id,
            considered_event_ids=considered,
            reason="obligation",
        )
    return AgentDecision(
        action="SKIP",
        trigger_event_id=trigger.id,
        considered_event_ids=considered,
        reason="not_eligible",
    )


# ---------------------------------------------------------------------------
# ParticipantActor
# ---------------------------------------------------------------------------

DraftHandler = Callable[["ParticipantActor", Event, TurnLease], None]
"""Callback the actor invokes after acquiring a lease.

Implemented by :mod:`loom.chat.loom.streaming` in production. Tests can
substitute a mock that records calls without firing a provider.
"""


class ParticipantActor:
    """One daemon thread per participant."""

    def __init__(
        self,
        participant_id: str,
        bus: MessageBus,
        coordinator: RoomCoordinator,
        draft_handler: DraftHandler,
        *,
        wakeup_timeout_s: Optional[float] = None,
        pending_mention_capacity: int = 100,
    ) -> None:
        self.id = participant_id
        self.bus = bus
        self.coordinator = coordinator
        self.draft_handler = draft_handler
        self.wakeup_timeout_s = (
            wakeup_timeout_s
            if wakeup_timeout_s is not None
            else min(
                coordinator.config.user_turn_idle_timeout_s,
                coordinator.config.lease_ttl_s,
            )
        )
        self._cursor = -1
        self._stopped = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending_direct_mentions: deque[int] = deque(maxlen=pending_mention_capacity)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spin up the daemon thread that drives this actor's wakeup loop.

        Idempotent — a second call while running is a no-op.
        """
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name=f"loom-actor-{self.id}",
        )
        self._thread.start()

    def stop(self, *, timeout: float = 1.0) -> None:
        """Signal the actor to exit and join its thread.

        Idempotent. Safe to call from any thread.
        """
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def stopped(self) -> bool:
        """``True`` once :meth:`stop` has been signalled."""
        return self._stopped.is_set()

    # ------------------------------------------------------------------
    # One iteration (used by tests + the loop)
    # ------------------------------------------------------------------

    def step(self) -> AgentDecision:
        """Process one wakeup synchronously and return the decision.

        Useful in tests: the bus, coordinator, and actor can be driven
        without spawning a thread. The production loop simply calls
        :meth:`step` after each ``bus.wait_after`` wakeup.
        """
        decision = self._decide_once()
        self._dispatch_decision(decision)
        return decision

    def _loop(self) -> None:
        # P1 / sender authentication: bind this thread to the actor's id
        # so any ``bus.post`` from the actor's draft handler / proxy /
        # streaming code is sender-authenticated. Privileged emissions
        # (actor_error with sender="system") use ``post_internal`` to
        # bypass. The unbind handle is invoked in the finally block so a
        # stopped or crashed actor does not leak a stale binding.
        unbind = self.bus.bind_actor(self.id)
        try:
            while not self._stopped.is_set():
                new_len = self.bus.wait_after(self._cursor, timeout=self.wakeup_timeout_s)
                if self._stopped.is_set() or self.bus.stopped:
                    return
                if new_len <= self._cursor + 1:
                    self.coordinator.check_idle_timeout()
                    continue
                self._step_with_error_handling()
        finally:
            unbind()

    def _step_with_error_handling(self) -> None:
        try:
            self.step()
        except Exception as exc:
            try:
                # actor_error has sender="system"; this code runs on
                # the actor's bound thread, so ``post`` would raise
                # ``SenderMismatchError``. ``post_internal`` is the
                # documented kernel-internal escape hatch for the
                # crash-handler emission path.
                self.bus.post_internal(
                    ev.actor_error(
                        participant_id=self.id,
                        exception_class=type(exc).__name__,
                        message=str(exc)[:500],
                    ),
                    auth=_KERNEL_AUTH,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Decision pipeline
    # ------------------------------------------------------------------

    def _decide_once(self) -> AgentDecision:
        snap = self.bus.snapshot(audience=self.id, since=self._cursor)
        snap = [e for e in snap if e.sender != self.id]

        if self._pending_direct_mentions:
            seen = {e.id for e in snap}
            replays: list[Event] = []
            for ev_id in list(self._pending_direct_mentions):
                if ev_id in seen:
                    continue
                hit = self._lookup_event(ev_id)
                if hit is None:
                    try:
                        self._pending_direct_mentions.remove(ev_id)
                    except ValueError:
                        pass
                    continue
                replays.append(hit)
            snap = replays + snap

        priority_fn = self.coordinator.config.trigger_priority or None
        decision = decide(snap, self.id, self.coordinator.user_turn, priority_fn=priority_fn)

        if snap:
            highest = max(e.id for e in snap)
            if highest > self._cursor:
                self._cursor = highest

        self._update_pending_mentions(decision, snap)
        return decision

    def _update_pending_mentions(self, decision: AgentDecision, considered: list[Event]) -> None:
        # Only user-sourced mentions are actionable in v0; pending-LRU
        # tracks the same set so we don't store agent-to-agent mentions
        # we would refuse to draft for anyway.
        for e in considered:
            if (
                e.kind == "chat"
                and e.sender == "user"
                and self.id in e.addressees
                and e.id != decision.trigger_event_id
            ):
                if e.id not in self._pending_direct_mentions:
                    self._pending_direct_mentions.append(e.id)
        if decision.trigger_event_id is not None:
            try:
                self._pending_direct_mentions.remove(decision.trigger_event_id)
            except ValueError:
                pass

    def _dispatch_decision(self, decision: AgentDecision) -> None:
        trigger = self._lookup_event(decision.trigger_event_id)
        if decision.action == "SKIP":
            self.coordinator.handle_skip(self.id, trigger)
            return
        # DRAFT. Direct-mention bypass is restricted to user-sourced
        # mentions — agent-to-agent @ goes through the standard
        # allowed_speakers gate so chains close at max_responses.
        is_direct = bool(trigger and trigger.sender == "user" and self.id in trigger.addressees)
        # action == "DRAFT" guarantees both fields are set; the asserts
        # encode this invariant for the type checker and fail loudly if
        # a future code path violates it.
        assert decision.trigger_event_id is not None
        assert trigger is not None
        lease = self.coordinator.acquire_lease(
            self.id,
            decision.trigger_event_id,
            is_direct_mention=is_direct,
        )
        if lease is None:
            # Speaker cap, throttle, or budget rejected. Fall back to a
            # SKIP record so the empty-batch path doesn't loop.
            self.coordinator.handle_skip(self.id, trigger)
            return
        try:
            self.draft_handler(self, trigger, lease)
        finally:
            self.coordinator.release_lease(lease)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _lookup_event(self, event_id: Optional[int]) -> Optional[Event]:
        if event_id is None:
            return None
        return self.bus.get(event_id)
