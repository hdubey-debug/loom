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
from typing import Callable, Optional
import threading
import time

from loom.contracts import (
    PASSED,
    ConversationPolicy,
    LeaseCheck,
    LeaseCheckResult,
)
from loom.kernel import events as ev
from loom.kernel.bus import _KERNEL_AUTH, MessageBus
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
)
from loom.kernel.actor_state import register_cursor_advanced_reducer
from loom.kernel.budgets import BudgetLedger, register_budget_reducers
from loom.kernel.capabilities import (
    CapabilityName,
    CapabilityState,
    register_capability_reducers,
)
from loom.kernel.control_actions import (
    ControlActionRegistry,
    ControlActionResult,
    DenialReason,
    build_kernel_action_registry,
)
from loom.kernel.floor_overrides import (
    FLOOR_OVERRIDE_ACTIONS,
    prune_overrides_for_lease,
    prune_overrides_for_turn,
    register_floor_override_reducer,
)
from loom.kernel.causality import TraceContext, child_span, new_trace
from loom.kernel.leases import (
    ALL_LEASE_KINDS,
    ControlActionContext,
    Lease,
    LeaseContext,
    LeaseKind,
    ReactiveContext,
    SummarizationContext,
    UserTurnContext,
    check_applies_to,
)
from loom.kernel.state import KernelState, new_kernel_state
from loom.kernel.context import (
    ContextScope,
    SummaryFailureReason,
    SummaryRecord,
    validate_summary_record,
)
from loom.kernel.effects import (
    AnchorAssignedEffect,
    ChairAssignedEffect,
    CompactionDisabledEffect,
    ControlEffect,
    DefaultResponderSetEffect,
    DefaultSummarizerSetEffect,
    EffectRegistry,
    RolesAssignedEffect,
    StyleChangedEffect,
    SummaryCommittedEffect,
    SummaryFailedEffect,
    SummaryProposedEffect,
    TopicChangedEffect,
    build_kernel_registry,
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
#
# v0.3 PR 13 (closes audit D3): the per-policy slow threshold now
# lives at :attr:`loom.kernel.room.RoomConfig.policy_slow_threshold_ms`.
# The module-level constant remains as the back-compat default
# (`getattr(config, "policy_slow_threshold_ms", _POLICY_SLOW_THRESHOLD_MS)`)
# so pre-v0.3 RoomConfig pickles continue to drive the same behavior.
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
# Lock discipline (v0.3 PR 2 / doctrine P4 / §2)
# ---------------------------------------------------------------------------


class _TrackedRLock:
    """:class:`threading.RLock` wrapper that records the owning thread.

    The stdlib RLock exposes no portable way to ask "does the current
    thread hold this lock?" — the pure-Python ``_RLock._owner`` slot is
    an implementation detail. This wrapper records the owner alongside
    a depth counter so :meth:`RoomCoordinator._assert_not_holding_lock`
    can fail loudly when an I/O entry point is invoked under-lock.

    Drop-in for ``threading.RLock``: supports ``__enter__`` / ``__exit__``
    (the dominant call site is ``with coord._lock: ...``) plus the
    legacy ``acquire`` / ``release`` methods. Reentrant: nested
    acquires from the same thread succeed without bumping the owner.
    """

    __slots__ = ("_lock", "_owner_ident", "_depth")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner_ident: Optional[int] = None
        self._depth = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        ok = self._lock.acquire(blocking, timeout)
        if ok:
            self._owner_ident = threading.get_ident()
            self._depth += 1
        return ok

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner_ident = None
        self._lock.release()

    def __enter__(self) -> "_TrackedRLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def is_held_by_current_thread(self) -> bool:
        return self._depth > 0 and self._owner_ident == threading.get_ident()


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
    acquired_at: float  # time.monotonic
    expires_at: float  # time.monotonic
    valid: bool = True


# ---------------------------------------------------------------------------
# v0.3.x PR 3 — SummaryCommitResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchedulingResult:
    """Outcome of :meth:`RoomCoordinator.schedule_summarization` (Path A).

    ``scheduled`` is True iff a SUMMARIZATION lease was acquired and
    a ``summarization_scheduled`` audit event was emitted. On failure,
    ``denial_reason`` carries a short structured string (e.g.
    ``"no_default_summarizer"``, ``"scope_disabled"``,
    ``"lease_denied:not_default_summarizer"``).
    """

    scheduled: bool
    lease_id: Optional[int]
    summarizer_id: Optional[str]
    scope: Optional[ContextScope]
    denial_reason: Optional[str] = None


@dataclass(frozen=True)
class SummaryCommitResult:
    """Outcome of :meth:`RoomCoordinator.submit_summary_proposed`.

    ``committed`` is the headline boolean; on success ``reason`` is
    ``None`` and ``committed_at_event_id`` is the bus id of the
    ``summary_committed`` event. On failure ``reason`` carries the
    structural :class:`SummaryFailureReason`, ``details`` carries the
    validator's short diagnostic string, and ``failed_validator`` is
    ``"structural"`` (off-lock pre-validator) or ``"anchor"`` (under-
    lock anchor check).
    """

    committed: bool
    summary_id: str
    reason: Optional[SummaryFailureReason]
    details: Optional[str]
    failed_validator: Optional[str]
    committed_at_event_id: Optional[int]


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
    _last: dict[str, str] = field(default_factory=dict, init=False, compare=False, repr=False)

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
        default_factory=dict, init=False, compare=False, repr=False
    )
    _channel_hist: dict[str, list[float]] = field(
        default_factory=dict, init=False, compare=False, repr=False
    )

    def try_consume(self, participant_id: str, channel: str, now: Optional[float] = None) -> bool:
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
    _per_turn: dict[int, int] = field(default_factory=dict, init=False, compare=False, repr=False)

    def can_acquire(self, user_turn_id: Optional[int], estimated_cost: int = 0) -> bool:
        if user_turn_id is None:
            return True
        used = self._per_turn.get(user_turn_id, 0)
        return (used + estimated_cost) <= self.max_tokens_per_user_turn

    def record(self, user_turn_id: Optional[int], cost: int) -> None:
        if user_turn_id is None:
            return
        self._per_turn[user_turn_id] = self._per_turn.get(user_turn_id, 0) + cost

    def used(self, user_turn_id: int) -> int:
        return self._per_turn.get(user_turn_id, 0)


# ---------------------------------------------------------------------------
# Default LeaseCheck chain (v0.2)
# ---------------------------------------------------------------------------
#
# The kernel's eight built-in lease-grant gates. Each preserves the
# v0.1.2 behavior exactly; the only change is that rejection now goes
# through :class:`loom.contracts.LeaseCheckResult` (and emits an
# observable ``lease_denied`` event) instead of returning ``None``
# silently. Custom checks plug in via ``RoomConfig.lease_checks``.


class _OpenTurnCheck:
    name = "open_turn"
    # v0.3 PR 7 / doctrine §3: applies only to USER_TURN leases — control
    # action / reactive / tool leases don't gate on the user turn being
    # open.
    applies_to = frozenset({LeaseKind.USER_TURN})

    def check(
        self, *, holder, trigger_event_id, is_direct_mention, coordinator
    ) -> LeaseCheckResult:
        del holder, trigger_event_id, is_direct_mention
        ut = coordinator._user_turn
        if ut is None or ut.state != "open":
            return LeaseCheckResult(False, "no_open_user_turn")
        return PASSED


class _ParticipantRegisteredCheck:
    name = "participant_registered"
    # Applies to every lease kind — even REACTIVE / CONTROL_ACTION
    # holders must be known participants.
    applies_to = ALL_LEASE_KINDS

    def check(
        self, *, holder, trigger_event_id, is_direct_mention, coordinator
    ) -> LeaseCheckResult:
        del trigger_event_id, is_direct_mention
        if holder not in coordinator.state.participants:
            return LeaseCheckResult(False, "unknown_participant")
        return PASSED


class _ParticipantActiveCheck:
    name = "participant_active"
    applies_to = ALL_LEASE_KINDS

    def check(
        self, *, holder, trigger_event_id, is_direct_mention, coordinator
    ) -> LeaseCheckResult:
        del trigger_event_id, is_direct_mention
        info = coordinator.state.participants.get(holder)
        if info is None or not info.active:
            return LeaseCheckResult(False, "participant_inactive")
        return PASSED


class _AllowedSpeakerCheck:
    name = "allowed_speaker"
    applies_to = frozenset({LeaseKind.USER_TURN})

    def check(
        self, *, holder, trigger_event_id, is_direct_mention, coordinator
    ) -> LeaseCheckResult:
        del trigger_event_id
        ut = coordinator._user_turn
        plan = ut.frozen_plan
        if plan.allowed_speakers:
            if holder not in plan.allowed_speakers and not is_direct_mention:
                return LeaseCheckResult(False, "not_in_allowed_speakers")
        else:
            # Legacy fallback path: no explicit allowed_speakers on the
            # plan → require an obligation, optional status, or direct
            # mention. Same semantics as v0.1.2.
            has_obligation = ut.obligation_for(holder) is not None
            is_optional = holder in ut.optional_participants
            if not (has_obligation or is_optional or is_direct_mention):
                return LeaseCheckResult(False, "no_obligation")
        return PASSED


class _PerParticipantCapCheck:
    name = "per_participant_cap"
    applies_to = frozenset({LeaseKind.USER_TURN})

    def check(
        self, *, holder, trigger_event_id, is_direct_mention, coordinator
    ) -> LeaseCheckResult:
        del trigger_event_id
        if is_direct_mention:
            return PASSED
        cap = coordinator.config.max_drafts_per_participant
        ut = coordinator._user_turn
        if ut.speaker_counts.get(holder, 0) >= cap:
            return LeaseCheckResult(False, "speaker_cap_reached")
        return PASSED


class _MaxResponsesCheck:
    name = "max_responses"
    applies_to = frozenset({LeaseKind.USER_TURN})

    def check(
        self, *, holder, trigger_event_id, is_direct_mention, coordinator
    ) -> LeaseCheckResult:
        del trigger_event_id
        if is_direct_mention:
            return PASSED
        ut = coordinator._user_turn
        cap_max = ut.frozen_plan.max_responses
        if cap_max <= 0:
            return PASSED
        committed = len(ut.drafted)
        outstanding = sum(
            1
            for lease in coordinator._leases.values()
            if lease.user_turn_id == ut.id and lease.valid and lease.holder not in ut.drafted
        )
        if committed + outstanding >= cap_max:
            return LeaseCheckResult(False, "max_responses_reached")
        return PASSED


class _ThrottleCheck:
    name = "throttle"
    # USER_TURN only — control actions and reactive leases are not
    # subject to the chat-rate throttle bucket (each kind would
    # justify its own bucket if needed; that's a v0.4+ refinement).
    applies_to = frozenset({LeaseKind.USER_TURN})

    def check(
        self, *, holder, trigger_event_id, is_direct_mention, coordinator
    ) -> LeaseCheckResult:
        del trigger_event_id, is_direct_mention
        # ``try_consume`` is side-effecting (decrements the bucket); it
        # must run exactly when we'd grant the lease, so this check
        # lives near the tail of the chain.
        if not coordinator._throttle.try_consume(holder, "main"):
            return LeaseCheckResult(False, "throttle_exceeded")
        return PASSED


class _BudgetCheck:
    name = "budget"
    # v0.3 PR 7: budget cap applies to USER_TURN today. PR 12 will
    # extend to every lease kind that pays for an external dependency
    # (tool calls, workflow steps). For now control_action / reactive
    # leases are exempt because their cost model is undefined.
    applies_to = frozenset({LeaseKind.USER_TURN})

    def check(
        self, *, holder, trigger_event_id, is_direct_mention, coordinator
    ) -> LeaseCheckResult:
        del holder, trigger_event_id, is_direct_mention
        ut = coordinator._user_turn
        if not coordinator._budget.can_acquire(ut.id):
            return LeaseCheckResult(False, "budget_exceeded")
        return PASSED


class _CapabilityCheck:
    """v0.3 PR 7 / doctrine §3 — capability gate for CONTROL_ACTION leases.

    Inspects the lease context to identify the action being proposed
    and confirms the holder has the required capability via
    :class:`CapabilityState.has`. PR 9 wires the
    ``required_capability`` mapping from each ``ControlAction``; for
    PR 7 the check is wired into the default check tuple so the
    structural shape is in place — without PR 9's action registry
    every CONTROL_ACTION proposal is denied as
    ``"insufficient_capability"`` until the corresponding capability
    is granted.

    The user (holder == ``"user"``) bypasses the check per P15 —
    human root actions enter via the slash-command path (PR 11) and
    are not subject to the agent capability gate.
    """

    name = "capability"
    # v0.3.x PR 5: SUMMARIZATION leases also gate on a required
    # capability (read off SummarizationContext.required_capability).
    applies_to = frozenset({LeaseKind.CONTROL_ACTION, LeaseKind.SUMMARIZATION})

    def check(
        self, *, holder, trigger_event_id, is_direct_mention, coordinator
    ) -> LeaseCheckResult:
        del trigger_event_id, is_direct_mention
        if holder == "user":
            return PASSED
        ctx = getattr(coordinator, "_pending_lease_context", None)
        from loom.kernel.leases import SummarizationContext
        if not isinstance(ctx, (ControlActionContext, SummarizationContext)):
            # Defensive: PR 7 always sets the context before invoking
            # the check chain for a typed lease. Missing context means
            # the caller used the v0.2 untyped path (USER_TURN) — this
            # check doesn't apply.
            return PASSED
        # v0.3 PR 9: the context carries the action's
        # ``required_capability`` directly. Fall back to a name→enum
        # match (legacy bare-context tests) when absent.
        cap_value = ctx.required_capability
        if cap_value is None:
            cap_value = ctx.action_name
        try:
            cap = CapabilityName(cap_value)
        except ValueError:
            return LeaseCheckResult(False, "unknown_control_action")
        caps = coordinator.kernel_state.capabilities
        if caps is None or not caps.has(holder, cap):
            return LeaseCheckResult(False, "insufficient_capability")
        return PASSED


class _SummarizerSlotCheck:
    """v0.3.x PR 5 / doctrine P22 — Path A slot enforcement.

    Verifies that a SUMMARIZATION lease's holder matches the room's
    :attr:`RoomState.default_summarizer_id`. Path A (policy-triggered)
    auto-summarisation must run as the slot occupant; Path B (control
    action) is gated separately via the SUMMARIZE capability and is
    NOT subject to the slot check (a granted SUMMARIZE capability can
    arrive at a non-summariser participant, as in
    ``/summarize <alt-agent>``).

    The ``triggered_by`` field on
    :class:`SummarizationContext` discriminates: ``"policy"`` enforces
    the slot match; ``"control_action"`` skips it (the capability
    check is the gate).
    """

    name = "summarizer_slot"
    applies_to = frozenset({LeaseKind.SUMMARIZATION})

    def check(
        self, *, holder, trigger_event_id, is_direct_mention, coordinator
    ) -> LeaseCheckResult:
        del trigger_event_id, is_direct_mention
        ctx = getattr(coordinator, "_pending_lease_context", None)
        from loom.kernel.leases import SummarizationContext
        if not isinstance(ctx, SummarizationContext):
            return PASSED
        if ctx.triggered_by != "policy":
            # Path B (control_action) — capability check is the gate;
            # holder need not be the slot occupant.
            return PASSED
        slot = coordinator.state.default_summarizer_id
        if slot is None:
            return LeaseCheckResult(False, "no_default_summarizer")
        if holder != slot:
            return LeaseCheckResult(False, "not_default_summarizer")
        return PASSED


DEFAULT_LEASE_CHECKS: tuple[LeaseCheck, ...] = (
    _OpenTurnCheck(),
    _ParticipantRegisteredCheck(),
    _ParticipantActiveCheck(),
    _AllowedSpeakerCheck(),
    _PerParticipantCapCheck(),
    _MaxResponsesCheck(),
    _ThrottleCheck(),
    _BudgetCheck(),
    # v0.3 PR 7: only fires for CONTROL_ACTION (+ v0.3.x PR 5
    # SUMMARIZATION) leases (filtered via ``check_applies_to``);
    # USER_TURN flows are unaffected because the check's
    # ``applies_to`` set excludes USER_TURN.
    _CapabilityCheck(),
    # v0.3.x PR 5: SUMMARIZATION-only slot enforcement for Path A.
    _SummarizerSlotCheck(),
)


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

    def __init__(
        self,
        bus: MessageBus,
        state: "RoomState | KernelState",
        *,
        policy_error_mode: PolicyErrorMode = "close_turn",
        policy: Optional["ConversationPolicy"] = None,
    ) -> None:
        self.bus = bus
        # v0.3 P5 / §1: KernelState is now the canonical mutable root.
        # For back-compat the constructor still accepts a bare RoomState
        # (every test + library call site in v0.2 passes one); we wrap
        # it. ``self.state`` continues to expose the RoomState so v0.2
        # call sites (coordinator-internal, runtime, tests) keep working
        # — it is now an alias for ``self._kernel.room``. v0.3
        # subsystem PRs (5, 6, 13) access their state via
        # ``self._kernel.capabilities`` / ``.budget`` / ``.actors``.
        if isinstance(state, KernelState):
            self._kernel: KernelState = state
        else:
            self._kernel = new_kernel_state(state)
        self.state = self._kernel.room
        self.config: RoomConfig = self.state.config
        # v0.3 PR 2 / doctrine P4: _TrackedRLock records the owning
        # thread so I/O entry points can assert no lock is held before
        # they begin a potentially-blocking call. Stdlib RLock has no
        # portable owner check.
        self._lock = _TrackedRLock()

        # v0.3 PR 3 / doctrine P6, P7, §5: typed effect registry. Slot
        # mutations route through ``_apply_effect`` so future PRs can
        # add reducers (capability — PR 5, budget — PR 6, lease taxonomy
        # — PR 8, floor overrides — PR 10, cursor — PR 13) by extending
        # this same registry instance.
        self._effect_registry: EffectRegistry = build_kernel_registry()
        # v0.3 PR 5 (doctrine §6, P1, P10): the capability ledger lives
        # on ``KernelState.capabilities``; the three reducers register
        # themselves onto the coordinator's registry instance at
        # room construction so ``_apply_effect`` can dispatch a
        # ``CapabilityGrantedEffect`` / ``Revoked`` / ``Expired``
        # without further wiring.
        if self._kernel.capabilities is None:
            self._kernel.capabilities = CapabilityState()
        register_capability_reducers(self._effect_registry)
        # v0.3 PR 6 (doctrine §9, P9): the budget ledger lives on
        # ``KernelState.budget``; the three reducers (reserve / commit
        # / refund) register themselves alongside the capability ones.
        # PR 7 (lease unification) wires reservation into the
        # ``acquire_lease`` path; PR 8 wires commit/refund into the
        # unified ``release_lease(reason=...)`` taxonomy. PR 6 ships
        # the ledger so the registry slot exists and unit tests can
        # drive end-to-end accounting flows.
        if self._kernel.budget is None:
            self._kernel.budget = BudgetLedger()
        register_budget_reducers(self._effect_registry)

        # v0.3 PR 9 (doctrine §7, P14): control-action dispatch
        # registry. Hydrated with the kernel built-ins + PR 10's floor
        # override actions + any ``RoomConfig.custom_control_actions``
        # (v0.3 reserves the config field; pre-v0.3 RoomConfig
        # instances pass an empty tuple via ``getattr``).
        customs = tuple(getattr(self.config, "custom_control_actions", ()) or ())
        self._action_registry: ControlActionRegistry = (
            build_kernel_action_registry(FLOOR_OVERRIDE_ACTIONS + customs)
        )
        # v0.3 PR 10 (doctrine §10): wire the FloorOverrideEffect
        # reducer onto the same registry the slot setters use.
        register_floor_override_reducer(self._effect_registry)
        # v0.3 PR 13 (closes audit A3): wire the CursorAdvancedEffect
        # reducer so actor cursor state can persist via the journal.
        # The actor side opt-in is a v0.3.x follow-up; PR 13 ships
        # the data shape + reducer so the registry slot exists.
        register_cursor_advanced_reducer(self._effect_registry)

        # v0.3 PR 4 / doctrine P12: trace context. ``_trace_root`` is
        # the room-session-scoped trace (allocated at coordinator
        # construction; outlives every lease). Lease acquisition opens
        # a child span via :func:`child_span`; events posted under a
        # held lease inherit the lease's span. PR 4 ships the root +
        # the helper; lease-scoped span tracking joins in PR 7's lease
        # unification (which is where ``Lease`` gains a ``trace_span_id``
        # field).
        self._trace_root: TraceContext = new_trace()

        if policy_error_mode not in ("close_turn", "default_responder", "raise"):
            raise ValueError(f"unknown policy_error_mode: {policy_error_mode!r}")
        self.policy_error_mode: PolicyErrorMode = policy_error_mode

        # Optional reference to the room's :class:`ConversationPolicy`,
        # used by background paths (dead-letter routing on participant
        # removal, watchdog hooks) that fire outside the
        # ``post_user_event_and_open_turn`` call site where the policy
        # would otherwise arrive via ``classify_fn``. When ``None`` the
        # coordinator uses kernel-default behavior (mirrors v0.1.2).
        self._policy: Optional["ConversationPolicy"] = policy

        self._leases: dict[int, TurnLease] = {}
        # v0.3 PR 7 / doctrine §3: typed-lease registry. USER_TURN
        # leases continue to live in ``_leases`` (so v0.2 call sites
        # stay byte-identical); CONTROL_ACTION / REACTIVE / future
        # tool / workflow leases live here. PR 8 collapses the two
        # maps into one under the unified ``Lease`` type.
        self._typed_leases: dict[int, Lease] = {}
        # v0.3 PR 12 / closes audit D2: streaming-stall watchdog
        # state. ``_last_chunk_at[lease_id]`` holds the monotonic
        # timestamp of the most recent stream delta for that lease;
        # ``check_streaming_stall`` reaps leases whose latest chunk
        # exceeds ``RoomConfig.stream_stall_threshold_s``.
        self._last_chunk_at: dict[int, float] = {}
        # ``_CapabilityCheck`` (PR 7) reads this slot to identify the
        # action being proposed. Set by ``acquire_typed_lease`` before
        # the check chain runs; cleared after.
        self._pending_lease_context: Optional[LeaseContext] = None
        self._next_lease_id = 0

        self._user_turn: Optional[UserTurn] = None
        self._next_user_turn_id = 0
        self._next_obligation_id = 1
        self._last_user_post_ts: Optional[float] = None

        self._loop_guard = LoopGuardConfig()
        self._throttle = ThrottleConfig()
        self._budget = BudgetConfig()

        self._compaction_in_flight = False

        # v0.2: dedicated watchdog thread state. ``start_watchdog`` and
        # ``stop_watchdog`` are idempotent; the runtime layer wires
        # them into its own start/stop. Until started, idle timeouts
        # rely on whoever calls ``check_idle_timeout`` directly (e.g.
        # tests using ``actor.step()``).
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop_event = threading.Event()

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

    @property
    def trace_root(self) -> TraceContext:
        """v0.3 PR 4 / doctrine P12 — room-session root :class:`TraceContext`.

        Allocated once at coordinator construction; outlives every
        lease. PR 7 (lease unification) will store a child span on the
        :class:`Lease` dataclass; v0.3 PRs after that stamp the
        lease's span on every event posted while the lease is held.
        """
        return self._trace_root

    def new_child_span(self) -> TraceContext:
        """Return a fresh :class:`TraceContext` under :attr:`trace_root`.

        Used by lease acquisition (PR 7) and any v0.4+ scope-opening
        event that wants its own span. Safe to call without the lock;
        :func:`child_span` is pure (no shared state mutation).
        """
        return child_span(self._trace_root)

    @property
    def kernel_state(self) -> KernelState:
        """v0.3 P5 / §1 transactional root.

        Exposed read-only at this layer — mutation paths bump
        ``KernelState.version`` via :meth:`_bump_state_version` under
        the coordinator lock. Tests + library code that need the
        canonical v0.3 root use this property; legacy ``self.state``
        continues to alias ``kernel_state.room`` for back-compat.
        """
        return self._kernel

    def _bump_state_version(self) -> int:
        """Increment ``KernelState.version`` under lock and return it.

        Caller must hold ``self._lock``. PR 3's effect-registry dispatch
        will be the dominant call site (every applied effect bumps);
        PR 1 wires the helper into the existing in-place mutation
        methods (membership / slots / control state) so the version
        counter is meaningful immediately rather than at the v0.3
        release-cut.
        """
        return self._kernel.bump_version()

    def _apply_effect(self, effect: ControlEffect) -> ControlEffect:
        """v0.3 PR 3 / doctrine P6, P7, §5 — apply a typed semantic effect.

        Caller MUST hold ``self._lock``. Looks up the registered
        reducer for ``(effect.effect_type, effect.schema_version)``,
        runs it in place against :attr:`KernelState`, bumps the
        :attr:`KernelState.version` counter, and returns the effect
        for caller bookkeeping. The reducer is responsible for the
        state delta only — bus emission, lease invalidation, and
        watchdog wiring continue to live at the calling method so
        legacy semantics are byte-identical post-refactor.

        Raises :class:`loom.kernel.effects.UnknownEffect` if no
        reducer is registered — a programmer error in v0.3 (every
        coordinator-emitted effect must have its reducer registered at
        room-construction time).
        """
        self._effect_registry.apply(self._kernel, effect)
        self._bump_state_version()
        return effect

    def propose_control_action(
        self,
        proposer_id: str,
        action_name: str,
        params: Optional[dict] = None,
    ) -> ControlActionResult:
        """v0.3 PR 9 / doctrine §7 — full control-action lifecycle.

        Steps:

        1. Emit ``control_action_proposed`` (P2 control plane).
        2. Resolve ``action_name`` via the registry; emit
           ``control_action_denied(UNKNOWN_ACTION)`` if missing.
        3. ``validate_params``; emit ``control_action_denied(INVALID_PARAMS)``
           on failure.
        4. Acquire ``LeaseKind.CONTROL_ACTION`` lease (PR 7) — the
           ``_CapabilityCheck`` (PR 7) enforces P10 capability gating.
           Denial emits ``control_action_denied(INSUFFICIENT_CAPABILITY)``.
        5. On grant, run ``action.propose_effect`` against the frozen
           ``KernelStateView``; apply each effect via
           :meth:`_apply_effect`; release the lease; emit
           ``control_action_applied`` with an effect summary.
        6. Return a :class:`ControlActionResult` for the caller.

        v0.3 PR 14 (custom actions return built-in effects only): the
        registered reducer set on ``self._effect_registry`` rejects
        unknown effect types — a custom action that returns a
        non-built-in :class:`ControlEffect` triggers ``UnknownEffect``
        from the registry, surfaced as ``CHECK_RAISED``.
        """
        params = dict(params or {})
        # Step 1 — emit proposed event.
        self.bus.post_internal(
            ev.control_action_proposed(
                action_name=action_name,
                proposer_id=proposer_id,
                params=params,
            ),
            auth=_KERNEL_AUTH,
        )

        action = self._action_registry.get(action_name)
        if action is None:
            return self._deny_control_action(
                action_name, proposer_id,
                DenialReason.UNKNOWN_ACTION,
                f"unknown action: {action_name!r}",
            )

        ok, why = action.validate_params(params)
        if not ok:
            return self._deny_control_action(
                action_name, proposer_id,
                DenialReason.INVALID_PARAMS,
                why or "invalid params",
            )

        # Step 4 — lease + capability check.
        ctx = ControlActionContext(
            action_name=action_name,
            params=(),
            required_capability=action.required_capability.value,
        )
        lease = self.acquire_typed_lease(
            LeaseKind.CONTROL_ACTION, proposer_id, ctx
        )
        if lease is None:
            return self._deny_control_action(
                action_name, proposer_id,
                DenialReason.INSUFFICIENT_CAPABILITY,
                "lease denied",
                check_name="capability",
            )

        # Step 5 — apply effects under lock.
        try:
            view = self._kernel.view()
            effects = action.propose_effect(params, view)
        except Exception as exc:
            self._release_typed_lease(lease.id)
            return self._deny_control_action(
                action_name, proposer_id,
                DenialReason.CHECK_RAISED,
                f"{type(exc).__name__}: {exc}",
            )

        applied: list[ControlEffect] = []
        try:
            with self._lock:
                for eff in effects:
                    self._apply_effect(eff)
                    applied.append(eff)
        except Exception as exc:
            self._release_typed_lease(lease.id)
            return self._deny_control_action(
                action_name, proposer_id,
                DenialReason.CHECK_RAISED,
                f"{type(exc).__name__}: {exc}",
            )

        self._release_typed_lease(lease.id)

        summary = [
            {"effect_type": e.effect_type, "schema_version": e.schema_version}
            for e in applied
        ]
        self.bus.post_internal(
            ev.control_action_applied(
                action_name=action_name,
                applier_id=proposer_id,
                effects=summary,
            ),
            auth=_KERNEL_AUTH,
        )
        return ControlActionResult(granted=True, effects=tuple(applied))

    def _deny_control_action(
        self,
        action_name: str,
        proposer_id: str,
        reason: DenialReason,
        message: str,
        check_name: Optional[str] = None,
    ) -> ControlActionResult:
        self.bus.post_internal(
            ev.control_action_denied(
                action_name=action_name,
                proposer_id=proposer_id,
                reason=reason.value,
                check_name=check_name,
            ),
            auth=_KERNEL_AUTH,
        )
        return ControlActionResult(
            granted=False, reason=reason, message=message
        )

    def _release_typed_lease(self, lease_id: int) -> None:
        """v0.3 PR 7+8 — release a typed lease and emit `lease_closed`."""
        with self._lock:
            lease = self._typed_leases.pop(lease_id, None)
            if lease is None:
                return
            lease.valid = False
        self.bus.post_internal(
            ev.lease_closed(
                lease_id=lease.id,
                holder=lease.holder,
                kind=lease.kind.value,
                reason="released",
                span_id=lease.trace_span_id,
            ),
            auth=_KERNEL_AUTH,
        )

    def _assert_not_holding_lock(self, where: str) -> None:
        """v0.3 PR 2 / doctrine P4: refuse to enter an I/O path under lock.

        Raises :class:`RuntimeError` if the current thread holds
        ``self._lock``. Call from every entry point that may perform a
        long-running operation: LLM invocations, tool calls, file I/O,
        snapshot writes, sleeps. The doctrine prohibits these under the
        coordinator lock because they would serialize the whole room on
        a single external dependency.

        ``where`` is a short symbolic identifier (e.g.
        ``"streaming.run_streaming_call"``) included in the error so
        the offending call site is grep-able in test failures.

        Cost is one attribute compare; safe to call on hot paths.
        """
        if self._lock.is_held_by_current_thread():
            raise RuntimeError(
                "loom.kernel: lock-discipline violation — "
                f"{where} called while holding the coordinator lock. "
                "See docs/lock-discipline.md (doctrine P4)."
            )

    # ------------------------------------------------------------------
    # v0.3.x PR 1 — thread_id emit helpers (doctrine P21)
    # ------------------------------------------------------------------

    def _emit_under_lease(self, lease: Lease, event: Event) -> int:
        """Post ``event`` to the bus, inheriting ``thread_id`` from ``lease``.

        Doctrine P21 — every event posted while a lease is held belongs
        to the lease's thread. The lease's :class:`LeaseContext`
        (each of the five v0.3 subclasses) carries a ``thread_id``
        field; this helper copies it onto the event if the caller
        hasn't already populated a non-default value.

        Existing v0.3 emit sites that don't yet route through this
        helper continue to work because ``Event.thread_id`` defaults
        to ``"main"`` — which is the correct value for room-wide
        events. Future compaction emitters (PR 3, PR 5) call this
        helper so per-thread summaries inherit the lease's scope.
        """
        ctx_tid = getattr(lease.context, "thread_id", None)
        if isinstance(ctx_tid, str) and ctx_tid:
            if event.thread_id == "main":
                event.thread_id = ctx_tid
        return self.bus.post_internal(event, auth=_KERNEL_AUTH)

    def _emit_system(self, event: Event, thread_id: str = "main") -> int:
        """Post a kernel-originated ``event`` not bound to any lease.

        Stamps ``thread_id`` (default ``"main"``) onto the event
        before bus emission. Use for coordinator-internal control
        events (lifecycle / dead-letter / membership) that aren't
        scoped to a lease's thread. Most v0.3 internal emits are
        room-wide and need no override; pass an explicit
        ``thread_id`` only when posting into a non-main thread.
        """
        if event.thread_id == "main" and thread_id != "main":
            event.thread_id = thread_id
        return self.bus.post_internal(event, auth=_KERNEL_AUTH)

    # ------------------------------------------------------------------
    # v0.3.x PR 3 — view-layer compaction commit lifecycle
    # (doctrine P18 / P19 / §6 / study/14)
    # ------------------------------------------------------------------

    def submit_summary_proposed(self, record: SummaryRecord) -> "SummaryCommitResult":
        """Off-lock pre-validate, then under-lock commit a summary record.

        Doctrine P19: structural validation runs *off-lock* (so the
        coordinator lock isn't held across the validator), then the
        commit step re-acquires the lock for an anchor-conflict check
        and the registered ``summary_committed`` reducer.

        Three terminal outcomes:

        - **Pre-validator rejects** → emit ``summary_failed`` (reason =
          structural class), apply :class:`SummaryFailedEffect`, return
          ``SummaryCommitResult(committed=False, ...)``.
        - **Under-lock anchor check rejects** (another summariser
          advanced ``active_summary_by_scope`` for the same scope while
          this proposal was being validated) → emit ``summary_proposed``
          (so the journal shows the attempt), then
          ``summary_failed(reason=ANCHOR_CONFLICT)``. ANCHOR_CONFLICT
          does NOT increment ``failure_count`` (doctrine §7 — anchor
          races are benign and don't count toward backoff).
        - **Commit succeeds** → emit ``summary_proposed`` then
          ``summary_committed``; apply the committed effect under-lock.

        Callers (PR 5 will be both Path A and Path B) drive this from
        an off-lock context; calling under-lock raises
        :class:`RuntimeError` via :meth:`_assert_not_holding_lock`.
        """
        self._assert_not_holding_lock("coordinator.submit_summary_proposed")

        # --- Off-lock pre-validation -------------------------------
        # Snapshot bus length and current summaries for the validator
        # input. The snapshot is consistent for the purpose of the
        # structural check — any concurrent appends only ADD to bus
        # length, which can never *narrow* a previously valid range.
        bus_length = len(self.bus.snapshot())
        # ContextState.summaries is mutated only under the coordinator
        # lock by reducers; reading it off-lock is safe for the lookup
        # because validate_lineage tolerates absent ids (the under-lock
        # commit re-checks anchor state authoritatively).
        summaries_view = dict(self.kernel_state.context.summaries)
        ok, reason, detail = validate_summary_record(
            record,
            bus_length=bus_length,
            input_summary_lookup=summaries_view,
        )
        if not ok:
            assert reason is not None
            return self._reject_summary(
                record,
                reason=reason,
                detail=detail or "",
                failed_validator="structural",
                emit_proposed_first=False,
            )

        # --- Under-lock commit -------------------------------------
        with self._lock:
            # Anchor check: the record's input_summary_ids must include
            # whatever is currently in active_summary_by_scope[scope]
            # (or the slot must be empty if the record has no inputs).
            active = self.kernel_state.context.active_summary_by_scope.get(
                record.scope
            )
            anchor_ok: bool
            if active is None:
                # First summary for this scope — only OK if the record
                # has no input summaries (otherwise it claims to extend
                # a non-existent prior).
                anchor_ok = len(record.input_summary_ids) == 0
                anchor_detail = (
                    "active_summary_by_scope empty but record has input_summary_ids"
                )
            else:
                anchor_ok = active in record.input_summary_ids
                anchor_detail = (
                    f"active summary {active!r} not in record.input_summary_ids "
                    f"{record.input_summary_ids!r}"
                )

            if not anchor_ok:
                # Emit the proposed event so the journal shows the
                # attempt, then fail. Both events go through
                # _emit_system (no lease — Path A is policy-triggered
                # in PR 5; Path B uses a lease but this PR doesn't
                # introduce it yet).
                self._emit_summary_proposed(record)
                return self._reject_summary(
                    record,
                    reason=SummaryFailureReason.ANCHOR_CONFLICT,
                    detail=anchor_detail,
                    failed_validator="anchor",
                    emit_proposed_first=False,  # already emitted
                )

            # Commit path: emit proposed → apply proposed effect →
            # emit committed → apply committed effect.
            self._emit_summary_proposed(record)
            self._apply_effect(SummaryProposedEffect(record=record))

            committed_at_event_id = self._emit_summary_committed(
                record, supersedes_summary_ids=tuple(record.input_summary_ids)
            )
            self._apply_effect(
                SummaryCommittedEffect(
                    record=record,
                    supersedes_summary_ids=tuple(record.input_summary_ids),
                )
            )
            # Doctrine §7: a successful commit clears the backoff
            # counter for this (summarizer_id, scope) pair so the next
            # rolling cycle starts with a clean slate.
            key = (record.summarizer_id, record.scope.as_tuple())
            self.kernel_state.context.failure_count.pop(key, None)

            return SummaryCommitResult(
                committed=True,
                summary_id=record.summary_id,
                reason=None,
                details=None,
                failed_validator=None,
                committed_at_event_id=committed_at_event_id,
            )

    def schedule_summarization(
        self,
        scope: ContextScope,
        *,
        covers_event_range: Optional[tuple[int, int]] = None,
        trigger_pressure_ratio: float = 0.0,
        triggering_event_id: int = -1,
        ttl_s: Optional[float] = None,
    ) -> "SchedulingResult":
        """Path A entry — policy-triggered summarisation (doctrine P22 / §7).

        Pre-conditions:

        - ``RoomState.default_summarizer_id`` must be set.
        - ``(default_summarizer_id, scope)`` must not be in
          :attr:`ContextState.disabled_scopes`.

        On success:

        1. Acquire a SUMMARIZATION lease for the slot occupant with
           ``triggered_by="policy"`` — gates on
           :class:`_SummarizerSlotCheck` + the SUMMARIZE capability
           check.
        2. Emit ``summarization_scheduled`` audit event.
        3. Return :class:`SchedulingResult` with the lease id.

        The caller (typically a policy pre-turn hook in v0.4+) then
        drives the actual summarisation off-lock and calls
        :meth:`submit_summary_proposed` with the resulting record.

        Path B (``SummarizeControlAction``) bypasses this method and
        goes through the v0.3 PR 9 control-action lifecycle, then
        acquires the SUMMARIZATION lease internally with
        ``triggered_by="control_action"``.
        """
        self._assert_not_holding_lock("coordinator.schedule_summarization")

        slot = self.state.default_summarizer_id
        if slot is None:
            return SchedulingResult(
                scheduled=False,
                lease_id=None,
                summarizer_id=None,
                scope=scope,
                denial_reason="no_default_summarizer",
            )

        ctx_state = self.kernel_state.context
        if (slot, scope.as_tuple()) in ctx_state.disabled_scopes:
            return SchedulingResult(
                scheduled=False,
                lease_id=None,
                summarizer_id=slot,
                scope=scope,
                denial_reason="scope_disabled",
            )

        # Default the covers range to the rolling tail.
        if covers_event_range is None:
            from loom.kernel.context import select_compaction_range
            bus_length = len(self.bus.snapshot())
            covers_event_range = select_compaction_range(
                ctx_state, scope, bus_length=bus_length
            )

        context = SummarizationContext(
            scope=scope,
            covers_event_range=tuple(covers_event_range),
            triggered_by="policy",
            triggering_event_id=triggering_event_id,
            required_capability=CapabilityName.EMIT_SUMMARY.value,
            thread_id=scope.thread_id,
        )
        lease = self.acquire_typed_lease(
            LeaseKind.SUMMARIZATION,
            holder=slot,
            context=context,
            ttl_s=ttl_s,
        )
        if lease is None:
            return SchedulingResult(
                scheduled=False,
                lease_id=None,
                summarizer_id=slot,
                scope=scope,
                denial_reason="lease_denied",
            )

        # Audit event.
        self._emit_system(
            ev.summarization_scheduled(
                scope=scope,
                lease_id=lease.id,
                summarizer_id=slot,
                trigger_pressure_ratio=trigger_pressure_ratio,
                triggered_by="policy",
                thread_id=scope.thread_id,
            ),
            thread_id=scope.thread_id,
        )
        return SchedulingResult(
            scheduled=True,
            lease_id=lease.id,
            summarizer_id=slot,
            scope=scope,
        )

    def request_summarization(
        self,
        requester: str,
        scope: ContextScope,
        *,
        covers_event_range: Optional[tuple[int, int]] = None,
        triggering_event_id: int = -1,
        ttl_s: Optional[float] = None,
    ) -> "SchedulingResult":
        """Path B entry — user/agent-triggered summarisation (doctrine P22).

        Symmetric to :meth:`schedule_summarization` (Path A) but the
        ``SummarizationContext.triggered_by`` is ``"control_action"``,
        which makes :class:`_SummarizerSlotCheck` skip the slot check
        (so a holder other than the default summariser can be granted
        the lease as long as it has the SUMMARIZE capability).

        Used by:

        - The ``/summarize`` slash command (proposer = ``"user"``;
          user holders bypass the capability gate per v0.3 P15).
        - PR 9-style ``SummarizeControlAction`` (proposer = agent
          holding ``CapabilityName.SUMMARIZE``).

        Always converges with Path A at the SUMMARIZATION lease, so
        the downstream commit pipeline (off-lock pre-validation +
        under-lock anchor check) is byte-identical between paths.
        """
        self._assert_not_holding_lock("coordinator.request_summarization")

        if covers_event_range is None:
            from loom.kernel.context import select_compaction_range
            bus_length = len(self.bus.snapshot())
            covers_event_range = select_compaction_range(
                self.kernel_state.context, scope, bus_length=bus_length
            )

        context = SummarizationContext(
            scope=scope,
            covers_event_range=tuple(covers_event_range),
            triggered_by="control_action",
            triggering_event_id=triggering_event_id,
            required_capability=CapabilityName.SUMMARIZE.value,
            thread_id=scope.thread_id,
        )
        lease = self.acquire_typed_lease(
            LeaseKind.SUMMARIZATION,
            holder=requester,
            context=context,
            ttl_s=ttl_s,
        )
        if lease is None:
            return SchedulingResult(
                scheduled=False,
                lease_id=None,
                summarizer_id=requester,
                scope=scope,
                denial_reason="lease_denied",
            )

        self._emit_system(
            ev.summarization_scheduled(
                scope=scope,
                lease_id=lease.id,
                summarizer_id=requester,
                trigger_pressure_ratio=0.0,
                triggered_by="control_action",
                thread_id=scope.thread_id,
            ),
            thread_id=scope.thread_id,
        )
        return SchedulingResult(
            scheduled=True,
            lease_id=lease.id,
            summarizer_id=requester,
            scope=scope,
        )

    def _maybe_disable_scope_after_failure(
        self,
        *,
        summarizer_id: str,
        scope: ContextScope,
        last_failed_summary_id: str,
    ) -> None:
        """If failure_count for ``(summarizer_id, scope)`` reached the
        configured threshold and the scope isn't already disabled,
        emit ``compaction_disabled`` and apply
        :class:`CompactionDisabledEffect`. Caller MUST hold the lock.
        """
        key = (summarizer_id, scope.as_tuple())
        if key in self.kernel_state.context.disabled_scopes:
            return
        count = self.kernel_state.context.failure_count.get(key, 0)
        threshold = getattr(
            self.config, "summarizer_max_consecutive_failures", 3
        )
        if count < threshold:
            return
        self._emit_system(
            ev.compaction_disabled(
                scope=scope,
                summarizer_id=summarizer_id,
                failure_count=count,
                reason="consecutive_failures",
                last_failed_summary_id=last_failed_summary_id,
                thread_id=scope.thread_id,
            ),
            thread_id=scope.thread_id,
        )
        self._apply_effect(
            CompactionDisabledEffect(
                summarizer_id=summarizer_id,
                scope=scope,
                failure_count_at_disable=count,
                reason="consecutive_failures",
            )
        )

    def _emit_summary_proposed(self, record: SummaryRecord) -> int:
        return self._emit_system(
            ev.summary_proposed(
                summary_id=record.summary_id,
                scope=record.scope,
                covers_event_range=record.covers_event_range,
                proposed_text=record.text,
                retained_event_ids=tuple(record.retained_event_ids),
                input_summary_ids=tuple(record.input_summary_ids),
                input_event_ranges=tuple(record.input_event_ranges),
                model_id=record.model_id,
                prompt_hash=record.prompt_hash,
                summarizer_id=record.summarizer_id,
                proposed_at_event_id=record.proposed_at_event_id,
                thread_id=record.scope.thread_id,
            ),
            thread_id=record.scope.thread_id,
        )

    def _emit_summary_committed(
        self,
        record: SummaryRecord,
        *,
        supersedes_summary_ids: tuple[str, ...],
    ) -> int:
        return self._emit_system(
            ev.summary_committed(
                summary_id=record.summary_id,
                scope=record.scope,
                covers_event_range=record.covers_event_range,
                proposed_text=record.text,
                retained_event_ids=tuple(record.retained_event_ids),
                input_summary_ids=tuple(record.input_summary_ids),
                input_event_ranges=tuple(record.input_event_ranges),
                model_id=record.model_id,
                prompt_hash=record.prompt_hash,
                summarizer_id=record.summarizer_id,
                proposed_at_event_id=record.proposed_at_event_id,
                supersedes_summary_ids=supersedes_summary_ids,
                committed_at_event_id=-1,
                thread_id=record.scope.thread_id,
            ),
            thread_id=record.scope.thread_id,
        )

    def _reject_summary(
        self,
        record: SummaryRecord,
        *,
        reason: SummaryFailureReason,
        detail: str,
        failed_validator: str,
        emit_proposed_first: bool,
    ) -> "SummaryCommitResult":
        """Shared post-failure emit + apply path.

        Apply runs under the coordinator lock; if we're already under
        the lock (anchor-conflict branch), the caller passes
        ``emit_proposed_first=False`` and ``_apply_effect`` runs
        re-entrantly. Otherwise we acquire the lock here.
        """
        if emit_proposed_first:
            self._emit_summary_proposed(record)

        self._emit_system(
            ev.summary_failed(
                proposed_summary_id=record.summary_id,
                scope=record.scope,
                reason=reason.value,
                details=detail,
                failed_validator=failed_validator,
                summarizer_id=record.summarizer_id,
                thread_id=record.scope.thread_id,
            ),
            thread_id=record.scope.thread_id,
        )
        failed_effect = SummaryFailedEffect(
            summarizer_id=record.summarizer_id,
            scope=record.scope,
            reason=reason,
        )
        if self._lock.is_held_by_current_thread():
            self._apply_effect(failed_effect)
            self._maybe_disable_scope_after_failure(
                summarizer_id=record.summarizer_id,
                scope=record.scope,
                last_failed_summary_id=record.summary_id,
            )
        else:
            with self._lock:
                self._apply_effect(failed_effect)
                self._maybe_disable_scope_after_failure(
                    summarizer_id=record.summarizer_id,
                    scope=record.scope,
                    last_failed_summary_id=record.summary_id,
                )

        return SummaryCommitResult(
            committed=False,
            summary_id=record.summary_id,
            reason=reason,
            details=detail,
            failed_validator=failed_validator,
            committed_at_event_id=None,
        )

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def register_participant(self, info: ParticipantInfo) -> None:
        with self._lock:
            self.state.add_participant(info)
            self._bump_state_version()
            self.bus.post_internal(
                ev.participant_added(info.id, info.role_hints), auth=_KERNEL_AUTH
            )

    def unregister_participant(self, pid: str) -> None:
        """Remove participant, re-resolve slots, dead-letter pending mentions.

        Also marks any open obligation held by ``pid`` as resolved-by-
        removal so the UserTurn can still close cleanly.

        The caller (slash-command handler) is responsible for stopping
        the participant's actor thread separately.
        """
        with self._lock:
            slot_changes = self.state.remove_participant(pid)
            self._bump_state_version()
            self.bus.post_internal(ev.participant_removed(pid), auth=_KERNEL_AUTH)

            # Slot-change events. Only default_responder_changed is
            # required v0; anchor/chair/summarizer get analogues if any
            # were defined for the room.
            if "default_responder_id" in slot_changes:
                self.bus.post_internal(
                    ev.default_responder_changed(pid, slot_changes["default_responder_id"]),
                    auth=_KERNEL_AUTH,
                )
            for slot in ("anchor_id", "chair_id", "default_summarizer_id"):
                if slot in slot_changes:
                    factory = {
                        "anchor_id": "anchor_changed",
                        "chair_id": "chair_changed",
                        "default_summarizer_id": "default_summarizer_changed",
                    }[slot]
                    self.bus.post_internal(
                        ev._control(factory, old_id=pid, new_id=slot_changes[slot]),
                        auth=_KERNEL_AUTH,
                    )

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
                        self._resolve_obligation_locked(ob.id, by_event_id=None)

            # Dead-letter pending direct mentions to pid.
            self._dead_letter_pending_mentions(pid, slot_changes)

            # The removal may have closed out the last unresolved required
            # obligation; re-check for completion.
            self._maybe_close_user_turn_locked()

    def _transfer_required_obligations_locked(
        self,
        removed_pid: str,
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
            self.bus.post_internal(
                ev.obligation_recorded(
                    obligation_id=new_ob.id,
                    participant_id=new_ob.participant_id,
                    level=new_ob.level,
                    target_event_ids=list(new_ob.target_event_ids),
                    reason=new_ob.reason,
                ),
                auth=_KERNEL_AUTH,
            )
            # Only transfer once — additional must/should obligations
            # from the removed participant collapse onto the same
            # fallback rather than producing N duplicate obligations.
            return

    def _dead_letter_pending_mentions(
        self,
        removed_pid: str,
        slot_changes: dict[str, Optional[str]],
    ) -> None:
        """Re-route or dead-letter outstanding @mentions to ``removed_pid``.

        Routing choice is delegated to
        :meth:`ConversationPolicy.dead_letter_target` when a policy is
        wired onto the coordinator; otherwise we fall back to the
        kernel default chain (configured default-responder slot, then
        cheapest active capable). The policy hook receives a fresh
        view (``removed_pid`` has already been pulled from the
        participant map) plus the removed pid for context.

        ``slot_changes`` is unused now that routing is delegated — it
        was the legacy in-flight transfer of the new
        ``default_responder_id`` produced by slot re-resolution; the
        same value is already on ``state.default_responder_id`` by the
        time we reach this method, so the policy hook reads it from
        the state view directly.
        """
        del slot_changes
        if not self._user_turn:
            return
        ut = self._user_turn
        snap = self.bus.snapshot(channel="main", kinds=["chat"], since=ut.user_event_id - 1)
        last_response_id = -1
        for e in snap:
            if e.sender == removed_pid:
                last_response_id = e.id
        for e in snap:
            if removed_pid in e.addressees and e.id > last_response_id:
                reroute_to = self._resolve_dead_letter_target(removed_pid)
                self.bus.post_internal(
                    ev.dead_letter(
                        original_mention_event_id=e.id,
                        reroute_to=reroute_to,
                        reason="participant_removed",
                    ),
                    auth=_KERNEL_AUTH,
                )

    def _resolve_dead_letter_target(self, removed_pid: str) -> Optional[str]:
        """Pick a dead-letter reroute target via the policy hook.

        Falls back to the kernel default (default-responder slot, then
        cheapest active capable) when no policy is registered on this
        coordinator. The fallback mirrors v0.1.2 behavior exactly.
        """
        if self._policy is not None:
            try:
                return self._policy.dead_letter_target(
                    state=self.state.view(), removed_participant=removed_pid
                )
            except Exception:
                # A buggy hook must not block dead-letter emission —
                # silently fall through to the kernel default.
                pass
        reroute_to = self.state.default_responder_id
        if reroute_to is None:
            reroute_to = self.state.cheapest_active_capable()
        return reroute_to

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
            self._apply_effect(TopicChangedEffect(topic=new_topic))
            self.bus.post_internal(ev.topic_changed(old, new_topic or ""), auth=_KERNEL_AUTH)

    def set_default_responder(self, pid: Optional[str]) -> None:
        with self._lock:
            old = self.state.default_responder_id
            if old == pid:
                return
            self._apply_effect(DefaultResponderSetEffect(participant_id=pid))
            for lease in self._leases.values():
                lease.valid = False
            self.bus.post_internal(ev.default_responder_changed(old, pid), auth=_KERNEL_AUTH)

    def set_anchor(self, pid: Optional[str]) -> None:
        with self._lock:
            old = self.state.anchor_id
            if old == pid:
                return
            self._apply_effect(AnchorAssignedEffect(anchor_id=pid))
            self.bus.post_internal(
                ev._control("anchor_changed", old_id=old, new_id=pid), auth=_KERNEL_AUTH
            )

    def set_chair(self, pid: Optional[str]) -> None:
        with self._lock:
            old = self.state.chair_id
            if old == pid:
                return
            self._apply_effect(ChairAssignedEffect(chair_id=pid))
            self.bus.post_internal(
                ev._control("chair_changed", old_id=old, new_id=pid), auth=_KERNEL_AUTH
            )

    def set_default_summarizer(self, pid: Optional[str]) -> None:
        with self._lock:
            old = self.state.default_summarizer_id
            if old == pid:
                return
            self._apply_effect(DefaultSummarizerSetEffect(participant_id=pid))
            self.bus.post_internal(
                ev._control("default_summarizer_changed", old_id=old, new_id=pid), auth=_KERNEL_AUTH
            )

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
            old = dict(self.state.control.roles)
            # set_roles filters unknown participants; precompute the
            # filtered mapping so the no-op short-circuit can run
            # against the canonical "what set_roles would write" value
            # without applying the effect twice.
            filtered = {
                pid: role for pid, role in roles.items() if pid in self.state.participants
            }
            if filtered == old:
                return
            self._apply_effect(RolesAssignedEffect(roles=filtered))
            new = dict(self.state.control.roles)
            self.bus.post_internal(ev.roles_assigned(new), auth=_KERNEL_AUTH)

    def set_wait_for_user_flag(self, wait_for_user: bool) -> None:
        """Set or clear the cross-turn ``wait_for_user`` flag.

        Emits a ``floor_updated`` control event (kept under the legacy
        event name for journal back-compat) when the flag value
        changes. Used by lifecycle paths that need to gate agent
        wakeups on a user post without opening a UserTurn.
        """
        with self._lock:
            old = self.state.set_wait_for_user(wait_for_user)
            new = self.state.control.wait_for_user
            if old == new:
                return
            self._bump_state_version()
            self.bus.post_internal(ev.floor_updated(wait_for_user=new), auth=_KERNEL_AUTH)

    def set_style(self, style: StyleLevel) -> None:
        """Update the brevity preference."""
        with self._lock:
            old = self.state.control.style
            if old == style:
                return
            # The reducer calls ``state.room.set_style`` which raises
            # ``ValueError`` on an unknown style level — preserved
            # post-PR 3 by letting the exception propagate.
            self._apply_effect(StyleChangedEffect(style=style))
            new = self.state.control.style
            self.bus.post_internal(ev.style_changed(old, new), auth=_KERNEL_AUTH)

    # ------------------------------------------------------------------
    # UserTurn lifecycle
    # ------------------------------------------------------------------

    def post_user_event_and_open_turn(
        self,
        user_event: Event,
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
            self.bus.post_internal(user_event, auth=_KERNEL_AUTH)
            plan = self._run_policy_under_lock(classify_fn, user_event)
            # Apply plan-driven state changes (turn_order) BEFORE the
            # open check so acknowledgement plans carrying
            # ``set_turn_order=[]`` (game-end phrase exit) still clear
            # the rotation even though no turn opens.
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
            self.bus.post_internal(
                ev.policy_error(
                    exception_class=type(exc).__name__,
                    message=str(exc)[:500],
                    elapsed_ms=elapsed_ms,
                    user_event_id=user_event.id,
                ),
                auth=_KERNEL_AUTH,
            )
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
        # v0.3 PR 13 (closes audit D3): per-policy slow threshold via
        # RoomConfig. v0.2.1 used the module-level constant
        # ``_POLICY_SLOW_THRESHOLD_MS``; v0.3 reads from config so
        # different policies can tune the observability noise floor.
        threshold = float(
            getattr(self.config, "policy_slow_threshold_ms",
                    _POLICY_SLOW_THRESHOLD_MS)
        )
        if elapsed_ms > threshold:
            self.bus.post_internal(
                ev.policy_slow(
                    elapsed_ms=elapsed_ms,
                    threshold_ms=threshold,
                    user_event_id=user_event.id,
                ),
                auth=_KERNEL_AUTH,
            )
        return plan

    def _apply_plan_state_changes_locked(self, plan: UserTurnPlan) -> None:
        """Apply :class:`UserTurnPlan` state-mutation directives to ``state.control``.

        The interpreter is pure; it returns a plan with ``set_turn_order``
        set when an external state change is warranted (e.g. auto-arming
        round-robin on a game-start phrase, or clearing it to exit
        round-robin). A non-empty ``turn_order`` is itself the
        round-robin mode signal; clearing it (``[]``) leaves broadcast
        mode. The coordinator is the only mutator, so we apply them
        here under the lock.

        ``advance_turn_pointer`` is read at turn-close time (see
        :meth:`_close_user_turn_locked`), not here.
        """
        if plan.set_turn_order is not None:
            self.state.set_turn_order(plan.set_turn_order)

    def open_user_turn(self, user_event: Event, plan: UserTurnPlan) -> UserTurn:
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
            if (
                not should_open_new_user_turn(
                    self._last_user_post_ts,
                    now,
                    self.config.user_turn_debounce_ms,
                )
                and self._user_turn
                and self._user_turn.state == "open"
            ):
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
                self.bus.post_internal(ev.floor_updated(wait_for_user=False), auth=_KERNEL_AUTH)

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

            self.bus.post_internal(
                ev.user_turn_opened(
                    user_turn_id=turn.id,
                    routing_case=plan.routing_case,
                    required_participants=sorted(plan.required_participants),
                    optional_participants=sorted(plan.optional_participants),
                    rationale=plan.rationale,
                ),
                auth=_KERNEL_AUTH,
            )
            for ob in turn.obligations.values():
                self.bus.post_internal(
                    ev.obligation_recorded(
                        obligation_id=ob.id,
                        participant_id=ob.participant_id,
                        level=ob.level,
                        target_event_ids=list(ob.target_event_ids),
                        reason=ob.reason,
                    ),
                    auth=_KERNEL_AUTH,
                )

            # If the plan declared no required participants and no
            # optional participants, there's nothing to wait for —
            # close immediately as ``no_responder``.
            if not plan.required_participants and not plan.optional_participants:
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
                        self._resolve_obligation_locked(ob.id, by_event_id=None)
            self._close_user_turn_locked(reason)

    def _close_user_turn_locked(self, reason: ClosureReason) -> None:
        if not self._user_turn or self._user_turn.state == "closed":
            return
        plan = self._user_turn.frozen_plan
        self._user_turn.close(reason)
        self.state.current_user_turn_id = None
        self.bus.post_internal(
            ev.user_turn_closed(
                user_turn_id=self._user_turn.id,
                reason=reason,
            ),
            auth=_KERNEL_AUTH,
        )
        # Apply ``wait_for_user_after`` from the plan now that the turn
        # is over. The flag stays set until the next user post (which
        # clears it inside ``open_user_turn``). It also fires for
        # cancelled turns — the user explicitly stopped the floor.
        if plan.wait_for_user_after and not self.state.control.wait_for_user:
            self.state.set_wait_for_user(True)
            self.bus.post_internal(ev.floor_updated(wait_for_user=True), auth=_KERNEL_AUTH)
        # Advance the round-robin rotation pointer if the closed plan
        # came from the rotation (``advance_turn_pointer=True``) and the
        # rotation is still armed (``turn_order`` non-empty).
        # ``@-mention`` / vocative overrides leave this flag False so the
        # rotation slot is preserved.
        if plan.advance_turn_pointer and self.state.control.turn_order:
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

    def check_lease_ttl(self, *, now: Optional[float] = None) -> int:
        """Proactively expire and reap leases past their TTL (v0.2.1, audit D1).

        Without this sweep, a lease held while no stream is active stays
        nominally valid until something accesses it via
        :meth:`validate_lease` (which only fires from the stream path).
        The watchdog calls this on each tick so lease state stays
        authoritative — doctrine §control-plane.

        For each expired lease (``expires_at < now``): mark
        ``valid=False``, drop the lease from ``self._leases``, and
        emit a ``lease_expired`` control event under
        ``post_internal``. Returns the number of leases reaped.

        Holds the coordinator lock for the iteration; emission happens
        OUTSIDE the lock so a slow subscriber on the bus cannot stall
        actor threads.
        """
        # P3.3 / audit TIME1: lease TTL math uses time.monotonic.
        cutoff = now if now is not None else time.monotonic()
        expired: list[TurnLease] = []
        with self._lock:
            for lease_id, lease in list(self._leases.items()):
                if not lease.valid:
                    continue
                if lease.expires_at < cutoff:
                    lease.valid = False
                    self._leases.pop(lease_id, None)
                    expired.append(lease)
        # Emit OUTSIDE the lock — bus subscribers are user code.
        for lease in expired:
            self.bus.post_internal(
                ev.lease_expired(
                    holder=lease.holder,
                    lease_id=lease.id,
                    trigger_event_id=lease.trigger_event_id,
                ),
                auth=_KERNEL_AUTH,
            )
            # v0.3 PR 8: unified termination event alongside the
            # v0.2.1 ``lease_expired`` so v0.3 consumers can listen on
            # one stream. ``lease_expired`` stays until the v0.3.x
            # release-cut drops the legacy duplicate.
            self.bus.post_internal(
                ev.lease_closed(
                    lease_id=lease.id,
                    holder=lease.holder,
                    kind=LeaseKind.USER_TURN.value,
                    reason="expired",
                ),
                auth=_KERNEL_AUTH,
            )
        return len(expired)

    # ------------------------------------------------------------------
    # Watchdog thread (v0.2)
    # ------------------------------------------------------------------

    def start_watchdog(self) -> None:
        """Start the dedicated watchdog thread.

        Idempotent: re-calling is a no-op while the thread is alive.
        Runs :meth:`check_idle_timeout` every
        ``config.watchdog_interval_s`` seconds until
        :meth:`stop_watchdog` is called. Exceptions in the loop body
        are swallowed — the watchdog is best-effort and must not
        crash on a single bad tick.
        """
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop_event.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="loom-coord-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def stop_watchdog(self, *, timeout: float = 1.0) -> None:
        """Signal the watchdog thread to exit and wait for it.

        Idempotent: re-calling on a stopped watchdog is a no-op.
        """
        self._watchdog_stop_event.set()
        thread = self._watchdog_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._watchdog_thread = None

    def _watchdog_loop(self) -> None:
        interval = max(0.05, float(self.config.watchdog_interval_s))
        while not self._watchdog_stop_event.is_set():
            try:
                self.check_idle_timeout()
            except Exception:
                # Best-effort: never crash on a single bad tick.
                pass
            try:
                # v0.2.1 (PR 1, audit finding D1): proactively reap
                # leases past TTL so lease state stays authoritative.
                self.check_lease_ttl()
            except Exception:
                pass
            try:
                # v0.3 PR 12 (closes audit D2): catch stalled streams
                # whose lease is still nominally valid but whose
                # provider has gone silent.
                self.check_streaming_stall()
            except Exception:
                pass
            # ``Event.wait`` returns True if the event was set, False on
            # timeout — either way we re-enter the loop and the
            # condition above decides whether to exit.
            self._watchdog_stop_event.wait(timeout=interval)

    # ------------------------------------------------------------------
    # Obligation helpers
    # ------------------------------------------------------------------

    def obligation_for(
        self,
        holder: str,
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
        self,
        obligation_id: int,
        *,
        by_event_id: Optional[int],
        expected_holder: Optional[str] = None,
    ) -> None:
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
                    f"holder {expected_holder!r}"
                )
        if ut.mark_obligation_resolved(obligation_id, by_event_id=by_event_id):
            ob = ut.obligations.get(obligation_id)
            if ob is not None:
                self.bus.post_internal(
                    ev.obligation_resolved(
                        obligation_id=obligation_id,
                        participant_id=ob.participant_id,
                        resolved_by_event_id=by_event_id,
                    ),
                    auth=_KERNEL_AUTH,
                )

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
            checks = self.config.lease_checks or DEFAULT_LEASE_CHECKS
            for chk in checks:
                try:
                    result = chk.check(
                        holder=holder,
                        trigger_event_id=trigger_event_id,
                        is_direct_mention=is_direct_mention,
                        coordinator=self,
                    )
                except Exception as exc:
                    result = LeaseCheckResult(False, f"check_raised:{type(exc).__name__}")
                if not result.passed:
                    self.bus.post_internal(
                        ev.lease_denied(
                            holder=holder,
                            check_name=chk.name,
                            deny_reason=result.deny_reason or "denied",
                            trigger_event_id=trigger_event_id,
                        ),
                        auth=_KERNEL_AUTH,
                    )
                    return None

            ut = self._user_turn
            # The OpenTurnCheck above guarantees ``ut`` is non-None and
            # has ``state == "open"`` — assert it for the type checker.
            assert ut is not None
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

    def acquire_typed_lease(
        self,
        kind: LeaseKind,
        holder: str,
        context: LeaseContext,
        *,
        ttl_s: Optional[float] = None,
    ) -> Optional[Lease]:
        """v0.3 PR 7 / doctrine §3 — acquire a typed lease of any kind.

        Sibling to v0.2's :meth:`acquire_lease` (which remains the
        canonical USER_TURN path). For ``kind=USER_TURN`` this delegates
        to :meth:`acquire_lease` to keep behavior byte-identical; for
        all other kinds it runs the check chain filtered by
        :func:`check_applies_to` and registers the granted lease in
        ``self._typed_leases``.

        On rejection, emits ``lease_denied`` with the failing check's
        ``name`` (same emit pattern as v0.2). On grant, returns a
        :class:`Lease` with a fresh ``trace_span_id`` under the room's
        trace root.
        """
        with self._lock:
            if kind == LeaseKind.USER_TURN:
                # USER_TURN keeps the v0.2 path: extract the v0.2-shape
                # parameters from the context and delegate. This avoids
                # parallel maintenance burden for the dominant kind.
                if not isinstance(context, UserTurnContext):
                    raise TypeError(
                        "USER_TURN lease requires UserTurnContext"
                    )
                legacy = self._acquire_user_turn_lease_locked(
                    holder=holder,
                    trigger_event_id=context.trigger_event_id,
                    is_direct_mention=False,
                )
                if legacy is None:
                    return None
                span = self.new_child_span()
                lease = Lease(
                    id=legacy.id,
                    kind=kind,
                    holder=holder,
                    context=context,
                    acquired_at=legacy.acquired_at,
                    expires_at=legacy.expires_at,
                    valid=True,
                    trace_span_id=span.span_id,
                )
                self._typed_leases[lease.id] = lease
                return lease

            # Non-USER_TURN: run the check chain filtered by kind.
            self._pending_lease_context = context
            try:
                checks = self.config.lease_checks or DEFAULT_LEASE_CHECKS
                for chk in checks:
                    if kind not in check_applies_to(chk):
                        continue
                    try:
                        result = chk.check(
                            holder=holder,
                            trigger_event_id=getattr(context, "target_event_id", -1) or -1,
                            is_direct_mention=False,
                            coordinator=self,
                        )
                    except Exception as exc:
                        result = LeaseCheckResult(
                            False, f"check_raised:{type(exc).__name__}"
                        )
                    if not result.passed:
                        self.bus.post_internal(
                            ev.lease_denied(
                                holder=holder,
                                check_name=chk.name,
                                deny_reason=result.deny_reason or "denied",
                                trigger_event_id=getattr(
                                    context, "target_event_id", -1
                                ) or -1,
                            ),
                            auth=_KERNEL_AUTH,
                        )
                        return None
            finally:
                self._pending_lease_context = None

            now = time.monotonic()
            ttl = ttl_s if ttl_s is not None else float(self.config.lease_ttl_s)
            span = self.new_child_span()
            lease = Lease(
                id=self._next_lease_id,
                kind=kind,
                holder=holder,
                context=context,
                acquired_at=now,
                expires_at=now + ttl,
                valid=True,
                trace_span_id=span.span_id,
            )
            self._next_lease_id += 1
            self._typed_leases[lease.id] = lease
            return lease

    def _acquire_user_turn_lease_locked(
        self,
        *,
        holder: str,
        trigger_event_id: int,
        is_direct_mention: bool,
    ) -> Optional[TurnLease]:
        """Inline the v0.2 ``acquire_lease`` body so the typed path
        can reuse it under-lock without re-entering the public API.

        Caller (``acquire_typed_lease``) already holds ``self._lock``.
        """
        checks = self.config.lease_checks or DEFAULT_LEASE_CHECKS
        for chk in checks:
            # v0.3: respect applies_to so a v0.2-shape USER_TURN call
            # site skips CONTROL_ACTION-only checks (``_CapabilityCheck``).
            if LeaseKind.USER_TURN not in check_applies_to(chk):
                continue
            try:
                result = chk.check(
                    holder=holder,
                    trigger_event_id=trigger_event_id,
                    is_direct_mention=is_direct_mention,
                    coordinator=self,
                )
            except Exception as exc:
                result = LeaseCheckResult(False, f"check_raised:{type(exc).__name__}")
            if not result.passed:
                self.bus.post_internal(
                    ev.lease_denied(
                        holder=holder,
                        check_name=chk.name,
                        deny_reason=result.deny_reason or "denied",
                        trigger_event_id=trigger_event_id,
                    ),
                    auth=_KERNEL_AUTH,
                )
                return None

        ut = self._user_turn
        assert ut is not None
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

    def on_stream_chunk(self, lease: TurnLease, *, now: Optional[float] = None) -> None:
        """v0.3 PR 12 — record activity for the streaming-stall watchdog.

        Called by :func:`streaming.run_streaming_call` on each emitted
        chunk so the watchdog can distinguish "stream is alive but
        slow" from "stream is dead and the lease is leaking". Uses
        ``time.monotonic`` (TTL/duration math discipline; see
        ``docs/timing-discipline.md``).
        """
        n = now if now is not None else time.monotonic()
        with self._lock:
            self._last_chunk_at[lease.id] = n

    def check_streaming_stall(self, *, now: Optional[float] = None) -> int:
        """v0.3 PR 12 / closes audit D2 — reap leases with silent streams.

        Iterates active leases; any whose ``_last_chunk_at`` is older
        than ``RoomConfig.stream_stall_threshold_s`` (or whose stream
        has never produced a chunk after the lease's
        ``acquired_at + threshold``) is marked invalid; the
        coordinator emits ``stream_stalled`` then
        ``lease_closed(reason="aborted")`` and pops the lease.

        Holds the coordinator lock for the snapshot iteration;
        emission happens OUTSIDE the lock so a slow subscriber on the
        bus cannot stall actor threads (doctrine P4).

        Returns the count of stalled leases reaped on this tick.
        """
        cutoff = now if now is not None else time.monotonic()
        threshold = float(self.config.stream_stall_threshold_s)
        stalled: list[tuple[TurnLease, float]] = []
        with self._lock:
            for lease_id, lease in list(self._leases.items()):
                if not lease.valid:
                    continue
                last = self._last_chunk_at.get(lease_id, lease.acquired_at)
                silent = cutoff - last
                if silent > threshold:
                    lease.valid = False
                    self._leases.pop(lease_id, None)
                    self._last_chunk_at.pop(lease_id, None)
                    stalled.append((lease, silent))
        # Off-lock emission (doctrine P4): bus subscribers are user code.
        for lease, silent in stalled:
            self.bus.post_internal(
                ev.stream_stalled(
                    lease_id=lease.id,
                    holder=lease.holder,
                    seconds_silent=silent,
                ),
                auth=_KERNEL_AUTH,
            )
            self.bus.post_internal(
                ev.lease_closed(
                    lease_id=lease.id,
                    holder=lease.holder,
                    kind=LeaseKind.USER_TURN.value,
                    reason="aborted",
                ),
                auth=_KERNEL_AUTH,
            )
        return len(stalled)

    def release_lease(self, lease: TurnLease) -> None:
        with self._lock:
            self._leases.pop(lease.id, None)
            self._last_chunk_at.pop(lease.id, None)
            lease.valid = False
            # v0.3 PR 8 (doctrine P2 / §4): emit the unified
            # ``lease_closed`` event alongside the v0.2 lifecycle.
            # ``release`` is the "clean termination" reason; PR 12
            # adds richer reasons (aborted / aborted_validation).
            self.bus.post_internal(
                ev.lease_closed(
                    lease_id=lease.id,
                    holder=lease.holder,
                    kind=LeaseKind.USER_TURN.value,
                    reason="released",
                ),
                auth=_KERNEL_AUTH,
            )

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
            is_direct = bool(triggering and lease.holder in triggering.addressees)

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
                        ob.id, by_event_id=committed_event_id, expected_holder=lease.holder
                    )
            elif status == "passed":
                # PASS is a valid completion: the agent took the floor
                # and chose silence. Resolve the obligation but do not
                # mark a draft (no chat was committed) so speaker_count
                # caps are unaffected.
                ob = ut.obligation_for(lease.holder)
                if ob is not None:
                    self._resolve_obligation_locked(
                        ob.id, by_event_id=None, expected_holder=lease.holder
                    )

            self._maybe_close_user_turn_locked()

    def handle_skip(self, holder: str, trigger_event: Optional[Event] = None) -> None:
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
