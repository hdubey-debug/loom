"""Loom policy — pluggable extension layer.

Modules here implement :class:`loom.contracts.ConversationPolicy`
subclasses. Policies decide who may speak in a room, when, and what
extra system / role instructions to render. They must not mutate
:class:`RoomState` or post to the bus directly — the kernel is the
only mutator.

The canonical user-facing import path for plan-builder helpers is
this module: ``from loom.policy import plan_with_required``. The
helpers themselves live under :mod:`loom.kernel.obligations`; importing
them through this module is the supported path for policy authors.

Reference policies bundled with v0:

- :class:`DefaultPolicy`         — v0.0 floor-aware classifier with
  vocative + game-start detection. Used by Loom.
- :class:`OpenChatPolicy`        — broadcast every user post to all
  active capable participants. Simplest non-trivial policy.
- :class:`SingleResponderPolicy` — every user post routes to one
  configured participant. Useful for "talk to one assistant".
  Canonical reference for new policy authors.
- :class:`RoundRobinPolicy`      — rotate through a fixed order, one
  speaker per user post. Reference for declarative state mutation
  (``set_turn_order`` / ``advance_turn_pointer``). A non-empty
  ``turn_order`` is itself the round-robin mode signal.

Plan-builder helpers (re-exported from :mod:`loom.kernel.obligations`
so policy authors don't reach into ``loom.kernel.*``):

- :func:`plan_for_acknowledgement`  — no-response plan; runtime
  skips ``open_user_turn``.
- :func:`plan_with_required`        — common case: route to a
  required set of participants with one ``must`` obligation each.
- :func:`plan_for_default`          — fall back to a single
  configured default responder.
"""
from loom.kernel.obligations import (
    plan_for_acknowledgement,
    plan_for_default,
    plan_with_required,
)
from loom.policy.base import BasicPolicy
from loom.policy.default import DefaultPolicy
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.round_robin import RoundRobinPolicy
from loom.policy.single_responder import SingleResponderPolicy

__all__ = [
    "BasicPolicy",
    "DefaultPolicy",
    "OpenChatPolicy",
    "SingleResponderPolicy",
    "RoundRobinPolicy",
    "plan_for_acknowledgement",
    "plan_for_default",
    "plan_with_required",
]
