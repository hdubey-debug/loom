"""Loom policy base — :class:`BasicPolicy` template-method ABC.

Most policies follow the same shape: pick a set of responders, return
either an acknowledgement (when no one should reply) or a
``plan_with_required`` covering the chosen responders. ``BasicPolicy``
encapsulates that shape so authors override one method
(:meth:`_choose_responders`) and a few small hooks instead of
reconstructing a :class:`UserTurnPlan` by hand.

Use ``BasicPolicy`` when:

- Your policy maps each user post to a *set* of speakers (possibly
  empty), with no cross-turn state and no floor narrowing.
- You don't need a custom acknowledgement reason, custom
  ``allowed_speakers`` (it defaults to the chosen responders), or
  custom ``max_responses`` (defaults to ``len(responders)``).

Use the raw :class:`loom.contracts.ConversationPolicy` ABC when:

- You mutate cross-turn state (round-robin pointer, debate phase,
  20-questions count) — see :class:`loom.policy.round_robin.RoundRobinPolicy`.
- You need different routing per case (vocative vs broadcast vs
  game-mode) — see :class:`loom.policy.default.DefaultPolicy`.
- You set ``allowed_speakers`` to a strict superset of responders, or
  emit declarative control-state changes (``set_turn_order``,
  ``advance_turn_pointer``) on the plan.

Hook overview (override what you need):

- :meth:`_choose_responders` — REQUIRED. Returns the set of
  participant ids that must respond.
- :meth:`_routing_case` — defaults to ``"multi_opinion"``. Override
  with a literal from :data:`loom.kernel.obligations.RoutingCase` when
  appropriate.
- :meth:`_wait_for_user_after` — defaults to ``False``. Set ``True``
  for "talk to one assistant" patterns.
- :meth:`_instruction` — defaults to ``""``. Per-turn instruction
  rendered into actor prompts.
- :meth:`_rationale` / :meth:`_no_responders_rationale` — short
  human-readable strings written into the plan for debugging.
"""

from __future__ import annotations

from abc import abstractmethod

from loom.contracts import ConversationPolicy
from loom.kernel import obligations as obl
from loom.kernel.events import Event
from loom.kernel.room import RoomStateView


class BasicPolicy(ConversationPolicy):
    """Template-method base for policies with the common one-set shape.

    Subclasses implement :meth:`_choose_responders` and (optionally)
    override the routing-case / wait / instruction / rationale hooks.
    See module docstring for when to use this vs the raw ABC.
    """

    def plan_user_turn(
        self,
        user_event: Event,
        state: RoomStateView,
    ) -> obl.UserTurnPlan:
        target_event_ids = [user_event.id] if user_event.id is not None else []
        responders = self._choose_responders(user_event, state)
        if not responders:
            return obl.plan_for_acknowledgement(
                target_event_ids=target_event_ids,
                rationale=self._no_responders_rationale(state),
            )
        responders_list = sorted(responders)
        return obl.plan_with_required(
            responders_list,
            routing_case=self._routing_case(),
            target_event_ids=target_event_ids,
            reason=self.name,
            rationale=self._rationale(responders_list, state),
            allowed_speakers=set(responders_list),
            max_responses=len(responders_list),
            wait_for_user_after=self._wait_for_user_after(),
            instruction=self._instruction(state),
        )

    @abstractmethod
    def _choose_responders(
        self,
        user_event: Event,
        state: RoomStateView,
    ) -> set[str]:
        """Return the set of participant ids that must respond.

        An empty set yields an acknowledgement plan (no actor turn
        opens). The kernel passes a read-only :class:`RoomStateView`;
        do not mutate it.
        """

    def _routing_case(self) -> obl.RoutingCase:
        return "multi_opinion"

    def _wait_for_user_after(self) -> bool:
        return False

    def _instruction(self, state: RoomStateView) -> str:
        return ""

    def _rationale(
        self,
        responders: list[str],
        state: RoomStateView,
    ) -> str:
        return self.name

    def _no_responders_rationale(self, state: RoomStateView) -> str:
        return "no responders chosen"
