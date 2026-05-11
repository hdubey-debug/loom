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
from dataclasses import dataclass
from typing import (
    Any,
    Iterator,
    NamedTuple,
    Optional,
    Protocol,
    runtime_checkable,
)

from loom.kernel.events import Event
from loom.kernel.obligations import UserTurnPlan
from loom.kernel.room import RoomStateView


@runtime_checkable
class TriggerPriorityFn(Protocol):
    """Function signature for the actor's trigger-priority hook.

    Returns the priority class for ``event`` from ``my_id``'s
    perspective (lower = higher priority), or ``None`` if the event
    is not actionable. Used by
    :func:`loom.kernel.actor.pick_priority_trigger` to choose which
    event in a wakeup batch should drive a draft.

    The kernel default
    (:data:`loom.kernel.actor.DEFAULT_TRIGGER_PRIORITY`) implements
    the v0.1.2 three-class scheme: direct user mention (1), dead-letter
    or rerouted obligation (2), required obligation in the current
    user turn (3). Custom hooks plug in via
    :attr:`loom.kernel.room.RoomConfig.trigger_priority`.
    """

    def __call__(self, event: Event, my_id: str, user_turn: Any) -> Optional[int]: ...


@dataclass(frozen=True)
class PromptSection:
    """A policy-supplied prompt section injected by :func:`build_prompt`.

    Injected into the system preamble AFTER the kernel charter (which
    is fixed and unconditional) and after the policy's
    :meth:`ConversationPolicy.role_prompt` output. The section's
    ``name`` is rendered as a comment header so prompt diffs are easy
    to attribute; ``text`` is the body.
    """

    name: str
    text: str


class LeaseCheckResult(NamedTuple):
    """Return value from :meth:`LeaseCheck.check`.

    ``passed=True`` lets the lease-grant chain continue to the next
    check; ``passed=False`` immediately rejects the lease and emits a
    ``lease_denied`` control event carrying ``deny_reason`` and the
    failing check's name.
    """

    passed: bool
    deny_reason: Optional[str] = None


PASSED: LeaseCheckResult = LeaseCheckResult(True, None)


@runtime_checkable
class LeaseCheck(Protocol):
    """One gate in the lease-grant chain.

    Custom checks plug into :attr:`RoomConfig.lease_checks` to extend
    or replace the default 8-step chain (open turn / participant
    valid+active / allowed speaker / per-speaker cap / max-responses
    cap / throttle / budget). Each check returns
    :class:`LeaseCheckResult`; the first failing check rejects the
    lease and emits a ``lease_denied`` event with the check's ``name``
    and ``deny_reason`` for observability.

    Checks are called under the coordinator's lock, so they MUST be
    fast and side-effect free — read-only inspection of the supplied
    context only. A check that raises is treated as a denial with
    deny_reason ``"check_raised:<class_name>"``.
    """

    name: str

    def check(
        self, *, holder: str, trigger_event_id: int, is_direct_mention: bool, coordinator: Any
    ) -> LeaseCheckResult: ...


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
    ``participants`` mapping (whose values are
    :class:`ParticipantInfoView` instances) and ``control.roles`` are
    :class:`MappingProxyType` instances; ``control.turn_order`` is a
    tuple. Mutation through these surfaces raises ``TypeError`` /
    ``AttributeError`` / ``FrozenInstanceError`` at runtime. Policies
    must also not post to the bus — that is the kernel's job. v0.1
    enforces this with the runtime view + a CI grep over
    ``loom/policy/**/*.py`` (see
    ``tests/test_kernel_kernel_boundary.py``).

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

    def charter_text(self, state: RoomStateView) -> str:
        """Policy-supplied addendum to the kernel charter section.

        Rendered immediately after the kernel's fixed
        :data:`loom.kernel.prompt.LOOM_PROTOCOL_INSTRUCTIONS` and
        BEFORE persona / participant id / topic. Use to describe
        policy-specific behavioral rules that should sit alongside the
        protocol-level rules. The kernel charter is unconditional and
        policy-supplied text cannot precede it — preserving invariant 5.

        Default emits a one-line advisory when the room is in
        round-robin mode (``state.control.turn_order`` non-empty). All
        bundled policies inherit this default; subclasses can override
        to suppress (return ``""``) or extend with policy-specific
        framing.
        """
        order = state.control.turn_order
        if order:
            rotation = ", ".join(order)
            return (
                "Round-robin mode is active. Speakers rotate in this "
                f"order: {rotation}. Wait for the user to address you "
                "before drafting."
            )
        return ""

    def system_prompt(self, participant_id: str, state: RoomStateView) -> str:
        """Additional system instructions appended after the kernel charter.

        The kernel always renders the charter
        (:data:`loom.kernel.prompt.LOOM_PROTOCOL_INSTRUCTIONS`) first,
        followed by the policy's :meth:`charter_text`; the value
        returned here follows both. Default returns ``""`` so policies
        can omit it.

        ``participant_id`` was named ``actor_id`` pre-v0.1; renamed for
        cross-layer naming consistency (P2.4 / audit §5.2).
        """
        return ""

    def role_prompt(self, participant_id: str, state: RoomStateView) -> str:
        """Extra instructions for actors holding distinguished roles.

        Examples: anchor synthesis framing, teacher / quizzer role
        prompts, debater stance assignment. Default returns ``""``.
        """
        return ""

    def should_post_response(
        self,
        *,
        body: str,
        state: RoomStateView,
        participant_id: str,
    ) -> bool:
        """Veto the post-stream commit of a participant's response.

        Called by :func:`loom.kernel.streaming.run_streaming_call`
        AFTER the kernel's own filters (empty / idle-phrase / IoU
        loop-guard). Returning ``False`` suppresses the response;
        returning ``True`` lets the commit proceed.

        Default ``True`` — the kernel's filters already cover
        idle-phrase + bag-of-words duplicate detection. Policies that
        want additional veto rules (semantic similarity check, an
        off-topic detector, a rate-limiter) override this hook.
        Buggy hooks that raise are treated as ``True`` so a bad policy
        cannot lose a legitimate response.
        """
        del body, state, participant_id
        return True

    def prompt_sections(
        self,
        *,
        state: RoomStateView,
        participant_id: str,
        trigger_event: Optional[Event],
    ) -> list[PromptSection]:
        """Additional system-preamble sections supplied by the policy.

        Each :class:`PromptSection` is rendered into the system
        preamble immediately after the kernel charter, persona, topic,
        participant id, capabilities, and the legacy ``system_prompt``
        / ``role_prompt`` blocks — i.e. the policy gets a stable
        position late in the preamble but before the transcript and
        trigger pointer.

        Default returns ``[]``; the bundled policies emit no extra
        sections. Custom policies can populate this to inject
        scenario-specific framing (taxonomy hints, slot-aware
        directives, debate stance, etc.) without subclassing
        :func:`loom.kernel.prompt.build_prompt`.

        ``trigger_event`` is the same value :func:`build_prompt`
        receives — the event the actor is responding to, or ``None``
        in the idle/no-trigger case.
        """
        del state, participant_id, trigger_event
        return []

    def dead_letter_target(
        self, *, state: RoomStateView, removed_participant: str
    ) -> Optional[str]:
        """Pick the reroute target for a dead-lettered @mention.

        Called by :meth:`RoomCoordinator._dead_letter_pending_mentions`
        when an @-mention can no longer be served because the named
        participant was removed mid-turn. The hook returns the pid that
        should inherit the obligation, or ``None`` to drop the mention
        entirely.

        Default: the configured ``default_responder_id`` if it points
        to an active+capable participant, otherwise the cheapest
        active+capable participant (preserves v0.1.2 kernel behavior).

        ``removed_participant`` is informational — the participant has
        already been removed from ``state.participants`` by the time
        this hook fires.
        """
        del removed_participant
        if state.default_responder_id:
            info = state.participants.get(state.default_responder_id)
            if info is not None and info.active and info.capable:
                return state.default_responder_id
        candidates = [p for p in state.participants.values() if p.active and p.capable]
        if not candidates:
            return None
        candidates.sort(key=lambda p: (p.cost_tier, p.id))
        return candidates[0].id
