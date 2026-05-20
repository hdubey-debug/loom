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


# v0.3 PR 8 / doctrine P2 / §4 — three event planes.
#
# - CONVERSATION: chat content; pure exchange, no side effects.
# - CONTROL: kernel-state mutations (slot setters, capabilities,
#   budget, leases, control actions).
# - EXECUTION: external-world effects (tool calls, sandbox lifecycle;
#   v0.4+).
#
# The three planes share the bus + journal; the distinction is in
# *taxonomy + replay treatment*, not in transport.

from enum import Enum as _Enum  # noqa: E402


class EventPlane(str, _Enum):
    CONVERSATION = "conversation"
    CONTROL = "control"
    EXECUTION = "execution"


# Mapping from ``Event.kind`` to its plane. ``control`` events are
# ambiguous at the kind level — they span both CONTROL and EXECUTION
# planes; the per-control_type table below disambiguates.
_KIND_TO_PLANE: dict[str, EventPlane] = {
    "chat": EventPlane.CONVERSATION,
    "stream": EventPlane.CONVERSATION,
    "system": EventPlane.CONVERSATION,
    "topic": EventPlane.CONVERSATION,
    "summary": EventPlane.CONVERSATION,
    "presence": EventPlane.CONVERSATION,
    "control": EventPlane.CONTROL,  # default; per-control_type below may shift
}


# Per-control_type plane override. The default (CONTROL) is correct
# for every control event Loom emits today; v0.4 tool events will
# populate the EXECUTION entries.
_CONTROL_TYPE_TO_PLANE: dict[str, EventPlane] = {
    # v0.4+ tool_call_proposed / tool_result land in EXECUTION.
}


def plane_of(event: "Event") -> EventPlane:
    """Return the :class:`EventPlane` for ``event``.

    For control events, consults the per-control_type table first
    (so v0.4 tool events drop into EXECUTION without re-checking the
    kind-level default). Falls back to ``_KIND_TO_PLANE``.
    """
    if event.kind == "control":
        ct = event.body.get("control_type") if isinstance(event.body, dict) else None
        if isinstance(ct, str):
            plane = _CONTROL_TYPE_TO_PLANE.get(ct)
            if plane is not None:
                return plane
    return _KIND_TO_PLANE.get(event.kind, EventPlane.CONTROL)


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
    # v0.2.1 envelope additions (PR 3 of the hardening audit). Both
    # default-initialise to v1 / empty, and both default-apply when a
    # v0.2.0-shaped journal line is read back via ``from_jsonl`` (the
    # old lines lack these keys). Old journals load cleanly.
    schema_version: int = 1
    # v0.3 PR 4 (doctrine P11): typed causal graph. v0.2.1 reserved the
    # slot as an untyped tuple; v0.3 tightens to ``tuple[CausalRef,
    # ...]`` and coerces JSON-loaded list-of-dicts into the typed form
    # via :func:`loom.kernel.causality.coerce_causal_refs` in
    # ``__post_init__``. Empty by default; populated by the kernel call
    # sites that have a meaningful predicate (stream_* + control_action_*
    # land in PRs 4 / 8 / 9). Old v0.2.0 / v0.2.1 lines load as ``()``.
    causal_refs: tuple = ()
    # v0.3 PR 4 (doctrine P12): trace metadata on every event. ``None``
    # by default — the coordinator stamps a :class:`TraceContext` on
    # events posted under a held lease. Old journal lines without the
    # key load as ``None``.
    trace: Optional[Any] = None
    # v0.3.x PR 1 (doctrine P21): thread membership as a first-class
    # envelope field. Every event belongs to exactly one logical
    # thread; ``"main"`` is the default for the room-level thread.
    # Leases inherit ``thread_id`` from their ``LeaseContext`` and
    # the coordinator's ``_emit_under_lease`` helper propagates it
    # onto the emitted event. Old journal lines without the key load
    # as ``"main"``.
    thread_id: str = "main"

    def __post_init__(self) -> None:
        # JSON has no tuple type, so a round-trip through ``from_jsonl``
        # surfaces ``causal_refs`` as a list. The v0.3 coercer accepts
        # already-typed input AND list[dict]; the result is always
        # ``tuple[CausalRef, ...]`` so ``from_jsonl(to_jsonl(e)) == e``
        # holds bit-stably.
        from loom.kernel.causality import coerce_causal_refs, coerce_trace

        self.causal_refs = coerce_causal_refs(self.causal_refs)
        # ``trace`` accepts dict (just-loaded JSON), already-typed
        # TraceContext (kernel-emitted), or None (legacy).
        self.trace = coerce_trace(self.trace)

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
            "schema_version": self.schema_version,
            "causal_refs": [r.to_jsonable() for r in self.causal_refs],
            "trace": self.trace.to_jsonable() if self.trace is not None else None,
            "thread_id": self.thread_id,
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
        "schema_version",
        "causal_refs",
        "trace",  # v0.3 PR 4 (doctrine P12).
        "thread_id",  # v0.3.x PR 1 (doctrine P21).
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
        # v0.2.1 PR 2 (audit C4 partial): per-control-type payload
        # validation. ``_CONTROL_PAYLOAD_VALIDATORS`` is seeded for the
        # two policy events promoted in PR 2; v0.3 will populate the
        # full registry per doctrine P7 (versioned semantic effects).
        # Control types without an entry pass through unchanged so this
        # validator can be extended incrementally without breaking
        # journals.
        validator = _CONTROL_PAYLOAD_VALIDATORS.get(ct)
        if validator is not None:
            validator(body)
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


def _validate_policy_slow(body: dict) -> None:
    if not _is_number(body.get("elapsed_ms")):
        raise EventShapeError("policy_slow body must have number 'elapsed_ms'")
    if not _is_number(body.get("threshold_ms")):
        raise EventShapeError("policy_slow body must have number 'threshold_ms'")
    if not _is_int(body.get("user_event_id")):
        raise EventShapeError("policy_slow body must have int 'user_event_id'")


def _validate_policy_error(body: dict) -> None:
    ec = body.get("exception_class")
    if not isinstance(ec, str) or not ec:
        raise EventShapeError("policy_error body must have non-empty str 'exception_class'")
    msg = body.get("message")
    if not isinstance(msg, str):
        raise EventShapeError("policy_error body must have str 'message'")
    if not _is_number(body.get("elapsed_ms")):
        raise EventShapeError("policy_error body must have number 'elapsed_ms'")
    if not _is_int(body.get("user_event_id")):
        raise EventShapeError("policy_error body must have int 'user_event_id'")


# Per-(control_type) payload validators. v0.2.1 PR 2 (audit C4 partial)
# seeds the table for the two policy events; v0.3 will populate the
# full registry per doctrine P7. Control types not listed here pass
# through the kind-level validator unchanged.
def _validate_capability_granted(body: dict) -> None:
    for k in ("grant_id", "grantor_id", "grantee_id", "capability"):
        v = body.get(k)
        if not isinstance(v, str) or not v:
            raise EventShapeError(f"capability_granted body must have non-empty str {k!r}")
    if not _is_int(body.get("source_event_id")):
        raise EventShapeError("capability_granted body must have int 'source_event_id'")
    ea = body.get("expires_at")
    if ea is not None and not _is_number(ea):
        raise EventShapeError("capability_granted body 'expires_at' must be number or null")


def _validate_capability_revoked(body: dict) -> None:
    gid = body.get("grant_id")
    if not isinstance(gid, str) or not gid:
        raise EventShapeError("capability_revoked body must have non-empty str 'grant_id'")
    rid = body.get("revoker_id")
    if not isinstance(rid, str) or not rid:
        raise EventShapeError("capability_revoked body must have non-empty str 'revoker_id'")


def _validate_capability_expired(body: dict) -> None:
    gid = body.get("grant_id")
    if not isinstance(gid, str) or not gid:
        raise EventShapeError("capability_expired body must have non-empty str 'grant_id'")


def _validate_budget_reserved(body: dict) -> None:
    if not _is_int(body.get("lease_id")):
        raise EventShapeError("budget_reserved body must have int 'lease_id'")
    if not _is_number(body.get("amount")):
        raise EventShapeError("budget_reserved body must have number 'amount'")


def _validate_budget_committed(body: dict) -> None:
    if not _is_int(body.get("lease_id")):
        raise EventShapeError("budget_committed body must have int 'lease_id'")
    if not _is_number(body.get("actual")):
        raise EventShapeError("budget_committed body must have number 'actual'")


def _validate_budget_refunded(body: dict) -> None:
    if not _is_int(body.get("lease_id")):
        raise EventShapeError("budget_refunded body must have int 'lease_id'")
    if not _is_number(body.get("amount")):
        raise EventShapeError("budget_refunded body must have number 'amount'")
    if not isinstance(body.get("reason"), str):
        raise EventShapeError("budget_refunded body must have str 'reason'")


def _validate_control_action_proposed(body: dict) -> None:
    for k in ("action_name", "proposer_id"):
        if not isinstance(body.get(k), str) or not body[k]:
            raise EventShapeError(f"control_action_proposed body must have non-empty str {k!r}")


def _validate_control_action_applied(body: dict) -> None:
    for k in ("action_name", "applier_id"):
        if not isinstance(body.get(k), str) or not body[k]:
            raise EventShapeError(f"control_action_applied body must have non-empty str {k!r}")


def _validate_control_action_denied(body: dict) -> None:
    for k in ("action_name", "proposer_id", "reason"):
        if not isinstance(body.get(k), str) or not body[k]:
            raise EventShapeError(f"control_action_denied body must have non-empty str {k!r}")


def _validate_lease_closed(body: dict) -> None:
    if not _is_int(body.get("lease_id")):
        raise EventShapeError("lease_closed body must have int 'lease_id'")
    for k in ("holder", "kind", "reason"):
        if not isinstance(body.get(k), str) or not body[k]:
            raise EventShapeError(f"lease_closed body must have non-empty str {k!r}")


def _validate_stream_stalled(body: dict) -> None:
    if not _is_int(body.get("lease_id")):
        raise EventShapeError("stream_stalled body must have int 'lease_id'")
    if not isinstance(body.get("holder"), str) or not body["holder"]:
        raise EventShapeError("stream_stalled body must have non-empty str 'holder'")
    if not _is_number(body.get("seconds_silent")):
        raise EventShapeError("stream_stalled body must have number 'seconds_silent'")


def _validate_summary_proposed(body: dict) -> None:
    for k in ("summary_id", "summarizer_id"):
        if not isinstance(body.get(k), str) or not body[k]:
            raise EventShapeError(f"summary_proposed body must have non-empty str {k!r}")
    scope = body.get("scope")
    if not isinstance(scope, dict) or not isinstance(scope.get("room_id"), str):
        raise EventShapeError("summary_proposed body must have dict 'scope' with str 'room_id'")
    cer = body.get("covers_event_range")
    if not (isinstance(cer, list) and len(cer) == 2 and _is_int(cer[0]) and _is_int(cer[1])):
        raise EventShapeError("summary_proposed body must have list[int, int] 'covers_event_range'")


def _validate_summary_committed(body: dict) -> None:
    _validate_summary_proposed(body)
    if not _is_int(body.get("committed_at_event_id", 0)):
        raise EventShapeError("summary_committed body must have int 'committed_at_event_id'")


def _validate_summary_failed(body: dict) -> None:
    for k in ("proposed_summary_id", "reason"):
        if not isinstance(body.get(k), str) or not body[k]:
            raise EventShapeError(f"summary_failed body must have non-empty str {k!r}")
    scope = body.get("scope")
    if not isinstance(scope, dict) or not isinstance(scope.get("room_id"), str):
        raise EventShapeError("summary_failed body must have dict 'scope' with str 'room_id'")


def _validate_summarization_scheduled(body: dict) -> None:
    if not _is_int(body.get("lease_id")):
        raise EventShapeError("summarization_scheduled body must have int 'lease_id'")
    if not isinstance(body.get("summarizer_id"), str) or not body["summarizer_id"]:
        raise EventShapeError(
            "summarization_scheduled body must have non-empty str 'summarizer_id'"
        )
    scope = body.get("scope")
    if not isinstance(scope, dict) or not isinstance(scope.get("room_id"), str):
        raise EventShapeError(
            "summarization_scheduled body must have dict 'scope' with str 'room_id'"
        )


def _validate_compaction_disabled(body: dict) -> None:
    for k in ("summarizer_id", "reason"):
        if not isinstance(body.get(k), str) or not body[k]:
            raise EventShapeError(f"compaction_disabled body must have non-empty str {k!r}")
    if not _is_int(body.get("failure_count")):
        raise EventShapeError("compaction_disabled body must have int 'failure_count'")
    scope = body.get("scope")
    if not isinstance(scope, dict) or not isinstance(scope.get("room_id"), str):
        raise EventShapeError("compaction_disabled body must have dict 'scope' with str 'room_id'")


_CONTROL_PAYLOAD_VALIDATORS: dict = {
    "policy_slow": _validate_policy_slow,
    "policy_error": _validate_policy_error,
    # v0.3 PR 5 — capability ledger lifecycle (doctrine §6).
    "capability_granted": _validate_capability_granted,
    "capability_revoked": _validate_capability_revoked,
    "capability_expired": _validate_capability_expired,
    # v0.3 PR 6 — budget ledger three-way accounting (doctrine §9).
    "budget_reserved": _validate_budget_reserved,
    "budget_committed": _validate_budget_committed,
    "budget_refunded": _validate_budget_refunded,
    # v0.3 PR 8 — control-plane action lifecycle (doctrine P2 / §4).
    "control_action_proposed": _validate_control_action_proposed,
    "control_action_applied": _validate_control_action_applied,
    "control_action_denied": _validate_control_action_denied,
    "lease_closed": _validate_lease_closed,
    # v0.3 PR 12 — streaming-stall watchdog (closes audit D2).
    "stream_stalled": _validate_stream_stalled,
    # v0.3.x PR 3 — view-layer compaction lifecycle.
    "summary_proposed": _validate_summary_proposed,
    "summary_committed": _validate_summary_committed,
    "summary_failed": _validate_summary_failed,
    # v0.3.x PR 5 — Path A scheduling + per-scope disablement.
    "summarization_scheduled": _validate_summarization_scheduled,
    "compaction_disabled": _validate_compaction_disabled,
}


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

    # v0.2.1 envelope additions: missing keys are accepted (old
    # v0.2.0 journal lines lack them); present keys must satisfy
    # the declared shape.
    if "schema_version" in d:
        sv = d["schema_version"]
        if not _is_int(sv) or sv < 1:
            raise EventShapeError("schema_version must be a positive int")
    if "causal_refs" in d:
        cr = d["causal_refs"]
        if not isinstance(cr, list):
            raise EventShapeError(f"causal_refs must be a list, got {type(cr).__name__}")
    # v0.3 PR 4 envelope addition (doctrine P12). ``trace`` is dict
    # or null; absent means "no trace context" (legacy lines). The
    # full structural check (trace_id / span_id non-empty) lives in
    # :meth:`TraceContext.from_jsonable`, exercised by
    # :meth:`Event.__post_init__`.
    if "trace" in d:
        tr = d["trace"]
        if tr is not None and not isinstance(tr, dict):
            raise EventShapeError(f"trace must be a dict or null, got {type(tr).__name__}")
    # v0.3.x PR 1 (doctrine P21): thread_id is a non-empty str. Absent
    # in legacy lines → loads as default "main" via Event(...).
    if "thread_id" in d:
        tid = d["thread_id"]
        if not isinstance(tid, str) or not tid:
            raise EventShapeError("thread_id must be a non-empty string")

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
        # Proactive lease TTL expiry (v0.2.1 PR 1, audit finding D1).
        # Emitted by the coordinator watchdog when it discovers a lease
        # whose ``expires_at`` has passed. Distinct from the ``stream_end``
        # body status ``"lease_expired"`` (control plane vs stream
        # plane — see audit §11 Q2).
        "lease_expired",
        # v0.3 PR 5 (doctrine §6). Capability ledger lifecycle.
        "capability_granted",
        "capability_revoked",
        "capability_expired",
        # v0.3 PR 6 (doctrine §9). Budget ledger three-way accounting.
        "budget_reserved",
        "budget_committed",
        "budget_refunded",
        # v0.3 PR 8 (doctrine P2 / §4). Control-plane action lifecycle.
        "control_action_proposed",
        "control_action_applied",
        "control_action_denied",
        # v0.3 PR 8 — unified lease termination event. Replaces
        # ``lease_denied`` and ``lease_expired`` for go-forward emit.
        # The legacy two are kept loadable for v0.2.x journal replay
        # but the coordinator emits ``lease_closed`` alongside them
        # (post-v0.3 release-cut drops the duplicates).
        "lease_closed",
        # v0.3 PR 12 (closes audit D2): streaming-stall watchdog
        # observability. Emitted when a USER_TURN lease's stream has
        # produced no chunks for ``RoomConfig.stream_stall_threshold_s``
        # seconds; coordinator follows with ``lease_closed(reason=aborted)``
        # so the lease is released.
        "stream_stalled",
        # v0.3.x PR 3 (doctrine P18 / §3 / §6). View-layer compaction
        # lifecycle. ``summary_proposed`` records the candidate;
        # ``summary_committed`` records the successful structural
        # validation + state advance; ``summary_failed`` records a
        # rejected candidate with the structural failure reason.
        # Together these are the *only* source of truth replay uses to
        # rebuild ``ContextState``; the validator is NEVER re-run on
        # replay.
        "summary_proposed",
        "summary_committed",
        "summary_failed",
        # v0.3.x PR 5 (doctrine P22 / §7). Path A audit event +
        # per-scope backoff/disablement event.
        "summarization_scheduled",
        "compaction_disabled",
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


def policy_slow(*, elapsed_ms: float, threshold_ms: float, user_event_id: int) -> Event:
    """Emit when a policy's ``classify_fn`` exceeded the slow-threshold (v0.2.1).

    Promoted from an inline ``_control(...)`` call at
    ``coordinator.py:854`` to a typed constructor as part of the
    v0.2.1 hardening audit (PR 2, finding C2). Pure observability —
    Python cannot safely interrupt arbitrary code so the call still
    completes; this event surfaces the slowness.

    Fields:
    - ``elapsed_ms``: measured wall-time of the policy call (rounded).
    - ``threshold_ms``: the configured threshold that was exceeded
      (currently the kernel constant ``_POLICY_SLOW_THRESHOLD_MS``;
      v0.3 will move it to ``RoomConfig`` per audit D3).
    - ``user_event_id``: the bus id of the user event being classified,
      for correlation in the journal.
    """
    return _control(
        "policy_slow",
        elapsed_ms=round(float(elapsed_ms), 3),
        threshold_ms=round(float(threshold_ms), 3),
        user_event_id=int(user_event_id),
    )


def policy_error(
    *,
    exception_class: str,
    message: str,
    elapsed_ms: float,
    user_event_id: int,
) -> Event:
    """Emit when a policy's ``classify_fn`` raised (v0.2.1).

    Promoted from an inline ``_control(...)`` call at
    ``coordinator.py:824`` to a typed constructor as part of the
    v0.2.1 hardening audit (PR 2, finding C2). The coordinator then
    dispatches on ``policy_error_mode`` (``close_turn`` /
    ``default_responder`` / ``raise``).

    ``message`` is run through :func:`redact_error_text` at the
    kernel boundary so policy exception text (which can include
    request payloads, prompt fragments, or secret strings depending
    on the policy implementation) cannot reach the journal verbatim.
    Matches the scrubbing discipline of :func:`actor_error` and
    :func:`journal_error`.
    """
    return _control(
        "policy_error",
        exception_class=exception_class,
        message=redact_error_text(message),
        elapsed_ms=round(float(elapsed_ms), 3),
        user_event_id=int(user_event_id),
    )


def lease_expired(*, holder: str, lease_id: int, trigger_event_id: int) -> Event:
    """Emit when the coordinator watchdog reaps an unattended lease (v0.2.1).

    Distinct from the ``stream_end.body["status"] == "lease_expired"``
    signal, which marks "this stream's terminal disposition was that
    the lease ran out mid-stream". The control event here marks
    "the watchdog discovered an idle lease past TTL and reaped it";
    there may be no associated stream lifecycle. See audit §11 Q2.

    Sender is always ``"system"``; emitted under ``post_internal``.
    """
    return _control(
        "lease_expired",
        holder=holder,
        lease_id=int(lease_id),
        trigger_event_id=int(trigger_event_id),
    )


def capability_granted(
    *,
    grant_id: str,
    grantor_id: str,
    grantee_id: str,
    capability: str,
    expires_at: Optional[float] = None,
    source_event_id: int,
) -> Event:
    """v0.3 PR 5 / doctrine §6 — capability grant emitted to the journal.

    Always sender ``"system"`` (under ``post_internal``). The
    coordinator constructs and emits one of these per
    ``CapabilityGrantedEffect`` it applies; ``grant_id`` is the
    ledger key that future ``capability_revoked`` /
    ``capability_expired`` events reference. ``expires_at`` is a
    ``time.monotonic`` value (or ``None`` for non-expiring grants).
    """
    return _control(
        "capability_granted",
        grant_id=str(grant_id),
        grantor_id=str(grantor_id),
        grantee_id=str(grantee_id),
        capability=str(capability),
        expires_at=expires_at,
        source_event_id=int(source_event_id),
    )


def capability_revoked(*, grant_id: str, revoker_id: str, reason: str = "revoked") -> Event:
    """v0.3 PR 5 / doctrine §6 — capability revocation."""
    return _control(
        "capability_revoked",
        grant_id=str(grant_id),
        revoker_id=str(revoker_id),
        reason=str(reason),
    )


def capability_expired(*, grant_id: str) -> Event:
    """v0.3 PR 5 / doctrine §6 — TTL-based capability expiry, emitted by watchdog."""
    return _control("capability_expired", grant_id=str(grant_id))


def budget_reserved(*, lease_id: int, amount: float, scope: Optional[dict] = None) -> Event:
    """v0.3 PR 6 / doctrine §9 — budget reservation against a lease.

    ``scope`` round-trips as a dict (the dimensional key); the
    reducer reconstructs a :class:`BudgetScope` from it. ``None``
    means the room-level no-narrowing scope.
    """
    return _control(
        "budget_reserved",
        lease_id=int(lease_id),
        amount=float(amount),
        scope=dict(scope) if scope else {},
    )


def budget_committed(*, lease_id: int, actual: float, scope: Optional[dict] = None) -> Event:
    """v0.3 PR 6 / doctrine §9 — commit actual cost against an outstanding reservation."""
    return _control(
        "budget_committed",
        lease_id=int(lease_id),
        actual=float(actual),
        scope=dict(scope) if scope else {},
    )


def control_action_proposed(
    *,
    action_name: str,
    proposer_id: str,
    params: Optional[dict] = None,
    target_event_id: Optional[int] = None,
) -> Event:
    """v0.3 PR 8 / doctrine P2 / §4 — control-action proposal entered the kernel.

    Sender ``"system"`` (via ``post_internal``). The proposer's id is
    in the body, distinct from the kernel's sender field so the
    journal carries both: who *asked* and who *journaled*.
    """
    body: dict = {
        "action_name": str(action_name),
        "proposer_id": str(proposer_id),
        "params": dict(params or {}),
    }
    if target_event_id is not None:
        body["target_event_id"] = int(target_event_id)
    return _control("control_action_proposed", **body)


def control_action_applied(
    *,
    action_name: str,
    applier_id: str,
    effects: Optional[list] = None,
    applied_at_event_id: Optional[int] = None,
) -> Event:
    """v0.3 PR 8 / doctrine P2 / §4 — control-action effects were applied.

    ``effects`` is a list of `{effect_type, schema_version, ...}`
    dicts describing what the registered reducers produced; the
    coordinator's apply path records them so the journal carries
    enough detail to reconstruct state during replay even after a
    reducer-version bump.
    """
    body: dict = {
        "action_name": str(action_name),
        "applier_id": str(applier_id),
        "effects": list(effects or []),
    }
    if applied_at_event_id is not None:
        body["applied_at_event_id"] = int(applied_at_event_id)
    return _control("control_action_applied", **body)


def control_action_denied(
    *,
    action_name: str,
    proposer_id: str,
    reason: str,
    check_name: Optional[str] = None,
) -> Event:
    """v0.3 PR 8 / doctrine P2 / §4 — control-action proposal rejected.

    ``reason`` is a short structured string from the
    :class:`DenialReason` enum (PR 9 — INSUFFICIENT_CAPABILITY /
    INVALID_PARAMS / etc.); ``check_name`` is the failing lease
    check when the denial happened during lease acquisition.
    """
    body: dict = {
        "action_name": str(action_name),
        "proposer_id": str(proposer_id),
        "reason": str(reason),
    }
    if check_name:
        body["check_name"] = str(check_name)
    return _control("control_action_denied", **body)


def stream_stalled(
    *,
    lease_id: int,
    holder: str,
    seconds_silent: float,
) -> Event:
    """v0.3 PR 12 / audit D2 — streaming-stall watchdog observability.

    Emitted when a USER_TURN lease's stream has been silent (no
    chunks) for ``RoomConfig.stream_stall_threshold_s`` seconds while
    the lease was still nominally valid. The coordinator follows
    with a ``lease_closed(reason="aborted")`` so the lease releases
    and the room can move on.

    Distinct from the v0.2.1 ``lease_expired`` event (which fires on
    TTL, not on silence): a stall can happen well before the TTL when
    a remote provider hangs partway through a stream.
    """
    return _control(
        "stream_stalled",
        lease_id=int(lease_id),
        holder=str(holder),
        seconds_silent=float(seconds_silent),
    )


def lease_closed(
    *,
    lease_id: int,
    holder: str,
    kind: str,
    reason: str,
    span_id: Optional[str] = None,
) -> Event:
    """v0.3 PR 8 / doctrine P2 / §4 — unified lease termination event.

    Replaces the v0.2 split between ``lease_denied`` and
    ``lease_expired``. ``reason`` admits the broader vocabulary:

    - ``released`` — clean release after successful work.
    - ``denied`` — failed a check at acquire time.
    - ``expired`` — TTL exceeded; reaped by watchdog.
    - ``cancelled`` — explicit cancel (revoke / participant removed /
      room shutdown).
    - ``aborted`` — runtime abort (exception, fail-closed).
    - ``aborted_validation`` — post-LLM validation suppressed; partial
      commit + refund (see :meth:`BudgetLedger.partial_commit_and_refund`).

    ``kind`` is the :class:`LeaseKind` value of the closed lease.
    ``span_id`` (when present) is the lease's trace span (PR 4) for
    observability correlation.

    For one v0.3.x release the coordinator continues to emit the
    legacy ``lease_denied`` / ``lease_expired`` events alongside
    ``lease_closed`` so v0.2-era consumers and tests don't break.
    """
    body: dict = {
        "lease_id": int(lease_id),
        "holder": str(holder),
        "kind": str(kind),
        "reason": str(reason),
    }
    if span_id is not None:
        body["span_id"] = str(span_id)
    return _control("lease_closed", **body)


def budget_refunded(
    *, lease_id: int, amount: float, reason: str, scope: Optional[dict] = None
) -> Event:
    """v0.3 PR 6 / doctrine §9 — refund a reservation.

    ``reason`` is a short structured string mirroring the lease
    termination reason (``denied`` / ``expired`` / ``cancelled``).
    """
    return _control(
        "budget_refunded",
        lease_id=int(lease_id),
        amount=float(amount),
        reason=str(reason),
        scope=dict(scope) if scope else {},
    )


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


# ---------------------------------------------------------------------------
# v0.3.x PR 3 — view-layer compaction lifecycle (doctrine P18 / §3 / §6)
# ---------------------------------------------------------------------------


def _scope_payload(scope: Any) -> dict:
    """Coerce a :class:`loom.kernel.context.ContextScope` (or a plain
    dict in the same shape) into the JSON-able sub-dict used on
    compaction event bodies.
    """
    if isinstance(scope, dict):
        room_id = scope.get("room_id", "")
        thread_id = scope.get("thread_id", "main")
        actor_id = scope.get("actor_id")
    else:
        room_id = getattr(scope, "room_id", "")
        thread_id = getattr(scope, "thread_id", "main")
        actor_id = getattr(scope, "actor_id", None)
    return {
        "room_id": str(room_id),
        "thread_id": str(thread_id),
        "actor_id": actor_id if actor_id is None else str(actor_id),
    }


def summary_proposed(
    *,
    summary_id: str,
    scope: Any,
    covers_event_range: tuple,
    proposed_text: str,
    retained_event_ids: tuple = (),
    input_summary_ids: tuple = (),
    input_event_ranges: tuple = (),
    model_id: str = "",
    prompt_hash: str = "",
    summarizer_id: str,
    proposed_at_event_id: int = -1,
    thread_id: Optional[str] = None,
) -> Event:
    """``summary_proposed`` — a candidate compaction record (doctrine §6).

    The body carries the *full* :class:`SummaryRecord` payload so a
    journal replay can reconstruct the proposal without re-running the
    summariser. ``thread_id`` defaults to the scope's ``thread_id``;
    callers running under a lease should leave it ``None`` and rely on
    the coordinator's :meth:`_emit_under_lease` to stamp it.
    """
    sc = _scope_payload(scope)
    body: dict = {
        "summary_id": str(summary_id),
        "scope": sc,
        "covers_event_range": list(covers_event_range),
        "proposed_text": str(proposed_text),
        "retained_event_ids": list(retained_event_ids),
        "input_summary_ids": list(input_summary_ids),
        "input_event_ranges": [list(r) for r in input_event_ranges],
        "model_id": str(model_id),
        "prompt_hash": str(prompt_hash),
        "summarizer_id": str(summarizer_id),
        "proposed_at_event_id": int(proposed_at_event_id),
    }
    ev_obj = _control("summary_proposed", **body)
    ev_obj.thread_id = thread_id or sc["thread_id"]
    return ev_obj


def summary_committed(
    *,
    summary_id: str,
    scope: Any,
    covers_event_range: tuple,
    proposed_text: str,
    retained_event_ids: tuple = (),
    input_summary_ids: tuple = (),
    input_event_ranges: tuple = (),
    model_id: str = "",
    prompt_hash: str = "",
    summarizer_id: str,
    proposed_at_event_id: int = -1,
    supersedes_summary_ids: tuple = (),
    committed_at_event_id: int = -1,
    thread_id: Optional[str] = None,
) -> Event:
    """``summary_committed`` — the structural validator and the under-
    lock commit step both passed; the record is now part of
    :class:`ContextState`. Carries the full record + supersession list
    so replay can rebuild state from the journal alone.
    """
    sc = _scope_payload(scope)
    body: dict = {
        "summary_id": str(summary_id),
        "scope": sc,
        "covers_event_range": list(covers_event_range),
        "proposed_text": str(proposed_text),
        "retained_event_ids": list(retained_event_ids),
        "input_summary_ids": list(input_summary_ids),
        "input_event_ranges": [list(r) for r in input_event_ranges],
        "model_id": str(model_id),
        "prompt_hash": str(prompt_hash),
        "summarizer_id": str(summarizer_id),
        "proposed_at_event_id": int(proposed_at_event_id),
        "supersedes_summary_ids": list(supersedes_summary_ids),
        "committed_at_event_id": int(committed_at_event_id),
    }
    ev_obj = _control("summary_committed", **body)
    ev_obj.thread_id = thread_id or sc["thread_id"]
    return ev_obj


def summary_failed(
    *,
    proposed_summary_id: str,
    scope: Any,
    reason: str,
    details: str = "",
    failed_validator: str = "structural",
    summarizer_id: str = "",
    thread_id: Optional[str] = None,
) -> Event:
    """``summary_failed`` — the validator (off-lock or under-lock)
    rejected the proposal. ``reason`` is a :class:`SummaryFailureReason`
    value; ``failed_validator`` is ``"structural"`` for the pre-validator
    and ``"anchor"`` for the under-lock anchor check.
    """
    sc = _scope_payload(scope)
    body: dict = {
        "proposed_summary_id": str(proposed_summary_id),
        "scope": sc,
        "reason": str(reason),
        "details": str(details),
        "failed_validator": str(failed_validator),
        "summarizer_id": str(summarizer_id),
    }
    ev_obj = _control("summary_failed", **body)
    ev_obj.thread_id = thread_id or sc["thread_id"]
    return ev_obj


def summarization_scheduled(
    *,
    scope: Any,
    lease_id: int,
    summarizer_id: str,
    trigger_pressure_ratio: float = 0.0,
    triggered_by: str = "policy",
    thread_id: Optional[str] = None,
) -> Event:
    """v0.3.x PR 5 / doctrine P22 / §7 — Path A audit event.

    Emitted when the policy hook schedules a SUMMARIZATION lease
    (Path A). Path B (control action) does NOT emit this — it goes
    through the v0.3 PR 9 control-action lifecycle which already
    has its own ``control_action_proposed`` / ``_applied`` /
    ``_denied`` taxonomy.
    """
    sc = _scope_payload(scope)
    body: dict = {
        "scope": sc,
        "lease_id": int(lease_id),
        "summarizer_id": str(summarizer_id),
        "trigger_pressure_ratio": float(trigger_pressure_ratio),
        "triggered_by": str(triggered_by),
    }
    ev_obj = _control("summarization_scheduled", **body)
    ev_obj.thread_id = thread_id or sc["thread_id"]
    return ev_obj


def compaction_disabled(
    *,
    scope: Any,
    summarizer_id: str,
    failure_count: int,
    reason: str = "consecutive_failures",
    last_failed_summary_id: str = "",
    thread_id: Optional[str] = None,
) -> Event:
    """v0.3.x PR 5 / doctrine §7 — per-scope backoff.

    Emitted when consecutive structural failures
    (``summary_failed`` with reason ≠ ANCHOR_CONFLICT) for
    ``(summarizer_id, scope)`` reach
    ``RoomConfig.summarizer_max_consecutive_failures``. The
    coordinator stops scheduling Path A summarisations for this
    scope until a :class:`DefaultSummarizerSetEffect` resets the
    counter or the policy explicitly re-enables.
    """
    sc = _scope_payload(scope)
    body: dict = {
        "scope": sc,
        "summarizer_id": str(summarizer_id),
        "failure_count": int(failure_count),
        "reason": str(reason),
        "last_failed_summary_id": str(last_failed_summary_id),
    }
    ev_obj = _control("compaction_disabled", **body)
    ev_obj.thread_id = thread_id or sc["thread_id"]
    return ev_obj


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
