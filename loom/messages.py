"""Loom user-facing message + turn-result projection.

Library authors interact with these typed projections rather than the
kernel-internal :class:`loom.kernel.events.Event`. The projection hides
``room_epoch``, ``user_turn_id``, ``addressees``, ``meta`` and other
kernel bookkeeping fields a first-time user does not need.

- :class:`Message` — frozen projection of a chat-shaped event.
- :class:`TurnResult` — typed return value of
  :meth:`loom.LoomRoom.post_and_wait`. Iterable over its ``messages``
  list so existing ``for r in replies`` patterns keep working.

The two-tier event surface (UX spec §5.3): the full
:class:`~loom.kernel.events.Event` stays kernel-internal; user-facing
APIs return :class:`Message` and :class:`TurnResult`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Literal, Optional, Union

from loom.kernel.events import Event


TurnClosedReason = Literal[
    "all_obligations_resolved",
    "timeout",
    "no_turn_opened",
    "cancelled",
    "obligation_unresolved",
    "new_user_post",
    "topic_changed",
]


# Maps kernel ``UserTurn.closure_reason`` values to the user-facing
# :data:`TurnClosedReason` set. Kernel reasons may evolve; unknown
# values pass through as the raw string so a kernel-introduced reason
# is still observable from a TurnResult without an immediate breakage.
_CLOSURE_REASON_MAP: dict[str, TurnClosedReason] = {
    "completed": "all_obligations_resolved",
    "idle_timeout": "timeout",
    "no_responder": "no_turn_opened",
    "cancelled": "cancelled",
    "obligation_unresolved": "obligation_unresolved",
    "new_user_post": "new_user_post",
    "topic_changed": "topic_changed",
}


def _project_closure_reason(kernel_reason: Optional[str]) -> str:
    if kernel_reason is None:
        return "timeout"
    return _CLOSURE_REASON_MAP.get(kernel_reason, kernel_reason)


@dataclass(frozen=True, slots=True)
class Message:
    """User-facing transcript projection of a chat :class:`Event`.

    Carries the fields a library author actually needs to render a
    conversation: ``sender``, ``body``, ``channel``, ``timestamp``,
    ``kind``. Kernel bookkeeping (``room_epoch``, ``user_turn_id``,
    ``addressees``, ``meta``) is hidden — advanced consumers who need
    those values import :class:`loom.kernel.events.Event` directly.
    """

    sender: str
    body: str
    channel: str
    timestamp: float
    kind: str

    @classmethod
    def from_event(cls, ev: Event) -> "Message":
        body = ev.body if isinstance(ev.body, str) else str(ev.body)
        return cls(
            sender=ev.sender,
            body=body,
            channel=ev.channel,
            timestamp=ev.ts,
            kind=ev.kind,
        )


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Typed outcome of :meth:`loom.LoomRoom.post_and_wait`.

    Iterable over :attr:`messages` so existing ``for r in replies``
    and ``{r.sender for r in replies}`` patterns continue to work.
    Truthy iff any messages were collected; supports ``len()`` and
    integer / slice indexing.

    Fields:

    - ``messages``               — committed chat replies in posted
      order (excluding the user's own post).
    - ``turn_id``                — kernel user-turn id (``-1`` if no
      turn opened).
    - ``routing_case``           — policy classification string;
      promoted to a typed ``Literal`` in P2.6.
    - ``closed_reason``          — why the turn closed (see
      :data:`TurnClosedReason`).
    - ``participant_responses``  — ``messages`` grouped by sender.
    - ``elapsed_s``              — wall-clock elapsed from post to
      close (or to timeout).
    """

    messages: list[Message] = field(default_factory=list)
    turn_id: int = -1
    routing_case: str = ""
    closed_reason: str = "no_turn_opened"
    participant_responses: dict[str, list[Message]] = field(default_factory=dict)
    elapsed_s: float = 0.0

    def __iter__(self) -> Iterator[Message]:
        return iter(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def __bool__(self) -> bool:
        return bool(self.messages)

    def __getitem__(
        self,
        idx: Union[int, slice],
    ) -> Union[Message, list[Message]]:
        return self.messages[idx]


__all__ = [
    "Message",
    "TurnResult",
    "TurnClosedReason",
]
