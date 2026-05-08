"""Property: ThrottleConfig's sliding 60s window prunes correctly.

Invariants:
- A participant who has consumed N tokens in the last window remains
  rate-limited until enough timestamps drop out of the window.
- After the window slides past all stored timestamps, the participant
  can consume again at the full quota.
- The per-channel limit is independent of the per-participant limit:
  a slow producer cannot starve a fast channel and vice versa.
"""
from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from loom.kernel.coordinator import ThrottleConfig


@given(
    quota=st.integers(min_value=1, max_value=20),
    over=st.integers(min_value=1, max_value=10),
)
def test_per_participant_quota_enforced(quota, over):
    """After ``quota`` consumes the participant is rate-limited."""
    t = ThrottleConfig(per_participant_per_min=quota,
                 per_channel_per_min=10_000)
    # Burn through the quota.
    for i in range(quota):
        assert t.try_consume("alice", "main", now=100.0 + i * 0.001) is True
    # Next ``over`` consumes within the same window must fail.
    for i in range(over):
        assert t.try_consume("alice", "main", now=100.5 + i * 0.001) is False


@given(quota=st.integers(min_value=1, max_value=20))
def test_window_slide_re_enables_consumption(quota):
    """After 60s the participant can consume the full quota again."""
    t = ThrottleConfig(per_participant_per_min=quota,
                 per_channel_per_min=10_000)
    for i in range(quota):
        assert t.try_consume("alice", "main", now=100.0 + i * 0.001) is True
    # Window slid past — every prior timestamp is older than 60s.
    for i in range(quota):
        assert t.try_consume(
            "alice", "main", now=200.0 + i * 0.001) is True


@given(
    p_quota=st.integers(min_value=1, max_value=10),
    c_quota=st.integers(min_value=11, max_value=30),
)
def test_channel_limit_independent_of_participant(p_quota, c_quota):
    """Hitting per-participant limit doesn't burn the channel quota.

    Uses many distinct participants so each can post p_quota times
    before being rate-limited; the channel cap is the only ceiling.
    """
    assert c_quota > p_quota  # precondition for the scenario
    t = ThrottleConfig(per_participant_per_min=p_quota,
                 per_channel_per_min=c_quota)
    # alice burns through her per-participant quota.
    for i in range(p_quota):
        assert t.try_consume("alice", "main", now=100.0 + i * 0.001) is True
    # alice is rate-limited.
    assert t.try_consume("alice", "main", now=100.5) is False
    # Use distinct participants for the remaining channel quota; each
    # has a fresh per-participant counter.
    remaining_channel = c_quota - p_quota
    for i in range(remaining_channel):
        assert t.try_consume(
            f"agent{i}", "main", now=101.0 + i * 0.001) is True


@given(
    quota=st.integers(min_value=1, max_value=10),
    n_participants=st.integers(min_value=2, max_value=5),
)
def test_per_participant_independent(quota, n_participants):
    """Different participants share no per-participant counters."""
    t = ThrottleConfig(per_participant_per_min=quota,
                 per_channel_per_min=10_000)
    for p in range(n_participants):
        for i in range(quota):
            assert t.try_consume(
                f"p{p}", "main", now=100.0 + p * 100 + i * 0.001) is True
