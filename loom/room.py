"""Loom room — public-facing facade over :mod:`loom.runtime`.

:class:`LoomRoom` is the main entry point for consumers. It accepts a
list of :class:`loom.contracts.Agent` objects (built directly or via the
adapters in :mod:`loom.adapters`), wraps them into
:class:`ParticipantWiring` records, and drives a fully-running
:class:`LoomSession` underneath. The constructor uses sensible defaults
so that a four-line "hello world" works:

.. code-block:: python

    from loom import LoomRoom, agent_from_send, OpenChatPolicy

    room = LoomRoom(
        agents=[
            agent_from_send("gpt", openai_send),
            agent_from_send("claude", anthropic_send),
        ],
        policy=OpenChatPolicy(),
    )
    with room:
        result = room.post_and_wait("hello, what do you all think?")
        room.add_agent(agent_from_send("llama", llama_send))
        room.remove_agent("gpt")

The ``with room:`` block (or an explicit ``room.start()`` /
``room.stop()`` pair) is **required** — actor threads only start on
``__enter__``. Posting to a room that hasn't started times out
silently because no actor is listening.

Sensible defaults chosen for v0.1:

- ``policy``             :class:`DefaultPolicy` (v0.0 floor-aware classifier).
  Pass :class:`OpenChatPolicy` for the simplest broadcast behavior, or
  one of :class:`SingleResponderPolicy` / :class:`RoundRobinPolicy`.
- ``policy_error_mode``  ``"close_turn"`` (fail-closed; library default).
  Loom passes ``"default_responder"`` for v0.0 behavior compat.
- ``anchor_id``          first agent's id, when not specified.
- ``default_responder_id``  ``None`` — only used by the
  ``policy_error_mode="default_responder"`` fallback path.
- ``topic``              ``None`` — set later via the planned
  :meth:`set_topic` facade method or the ``/topic`` slash command.
- ``journal_dir``        ``None`` — no audit trail unless requested.
- ``room_config``        :class:`RoomConfig`'s defaults (throttle,
  budget, loop guard, body-size cap).
- ``notify``             :func:`_thread_safe_print` — wraps :func:`print`
  in a module-level :class:`threading.Lock` so concurrent actor threads
  don't shear streamed output.
- ``prompt_fn``          ``lambda: input("you ▸ ")``.

The room is thread-safe: all facade methods may be called from any
thread, including actor threads, the runtime thread, and external
consumer threads. The underlying :class:`LoomSession` serializes
state transitions internally.

The room does not introduce its own state — every kernel concept
(bus, coordinator, journal, policy) lives on the underlying
:class:`LoomSession` and is reachable via :attr:`session` for advanced
consumers (escape hatch; not part of the supported facade).
"""
from __future__ import annotations

import difflib
import warnings
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
import threading

from loom.contracts import Agent, ConversationPolicy
from loom.kernel import events as _ev
from loom.kernel.events import Event
from loom.kernel.obligations import plan_for_default
from loom.kernel.room import RoomConfig, StyleLevel
from loom.messages import Message, TurnResult, _project_closure_reason
from loom.runtime import (
    LoomSession,
    ParticipantWiring,
    SendProxyAdapter,
    _make_console_subscriber,
    build_loom_session,
    handle_slash_command,
    post_user_text,
)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_NOTIFY_LOCK = threading.Lock()

# Recognised optional ``Agent`` attribute names. ``_agent_to_wiring``
# walks an agent's ``dir()`` and warns when an attribute is a near-miss
# (typo) on one of these — silently falling back to the default value
# is a known footgun (audit F3.2 / P0.7).
_KNOWN_OPTIONAL_AGENT_ATTRS = (
    "persona",
    "capability_block",
    "cost_tier",
    "capable",
    "cancel",
    "id",
    "stream",
    "send",
)


def _thread_safe_print(msg: str) -> None:
    """Default :func:`print` wrapper safe for concurrent callers.

    Actor threads call ``notify`` from arbitrary moments; without a
    lock, two streams can interleave mid-line and produce sheared
    output. This wrapper serializes calls behind a module-level lock.
    """
    with _NOTIFY_LOCK:
        print(msg)


def _default_prompt() -> str:
    return input("you ▸ ")


# ---------------------------------------------------------------------------
# Agent → ParticipantWiring
# ---------------------------------------------------------------------------

def _warn_on_typoed_agent_attrs(agent: Agent) -> None:
    """Warn when an agent has attribute names that look like typos.

    F3.2 / P0.7 — silently falling back to defaults when the user spells
    ``personality`` instead of ``persona`` is a known footgun. This
    function uses :func:`difflib.get_close_matches` to spot near-misses
    on the documented optional attribute names and emits a
    :class:`UserWarning` so the typo is loud.

    Skips dunders, private attrs, and exact matches. Subclasses /
    duck-typed objects with extra unrelated attrs (``client``,
    ``model``, ...) won't trip this — only attrs that look close
    enough to a known optional that ``difflib`` ranks them as a match.
    """
    candidates: set[str] = set()
    for name in dir(agent):
        if name.startswith("_"):
            continue
        if name in _KNOWN_OPTIONAL_AGENT_ATTRS:
            continue
        # Cheap pre-filter: only consider attrs short enough to plausibly
        # be a typo (max length is "capability_block" + 4 = 20).
        if len(name) > 24:
            continue
        candidates.add(name)

    for cand in candidates:
        # Cutoff 0.75 catches ``personality`` (0.778 vs ``persona``) and
        # ``cost_tiers`` (0.947 vs ``cost_tier``); empirically does not
        # match common adapter attrs (``model``, ``client``, ``api_key``,
        # ``temperature``, ``max_tokens``, ...).
        matches = difflib.get_close_matches(
            cand, _KNOWN_OPTIONAL_AGENT_ATTRS, n=1, cutoff=0.75)
        if not matches:
            continue
        warnings.warn(
            f"Agent {getattr(agent, 'id', '?')!r}: attribute "
            f"{cand!r} looks like a typo of {matches[0]!r} (an Loom "
            f"optional). The expected attribute will fall back to its "
            f"default value, which may not be what you meant.",
            UserWarning,
            stacklevel=3,
        )


def _agent_to_wiring(agent: Agent) -> ParticipantWiring:
    """Convert an :class:`Agent` into a :class:`ParticipantWiring`.

    Reads optional metadata via :func:`getattr` with documented
    defaults. The agent's :meth:`stream` makes it a valid
    :class:`StreamingProxy` already, so it's used as the proxy
    directly — no extra wrapping unless ``stream`` is missing (in
    which case ``send`` is wrapped via :class:`SendProxyAdapter`).

    Warns (UserWarning) if any agent attribute name looks like a typo
    of a known optional (``persona``, ``capability_block``,
    ``cost_tier``, ``capable``, ``cancel``).
    """
    if not hasattr(agent, "id") or not isinstance(agent.id, str) \
            or not agent.id:
        raise TypeError(
            "Agent must have a non-empty string ``id`` attribute")

    stream_method = getattr(agent, "stream", None)
    if callable(stream_method):
        proxy: Any = agent
    else:
        send_method = getattr(agent, "send", None)
        if not callable(send_method):
            raise TypeError(
                f"Agent {agent.id!r} exposes neither stream() nor send()")
        proxy = SendProxyAdapter(agent, send_method="send")

    _warn_on_typoed_agent_attrs(agent)

    return ParticipantWiring(
        id=agent.id,
        proxy=proxy,
        persona=getattr(agent, "persona", ""),
        capability_block=getattr(agent, "capability_block", ""),
        cost_tier=int(getattr(agent, "cost_tier", 1)),
        capable=bool(getattr(agent, "capable", True)),
    )


# ---------------------------------------------------------------------------
# LoomRoom
# ---------------------------------------------------------------------------

class LoomRoom:
    """Public-facing room facade.

    Wraps an :class:`LoomSession` with a small surface for the common
    cases. Construct, ``start()`` (or use as a context manager), then
    drive with :meth:`post`, :meth:`post_and_wait`, or
    :meth:`run_console`. Membership can be changed at runtime via
    :meth:`add_agent` / :meth:`remove_agent`.

    Lifecycle (mandatory):

    - ``with room:``  — actor threads start on ``__enter__``, stop on
      ``__exit__``. The recommended path. **Required** — calling
      :meth:`post` / :meth:`post_and_wait` on an un-started room
      returns nothing because no actor is listening.
    - ``room.start()`` / ``room.stop()`` — explicit pair, equivalent
      to the context manager. Idempotent.

    Method overview:

    - :meth:`post(text)`           — fire-and-forget; returns the
      bus event id.
    - :meth:`post_and_wait(text)`  — block until the turn closes.
      Returns a typed :class:`~loom.messages.TurnResult`.
    - :meth:`dm(target, text)`     — direct message to one participant.
    - :meth:`add_agent(agent)`     — register a new participant
      mid-session.
    - :meth:`remove_agent(id)`     — stop the actor + unregister.
    - :meth:`run_console()`        — interactive REPL.
    - :attr:`participants`         — sorted list of participant ids.
    - :attr:`topic`                — current room topic.
    - :attr:`session`              — underlying :class:`LoomSession`
      (escape hatch).

    Room-control facade methods (typed equivalents of the slash
    commands; library-author UIs should prefer these over
    :func:`handle_slash_command`):

    - :meth:`set_topic(text)`             — set or clear the topic.
    - :meth:`set_anchor(pid)`             — set the anchor participant.
    - :meth:`set_default_responder(pid)`  — set the policy fallback.
    - :meth:`set_roles({pid: role})`      — replace the role map.
    - :meth:`set_floor([pid, ...])`       — narrow / open the floor.
    - :meth:`set_style(level)`            — brief | normal | detailed.
    - :meth:`cancel_turn()`               — cancel the open user turn.

    Slash commands (still available via :func:`run_console` for
    interactive use): ``/topic``, ``/who``, ``/dm``, ``/anchor``,
    ``/responder``, ``/roles``, ``/floor``, ``/release``, ``/quiet``,
    ``/goal``, ``/brief`` / ``/normal`` / ``/detailed``, ``/control``,
    ``/cancel``, ``/leave``.
    """

    def __init__(
        self,
        agents: Iterable[Agent],
        *,
        topic: Optional[str] = None,
        anchor_id: Optional[str] = None,
        default_responder_id: Optional[str] = None,
        journal_dir: Optional[str | Path] = None,
        policy: Optional[ConversationPolicy] = None,
        policy_error_mode: str = "close_turn",
        room_config: Optional[RoomConfig] = None,
    ) -> None:
        """Build a room. Use ``with room:`` (or ``room.start()``) to run it.

        ``agents`` must be a non-empty iterable of objects satisfying
        :class:`loom.contracts.Agent` — that is, they have a non-empty
        string ``id`` plus a ``stream(prompt) -> Iterator[str]``
        method. Optional metadata (``persona``, ``capability_block``,
        ``cost_tier``, ``capable``, ``cancel``) is read via
        :func:`getattr` with documented defaults; misspelled
        attributes silently fall back. Use :func:`agent_from_send` /
        :func:`agent_from_stream` / :func:`agent_from_object` to wrap
        ordinary callables into agents.

        ``policy`` defaults to :class:`DefaultPolicy` (v0.0
        floor-aware classifier); pass :class:`OpenChatPolicy` for
        simple broadcast, :class:`SingleResponderPolicy(responder_id)`
        for one-to-one, or :class:`RoundRobinPolicy(order)` for
        rotation.

        Raises :class:`ValueError` if ``agents`` is empty, if any agent
        id is duplicated, or if ``anchor_id`` /
        ``default_responder_id`` references an unknown participant.
        Raises :class:`TypeError` if any agent fails the structural
        ``Agent`` check (no ``stream``, no ``send``, no string ``id``).
        """
        agent_list = list(agents)
        if not agent_list:
            raise ValueError("LoomRoom requires at least one agent")
        seen_ids: set[str] = set()
        wirings: list[ParticipantWiring] = []
        for a in agent_list:
            wiring = _agent_to_wiring(a)
            if wiring.id in seen_ids:
                raise ValueError(
                    f"duplicate agent id: {wiring.id!r}")
            seen_ids.add(wiring.id)
            wirings.append(wiring)

        resolved_anchor = anchor_id or agent_list[0].id

        self._journal_dir = Path(journal_dir) if journal_dir else None
        self._session: LoomSession = build_loom_session(
            wirings,
            config=room_config,
            default_responder_id=default_responder_id,
            anchor_id=resolved_anchor,
            topic=topic,
            journal_dir=self._journal_dir,
            auto_start=False,
            policy=policy,
            policy_error_mode=policy_error_mode,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start actor threads. Idempotent."""
        self._session.start()

    def stop(self, *, timeout: float = 1.0) -> None:
        """Stop actor threads, close journal, stop bus. Idempotent."""
        self._session.stop(timeout=timeout)

    def __enter__(self) -> "LoomRoom":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def add_agent(self, agent: Agent) -> None:
        """Add an agent mid-session and start its actor immediately."""
        self._session.add_agent(_agent_to_wiring(agent))

    def remove_agent(self, agent_id: str) -> None:
        """Remove an agent + stop its actor.

        Re-resolves slot pointers and unblocks any open obligation it
        held. Raises :class:`KeyError` if ``agent_id`` is not in the room.
        """
        self._session.remove_agent(agent_id)

    @property
    def participants(self) -> list[str]:
        """Sorted list of participant ids currently in the room."""
        return sorted(self._session.state.participants.keys())

    @property
    def topic(self) -> Optional[str]:
        return self._session.state.topic

    @property
    def session(self) -> LoomSession:
        """Underlying :class:`LoomSession` — for advanced consumers."""
        return self._session

    @property
    def journal_dir(self) -> Optional[Path]:
        return self._journal_dir

    # ------------------------------------------------------------------
    # Posting messages
    # ------------------------------------------------------------------

    def post(self, text: str, *, channel: str = "main") -> int:
        """Post a user message. Returns the bus event id (non-blocking).

        ``user_turn_id`` for the opened turn (when one opens) is
        accessible via :attr:`session.coordinator.user_turn` immediately
        afterwards. For a blocking call that returns committed replies,
        use :meth:`post_and_wait`.
        """
        if not text:
            raise ValueError("post() requires a non-empty text message")
        event = post_user_text(self._session, text, channel=channel)
        return event.id

    def post_and_wait(
        self,
        text: str,
        *,
        timeout: float = 30.0,
        channel: str = "main",
    ) -> TurnResult:
        """Post a user message and block until the turn closes.

        Returns a :class:`~loom.messages.TurnResult` with the committed
        replies (in posted order, excluding the user's own message),
        the kernel turn id, the policy ``routing_case``, and a
        ``closed_reason`` indicating why the turn ended.

        ``TurnResult`` is iterable over its ``messages`` list, so
        ``for r in result`` and ``{r.sender for r in result}`` keep
        working at the call site.

        On timeout, returns whatever has accumulated so far without
        raising — callers who care about the difference inspect
        :attr:`TurnResult.closed_reason` (``"timeout"`` vs
        ``"all_obligations_resolved"``).

        If the policy classified the message as no-response (e.g.
        ``thanks``), no turn opens; the result has empty ``messages``,
        ``turn_id == -1``, and ``closed_reason == "no_turn_opened"``.
        """
        if not text:
            raise ValueError(
                "post_and_wait() requires a non-empty text message")

        bus = self._session.bus
        # Snapshot bus length BEFORE posting so the wait loop measures
        # against the pre-post cursor. After post_user_text the bus has
        # grown by 1 (user chat) + N (control events for opened turn).
        last_seen_len = len(bus)

        started_at = self._monotonic()
        event = post_user_text(self._session, text, channel=channel)
        ut = self._session.coordinator.user_turn
        if ut is None or ut.user_event_id != event.id:
            # Policy returned an acknowledgement plan — no turn opened.
            return TurnResult(
                messages=[],
                turn_id=-1,
                routing_case="acknowledgement",
                closed_reason="no_turn_opened",
                participant_responses={},
                elapsed_s=self._monotonic() - started_at,
            )
        target_turn_id = ut.id
        routing_case = getattr(ut.frozen_plan, "routing_case", "")

        deadline = started_at + timeout
        timed_out = True
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            new_len = bus.wait_after(last_seen_len, timeout=remaining)
            if new_len <= last_seen_len:
                # Timed out (or bus stopped) with no new events.
                break
            last_seen_len = new_len
            ut = self._session.coordinator.user_turn
            if ut is None or ut.id != target_turn_id \
                    or ut.state != "open":
                timed_out = False
                break  # turn closed (or replaced)

        # Collect chat events for this turn.
        messages: list[Message] = []
        for ev in bus.snapshot():
            if ev.kind != "chat" or ev.sender == "user":
                continue
            if ev.user_turn_id != target_turn_id:
                continue
            if channel == "main" and ev.channel != "main":
                continue
            messages.append(Message.from_event(ev))

        # Resolve closure reason: prefer the kernel's recorded reason
        # when the turn closed; fall back to "timeout" if the wait
        # loop exited without observing closure.
        ut_final = self._session.coordinator.user_turn
        if timed_out:
            closed_reason: str = "timeout"
        elif ut_final is not None and ut_final.id == target_turn_id:
            closed_reason = _project_closure_reason(ut_final.closure_reason)
        else:
            # User-turn slot has been replaced (new_user_post path).
            closed_reason = "new_user_post"

        responses: dict[str, list[Message]] = {}
        for m in messages:
            responses.setdefault(m.sender, []).append(m)

        return TurnResult(
            messages=messages,
            turn_id=target_turn_id,
            routing_case=routing_case,
            closed_reason=closed_reason,
            participant_responses=responses,
            elapsed_s=self._monotonic() - started_at,
        )

    @staticmethod
    def _monotonic() -> float:
        # Indirected so tests can patch it.
        import time
        return time.monotonic()

    def dm(
        self,
        participant_id: str,
        text: str,
    ) -> int:
        """Send a direct message to ``participant_id``.

        Posts a chat event on channel ``dm:<participant_id>`` with the
        recipient as the sole addressee, then opens a single-responder
        turn so the target is required to reply. Other participants
        do not see the message.

        Returns the bus event id of the posted message. Raises
        :class:`KeyError` if ``participant_id`` is not in the room.
        """
        if not text:
            raise ValueError("dm() requires non-empty text")
        state = self._session.state
        if participant_id not in state.participants:
            raise KeyError(
                f"unknown participant: {participant_id!r}; "
                f"members: {sorted(state.participants)}")
        e = _ev.chat(
            sender="user", body=text,
            addressees=[participant_id],
            channel=f"dm:{participant_id}",
            room_epoch=state.room_epoch,
        )

        def _dm_plan(posted_event: Event):
            return plan_for_default(
                participant_id, reason="dm",
                target_event_ids=[posted_event.id],
                rationale="direct DM",
            )

        self._session.coordinator.post_user_event_and_open_turn(e, _dm_plan)
        return e.id

    # ------------------------------------------------------------------
    # Room control — typed facade methods (P1.3)
    #
    # These mirror the slash-command set but with typed Python args and
    # validation. Library-author UIs should prefer these over routing
    # through ``handle_slash_command``. Every method validates inputs
    # locally with a teaching error (audit principle 3.7) and delegates
    # to the coordinator.
    # ------------------------------------------------------------------

    _MAX_TOPIC_CHARS = 500

    def _require_participant(self, pid: str) -> None:
        members = self._session.state.participants
        if pid not in members:
            raise KeyError(
                f"unknown participant: {pid!r}; "
                f"members: {sorted(members)}")

    def set_topic(self, topic: Optional[str]) -> None:
        """Set or clear the room topic. Pass ``None`` (or ``""``) to clear.

        Caps at 500 chars; raises :class:`ValueError` past that.
        """
        if topic is not None and len(topic) > self._MAX_TOPIC_CHARS:
            raise ValueError(
                f"topic too long ({len(topic)} > "
                f"{self._MAX_TOPIC_CHARS} chars)")
        self._session.coordinator.set_topic(topic or None)

    def set_anchor(self, participant_id: str) -> None:
        """Set the anchor (synthesizer) participant."""
        self._require_participant(participant_id)
        self._session.coordinator.set_anchor(participant_id)

    def set_default_responder(self, participant_id: str) -> None:
        """Set the default responder used when the policy falls back."""
        self._require_participant(participant_id)
        self._session.coordinator.set_default_responder(participant_id)

    def set_roles(self, roles: dict[str, str]) -> None:
        """Replace the role map. Pass ``{}`` to clear all roles.

        Each key must be a member of the room. Unknown ids raise
        :class:`KeyError`.
        """
        for pid in roles:
            self._require_participant(pid)
        self._session.coordinator.set_roles(dict(roles))

    def set_floor(
        self,
        participant_ids: Optional[Iterable[str]],
    ) -> None:
        """Set or clear the floor owner(s).

        Pass ``None`` or an empty iterable to open the floor. Otherwise
        each id must be a current participant.
        """
        if participant_ids is None:
            self._session.coordinator.set_floor_owner(None)
            return
        ids = list(participant_ids)
        if not ids:
            self._session.coordinator.set_floor_owner(None)
            return
        for pid in ids:
            self._require_participant(pid)
        self._session.coordinator.set_floor_owner(ids)

    def set_style(self, style: StyleLevel) -> None:
        """Update brevity preference. One of ``"brief" | "normal" | "detailed"``."""
        if style not in ("brief", "normal", "detailed"):
            raise ValueError(
                f"style must be one of 'brief', 'normal', 'detailed'; "
                f"got {style!r}")
        self._session.coordinator.set_style(style)

    def cancel_turn(self) -> None:
        """Cancel the currently open user turn (if any)."""
        self._session.coordinator.close_user_turn("cancelled")

    # ------------------------------------------------------------------
    # Console
    # ------------------------------------------------------------------

    def run_console(
        self,
        *,
        prompt_fn: Optional[Callable[[], str]] = None,
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Run an interactive REPL bound to ``prompt_fn`` / ``notify``.

        Both callables default to stdlib (``input`` + a thread-safe
        ``print``). Loom passes its own ``prompt_toolkit`` /
        ``rich``-backed implementations.

        ``EOFError`` / ``KeyboardInterrupt`` from ``prompt_fn`` exit
        cleanly; ``/leave`` / ``/quit`` / ``/exit`` slash commands also
        exit. The room is :meth:`stop`'d on exit.
        """
        if prompt_fn is None:
            prompt_fn = _default_prompt
        if notify is None:
            notify = _thread_safe_print

        # Start actor threads if not already running.
        self.start()

        unsubscribe = self._session.bus.subscribe(
            _make_console_subscriber(notify))
        try:
            while True:
                try:
                    text = prompt_fn()
                except (EOFError, KeyboardInterrupt):
                    break
                text = text.strip()
                if not text:
                    continue
                if text.startswith("/"):
                    result = handle_slash_command(
                        text, self._session, console=notify)
                    if result.message:
                        notify(result.message)
                    if result.quit:
                        break
                    continue
                self.post(text)
        finally:
            unsubscribe()
            self.stop()
