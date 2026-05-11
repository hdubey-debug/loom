"""Loom v0 — Event schema.

All events flow through the :class:`MessageBus` and are journaled
append-only. Events are typed by ``kind``:

- ``chat``     Final committed participant reply (visible content).
- ``control``  State transitions (topic, participants, obligations, etc.).
- ``stream``   Render hints (start, delta, end). Also journaled for replay.
- ``system``   Boot/shutdown notices, errors not tied to a stream.
- ``topic``    Topic announcement.
- ``presence`` Ephemeral typing/idle indicators (not required v0).
- ``summary``  Compaction output (canonical main_summary).

The bus assigns ``id`` and ``ts`` on post; constructors leave them at zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    Literal,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)
import json
import re

# ``orjson`` is an optional perf extra (``pip install -e .[perf]``). When
# present, ``Event.to_jsonl`` uses it for ~5-10x faster serialization;
# otherwise we fall back to the stdlib ``json`` module. Both produce
# byte-equivalent output for the values we serialize (Event payloads
# only contain str/int/float/bool/None/list/dict). Tests round-trip
# fine in either mode.
try:  # pragma: no cover - optional dep
    import orjson as _orjson  # type: ignore[import-not-found,unused-ignore]
except ImportError:  # pragma: no cover
    _orjson = None  # type: ignore[assignment]


EventKind = Literal[
    "chat",
    "control",
    "stream",
    "system",
    "topic",
    "presence",
    "summary",
]


_VALID_KINDS = frozenset(
    {
        "chat",
        "control",
        "stream",
        "system",
        "topic",
        "presence",
        "summary",
    }
)


from loom.errors import LoomError  # noqa: E402  (kept here to avoid circular import at module-load)


class EventShapeError(LoomError, ValueError):
    """Raised by :meth:`Event.from_jsonl` when a JSON line's shape is
    incompatible with kernel per-kind invariants.

    Downstream consumers do shape-dependent reads on ``Event.body``
    (``body.startswith(...)`` for chat, ``body.get(...)`` for control /
    stream) without type-guarding. A tampered or corrupt journal line
    that produces a wrong-shape body would crash an actor or coordinator
    thread on first read. ``from_jsonl`` raises this error early so
    callers (the journal replay path in particular) can quarantine the
    line rather than let it propagate.

    Inherits from :class:`loom.errors.LoomError` so user code can catch
    via ``except LoomError``, and from :class:`ValueError` for back-compat.
    """


# ---------------------------------------------------------------------------
# Secret redaction (P0.7 — scrubs error-event bodies before they reach the
# bus / journal / subscribers). Provider exception strings are known to
# include API-key fragments and request payloads; the kernel boundary
# applies a defensive scrub even before adapter-specific scrubbers run.
#
# v0.2: the seven default patterns are now first-class ``SecretShape``
# objects (named structural detectors) rather than an anonymous tuple
# of regexes. The shape framework lets adapters add new detectors by
# name without monkey-patching the kernel module, and surfaces what
# class of secret was found at the call site that emits the
# redaction placeholder (useful for future audit logging).
# ---------------------------------------------------------------------------

_REDACT_PLACEHOLDER = "[redacted-secret]"


@runtime_checkable
class SecretShape(Protocol):
    """A named structural detector for a class of secret.

    Implementations expose a short identifier and a ``detect`` method
    returning the half-open ``(start, end)`` byte offsets of every
    occurrence in ``text``. They never raise — a buggy detector must
    never break the error-emission path — and they return matches in
    increasing start-offset order. Overlapping matches across
    different shapes are merged by :func:`redact_error_text`.
    """

    name: str

    def detect(self, text: str) -> Iterable[Tuple[int, int]]: ...


@dataclass(frozen=True)
class _RegexShape:
    """A :class:`SecretShape` backed by a single :class:`re.Pattern`."""

    name: str
    pattern: "re.Pattern[str]"

    def detect(self, text: str) -> Iterator[Tuple[int, int]]:
        for match in self.pattern.finditer(text):
            yield match.span()


_DEFAULT_SHAPES: "tuple[SecretShape, ...]" = (  # type: ignore[assignment]
    # Anthropic explicit ``sk-ant-`` prefix; listed before the generic
    # ``sk-`` shape so it wins span-merging when both match.
    _RegexShape("anthropic_sk_ant", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    # OpenAI / Anthropic legacy ``sk-`` prefix.
    _RegexShape("openai_sk", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    # OAuth / generic Bearer tokens. Real tokens are >=16 non-space chars
    # so ``Bearer foo`` doesn't trip the detector.
    _RegexShape("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    # AWS access key id (AKIA + 16 uppercase alphanumerics).
    _RegexShape("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    # JWT (header.payload.signature) — three base64url segments.
    _RegexShape("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")),
    # Google API key (AIza + 35 chars).
    _RegexShape("gcp_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    # Google OAuth access token (ya29. + 20+ chars).
    _RegexShape("gcp_oauth", re.compile(r"ya29\.[A-Za-z0-9_-]{20,}")),
)

_ADAPTER_SHAPES: list[SecretShape] = []
_ADAPTER_SCRUBBERS: list[Callable[[str], str]] = []


def register_secret_shape(shape: SecretShape) -> None:
    """Register a new structural :class:`SecretShape` detector.

    Detectors are applied in addition to the kernel's defaults BEFORE
    any legacy scrubber callable (see :func:`register_secret_scrubber`).
    Registering the same shape instance twice is a no-op. Useful for
    adapters that recognize a provider-specific token shape and want
    structural detection rather than ad-hoc string replacement.
    """
    if shape in _ADAPTER_SHAPES:
        return
    _ADAPTER_SHAPES.append(shape)


def register_secret_scrubber(scrubber: Callable[[str], str]) -> None:
    """Register an adapter-specific secret scrubber (legacy API).

    ``scrubber`` takes a string and returns the same string with any
    provider-specific secret shapes replaced by a placeholder.
    Scrubbers are applied in registration order AFTER all
    :class:`SecretShape` detectors. Idempotent: registering the same
    callable twice is a no-op.

    Adapters call this at module import time so the kernel default
    redactor (:func:`redact_error_text`) picks them up automatically.
    A buggy scrubber that raises is silently skipped — no error path
    must be allowed to break error-event emission. Prefer
    :func:`register_secret_shape` for new code.
    """
    if scrubber in _ADAPTER_SCRUBBERS:
        return
    _ADAPTER_SCRUBBERS.append(scrubber)


def clear_secret_scrubbers() -> None:
    """Remove all adapter-installed scrubbers + shapes. Test-only convenience."""
    _ADAPTER_SCRUBBERS.clear()
    _ADAPTER_SHAPES.clear()


def _collect_spans(text: str, shapes: Iterable[SecretShape]) -> list[Tuple[int, int]]:
    """Collect non-overlapping ``(start, end)`` spans across detectors.

    Detectors that raise are skipped (errors must never break the
    error-emission path). Overlapping or adjacent spans are merged so
    each replacement renders a single ``[redacted-secret]`` placeholder
    regardless of how many shapes matched the same text.
    """
    spans: list[Tuple[int, int]] = []
    for shape in shapes:
        try:
            for span in shape.detect(text):
                start, end = span
                if 0 <= start < end <= len(text):
                    spans.append((start, end))
        except Exception:
            continue
    if not spans:
        return spans
    spans.sort()
    merged: list[Tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def redact_error_text(s: Any, *, max_chars: int = 500) -> str:
    """Trim and scrub an error-event body string.

    (a) Hard length cap (default 500 chars). (b) Kernel default
    :class:`SecretShape` detectors plus any adapter-registered shapes
    (see :func:`register_secret_shape`). (c) Legacy adapter-installed
    scrubber callables (see :func:`register_secret_scrubber`).

    Applied at the kernel boundary in :func:`stream_end`,
    :func:`actor_error`, and :func:`journal_error` so a leaky adapter
    exception is partially defended even before adapter-specific
    scrubbers run. ``None`` and empty strings pass through as ``""``.
    Non-string inputs are coerced via ``str(...)`` so a stray exception
    object cannot fall through.
    """
    if not s:
        return ""
    text = s if isinstance(s, str) else str(s)
    spans = _collect_spans(text, tuple(_DEFAULT_SHAPES) + tuple(_ADAPTER_SHAPES))
    if spans:
        parts: list[str] = []
        cursor = 0
        for start, end in spans:
            parts.append(text[cursor:start])
            parts.append(_REDACT_PLACEHOLDER)
            cursor = end
        parts.append(text[cursor:])
        text = "".join(parts)
    for scrubber in _ADAPTER_SCRUBBERS:
        try:
            text = scrubber(text)
        except Exception:
            # A buggy scrubber must not break error-path emission. The
            # shape detectors have already run, so partial scrubbing
            # is preserved.
            continue
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"  # ellipsis
    return text


@dataclass(slots=True)
class Event:
    """Append-only event record (kernel-internal).

    The canonical construction path is the per-kind factory functions
    defined later in this module — :func:`chat`, :func:`system`,
    :func:`summary`, the various ``_control`` helpers — not direct
    ``Event(...)`` calls. The factories enforce the per-kind body
    shape that :func:`_validate_body_for_kind` later relies on; a raw
    constructor call can produce a malformed ``Event`` that the
    journal will quarantine on replay.

    Library authors interacting with replies see the
    :class:`loom.messages.Message` projection, not raw events. ``Event``
    itself is reachable but advanced — direct use is reserved for
    journal / bus / replay code.

    ``slots=True`` shrinks the per-instance footprint by ~30% and makes
    attribute access ~5-10% faster. We cannot use ``frozen=True`` on
    top of slots because :class:`MessageBus` mutates ``id`` and ``ts``
    after construction (see ``bus.post``).
    """

    kind: EventKind
    sender: str  # participant id, "user", or "system"
    body: Any  # str for chat; dict for control/stream
    channel: str = "main"  # "main" | "dm:<id>"
    addressees: list[str] = field(default_factory=list)
    room_epoch: int = 0
    user_turn_id: Optional[int] = None
    meta: dict = field(default_factory=dict)
    id: int = 0  # assigned by bus
    # ``ts`` is wall-clock (``time.time``) — assigned by the bus on
    # post and used only for human-readable journal lines and replay
    # correlation. Duration math (idle timeouts, debounce, throttle
    # windows, lease bookkeeping) uses ``time.monotonic`` instead, so
    # an NTP step does not warp those windows. Don't compare ``ts``
    # against ``time.monotonic`` values.
    ts: float = 0.0  # epoch seconds; assigned by bus

    def to_jsonl(self) -> str:
        # Direct field-access dict construction (avoids ``asdict``'s
        # deepcopy of every nested list/dict). Field order matches the
        # dataclass declaration so ``from_jsonl(to_jsonl(e)) == e``
        # round-trips bit-stably across releases.
        d = {
            "kind": self.kind,
            "sender": self.sender,
            "body": self.body,
            "channel": self.channel,
            "addressees": self.addressees,
            "room_epoch": self.room_epoch,
            "user_turn_id": self.user_turn_id,
            "meta": self.meta,
            "id": self.id,
            "ts": self.ts,
        }
        if _orjson is not None:
            return _orjson.dumps(d).decode("utf-8")
        return json.dumps(d, separators=(",", ":"), default=str)

    @classmethod
    def from_jsonl(cls, line: str) -> "Event":
        """Parse a JSON line into an :class:`Event`.

        Validates per-kind body shape and field types so a tampered or
        truncated journal line cannot crash a downstream consumer that
        does ``body.startswith(...)`` / ``body.get(...)`` without an
        ``isinstance`` guard.

        Raises :class:`EventShapeError` on a malformed line (invalid
        JSON, wrong field types, or mismatched body shape for the
        declared ``kind``). The journal's replay path catches this
        explicitly and surfaces a ``journal_corruption`` control event
        rather than letting the corrupted line propagate.
        """
        try:
            d = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EventShapeError(f"invalid json: {exc}") from exc
        _validate_event_dict(d)
        # Filter to the dataclass-declared field set so a tampered line
        # with extra unknown keys (a future field name, or random bytes
        # that JSON-parsed by chance) raises EventShapeError above
        # rather than TypeError'ing out of cls.__init__.
        return cls(**{k: v for k, v in d.items() if k in _EVENT_FIELDS})


# ---------------------------------------------------------------------------
# Shape validation (used by Event.from_jsonl)
# ---------------------------------------------------------------------------

_EVENT_FIELDS = frozenset(
    {
        "kind",
        "sender",
        "body",
        "channel",
        "addressees",
        "room_epoch",
        "user_turn_id",
        "meta",
        "id",
        "ts",
    }
)


def _is_int(v: Any) -> bool:
    """``isinstance(v, int)`` minus the bool subclass surprise.

    JSON ``true``/``false`` parses to Python ``True``/``False``, both
    of which are ``int`` subclasses. A tampered line with ``"id": true``
    must not slip through an int check.
    """
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_body_for_kind(kind: str, body: Any) -> None:
    """Per-kind body shape check. Caller has already validated ``kind``."""
    if kind in ("chat", "system", "summary", "topic"):
        if not isinstance(body, str):
            raise EventShapeError(f"{kind} body must be str, got {type(body).__name__}")
        return
    if kind == "control":
        if not isinstance(body, dict):
            raise EventShapeError(f"control body must be dict, got {type(body).__name__}")
        ct = body.get("control_type")
        if not isinstance(ct, str) or not ct:
            raise EventShapeError("control body must contain a non-empty 'control_type' string")
        return
    if kind == "stream":
        if not isinstance(body, dict):
            raise EventShapeError(f"stream body must be dict, got {type(body).__name__}")
        se = body.get("stream_event")
        if se not in ("start", "delta", "end"):
            raise EventShapeError(
                f"stream body must have stream_event in {{start,delta,end}}, got {se!r}"
            )
        if not _is_int(body.get("lease_id")):
            raise EventShapeError("stream body must have int 'lease_id'")
        return
    # ``presence`` is intentionally permissive — its shape is unspecified
    # in v0 and no consumer reads its body today.


def _validate_event_dict(d: Any) -> None:
    """Validate the JSON-parsed dict before constructing an :class:`Event`.

    Tight enough to reject the corruption shapes that crash downstream
    consumers (T1); lenient enough that legitimate round-trips of
    every existing factory output pass cleanly.
    """
    if not isinstance(d, dict):
        raise EventShapeError(f"event must be JSON object, got {type(d).__name__}")

    kind = d.get("kind")
    # ``in`` on a frozenset raises TypeError for unhashable values
    # (list, dict, set). Type-check first so a tampered ``kind: []`` is
    # rejected by EventShapeError, not by a bare TypeError that would
    # crash an actor's wakeup loop.
    if not isinstance(kind, str) or kind not in _VALID_KINDS:
        raise EventShapeError(f"unknown event kind: {kind!r}")

    sender = d.get("sender")
    if not isinstance(sender, str):
        raise EventShapeError(f"sender must be str, got {type(sender).__name__}")

    channel = d.get("channel", "main")
    if not isinstance(channel, str):
        raise EventShapeError(f"channel must be str, got {type(channel).__name__}")

    addressees = d.get("addressees", [])
    if not isinstance(addressees, list):
        raise EventShapeError(f"addressees must be list, got {type(addressees).__name__}")
    if not all(isinstance(a, str) for a in addressees):
        raise EventShapeError("addressees must be a list of str")

    if not _is_int(d.get("room_epoch", 0)):
        raise EventShapeError("room_epoch must be int")

    user_turn_id = d.get("user_turn_id")
    if user_turn_id is not None and not _is_int(user_turn_id):
        raise EventShapeError("user_turn_id must be int or null")

    if not isinstance(d.get("meta", {}), dict):
        raise EventShapeError("meta must be a JSON object")

    if not _is_int(d.get("id", 0)):
        raise EventShapeError("id must be int")

    if not _is_number(d.get("ts", 0.0)):
        raise EventShapeError("ts must be a number")

    _validate_body_for_kind(kind, d.get("body"))


# ---------------------------------------------------------------------------
# Control events
# ---------------------------------------------------------------------------

# Required v0 control_types. The optional tail (chair_changed /
# anchor_changed / default_summarizer_changed) is allowed but not required
# to be emitted in v0. Mode/debate control types are gone — Loom v0 is a
# single group-chat protocol with obligation-based routing.
CONTROL_TYPES = frozenset(
    {
        "topic_changed",
        "participant_added",
        "participant_removed",
        "user_turn_opened",
        "user_turn_closed",
        "obligation_recorded",
        "obligation_resolved",
        "dead_letter",
        "default_responder_changed",
        "chair_changed",
        "anchor_changed",
        "default_summarizer_changed",
        "roles_assigned",
        "floor_updated",
        "style_changed",
        # Policy watchdog (kernel-emitted, not policy-emitted) — observability
        # for the kernel↔policy contract. ``policy_slow`` fires when a
        # policy's ``plan_user_turn`` exceeds ~100ms; ``policy_error`` fires
        # on a raised exception, then the coordinator dispatches on
        # ``policy_error_mode``.
        "policy_slow",
        "policy_error",
        # Journal degraded — fired the first time an events.jsonl write
        # fails. The room keeps running but the on-disk audit trail is
        # incomplete; downstream replay/resume cannot be trusted.
        "journal_error",
        # Actor loop error — fired when a participant's actor thread caught
        # an exception while running ``step()``. The thread continues; the
        # event surfaces the failure for diagnosis.
        "actor_error",
        # Journal-line tampering / parse failure mid-stream. Surfaces the
        # offending line offset and a short raw-bytes excerpt so operators
        # can locate the corrupt entry; emitted by ``Journal.iter_events``
        # at replay time when ``Event.from_jsonl`` raises ``EventShapeError``.
        "journal_corruption",
        # Journal final-line truncation. Emitted when the LAST line of
        # ``events.jsonl`` is non-empty but unparseable — almost always
        # means an interrupted write at crash. The room can keep running;
        # the last few seconds of state are lost.
        "journal_truncated",
        # Snapshot queue overflow (P2.3 / audit RES3). Emitted when the
        # journal's bounded background-write queue dropped a snapshot
        # because the disk couldn't keep up. The on-disk room_state.json
        # remains coherent (atomic-rename); only the most-stale queued
        # snapshot was discarded. Surfaces as a degraded-mode signal.
        "snapshot_dropped",
        # Lease-grant rejection (v0.2). Emitted when a lease-check chain
        # rejects an :meth:`RoomCoordinator.acquire_lease` request. Carries
        # ``holder``, ``check_name`` (the failing :class:`LeaseCheck`),
        # ``deny_reason`` (short structured string), and
        # ``trigger_event_id`` for observability.
        "lease_denied",
    }
)


def _control(control_type: str, **payload: Any) -> Event:
    if control_type not in CONTROL_TYPES:
        raise ValueError(f"unknown control_type: {control_type!r}")
    return Event(
        kind="control",
        sender="system",
        body={"control_type": control_type, **payload},
    )


def topic_changed(old: Optional[str], new: str) -> Event:
    return _control("topic_changed", old=old, new=new)


def participant_added(participant_id: str, role_hints: Optional[dict] = None) -> Event:
    return _control("participant_added", id=participant_id, role_hints=role_hints or {})


def participant_removed(participant_id: str) -> Event:
    return _control("participant_removed", id=participant_id)


def user_turn_opened(
    user_turn_id: int,
    *,
    routing_case: str,
    required_participants: list[str],
    optional_participants: Optional[list[str]] = None,
    rationale: str = "",
) -> Event:
    """Emit when a UserTurn opens.

    ``routing_case`` is the interpreter's classification (e.g.
    ``"direct_mention"``, ``"question"``, ``"challenge"``, ``"followup"``,
    ``"multi_opinion"``, ``"none"``). ``required_participants`` lists ids
    that hold a ``must`` obligation; ``optional_participants`` (if any)
    lists ids that may but need not respond. ``rationale`` is a short
    debug string explaining the classification.
    """
    return _control(
        "user_turn_opened",
        user_turn_id=user_turn_id,
        routing_case=routing_case,
        required_participants=list(required_participants),
        optional_participants=list(optional_participants or []),
        rationale=rationale,
    )


UserTurnCloseReason = Literal[
    "completed",
    "idle_timeout",
    "new_user_post",
    "cancelled",
    "topic_changed",
    "no_responder",
    "obligation_unresolved",
]


def user_turn_closed(user_turn_id: int, reason: UserTurnCloseReason) -> Event:
    return _control("user_turn_closed", user_turn_id=user_turn_id, reason=reason)


ObligationLevel = Literal["may", "should", "must"]


def obligation_recorded(
    obligation_id: int,
    participant_id: str,
    level: ObligationLevel,
    target_event_ids: list[int],
    reason: str,
) -> Event:
    """Emit when the interpreter assigns a response obligation.

    ``target_event_ids`` lists the user-event ids the obligation answers
    to (typically the single triggering user event, but multi-mention
    classifications may target multiple).
    """
    return _control(
        "obligation_recorded",
        obligation_id=obligation_id,
        participant_id=participant_id,
        level=level,
        target_event_ids=list(target_event_ids),
        reason=reason,
    )


def obligation_resolved(
    obligation_id: int, participant_id: str, resolved_by_event_id: Optional[int]
) -> Event:
    """Emit when an obligation is satisfied.

    ``resolved_by_event_id`` points at the committed chat event that
    discharged it; ``None`` is used for administrative resolutions
    (``/cancel``, room reset, etc.).
    """
    return _control(
        "obligation_resolved",
        obligation_id=obligation_id,
        participant_id=participant_id,
        resolved_by_event_id=resolved_by_event_id,
    )


def dead_letter(
    original_mention_event_id: int, reason: str, reroute_to: Optional[str] = None
) -> Event:
    return _control(
        "dead_letter",
        original_mention_event_id=original_mention_event_id,
        reroute_to=reroute_to,
        reason=reason,
    )


def default_responder_changed(old_id: Optional[str], new_id: Optional[str]) -> Event:
    return _control("default_responder_changed", old_id=old_id, new_id=new_id)


def roles_assigned(roles: dict[str, str]) -> Event:
    """Emit when the room's task-role assignments change.

    ``roles`` is the *new* full mapping of participant id to role label
    (e.g. ``{"gemini": "teacher", "claude_code": "quizzer"}``); an empty
    dict means roles were cleared. Roles drive the per-turn TurnCard
    rendered into the selected speaker's prompt.
    """
    return _control("roles_assigned", roles=dict(roles))


def floor_updated(*, wait_for_user: Optional[bool] = None) -> Event:
    """Emit when the cross-turn ``wait_for_user`` flag changes.

    Event name preserved for journal back-compat with v0.1.2. Older
    journal lines that include a ``floor_owner`` body field replay
    cleanly — :func:`is_known_control` accepts the name and the
    coordinator silently ignores the removed field (v0.2 dropped
    ``floor_owner`` from kernel state).

    P2.3: ``active_goal`` parameter removed (merged into ``topic``).
    Topic changes flow through the ``topic_changed`` event instead.
    """
    payload: dict = {}
    if wait_for_user is not None:
        payload["wait_for_user"] = bool(wait_for_user)
    return _control("floor_updated", **payload)


def style_changed(old: str, new: str) -> Event:
    return _control("style_changed", old=old, new=new)


def lease_denied(*, holder: str, check_name: str, deny_reason: str, trigger_event_id: int) -> Event:
    """Emit when an :meth:`acquire_lease` request is rejected.

    Carries the failing :class:`LeaseCheck` ``check_name`` and a short
    structured ``deny_reason`` so observability tools can distinguish
    e.g. ``"throttle_exceeded"`` from ``"speaker_cap_reached"``.
    Always sender ``"system"``; emitted under ``post_internal``.
    """
    return _control(
        "lease_denied",
        holder=holder,
        check_name=check_name,
        deny_reason=deny_reason,
        trigger_event_id=int(trigger_event_id),
    )


def journal_error(exception_class: str, message: str) -> Event:
    """Emit when a journal write fails.

    ``exception_class`` is the originating exception's type name (e.g.
    ``"OSError"``); ``message`` is the exception text. Posted once per
    session (the journal stays in degraded mode after the first failure
    but does not re-emit on every subsequent failure). The message is
    pushed through :func:`redact_error_text` so any leaked secret
    fragments are scrubbed at the kernel boundary regardless of caller
    discipline.
    """
    return _control(
        "journal_error", exception_class=exception_class, message=redact_error_text(message)
    )


def actor_error(participant_id: str, exception_class: str, message: str) -> Event:
    """Emit when an actor's loop catches an exception around ``step()``.

    The actor thread keeps running — this event is purely diagnostic.
    ``message`` is run through :func:`redact_error_text` (length-capped
    + secret-pattern scrub) at the kernel boundary so a leaky adapter
    exception cannot reach the journal verbatim.
    """
    return _control(
        "actor_error",
        participant_id=participant_id,
        exception_class=exception_class,
        message=redact_error_text(message),
    )


def journal_corruption(
    line_offset: int, raw_excerpt: str, error_class: str, error_message: str
) -> Event:
    """Emit when a mid-stream journal line fails per-kind shape validation.

    Surfaced by :meth:`Journal.iter_events` at replay time on an
    :class:`EventShapeError` from :meth:`Event.from_jsonl`. The kernel
    keeps running; this event makes the corrupt line operator-visible
    rather than silently skipped.

    ``raw_excerpt`` is a short prefix of the offending line, redacted
    via :func:`redact_error_text` so any secret bytes that ended up in
    the journal don't get re-leaked into the corruption event.
    """
    return _control(
        "journal_corruption",
        line_offset=line_offset,
        raw_excerpt=redact_error_text(raw_excerpt, max_chars=120),
        error_class=error_class,
        error_message=redact_error_text(error_message),
    )


def journal_truncated(line_offset: int, raw_excerpt: str) -> Event:
    """Emit when the FINAL journal line is non-empty but unparseable.

    Distinguished from :func:`journal_corruption` by position: only the
    trailing line. Almost always means an interrupted write at crash;
    the room can keep running. Replay simply discards the truncated
    line.
    """
    return _control(
        "journal_truncated",
        line_offset=line_offset,
        raw_excerpt=redact_error_text(raw_excerpt, max_chars=120),
    )


def snapshot_dropped(dropped_total: int, queue_depth: int) -> Event:
    """Emit when the journal's bounded snapshot queue overflowed.

    The bounded queue (P2.3 / audit RES3) drops the oldest pending
    snapshot when a new one arrives and the queue is full. ``dropped_total``
    is the cumulative count since process start; ``queue_depth`` is the
    queue's configured maxsize.
    """
    return _control(
        "snapshot_dropped",
        dropped_total=dropped_total,
        queue_depth=queue_depth,
    )


# ---------------------------------------------------------------------------
# Stream events
# ---------------------------------------------------------------------------

StreamEndStatus = Literal[
    "committed",
    "suppressed",
    "cancelled",
    "error",
    "lease_expired",
    "passed",
]


def stream_start(lease_id: int, participant_id: str, trigger_event_id: int) -> Event:
    return Event(
        kind="stream",
        sender=participant_id,
        body={
            "stream_event": "start",
            "lease_id": lease_id,
            "trigger_event_id": trigger_event_id,
        },
    )


def stream_delta(lease_id: int, participant_id: str, text: str) -> Event:
    return Event(
        kind="stream",
        sender=participant_id,
        body={"stream_event": "delta", "lease_id": lease_id, "text": text},
    )


def stream_end(
    lease_id: int,
    participant_id: str,
    status: StreamEndStatus,
    error: Optional[str] = None,
    committed_event_id: Optional[int] = None,
) -> Event:
    """Terminal stream event.

    The optional ``error`` field is run through :func:`redact_error_text`
    at the kernel boundary so a provider exception (which can include
    API-key fragments / request payloads / full URLs) cannot reach the
    journal or any subscriber unscrubbed. This is the kernel's last-line
    defense regardless of whether the calling adapter remembered to
    scrub.
    """
    body: dict = {"stream_event": "end", "lease_id": lease_id, "status": status}
    if error:
        body["error"] = redact_error_text(error)
    if committed_event_id is not None:
        body["committed_event_id"] = committed_event_id
    return Event(kind="stream", sender=participant_id, body=body)


# ---------------------------------------------------------------------------
# Chat / system / summary
# ---------------------------------------------------------------------------


def chat(
    sender: str,
    body: str,
    *,
    addressees: Optional[list[str]] = None,
    channel: str = "main",
    user_turn_id: Optional[int] = None,
    room_epoch: int = 0,
    meta: Optional[dict] = None,
) -> Event:
    return Event(
        kind="chat",
        sender=sender,
        body=body,
        addressees=list(addressees or []),
        channel=channel,
        user_turn_id=user_turn_id,
        room_epoch=room_epoch,
        meta=meta or {},
    )


def system(body: str, **kwargs: Any) -> Event:
    return Event(kind="system", sender="system", body=body, **kwargs)


def summary(
    body: str, *, channel: str = "main", room_epoch: int = 0, meta: Optional[dict] = None
) -> Event:
    """Canonical main-channel compaction summary."""
    return Event(
        kind="summary",
        sender="system",
        body=body,
        channel=channel,
        room_epoch=room_epoch,
        meta=meta or {},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def control_type_of(ev: Event) -> Optional[str]:
    """Return the ``control_type`` of a control event, else ``None``."""
    if ev.kind != "control":
        return None
    if isinstance(ev.body, dict):
        return ev.body.get("control_type")
    return None


def stream_event_of(ev: Event) -> Optional[str]:
    """Return ``"start"`` / ``"delta"`` / ``"end"`` for stream events."""
    if ev.kind != "stream":
        return None
    if isinstance(ev.body, dict):
        return ev.body.get("stream_event")
    return None


def is_direct_mention(ev: Event, participant_id: str) -> bool:
    """A participant is directly mentioned when their id is in addressees."""
    return ev.kind == "chat" and participant_id in ev.addressees


def is_known_control(ev: Event) -> bool:
    """True iff ``ev`` is a control event with a currently-registered control_type.

    Used at journal replay time to filter retired control events (e.g.
    legacy ``mode_changed``, ``debate_turn``, ``forfeit``, ``debate_end``
    lines from older sessions). Such lines deserialize cleanly via
    :meth:`Event.from_jsonl` but should not feed back into coordinator
    state on replay.
    """
    if ev.kind != "control":
        return False
    return control_type_of(ev) in CONTROL_TYPES
