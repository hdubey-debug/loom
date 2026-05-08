"""Loom open-chat policy — every active capable agent broadcasts every turn.

The simplest non-trivial policy: no addressing detection, no floor
narrowing, no round-robin. Every user post broadcasts to the room and
every active capable participant gets a ``must`` obligation. Agents
self-route via prompt and may :pep:`PASS`.

Useful as:

- A reference for authors writing their own policy (compare against
  :class:`loom.policy.default.DefaultPolicy` for added behavior).
- A no-frills broadcast room when the consumer doesn't need vocative
  detection or game-mode round-robin.

Built on :class:`loom.policy.base.BasicPolicy` — overrides only
:meth:`_choose_responders` and the per-turn instruction.
"""
from __future__ import annotations

from loom.kernel.events import Event
from loom.kernel.room import RoomStateView
from loom.policy.base import BasicPolicy


class OpenChatPolicy(BasicPolicy):
    """Broadcast every user post to every active capable participant."""

    name = "open_chat"

    def _choose_responders(
        self, user_event: Event, state: RoomStateView,
    ) -> set[str]:
        return {
            pid for pid, info in state.participants.items()
            if info.active and info.capable
        }

    def _routing_case(self) -> str:
        return "broadcast"

    def _instruction(self, state: RoomStateView) -> str:
        return "Open group chat. Reply with substance or [PASS]."

    def _rationale(
        self, responders: list[str], state: RoomStateView,
    ) -> str:
        return f"open chat: broadcast to {len(responders)} agent(s)"

    def _no_responders_rationale(self, state: RoomStateView) -> str:
        return "no active capable participants"
