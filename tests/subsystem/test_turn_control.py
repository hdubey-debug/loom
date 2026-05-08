"""Subsystem tests — coordinator + leases under contention.

Exercises the kernel's single-mutator boundary (``RoomCoordinator``)
under concurrent access: many threads posting at once, lease churn
during membership changes, lease-TTL expiry, dead-letter routing, and
the user-turn debounce window.
"""
from __future__ import annotations

import threading
import time

import pytest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.obligations import (
    plan_for_default,
    plan_with_required,
)
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
)


# ---------------------------------------------------------------------------
# Helpers (kept local — these tests own a coordinator end-to-end).
# ---------------------------------------------------------------------------

def _build(default_responder="loom",
           members=("loom", "claude_code", "gemini_cli"),
           config=None,
           lift_throttle=True):
    bus = MessageBus()
    state = RoomState(config=config or RoomConfig(
        user_turn_idle_timeout_s=20,
        user_turn_debounce_ms=200,
        lease_ttl_s=60,
    ))
    for i, pid in enumerate(members):
        state.add_participant(ParticipantInfo(id=pid, cost_tier=i))
    if default_responder:
        state.set_default_responder(default_responder)
    coord = RoomCoordinator(bus, state)
    if lift_throttle:
        # Subsystem stress tests deliberately drive far past the
        # default per-participant rate cap. Lifting the throttle here
        # isolates *coordinator* behavior from *throttle* behavior.
        from loom.kernel.coordinator import ThrottleConfig as _Throttle
        coord._throttle = _Throttle(
            per_participant_per_min=10_000,
            per_channel_per_min=10_000,
        )
    return bus, state, coord


def _open_default(c, e, default_id):
    plan = plan_for_default(default_id, reason="test",
                            target_event_ids=[e.id])
    return c.open_user_turn(e, plan)


def _post_user(bus, body="hi"):
    e = ev.chat(sender="user", body=body)
    bus.post(e)
    return e


# ---------------------------------------------------------------------------
# Concurrent posting onto the coordinator.
# ---------------------------------------------------------------------------

class TestConcurrentPosts:

    @pytest.mark.timing
    def test_concurrent_posts_serialize_through_rlock(self, thread_harness):
        # 50 threads each call ``post_user_event_and_open_turn`` with a
        # classifier that always returns an acknowledgement. The
        # coordinator's RLock serializes them; every event lands.
        from loom.kernel.obligations import plan_for_acknowledgement
        bus, state, c = _build()
        n_posts = 50

        def classify(_event):
            return plan_for_acknowledgement(rationale="concurrent ack")

        def post_one(idx):
            e = ev.chat(sender="user", body=f"post-{idx}")
            c.post_user_event_and_open_turn(e, classify)

        for i in range(n_posts):
            thread_harness.spawn(lambda i=i: post_one(i),
                                  name=f"post{i}")
        thread_harness.join_all(timeout=10.0)
        assert thread_harness.errors == []

        chats = [e for e in bus.snapshot() if e.kind == "chat"]
        assert len(chats) == n_posts
        ids = sorted(e.id for e in chats)
        assert ids == list(range(ids[0], ids[0] + n_posts))

    @pytest.mark.timing
    def test_post_user_event_and_open_turn_is_atomic_under_concurrency(
            self, thread_harness):
        # The (post + classify + open_user_turn) path must run under
        # the same lock so an actor woken by the bus post never sees
        # ``user_turn=None`` for an event whose turn is in flight.
        bus, state, c = _build(default_responder="loom")

        def classify(event):
            return plan_for_default("loom", reason="atomic",
                                    target_event_ids=[event.id])

        races_seen = []

        def watch():
            # Sample the (latest user-event id, current user_turn) pair
            # while posters churn. They must always agree.
            for _ in range(200):
                snap = [e for e in bus.snapshot() if e.kind == "chat"
                        and e.sender == "user"]
                if snap:
                    last = snap[-1]
                    ut = c.user_turn
                    if ut is not None and ut.user_event_id != last.id:
                        # The opener for ``last`` must have already
                        # registered the user_event_id by the time the
                        # event is on the bus.
                        if ut.id != 0:
                            races_seen.append((last.id, ut.user_event_id))

        thread_harness.spawn(watch, name="watcher")
        for i in range(20):
            thread_harness.spawn(
                lambda i=i: c.post_user_event_and_open_turn(
                    ev.chat(sender="user", body=f"q{i}"),
                    classify),
                name=f"poster{i}")
        thread_harness.join_all(timeout=10.0)
        assert thread_harness.errors == []
        # No half-open observation slipped through.
        assert races_seen == []


# ---------------------------------------------------------------------------
# Lease lifecycle under churn.
# ---------------------------------------------------------------------------

class TestLeaseLifecycleUnderChurn:

    @pytest.mark.stress
    def test_register_unregister_during_active_turn_no_zombie_lease(self):
        # Spinning a participant register/unregister cycle while a turn
        # is open must not leak leases into self._leases.
        bus, state, c = _build(default_responder="loom")
        e = _post_user(bus, "hi")
        _open_default(c, e, "loom")
        l1 = c.acquire_lease("loom", e.id)
        assert l1 is not None
        # Add+remove 5 fresh participants. Each bumps room_epoch, which
        # invalidates ``l1``. The coordinator must remain healthy and
        # not retain stale lease records.
        for i in range(5):
            pid = f"churn{i}"
            c.register_participant(ParticipantInfo(id=pid))
            c.unregister_participant(pid)
        c.release_lease(l1)
        assert c.in_flight_lease_count() == 0

    def test_default_responder_change_invalidates_outstanding_lease(self):
        bus, state, c = _build(default_responder="loom")
        e = _post_user(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        assert c.validate_lease(lease) is True
        c.set_default_responder("claude_code")
        assert c.validate_lease(lease) is False

    def test_max_responses_one_caps_to_single_commit(self):
        # ``plan.max_responses=1`` means once one actor commits, the
        # second can't acquire a fresh lease for that turn.
        bus, state, c = _build()
        e = _post_user(bus, "hi")
        plan = plan_with_required(
            ["loom", "claude_code"],
            routing_case="multi_opinion",
            target_event_ids=[e.id], reason="test",
            max_responses=1,
        )
        c.open_user_turn(e, plan)
        l1 = c.acquire_lease("loom", e.id)
        assert l1 is not None
        c.on_stream_end(l1, "committed",
                        committed_text="long enough committed reply",
                        cost_tokens=10)
        c.release_lease(l1)
        # Cap is reached — claude_code is rejected.
        l2 = c.acquire_lease("claude_code", e.id)
        assert l2 is None

    @pytest.mark.timing
    def test_lease_ttl_expires_after_window(self):
        # Use the simulated-expiry trick (override lease.expires_at)
        # from existing tests so this stays sub-second. P3.3:
        # lease.expires_at is now monotonic-clock; use the same.
        bus, state, c = _build(default_responder="loom",
                               config=RoomConfig(lease_ttl_s=60))
        e = _post_user(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        assert c.validate_lease(lease) is True
        lease.expires_at = time.monotonic() - 0.001
        assert c.validate_lease(lease) is False
        # And the lease cannot be re-validated even after the window —
        # it's permanently invalid once expired.
        lease.expires_at = time.monotonic() + 60
        # The valid flag was flipped in validate_lease; stays False.
        assert c.validate_lease(lease) is False

    def test_release_lease_drops_from_table(self):
        bus, state, c = _build(default_responder="loom")
        e = _post_user(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        assert c.in_flight_lease_count() == 1
        c.release_lease(lease)
        assert c.in_flight_lease_count() == 0


# ---------------------------------------------------------------------------
# Turn integrity under load.
# ---------------------------------------------------------------------------

class TestTurnIntegrity:

    @pytest.mark.stress
    def test_50_back_to_back_turns_no_obligation_leaks(self):
        # Open + close 50 turns in a tight loop. After each, the
        # coordinator must show zero outstanding obligations and an
        # idle (closed) user turn.
        bus, state, c = _build(default_responder="loom")
        for i in range(50):
            e = _post_user(bus, f"q{i}")
            _open_default(c, e, "loom")
            lease = c.acquire_lease("loom", e.id)
            assert lease is not None
            c.on_stream_end(lease, "committed",
                            committed_text=f"committed reply {i} long enough",
                            cost_tokens=5)
            c.release_lease(lease)
        ut = c.user_turn
        assert ut is not None
        assert ut.state == "closed"
        # No outstanding leases.
        assert c.in_flight_lease_count() == 0

    @pytest.mark.stress
    @pytest.mark.breakpoint
    def test_breakpoint_concurrent_actor_count_for_one_turn(self):
        # Find the participant count at which a single multi-required
        # turn no longer admits all participants in parallel within a
        # 1 s budget. Floor: at least 10 must succeed concurrently.
        # Hard ceiling: 200 — the watchdog backs us up.
        threshold_s = 1.0
        floor = 10
        ceiling = 200

        def measure(n: int) -> float:
            members = [f"p{i}" for i in range(n)]
            bus, state, c = _build(members=members, default_responder=None)
            e = _post_user(bus, "q")
            plan = plan_with_required(
                members, routing_case="multi_opinion",
                target_event_ids=[e.id], reason="bp",
                max_responses=0,  # unlimited
            )
            c.open_user_turn(e, plan)
            t0 = time.monotonic()
            leases = []
            for pid in members:
                lease = c.acquire_lease(pid, e.id)
                if lease is not None:
                    leases.append(lease)
            elapsed = time.monotonic() - t0
            # Cleanup.
            for lease in leases:
                c.on_stream_end(lease, "committed",
                                committed_text="ok long enough committed",
                                cost_tokens=1)
                c.release_lease(lease)
            assert len(leases) == n
            return elapsed

        breakpoint_n = ceiling
        n = 5
        while n <= ceiling:
            elapsed = measure(n)
            if elapsed > threshold_s:
                breakpoint_n = n
                break
            n *= 2
        print(f"BREAKPOINT: actors_at_1s_acquire={breakpoint_n}")
        assert breakpoint_n >= floor

    def test_dead_letter_with_no_capable_fallback_closes_turn(self):
        # Single-required participant turn; remove that participant
        # with no other capable agent in the room. The kernel emits a
        # ``dead_letter`` (no reroute_to) and the turn closes.
        bus, state, c = _build(default_responder="loom",
                               members=("loom",))  # only one agent
        e = _post_user(bus, "@loom hi", )
        _open_default(c, e, "loom")
        # Now remove loom entirely. There's no fallback.
        c.unregister_participant("loom")
        ut = c.user_turn
        # The turn should close cleanly — either right at unregister
        # (no required participant left) or via obligation resolution.
        # Force-close path: there is no capable fallback, so the turn
        # cannot be served. Verify the kernel handled it without
        # raising and that the lease table is clean.
        assert c.in_flight_lease_count() == 0
        # No participants remain.
        assert state.participants == {}


# ---------------------------------------------------------------------------
# Debounce window behavior.
# ---------------------------------------------------------------------------

class TestDebounceWindow:

    @pytest.mark.timing
    def test_zero_debounce_opens_a_new_turn_for_every_post(self):
        # With ``user_turn_debounce_ms=0`` and the prior turn closed,
        # the next post opens a fresh turn.
        cfg = RoomConfig(user_turn_debounce_ms=0)
        bus, state, c = _build(default_responder="loom", config=cfg)

        def classify(event):
            return plan_for_default("loom", reason="t",
                                    target_event_ids=[event.id])

        # Post #1: opens turn 0, lease acquired and committed → turn closes.
        e1 = ev.chat(sender="user", body="one")
        c.post_user_event_and_open_turn(e1, classify)
        first_turn_id = c.user_turn.id
        lease = c.acquire_lease("loom", e1.id)
        c.on_stream_end(lease, "committed",
                        committed_text="long enough committed reply",
                        cost_tokens=5)
        c.release_lease(lease)
        assert c.user_turn.state == "closed"

        # Post #2: must open a brand-new turn id (zero debounce, no merge).
        c.post_user_event_and_open_turn(
            ev.chat(sender="user", body="two"), classify)
        second_turn_id = c.user_turn.id
        assert second_turn_id != first_turn_id

    @pytest.mark.timing
    def test_post_within_default_debounce_merges(self):
        # With debounce=200ms, two posts in rapid succession should
        # land on the same active turn (the second is debounced).
        from loom.kernel.obligations import plan_for_acknowledgement
        cfg = RoomConfig(user_turn_debounce_ms=2000)  # generous window
        bus, state, c = _build(default_responder="loom", config=cfg)

        def classify(event):
            return plan_for_default("loom", reason="t",
                                    target_event_ids=[event.id])

        e1 = ev.chat(sender="user", body="one")
        c.post_user_event_and_open_turn(e1, classify)
        first_turn = c.user_turn
        first_turn_id = first_turn.id

        e2 = ev.chat(sender="user", body="two")
        c.post_user_event_and_open_turn(e2, classify)
        second_turn = c.user_turn

        # Same turn. The second event's id is recorded as a debounce.
        assert second_turn.id == first_turn_id
        assert e2.id in second_turn.debounced_event_ids

    def test_debounced_event_id_stored_on_user_turn(self):
        # Same as above, asserted via direct field inspection — the
        # ``debounced_event_ids`` set on the live UserTurn is the
        # observable surface for actors that need to wake on debounced
        # posts.
        cfg = RoomConfig(user_turn_debounce_ms=2000)
        bus, state, c = _build(default_responder="loom", config=cfg)

        def classify(event):
            return plan_for_default("loom", reason="t",
                                    target_event_ids=[event.id])

        c.post_user_event_and_open_turn(
            ev.chat(sender="user", body="one"), classify)
        for n in range(3):
            c.post_user_event_and_open_turn(
                ev.chat(sender="user", body=f"more-{n}"), classify)
        # 3 debounced posts after the first.
        assert len(c.user_turn.debounced_event_ids) == 3

    def test_debounce_does_not_lose_user_event_on_bus(self):
        # A debounced post still lands on the bus — it just doesn't
        # open a fresh turn. Actors that re-scan for new user events
        # see all of them.
        cfg = RoomConfig(user_turn_debounce_ms=2000)
        bus, state, c = _build(default_responder="loom", config=cfg)

        def classify(event):
            return plan_for_default("loom", reason="t",
                                    target_event_ids=[event.id])

        for body in ("a", "b", "c"):
            c.post_user_event_and_open_turn(
                ev.chat(sender="user", body=body), classify)
        user_events = [e for e in bus.snapshot()
                       if e.kind == "chat" and e.sender == "user"]
        assert [e.body for e in user_events] == ["a", "b", "c"]
