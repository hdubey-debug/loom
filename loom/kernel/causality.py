"""Loom v0.3 — typed causal refs + trace context.

Doctrine: **P11** (typed causal graph), **P12** (trace metadata on every
event), §8 (causal refs & trace). Closes v0.2.1 audit deferral C3
typed form.

v0.2.1 PR 3 reserved the ``Event.causal_refs: tuple = ()`` envelope
slot with the explicit promise that v0.3 would tighten the type. This
module is that tightening: a small fully-frozen dataclass tree that
serializes to / from JSON with predictable shape, plus the
:class:`TraceContext` introduced to carry observability metadata
across lease scopes.

Shape:

- :class:`EventRef` identifies an event by ``(room_id, event_id,
  event_type)``. Cross-room refs are admissible (and will be needed
  by future workflow/synthesis events); v0.3 single-room rooms always
  reference their own.
- :class:`CausalRelation` enumerates the kernel-recognised causality
  predicates. Each ``CausalRef`` instance carries exactly one
  relation; multi-relation links are represented as multiple refs in
  the same ``causal_refs`` tuple.
- :class:`CausalRef` pairs an :class:`EventRef` with a
  :class:`CausalRelation` plus an optional free-form ``note`` for
  human-readability in the journal.
- :class:`TraceContext` carries ``(trace_id, span_id,
  parent_span_id)``. The coordinator allocates one ``trace_id`` per
  room at construction; each lease begins a new span (child of the
  room trace); events posted under a held lease inherit the lease's
  ``span_id``.

JSON round-trip:

- ``CausalRef.to_jsonable()`` returns a dict; ``CausalRef.from_jsonable``
  reconstructs. Enum survives via the ``.value`` string.
- ``TraceContext.to_jsonable() / from_jsonable`` mirror that pattern.
- The :class:`loom.kernel.events.Event` envelope serializes
  ``causal_refs`` as ``list[dict]`` and ``trace`` as ``dict | None``;
  legacy v0.2.1 lines without these keys load with the defaults
  (``()`` and ``None``).

ID generation: ULIDs would be ideal but the kernel has no ULID
dependency; we use a 128-bit random hex (`secrets.token_hex(16)`)
which is monotonically-comparable-enough for observability while
remaining stdlib-only.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Causal references
# ---------------------------------------------------------------------------


class CausalRelation(str, Enum):
    """Doctrine §8 — the kernel-recognised causality predicates.

    Subclassing ``str`` keeps the JSON form trivial (``e.value`` is
    serialized directly) and lets equality interop with raw strings
    in tests / replay-shim code.

    Predicates:

    - ``RESPONDS_TO`` — a draft / chat event answers a user post or
      another participant's chat. The relation an end-user mentally
      models as "reply to X".
    - ``TOOL_RESULT_FOR`` — an execution-plane event records the
      outcome of a tool call. v0.4 introduces tool events; v0.3 uses
      this predicate for the analogous ``stream_*`` → trigger link so
      streaming events can already reference their cause.
    - ``CONTROL_ACTION_APPLIED`` — a ``control_action_applied`` (PR 8)
      references the proposing ``control_action_proposed`` event.
    - ``JOINED_FROM`` — a participant added via cross-room join cites
      the originating room/event.
    - ``TRIGGERED_BY`` — generic "this event was caused by X" used
      when none of the more-specific predicates apply.
    - ``REPLAY_OF`` — replay-shim event that re-derives state from a
      historical event.
    - ``DEAD_LETTER_REROUTED_FROM`` — a ``dead_letter`` event
      references the original mention that became un-routable.
    """

    RESPONDS_TO = "responds_to"
    TOOL_RESULT_FOR = "tool_result_for"
    CONTROL_ACTION_APPLIED = "control_action_applied"
    JOINED_FROM = "joined_from"
    TRIGGERED_BY = "triggered_by"
    REPLAY_OF = "replay_of"
    DEAD_LETTER_REROUTED_FROM = "dead_letter_rerouted_from"


@dataclass(frozen=True)
class EventRef:
    """``(room_id, event_id, event_type)`` triple identifying an event.

    ``event_type`` is denormalized for human-readable journal lines;
    it must agree with the referenced event's actual type. The
    coordinator does not validate the agreement at PR 4 — that's a
    journal-replay-time check left for v0.4+ when cross-room refs go
    live.
    """

    room_id: str
    event_id: int
    event_type: str

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "event_id": self.event_id,
            "event_type": self.event_type,
        }

    @classmethod
    def from_jsonable(cls, d: Any) -> "EventRef":
        if not isinstance(d, dict):
            raise ValueError(f"EventRef requires dict, got {type(d).__name__}")
        return cls(
            room_id=str(d.get("room_id", "")),
            event_id=int(d.get("event_id", 0)),
            event_type=str(d.get("event_type", "")),
        )


@dataclass(frozen=True)
class CausalRef:
    """One typed link in :attr:`Event.causal_refs`.

    Pairs an :class:`EventRef` (target) with a :class:`CausalRelation`
    (predicate). The optional ``note`` is free-form text for
    observability — it never affects routing or replay.
    """

    ref: EventRef
    relation: CausalRelation
    note: Optional[str] = None

    def to_jsonable(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ref": self.ref.to_jsonable(),
            "relation": self.relation.value,
        }
        if self.note is not None:
            d["note"] = self.note
        return d

    @classmethod
    def from_jsonable(cls, d: Any) -> "CausalRef":
        if not isinstance(d, dict):
            raise ValueError(f"CausalRef requires dict, got {type(d).__name__}")
        rel_raw = d.get("relation")
        try:
            relation = CausalRelation(rel_raw)
        except ValueError as exc:
            raise ValueError(f"unknown CausalRelation: {rel_raw!r}") from exc
        return cls(
            ref=EventRef.from_jsonable(d.get("ref")),
            relation=relation,
            note=d.get("note"),
        )


# ---------------------------------------------------------------------------
# Trace context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceContext:
    """Trace + span + parent-span triple for observability.

    Allocated by :func:`new_trace` at room-session start and inherited
    via :func:`child_span` when a new scope opens (lease acquisition
    in v0.3, tool call in v0.4). All three fields are 128-bit
    hex strings (``secrets.token_hex(16)``); ``parent_span_id`` is
    ``None`` at the root span.
    """

    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
        }

    @classmethod
    def from_jsonable(cls, d: Any) -> "TraceContext":
        if not isinstance(d, dict):
            raise ValueError(f"TraceContext requires dict, got {type(d).__name__}")
        trace_id = d.get("trace_id")
        span_id = d.get("span_id")
        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("TraceContext requires non-empty 'trace_id'")
        if not isinstance(span_id, str) or not span_id:
            raise ValueError("TraceContext requires non-empty 'span_id'")
        parent = d.get("parent_span_id")
        if parent is not None and not isinstance(parent, str):
            raise ValueError("TraceContext 'parent_span_id' must be str or null")
        return cls(trace_id=trace_id, span_id=span_id, parent_span_id=parent)


def _new_id() -> str:
    """Generate a 128-bit random hex id (32 chars).

    Used for both trace and span ids. ULIDs would be tidier (lex
    sortable) but the kernel has no ULID dependency.
    """
    return secrets.token_hex(16)


def new_trace() -> TraceContext:
    """Allocate a fresh root :class:`TraceContext`.

    Both ``trace_id`` and ``span_id`` are fresh; ``parent_span_id`` is
    ``None``. Used by the coordinator at room-session start.
    """
    tid = _new_id()
    sid = _new_id()
    return TraceContext(trace_id=tid, span_id=sid, parent_span_id=None)


def child_span(parent: TraceContext) -> TraceContext:
    """Return a child :class:`TraceContext` under ``parent``.

    Inherits ``trace_id``; new ``span_id``; ``parent_span_id`` is
    ``parent.span_id``.
    """
    return TraceContext(
        trace_id=parent.trace_id,
        span_id=_new_id(),
        parent_span_id=parent.span_id,
    )


# ---------------------------------------------------------------------------
# Event-envelope helpers (used by ``loom.kernel.events.Event``)
# ---------------------------------------------------------------------------


def coerce_causal_refs(value: Any) -> tuple[CausalRef, ...]:
    """Coerce a JSON-list of ref dicts into a ``tuple[CausalRef, ...]``.

    Used by :meth:`Event.__post_init__` so a freshly-loaded JSON line
    re-emerges as the typed tuple. Accepts already-typed input
    (passthrough) and dict-list input (per-element ``from_jsonable``).
    Empty input returns the empty tuple.
    """
    if not value:
        return ()
    if isinstance(value, CausalRef):
        return (value,)
    out: list[CausalRef] = []
    for item in value:
        if isinstance(item, CausalRef):
            out.append(item)
        else:
            out.append(CausalRef.from_jsonable(item))
    return tuple(out)


def coerce_trace(value: Any) -> Optional[TraceContext]:
    """Coerce dict-or-None input into ``Optional[TraceContext]``.

    Symmetric to :func:`coerce_causal_refs`. Passes through an
    already-typed :class:`TraceContext` unchanged; converts dict via
    :meth:`TraceContext.from_jsonable`; treats falsy / missing as
    ``None``.
    """
    if value is None:
        return None
    if isinstance(value, TraceContext):
        return value
    return TraceContext.from_jsonable(value)
