"""Loom single-responder policy — one configured participant always replies.

**Canonical reference for new policy authors. If you're new here,
copy this module first.** It is the smallest non-trivial policy
in-tree: a single ``__init__`` with input validation, one
``_choose_responders`` method, and a few hook overrides on
:class:`loom.policy.base.BasicPolicy`. ``docs/writing-a-policy.md``
walks through it line-by-line.

Every user post routes to the same participant. ``max_responses=1``
(implicit — the chosen responder set has size 1) and
``wait_for_user_after=True`` so other agents stay silent until the
next user post. If the configured responder isn't currently active
and capable, the turn becomes an acknowledgement (no response).

Useful as:

- A "talk to one assistant" mode (the dual of :class:`OpenChatPolicy`).
- A reference for authors who want a fixed-recipient policy with no
  fall-through to other participants.
"""
from __future__ import annotations

from loom.kernel.events import Event
from loom.kernel.room import RoomStateView
from loom.policy.base import BasicPolicy


class SingleResponderPolicy(BasicPolicy):
    """Route every user post to a single configured participant."""

    name = "single_responder"

    def __init__(self, responder_id: str) -> None:
        if not responder_id or not isinstance(responder_id, str):
            raise ValueError(
                "SingleResponderPolicy requires a non-empty responder_id")
        self.responder_id = responder_id

    def _choose_responders(
        self, user_event: Event, state: RoomStateView,
    ) -> set[str]:
        info = state.participants.get(self.responder_id)
        if info is None or not info.active or not info.capable:
            return set()
        return {self.responder_id}

    def _routing_case(self) -> str:
        return "single_responder"

    def _wait_for_user_after(self) -> bool:
        return True

    def _instruction(self, state: RoomStateView) -> str:
        return (f"You ({self.responder_id}) are the configured "
                "responder for this room.")

    def _rationale(
        self, responders: list[str], state: RoomStateView,
    ) -> str:
        return f"single responder: {self.responder_id}"

    def _no_responders_rationale(self, state: RoomStateView) -> str:
        return (f"configured responder {self.responder_id!r} "
                "not active/capable")
