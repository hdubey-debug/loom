"""Loom kernel — addressee parsing and "last responsible speaker" lookup.

These two helpers are kernel concerns, not policy decisions:

- :func:`parse_addressees` runs at user-post time inside the runtime
  (BEFORE any policy classification) so :attr:`Event.addressees` is
  populated for visibility and DM filtering. It also runs at
  draft-commit time inside :mod:`loom.kernel.streaming` to decorate an
  agent's reply with implicit @-mentions.

- :func:`last_responsible_speaker` walks the bus snapshot — the bus is
  a kernel object — and returns who spoke most recently on a channel.
  Used by the runtime to thread ``prior_speaker`` into the policy's
  :meth:`ConversationPolicy.plan_user_turn` call. UI consumers may call
  this too.

The shared regex :data:`_MENTION_RE` is module-level so callers can
monkeypatch it in tests.
"""
from __future__ import annotations

import re
from typing import Optional

from loom.kernel.bus import MessageBus


_MENTION_RE = re.compile(r"@([A-Za-z][\w-]*)")


def parse_addressees(text: str, addressable: list[str], *,
                     exclude: Optional[str] = None) -> list[str]:
    """Pull ``@id`` tokens from ``text`` filtered to ``addressable``.

    Order-preserving. Each id appears at most once. Self-mentions
    (``exclude``) are filtered. The same parser is used both at user-post
    time (to populate ``Event.addressees``) and at draft-commit time (to
    decorate the agent's reply with implicit @-mentions).
    """
    pool = set(addressable)
    seen: set[str] = set()
    out: list[str] = []
    for m in _MENTION_RE.findall(text):
        if m == exclude or m not in pool or m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out


def last_responsible_speaker(
    bus: MessageBus,
    *,
    channel: str = "main",
    exclude_user: bool = True,
) -> Optional[str]:
    """Return the participant id whose committed chat sat most recently on ``channel``.

    Walks the bus snapshot in reverse and returns the first ``chat``
    sender that is not ``"user"`` (when ``exclude_user``). Returns
    ``None`` when no eligible chat exists yet. Useful for UI + future
    LLM-backed classifiers; the deterministic v0 classifier ignores it.
    """
    log = bus.snapshot(channel=channel, kinds=["chat"])
    for ev in reversed(log):
        if exclude_user and ev.sender == "user":
            continue
        if ev.sender == "system":
            continue
        return ev.sender
    return None
