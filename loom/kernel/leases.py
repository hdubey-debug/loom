"""Loom v0.3 — unified :class:`Lease` abstraction.

Doctrine: **P8** (one ``Lease`` abstraction with ``kind`` discriminator),
§3 (lease abstraction).

v0.2 had a single :class:`loom.kernel.coordinator.TurnLease` type that
gated speech (USER_TURN). v0.3 generalises: every privileged action
the kernel grants — speech, control actions, future tool calls,
workflow steps, reactive system actions — runs under a *lease*. The
:class:`LeaseKind` discriminator and :class:`LeaseContext` tagged
union let one set of bookkeeping (acquire / validate / release / TTL
expiry / budget reservation) cover all five kinds.

For back-compat, :class:`loom.kernel.coordinator.TurnLease` stays in
place as the USER_TURN specialization — every v0.2 call site that
constructs ``TurnLease(id=..., holder=..., user_turn_id=..., ...)``
continues to work, and ``acquire_lease`` still returns ``TurnLease``.
PR 7 adds the parallel :class:`Lease` shape + the
:meth:`RoomCoordinator.acquire_typed_lease` entry point that returns
it; the v0.2 path becomes a thin wrapper around the typed path in
later PRs.

Lease checks are filtered by :attr:`LeaseCheck.applies_to` — a check
that only makes sense for user-turn leases (``_OpenTurnCheck``,
``_AllowedSpeakerCheck``, etc.) declares ``applies_to =
frozenset({LeaseKind.USER_TURN})``; the iteration in
``acquire_typed_lease`` skips checks whose ``applies_to`` doesn't
contain the requested kind. Custom checks default to "all kinds"
when ``applies_to`` is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class LeaseKind(str, Enum):
    """Doctrine §3 — the v0.3 + v0.3.x lease kinds.

    - ``USER_TURN`` — gates speech (a ``chat`` event by a
      participant). v0.2's only kind; in v0.3 still routed through
      ``acquire_lease``.
    - ``CONTROL_ACTION`` — gates a control-action proposal (PR 9).
      Requires the proposer to have the corresponding
      :class:`CapabilityName` mutation verb.
    - ``TOOL_INVOCATION`` — reserved for v0.4 tool execution.
    - ``WORKFLOW_STEP`` — reserved for v0.5 workflow subsystem.
    - ``REACTIVE`` — gates system-initiated reactions (e.g. dead
      letter reroute). Not subject to per-participant caps or
      throttle.
    - ``SUMMARIZATION`` — v0.3.x PR 5 (doctrine P22 / §3). Gates
      view-layer compaction. Both Path A (policy-triggered) and
      Path B (control-action) converge on this lease kind so the
      slot/capability gate is identical regardless of trigger.
    """

    USER_TURN = "user_turn"
    CONTROL_ACTION = "control_action"
    TOOL_INVOCATION = "tool_invocation"
    WORKFLOW_STEP = "workflow_step"
    REACTIVE = "reactive"
    SUMMARIZATION = "summarization"


ALL_LEASE_KINDS: frozenset[LeaseKind] = frozenset(LeaseKind)


# ---------------------------------------------------------------------------
# Tagged-union contexts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserTurnContext:
    """Context for a :data:`LeaseKind.USER_TURN` lease."""

    user_turn_id: int
    trigger_event_id: int
    room_epoch: int
    thread_id: str = "main"


@dataclass(frozen=True)
class ControlActionContext:
    """Context for a :data:`LeaseKind.CONTROL_ACTION` lease (PR 9).

    ``required_capability`` is the action's
    :class:`loom.kernel.capabilities.CapabilityName` (passed as
    its string value to avoid an import cycle with ``capabilities``).
    The kernel's ``_CapabilityCheck`` (PR 7) uses it to gate the
    lease — proposer must hold this capability or the lease is
    denied with ``insufficient_capability``.
    """

    action_name: str
    params: tuple = ()  # frozen-tuple key=value pairs; PR 9 normalizes
    target_event_id: Optional[int] = None
    required_capability: Optional[str] = None
    thread_id: str = "main"


@dataclass(frozen=True)
class ToolInvocationContext:
    """Context for a :data:`LeaseKind.TOOL_INVOCATION` lease (v0.4 reserved)."""

    tool_name: str
    params: tuple = ()
    thread_id: str = "main"


@dataclass(frozen=True)
class WorkflowStepContext:
    """Context for a :data:`LeaseKind.WORKFLOW_STEP` lease (v0.5 reserved)."""

    workflow_id: str
    step_id: str
    thread_id: str = "main"


@dataclass(frozen=True)
class ReactiveContext:
    """Context for a :data:`LeaseKind.REACTIVE` lease."""

    reason: str
    source_event_id: Optional[int] = None
    thread_id: str = "main"


@dataclass(frozen=True)
class SummarizationContext:
    """Context for a :data:`LeaseKind.SUMMARIZATION` lease (v0.3.x PR 5).

    Doctrine P22 / §3. Both Path A (policy-triggered) and Path B
    (control action) produce a SUMMARIZATION lease with this context.

    - ``scope`` is the :class:`loom.kernel.context.ContextScope` the
      summary will cover; stamped onto the produced summary events.
    - ``covers_event_range`` is the inclusive ``(lo, hi)`` slice the
      summariser will summarise; chosen by the caller (policy or user)
      and validated by the under-lock anchor check at commit time.
    - ``triggered_by`` is ``"policy"`` (Path A) or ``"control_action"``
      (Path B) for audit.
    - ``triggering_event_id`` is the bus id of the event that drove
      the lease (e.g. the user's ``/summarize`` chat event, or the
      pre-turn pressure-check event); ``-1`` when triggered without a
      single drive event.
    - ``required_capability`` mirrors :class:`ControlActionContext` —
      the kernel's :class:`_CapabilityCheck` reads it for the
      ``SUMMARIZE`` gate on Path B and ``EMIT_SUMMARY`` on Path A.
    """

    scope: Any  # ContextScope (typed-import would cycle with context.py)
    covers_event_range: tuple
    triggered_by: str = "policy"
    triggering_event_id: int = -1
    required_capability: Optional[str] = None
    thread_id: str = "main"


LeaseContext = (
    UserTurnContext
    | ControlActionContext
    | ToolInvocationContext
    | WorkflowStepContext
    | ReactiveContext
    | SummarizationContext
)


# Mapping from kind → expected context type — used by
# :meth:`Lease.__post_init__` to enforce the tagged-union invariant.
_KIND_CONTEXT_TYPES: dict[LeaseKind, type] = {
    LeaseKind.USER_TURN: UserTurnContext,
    LeaseKind.CONTROL_ACTION: ControlActionContext,
    LeaseKind.TOOL_INVOCATION: ToolInvocationContext,
    LeaseKind.WORKFLOW_STEP: WorkflowStepContext,
    LeaseKind.REACTIVE: ReactiveContext,
    LeaseKind.SUMMARIZATION: SummarizationContext,
}


# ---------------------------------------------------------------------------
# Lease
# ---------------------------------------------------------------------------


@dataclass
class Lease:
    """Generalised v0.3 lease — one shape for every :class:`LeaseKind`.

    For USER_TURN leases, v0.2's :class:`TurnLease` continues to be
    used at the coordinator boundary so existing call sites
    (``coordinator.acquire_lease``, ``coordinator.validate_lease``,
    ``streaming.run_streaming_call``) keep working byte-identical.
    :class:`Lease` is the future shape: PR 9's control action lifecycle
    uses :meth:`RoomCoordinator.acquire_typed_lease` which returns a
    :class:`Lease` directly.

    Invariant: ``self.kind`` matches ``type(self.context)`` per
    :data:`_KIND_CONTEXT_TYPES`. ``__post_init__`` enforces this — a
    mismatched construction raises :class:`TypeError`.
    """

    id: int
    kind: LeaseKind
    holder: str
    context: LeaseContext
    acquired_at: float  # time.monotonic
    expires_at: float  # time.monotonic
    valid: bool = True
    # v0.3 PR 4 (doctrine P12): every lease begins a child span under
    # the coordinator's room-session trace. PR 9 stamps it on every
    # event posted while the lease is held.
    trace_span_id: Optional[str] = None

    def __post_init__(self) -> None:
        expected = _KIND_CONTEXT_TYPES.get(self.kind)
        if expected is None:
            raise TypeError(f"unknown LeaseKind: {self.kind!r}")
        if not isinstance(self.context, expected):
            raise TypeError(
                f"Lease.kind={self.kind.value!r} requires context of type "
                f"{expected.__name__}, got {type(self.context).__name__}"
            )


# ---------------------------------------------------------------------------
# Lease-check applicability
# ---------------------------------------------------------------------------


def check_applies_to(check: object) -> frozenset[LeaseKind]:
    """Return the lease kinds a :class:`LeaseCheck` applies to.

    Lookup order:

    1. ``check.applies_to`` if the attribute exists and is a
       ``frozenset[LeaseKind]``.
    2. ``ALL_LEASE_KINDS`` (default) — pre-v0.3 checks have no
       ``applies_to``; they are treated as universally-applicable so
       USER_TURN flows continue to gate the same way.

    Centralizing this lookup means a new custom check can opt into
    the v0.3 typed-lease world without changing the LeaseCheck
    Protocol (which is in :mod:`loom.contracts` and back-compat
    sensitive).
    """
    applies = getattr(check, "applies_to", None)
    if isinstance(applies, frozenset):
        return applies
    return ALL_LEASE_KINDS
