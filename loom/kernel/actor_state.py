"""Loom v0.3 — actor state persistence (PR 13; closes audit A3).

Doctrine: **P6** (event-sourced replay applies committed effects),
extended to actor cursor state.

v0.2 actors carried their cursor (the "highest event id examined")
in-process only — a restart re-read the journal from id 0 and
re-processed every event, with all the redundancy that implies. The
v0.2.1 hardening audit flagged this (finding A3) and deferred the
fix to v0.3.

This module ships the minimal persistence shape:

- :class:`ActorStateRecord` — frozen dataclass holding
  ``(participant_id, cursor, last_advanced_at_event_id)``. One per
  participant.
- :class:`CursorAdvancedEffect` — typed semantic effect (extends
  :class:`loom.kernel.effects.ControlEffect`). Reducer registered
  via :func:`register_cursor_advanced_reducer`.
- :class:`KernelState.actors` (declared by PR 1 as ``Optional[Any]``;
  PR 13 hydrates it as ``dict[str, ActorStateRecord]`` when the
  first cursor event applies).

The full actor-side wiring (have ``ParticipantActor._advance_cursor``
apply a :class:`CursorAdvancedEffect` through the coordinator) is a
separate refactor — PR 13 ships the data shape + reducer so the
actor can opt in. The reducer is idempotent: replaying the journal
reconstructs the same cursor map.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from loom.kernel.effects import ControlEffect, EffectRegistry
from loom.kernel.state import KernelState


@dataclass(frozen=True)
class ActorStateRecord:
    """One participant's persisted actor state — minimal in v0.3.

    Frozen — the reducer replaces the record rather than mutating
    in place. v0.5+ will extend with worker / wakeup metadata.
    """

    participant_id: str
    cursor: int
    last_advanced_at_event_id: int = -1


@dataclass
class CursorAdvancedEffect(ControlEffect):
    """v0.3 PR 13 — semantic effect for actor cursor advance.

    Carries the source-target pair so reducers can both replace the
    record and reason about the delta. ``examined_event_ids`` is the
    sequence of events the actor inspected during this step (used by
    PR 4 ``causal_refs`` for observability links into the
    ``cursor_advanced`` journal event).
    """

    effect_type: str = field(default="cursor_advanced", init=False)
    participant_id: str = ""
    from_cursor: int = -1
    to_cursor: int = -1
    examined_event_ids: tuple = ()


def _apply_cursor_advanced(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, CursorAdvancedEffect)
    if state.actors is None:
        state.actors = {}
    state.actors[effect.participant_id] = ActorStateRecord(
        participant_id=effect.participant_id,
        cursor=effect.to_cursor,
        last_advanced_at_event_id=effect.applied_at_event_id or -1,
    )


def register_cursor_advanced_reducer(registry: EffectRegistry) -> None:
    """Wire the cursor-advanced reducer into ``registry``."""
    registry.register("cursor_advanced", 1, _apply_cursor_advanced)


def cursor_for(state: KernelState, participant_id: str) -> Optional[int]:
    """Return the persisted cursor for ``participant_id`` (or ``None``)."""
    if state.actors is None:
        return None
    rec = state.actors.get(participant_id)
    return rec.cursor if rec is not None else None
