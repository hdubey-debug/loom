"""Loom v0.3 — floor-override precedence (PR 10).

Doctrine: §10 (policy/control precedence).

Floor overrides let a control action (PR 9) extend, replace, or
block the policy-derived ``allowed_speakers`` set without rewriting
the policy. Each override is *scoped* — applies for one lease, one
turn, until cleared, or persistently across room config — and *modal*
— add to, replace, or block from the policy plan.

Composition rule (doctrine §10):

```
effective_speakers = base_policy_plan ∩ (ADD-extended set)
                    \ BLOCKED set
                    \ REPLACED set   (REPLACE wins over ADD)
```

Overrides apply in journal order: a later override can countermand an
earlier one. Pruning happens at lifecycle events tracked by the
coordinator (lease close, turn close, explicit clear).

For PR 10 the kernel ships:

- :class:`FloorOverrideMode` and :class:`FloorOverrideScope` enums.
- :class:`ActiveOverride` dataclass stored on
  ``RoomControlState.active_overrides`` (lazy-init'd by the reducer
  so v0.2 RoomControlState instances are forward-compatible).
- :class:`compute_effective_speakers` helper that takes a base set
  and the active overrides and applies the composition rule.
- A reducer for :class:`FloorOverrideEffect` (declared in PR 3,
  wired here) that appends an ``ActiveOverride`` row.
- Three :class:`ControlAction` factory helpers (`GrantFloorAction`,
  `BlockFloorAction`, `OverrideAllowedSpeakersAction`) for the
  PR 9 registry.

Coordinator-side pruning (ONE_LEASE on lease close, CURRENT_TURN on
turn close) is wired in :class:`RoomCoordinator` via
:func:`prune_overrides_for_lease` / :func:`prune_overrides_for_turn`
helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

from loom.kernel.capabilities import CapabilityName
from loom.kernel.effects import ControlEffect, EffectRegistry, FloorOverrideEffect
from loom.kernel.state import KernelState


class FloorOverrideMode(str, Enum):
    """Doctrine §10 — how the override composes with the base plan."""

    ADD = "ADD"
    REPLACE = "REPLACE"
    BLOCK = "BLOCK"


class FloorOverrideScope(str, Enum):
    """Doctrine §10 — how long the override stays in force."""

    ONE_LEASE = "ONE_LEASE"
    CURRENT_TURN = "CURRENT_TURN"
    UNTIL_CLEARED = "UNTIL_CLEARED"
    PERSISTENT_ROOM_CONFIG = "PERSISTENT_ROOM_CONFIG"


@dataclass(frozen=True)
class ActiveOverride:
    """One row in ``RoomControlState.active_overrides`` (PR 10).

    Frozen — lifecycle (expire / prune) replaces the row rather than
    mutating it, mirroring :class:`CapabilityGrant` discipline.
    """

    mode: FloorOverrideMode
    scope: FloorOverrideScope
    speakers: tuple[str, ...]
    turn_id: Optional[int] = None
    lease_id: Optional[int] = None
    applied_at_event_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def compute_effective_speakers(
    base: Iterable[str],
    overrides: Iterable[ActiveOverride],
) -> frozenset[str]:
    """Apply ``overrides`` to ``base`` per the §10 composition rule.

    Overrides apply in journal order; later REPLACE wins over earlier
    ADD on the same speakers; BLOCK strips speakers regardless of
    mode. Returns a frozen set of the effective allowed speakers.
    """
    speakers = set(base)
    has_replace = False
    replace_set: set[str] = set()
    for ov in overrides:
        if ov.mode == FloorOverrideMode.ADD:
            for s in ov.speakers:
                speakers.add(s)
        elif ov.mode == FloorOverrideMode.REPLACE:
            has_replace = True
            replace_set = set(ov.speakers)
        elif ov.mode == FloorOverrideMode.BLOCK:
            for s in ov.speakers:
                speakers.discard(s)
                replace_set.discard(s)
    if has_replace:
        # REPLACE wins by definition — collapse to the replace_set
        # minus BLOCK-filtered entries.
        return frozenset(replace_set)
    return frozenset(speakers)


# ---------------------------------------------------------------------------
# Reducer
# ---------------------------------------------------------------------------


def _ensure_overrides_slot(state: KernelState) -> list:
    """Lazy-init the ``active_overrides`` field on ``state.room.control``.

    v0.2 :class:`RoomControlState` doesn't carry this field; PR 10 adds
    it dynamically on first write so older snapshots forward-load
    cleanly. v0.4+ may promote it to a declared field.
    """
    ctrl = state.room.control
    if not hasattr(ctrl, "active_overrides"):
        # Inject the attribute on the live instance. Using object.__setattr__
        # to bypass dataclass __setattr__ guards on frozen-ish containers.
        object.__setattr__(ctrl, "active_overrides", [])
    return ctrl.active_overrides


def _apply_floor_override(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, FloorOverrideEffect)
    overrides = _ensure_overrides_slot(state)
    try:
        mode = FloorOverrideMode(effect.mode)
    except ValueError as exc:
        raise ValueError(f"unknown FloorOverrideMode: {effect.mode!r}") from exc
    try:
        scope = FloorOverrideScope(effect.scope)
    except ValueError as exc:
        raise ValueError(f"unknown FloorOverrideScope: {effect.scope!r}") from exc
    overrides.append(
        ActiveOverride(
            mode=mode,
            scope=scope,
            speakers=tuple(effect.speakers),
            turn_id=effect.turn_id,
            applied_at_event_id=effect.applied_at_event_id,
        )
    )


def register_floor_override_reducer(registry: EffectRegistry) -> None:
    registry.register("floor_override", 1, _apply_floor_override)


# ---------------------------------------------------------------------------
# Pruning helpers
# ---------------------------------------------------------------------------


def prune_overrides_for_lease(state: KernelState, lease_id: int) -> int:
    """Remove ``ONE_LEASE`` overrides that reference ``lease_id``."""
    overrides = _ensure_overrides_slot(state)
    before = len(overrides)
    overrides[:] = [
        ov for ov in overrides
        if not (ov.scope == FloorOverrideScope.ONE_LEASE and ov.lease_id == lease_id)
    ]
    return before - len(overrides)


def prune_overrides_for_turn(state: KernelState, turn_id: int) -> int:
    """Remove ``CURRENT_TURN`` overrides tied to ``turn_id``."""
    overrides = _ensure_overrides_slot(state)
    before = len(overrides)
    overrides[:] = [
        ov for ov in overrides
        if not (ov.scope == FloorOverrideScope.CURRENT_TURN and ov.turn_id == turn_id)
    ]
    return before - len(overrides)


# ---------------------------------------------------------------------------
# Control actions
# ---------------------------------------------------------------------------


class GrantFloorAction:
    """v0.3 PR 10 — grant the floor to one or more speakers for one lease."""

    name = "GRANT_FLOOR"
    required_capability = CapabilityName.GRANT_FLOOR

    def validate_params(self, params: dict) -> tuple[bool, Optional[str]]:
        speakers = params.get("speakers")
        if not isinstance(speakers, list) or not all(isinstance(s, str) for s in speakers):
            return False, "'speakers' must be list[str]"
        return True, None

    def propose_effect(self, params: dict, state_view) -> tuple[ControlEffect, ...]:
        return (
            FloorOverrideEffect(
                mode=FloorOverrideMode.ADD.value,
                scope=FloorOverrideScope.ONE_LEASE.value,
                speakers=tuple(params["speakers"]),
            ),
        )


class BlockFloorAction:
    """v0.3 PR 10 — block one or more speakers for the current turn."""

    name = "BLOCK_FLOOR"
    required_capability = CapabilityName.UPDATE_ALLOWED_SPEAKERS

    def validate_params(self, params: dict) -> tuple[bool, Optional[str]]:
        speakers = params.get("speakers")
        if not isinstance(speakers, list) or not all(isinstance(s, str) for s in speakers):
            return False, "'speakers' must be list[str]"
        return True, None

    def propose_effect(self, params: dict, state_view) -> tuple[ControlEffect, ...]:
        return (
            FloorOverrideEffect(
                mode=FloorOverrideMode.BLOCK.value,
                scope=FloorOverrideScope.CURRENT_TURN.value,
                speakers=tuple(params["speakers"]),
            ),
        )


class OverrideAllowedSpeakersAction:
    """v0.3 PR 10 — replace the allowed-speakers set until explicitly cleared."""

    name = "OVERRIDE_ALLOWED_SPEAKERS"
    required_capability = CapabilityName.UPDATE_ALLOWED_SPEAKERS

    def validate_params(self, params: dict) -> tuple[bool, Optional[str]]:
        speakers = params.get("speakers")
        if not isinstance(speakers, list) or not all(isinstance(s, str) for s in speakers):
            return False, "'speakers' must be list[str]"
        return True, None

    def propose_effect(self, params: dict, state_view) -> tuple[ControlEffect, ...]:
        return (
            FloorOverrideEffect(
                mode=FloorOverrideMode.REPLACE.value,
                scope=FloorOverrideScope.UNTIL_CLEARED.value,
                speakers=tuple(params["speakers"]),
            ),
        )


FLOOR_OVERRIDE_ACTIONS: tuple = (
    GrantFloorAction(),
    BlockFloorAction(),
    OverrideAllowedSpeakersAction(),
)
