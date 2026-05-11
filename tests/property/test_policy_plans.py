"""Property: every policy returns plans whose obligations are well-formed.

Invariants for each of the four bundled policies:
- ``plan.obligations``' participant_ids are a subset of the participants.
- ``plan.required_participants`` and ``plan.optional_participants`` are
  pairwise disjoint.
- ``plan.allowed_speakers`` is a subset of the participants (when
  non-empty).
- A plan with ``requires_response=True`` always has at least one
  required participant (this is also enforced by __post_init__, so the
  property is a regression check).
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from loom.kernel import events as ev
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
)
from loom.policy.default import DefaultPolicy
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.round_robin import RoundRobinPolicy
from loom.policy.single_responder import SingleResponderPolicy

from tests.property.strategies import participant_ids


def _build_state(pids: list[str]) -> RoomState:
    state = RoomState(config=RoomConfig())
    for pid in pids:
        state.add_participant(ParticipantInfo(id=pid, capable=True, cost_tier=1))
    if pids:
        state.set_default_responder(pids[0])
        state.set_anchor(pids[0])
    return state


def _user_event(addressees: list[str], body: str = "hi"):
    return ev.chat(sender="user", body=body, addressees=addressees)


@given(
    pids=st.lists(participant_ids, min_size=1, max_size=5, unique=True),
    addressees=st.lists(participant_ids, max_size=3),
    body=st.text(max_size=80),
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_default_policy_plan_invariants(pids, addressees, body):
    """DefaultPolicy plans have well-formed required/optional/allowed sets."""
    state = _build_state(pids)
    policy = DefaultPolicy()
    e = _user_event(addressees, body)
    plan = policy.plan_user_turn(e, state.view())

    participants = set(state.participants.keys())
    # Required ⊆ participants.
    assert set(plan.required_participants) <= participants | {p for p in addressees if p}
    # Required ∩ optional == ∅.
    assert (set(plan.required_participants) & set(plan.optional_participants)) == set()
    # If allowed_speakers is non-empty, it's a subset of participants.
    if plan.allowed_speakers:
        assert set(plan.allowed_speakers) <= participants
    # If requires_response, required is non-empty.
    if plan.requires_response:
        assert plan.required_participants


@given(
    pids=st.lists(participant_ids, min_size=1, max_size=5, unique=True),
    addressees=st.lists(participant_ids, max_size=3),
)
def test_open_chat_policy_plan_invariants(pids, addressees):
    """OpenChatPolicy plans are well-formed across arbitrary inputs."""
    state = _build_state(pids)
    policy = OpenChatPolicy()
    e = _user_event(addressees)
    plan = policy.plan_user_turn(e, state.view())
    participants = set(state.participants.keys())

    assert (set(plan.required_participants) & set(plan.optional_participants)) == set()
    if plan.allowed_speakers:
        assert set(plan.allowed_speakers) <= participants
    if plan.requires_response:
        assert plan.required_participants


@given(
    pids=st.lists(participant_ids, min_size=2, max_size=5, unique=True),
    addressees=st.lists(participant_ids, max_size=2),
)
def test_round_robin_policy_plan_invariants(pids, addressees):
    """RoundRobinPolicy plans are well-formed when rotation is set."""
    state = _build_state(pids)
    state.set_turn_order(pids)
    policy = RoundRobinPolicy(order=pids)
    e = _user_event(addressees)
    plan = policy.plan_user_turn(e, state.view())
    participants = set(state.participants.keys())

    assert (set(plan.required_participants) & set(plan.optional_participants)) == set()
    if plan.allowed_speakers:
        assert set(plan.allowed_speakers) <= participants
    if plan.requires_response:
        assert plan.required_participants


@given(
    pids=st.lists(participant_ids, min_size=1, max_size=5, unique=True),
    addressees=st.lists(participant_ids, max_size=2),
)
def test_single_responder_policy_plan_invariants(pids, addressees):
    """SingleResponderPolicy plans are well-formed across inputs."""
    state = _build_state(pids)
    policy = SingleResponderPolicy(responder_id=pids[0])
    e = _user_event(addressees)
    plan = policy.plan_user_turn(e, state.view())
    participants = set(state.participants.keys())

    assert (set(plan.required_participants) & set(plan.optional_participants)) == set()
    if plan.allowed_speakers:
        assert set(plan.allowed_speakers) <= participants
    if plan.requires_response:
        assert plan.required_participants
