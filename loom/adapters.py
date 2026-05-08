"""Loom adapters — turn ordinary callables into :class:`loom.contracts.Agent`.

**Canonical reference for new adapter authors: start with
:func:`agent_from_send`.** If your LLM client exposes a
``send(prompt) -> str``-shaped method, you do not need to write a
proxy class — wrap it with this function and you're done.
``docs/writing-an-adapter.md`` walks through both the
:func:`agent_from_send` path and the custom-class path.

Three helpers cover the common shapes of an LLM client:

- :func:`agent_from_send`  for non-streaming ``send(prompt) -> str | obj``
  callables. The returned agent yields the full reply as a single chunk.
- :func:`agent_from_stream` for callables that already yield deltas.
- :func:`agent_from_object` for client objects exposing ``.stream`` or
  ``.send`` (auto-detected; ``.stream`` wins when both are present).

Every helper returns an object satisfying the :class:`loom.contracts.Agent`
protocol — ``id`` plus ``stream(prompt) -> Iterator[str]`` plus the
optional metadata attributes (``persona`` / ``capability_block`` /
``cost_tier`` / ``capable``). The room consumes those attributes when
converting an ``Agent`` into a :class:`loom.runtime.ParticipantWiring`.

These adapters are deliberately stdlib-only: no provider SDK
dependencies. Any caller that already has a richer streaming proxy
(e.g. one of the bundled provider proxies under :mod:`loom.chat.proxies`)
can feed it straight into a :class:`ParticipantWiring` without going
through these helpers — the agent abstraction is duck-typed.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Iterator, Optional


# ---------------------------------------------------------------------------
# Send → stream wrapper (canonical pattern for non-streaming clients)
# ---------------------------------------------------------------------------

class SendProxyAdapter:
    """Wrap any proxy with a ``send(prompt) -> str`` method into a streaming proxy.

    Yields the entire response as a single chunk. PASS detection still
    works (the buffer matches on the first 16 chars). True streaming
    is a v0.1 enhancement when proxies expose their own streaming.

    This class is the canonical pattern for adapter authors whose
    provider client only exposes a non-streaming ``send`` call. Most
    authors should reach for :func:`agent_from_send` instead — it
    builds an :class:`Agent` directly from a callable. Use this class
    when you have a richer object you want to wrap (e.g. preserving
    ``cancel()`` semantics or attaching extra metadata attributes).
    """

    def __init__(self, proxy: Any,
                 send_method: str = "send") -> None:
        self._proxy = proxy
        self._send_method = send_method
        self._cancelled = False

    def stream(self, prompt: str) -> Iterator[str]:
        if self._cancelled:
            return
        send = getattr(self._proxy, self._send_method)
        result = send(prompt)
        text = self._extract_text_static(result)
        if text:
            yield text

    def cancel(self) -> None:
        self._cancelled = True
        cancel = getattr(self._proxy, "cancel", None)
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass

    @staticmethod
    def _extract_text_static(result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        for attr in ("text", "body", "content", "output"):
            v = getattr(result, attr, None)
            if isinstance(v, str):
                return v
        return str(result)


def _extract_text(result: Any) -> str:
    """Pull a plain string out of a non-streaming ``send`` result.

    Accepts a string directly, or duck-types ``.text`` / ``.body`` /
    ``.content`` / ``.output``. Anything else falls back to ``str(result)``.
    Returns ``""`` for ``None`` so the caller can detect "no draft".
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    for attr in ("text", "body", "content", "output"):
        v = getattr(result, attr, None)
        if isinstance(v, str):
            return v
    return str(result)


class _FunctionAgent:
    """Concrete :class:`Agent` produced by the adapters.

    Holds the id, optional metadata, and a callable that knows how to
    produce text for a prompt. The same shape works for both send-based
    and stream-based callables — the adapters bind the right inner
    callable when constructing.
    """

    __slots__ = (
        "id", "persona", "capability_block", "cost_tier", "capable",
        "_stream_callable", "_cancel_callable", "_cancelled",
    )

    def __init__(
        self,
        agent_id: str,
        stream_callable: Callable[[object], Iterator[str]],
        *,
        persona: str = "",
        capability_block: str = "",
        cost_tier: int = 1,
        capable: bool = True,
        cancel_callable: Optional[Callable[[], None]] = None,
    ) -> None:
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("agent id must be a non-empty string")
        self.id = agent_id
        self.persona = persona
        self.capability_block = capability_block
        self.cost_tier = cost_tier
        self.capable = capable
        self._stream_callable = stream_callable
        self._cancel_callable = cancel_callable
        self._cancelled = False

    def stream(self, prompt: object) -> Iterator[str]:
        if self._cancelled:
            return
        for chunk in self._stream_callable(prompt):
            if self._cancelled:
                return
            if chunk:
                yield chunk

    def cancel(self) -> None:
        self._cancelled = True
        if self._cancel_callable is not None:
            try:
                self._cancel_callable()
            except Exception:
                # Best-effort; underlying client may not support cancel.
                pass


def agent_from_send(
    agent_id: str,
    send_fn: Callable[[object], Any],
    *,
    persona: str = "",
    capability_block: str = "",
    cost_tier: int = 1,
    capable: bool = True,
    cancel_fn: Optional[Callable[[], None]] = None,
) -> _FunctionAgent:
    """Wrap a non-streaming ``send(prompt) -> str | obj-with-.text``.

    The returned agent yields the full reply as a single chunk. PASS
    detection still works (the room's stream buffer matches against the
    first 16 chars). True streaming is opt-in via :func:`agent_from_stream`.

    ``send_fn`` may return a plain string or an object exposing one of
    ``.text`` / ``.body`` / ``.content`` / ``.output``.
    """
    if not callable(send_fn):
        raise TypeError("send_fn must be callable")

    def _stream(prompt: object) -> Iterator[str]:
        result = send_fn(prompt)
        text = _extract_text(result)
        if text:
            yield text

    return _FunctionAgent(
        agent_id, _stream,
        persona=persona, capability_block=capability_block,
        cost_tier=cost_tier, capable=capable,
        cancel_callable=cancel_fn,
    )


def agent_from_stream(
    agent_id: str,
    stream_fn: Callable[[object], Iterable[str]],
    *,
    persona: str = "",
    capability_block: str = "",
    cost_tier: int = 1,
    capable: bool = True,
    cancel_fn: Optional[Callable[[], None]] = None,
) -> _FunctionAgent:
    """Wrap a streaming callable that yields chunks for each prompt.

    The callable should be re-callable: every ``stream(prompt)`` call
    must produce a fresh iterable.
    """
    if not callable(stream_fn):
        raise TypeError("stream_fn must be callable")

    def _stream(prompt: object) -> Iterator[str]:
        for chunk in stream_fn(prompt):
            if isinstance(chunk, str):
                yield chunk
            elif chunk is None:
                continue
            else:
                yield str(chunk)

    return _FunctionAgent(
        agent_id, _stream,
        persona=persona, capability_block=capability_block,
        cost_tier=cost_tier, capable=capable,
        cancel_callable=cancel_fn,
    )


def agent_from_object(
    agent_id: str,
    obj: Any,
    *,
    persona: Optional[str] = None,
    capability_block: Optional[str] = None,
    cost_tier: Optional[int] = None,
    capable: Optional[bool] = None,
) -> _FunctionAgent:
    """Wrap an existing client object exposing ``.stream`` or ``.send``.

    Resolution order:

    1. If ``obj.stream`` is callable, wrap it with :func:`agent_from_stream`.
    2. Else if ``obj.send`` is callable, wrap it with :func:`agent_from_send`.
    3. Else raise :class:`TypeError`.

    Optional metadata kwargs override values pulled from ``obj`` (which
    are read via :func:`getattr` and fall through to the documented
    defaults if absent).

    ``obj.cancel`` (when present) is forwarded — best-effort.
    """
    stream_method = getattr(obj, "stream", None)
    send_method = getattr(obj, "send", None)

    resolved_persona = persona if persona is not None else getattr(
        obj, "persona", "")
    resolved_caps = capability_block if capability_block is not None else \
        getattr(obj, "capability_block", "")
    resolved_tier = cost_tier if cost_tier is not None else getattr(
        obj, "cost_tier", 1)
    resolved_capable = capable if capable is not None else getattr(
        obj, "capable", True)
    cancel_fn = getattr(obj, "cancel", None)
    if not callable(cancel_fn):
        cancel_fn = None

    if callable(stream_method):
        return agent_from_stream(
            agent_id, stream_method,
            persona=resolved_persona,
            capability_block=resolved_caps,
            cost_tier=resolved_tier,
            capable=resolved_capable,
            cancel_fn=cancel_fn,
        )
    if callable(send_method):
        return agent_from_send(
            agent_id, send_method,
            persona=resolved_persona,
            capability_block=resolved_caps,
            cost_tier=resolved_tier,
            capable=resolved_capable,
            cancel_fn=cancel_fn,
        )
    raise TypeError(
        f"agent_from_object: {type(obj).__name__} exposes neither "
        ".stream(prompt) nor .send(prompt)")
