"""Loom v0 — ParticipantActor + decision policy.

A :class:`ParticipantActor` is one daemon thread per participant. On
each bus wakeup it:

1. Reads new events visible to it (``bus.snapshot(audience=self.id,
   since=cursor)``), filtering out events it sent. The cursor is a
   single high-water mark: ``bus.snapshot(since=cursor)`` returns
   events with ``id > cursor``.
2. Picks the highest-priority trigger from the batch, calls
   :func:`decide` to produce an :class:`AgentDecision`.
3. Dispatches the decision:
   - ``SKIP`` → ``coordinator.handle_skip(self.id, trigger)``
   - ``DRAFT`` → ``coordinator.acquire_lease(...)`` then run streaming
     via the configured ``draft_handler`` callback. Streaming is a
     separate module (Section 7) and is supplied here so this module
     stays pure for unit tests.
4. **On lease denial, re-pend the trigger for replay** (v0.2.1,
   audit finding A1): the cursor still advances to ``max(snap.id)``
   to avoid a tight wakeup loop (kernel-emitted ``lease_denied``
   events keep ``bus.wait_after`` returning), but the denied trigger
   is added to the bounded LRU ``_pending_direct_mentions`` so the
   next ``_decide_once`` re-snapshots it via the replay path. A
   subsequent eligibility change (throttle reset, budget release,
   speaker cap clear) can then re-pick the trigger. The LRU's prior
   role (carrying user direct mentions that were considered but not
   picked) is preserved; it now also carries denied triggers of any
   priority class.

Trigger priority (highest first):

1. Direct ``@mention`` to this participant.
2. Rerouted ``dead_letter`` trigger assigned to this participant.
3. The user message that opened the current UserTurn — *if* this
   participant holds an unresolved obligation for it.

Tie-break: newest event wins (highest id) within the same priority class.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
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

    v0.2.1 (audit finding A2) dropped the previously unused
    ``considered_event_ids`` field. The actor advances its cursor
    using the in-hand snapshot, not a hint on the decision.
    """

    action: DecisionAction
    trigger_event_id: Optional[int]
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
    if not events or user_turn is None:
        return AgentDecision(
            action="SKIP",
            trigger_event_id=None,
            reason="empty batch" if not events else "no open user_turn",
        )

    trigger = pick_priority_trigger(events, my_id, user_turn, priority_fn=priority_fn)
    if trigger is None:
        return AgentDecision(
            action="SKIP",
            trigger_event_id=None,
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
            reason="direct_mention",
        )
    if is_dead_letter:
        return AgentDecision(
            action="DRAFT",
            trigger_event_id=trigger.id,
            reason="dead_letter_rerouted",
        )
    if has_obligation:
        return AgentDecision(
            action="DRAFT",
            trigger_event_id=trigger.id,
            reason="obligation",
        )
    return AgentDecision(
        action="SKIP",
        trigger_event_id=trigger.id,
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
        # v0.2.1 (audit A1): triggers whose most recent lease attempt
        # was denied. ``_decide_once`` short-circuits to SKIP when the
        # picked trigger is in this set, preventing the tight loop
        # where re-pending a denied trigger + kernel-emitted
        # ``lease_denied`` events cause ``wait_after`` to immediately
        # re-wake the actor. The set is cleared whenever a new
        # user-posted event arrives (signalling potential eligibility
        # change); individual entries are dropped on grant or when
        # the trigger ages out of the replay LRU.
        self._denied_trigger_ids: set[int] = set()

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

        v0.2.1 (audit finding A1): the dispatch outcome decides
        whether the trigger gets re-pended for replay. The cursor
        itself always advances to ``max(snap.id)`` to avoid the
        ``lease_denied`` → ``wait_after`` → re-decide tight loop.
        """
        snap, decision = self._decide_once()
        granted = self._dispatch_decision(decision)
        self._advance_cursor(snap)
        if decision.action == "DRAFT" and decision.trigger_event_id is not None:
            if granted:
                # Successful attempt — clear any stale denial entry.
                self._denied_trigger_ids.discard(decision.trigger_event_id)
            else:
                # Re-pend so the next eligibility-changing event
                # (a fresh user post — see ``_decide_once``) re-picks
                # this trigger. Mark it as denied so the actor does
                # not re-attempt under the same conditions.
                self._repend_trigger(decision.trigger_event_id)
                self._denied_trigger_ids.add(decision.trigger_event_id)
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

    def _decide_once(self) -> tuple[list[Event], AgentDecision]:
        """Compute the decision for one wakeup without advancing the cursor.

        Returns ``(snap, decision)``. The caller (``step``) advances
        the cursor based on the dispatch outcome — see
        :meth:`_advance_cursor`. v0.2.1 split the cursor advance off
        of this method per audit finding A1 so a lease denial cannot
        lose the trigger event.

        A new user-posted event in ``snap`` clears
        ``self._denied_trigger_ids`` since it signals possible
        eligibility change (throttle bucket rolling, budget reset, a
        new turn opening). If the picked trigger is still in the
        denied set after that clear, the decision is downgraded to
        ``SKIP`` so the actor doesn't re-attempt a known-failing
        trigger and tight-loop on its own ``lease_denied`` emissions.
        """
        snap = self.bus.snapshot(audience=self.id, since=self._cursor)
        snap = [e for e in snap if e.sender != self.id]

        if any(e.sender == "user" for e in snap):
            self._denied_trigger_ids.clear()

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

        if decision.action == "DRAFT" and decision.trigger_event_id in self._denied_trigger_ids:
            decision = AgentDecision(
                action="SKIP",
                trigger_event_id=None,
                reason="recently_denied_no_eligibility_change",
            )

        self._update_pending_mentions(decision, snap)
        return snap, decision

    def _advance_cursor(self, snap: list[Event]) -> None:
        """Advance ``self._cursor`` to ``max(snap.id)``.

        Cursor advance is unconditional because a stopped cursor
        combined with a kernel-emitted ``lease_denied`` event would
        wake the actor immediately via ``bus.wait_after`` and re-run
        the same denied decision. The trigger-not-lost guarantee is
        delivered via the replay LRU (see :meth:`_repend_trigger`)
        rather than via the cursor.

        Monotonic: ``new < self._cursor`` is silently discarded.
        """
        if not snap:
            return
        highest = max(e.id for e in snap)
        if highest > self._cursor:
            self._cursor = highest

    def _repend_trigger(self, trigger_id: int) -> None:
        """Add ``trigger_id`` to the replay LRU after a lease denial.

        Reuses the existing ``_pending_direct_mentions`` deque (whose
        replay path in :meth:`_decide_once` lifts pending event ids
        back into the next snap). The deque is bounded so unbounded
        re-pending cannot occur; if the same trigger keeps failing
        eligibility, it cycles out as new mentions arrive.
        """
        if trigger_id not in self._pending_direct_mentions:
            self._pending_direct_mentions.append(trigger_id)

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

    def _dispatch_decision(self, decision: AgentDecision) -> bool:
        """Dispatch ``decision`` and return ``True`` iff a lease was granted.

        The bool is consumed by :meth:`_advance_cursor` to decide
        whether to keep the trigger eligible for re-snapshotting
        (v0.2.1 audit A1). SKIP decisions always return ``False`` —
        no lease was requested, so there is no grant outcome and the
        cursor advances normally.
        """
        trigger = self._lookup_event(decision.trigger_event_id)
        if decision.action == "SKIP":
            self.coordinator.handle_skip(self.id, trigger)
            return False
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
            # SKIP record so the empty-batch path doesn't loop. The
            # cursor is held at trigger.id - 1 by ``_advance_cursor``
            # so the next eligibility change re-picks this trigger.
            self.coordinator.handle_skip(self.id, trigger)
            return False
        try:
            self.draft_handler(self, trigger, lease)
        finally:
            self.coordinator.release_lease(lease)
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _lookup_event(self, event_id: Optional[int]) -> Optional[Event]:
        if event_id is None:
            return None
        return self.bus.get(event_id)
