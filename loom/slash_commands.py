"""Loom v0.3 — human root actions via slash commands (PR 11).

Doctrine: **P15** (human root actions use the control-action path).

Slash commands let a trusted user (the session owner) propose
control actions without going through the agent capability gate.
The kernel's :meth:`RoomCoordinator.propose_control_action`
recognises ``proposer_id="user"`` as authorised for any action; this
module is the parser layer that turns ``/grant claude_code SET_TOPIC``
into ``("GRANT_CAPABILITY_GRANT_FLOOR", {"grantee_id": "claude_code",
"capability": "SET_TOPIC"})`` and dispatches it.

The parser is intentionally narrow — exactly the commands needed for
the v0.3 doctrine subsystems. Custom commands plug in via
:class:`SlashCommandRegistry` at runtime.

Recognised commands (PR 11 set):

- ``/grant <participant> <CAPABILITY> [expires_in=<seconds>]``
- ``/revoke <grant_id>``
- ``/topic <text...>``
- ``/anchor <participant>``
- ``/responder <participant>``
- ``/floor <participant1> [participant2 ...]``  (one-lease grant_floor)
- ``/policy <policy_name>``  (placeholder; PR 9 reducer pending)

Each command parses to ``(action_name, params)`` matching the PR 9
:class:`ControlAction` registry. Unknown commands or malformed
arguments raise :class:`SlashCommandError`.

Authentication: the parser does not perform authn. The runtime's
session layer is responsible for enforcing "only trusted local
sessions may invoke slash commands"; this module assumes the caller
has already cleared that gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional


class SlashCommandError(ValueError):
    """Raised by :func:`parse_slash_command` on a malformed command."""


@dataclass(frozen=True)
class ParsedCommand:
    """Result of :func:`parse_slash_command`: action name + params dict."""

    action_name: str
    params: dict


def _split_args(args: str) -> list[str]:
    """Whitespace-split ``args`` (quoting not supported in v0.3)."""
    return args.split()


def _parse_kv(tokens: list[str]) -> dict:
    """Extract ``key=value`` tokens from a token list (mutates the list)."""
    kv = {}
    remaining = []
    for t in tokens:
        if "=" in t:
            k, v = t.split("=", 1)
            kv[k] = v
        else:
            remaining.append(t)
    tokens[:] = remaining
    return kv


# ---------------------------------------------------------------------------
# Individual command parsers
# ---------------------------------------------------------------------------


def _parse_grant(args: str) -> ParsedCommand:
    tokens = _split_args(args)
    kv = _parse_kv(tokens)
    if len(tokens) != 2:
        raise SlashCommandError(
            "usage: /grant <participant> <CAPABILITY> [expires_in=<seconds>]"
        )
    grantee, capability = tokens
    expires_at = None
    if "expires_in" in kv:
        try:
            expires_at = float(kv["expires_in"])
        except ValueError as exc:
            raise SlashCommandError("expires_in must be numeric") from exc
    # NB: the kernel's capability_granted constructor will be wrapped
    # in a custom GrantCapabilityAction; for PR 11 we surface the raw
    # parameters so a future PR can introduce the action without
    # changing the parser shape.
    return ParsedCommand(
        action_name="GRANT_CAPABILITY",
        params={
            "grantee_id": grantee,
            "capability": capability,
            "expires_at": expires_at,
        },
    )


def _parse_revoke(args: str) -> ParsedCommand:
    tokens = _split_args(args)
    if len(tokens) != 1:
        raise SlashCommandError("usage: /revoke <grant_id>")
    return ParsedCommand(
        action_name="REVOKE_CAPABILITY",
        params={"grant_id": tokens[0]},
    )


def _parse_topic(args: str) -> ParsedCommand:
    topic = args.strip()
    if not topic:
        raise SlashCommandError("usage: /topic <text...>")
    return ParsedCommand(action_name="SET_TOPIC", params={"topic": topic})


def _parse_anchor(args: str) -> ParsedCommand:
    tokens = _split_args(args)
    if len(tokens) != 1:
        raise SlashCommandError("usage: /anchor <participant>")
    return ParsedCommand(
        action_name="SET_ANCHOR",
        params={"participant_id": tokens[0]},
    )


def _parse_responder(args: str) -> ParsedCommand:
    tokens = _split_args(args)
    if len(tokens) != 1:
        raise SlashCommandError("usage: /responder <participant>")
    return ParsedCommand(
        action_name="SET_DEFAULT_RESPONDER",
        params={"participant_id": tokens[0]},
    )


def _parse_floor(args: str) -> ParsedCommand:
    speakers = _split_args(args)
    if not speakers:
        raise SlashCommandError("usage: /floor <participant1> [participant2 ...]")
    return ParsedCommand(
        action_name="GRANT_FLOOR",
        params={"speakers": speakers},
    )


def _parse_summarize(args: str) -> ParsedCommand:
    """``/summarize [thread=<id>] [actor=<id>]`` — v0.3.x PR 5.

    Triggers Path B (control-action) summarisation via the
    coordinator's :meth:`RoomCoordinator.request_summarization`.
    Optional ``thread=`` and ``actor=`` keyword args narrow the
    :class:`ContextScope`; without them, the room's main scope
    (``thread_id="main"``, ``actor_id=None``) is used.
    """
    tokens = _split_args(args)
    kv = _parse_kv(tokens)
    params: dict = {}
    if "thread" in kv:
        params["thread_id"] = kv["thread"]
    if "actor" in kv:
        params["actor_id"] = kv["actor"]
    return ParsedCommand(action_name="SUMMARIZE", params=params)


def _parse_policy(args: str) -> ParsedCommand:
    tokens = _split_args(args)
    if len(tokens) != 1:
        raise SlashCommandError("usage: /policy <policy_name>")
    return ParsedCommand(
        action_name="SWITCH_POLICY",
        params={"policy_name": tokens[0]},
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


Parser = Callable[[str], ParsedCommand]


class SlashCommandRegistry:
    """``"/cmd" → parser`` lookup. Hydrated with kernel built-ins.

    Extensible by runtime: ``registry.register("/x", parser_fn)``.
    Custom commands integrate with ``RoomConfig.custom_slash_commands``
    once the field is wired (v0.3.x follow-up).
    """

    def __init__(self) -> None:
        self._parsers: dict[str, Parser] = {}

    def register(self, prefix: str, parser: Parser) -> None:
        if prefix in self._parsers:
            raise ValueError(f"slash command already registered: {prefix!r}")
        self._parsers[prefix] = parser

    def get(self, prefix: str) -> Optional[Parser]:
        return self._parsers.get(prefix)

    def commands(self) -> tuple[str, ...]:
        return tuple(sorted(self._parsers.keys()))


def build_default_registry() -> SlashCommandRegistry:
    """Construct the v0.3 PR 11 default slash-command registry."""
    reg = SlashCommandRegistry()
    reg.register("/grant", _parse_grant)
    reg.register("/revoke", _parse_revoke)
    reg.register("/topic", _parse_topic)
    reg.register("/anchor", _parse_anchor)
    reg.register("/responder", _parse_responder)
    reg.register("/floor", _parse_floor)
    reg.register("/policy", _parse_policy)
    reg.register("/summarize", _parse_summarize)
    return reg


# ---------------------------------------------------------------------------
# Top-level parser
# ---------------------------------------------------------------------------


_SLASH_RE = re.compile(r"^/(\w+)(?:\s+(.*))?$", re.DOTALL)


def is_slash_command(text: str) -> bool:
    """True iff ``text`` starts with ``/`` (the slash-command marker)."""
    return text.startswith("/")


def parse_slash_command(
    text: str,
    registry: Optional[SlashCommandRegistry] = None,
) -> ParsedCommand:
    """Parse a slash command into ``(action_name, params)``.

    Raises :class:`SlashCommandError` on a malformed command or one
    not in the registry. ``registry`` defaults to
    :func:`build_default_registry` when not provided — callers wanting
    to add custom commands should pass their own registry.
    """
    if not is_slash_command(text):
        raise SlashCommandError("not a slash command (must start with '/')")
    m = _SLASH_RE.match(text.strip())
    if m is None:
        raise SlashCommandError(f"malformed slash command: {text!r}")
    cmd = "/" + m.group(1)
    args = m.group(2) or ""
    reg = registry if registry is not None else build_default_registry()
    parser = reg.get(cmd)
    if parser is None:
        raise SlashCommandError(f"unknown slash command: {cmd}")
    return parser(args)


def dispatch_slash_command(
    coordinator,
    text: str,
    *,
    registry: Optional[SlashCommandRegistry] = None,
):
    """Parse ``text`` and dispatch via ``coordinator.propose_control_action``.

    Returns the :class:`ControlActionResult` from the dispatch. P15:
    user-issued commands run with ``proposer_id="user"`` so they
    bypass the agent capability gate — kept in one place (the
    coordinator's ``_CapabilityCheck``).

    The caller is responsible for authn — only invoke this for
    trusted local sessions.
    """
    parsed = parse_slash_command(text, registry=registry)
    # v0.3.x PR 5: ``/summarize`` is special-cased — it doesn't
    # produce a ControlEffect, it acquires a SUMMARIZATION lease via
    # :meth:`RoomCoordinator.request_summarization` (Path B). Returns
    # the :class:`SchedulingResult` directly so the caller can read
    # ``lease_id`` and ``scheduled`` flags.
    if parsed.action_name == "SUMMARIZE":
        from loom.kernel.context import ContextScope
        scope = ContextScope(
            room_id=getattr(coordinator.config, "room_id", "main"),
            thread_id=parsed.params.get("thread_id", "main"),
            actor_id=parsed.params.get("actor_id"),
        )
        return coordinator.request_summarization(
            requester="user",
            scope=scope,
        )
    return coordinator.propose_control_action(
        proposer_id="user",
        action_name=parsed.action_name,
        params=parsed.params,
    )
