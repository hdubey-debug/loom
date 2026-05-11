"""Loom round-robin policy — one participant per user post, in fixed order.

The first user post arms round-robin mode (``set_turn_order`` populated
with the configured order; a non-empty ``turn_order`` is itself the
round-robin mode signal). Subsequent posts route to the rotation pointer
the kernel maintains. ``advance_turn_pointer=True`` is set on every plan
so the kernel rotates on close.

Removing a participant mid-rotation is handled by skipping inactive ids
when picking the speaker — the visible rotation slot is preserved
across the gap. If no configured participants are currently active and
capable, the turn becomes an acknowledgement.

Reference for authors: this is the only built-in policy that mutates
state declaratively (via ``set_turn_order`` / ``advance_turn_pointer``
on the :class:`UserTurnPlan`). It does NOT touch :class:`RoomState` or
the bus directly — the kernel applies the requested transitions when
opening / closing the turn.
"""

from __future__ import annotations

from typing import Optional

from loom.contracts import ConversationPolicy
from loom.kernel import obligations as obl
from loom.kernel.events import Event
from loom.kernel.room import RoomControlStateView, RoomStateView


class RoundRobinPolicy(ConversationPolicy):
    """Rotate through a fixed order, one speaker per user post.

    Subclasses :class:`ConversationPolicy` directly (not
    :class:`loom.policy.BasicPolicy`) because the rotation logic needs
    declarative state mutation on the plan (``set_turn_order``,
    ``advance_turn_pointer``) — :class:`BasicPolicy` is shaped for the
    simpler "filter responders, return must-plan" case.
    """

    name = "round_robin"

    def __init__(self, order: list[str]) -> None:
        if not order:
            raise ValueError("RoundRobinPolicy requires at least one id")
        # Defensive copy + dedupe-while-preserving-order.
        seen: set[str] = set()
        self._order: list[str] = []
        for pid in order:
            if not isinstance(pid, str) or not pid:
                raise ValueError("RoundRobinPolicy order ids must be non-empty strings")
            if pid not in seen:
                seen.add(pid)
                self._order.append(pid)

    @property
    def order(self) -> list[str]:
        return list(self._order)

    def plan_user_turn(
        self,
        user_event: Event,
        state: RoomStateView,
    ) -> obl.UserTurnPlan:
        target_event_ids = [user_event.id] if user_event.id is not None else []
        active_capable = {
            pid for pid, info in state.participants.items() if info.active and info.capable
        }
        control = state.control

        # First post — arm round-robin (turn_order empty means broadcast)
        # and pick the first live speaker from our configured order.
        if not control.turn_order:
            speaker = self._first_live(self._order, active_capable)
            if speaker is None:
                return obl.plan_for_acknowledgement(
                    target_event_ids=target_event_ids,
                    rationale=("round-robin start: no configured participants are active+capable"),
                )
            return obl.plan_with_required(
                [speaker],
                routing_case="direct_mention",
                target_event_ids=target_event_ids,
                reason="round_robin_start",
                rationale=f"round-robin start: {speaker}",
                allowed_speakers={speaker},
                max_responses=1,
                wait_for_user_after=True,
                instruction=self._instruction(speaker),
                set_turn_order=list(self._order),
                advance_turn_pointer=True,
            )

        # Subsequent posts — use the kernel's rotation pointer over the
        # live subset of state.control.turn_order.
        speaker = self._pick_from_rotation(control, active_capable)
        if speaker is None:
            return obl.plan_for_acknowledgement(
                target_event_ids=target_event_ids,
                rationale=("round-robin: no live participants in rotation order"),
            )
        return obl.plan_with_required(
            [speaker],
            routing_case="direct_mention",
            target_event_ids=target_event_ids,
            reason="round_robin",
            rationale=(f"round-robin: {speaker} (idx {control.next_speaker_idx})"),
            allowed_speakers={speaker},
            max_responses=1,
            wait_for_user_after=True,
            instruction=self._instruction(speaker),
            advance_turn_pointer=True,
        )

    @staticmethod
    def _first_live(order: list[str], active_capable: set[str]) -> Optional[str]:
        for pid in order:
            if pid in active_capable:
                return pid
        return None

    @staticmethod
    def _pick_from_rotation(
        control: RoomControlStateView, active_capable: set[str]
    ) -> Optional[str]:
        live = [pid for pid in control.turn_order if pid in active_capable]
        if not live:
            return None
        idx = control.next_speaker_idx % len(live)
        return live[idx]

    @staticmethod
    def _instruction(speaker: str) -> str:
        return (
            f"Round-robin mode: you ({speaker}) are up this turn. "
            "Other agents are silent until the next user post. "
            "Make one move, then stop."
        )
