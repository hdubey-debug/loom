"""Loom v0.3 — ``KernelState`` transactional root.

v0.3 doctrine **P5** (`docs/internal/study/11-orchestration-os-doctrine.md`)
makes ``KernelState`` the single canonical root for all kernel-owned mutable
state. In v0.2 that root was :class:`loom.kernel.room.RoomState`; in v0.3
``KernelState`` wraps the room as one of several first-class subsystem
sub-states.

Subsystems landed in this PR (PR 1): ``room``.

Subsystem fields **reserved** for later v0.3 PRs (declared here as
``None`` placeholders so the field name stabilizes early; promoted to a
concrete dataclass instance when the owning PR lands):

- ``capabilities`` — v0.3 PR 5 (``CapabilityState``; doctrine §6, P1/P10).
- ``budget`` — v0.3 PR 6 (``BudgetLedger``; doctrine §9, P9).
- ``actors`` — v0.3 PR 13 (``ActorStateRecord`` map; closes audit A3).

Subsystem fields reserved for **post-v0.3** releases (declared here so
serialization shape and read-only views stabilize early):

- ``workflow`` — v0.5+.
- ``tools`` — v0.4+.

The ``version`` counter increments on every applied effect (every state
mutation, post-PR 3). It is the transactional fence for optimistic
re-read patterns (e.g. v0.3 PR 12's off-lock policy: snapshot under lock
→ classify off-lock → re-acquire lock, retry if ``version`` shifted).

``schema_version`` is the snapshot envelope version. v0.2 snapshots are
v5; v0.3 PR 1 bumps to v6 (this module's snapshot shape). The
``loom.kernel.journal`` module reads both and migrates v5→v6 on load.

The view layer (:class:`KernelStateView`) is what policy callbacks
receive in v0.3. It mirrors v0.2's :class:`loom.kernel.room.RoomStateView`
discipline: deep-frozen, no setters, read-only for the duration of the
policy call. Sub-state views are added as their owning PRs land (PR 5
adds ``capabilities``, PR 6 adds ``budget``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from loom.kernel.context import ContextState, new_context_state
from loom.kernel.room import RoomState, RoomStateView


# Snapshot version owned by KernelState. Kept here (rather than in
# journal.py) so the data shape and its version stay co-located.
# v0.3.x PR 2: bumped from 6 → 7 to nest ``ContextState`` in the
# snapshot envelope.
KERNEL_STATE_SCHEMA_VERSION = 7


@dataclass
class KernelState:
    """Transactional root for all kernel-owned mutable state (v0.3 P5).

    Single-writer (the coordinator). Sub-state fields are mutated in
    place by reducers (post-PR 3) running under the coordinator lock;
    ``version`` is bumped by the coordinator on each applied effect.

    PR 1 ships with only ``room`` as a populated sub-state. The
    ``capabilities`` / ``budget`` / ``actors`` placeholders are
    ``Optional[Any]`` and default to ``None`` so they can be promoted
    to concrete dataclasses in their owning PRs without changing this
    file's field order or breaking snapshot back-compat.
    """

    room: RoomState

    # v0.3 PR 5 — populated as CapabilityState; doctrine §6.
    capabilities: Optional[Any] = None
    # v0.3 PR 6 — populated as BudgetLedger; doctrine §9.
    budget: Optional[Any] = None
    # v0.3 PR 13 — populated as dict[str, ActorStateRecord]; closes audit A3.
    actors: Optional[Any] = None
    # v0.3.x PR 2 — view-layer compaction state (doctrine P17).
    # Replaces the legacy `summary` events as the canonical source of
    # truth for the prompt builder; populated by the PR 3 reducers.
    context: ContextState = field(default_factory=new_context_state)
    # Reserved (v0.5+) — workflow subsystem.
    workflow: Optional[Any] = None
    # Reserved (v0.4+) — tool subsystem.
    tools: Optional[Any] = None

    # Monotonic transactional counter. Bumped under coordinator lock on
    # every applied effect. Used by off-lock-policy revalidation
    # (v0.3 PR 12) and by debug / test helpers that want a
    # mutation-stamp without grepping the bus.
    version: int = 0

    # Snapshot envelope version. Class-level constant exposed as an
    # instance field so it round-trips through dataclass __init__ /
    # asdict cleanly. v0.3 PR 1: 6.
    schema_version: int = KERNEL_STATE_SCHEMA_VERSION

    def bump_version(self) -> int:
        """Increment ``version`` and return the new value.

        Caller MUST hold the coordinator lock. PR 3's effect-registry
        reducer dispatch is the long-term call site; PR 1 introduces the
        helper so coordinators can opt into version bumps at mutation
        points incrementally without waiting for the full registry.
        """
        self.version += 1
        return self.version

    def view(self) -> "KernelStateView":
        """Return a deep-frozen read-only view of this state.

        Equivalent to :meth:`RoomState.view` lifted to the kernel root.
        Policy code receives a ``KernelStateView`` (not a
        ``RoomStateView``) post-v0.3; the ``room`` field is the v0.2-
        compatible projection. Sub-state views (``capabilities``,
        ``budget``) are added as their owning PRs land.
        """
        return KernelStateView(
            room=self.room.view(),
            version=self.version,
            schema_version=self.schema_version,
            context=self.context,
        )


@dataclass(frozen=True)
class KernelStateView:
    """Read-only projection of :class:`KernelState` for policy callbacks.

    Mirrors v0.2's :class:`RoomStateView` discipline: deep-frozen, no
    setters, captures the state as of the call (not live).

    PR 1 exposes only ``room`` + ``version`` + ``schema_version``.
    Subsequent PRs extend:

    - PR 5 adds ``capabilities: CapabilityStateView``.
    - PR 6 adds ``budget: BudgetLedgerView``.
    - v0.3.x PR 2 adds ``context: ContextState`` (live reference, not
      a deep-copy view — :class:`ContextState`'s maps are only
      mutated under the coordinator lock by registered reducers).

    Reserved (post-v0.3) fields are not on the view until the owning
    subsystem lands — the field set tracks what is actually queryable.
    """

    room: RoomStateView
    version: int
    schema_version: int
    context: ContextState


def new_kernel_state(room: RoomState) -> KernelState:
    """Construct a fresh :class:`KernelState` wrapping ``room``.

    Equivalent to ``KernelState(room=room)``. Provided as a named
    constructor so caller intent ("I want a v0.3 kernel state root with
    these defaults") is grep-able and future field additions are
    centralized.
    """
    return KernelState(room=room)
