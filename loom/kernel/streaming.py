"""Loom v0 — Streaming wrapper + PASS prefix protocol.

A drafting call streams. The first ``pass_buffer_chars`` characters of
the model's output are buffered. If the buffer matches the deterministic
PASS regex, the call is *suppressed* — no UI deltas, no canonical chat
event, no rendering. Otherwise the buffer is flushed to the UI and
streaming continues normally.

Status codes posted on terminal ``stream_end``:

- ``committed``     normal completion; canonical ``chat`` event emitted
- ``passed``        PASS prefix detected — agent declined the floor; the
                    obligation is *resolved* administratively (no chat,
                    no draft, but the turn does not idle-time-out on it)
- ``suppressed``    post-stream idle/duplicate filter rejected the body,
                    OR provider returned empty (distinct from ``passed``;
                    obligation remains *unresolved*)
- ``cancelled``     coordinator-initiated soft/hard cancel
- ``error``         provider raised
- ``lease_expired`` lease invalidated mid-stream (mode/membership change)

The function is sync; one ``run_streaming_call`` per drafting actor at
a time. Concurrency between actors is provided by the actor threads
themselves and the coordinator's lease arbitration.

Cost tracking is intentionally crude in v0: we approximate token count
as ``ceil(chunk_chars / 4)`` per delta and pass the total to
``coordinator.on_stream_end(...cost_tokens=...)``. Real proxies expose
usage in their final response; integrating that is a v0.1 detail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Iterator, Optional, Protocol
import math
import re

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.events import StreamEndStatus

if TYPE_CHECKING:
    # Type-only imports — avoid a runtime dependency on coordinator
    # (which itself imports the rest of the kernel). Streaming references
    # ``RoomCoordinator`` / ``TurnLease`` only in annotations, so the
    # forward-reference strings under ``from __future__ import annotations``
    # are sufficient.
    from loom.kernel.coordinator import RoomCoordinator, TurnLease  # noqa: F401


PASS_RE = re.compile(r"^\s*\[PASS\](\s|$)")
"""Detection regex for the PASS prefix protocol.

``\\s*`` allows leading whitespace including newlines (some providers
emit a leading newline). The trailing ``(\\s|$)`` enforces that
``[PASS]`` is its own token, not the start of e.g. ``[PASSED_TESTS]``.
"""


# Idle / acknowledgement phrases that are belt-and-suspenders if a
# model fails to emit ``[PASS]``. Compared after stripping and lowercasing.
IDLE_PHRASES: frozenset[str] = frozenset(
    {
        "standing by",
        "waiting",
        "waiting for argument",
        "waiting for context",
        "ready",
        "ok",
        "okay",
        "got it",
        "received",
        "noted",
        "acknowledged",
        "ack",
    }
)


_CHAIR_SPEAK_RE = re.compile(
    r"\(\s*[^)]*\braised\s+hand\b[^)]*\)|"  # "(claude_code raised hand: ...)"
    r"\byou\s+have\s+the\s+floor\b|"  # "@OAI you have the floor"
    r"\bthe\s+floor\s+is\s+yours\b|"  # "the floor is yours"
    r"\bI\s+raise\s+my\s+hand\b",  # "I raise my hand"
    re.IGNORECASE,
)


def _strip_chair_speak(text: str) -> str:
    """Drop lines containing chair-speak / hand-raise hallucinations.

    Defense-in-depth against agents that learned the legacy ``/council``
    chair format. Returns the cleaned text; if every line is chair-speak,
    returns ``""``. Line-level granularity by design — if chair-speak
    appears mid-line, the whole line is dropped.
    """
    if not _CHAIR_SPEAK_RE.search(text):
        return text
    kept = [line for line in text.splitlines() if not _CHAIR_SPEAK_RE.search(line)]
    return "\n".join(kept).strip()


# ``parse_addressees`` lives in :mod:`loom.kernel.addressees`. This
# module re-exports it for backwards compatibility — callers that
# already imported it from streaming continue to work for one release.
# New code should import from :mod:`loom.kernel.addressees`.
from loom.kernel.addressees import parse_addressees  # noqa: E402,F401


class StreamingProxy(Protocol):
    """Minimal protocol every drafting proxy must satisfy.

    The bundled proxies (Claude, Gemini, OpenAI, local Gemma) wrap their
    own SDKs to satisfy this. ``cancel`` is best-effort; if the proxy
    doesn't support hard-cancel, the call simply returns without
    aborting the underlying request and v0 falls back to soft-cancel
    (no deltas hit the UI).

    ``prompt`` is the fully-rendered string produced by
    :func:`loom.kernel.prompt.build_prompt`. It is not structured; the
    adapter parses it (or hands it to a chat API as a single user
    message) at its discretion.
    """

    def stream(self, prompt: str) -> Iterator[str]:  # pragma: no cover
        ...

    def cancel(self) -> None:  # pragma: no cover - optional
        ...


def _try_cancel(proxy: object) -> None:
    cancel = getattr(proxy, "cancel", None)
    if cancel is None:
        return
    try:
        cancel()
    except Exception:
        pass


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _is_idle_phrase(text: str) -> bool:
    return text.strip().lower() in IDLE_PHRASES


def run_streaming_call(
    proxy: StreamingProxy,
    prompt: str,
    lease: TurnLease,
    bus: MessageBus,
    coordinator: RoomCoordinator,
    *,
    channel: str = "main",
    addressable: Optional[list[str]] = None,
) -> str:
    """Run one drafting call. Returns the committed text (``""`` if not committed).

    Posts the full sequence of stream events to the bus. On
    ``status="committed"``, also posts the canonical ``chat`` event.
    Always calls ``coordinator.on_stream_end(...)`` exactly once with
    the terminal status.
    """
    bus.post(
        ev.stream_start(
            lease_id=lease.id,
            participant_id=lease.holder,
            trigger_event_id=lease.trigger_event_id,
        )
    )

    buffer = ""
    visible = ""
    flushed = False
    status: StreamEndStatus = "committed"
    error: Optional[str] = None
    cost_tokens = 0

    try:
        for chunk in proxy.stream(prompt):
            cost_tokens += _estimate_tokens(chunk)
            if not coordinator.validate_lease(lease):
                status = "lease_expired"
                _try_cancel(proxy)
                break
            if not flushed:
                buffer += chunk
                if PASS_RE.match(buffer):
                    status = "passed"
                    _try_cancel(proxy)
                    break
                if len(buffer) >= coordinator.config.pass_buffer_chars:
                    bus.post(
                        ev.stream_delta(
                            lease_id=lease.id,
                            participant_id=lease.holder,
                            text=buffer,
                        )
                    )
                    visible = buffer
                    buffer = ""
                    flushed = True
                continue
            # Already flushed: append delta directly.
            visible += chunk
            bus.post(
                ev.stream_delta(
                    lease_id=lease.id,
                    participant_id=lease.holder,
                    text=chunk,
                )
            )
    except Exception as exc:
        status = "error"
        error = str(exc)

    # Buffer never reached the flush threshold and the stream completed.
    if status == "committed" and not flushed:
        if PASS_RE.match(buffer):
            status = "passed"
        else:
            visible = buffer
            if visible:
                bus.post(
                    ev.stream_delta(
                        lease_id=lease.id,
                        participant_id=lease.holder,
                        text=visible,
                    )
                )

    # Post-stream belt-and-suspenders: drop empty / idle / duplicate /
    # chair-speak.
    cleaned = visible.strip()
    if status == "committed":
        cleaned = _strip_chair_speak(cleaned)
        if not cleaned:
            status = "suppressed"
        elif _is_idle_phrase(cleaned):
            status = "suppressed"
        elif coordinator.loop_guard.is_idle_dup(lease.holder, cleaned):
            status = "suppressed"
        else:
            # v0.2: policy veto hook runs after the kernel's filters
            # so a policy that returns False can layer additional
            # suppression (semantic similarity, off-topic, rate limit).
            # The kernel filters cover the common loop-guard cases;
            # the policy hook handles policy-specific concerns.
            policy = getattr(coordinator, "_policy", None)
            if policy is not None:
                try:
                    allowed = policy.should_post_response(
                        body=cleaned,
                        state=coordinator.state.view(),
                        participant_id=lease.holder,
                    )
                except Exception:
                    allowed = True
                if not allowed:
                    status = "suppressed"

    committed_text: Optional[str] = None
    committed_event_id: Optional[int] = None
    if status == "committed":
        # Post the canonical chat event FIRST so subscribers that switch
        # on the terminal stream_end already see the committed body. The
        # stream_end below carries the chat's id for correlation.
        addressees = parse_addressees(
            cleaned,
            addressable or list(coordinator.state.participants.keys()),
            exclude=lease.holder,
        )
        chat_event = ev.chat(
            sender=lease.holder,
            body=cleaned,
            addressees=addressees,
            channel=channel,
            user_turn_id=lease.user_turn_id,
            room_epoch=lease.room_epoch,
            meta={"lease_id": lease.id, "cost_tokens": cost_tokens},
        )
        committed_event_id = bus.post(chat_event)
        committed_text = cleaned

    # Terminal stream_end (always exactly one). For committed drafts it
    # follows the chat event and carries ``committed_event_id``.
    bus.post(
        ev.stream_end(
            lease_id=lease.id,
            participant_id=lease.holder,
            status=status,
            error=error,
            committed_event_id=committed_event_id,
        )
    )

    coordinator.on_stream_end(
        lease,
        status,
        committed_text=committed_text,
        cost_tokens=cost_tokens,
        committed_event_id=committed_event_id,
    )

    return committed_text or ""


# ---------------------------------------------------------------------------
# Default draft handler — wires the actor's draft callback to streaming.
# ---------------------------------------------------------------------------


def make_default_draft_handler(
    proxy_for: Callable[[str], StreamingProxy],
    prompt_builder: "Callable[[str, ev.Event, RoomCoordinator], object]",
):
    """Create a draft handler bound to a proxy lookup + prompt builder.

    Used by :mod:`loom.runtime` wiring code to plug actors into streaming.
    ``proxy_for(participant_id)`` returns the appropriate
    :class:`StreamingProxy`. ``prompt_builder`` constructs the per-turn
    prompt; see :mod:`loom.kernel.prompt`.
    """

    def handler(actor, trigger, lease):
        proxy = proxy_for(actor.id)
        prompt = prompt_builder(actor.id, trigger, actor.coordinator)
        run_streaming_call(proxy, prompt, lease, actor.bus, actor.coordinator)

    return handler
