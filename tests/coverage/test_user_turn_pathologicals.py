"""Targeted coverage of loom/kernel/user_turn.py uncovered paths.

Covers the routing_case property, the can_draft cap-respecting branch,
and the _cap hook that gives subclasses a way to inject per-plan caps.
"""

from __future__ import annotations

from loom.kernel.obligations import (
    ResponseObligation,
    UserTurnPlan,
)
from loom.kernel.user_turn import UserTurn, make_user_turn


def _make_turn() -> UserTurn:
    plan = UserTurnPlan(
        requires_response=True,
        required_participants=["alice"],
        optional_participants=[],
        allowed_speakers=["alice"],
        obligations=[
            ResponseObligation(
                id=0,
                participant_id="alice",
                level="must",
                target_event_ids=[0],
                reason="addressed",
            ),
        ],
        routing_case="direct_mention",
        rationale="addressed",
    )
    turn, _ = make_user_turn(turn_id=1, user_event_id=0, plan=plan)
    return turn


def test_routing_case_property_returns_frozen_plan_routing_case():
    """Covers: user_turn.py:71-73 — routing_case is a frozen-plan view."""
    turn = _make_turn()
    assert turn.routing_case == "direct_mention"


def test_can_draft_with_no_cap_returns_true():
    """Covers: user_turn.py:140-142 — default _cap returns None → True."""
    turn = _make_turn()
    assert turn.can_draft("alice") is True
    assert turn.can_draft("anyone") is True


def test_can_draft_with_subclass_cap_enforced():
    """Covers: user_turn.py:143 — speaker count < cap branch.

    The default ``_cap`` returns None (the coordinator enforces caps at
    lease time). Subclasses can override ``_cap`` to inject a per-plan
    cap; this test exercises that future-proofed path.
    """

    class CappedTurn(UserTurn):
        def _cap(self):  # type: ignore[override]
            return 1

    plan = UserTurnPlan(
        requires_response=True,
        required_participants=["alice"],
        optional_participants=[],
        allowed_speakers=["alice"],
        obligations=[],
        routing_case="direct_mention",
        rationale="x",
    )
    capped = CappedTurn(
        id=1,
        user_event_id=0,
        started_at=0.0,
        frozen_plan=plan,
    )

    # Below cap → True.
    assert capped.can_draft("alice") is True
    # At/over cap → False.
    capped.mark_drafted("alice")
    assert capped.can_draft("alice") is False


def test_default_cap_returns_none():
    """Covers: user_turn.py:149 — base class _cap returns None."""
    turn = _make_turn()
    assert turn._cap() is None


def test_post_init_preserves_explicit_last_activity_at():
    """Covers: user_turn.py:56 branch — when last_activity_at was set explicitly.

    The default-zero branch is exercised everywhere; this verifies the
    "already set" branch (56->exit) does NOT clobber the value.
    """
    plan = UserTurnPlan(
        requires_response=True,
        required_participants=["alice"],
        optional_participants=[],
        allowed_speakers=["alice"],
        obligations=[],
        routing_case="direct_mention",
        rationale="x",
    )
    turn = UserTurn(
        id=1,
        user_event_id=0,
        started_at=100.0,
        frozen_plan=plan,
        last_activity_at=42.0,
    )
    assert turn.last_activity_at == 42.0
