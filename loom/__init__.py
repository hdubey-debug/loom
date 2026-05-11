"""Loom.

Role-symmetric, streaming, event-sourced room protocol for LLM agents.
Loom is a kernel — anyone can build chatrooms (debate, classroom,
20-questions, custom workflows) by writing their own
:class:`ConversationPolicy` and dropping it into a :class:`LoomRoom`.

The kernel handles mechanism (events, bus, journal, leases,
obligations, throttle, budget, streaming, prompt sandbox); the policy
handles "who may speak, when, and what extra instructions to render".

Quick start (four lines + agents). The ``with room:`` block is
**required** — actor threads start on ``__enter__`` and stop on
``__exit__``. Posting to a room that hasn't started returns nothing
because no actor is listening.

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

Bundled policies (pick one, pass via ``policy=``):

- :class:`DefaultPolicy`         — v0.0 floor-aware classifier with
  vocative + game-start detection. Matches Loom behavior. Used when
  ``policy=`` is omitted; v0.1 considers switching the default to
  :class:`OpenChatPolicy`.
- :class:`OpenChatPolicy`        — broadcast every user post to all
  active capable participants. Simplest non-trivial policy.
- :class:`SingleResponderPolicy` — every user post routes to one
  configured participant. Useful for "talk to one assistant".
  **Canonical reference for new policy authors.**
- :class:`RoundRobinPolicy`      — rotate through a fixed order, one
  speaker per user post.

Public surface (primary tier — what most consumers import):

- :class:`LoomRoom`               — main facade. Accepts agents and a policy.
- :class:`Agent`                 — protocol every actor satisfies.
- :func:`agent_from_send` /
  :func:`agent_from_stream` /
  :func:`agent_from_object`      — adapters that wrap ordinary callables.
- :class:`ConversationPolicy`    — extension point for room behavior.
- :class:`DefaultPolicy` /
  :class:`OpenChatPolicy` /
  :class:`SingleResponderPolicy` /
  :class:`RoundRobinPolicy`      — bundled reference policies.
- :class:`RoomConfig`            — boot-time room configuration.
- :class:`RoomStateView`         — read-only state view passed to
  :meth:`ConversationPolicy.plan_user_turn`.
- :class:`Message` /
  :class:`TurnResult`            — typed user-facing projections;
  :meth:`LoomRoom.post_and_wait` returns ``TurnResult``.
- :class:`LoomError`              — base class for all Loom-defined
  exceptions; see :mod:`loom.errors`.

Advanced surface (kept for power users; most consumers won't need it):

- :class:`ParticipantWiring` /
  :class:`SendProxyAdapter`      — direct actor wiring.
- :func:`build_loom_session` /
  :func:`run_loom_console`        — pre-facade entry points.

Tutorials & reference:

- ``docs/loom-ux-spec.md``        — UX contract + design principles.
- ``docs/writing-a-policy.md``   — tutorial for policy authors.
- ``docs/writing-an-adapter.md`` — tutorial for adapter authors.
- ``docs/security-model.md``     — security posture.

Plan-builder helpers for policy authors live in :mod:`loom.policy`
(re-exported from :mod:`loom.kernel.obligations` so authors don't need
to reach into the kernel namespace):
``from loom.policy import plan_with_required, plan_for_acknowledgement,
plan_for_default``.

Custom kernels and prompt machinery live under :mod:`loom.kernel.*` —
advanced; library examples and bundled docs MUST NOT import from
``loom.kernel.*``.
"""

from loom.adapters import agent_from_object, agent_from_send, agent_from_stream
from loom.contracts import Agent, ConversationPolicy
from loom.errors import LoomError
from loom.kernel.room import RoomConfig, RoomStateView
from loom.messages import Message, TurnResult
from loom.policy.default import DefaultPolicy
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.round_robin import RoundRobinPolicy
from loom.policy.single_responder import SingleResponderPolicy
from loom.room import LoomRoom
from loom.runtime import (
    ParticipantWiring,
    SendProxyAdapter,
    build_loom_session,
    run_loom_console,
)

__all__ = [
    # Primary public surface.
    "LoomRoom",
    "Agent",
    "agent_from_send",
    "agent_from_stream",
    "agent_from_object",
    "ConversationPolicy",
    "DefaultPolicy",
    "OpenChatPolicy",
    "SingleResponderPolicy",
    "RoundRobinPolicy",
    "RoomConfig",
    "RoomStateView",
    "Message",
    "TurnResult",
    "LoomError",
    # Advanced surface.
    "ParticipantWiring",
    "SendProxyAdapter",
    "build_loom_session",
    "run_loom_console",
]
