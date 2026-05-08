"""Loom contracts — :class:`ConversationPolicy` ABC + :class:`Agent` Protocol.

Neutral module imported by both :mod:`loom.kernel` and :mod:`loom.policy`.
The ABC lives here (rather than under :mod:`loom.policy.base`) so that
:mod:`loom.kernel.prompt` and :mod:`loom.runtime` can type their
``policy:`` parameters against it without violating the kernel/policy
import asymmetry — the kernel must never import :mod:`loom.policy`, but
it MAY import :mod:`loom.contracts`.

The :class:`Agent` Protocol is the public-facing actor shape every
Loom room consumes. Adapters in :mod:`loom.adapters` produce values
satisfying it from ordinary ``send`` / ``stream`` callables.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator, Protocol, runtime_checkable

from loom.kernel.events import Event
from loom.kernel.obligations import UserTurnPlan
from loom.kernel.room import RoomStateView


@runtime_checkable
class Agent(Protocol):
    """Public-facing actor shape consumed by :class:`loom.room.LoomRoom`.

    Every Loom participant — bundled provider proxies and user-supplied
    objects alike — is an ``Agent`` from the room's perspective. The
    contract is intentionally minimal:

    - ``id``    a stable string id, unique within the room.
    - ``stream(prompt) -> Iterator[str]``  yield one or more text
      chunks for the given prompt. The room buffers them, splits the
      preamble for ``[PASS]`` detection, and renders deltas. Returning
      an empty iterator (or yielding only ``""``) means "no draft" —
      equivalent to a soft pass.

    Optional attributes (read by the room via :func:`getattr` when
    converting to :class:`ParticipantWiring`; absent → documented
    defaults):

    - ``persona: str``           agent self-description rendered into
      the prompt. Default ``""``.
    - ``capability_block: str``  short feature/limit summary rendered
      into the prompt. Default ``""``.
    - ``cost_tier: int``         cheaper agents are preferred for slot
      fallback (anchor / default-responder re-resolution). Default ``1``.
    - ``capable: bool``          gate for slot fallback eligibility.
      Default ``True``.
    - ``cancel() -> None``       best-effort hard cancel. Optional.

    The protocol is :func:`runtime_checkable` so ``isinstance(x, Agent)``
    works for duck-typed objects that expose ``id`` and ``stream``.
    """

    id: str

    def stream(self, prompt: str) -> Iterator[str]:  # pragma: no cover
        """Yield text chunks for the given prompt.

        The kernel passes a fully-rendered string prompt (assembled by
        :func:`loom.kernel.prompt.build_prompt`). The adapter does not
        receive structured fields; if your provider needs a chat-message
        format, parse the string inside the adapter.

        Yield strings. Other types are coerced via ``str(...)``.
        Yielding nothing (empty iterator or only ``""``) is "no draft" —
        equivalent to a soft pass.
        """
        ...


class ConversationPolicy(ABC):
    """Pluggable extension layer that decides who may speak in a room.

    A policy classifies each user message into a :class:`UserTurnPlan`
    and (optionally) contributes additional system / role instructions
    rendered into actor prompts. The kernel is the only mutator of
    state and the only emitter of events; policies emit their wishes
    declaratively via :class:`UserTurnPlan` fields
    (``set_turn_taking_mode``, ``set_turn_order``,
    ``advance_turn_pointer``, ``allowed_speakers``,
    ``wait_for_user_after``, etc.).

    PERFORMANCE CONTRACT: :meth:`plan_user_turn` must be synchronous,
    deterministic, non-blocking, and local. Avoid I/O, LLM calls,
    sleeps, and network calls. Return in <10ms typical. The coordinator
    holds its lock across this call to prevent the actor-cursor race,
    so a slow policy blocks every actor thread for the duration. The
    coordinator emits a ``policy_slow`` control event when the call
    exceeds ~100ms (no interruption — Python cannot safely cancel
    arbitrary code).

    ERROR CONTRACT: if :meth:`plan_user_turn` raises, the coordinator
    emits a ``policy_error`` control event and dispatches on its
    ``policy_error_mode``:

    - ``"close_turn"`` (default, fail-closed): turn closes with no
      response. Library-default because "default responder" is a
      Loom-specific assumption that breaks for debate / classroom /
      20-questions policies.
    - ``"default_responder"``: fall back to
      :func:`loom.kernel.obligations.plan_for_default`. Loom opts into
      this for v0.0 behavioral compat.
    - ``"raise"``: re-raise the exception (dev mode).

    STATE CONTRACT (v0): policy instances are NOT journaled. Restart
    instantiates a fresh policy. Stateful policies (debate phase, 20Q
    question count) work in-process but reset across restart. v0.1
    will add ``snapshot()/restore()`` hooks.

    PURITY CONTRACT: policies receive a :class:`RoomStateView` —
    a read-only window onto :class:`RoomState`. The view's
    ``participants`` mapping and ``control.roles`` are
    :class:`MappingProxyType` instances; ``control.turn_order`` and
    ``control.floor_owner`` are tuples. Mutation through these surfaces
    raises ``TypeError`` / ``AttributeError`` at runtime. Policies must
    also not post to the bus — that is the kernel's job. v0.1 enforces
    this with the runtime view + a CI grep over ``loom/policy/**/*.py``
    (see ``tests/test_kernel_kernel_boundary.py``).

    The KERNEL CHARTER (visibility rules, PASS protocol, "do not
    impersonate kernel/system", stream/final separation) is rendered
    by :func:`loom.kernel.prompt.build_prompt` BEFORE
    :meth:`system_prompt` and :meth:`role_prompt` and CANNOT be
    overridden by a policy.

    PLAN-BUILDER HELPERS: import from :mod:`loom.policy`, not from
    :mod:`loom.kernel.obligations` — the public path is
    ``from loom.policy import plan_with_required, plan_for_acknowledgement,
    plan_for_default``. The helpers themselves still live in the kernel
    module; the re-export through :mod:`loom.policy` is the canonical
    import path for policy authors. See
    :mod:`loom.policy.single_responder` for a minimal worked example.
    """

    name: str = "unnamed"

    @abstractmethod
    def plan_user_turn(
        self,
        user_event: Event,
        state: RoomStateView,
    ) -> UserTurnPlan:
        """Classify a user chat event into a :class:`UserTurnPlan`.

        ``state`` is a :class:`RoomStateView` — read-only. Mutating it
        is impossible through the supplied surface (raises
        ``TypeError``); policies must instead express state changes
        declaratively on the returned :class:`UserTurnPlan`.

        P2.7: the v0 ``prior_speaker`` keyword was removed. None of
        the bundled policies used it. A policy that needs follow-up
        detection can compute it from ``state`` and the recent bus
        history, or take it via its own constructor argument.
        """

    def system_prompt(self, participant_id: str,
                      state: RoomStateView) -> str:
        """Additional system instructions appended after the kernel charter.

        The kernel always renders the charter
        (:data:`loom.kernel.prompt.LOOM_PROTOCOL_INSTRUCTIONS`) first;
        the value returned here follows. Default returns ``""`` so
        policies can omit it.

        ``participant_id`` was named ``actor_id`` pre-v0.1; renamed for
        cross-layer naming consistency (P2.4 / audit §5.2).
        """
        return ""

    def role_prompt(self, participant_id: str,
                    state: RoomStateView) -> str:
        """Extra instructions for actors holding distinguished roles.

        Examples: anchor synthesis framing, teacher / quizzer role
        prompts, debater stance assignment. Default returns ``""``.
        """
        return ""
