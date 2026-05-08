"""Loom runtime — session bootstrap + console loop.

Exposes :func:`run_loom_console` which wires the protocol pieces
together and runs the live group-chat loop. Designed to be called from
a slash-command handler; no hard dependency on the existing ``loom.ui``
plumbing beyond a minimal ``Console.input``-style prompt source.

The runtime accepts ``ParticipantWiring`` records — one per agent —
which pair a participant id with a streaming-capable proxy. For
existing non-streaming proxies use :class:`SendProxyAdapter` to wrap
their ``send()`` method into the streaming protocol.

The room is parameterized by a :class:`loom.contracts.ConversationPolicy`.
Pass a custom policy to :func:`build_loom_session` (or
:func:`run_loom_console`) to swap routing rules; the kernel itself does
not change. The default is :class:`loom.policy.default.DefaultPolicy`,
which preserves v0.0 behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
import re
import threading

from loom.adapters import SendProxyAdapter
from loom.contracts import ConversationPolicy
from loom.kernel import events as ev
from loom.kernel.actor import ParticipantActor
from loom.kernel.addressees import parse_addressees
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.events import Event
from loom.kernel.journal import Journal
from loom.kernel.obligations import plan_for_default
from loom.kernel.prompt import build_prompt
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
)
from loom.kernel.streaming import StreamingProxy, run_streaming_call
from loom.policy.default import DefaultPolicy


# ---------------------------------------------------------------------------
# Adapters — :class:`SendProxyAdapter` lives in :mod:`loom.adapters`
# (P1.4); re-imported above for backwards compatibility with callers
# that still do ``from loom.runtime import SendProxyAdapter``.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

@dataclass
class ParticipantWiring:
    """One entry per Loom participant in a session."""
    id: str
    proxy: StreamingProxy
    persona: str = ""
    capability_block: str = ""
    cost_tier: int = 1
    capable: bool = True


@dataclass
class LoomSession:
    """Live Loom session handle. Returned by :func:`build_loom_session`.

    The ``wirings`` dict and ``actors`` list are session-owned mutable
    registries. :meth:`add_agent` / :meth:`remove_agent` expose dynamic
    membership management — wiring up a fresh proxy + actor at runtime
    or unregistering an existing one. Both are safe to call after
    :func:`build_loom_session` returns and before :meth:`stop`.
    """
    bus: MessageBus
    state: RoomState
    coordinator: RoomCoordinator
    journal: Optional[Journal]
    actors: list[ParticipantActor]
    wirings: dict[str, ParticipantWiring]
    # The policy is the per-room extension boundary — every classification
    # of a user message goes through ``policy.plan_user_turn``. Stored on
    # the session so post-user-text helpers + the draft handler can both
    # see it without an extra threading.
    policy: ConversationPolicy = field(default_factory=DefaultPolicy)
    _stop_event: threading.Event = field(default_factory=threading.Event)
    # ``_draft_handler`` is the closure shared across actors — set by
    # :func:`build_loom_session` so :meth:`add_agent` can wire new actors
    # against the same closure (which closes over ``self.wirings`` by
    # reference, picking up new entries on every dispatch).
    _draft_handler: Optional[Callable] = field(default=None, repr=False)
    _started: bool = field(default=False, repr=False)
    _membership_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False)

    def add_agent(self, wiring: ParticipantWiring) -> None:
        """Register a fresh participant + start its actor thread.

        Safe to call mid-session. The actor begins processing bus events
        from the current bus tail; historical events for which the new
        participant had no obligation are SKIPped.

        Raises :class:`ValueError` if ``wiring.id`` is already present,
        :class:`RuntimeError` if the session has been stopped.
        """
        if self._stop_event.is_set():
            raise RuntimeError("session is stopped")
        if self._draft_handler is None:
            raise RuntimeError(
                "session was constructed without a draft handler; "
                "use build_loom_session() to create sessions")
        with self._membership_lock:
            if wiring.id in self.wirings:
                raise ValueError(
                    f"participant {wiring.id!r} already in room")
            # Order: wire the proxy first (so the closure can find it),
            # then register kernel-side, then spin up the actor.
            self.wirings[wiring.id] = wiring
            self.coordinator.register_participant(ParticipantInfo(
                id=wiring.id, capable=wiring.capable,
                cost_tier=wiring.cost_tier, active=True,
            ))
            actor = ParticipantActor(
                wiring.id, self.bus, self.coordinator, self._draft_handler)
            self.actors.append(actor)
            if self._started:
                actor.start()

    def remove_agent(self, agent_id: str, *,
                     actor_stop_timeout: float = 0.5) -> None:
        """Stop the actor + unregister the participant.

        Idempotent for a non-existent id only via :class:`KeyError` —
        callers are expected to know the participant existed.

        The kernel re-resolves slot pointers (anchor / default-responder /
        chair / summarizer), invalidates any in-flight leases, marks
        the participant's open obligations resolved-administratively,
        and dead-letters any pending direct mentions. Turn closure is
        unblocked if this was the last unresolved required participant.
        """
        with self._membership_lock:
            actor = next(
                (a for a in self.actors if a.id == agent_id), None)
            if actor is None and agent_id not in self.wirings \
                    and agent_id not in self.state.participants:
                raise KeyError(f"unknown participant: {agent_id}")
            if actor is not None:
                actor.stop(timeout=actor_stop_timeout)
                self.actors = [a for a in self.actors if a.id != agent_id]
            self.coordinator.unregister_participant(agent_id)
            self.wirings.pop(agent_id, None)

    def start(self) -> None:
        """Start any actor threads that are not yet running.

        Idempotent: re-calling does nothing. Use this when the session
        was constructed with ``auto_start=False`` and the caller
        controls when to start the actor pool.
        """
        if self._stop_event.is_set():
            raise RuntimeError("session is stopped; cannot restart")
        with self._membership_lock:
            for a in self.actors:
                a.start()  # ParticipantActor.start is idempotent
            self._started = True

    def stop(self, *, timeout: float = 1.0) -> None:
        """Signal shutdown: stop actors, close journal, stop bus."""
        self._stop_event.set()
        for a in list(self.actors):
            a.stop(timeout=timeout)
        if self.journal is not None:
            try:
                self.journal.snapshot(self.state)
            except Exception:
                pass
            self.journal.close()
        self.bus.stop()


def _make_draft_handler(wirings: dict[str, ParticipantWiring],
                        policy: ConversationPolicy):
    def handler(actor, trigger, lease):
        wiring = wirings[actor.id]
        prompt = build_prompt(
            actor.id, trigger, actor.coordinator,
            persona=wiring.persona,
            capability_block=wiring.capability_block,
            policy=policy,
        )
        run_streaming_call(
            wiring.proxy, prompt, lease,
            actor.bus, actor.coordinator,
        )
    return handler


def build_loom_session(
    wirings: list[ParticipantWiring],
    *,
    config: Optional[RoomConfig] = None,
    default_responder_id: Optional[str] = None,
    anchor_id: Optional[str] = None,
    topic: Optional[str] = None,
    journal_dir: Optional[str | Path] = None,
    auto_start: bool = True,
    policy: Optional[ConversationPolicy] = None,
    policy_error_mode: str = "close_turn",
) -> LoomSession:
    """Construct a running :class:`LoomSession`.

    Returns a session with actors started (unless ``auto_start=False``)
    and the journal open. The caller drives input via the bus
    (``post_user_text``).

    ``policy`` is the room's :class:`ConversationPolicy`. Defaults to a
    fresh :class:`DefaultPolicy` (v0.0 behavior).

    ``policy_error_mode`` is forwarded to the coordinator. Library
    default is ``"close_turn"`` (fail-closed); Loom passes
    ``"default_responder"`` for v0.0 behavioral compatibility.
    """
    cfg = config or RoomConfig()
    bus = MessageBus()
    state = RoomState(config=cfg)
    coord = RoomCoordinator(bus, state, policy_error_mode=policy_error_mode)

    if policy is None:
        policy = DefaultPolicy()

    journal = None
    if journal_dir is not None:
        journal = Journal(journal_dir)
        journal.open()
        bus.subscribe(journal.on_event)
        # Periodic snapshots: return a dict so the journal's background
        # writer thread does the slow disk work without blocking the
        # post path. Synchronous shutdown still goes through
        # ``journal.snapshot(state)`` directly in :meth:`LoomSession.stop`.
        journal.set_snapshot_due_callback(
            lambda: Journal._state_to_dict(state)  # type: ignore[arg-type]
        )

        # Surface write failures as a ``journal_error`` control event so
        # consumers can observe the degraded state. The journal's
        # internal recursion guard prevents re-entry if posting the
        # error event itself triggers another failed write.
        #
        # P1 / sender authentication: this callback runs on whichever
        # thread tripped the failed write — almost always an actor's
        # bound thread (the actor's ``bus.post(chat_event)`` ran the
        # journal subscriber inline). ``post_internal`` bypasses the
        # thread-actor binding check; ``journal_error`` is privileged
        # kernel emission with sender="system".
        def _on_journal_failure(exc: Exception) -> None:
            bus.post_internal(ev.journal_error(
                exception_class=type(exc).__name__,
                message=str(exc)[:500],
            ))

        journal.set_failure_callback(_on_journal_failure)

        # P2.3: surface a ``snapshot_dropped`` control event when the
        # bounded snapshot queue had to discard the oldest pending
        # write. ``post_internal`` because the callback runs on the
        # post thread (likely a bound actor thread) and the event has
        # sender="system".
        def _on_snapshot_drop(total: int, depth: int) -> None:
            bus.post_internal(ev.snapshot_dropped(
                dropped_total=total, queue_depth=depth))

        journal.set_snapshot_drop_callback(_on_snapshot_drop)

    by_id: dict[str, ParticipantWiring] = {}
    for w in wirings:
        coord.register_participant(ParticipantInfo(
            id=w.id, capable=w.capable,
            cost_tier=w.cost_tier, active=True,
        ))
        by_id[w.id] = w

    # Phase 0 audit: fail-loud if the wiring code passes a default
    # responder or anchor id that isn't in the registered participant
    # set. Without this check a typo silently sets the slot to a
    # nonexistent id and the room enters a state where no actor
    # matches the responder, breaking obligations.
    if default_responder_id is not None:
        if default_responder_id not in by_id:
            raise ValueError(
                f"default_responder_id {default_responder_id!r} is not "
                f"a registered participant; known ids: "
                f"{sorted(by_id)}")
        coord.set_default_responder(default_responder_id)
    if anchor_id is not None:
        if anchor_id not in by_id:
            raise ValueError(
                f"anchor_id {anchor_id!r} is not a registered "
                f"participant; known ids: {sorted(by_id)}")
        coord.set_anchor(anchor_id)
    if topic is not None:
        coord.set_topic(topic)

    handler = _make_draft_handler(by_id, policy)
    actors = [
        ParticipantActor(w.id, bus, coord, handler)
        for w in wirings
    ]
    if auto_start:
        for a in actors:
            a.start()

    return LoomSession(
        bus=bus, state=state, coordinator=coord,
        journal=journal, actors=actors, wirings=by_id,
        policy=policy,
        _draft_handler=handler,
        _started=auto_start,
    )


# ---------------------------------------------------------------------------
# Slash command parser
# ---------------------------------------------------------------------------

_SLASH_RE = re.compile(r"^/(\w+)(?:\s+(.*))?$")


@dataclass
class SlashResult:
    handled: bool
    quit: bool = False
    message: Optional[str] = None


def handle_slash_command(text: str, session: LoomSession,
                         *, console=None) -> SlashResult:
    """Interpret a single ``/<cmd> [args]`` line against ``session``.

    Returns ``handled=True`` if recognized, with ``quit=True`` for
    ``/leave`` / ``/quit``. Unknown commands return ``handled=False`` so
    the caller can decide whether to forward them as user input.
    """
    m = _SLASH_RE.match(text.strip())
    if not m:
        return SlashResult(handled=False)
    cmd = m.group(1).lower()
    args = (m.group(2) or "").strip()
    coord = session.coordinator
    state = session.state

    if cmd in ("leave", "quit", "exit"):
        return SlashResult(handled=True, quit=True,
                           message="leaving session")

    if cmd == "who":
        ps = sorted(state.participants.keys())
        ctl = state.control
        floor = (", ".join(ctl.floor_owner) if ctl.floor_owner
                 else "(open)")
        bits = [f"members: {', '.join(ps)}",
                f"topic: {state.topic or '(none)'}",
                f"floor: {floor}",
                f"style: {ctl.style}"]
        if ctl.roles:
            bits.append("roles: " + ", ".join(
                f"{pid}={role}" for pid, role in sorted(ctl.roles.items())))
        return SlashResult(handled=True, message=" | ".join(bits))

    if cmd == "mode":
        return SlashResult(
            handled=True,
            message=("/mode is removed in Loom v0 — group chat is the only "
                     "behavior. Use /responder to change the default "
                     "responder, /add /remove to manage members."),
        )

    if cmd == "topic":
        # Phase 0 audit: cap topic length at the slash-command entry.
        # The kernel's prompt-side fence (P0.8) prevents injection; the
        # cap prevents prompt-bloat / memory DoS from a 1 MB /topic
        # paste. 500 chars is generous for a natural-language topic.
        if args and len(args) > 500:
            return SlashResult(
                handled=True,
                message=(f"/topic argument too long "
                         f"({len(args)} > 500 chars)"),
            )
        coord.set_topic(args or None)
        return SlashResult(
            handled=True,
            message=f"topic → {args or '(cleared)'}",
        )

    if cmd == "add":
        # /add via slash command can't construct a proxy from a name —
        # programmatic add is the supported path. Direct callers to
        # ``session.add_agent(wiring)`` or ``room.add_agent(agent)``.
        return SlashResult(
            handled=True,
            message=("/add via slash command is not supported — proxies "
                     "must be wired in code. Use "
                     "session.add_agent(ParticipantWiring(...)) or "
                     "room.add_agent(agent_from_send(...))."),
        )

    if cmd == "remove":
        if not args:
            return SlashResult(handled=True, message="usage: /remove <id>")
        if args not in state.participants:
            return SlashResult(handled=True,
                               message=f"{args} not in room")
        try:
            session.remove_agent(args)
        except KeyError as exc:
            return SlashResult(handled=True, message=str(exc))
        return SlashResult(handled=True, message=f"removed {args}")

    if cmd == "cancel":
        coord.close_user_turn("cancelled")
        return SlashResult(handled=True,
                           message="user turn cancelled")

    if cmd == "dm":
        parts = args.split(None, 1)
        if len(parts) != 2:
            return SlashResult(handled=True,
                               message="usage: /dm <id> <message>")
        target, body = parts
        if target not in state.participants:
            return SlashResult(handled=True,
                               message=f"unknown participant: {target}")
        e = ev.chat(sender="user", body=body,
                    addressees=[target],
                    channel=f"dm:{target}",
                    room_epoch=state.room_epoch)

        def _dm_plan(posted_event: Event):
            return plan_for_default(target, reason="dm",
                                    target_event_ids=[posted_event.id],
                                    rationale="direct DM")

        coord.post_user_event_and_open_turn(e, _dm_plan)
        return SlashResult(handled=True,
                           message=f"DM → {target}")

    if cmd == "summary":
        snap = session.bus.snapshot(channel="main", kinds=["summary"])
        if not snap:
            return SlashResult(handled=True,
                               message="no summary yet")
        return SlashResult(handled=True, message=snap[-1].body)

    if cmd == "anchor":
        if not args:
            return SlashResult(handled=True,
                               message=f"anchor: {state.anchor_id}")
        if args not in state.participants:
            return SlashResult(handled=True,
                               message=f"unknown participant: {args}")
        coord.set_anchor(args)
        return SlashResult(handled=True, message=f"anchor → {args}")

    if cmd == "responder":
        if not args:
            return SlashResult(
                handled=True,
                message=f"default responder: {state.default_responder_id}",
            )
        if args not in state.participants:
            return SlashResult(handled=True,
                               message=f"unknown participant: {args}")
        coord.set_default_responder(args)
        return SlashResult(handled=True,
                           message=f"default_responder → {args}")

    # ------------------------------------------------------------------
    # Room control state — roles / floor / quiet / style / goal
    # Crisp deterministic levers for the user. The interpreter respects
    # these on every subsequent classification until cleared.
    # ------------------------------------------------------------------

    if cmd == "roles":
        if not args:
            current = state.control.roles
            if not current:
                return SlashResult(handled=True, message="(no roles set)")
            pretty = ", ".join(f"{pid}={role}"
                               for pid, role in sorted(current.items()))
            return SlashResult(handled=True, message=f"roles: {pretty}")
        new_roles, errors = _parse_roles_args(args, state.participants)
        if errors:
            return SlashResult(
                handled=True,
                message=f"usage: /roles pid=role pid=role ...  ({errors})",
            )
        coord.set_roles(new_roles)
        if not new_roles:
            return SlashResult(handled=True, message="roles cleared")
        pretty = ", ".join(f"{pid}={role}"
                           for pid, role in sorted(new_roles.items()))
        return SlashResult(handled=True, message=f"roles → {pretty}")

    if cmd == "floor":
        if not args:
            owners = state.control.floor_owner
            if not owners:
                return SlashResult(handled=True, message="floor: (open)")
            return SlashResult(handled=True,
                               message=f"floor: {', '.join(owners)}")
        ids = args.split()
        unknown = [p for p in ids if p not in state.participants]
        if unknown:
            return SlashResult(
                handled=True,
                message=f"unknown participant(s): {', '.join(unknown)}",
            )
        coord.set_floor_owner(ids)
        new_owners = state.control.floor_owner or []
        return SlashResult(
            handled=True,
            message=f"floor → {', '.join(new_owners)}",
        )

    if cmd == "release":
        if not state.control.floor_owner:
            return SlashResult(handled=True,
                               message="floor already open")
        coord.set_floor_owner(None)
        return SlashResult(handled=True, message="floor released (open)")

    if cmd == "quiet":
        if not args:
            return SlashResult(
                handled=True,
                message="usage: /quiet <pid> [<pid> ...]  (silences "
                        "those agents; everyone else holds the floor)",
            )
        silenced = args.split()
        unknown = [p for p in silenced if p not in state.participants]
        if unknown:
            return SlashResult(
                handled=True,
                message=f"unknown participant(s): {', '.join(unknown)}",
            )
        speakers = [p for p in state.participants if p not in silenced]
        if not speakers:
            return SlashResult(
                handled=True,
                message="cannot silence every participant — use /release "
                        "to open the floor.",
            )
        coord.set_floor_owner(speakers)
        return SlashResult(
            handled=True,
            message=f"silenced {', '.join(silenced)}; floor → "
                    f"{', '.join(speakers)}",
        )

    if cmd == "goal":
        # P2.3: /goal is now an alias for /topic. The two used to track
        # separate fields (state.topic + control.active_goal); they
        # collapsed into a single source of truth on state.topic.
        if not args:
            current = state.topic
            return SlashResult(
                handled=True,
                message=f"topic: {current or '(none)'}",
            )
        if len(args) > 500:
            return SlashResult(
                handled=True,
                message=(f"/goal argument too long "
                         f"({len(args)} > 500 chars)"),
            )
        coord.set_topic(args)
        return SlashResult(handled=True, message=f"topic → {args}")

    if cmd == "brief":
        coord.set_style("brief")
        return SlashResult(handled=True, message="style → brief")

    if cmd == "normal":
        coord.set_style("normal")
        return SlashResult(handled=True, message="style → normal")

    if cmd == "detailed":
        coord.set_style("detailed")
        return SlashResult(handled=True, message="style → detailed")

    if cmd == "control":
        # Diagnostic dump of the current room control state.
        ctl = state.control
        lines = [
            f"floor: {', '.join(ctl.floor_owner) if ctl.floor_owner else '(open)'}",
            f"wait_for_user: {ctl.wait_for_user}",
            f"style: {ctl.style}",
            f"topic: {state.topic or '(none)'}",
        ]
        if ctl.roles:
            roles_str = ", ".join(f"{pid}={role}"
                                  for pid, role in sorted(ctl.roles.items()))
            lines.append(f"roles: {roles_str}")
        else:
            lines.append("roles: (none)")
        return SlashResult(handled=True, message="\n".join(lines))

    return SlashResult(
        handled=True,
        message=(f"unknown command: /{cmd}  (try /who, /topic, /add, "
                 f"/remove, /cancel, /dm, /summary, /anchor, "
                 f"/responder, /roles, /floor, /release, /quiet, "
                 f"/goal, /brief, /normal, /detailed, /control, /leave)"),
    )


def _parse_roles_args(args: str,
                      participants: dict) -> tuple[dict[str, str], str]:
    """Parse ``/roles`` argument string into a {pid: role} dict.

    Format: ``pid=role pid=role ...``. Returns ``(roles, error_message)``;
    ``error_message`` is empty on success. Empty input clears all roles.
    Unknown participant ids produce an error and zero roles applied.
    """
    out: dict[str, str] = {}
    bad: list[str] = []
    for token in args.split():
        if "=" not in token:
            bad.append(token)
            continue
        pid, _, role = token.partition("=")
        pid = pid.strip()
        role = role.strip()
        if not pid or not role:
            bad.append(token)
            continue
        if pid not in participants:
            bad.append(f"unknown:{pid}")
            continue
        out[pid] = role
    if bad:
        return ({}, f"bad tokens: {', '.join(bad)}")
    return (out, "")


# ---------------------------------------------------------------------------
# Posting user input
# ---------------------------------------------------------------------------

_VALID_CHANNEL_RE = re.compile(r"^(main|dm:[A-Za-z][\w-]*)$")
"""P3.1 / audit T3 — accepted channel format at the runtime entry.

Defense in depth: today the ``channel`` argument is operator-supplied
(slash command), but a future caller that takes ``channel=`` from
untrusted input would otherwise let an attacker post into an
arbitrary made-up channel. This regex is the kernel's structural
contract for channel strings: ``main`` or ``dm:<participant_id>`` with
the standard mention-id alphabet.
"""


def post_user_text(session: LoomSession, text: str,
                   *, channel: str = "main") -> Event:
    """Post a user message and (re-)open a UserTurn via the room policy.

    Mentions (``@id``) are extracted to populate ``addressees``. The
    session's :class:`ConversationPolicy` then classifies the message
    and the coordinator opens a UserTurn — *unless* the policy returned
    an acknowledgement plan, in which case no turn opens.

    The bus post + classify + open are performed atomically (under the
    coordinator lock) so actor threads waking on the bus post do not
    observe ``user_turn=None`` for an event whose turn is about to open.

    P3.1: ``channel`` is structurally validated against
    :data:`_VALID_CHANNEL_RE`. An invalid channel string raises
    ``ValueError`` rather than reaching the bus — this is defense in
    depth against a future call site that pulls ``channel=`` from
    untrusted text.
    """
    if not _VALID_CHANNEL_RE.match(channel):
        raise ValueError(
            f"channel must match {_VALID_CHANNEL_RE.pattern!r}, got "
            f"{channel!r}")
    addressable = list(session.state.participants.keys())
    addressees = parse_addressees(text, addressable, exclude="user")
    e = ev.chat(
        sender="user", body=text,
        addressees=addressees, channel=channel,
        room_epoch=session.state.room_epoch,
    )

    def _classify_after_post(posted_event: Event):
        # P2.7: ``prior_speaker`` removed from the policy contract.
        # ``last_responsible_speaker`` remains usable for policies that
        # want it via their own constructor.
        return session.policy.plan_user_turn(
            posted_event, session.state.view())

    session.coordinator.post_user_event_and_open_turn(e, _classify_after_post)
    return e


# ---------------------------------------------------------------------------
# Console rendering helpers
# ---------------------------------------------------------------------------

def _format_control(event: Event) -> Optional[str]:
    """Pretty one-liner for a control event. Returns ``None`` to suppress."""
    if not isinstance(event.body, dict):
        return None
    ct = event.body.get("control_type")
    if ct == "topic_changed":
        return f"topic → {event.body.get('new') or '(cleared)'}"
    if ct == "dead_letter":
        return (f"mention dead-lettered → {event.body.get('reroute_to')} "
                f"({event.body.get('reason')})")
    if ct == "default_responder_changed":
        return (f"default responder: {event.body.get('old_id')} → "
                f"{event.body.get('new_id')}")
    if ct in ("anchor_changed", "chair_changed",
              "default_summarizer_changed"):
        slot = ct[: -len("_changed")].replace("_", " ")
        return (f"{slot}: {event.body.get('old_id')} → "
                f"{event.body.get('new_id')}")
    if ct == "participant_added":
        return f"+ {event.body.get('id')}"
    if ct == "participant_removed":
        return f"- {event.body.get('id')}"
    if ct == "user_turn_closed":
        reason = event.body.get("reason")
        if reason == "completed":
            return None
        if reason == "no_responder":
            return "(no agent responded)"
        if reason == "obligation_unresolved":
            return "(required participant did not reply)"
        return f"user turn closed ({reason})"
    if ct in ("user_turn_opened",
              "obligation_recorded",
              "obligation_resolved"):
        # Internal accounting — silent in the console.
        return None
    return None  # unknown control_type — drop, never leak dict repr


def _make_console_subscriber(
    notify: Callable[[str], None],
) -> Callable[[Event], None]:
    """Return the bus subscriber used by :func:`run_loom_console`."""
    def _on_event(event: Event) -> None:
        if event.kind == "chat":
            if event.sender == "user":
                if event.channel.startswith("dm:"):
                    target = event.channel[len("dm:"):]
                    notify(f"\n(dm → {target}) ▸ {event.body}")
                return
            if event.channel != "main":
                return  # agent-to-agent DMs stay private
            notify(f"\n{event.sender} ▸ {event.body}")
            return
        if event.kind == "control":
            msg = _format_control(event)
            if msg:
                notify(f"\n· {msg}")
            return
        # stream events: drop in v0 (chat event is the canonical render).
        return
    return _on_event


# ---------------------------------------------------------------------------
# Convenience: a simple console-driven loop (for /loom slash command)
# ---------------------------------------------------------------------------

def run_loom_console(
    wirings: list[ParticipantWiring],
    *,
    config: Optional[RoomConfig] = None,
    default_responder_id: Optional[str] = None,
    anchor_id: Optional[str] = None,
    topic: Optional[str] = None,
    journal_dir: Optional[str | Path] = None,
    prompt_fn: Optional[Callable[[], str]] = None,
    notify: Optional[Callable[[str], None]] = None,
    policy: Optional[ConversationPolicy] = None,
    policy_error_mode: str = "close_turn",
) -> None:
    """Run an interactive Loom session bound to an input function.

    ``prompt_fn()`` returns the next user input line (or raises EOFError
    / KeyboardInterrupt to quit). ``notify(msg)`` prints status messages
    (slash-command results, control events the user should see). When
    omitted, both default to ``input()`` and ``print()``.

    ``policy`` and ``policy_error_mode`` are forwarded to
    :func:`build_loom_session`. See that function for semantics.
    """
    if prompt_fn is None:
        def prompt_fn():  # pragma: no cover - interactive TTY default
            return input("you ▸ ")
    if notify is None:
        notify = print

    session = build_loom_session(
        wirings,
        config=config,
        default_responder_id=default_responder_id,
        anchor_id=anchor_id,
        topic=topic,
        journal_dir=journal_dir,
        policy=policy,
        policy_error_mode=policy_error_mode,
    )

    session.bus.subscribe(_make_console_subscriber(notify))

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
                result = handle_slash_command(text, session, console=notify)
                if result.message:
                    notify(result.message)
                if result.quit:
                    break
                continue
            post_user_text(session, text)
    finally:
        session.stop()
