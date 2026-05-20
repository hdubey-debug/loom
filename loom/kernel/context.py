"""Loom v0.3.x — context-compaction subsystem types.

Doctrine: P16–P22 (`docs/internal/study/14-context-compaction-doctrine.md`).

This module is the home for the v0.3.x compaction state schema and
its pure validators. The kernel coordinator (PR 3) owns the
lifecycle; the prompt builder (PR 4) reads ``ContextState`` to
render summaries; the summarisation lease (PR 5) gates production.

PR 1 introduced :class:`ContextScope`. PR 2 (this file) adds:

- :class:`SummaryRecord` — the persisted summary payload + lineage
  metadata.
- :class:`SummaryFailureReason` — the enum surfaced by the validator
  and emitted with ``summary_failed`` events.
- :class:`ContextState` — the view-layer compaction state owned by
  :class:`loom.kernel.state.KernelState`.
- :func:`validate_lineage` — pure invariant check on a record's
  ``input_event_ranges`` (contiguity + non-overlap, range union ==
  ``covers_event_range``).
- :func:`validate_summary_record` — full off-lock pre-validator;
  composes :func:`validate_lineage` with bus-range bounds and
  retained-id-in-range checks.

Doctrine P19 binds the validators: they are structural and
deterministic (no LLM grading). The same record produces the same
``(ok, reason, detail)`` triple regardless of who calls them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence


@dataclass(frozen=True)
class ContextScope:
    """Identifies the partition a summary or context view belongs to.

    Three components per doctrine §3.1:

    - ``room_id`` — every room is its own compaction universe.
    - ``thread_id`` — ``"main"`` is the room-level thread; per-thread
      summaries (e.g. a debate sub-thread) live under their own id.
      Matches :attr:`loom.kernel.events.Event.thread_id`.
    - ``actor_id`` — reserved for per-actor compaction (v0.4+);
      ``None`` means the room/thread-wide summary.

    Hashable so it can key the ``active_summary_by_scope`` /
    ``supersession_edges`` / ``failure_count`` dicts in PR 2.
    """

    room_id: str
    thread_id: str = "main"
    actor_id: Optional[str] = None

    def as_tuple(self) -> tuple[str, str, Optional[str]]:
        """Stable tuple form, useful for json keys and log lines."""
        return (self.room_id, self.thread_id, self.actor_id)


# ---------------------------------------------------------------------------
# SummaryRecord — the persisted compaction payload
# ---------------------------------------------------------------------------


# v0.3.x PR 2 ships SummaryRecord.schema_version = 1. Future v2
# reducers (e.g. selectable input filters; doctrine §11 deferral) will
# bump it and migrate by re-emitting summary_committed events.
SUMMARY_RECORD_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SummaryRecord:
    """Lineage-preserving summary payload (doctrine §3.2).

    A single ``SummaryRecord`` is what the summariser produces and
    what the validator inspects. Its ``covers_event_range`` (inclusive)
    is the slice of the bus that this summary stands in for; its
    ``input_summary_ids`` + ``input_event_ranges`` is the lineage
    that lets a replay reconstruct the same record from the same
    inputs. The pair is structurally constrained: the union of the
    input summaries' covered ranges plus the bare ``input_event_ranges``
    must equal ``covers_event_range`` exactly (no gaps, no overlap).
    """

    summary_id: str
    scope: ContextScope
    covers_event_range: tuple[int, int]  # inclusive on both ends
    text: str
    retained_event_ids: tuple[int, ...] = ()
    input_summary_ids: tuple[str, ...] = ()
    # ``input_event_ranges`` enumerates the *bus-event* slices this
    # summary subsumes directly. For a first-generation summary,
    # this will typically be a single range equal to ``covers_event_range``.
    # For a rolling compaction, it carries only the *new* tail beyond
    # the previous summary's coverage; the previous summary's
    # coverage is reached via ``input_summary_ids``.
    input_event_ranges: tuple[tuple[int, int], ...] = ()
    model_id: str = ""
    prompt_hash: str = ""
    summarizer_id: str = ""
    proposed_at_event_id: int = -1
    committed_at_event_id: Optional[int] = None
    schema_version: int = SUMMARY_RECORD_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# SummaryFailureReason
# ---------------------------------------------------------------------------


class SummaryFailureReason(str, Enum):
    """Validator-produced (or kernel-produced) failure taxonomy.

    Doctrine §3.4. Both the off-lock pre-validator
    (:func:`validate_summary_record`) and the under-lock commit step
    (PR 3) emit one of these reasons on ``summary_failed``.
    """

    SCHEMA_ERROR = "schema_error"
    LINEAGE_GAP = "lineage_gap"
    LINEAGE_OVERLAP = "lineage_overlap"
    COVERS_RANGE_MISMATCH = "covers_range_mismatch"
    BUS_OUT_OF_RANGE = "bus_out_of_range"
    RETAINED_OUT_OF_RANGE = "retained_out_of_range"
    ANCHOR_CONFLICT = "anchor_conflict"  # set under-lock by coordinator
    CROSS_SCOPE = "cross_scope"
    INPUT_SUMMARY_UNKNOWN = "input_summary_unknown"


# ---------------------------------------------------------------------------
# ContextState
# ---------------------------------------------------------------------------


@dataclass
class ContextState:
    """View-layer compaction state owned by :class:`KernelState` (P17).

    All four maps are mutated only by the PR 3 effect reducers (which
    run under the coordinator lock). The replay-determinism contract
    is that recorded ``summary_committed`` events drive every state
    change; the validator is NEVER re-run on replay.
    """

    summaries: dict[str, SummaryRecord] = field(default_factory=dict)
    active_summary_by_scope: dict[ContextScope, str] = field(default_factory=dict)
    # ``superseded_id -> superseder_id`` — every rolling compaction
    # adds one edge.
    supersession_edges: dict[str, str] = field(default_factory=dict)
    # ``(summarizer_id, scope.as_tuple()) -> int`` — backoff counter
    # consumed by PR 5's per-scope auto-disable rule.
    failure_count: dict[tuple[str, tuple], int] = field(default_factory=dict)
    # v0.3.x PR 5 — set of ``(summarizer_id, scope.as_tuple())`` pairs
    # marked disabled for Path A scheduling. Path A
    # (``schedule_summarization``) refuses to schedule when the pair
    # is in this set. Cleared by a re-run of
    # :class:`DefaultSummarizerSetEffect` for the affected slot.
    disabled_scopes: set[tuple[str, tuple]] = field(default_factory=set)


def new_context_state() -> ContextState:
    """Construct a fresh empty :class:`ContextState`.

    Named constructor symmetry with :func:`loom.kernel.state.new_kernel_state`.
    Acts as the ``default_factory`` for ``KernelState.context``.
    """
    return ContextState()


# ---------------------------------------------------------------------------
# Validators (pure, off-lock)
# ---------------------------------------------------------------------------


def _ranges_union_equals(
    ranges: Sequence[tuple[int, int]], expected: tuple[int, int]
) -> tuple[bool, Optional[str]]:
    """Return ``(True, None)`` iff the sorted union of ``ranges`` is
    contiguous, non-overlapping, and equal to ``expected`` as a single
    inclusive span. Otherwise ``(False, reason_detail)``.

    Helper for :func:`validate_lineage`; does not classify the
    failure into a :class:`SummaryFailureReason` itself (that's the
    caller's responsibility — gap vs overlap vs mismatch).
    """
    if not ranges:
        return (False, "no input ranges")
    sorted_ranges = sorted(ranges)
    # Pairwise check: each next.start must be exactly prev.end + 1.
    for i in range(1, len(sorted_ranges)):
        prev_end = sorted_ranges[i - 1][1]
        curr_start = sorted_ranges[i][0]
        if curr_start <= prev_end:
            return (False, f"overlap at {sorted_ranges[i - 1]} / {sorted_ranges[i]}")
        if curr_start != prev_end + 1:
            return (False, f"gap between {sorted_ranges[i - 1]} and {sorted_ranges[i]}")
    lo = sorted_ranges[0][0]
    hi = sorted_ranges[-1][1]
    if (lo, hi) != expected:
        return (False, f"union {(lo, hi)} != expected {expected}")
    return (True, None)


def validate_lineage(
    record: SummaryRecord,
    *,
    input_summary_lookup: Optional[dict[str, SummaryRecord]] = None,
) -> tuple[bool, Optional[SummaryFailureReason], Optional[str]]:
    """Pure invariant check on a record's lineage shape.

    Verifies:

    - ``covers_event_range`` is a well-formed inclusive pair ``(lo, hi)``
      with ``0 <= lo <= hi``.
    - Each entry in ``input_event_ranges`` is a well-formed inclusive
      pair with ``0 <= lo <= hi``.
    - When ``input_summary_lookup`` is provided, every id in
      ``input_summary_ids`` is present (else ``INPUT_SUMMARY_UNKNOWN``)
      and shares the record's ``scope`` (else ``CROSS_SCOPE``).
    - The combined ranges (input summaries' covered range +
      ``input_event_ranges``) form a contiguous, non-overlapping
      partition equal to ``covers_event_range``.

    Returns ``(True, None, None)`` on success, otherwise
    ``(False, reason, detail)`` where ``reason`` is the most-specific
    :class:`SummaryFailureReason` for the violation and ``detail`` is
    a short string suitable for the ``summary_failed`` event body.
    """
    lo, hi = record.covers_event_range
    if not (isinstance(lo, int) and isinstance(hi, int)) or lo < 0 or hi < lo:
        return (
            False,
            SummaryFailureReason.SCHEMA_ERROR,
            f"covers_event_range invalid: {record.covers_event_range!r}",
        )
    for r in record.input_event_ranges:
        if (
            not isinstance(r, tuple)
            or len(r) != 2
            or not isinstance(r[0], int)
            or not isinstance(r[1], int)
            or r[0] < 0
            or r[1] < r[0]
        ):
            return (
                False,
                SummaryFailureReason.SCHEMA_ERROR,
                f"input_event_range invalid: {r!r}",
            )

    combined: list[tuple[int, int]] = list(record.input_event_ranges)
    if record.input_summary_ids:
        if input_summary_lookup is None:
            # Cannot validate lineage IDs without a lookup; defer.
            return (True, None, None)
        for sid in record.input_summary_ids:
            parent = input_summary_lookup.get(sid)
            if parent is None:
                return (
                    False,
                    SummaryFailureReason.INPUT_SUMMARY_UNKNOWN,
                    f"input_summary_id {sid!r} not in state",
                )
            if parent.scope != record.scope:
                return (
                    False,
                    SummaryFailureReason.CROSS_SCOPE,
                    f"input summary {sid!r} scope {parent.scope!r} != record scope {record.scope!r}",
                )
            combined.append(parent.covers_event_range)

    ok, detail = _ranges_union_equals(combined, record.covers_event_range)
    if not ok:
        assert detail is not None
        if "overlap" in detail:
            return (False, SummaryFailureReason.LINEAGE_OVERLAP, detail)
        if "gap" in detail:
            return (False, SummaryFailureReason.LINEAGE_GAP, detail)
        return (False, SummaryFailureReason.COVERS_RANGE_MISMATCH, detail)
    return (True, None, None)


def validate_summary_record(
    record: SummaryRecord,
    *,
    bus_length: int,
    input_summary_lookup: Optional[dict[str, SummaryRecord]] = None,
) -> tuple[bool, Optional[SummaryFailureReason], Optional[str]]:
    """Off-lock structural pre-validator (doctrine P19, §6).

    Composes :func:`validate_lineage` with two boundary checks:

    - ``covers_event_range`` must lie entirely within the bus
      (``hi < bus_length``); otherwise ``BUS_OUT_OF_RANGE``.
    - Every id in ``retained_event_ids`` must be within
      ``covers_event_range``; otherwise ``RETAINED_OUT_OF_RANGE``.

    Returns the same ``(ok, reason, detail)`` shape as
    :func:`validate_lineage`. Pure: no I/O, no lock, deterministic in
    its three arguments.
    """
    ok, reason, detail = validate_lineage(
        record, input_summary_lookup=input_summary_lookup
    )
    if not ok:
        return (ok, reason, detail)

    lo, hi = record.covers_event_range
    if hi >= bus_length:
        return (
            False,
            SummaryFailureReason.BUS_OUT_OF_RANGE,
            f"covers_event_range hi={hi} >= bus_length={bus_length}",
        )

    for rid in record.retained_event_ids:
        if not isinstance(rid, int) or rid < lo or rid > hi:
            return (
                False,
                SummaryFailureReason.RETAINED_OUT_OF_RANGE,
                f"retained id {rid!r} not in covers {(lo, hi)}",
            )

    return (True, None, None)


# ---------------------------------------------------------------------------
# Serialisation helpers (consumed by journal.py in PR 2)
# ---------------------------------------------------------------------------


def _scope_to_jsonable(scope: ContextScope) -> dict:
    return {
        "room_id": scope.room_id,
        "thread_id": scope.thread_id,
        "actor_id": scope.actor_id,
    }


def _scope_from_jsonable(d: Any) -> Optional[ContextScope]:
    if not isinstance(d, dict):
        return None
    room_id = d.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        return None
    thread_id = d.get("thread_id", "main")
    if not isinstance(thread_id, str) or not thread_id:
        thread_id = "main"
    actor_id = d.get("actor_id")
    if actor_id is not None and not isinstance(actor_id, str):
        actor_id = None
    return ContextScope(room_id=room_id, thread_id=thread_id, actor_id=actor_id)


def summary_record_to_jsonable(record: SummaryRecord) -> dict:
    return {
        "summary_id": record.summary_id,
        "scope": _scope_to_jsonable(record.scope),
        "covers_event_range": list(record.covers_event_range),
        "text": record.text,
        "retained_event_ids": list(record.retained_event_ids),
        "input_summary_ids": list(record.input_summary_ids),
        "input_event_ranges": [list(r) for r in record.input_event_ranges],
        "model_id": record.model_id,
        "prompt_hash": record.prompt_hash,
        "summarizer_id": record.summarizer_id,
        "proposed_at_event_id": record.proposed_at_event_id,
        "committed_at_event_id": record.committed_at_event_id,
        "schema_version": record.schema_version,
    }


def summary_record_from_jsonable(d: Any) -> Optional[SummaryRecord]:
    if not isinstance(d, dict):
        return None
    sid = d.get("summary_id")
    if not isinstance(sid, str) or not sid:
        return None
    scope = _scope_from_jsonable(d.get("scope"))
    if scope is None:
        return None
    cer = d.get("covers_event_range")
    if not (
        isinstance(cer, list)
        and len(cer) == 2
        and isinstance(cer[0], int)
        and isinstance(cer[1], int)
    ):
        return None
    retained = d.get("retained_event_ids", []) or []
    input_ids = d.get("input_summary_ids", []) or []
    raw_ranges = d.get("input_event_ranges", []) or []
    input_ranges: list[tuple[int, int]] = []
    for r in raw_ranges:
        if (
            isinstance(r, list)
            and len(r) == 2
            and isinstance(r[0], int)
            and isinstance(r[1], int)
        ):
            input_ranges.append((r[0], r[1]))
    return SummaryRecord(
        summary_id=sid,
        scope=scope,
        covers_event_range=(cer[0], cer[1]),
        text=d.get("text", "") or "",
        retained_event_ids=tuple(int(x) for x in retained if isinstance(x, int)),
        input_summary_ids=tuple(str(x) for x in input_ids if isinstance(x, str)),
        input_event_ranges=tuple(input_ranges),
        model_id=d.get("model_id", "") or "",
        prompt_hash=d.get("prompt_hash", "") or "",
        summarizer_id=d.get("summarizer_id", "") or "",
        proposed_at_event_id=int(d.get("proposed_at_event_id", -1)),
        committed_at_event_id=d.get("committed_at_event_id"),
        schema_version=int(d.get("schema_version", SUMMARY_RECORD_SCHEMA_VERSION)),
    )


def context_state_to_jsonable(state: ContextState) -> dict:
    """Serialise a :class:`ContextState` to a JSON-able dict.

    Used by :meth:`Journal._state_to_dict` to nest compaction state
    in the v7 snapshot envelope. The scope keys are flattened to
    ``[room_id, thread_id, actor_id]`` tuples because JSON object
    keys must be strings; restore inverts via ``_scope_from_jsonable``
    on the parallel list of scopes.
    """
    # ``active_summary_by_scope`` and ``failure_count`` use composite
    # keys; serialise as parallel lists.
    return {
        "summaries": {
            sid: summary_record_to_jsonable(rec) for sid, rec in state.summaries.items()
        },
        "active_summary_by_scope": [
            {"scope": _scope_to_jsonable(scope), "summary_id": sid}
            for scope, sid in state.active_summary_by_scope.items()
        ],
        "supersession_edges": dict(state.supersession_edges),
        "failure_count": [
            {
                "summarizer_id": key[0],
                "scope": list(key[1]),
                "count": count,
            }
            for key, count in state.failure_count.items()
        ],
        "disabled_scopes": [
            {"summarizer_id": key[0], "scope": list(key[1])}
            for key in state.disabled_scopes
        ],
    }


# ---------------------------------------------------------------------------
# Pressure estimator (v0.3.x PR 4)
# ---------------------------------------------------------------------------


# Approximate token cost per character. A coarse 4-char ≈ 1-token
# heuristic is good enough for "should we compact" decisions; we
# explicitly do NOT want to depend on a model-specific tokenizer at
# this layer (the kernel doesn't pick the model).
_CHARS_PER_TOKEN_ESTIMATE = 4.0


# Default max-context budget used when a caller doesn't supply one.
# 200k tokens reflects current Claude / GPT long-context defaults;
# Path A consumers can override per call.
_DEFAULT_MAX_CONTEXT_TOKENS = 200_000


@dataclass(frozen=True)
class ContextPressure:
    """Result of :func:`estimate_context_pressure` (doctrine §10).

    Pure data — no I/O references. Path A consumers read
    ``needs_compaction`` to decide whether to schedule a summarisation
    lease; ``suggested_compaction_range`` is a coarse hint for what
    to compact, computed from the active summary's end (if any) plus
    a window of new events.
    """

    estimated_tokens: int
    max_context_tokens: int
    threshold: float
    pressure_ratio: float
    needs_compaction: bool
    suggested_compaction_range: Optional[tuple[int, int]] = None


@dataclass(frozen=True)
class _PressureCacheKey:
    """4-component cache key (doctrine §10): participant_id, scope,
    kernel_state_version, prompt_template_hash.

    Hashable, so it can sit in a stdlib ``functools.lru_cache``
    indirectly via the dict-cached :func:`estimate_context_pressure`
    helper below.
    """

    participant_id: str
    scope: ContextScope
    kernel_state_version: int
    prompt_template_hash: str


# Module-local cache. Bounded by participant × scope × version × template
# — in practice this remains small (per-room sessions don't accumulate
# millions of versions). PR 5's auto-compaction policy reuses this.
_PRESSURE_CACHE: dict[_PressureCacheKey, ContextPressure] = {}
_PRESSURE_CACHE_MAX = 1024


def _evict_pressure_cache_if_full() -> None:
    """Coarse FIFO eviction; preserves cache hits for the most-recent
    versions. Called from :func:`estimate_context_pressure` before
    insert; sufficient for a low-traffic cache that just guards
    against unbounded growth on long sessions.
    """
    if len(_PRESSURE_CACHE) >= _PRESSURE_CACHE_MAX:
        # Drop oldest 25% — dict insertion order preserves chronology
        # in CPython 3.7+, and this is faster than a strict LRU update
        # on every read.
        n_drop = _PRESSURE_CACHE_MAX // 4
        for k in list(_PRESSURE_CACHE.keys())[:n_drop]:
            del _PRESSURE_CACHE[k]


def select_compaction_range(
    state: ContextState,
    scope: ContextScope,
    *,
    bus_length: int,
    min_events: int = 10,
) -> tuple[int, int]:
    """Suggest a ``(lo, hi)`` event id range to compact (doctrine §10).

    Strategy:

    - If there's no active summary for ``scope``, recommend the full
      bus range ``(0, bus_length - 1)``.
    - If there's an active summary covering ``(_, end)``, recommend
      ``(end + 1, bus_length - 1)`` so the new summary is a rolling
      extension.

    Returns a degenerate ``(0, -1)`` range when ``bus_length`` is too
    small for ``min_events`` worth of fresh content; callers check
    ``hi < lo`` and skip scheduling.
    """
    if bus_length <= 0:
        return (0, -1)
    active_id = state.active_summary_by_scope.get(scope)
    if active_id is None:
        lo = 0
    else:
        active = state.summaries.get(active_id)
        if active is None:
            lo = 0
        else:
            lo = active.covers_event_range[1] + 1
    hi = bus_length - 1
    if hi - lo + 1 < min_events:
        return (lo, lo - 1)
    return (lo, hi)


def estimate_context_pressure(
    *,
    participant_id: str,
    scope: ContextScope,
    kernel_state_version: int,
    prompt_template_hash: str,
    estimated_prompt_chars: int,
    max_context_tokens: int = _DEFAULT_MAX_CONTEXT_TOKENS,
    threshold_ratio: float = 0.7,
    context_state: Optional[ContextState] = None,
    bus_length: int = 0,
) -> ContextPressure:
    """Estimate the prompt-budget pressure for an actor (doctrine §10).

    Pure function in its arguments; results are cached by the
    4-component key ``(participant_id, scope, kernel_state_version,
    prompt_template_hash)``. A version bump invalidates the entry
    automatically (the key changes); a prompt-template change
    invalidates similarly.

    ``estimated_prompt_chars`` is the caller's measurement of the
    rendered prompt (or its projection); the estimator converts to
    tokens via the conservative :data:`_CHARS_PER_TOKEN_ESTIMATE`
    constant. The cache lets PR 5's policy hook poll cheaply without
    re-measuring on every actor turn.
    """
    key = _PressureCacheKey(
        participant_id=participant_id,
        scope=scope,
        kernel_state_version=kernel_state_version,
        prompt_template_hash=prompt_template_hash,
    )
    cached = _PRESSURE_CACHE.get(key)
    if cached is not None:
        return cached

    estimated_tokens = max(0, int(estimated_prompt_chars / _CHARS_PER_TOKEN_ESTIMATE))
    if max_context_tokens <= 0:
        pressure_ratio = 0.0
    else:
        pressure_ratio = estimated_tokens / max_context_tokens
    needs = pressure_ratio >= threshold_ratio

    suggested = None
    if needs and context_state is not None and bus_length > 0:
        suggested = select_compaction_range(
            context_state, scope, bus_length=bus_length
        )

    out = ContextPressure(
        estimated_tokens=estimated_tokens,
        max_context_tokens=max_context_tokens,
        threshold=threshold_ratio,
        pressure_ratio=pressure_ratio,
        needs_compaction=needs,
        suggested_compaction_range=suggested,
    )
    _evict_pressure_cache_if_full()
    _PRESSURE_CACHE[key] = out
    return out


def context_state_from_jsonable(d: Any) -> ContextState:
    """Restore a :class:`ContextState` from its JSON-able dict shape.

    Returns an empty state on ``None`` / malformed input — callers
    rebuild from the bus via PR 3's reducer replay rather than
    crashing on a tampered snapshot.
    """
    state = ContextState()
    if not isinstance(d, dict):
        return state
    raw_summaries = d.get("summaries")
    if isinstance(raw_summaries, dict):
        for sid, rec_dict in raw_summaries.items():
            rec = summary_record_from_jsonable(rec_dict)
            if rec is not None:
                state.summaries[str(sid)] = rec
    raw_active = d.get("active_summary_by_scope")
    if isinstance(raw_active, list):
        for entry in raw_active:
            if not isinstance(entry, dict):
                continue
            scope = _scope_from_jsonable(entry.get("scope"))
            sid = entry.get("summary_id")
            if scope is not None and isinstance(sid, str) and sid:
                state.active_summary_by_scope[scope] = sid
    raw_edges = d.get("supersession_edges")
    if isinstance(raw_edges, dict):
        for k, v in raw_edges.items():
            if isinstance(k, str) and isinstance(v, str):
                state.supersession_edges[k] = v
    raw_failures = d.get("failure_count")
    if isinstance(raw_failures, list):
        for entry in raw_failures:
            if not isinstance(entry, dict):
                continue
            sumid = entry.get("summarizer_id")
            scope_list = entry.get("scope")
            count = entry.get("count")
            if (
                isinstance(sumid, str)
                and isinstance(scope_list, list)
                and len(scope_list) == 3
                and isinstance(count, int)
            ):
                key = (sumid, tuple(scope_list))
                state.failure_count[key] = count
    raw_disabled = d.get("disabled_scopes")
    if isinstance(raw_disabled, list):
        for entry in raw_disabled:
            if not isinstance(entry, dict):
                continue
            sumid = entry.get("summarizer_id")
            scope_list = entry.get("scope")
            if (
                isinstance(sumid, str)
                and isinstance(scope_list, list)
                and len(scope_list) == 3
            ):
                state.disabled_scopes.add((sumid, tuple(scope_list)))
    return state
