"""Loom v0.3 — :class:`BudgetLedger` three-way accounting.

Doctrine: **P9** (three-way budget reservation/commit/refund),
§9 (budget ledger).

A *budget* in v0.3 is an integer (token / call / dollar / whatever-
operationally-relevant) accounting unit that callers
**reserve** when a lease is acquired (worst-case estimate),
**commit** when the lease releases successfully (actual cost), and
**refund** when the lease is denied / cancelled / expired before
useful work happened. There is also a
**partial_commit_and_refund** path for the awkward middle: an LLM
call succeeded but post-stream validation suppressed the output —
the LLM tokens are committed (real cost), the post-LLM headroom is
refunded.

Data shape:

- :class:`BudgetScope` is a frozen tuple of dimensions
  ``(room_id, participant_id, action_kind, time_window)`` so the
  ledger can accumulate spend at any combination. Caller is
  responsible for canonical hashing; passing different orderings of
  the same dimensions produces different scopes.
- :class:`BudgetReservation` is one outstanding reservation keyed by
  ``lease_id`` (one reservation per lease in v0.3; PR 12+ may relax).
- :class:`BudgetLedger` stores ``reservations`` (live), ``commits``
  (per-scope cumulative), ``refunds`` (per-scope cumulative), and
  ``limits`` (per-scope; ``None`` or absent = no limit).

Three-way accounting invariant: at any time,
``sum_over_live_reservations(amount) + commits[scope] - refunds[scope]
<= limits[scope]``. The :meth:`can_reserve` query reports this without
mutating state.

Scope hierarchy: a child scope's amount cannot exceed the parent
scope's remaining capacity. PR 6 implements the simple two-level
shape (room → participant) implicitly via the dimensions; deeper
hierarchies join in PR 12+.

Reducers for ``BudgetReservedEffect`` / ``BudgetCommittedEffect`` /
``BudgetRefundedEffect`` register via :func:`register_budget_reducers`,
called by the coordinator's ``__init__`` immediately after
:func:`loom.kernel.effects.build_kernel_registry`.

Replay-determinism: ``reserve`` accepts an explicit ``now`` parameter
so the watchdog drives it in live operation while the journal-replay
path drives it from each event's ``Event.ts``. The ledger never
reads the clock itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from loom.kernel.effects import (
    BudgetCommittedEffect,
    BudgetRefundedEffect,
    BudgetReservedEffect,
    ControlEffect,
    EffectRegistry,
)
from loom.kernel.state import KernelState


# ---------------------------------------------------------------------------
# Scope + reservation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetScope:
    """Dimensional key into the ledger.

    All four fields default-initialise to ``None`` so the room-level
    "no-narrowing" scope is just ``BudgetScope()``. Equality and
    hashing are by tuple — passing ``room_id=None`` and
    ``room_id="default"`` produce distinct scopes; canonicalize at
    the caller if needed.

    ``time_window`` is reserved for v0.5+ (sliding/rolling windows);
    PR 6 always treats it as ``None``.
    """

    room_id: Optional[str] = None
    participant_id: Optional[str] = None
    action_kind: Optional[str] = None
    time_window: Optional[str] = None  # v0.5+ reservation.


@dataclass(frozen=True)
class BudgetReservation:
    """One outstanding reservation, keyed by ``lease_id``.

    Frozen — partial commits replace the reservation rather than
    mutating in place, keeping replay idempotent.
    """

    lease_id: int
    scope: BudgetScope
    amount: float
    reserved_at: float


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass
class BudgetLedger:
    """Three-way ledger. Single-writer (the coordinator under lock).

    ``commits`` and ``refunds`` are flat per-scope sums — the running
    total per scope. ``reservations`` is the live set of outstanding
    holds (one per lease). ``limits`` is the per-scope ceiling that
    :meth:`can_reserve` consults; absent or ``None`` means "no
    limit".
    """

    reservations: dict[int, BudgetReservation] = field(default_factory=dict)
    commits: dict[BudgetScope, float] = field(default_factory=dict)
    refunds: dict[BudgetScope, float] = field(default_factory=dict)
    limits: dict[BudgetScope, float] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Aggregates
    # ------------------------------------------------------------------

    def _live_reserved_at(self, scope: BudgetScope) -> float:
        return sum(r.amount for r in self.reservations.values() if r.scope == scope)

    def _net_committed(self, scope: BudgetScope) -> float:
        return self.commits.get(scope, 0.0) - self.refunds.get(scope, 0.0)

    def outstanding(self, scope: BudgetScope) -> float:
        """Live reservations + net committed for ``scope``.

        This is what :meth:`can_reserve` compares against the limit.
        """
        return self._live_reserved_at(scope) + self._net_committed(scope)

    def remaining(self, scope: BudgetScope) -> float:
        """``limits[scope] - outstanding(scope)``, or ``+inf`` if no limit."""
        limit = self.limits.get(scope)
        if limit is None:
            return float("inf")
        return limit - self.outstanding(scope)

    # ------------------------------------------------------------------
    # Mutation primitives
    # ------------------------------------------------------------------

    def can_reserve(self, scope: BudgetScope, amount: float) -> bool:
        """True iff adding a reservation of ``amount`` would not exceed the limit.

        Children with deeper scopes also consult their parent scope by
        convention (callers pass the child scope; the ledger's flat
        dictionaries handle each independently — multi-level
        hierarchies are caller-orchestrated in v0.3).
        """
        return amount <= self.remaining(scope)

    def reserve(
        self,
        lease_id: int,
        scope: BudgetScope,
        amount: float,
        *,
        now: float = 0.0,
    ) -> BudgetReservation:
        """Hold ``amount`` against ``scope`` for ``lease_id``.

        Raises :class:`ValueError` if a reservation already exists
        for that lease (one-reservation-per-lease invariant) or if
        ``amount`` exceeds the remaining headroom.
        """
        if lease_id in self.reservations:
            raise ValueError(f"lease_id {lease_id} already has a reservation")
        if not self.can_reserve(scope, amount):
            raise ValueError(
                f"reservation of {amount} would exceed limit "
                f"{self.limits.get(scope)} for scope {scope}"
            )
        r = BudgetReservation(
            lease_id=lease_id,
            scope=scope,
            amount=amount,
            reserved_at=now,
        )
        self.reservations[lease_id] = r
        return r

    def commit(self, lease_id: int, actual: float) -> tuple[BudgetReservation, float]:
        """Commit ``actual`` against the reservation for ``lease_id``.

        ``actual`` may be less than or equal to the reserved amount.
        The reservation is removed; ``commits[scope]`` is increased
        by ``actual``. Returns ``(original_reservation, actual)``.

        Raises :class:`KeyError` if no reservation exists for
        ``lease_id``; :class:`ValueError` if ``actual`` exceeds the
        reserved amount.
        """
        r = self.reservations.pop(lease_id, None)
        if r is None:
            raise KeyError(f"no reservation for lease {lease_id}")
        if actual > r.amount:
            raise ValueError(f"commit {actual} exceeds reservation {r.amount}")
        self.commits[r.scope] = self.commits.get(r.scope, 0.0) + actual
        return r, actual

    def refund(self, lease_id: int, reason: str) -> BudgetReservation:
        """Release the entire reservation for ``lease_id`` (no commit).

        Used when a lease is denied / cancelled / TTL-expired before
        any useful work. Returns the released reservation; raises
        :class:`KeyError` if none exists.
        """
        r = self.reservations.pop(lease_id, None)
        if r is None:
            raise KeyError(f"no reservation for lease {lease_id}")
        # We do NOT add to ``refunds[scope]`` here — a pure refund
        # never had a commit to offset. The ``refunds`` dict tracks
        # post-commit returns; pre-commit cancellations just drop the
        # hold. (Distinct from partial_commit_and_refund below, which
        # *does* update ``refunds`` because part of the spend was
        # already committed.)
        return r

    def partial_commit_and_refund(
        self,
        lease_id: int,
        actual_used: float,
    ) -> tuple[BudgetReservation, float, float]:
        """Commit ``actual_used`` and refund the rest of the reservation.

        Returns ``(reservation, committed, refunded)``. Used when an
        LLM call succeeded (committed cost = LLM tokens) but post-
        stream validation suppressed the output, so the post-LLM
        headroom is released. Raises :class:`KeyError` for no
        reservation; :class:`ValueError` if ``actual_used`` exceeds
        the reserved amount.
        """
        r = self.reservations.pop(lease_id, None)
        if r is None:
            raise KeyError(f"no reservation for lease {lease_id}")
        if actual_used > r.amount:
            raise ValueError(f"committed {actual_used} exceeds reservation {r.amount}")
        refunded = r.amount - actual_used
        self.commits[r.scope] = self.commits.get(r.scope, 0.0) + actual_used
        self.refunds[r.scope] = self.refunds.get(r.scope, 0.0) + refunded
        return r, actual_used, refunded


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------


def _apply_budget_reserved(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, BudgetReservedEffect)
    if state.budget is None:
        state.budget = BudgetLedger()
    scope = effect.scope if isinstance(effect.scope, BudgetScope) else BudgetScope()
    state.budget.reserve(effect.lease_id, scope, float(effect.amount))


def _apply_budget_committed(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, BudgetCommittedEffect)
    if state.budget is None:
        state.budget = BudgetLedger()
    state.budget.commit(effect.lease_id, float(effect.actual))


def _apply_budget_refunded(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, BudgetRefundedEffect)
    if state.budget is None:
        state.budget = BudgetLedger()
    state.budget.refund(effect.lease_id, effect.reason)


# ---------------------------------------------------------------------------
# Registry extension
# ---------------------------------------------------------------------------


def register_budget_reducers(registry: EffectRegistry) -> None:
    """Wire the three v0.3 budget reducers into ``registry``.

    Called by :class:`RoomCoordinator.__init__` immediately after
    :func:`register_capability_reducers` so the coordinator-bound
    registry covers the budget shape.
    """
    registry.register("budget_reserved", 1, _apply_budget_reserved)
    registry.register("budget_committed", 1, _apply_budget_committed)
    registry.register("budget_refunded", 1, _apply_budget_refunded)
