"""Loom v0.3 — typed, versioned semantic effects + reducer registry.

Doctrine: **P6** (event-sourced replay applies committed effects),
**P7** (applied events record versioned ``ControlEffect`` instances),
§5 (effect vocabulary + registry). Closes v0.2.1 audit deferral C4
(full ``(effect_type, schema_version)`` registry).

The kernel's authoritative state mutations are expressed as
:class:`ControlEffect` instances. Each effect is a typed payload with
its own ``schema_version``; the :class:`EffectRegistry` dispatches
``(effect_type, schema_version)`` to a registered reducer that
mutates :class:`loom.kernel.state.KernelState` in place. The pattern
gives v0.3 four properties at once:

1. **Replay-determinism**. The journal records the effect (or the
   triggering event); replay applies the *same* reducer to the *same*
   state delta, irrespective of when replay happens to run.
2. **Schema-evolution discipline**. A v2 reducer can ship alongside
   a v1 reducer in the same release; the registry indexes both. Old
   journals continue to replay through the v1 reducer until they are
   rewritten.
3. **Single mutation path**. Once PR 3 lands, every state mutation in
   the coordinator goes through ``coordinator._apply_effect(effect)``.
   Reviewers can grep for direct ``self.state.X`` assignments outside
   reducers to spot lock-discipline / state-shape regressions.
4. **Pluggability**. Custom control actions (PR 9 / P14) emit
   built-in :class:`ControlEffect` subclasses; reducer behavior stays
   in the kernel even when action *names* are user-defined.

PR 3 declares all 13 doctrine-required effect subclasses so the
shape stabilizes early. Reducers are wired here for the effects
whose backing v0.2 state already exists (topic, anchor, chair,
default_responder, default_summarizer, roles, style). Capability and
budget effects (subsystems landing in PR 5 / PR 6) are declared but
their reducers register themselves at their owning PR's load time —
the registry tolerates unknown ``(type, version)`` lookups by
raising :class:`UnknownEffect`, surfaced as a programmer error in
tests.

The :func:`build_kernel_registry` helper bootstraps the canonical
v0.3 registry the coordinator uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from loom.kernel.context import (
    ContextScope,
    SummaryFailureReason,
    SummaryRecord,
)
from loom.kernel.state import KernelState


# ---------------------------------------------------------------------------
# Base classes and exceptions
# ---------------------------------------------------------------------------


@dataclass
class ControlEffect:
    """Base class for every typed v0.3 semantic effect.

    Subclasses set ``effect_type`` as a class attribute (used as the
    registry key) and ``schema_version`` as an instance default
    (allowing v2 reducers to coexist alongside v1). The optional
    ``applied_at_event_id`` is set by the coordinator after the
    reducer runs so downstream observers can correlate effect-shape
    with the journal line that committed it.
    """

    # Class-level identifier (registry key).
    effect_type: str = field(default="<base>", init=False)

    schema_version: int = 1
    applied_at_event_id: Optional[int] = None


class UnknownEffect(KeyError):
    """Raised when the registry has no reducer for ``(effect_type, schema_version)``.

    Programmer error in v0.3: every effect the coordinator may apply
    must have a registered reducer at room-construction time. The
    boundary test (v0.3 PR 3) asserts that every kernel-emitted
    ``effect_type`` resolves.
    """


# ---------------------------------------------------------------------------
# Kernel-defined effect subclasses (13)
# ---------------------------------------------------------------------------
#
# Ordering mirrors doctrine §5. Each subclass is a frozen-ish typed
# payload — ``ControlEffect`` is a regular dataclass to permit the
# coordinator to set ``applied_at_event_id`` after the reducer runs.


# 1. Floor override (PR 10 wires the reducer fully; PR 3 declares
# the shape).
@dataclass
class FloorOverrideEffect(ControlEffect):
    effect_type: str = field(default="floor_override", init=False)
    mode: str = "ADD"  # FloorOverrideMode (declared in PR 10)
    scope: str = "ONE_LEASE"  # FloorOverrideScope (declared in PR 10)
    speakers: tuple[str, ...] = ()
    turn_id: Optional[int] = None


# 2-6. Slot setters (v0.2-backable; reducers wired here in PR 3).
@dataclass
class TopicChangedEffect(ControlEffect):
    effect_type: str = field(default="topic_changed", init=False)
    topic: Optional[str] = None


@dataclass
class AnchorAssignedEffect(ControlEffect):
    effect_type: str = field(default="anchor_assigned", init=False)
    anchor_id: Optional[str] = None


@dataclass
class ChairAssignedEffect(ControlEffect):
    """Sibling to :class:`AnchorAssignedEffect` for the chair slot.

    The doctrine groups chair under "slot setters"; v0.3 PR 3 wires it
    alongside anchor so the registry covers every slot the coordinator
    can mutate today.
    """

    effect_type: str = field(default="chair_assigned", init=False)
    chair_id: Optional[str] = None


@dataclass
class DefaultResponderSetEffect(ControlEffect):
    effect_type: str = field(default="default_responder_set", init=False)
    participant_id: Optional[str] = None


@dataclass
class DefaultSummarizerSetEffect(ControlEffect):
    effect_type: str = field(default="default_summarizer_set", init=False)
    participant_id: Optional[str] = None


@dataclass
class RolesAssignedEffect(ControlEffect):
    effect_type: str = field(default="roles_assigned", init=False)
    roles: dict[str, str] = field(default_factory=dict)


@dataclass
class StyleChangedEffect(ControlEffect):
    effect_type: str = field(default="style_changed", init=False)
    style: str = "normal"


# 7. Lease cancellation (PR 8 wires lease-closed taxonomy; PR 3
# declares the shape so the registry slot exists).
@dataclass
class LeaseCancelledEffect(ControlEffect):
    effect_type: str = field(default="lease_cancelled", init=False)
    lease_id: int = -1
    reason: str = "cancelled"


# 8-10. Capability effects (PR 5 wires reducers).
@dataclass
class CapabilityGrantedEffect(ControlEffect):
    effect_type: str = field(default="capability_granted", init=False)
    grant_id: str = ""
    grantee_id: str = ""
    capability: str = ""
    grantor_id: str = ""
    expires_at: Optional[float] = None


@dataclass
class CapabilityRevokedEffect(ControlEffect):
    effect_type: str = field(default="capability_revoked", init=False)
    grant_id: str = ""
    revoker_id: str = ""
    reason: str = "revoked"


@dataclass
class CapabilityExpiredEffect(ControlEffect):
    effect_type: str = field(default="capability_expired", init=False)
    grant_id: str = ""


# 11. Policy-switched (PR 9 wires the action; PR 3 declares the shape).
@dataclass
class PolicySwitchedEffect(ControlEffect):
    effect_type: str = field(default="policy_switched", init=False)
    from_policy: str = ""
    to_policy: str = ""


# 12-13. Budget effects (PR 6 wires reducers).
@dataclass
class BudgetReservedEffect(ControlEffect):
    effect_type: str = field(default="budget_reserved", init=False)
    lease_id: int = -1
    scope: Optional[Any] = None  # BudgetScope (declared in PR 6)
    amount: float = 0.0


@dataclass
class BudgetCommittedEffect(ControlEffect):
    effect_type: str = field(default="budget_committed", init=False)
    lease_id: int = -1
    scope: Optional[Any] = None
    actual: float = 0.0


@dataclass
class BudgetRefundedEffect(ControlEffect):
    effect_type: str = field(default="budget_refunded", init=False)
    lease_id: int = -1
    scope: Optional[Any] = None
    amount: float = 0.0
    reason: str = "denied"


# 14-16. v0.3.x PR 3 — view-layer compaction (doctrine P17 / P18 / §3).
# All three reducers mutate :class:`ContextState`, which lives on
# :class:`KernelState.context`. ``register_summary_reducers`` (below)
# binds them into the kernel registry — the coordinator calls that at
# room construction.
@dataclass
class SummaryProposedEffect(ControlEffect):
    """Audit-only effect — does NOT mutate state.

    The proposal is acknowledged for trace and observability
    completeness (so replay sees the same event sequence the live
    room produced), but no :class:`ContextState` field changes until
    the matching :class:`SummaryCommittedEffect` runs. Splitting the
    two events keeps the door open for a future ``summary_reviewed``
    intermediate step (doctrine §11 deferral).
    """

    effect_type: str = field(default="summary_proposed", init=False)
    record: Optional[SummaryRecord] = None


@dataclass
class SummaryCommittedEffect(ControlEffect):
    """Successful commit — install the record into
    :class:`ContextState`, advance ``active_summary_by_scope``, and
    record any supersession edges.
    """

    effect_type: str = field(default="summary_committed", init=False)
    record: Optional[SummaryRecord] = None
    supersedes_summary_ids: tuple[str, ...] = ()


@dataclass
class SummaryFailedEffect(ControlEffect):
    """Rejected proposal — bump
    :class:`ContextState.failure_count` for the
    ``(summarizer_id, scope.as_tuple())`` key. PR 5 reads this counter
    to drive the per-scope backoff.
    """

    effect_type: str = field(default="summary_failed", init=False)
    summarizer_id: str = ""
    scope: Optional[ContextScope] = None
    reason: Optional[SummaryFailureReason] = None


# v0.3.x PR 5 — per-scope compaction disablement (doctrine §7).
@dataclass
class CompactionDisabledEffect(ControlEffect):
    """Mark a ``(summarizer_id, scope)`` pair as disabled for Path A.

    The disablement lives on :attr:`ContextState.failure_count`
    indirectly — the coordinator's Path A scheduling logic checks
    whether the count has reached threshold AND whether
    :attr:`ContextState.disabled_scopes` records the pair. PR 5 adds
    the ``disabled_scopes`` field to :class:`ContextState` and the
    reducer here inserts the key.

    Cleared by re-running :class:`DefaultSummarizerSetEffect` for the
    same slot (the existing reducer is extended in PR 5 to also clear
    ``disabled_scopes`` / ``failure_count`` for the changed summariser).
    """

    effect_type: str = field(default="compaction_disabled", init=False)
    summarizer_id: str = ""
    scope: Optional[ContextScope] = None
    failure_count_at_disable: int = 0
    reason: str = "consecutive_failures"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


Reducer = Callable[[KernelState, ControlEffect], None]
"""Reducer signature.

In v0.3, reducers mutate :class:`KernelState` in place (under the
coordinator lock) and return ``None``. The coordinator bumps
``KernelState.version`` after the reducer returns and stamps
``effect.applied_at_event_id`` with the journal id of the
applied event. Returning a fresh state would be a clean
functional shape but would require deep-copying every sub-state on
every effect — prohibitive on hot paths. v0.4+ may revisit if a
COW state representation is introduced.
"""


class EffectRegistry:
    """``(effect_type, schema_version) → reducer`` lookup table.

    Independent of any specific coordinator instance — bootstrapped
    once at room construction by :func:`build_kernel_registry`. PR 5
    and PR 6 extend a built registry in place by calling
    :meth:`register` with their additional reducers; the coordinator
    stores the registry reference.
    """

    def __init__(self) -> None:
        self._reducers: dict[tuple[str, int], Reducer] = {}

    def register(
        self,
        effect_type: str,
        schema_version: int,
        reducer: Reducer,
    ) -> None:
        """Bind ``reducer`` to ``(effect_type, schema_version)``.

        Re-registering an existing key raises :class:`ValueError` —
        the doctrine treats reducer collisions as programmer error
        (two reducers for the same effect-version would yield
        non-deterministic state on replay).
        """
        key = (effect_type, schema_version)
        if key in self._reducers:
            raise ValueError(
                f"reducer already registered for {effect_type!r} "
                f"schema v{schema_version}"
            )
        self._reducers[key] = reducer

    def get(self, effect_type: str, schema_version: int) -> Reducer:
        """Look up the reducer for ``(effect_type, schema_version)``.

        Raises :class:`UnknownEffect` when no entry exists. Use
        :meth:`has` if a probing pattern is cleaner.
        """
        key = (effect_type, schema_version)
        try:
            return self._reducers[key]
        except KeyError as exc:
            raise UnknownEffect(
                f"no reducer registered for ({effect_type!r}, v{schema_version})"
            ) from exc

    def has(self, effect_type: str, schema_version: int) -> bool:
        return (effect_type, schema_version) in self._reducers

    def apply(self, state: KernelState, effect: ControlEffect) -> None:
        """Look up and run the reducer for ``effect`` against ``state``.

        Caller (the coordinator) holds the lock and is responsible for
        bumping ``KernelState.version`` and stamping
        ``effect.applied_at_event_id`` after the reducer returns. The
        registry stays free of locking and version concerns so PR 5/6
        unit tests can drive reducers directly without a coordinator.
        """
        reducer = self.get(effect.effect_type, effect.schema_version)
        reducer(state, effect)

    # ------------------------------------------------------------------
    # Decorator helper
    # ------------------------------------------------------------------

    def register_reducer(
        self,
        effect_type: str,
        schema_version: int = 1,
    ) -> Callable[[Reducer], Reducer]:
        """Decorator form of :meth:`register`.

        ::

            @registry.register_reducer("topic_changed", 1)
            def _apply_topic(state, effect):
                state.room.set_topic(effect.topic)
        """

        def deco(fn: Reducer) -> Reducer:
            self.register(effect_type, schema_version, fn)
            return fn

        return deco


# ---------------------------------------------------------------------------
# Kernel reducers (v0.2-backable subset)
# ---------------------------------------------------------------------------


def _apply_topic_changed(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, TopicChangedEffect)
    state.room.set_topic(effect.topic)


def _apply_anchor_assigned(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, AnchorAssignedEffect)
    state.room.set_anchor(effect.anchor_id)


def _apply_chair_assigned(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, ChairAssignedEffect)
    state.room.set_chair(effect.chair_id)


def _apply_default_responder_set(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, DefaultResponderSetEffect)
    state.room.set_default_responder(effect.participant_id)


def _apply_default_summarizer_set(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, DefaultSummarizerSetEffect)
    prev = state.room.default_summarizer_id
    state.room.set_default_summarizer(effect.participant_id)
    # v0.3.x PR 5 (doctrine §7): a slot change resets per-scope
    # backoff so the new summariser starts with a clean slate. Clear
    # both the failure_count map and the disabled_scopes set for the
    # OLD slot occupant (the new occupant has no prior history). When
    # ``prev`` is None (first-time install), nothing to clear.
    if prev is not None and prev != effect.participant_id:
        state.context.failure_count = {
            k: v for k, v in state.context.failure_count.items() if k[0] != prev
        }
        state.context.disabled_scopes = {
            k for k in state.context.disabled_scopes if k[0] != prev
        }


def _apply_roles_assigned(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, RolesAssignedEffect)
    state.room.set_roles(dict(effect.roles))


def _apply_style_changed(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, StyleChangedEffect)
    # set_style validates the level + raises on unknown — surface the
    # error to the coordinator which converts it to a typed denial.
    state.room.set_style(effect.style)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# v0.3.x PR 3 — view-layer compaction reducers
# ---------------------------------------------------------------------------


def _apply_summary_proposed(state: KernelState, effect: ControlEffect) -> None:
    """Audit-only reducer — no state mutation.

    The presence of the proposal in the journal is the audit trail;
    splitting "proposed" and "committed" leaves room for a future
    review step without re-shaping the schema.
    """
    assert isinstance(effect, SummaryProposedEffect)
    # Intentionally no state changes — see SummaryProposedEffect.


def _apply_summary_committed(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, SummaryCommittedEffect)
    rec = effect.record
    assert rec is not None, "SummaryCommittedEffect missing record"
    state.context.summaries[rec.summary_id] = rec
    state.context.active_summary_by_scope[rec.scope] = rec.summary_id
    for superseded in effect.supersedes_summary_ids:
        state.context.supersession_edges[superseded] = rec.summary_id


def _apply_summary_failed(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, SummaryFailedEffect)
    if effect.scope is None or not effect.summarizer_id:
        return
    # ANCHOR_CONFLICT failures are NOT counted toward backoff (doctrine
    # §7 — anchor races are benign retries, not summariser bugs).
    if effect.reason == SummaryFailureReason.ANCHOR_CONFLICT:
        return
    key = (effect.summarizer_id, effect.scope.as_tuple())
    state.context.failure_count[key] = state.context.failure_count.get(key, 0) + 1


def _apply_compaction_disabled(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, CompactionDisabledEffect)
    if effect.scope is None or not effect.summarizer_id:
        return
    state.context.disabled_scopes.add(
        (effect.summarizer_id, effect.scope.as_tuple())
    )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def build_kernel_registry() -> EffectRegistry:
    """Construct the canonical v0.3 registry the coordinator stores.

    PR 3 registers the 7 v0.2-backable reducers (the slot setters);
    PR 5, 6, 8, 10, 13 extend this same registry instance (the
    coordinator passes it to those subsystems at room-construction).
    Calling ``build_kernel_registry`` repeatedly yields independent
    registries — useful in tests that want a clean slate.

    v0.3.x PR 3 adds the three view-layer compaction reducers; they
    register inline (not via a sibling helper) because their lifecycle
    is owned by the coordinator from day one.
    """
    reg = EffectRegistry()
    reg.register("topic_changed", 1, _apply_topic_changed)
    reg.register("anchor_assigned", 1, _apply_anchor_assigned)
    reg.register("chair_assigned", 1, _apply_chair_assigned)
    reg.register("default_responder_set", 1, _apply_default_responder_set)
    reg.register("default_summarizer_set", 1, _apply_default_summarizer_set)
    reg.register("roles_assigned", 1, _apply_roles_assigned)
    reg.register("style_changed", 1, _apply_style_changed)
    reg.register("summary_proposed", 1, _apply_summary_proposed)
    reg.register("summary_committed", 1, _apply_summary_committed)
    reg.register("summary_failed", 1, _apply_summary_failed)
    reg.register("compaction_disabled", 1, _apply_compaction_disabled)
    # Effects declared in PR 3 but reducer-wired in their owning PR:
    #   floor_override         → PR 10
    #   lease_cancelled        → PR 8
    #   capability_granted     → PR 5
    #   capability_revoked     → PR 5
    #   capability_expired     → PR 5
    #   policy_switched        → PR 9
    #   budget_reserved        → PR 6
    #   budget_committed       → PR 6
    #   budget_refunded        → PR 6
    return reg
