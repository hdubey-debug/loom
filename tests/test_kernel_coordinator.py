"""Tests for ``loom.kernel.coordinator`` — RoomCoordinator + TurnLease."""
from __future__ import annotations

import time
import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import (
    BudgetConfig,
    LoopGuardConfig,
    RoomCoordinator,
    ThrottleConfig,
    TurnLease,
)
from loom.kernel.obligations import (
    plan_for_acknowledgement,
    plan_for_default,
    plan_with_required,
)
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
)


def _setup(
    *,
    default_responder=None,
    members=("loom", "claude_code", "gemini_cli"),
    config=None,
):
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
    return bus, state, RoomCoordinator(bus, state)


def _user_post(bus, body="hi", addressees=None):
    e = ev.chat(sender="user", body=body, addressees=list(addressees or []))
    bus.post(e)
    return e


def _open_with(c, e, *, required, optional=(), routing_case="direct_mention",
               reason="test"):
    plan = plan_with_required(
        list(required), routing_case=routing_case,
        target_event_ids=[e.id], reason=reason,
        optional=list(optional),
    )
    return c.open_user_turn(e, plan)


def _open_default(c, e, default_id, *, reason="fallback"):
    plan = plan_for_default(default_id, reason=reason,
                            target_event_ids=[e.id])
    return c.open_user_turn(e, plan)


# ---------------------------------------------------------------------------
# Helper sub-classes
# ---------------------------------------------------------------------------

class LoopGuardTests(unittest.TestCase):
    def test_first_reply_passes(self):
        g = LoopGuardConfig()
        self.assertFalse(g.is_idle_dup("a", "standing by"))

    def test_short_dup_caught(self):
        g = LoopGuardConfig()
        g.record("a", "standing by")
        self.assertTrue(g.is_idle_dup("a", "standing by"))

    def test_long_text_passes_even_when_dup(self):
        g = LoopGuardConfig(short_text_chars=20)
        g.record("a", "x " * 100)
        self.assertFalse(g.is_idle_dup("a", "x " * 100))

    def test_unrelated_short_passes(self):
        g = LoopGuardConfig()
        g.record("a", "standing by")
        self.assertFalse(g.is_idle_dup("a", "got the answer"))


class ThrottleTests(unittest.TestCase):
    def test_allows_under_limit(self):
        t = ThrottleConfig(per_participant_per_min=3, per_channel_per_min=10)
        self.assertTrue(t.try_consume("a", "main", now=100.0))
        self.assertTrue(t.try_consume("a", "main", now=100.5))
        self.assertTrue(t.try_consume("a", "main", now=101.0))

    def test_per_participant_limit(self):
        t = ThrottleConfig(per_participant_per_min=2, per_channel_per_min=10)
        self.assertTrue(t.try_consume("a", "main", now=100.0))
        self.assertTrue(t.try_consume("a", "main", now=100.5))
        self.assertFalse(t.try_consume("a", "main", now=101.0))

    def test_window_slides(self):
        t = ThrottleConfig(per_participant_per_min=1, per_channel_per_min=10)
        self.assertTrue(t.try_consume("a", "main", now=100.0))
        self.assertFalse(t.try_consume("a", "main", now=120.0))
        # 61 seconds later: window has slid past the first event.
        self.assertTrue(t.try_consume("a", "main", now=161.0))


class BudgetTests(unittest.TestCase):
    def test_under_budget_acquires(self):
        b = BudgetConfig(max_tokens_per_user_turn=1000)
        self.assertTrue(b.can_acquire(1, estimated_cost=500))
        b.record(1, 500)
        self.assertTrue(b.can_acquire(1, estimated_cost=500))

    def test_over_budget_blocks(self):
        b = BudgetConfig(max_tokens_per_user_turn=1000)
        b.record(1, 1000)
        self.assertFalse(b.can_acquire(1, estimated_cost=1))

    def test_no_user_turn_id_allows(self):
        b = BudgetConfig(max_tokens_per_user_turn=10)
        self.assertTrue(b.can_acquire(None, estimated_cost=999_999))


# ---------------------------------------------------------------------------
# UserTurn lifecycle
# ---------------------------------------------------------------------------

class UserTurnLifecycle(unittest.TestCase):
    def test_open_emits_user_turn_opened(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        ut = _open_default(c, e, "loom")
        opened = [x for x in bus.snapshot()
                  if ev.control_type_of(x) == "user_turn_opened"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].body["required_participants"], ["loom"])
        self.assertEqual(ut.required_participants, {"loom"})

    def test_open_emits_obligation_recorded_for_each_required(self):
        bus, state, c = _setup()
        e = _user_post(bus, "hi @loom @claude_code", addressees=["loom",
                                                                  "claude_code"])
        ut = _open_with(c, e, required=("loom", "claude_code"))
        recorded = [x for x in bus.snapshot()
                    if ev.control_type_of(x) == "obligation_recorded"]
        self.assertEqual(len(recorded), 2)
        ids = {x.body["participant_id"] for x in recorded}
        self.assertEqual(ids, {"loom", "claude_code"})
        for r in recorded:
            self.assertEqual(r.body["level"], "must")

    def test_debounce_appends_to_existing(self):
        bus, state, c = _setup(default_responder="loom")
        e1 = _user_post(bus, "hi")
        ut1 = _open_default(c, e1, "loom")
        e2 = ev.chat(sender="user", body="actually wait")
        bus.post(e2)
        e2.ts = e1.ts + 0.05  # 50ms later — within 200ms debounce
        ut2 = _open_default(c, e2, "loom")
        self.assertIs(ut1, ut2)

    def test_debounce_records_second_event_id(self):
        # Bug repro: pre-fix, the second user post inside the debounce
        # window was returned as the same turn but its event id was
        # not tracked, so actors with open obligations did not wake.
        bus, state, c = _setup(default_responder="loom")
        e1 = _user_post(bus, "hi")
        ut1 = _open_default(c, e1, "loom")
        e2 = ev.chat(sender="user", body="actually wait")
        bus.post(e2)
        e2.ts = e1.ts + 0.05
        _open_default(c, e2, "loom")
        self.assertIn(e2.id, ut1.debounced_event_ids)
        # The original opener id is NOT added to the debounced set.
        self.assertNotIn(e1.id, ut1.debounced_event_ids)

    def test_new_user_post_after_debounce_closes_prev(self):
        # P2.5: debounce now compares against time.monotonic() rather
        # than wall-clock; seed _last_user_post_ts in monotonic units.
        bus, state, c = _setup(default_responder="loom")
        e1 = _user_post(bus, "hi")
        ut1 = _open_default(c, e1, "loom")
        c._last_user_post_ts = time.monotonic() - 1.0
        e2 = _user_post(bus, "next thing")
        ut2 = _open_default(c, e2, "loom")
        self.assertIsNot(ut1, ut2)
        self.assertEqual(ut1.state, "closed")
        self.assertEqual(ut1.closure_reason, "new_user_post")

    def test_no_responder_plan_closes_immediately(self):
        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        # Empty state — no participants.
        c = RoomCoordinator(bus, state)
        e = _user_post(bus, "anyone there?")
        plan = plan_for_default(None, reason="empty room",
                                target_event_ids=[e.id])
        ut = c.open_user_turn(e, plan)
        self.assertEqual(ut.state, "closed")
        self.assertEqual(ut.closure_reason, "no_responder")

    def test_completion_closes_when_required_drafts(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        c.on_stream_end(lease, "committed",
                        committed_text="hello back", cost_tokens=10)
        c.release_lease(lease)
        self.assertEqual(c.user_turn.state, "closed")
        self.assertEqual(c.user_turn.closure_reason, "completed")

    def test_idle_timeout_with_resolved_obligations(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        # Required drafts → obligation resolved → would close completed
        # except we keep it artificially open and mock idle.
        # Instead: simulate a "should"-only / no-required scenario by
        # cancelling the resolution via direct manipulation.
        ut = c.user_turn
        ut.last_activity_at = time.monotonic() - 60.0
        # Mark every obligation resolved so unresolved_required is empty.
        for ob in ut.obligations.values():
            ob.resolved = True
        c.check_idle_timeout()
        self.assertEqual(c.user_turn.state, "closed")
        self.assertEqual(c.user_turn.closure_reason, "idle_timeout")

    def test_idle_timeout_with_unresolved_required(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        ut = c.user_turn
        ut.last_activity_at = time.monotonic() - 60.0
        c.check_idle_timeout()
        self.assertEqual(c.user_turn.state, "closed")
        self.assertEqual(c.user_turn.closure_reason, "obligation_unresolved")

    def test_topic_change_closes_open_turn(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        c.set_topic("new topic")
        self.assertEqual(c.user_turn.state, "closed")
        self.assertEqual(c.user_turn.closure_reason, "topic_changed")

    def test_close_user_turn_cancelled_resolves_all_obligations(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        c.close_user_turn("cancelled")
        # All obligations marked resolved.
        ut = c.user_turn
        for ob in ut.obligations.values():
            self.assertTrue(ob.resolved)
            self.assertIsNone(ob.resolved_by_event_id)


class PostUserEventAtomic(unittest.TestCase):
    """Race regression: bus.post + classify + open_user_turn must be atomic.

    Without the coordinator-level guard, an actor thread that wakes on the
    bus post can read ``coordinator.user_turn == None`` BEFORE the caller
    opens a turn, decide SKIP, and advance its cursor past the user event
    so the eventual turn never gets a trigger. This produces a ghosted
    chat where the user types but no agent ever drafts.
    """

    def test_atomic_helper_opens_turn_for_event(self):
        bus, state, c = _setup(default_responder="loom")
        e = ev.chat(sender="user", body="hi", room_epoch=state.room_epoch)
        plan = plan_for_default("loom", reason="t",
                                target_event_ids=[],
                                rationale="t")
        returned = c.post_user_event_and_open_turn(e, lambda _e: plan)
        self.assertEqual(returned.routing_case, plan.routing_case)
        self.assertGreaterEqual(e.id, 0)
        ut = c.user_turn
        self.assertIsNotNone(ut)
        self.assertEqual(ut.user_event_id, e.id)
        self.assertEqual(ut.required_participants, {"loom"})

    def test_atomic_helper_skips_open_for_acknowledgement(self):
        bus, state, c = _setup(default_responder="loom")
        e = ev.chat(sender="user", body="thanks",
                    room_epoch=state.room_epoch)
        plan = plan_for_acknowledgement(target_event_ids=[],
                                        rationale="ack")
        c.post_user_event_and_open_turn(e, lambda _e: plan)
        # Bus has the user event but no UserTurn opened.
        self.assertIsNone(c.user_turn)
        self.assertGreaterEqual(e.id, 0)

    def test_actor_sees_open_turn_when_waking_on_user_post(self):
        """Threaded test: an actor blocked on bus.wait_after must observe
        the open user_turn (not None) after the user event arrives."""
        import threading
        from loom.kernel.actor import ParticipantActor

        bus, state, c = _setup(default_responder="loom")
        observed: list = []
        ready = threading.Event()

        def handler(actor, trigger, lease):
            observed.append((actor.id, trigger.body,
                             c.user_turn is not None))

        actor = ParticipantActor("loom", bus, c, handler)
        actor.start()
        ready.set()
        try:
            e = ev.chat(sender="user", body="hi",
                        room_epoch=state.room_epoch)
            plan = plan_for_default("loom", reason="t",
                                    target_event_ids=[], rationale="t")
            c.post_user_event_and_open_turn(e, lambda _e: plan)
            # Wait for the actor to draft (or fail).
            for _ in range(100):
                if observed:
                    break
                time.sleep(0.01)
            self.assertEqual(observed, [("loom", "hi", True)])
        finally:
            actor.stop(timeout=0.5)
            bus.stop()


# ---------------------------------------------------------------------------
# Obligation handling
# ---------------------------------------------------------------------------

class ObligationHandling(unittest.TestCase):
    def test_obligation_for_returns_open(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        ob = c.obligation_for("loom")
        self.assertIsNotNone(ob)
        self.assertEqual(ob.participant_id, "loom")
        self.assertEqual(ob.level, "must")

    def test_obligation_for_returns_none_for_unrelated(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        self.assertIsNone(c.obligation_for("claude_code"))

    def test_committed_resolves_obligation(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        c.on_stream_end(lease, "committed",
                        committed_text="hello", cost_tokens=5)
        # An obligation_resolved event should have been posted.
        resolved = [x for x in bus.snapshot()
                    if ev.control_type_of(x) == "obligation_resolved"]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].body["participant_id"], "loom")

    def test_filter_suppressed_does_not_resolve_obligation(self):
        # ``suppressed`` is for post-stream filter rejections (idle phrase,
        # loop-guard duplicate, empty body). Obligation must stay open so
        # the idle timeout closes the turn as obligation_unresolved.
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        c.on_stream_end(lease, "suppressed", cost_tokens=2)
        resolved = [x for x in bus.snapshot()
                    if ev.control_type_of(x) == "obligation_resolved"]
        self.assertEqual(len(resolved), 0)
        # Obligation still open.
        self.assertIsNotNone(c.obligation_for("loom"))

    def test_passed_resolves_obligation(self):
        # ``passed`` is the agent emitting [PASS] — a valid completion.
        # Obligation resolves administratively (no chat to point at) and
        # the turn closes cleanly instead of idle-timing-out.
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        c.on_stream_end(lease, "passed", cost_tokens=0)
        resolved = [x for x in bus.snapshot()
                    if ev.control_type_of(x) == "obligation_resolved"]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].body["participant_id"], "loom")
        # The resolution is administrative — no chat event to reference.
        self.assertIsNone(resolved[0].body["resolved_by_event_id"])
        # And the turn closes cleanly (not obligation_unresolved).
        closed = [x for x in bus.snapshot()
                  if ev.control_type_of(x) == "user_turn_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].body["reason"], "completed")


# ---------------------------------------------------------------------------
# Lease arbitration
# ---------------------------------------------------------------------------

class LeaseAcquisition(unittest.TestCase):
    def test_required_participant_acquires_lease(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.holder, "loom")
        self.assertEqual(lease.room_epoch, state.room_epoch)
        self.assertTrue(c.validate_lease(lease))

    def test_non_eligible_participant_blocked(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        # claude_code has no obligation → blocked.
        lease = c.acquire_lease("claude_code", e.id)
        self.assertIsNone(lease)

    def test_direct_mention_bypasses_eligibility(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi @claude_code", addressees=["claude_code"])
        # Open with claude_code as required (direct-mention plan).
        _open_with(c, e, required=("claude_code",))
        # Even though we acquired with direct_mention=True, claude_code
        # is the required participant here so it would acquire either way.
        lease = c.acquire_lease("claude_code", e.id, is_direct_mention=True)
        self.assertIsNotNone(lease)

    def test_multi_required_get_parallel_leases(self):
        bus, state, c = _setup()
        e = _user_post(bus, "@a @b", addressees=["loom", "claude_code"])
        _open_with(c, e, required=("loom", "claude_code", "gemini_cli"))
        l1 = c.acquire_lease("loom", e.id)
        l2 = c.acquire_lease("claude_code", e.id)
        l3 = c.acquire_lease("gemini_cli", e.id)
        self.assertIsNotNone(l1)
        self.assertIsNotNone(l2)
        self.assertIsNotNone(l3)
        self.assertEqual(c.in_flight_lease_count(), 3)

    def test_speaker_cap_blocks_second_unprompted(self):
        bus, state, c = _setup()
        e = _user_post(bus, "hi")
        _open_with(c, e, required=("loom", "claude_code"))
        l1 = c.acquire_lease("loom", e.id)
        c.on_stream_end(l1, "committed", committed_text="hi back",
                        cost_tokens=10)
        c.release_lease(l1)
        # cap=1 (default); second unprompted draft from loom blocked.
        l2 = c.acquire_lease("loom", e.id)
        self.assertIsNone(l2)

    def test_direct_mention_does_not_consume_cap(self):
        bus, state, c = _setup(default_responder="loom")
        # Required=loom from default plan. claude_code is direct-mentioned
        # in the trigger but has no obligation.
        e = _user_post(bus, "@claude_code see this",
                       addressees=["claude_code"])
        _open_default(c, e, "loom")
        # claude_code acquires via direct mention.
        lease = c.acquire_lease("claude_code", e.id, is_direct_mention=True)
        self.assertIsNotNone(lease)
        c.on_stream_end(lease, "committed", committed_text="ack",
                        cost_tokens=5)
        c.release_lease(lease)
        # claude_code's count should still be 0 (direct-mention drafts
        # don't consume cap).
        ut = c.user_turn
        # If completion auto-closed the turn (loom never drafted), the
        # turn might already be closed. Either way, the speaker_counts
        # entry should be 0 for claude_code.
        # The turn IS still open: loom hasn't drafted, so unresolved_required
        # is non-empty.
        self.assertEqual(ut.speaker_counts.get("claude_code", 0), 0)

    def test_inactive_participant_cannot_acquire(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        state.set_active("loom", False)
        lease = c.acquire_lease("loom", e.id)
        self.assertIsNone(lease)

    def test_validate_lease_fails_after_default_responder_change(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        self.assertTrue(c.validate_lease(lease))
        c.set_default_responder("claude_code")
        self.assertFalse(c.validate_lease(lease))

    def test_validate_lease_fails_after_expiry(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        # P3.3: lease.expires_at is now monotonic-clock; use the same
        # clock to push expiry into the past.
        lease.expires_at = time.monotonic() - 1.0
        self.assertFalse(c.validate_lease(lease))

    def test_release_lease_invalidates(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        c.release_lease(lease)
        self.assertFalse(c.validate_lease(lease))


# ---------------------------------------------------------------------------
# Additional rejection-path coverage for acquire_lease.
# ---------------------------------------------------------------------------

class AcquireLeaseRejection(unittest.TestCase):
    """Edge cases for the rejection branches in ``acquire_lease``.

    The happy paths are covered above; this class exhaustively exercises
    the early-return branches and post-commit follow-up acquires.
    """

    def test_acquire_with_no_open_user_turn_returns_none(self):
        # No open user turn — every acquire must short-circuit to None
        # at the first guard.
        bus, state, c = _setup(default_responder="loom")
        # Note: no _user_post / _open_default. UserTurn is None.
        lease = c.acquire_lease("loom", trigger_event_id=0)
        self.assertIsNone(lease)
        self.assertIsNone(c.user_turn)

    def test_acquire_unknown_holder_returns_none(self):
        # Plan source guarantees ``holder not in state.participants``
        # short-circuits to None; this is NOT an exception.
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("ghost_agent", e.id)
        self.assertIsNone(lease)

    def test_acquire_after_release_returns_new_lease_id(self):
        # Released lease is removed from the table; a fresh acquire
        # returns a brand-new lease with a different id (not a recycle
        # of the released one).
        bus, state, c = _setup(default_responder="loom",
                               members=("loom", "claude_code"))
        e = _user_post(bus, "@loom @claude_code please reply",
                       addressees=["loom", "claude_code"])
        _open_with(c, e, required=("loom", "claude_code"))
        l1 = c.acquire_lease("loom", e.id)
        self.assertIsNotNone(l1)
        c.release_lease(l1)
        # claude_code (a different holder, not yet capped) acquires.
        l2 = c.acquire_lease("claude_code", e.id)
        self.assertIsNotNone(l2)
        self.assertNotEqual(l1.id, l2.id)

    def test_room_epoch_bump_invalidates_outstanding_lease(self):
        # Adding a participant bumps room_epoch; the lease was issued
        # at the prior epoch so ``validate_lease`` flips it to invalid.
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        self.assertTrue(c.validate_lease(lease))
        epoch_before = state.room_epoch
        c.register_participant(ParticipantInfo(id="newcomer"))
        self.assertGreater(state.room_epoch, epoch_before)
        # The original lease no longer matches the current room_epoch.
        self.assertFalse(c.validate_lease(lease))

    def test_acquire_lease_after_user_turn_close_returns_none(self):
        # Once a user turn closes (via on_stream_end + commit completing
        # the only obligation), subsequent acquires for that turn are
        # rejected — there is no longer an "open" user turn.
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        lease = c.acquire_lease("loom", e.id)
        self.assertIsNotNone(lease)
        c.on_stream_end(lease, "committed",
                        committed_text="long enough committed reply",
                        cost_tokens=10)
        c.release_lease(lease)
        ut = c.user_turn
        self.assertEqual(ut.state, "closed")
        # New acquire on the closed turn — rejected at the open-turn
        # guard.
        l2 = c.acquire_lease("loom", e.id)
        self.assertIsNone(l2)


# ---------------------------------------------------------------------------
# Membership and slot re-resolution
# ---------------------------------------------------------------------------

class MembershipAndSlots(unittest.TestCase):
    def test_register_participant_emits_event(self):
        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        c = RoomCoordinator(bus, state)
        c.register_participant(ParticipantInfo(id="loom"))
        events = [e for e in bus.snapshot()
                  if ev.control_type_of(e) == "participant_added"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].body["id"], "loom")

    def test_remove_emits_default_responder_changed_when_slot_held(self):
        bus, state, c = _setup(default_responder="claude_code")
        c.unregister_participant("claude_code")
        changes = [e for e in bus.snapshot()
                   if ev.control_type_of(e) == "default_responder_changed"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].body["old_id"], "claude_code")
        # New id is whoever was the cheapest active capable.
        self.assertIn(changes[0].body["new_id"], {"loom", "gemini_cli"})

    def test_remove_dead_letters_pending_mention(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hey claude_code", addressees=["claude_code"])
        _open_with(c, e, required=("claude_code",))
        c.unregister_participant("claude_code")
        dl = [x for x in bus.snapshot()
              if ev.control_type_of(x) == "dead_letter"]
        self.assertEqual(len(dl), 1)
        self.assertEqual(dl[0].body["original_mention_event_id"], e.id)
        self.assertEqual(dl[0].body["reroute_to"], "loom")

    def test_remove_invalidates_in_flight_leases(self):
        bus, state, c = _setup(default_responder="claude_code")
        e = _user_post(bus, "hi")
        _open_default(c, e, "claude_code")
        lease = c.acquire_lease("claude_code", e.id)
        self.assertTrue(c.validate_lease(lease))
        c.unregister_participant("claude_code")
        self.assertFalse(c.validate_lease(lease))

    def test_remove_resolves_their_obligations(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "@claude_code", addressees=["claude_code"])
        _open_with(c, e, required=("loom", "claude_code"))
        c.unregister_participant("claude_code")
        # claude_code's obligation is resolved-administratively; loom's
        # is still open.
        ut = c.user_turn
        # Turn should still be open because loom hasn't drafted yet.
        # (claude_code's obligation is gone.)
        self.assertEqual(ut.unresolved_required(), {"loom"})


class DeadLetterTransfer(unittest.TestCase):
    """Removed-required-participant obligations transfer to a fallback.

    Pre-fix the dead-letter event was a trace only — the rerouted
    agent had no obligation, so they couldn't drive a draft and the
    turn closed prematurely. The fix transfers the obligation to a
    live fallback before resolving the original.
    """

    def test_required_obligation_transfers_to_fallback(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "@claude_code", addressees=["claude_code"])
        # Only claude_code is required; loom is the default responder
        # but holds no obligation in this turn.
        _open_with(c, e, required=("claude_code",),
                   routing_case="direct_mention")
        c.unregister_participant("claude_code")
        ut = c.user_turn
        # Turn must still be open — the rerouted obligation gates closure.
        self.assertEqual(ut.state, "open")
        # Loom now holds a transferred obligation.
        ob = ut.obligation_for("loom")
        self.assertIsNotNone(ob)
        self.assertIn(ob.level, ("must", "should"))
        self.assertEqual(ob.reason, "rerouted_from_claude_code")
        self.assertEqual(ob.target_event_ids, [e.id])
        # An ``obligation_recorded`` event was emitted for the new
        # obligation (in addition to the open-time emissions).
        recorded = [
            x for x in bus.snapshot()
            if ev.control_type_of(x) == "obligation_recorded"
            and x.body["participant_id"] == "loom"
            and x.body["reason"] == "rerouted_from_claude_code"
        ]
        self.assertEqual(len(recorded), 1)

    def test_no_transfer_when_fallback_already_obligated(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "talk", addressees=[])
        # Both required.
        _open_with(c, e, required=("loom", "claude_code"))
        c.unregister_participant("claude_code")
        ut = c.user_turn
        # Loom still has its single original obligation — no duplicate.
        loom_obs = [
            ob for ob in ut.obligations.values()
            if ob.participant_id == "loom"
        ]
        self.assertEqual(len(loom_obs), 1)
        self.assertNotEqual(loom_obs[0].reason,
                            "rerouted_from_claude_code")

    def test_no_transfer_when_no_fallback_available(self):
        # Only one participant — no fallback after removal.
        bus = MessageBus()
        state = RoomState(config=RoomConfig(
            user_turn_idle_timeout_s=20,
            user_turn_debounce_ms=200,
        ))
        state.add_participant(ParticipantInfo(id="solo"))
        state.set_default_responder("solo")
        c = RoomCoordinator(bus, state)
        e = ev.chat(sender="user", body="@solo", addressees=["solo"])
        bus.post(e)
        _open_with(c, e, required=("solo",),
                   routing_case="direct_mention")
        c.unregister_participant("solo")
        # No fallback existed — turn closes via the standard path
        # (no required obligations remaining).
        ut = c.user_turn
        self.assertEqual(ut.state, "closed")

    def test_no_transfer_when_fallback_already_drafted(self):
        # If the candidate fallback has already drafted in this turn,
        # do not obligate them again — they've already replied.
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "talk", addressees=[])
        _open_with(c, e, required=("loom", "claude_code"))
        l1 = c.acquire_lease("loom", e.id)
        c.on_stream_end(l1, "committed",
                        committed_text="loom replied", cost_tokens=1)
        # Loom committed; only claude_code's obligation outstanding.
        c.unregister_participant("claude_code")
        ut = c.user_turn
        # Turn closes — no transfer because loom already drafted.
        self.assertEqual(ut.state, "closed")


# ---------------------------------------------------------------------------
# Skip handling
# ---------------------------------------------------------------------------

class SkipHandling(unittest.TestCase):
    def test_skip_does_not_close_turn(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        c.handle_skip("loom", e)
        # SKIP alone doesn't resolve the obligation; turn stays open.
        self.assertEqual(c.user_turn.state, "open")
        self.assertIsNotNone(c.obligation_for("loom"))

    def test_skip_bumps_activity(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_default(c, e, "loom")
        ut = c.user_turn
        ut.last_activity_at = 0.0
        c.handle_skip("loom", e)
        self.assertGreater(ut.last_activity_at, 0.0)


# ---------------------------------------------------------------------------
# Floor control — RoomControlState setters + plan-level allowed_speakers
# enforcement + max_responses early-close + wait_for_user_after.
# ---------------------------------------------------------------------------

class RoomControlSetters(unittest.TestCase):
    def test_set_roles_emits_event_and_updates_state(self):
        bus, state, c = _setup()
        c.set_roles({"loom": "teacher", "claude_code": "quizzer"})
        self.assertEqual(state.control.roles,
                         {"loom": "teacher", "claude_code": "quizzer"})
        events = [e for e in bus.snapshot()
                  if ev.control_type_of(e) == "roles_assigned"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].body["roles"],
                         {"loom": "teacher", "claude_code": "quizzer"})

    def test_set_roles_no_change_no_event(self):
        bus, state, c = _setup()
        c.set_roles({"loom": "teacher"})
        before = len([e for e in bus.snapshot()
                      if ev.control_type_of(e) == "roles_assigned"])
        c.set_roles({"loom": "teacher"})  # no change
        after = len([e for e in bus.snapshot()
                     if ev.control_type_of(e) == "roles_assigned"])
        self.assertEqual(before, after)

    def test_set_floor_owner_emits_floor_updated(self):
        bus, state, c = _setup()
        c.set_floor_owner(["loom"])
        events = [e for e in bus.snapshot()
                  if ev.control_type_of(e) == "floor_updated"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].body["floor_owner"], ["loom"])

    def test_clear_floor_emits_empty_list(self):
        bus, state, c = _setup()
        c.set_floor_owner(["loom"])
        c.set_floor_owner(None)
        events = [e for e in bus.snapshot()
                  if ev.control_type_of(e) == "floor_updated"]
        # Two events: set then clear. Clear carries floor_owner=[].
        self.assertEqual(events[-1].body["floor_owner"], [])

    def test_set_style_emits_event(self):
        bus, state, c = _setup()
        c.set_style("brief")
        events = [e for e in bus.snapshot()
                  if ev.control_type_of(e) == "style_changed"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].body["new"], "brief")
        self.assertEqual(state.control.style, "brief")

    def test_floor_set_emits_floor_updated(self):
        # P2.3: ``active_goal`` parameter removed from set_floor_owner.
        # Topic now flows through ``set_topic``; this test now verifies
        # only the floor side of the call.
        bus, state, c = _setup()
        c.set_floor_owner(["loom"])
        self.assertEqual(state.control.floor_owner, ["loom"])


class AllowedSpeakersGate(unittest.TestCase):
    """Coordinator denies leases for ids outside ``plan.allowed_speakers``."""

    def test_outsider_denied_lease_in_directed_turn(self):
        bus, state, c = _setup()
        e = _user_post(bus, "@loom teach me", addressees=["loom"])
        # plan_with_required defaults: allowed_speakers = required.
        plan = plan_with_required(
            ["loom"], routing_case="direct_mention",
            target_event_ids=[e.id], reason="direct",
        )
        c.open_user_turn(e, plan)
        # claude_code has no obligation, isn't allowed; lease denied
        # — even though direct-mention bypass is False (no user @ to him).
        self.assertIsNone(c.acquire_lease("claude_code", e.id))

    def test_user_direct_mention_bypasses_allowed_speakers(self):
        bus, state, c = _setup()
        # Floor narrowed to loom — but user @-mentions claude_code in
        # the trigger; direct-mention bypass should let claude in.
        e = _user_post(bus, "@claude_code chime in",
                       addressees=["claude_code"])
        plan = plan_with_required(
            ["loom"], routing_case="direct_mention",
            target_event_ids=[e.id], reason="floor",
            allowed_speakers={"loom"},
        )
        c.open_user_turn(e, plan)
        lease = c.acquire_lease("claude_code", e.id,
                                is_direct_mention=True)
        self.assertIsNotNone(lease)

    def test_allowed_speaker_acquires_lease(self):
        bus, state, c = _setup()
        e = _user_post(bus, "hi room")
        # Broadcast: all three are allowed.
        plan = plan_with_required(
            ["loom", "claude_code", "gemini_cli"],
            routing_case="multi_opinion",
            target_event_ids=[e.id], reason="broadcast",
        )
        c.open_user_turn(e, plan)
        lease = c.acquire_lease("claude_code", e.id)
        self.assertIsNotNone(lease)


class MaxResponsesEarlyClose(unittest.TestCase):
    """Turn closes once the committed-reply count reaches ``max_responses``."""

    def test_directed_turn_closes_after_one_commit(self):
        bus, state, c = _setup()
        e = _user_post(bus, "@loom teach", addressees=["loom"])
        plan = plan_with_required(
            ["loom"], routing_case="direct_mention",
            target_event_ids=[e.id], reason="direct",
            max_responses=1,
        )
        c.open_user_turn(e, plan)
        lease = c.acquire_lease("loom", e.id)
        c.on_stream_end(lease, "committed",
                        committed_text="ok", cost_tokens=10)
        self.assertEqual(c.user_turn.state, "closed")
        self.assertEqual(c.user_turn.closure_reason, "completed")

    def test_broadcast_turn_closes_when_all_speakers_replied(self):
        bus, state, c = _setup()
        e = _user_post(bus, "hello room")
        plan = plan_with_required(
            ["loom", "claude_code"],
            routing_case="multi_opinion",
            target_event_ids=[e.id], reason="broadcast",
            max_responses=2,
        )
        c.open_user_turn(e, plan)
        l1 = c.acquire_lease("loom", e.id)
        c.on_stream_end(l1, "committed",
                        committed_text="hi from loom", cost_tokens=5)
        # claude_code still has obligation; loom's drafted, but the
        # cap hasn't been reached yet.
        self.assertEqual(c.user_turn.state, "open")
        l2 = c.acquire_lease("claude_code", e.id)
        c.on_stream_end(l2, "committed",
                        committed_text="hi from claude", cost_tokens=5)
        # Both committed → cap reached → close.
        self.assertEqual(c.user_turn.state, "closed")


class MaxResponsesConcurrency(unittest.TestCase):
    """``max_responses`` enforced at lease-grant time, not retroactively.

    Pre-fix the cap was only checked by ``_maybe_close_user_turn_locked``
    *after* a commit landed. Two actors waking on the same trigger could
    both pass the lease checks before either committed — both ended up
    with chats. The fix gates ``acquire_lease`` on
    ``len(ut.drafted) + outstanding_valid_leases``.
    """

    def test_max_responses_one_blocks_second_lease(self):
        bus, state, c = _setup()
        e = _user_post(bus, "hello room")
        plan = plan_with_required(
            ["loom", "claude_code"],
            routing_case="multi_opinion",
            target_event_ids=[e.id], reason="broadcast",
            max_responses=1,
        )
        c.open_user_turn(e, plan)
        l1 = c.acquire_lease("loom", e.id)
        self.assertIsNotNone(l1)
        # Second lease must be rejected — l1 is outstanding, cap is 1.
        l2 = c.acquire_lease("claude_code", e.id)
        self.assertIsNone(l2)

    def test_direct_mention_bypasses_cap(self):
        bus, state, c = _setup()
        e = _user_post(bus, "hello room")
        plan = plan_with_required(
            ["loom", "claude_code"],
            routing_case="multi_opinion",
            target_event_ids=[e.id], reason="broadcast",
            max_responses=1,
        )
        c.open_user_turn(e, plan)
        l1 = c.acquire_lease("loom", e.id)
        self.assertIsNotNone(l1)
        # Direct user mention bypasses the cap (the user explicitly
        # addressed the speaker).
        l2 = c.acquire_lease("claude_code", e.id, is_direct_mention=True)
        self.assertIsNotNone(l2)

    def test_committed_plus_outstanding_counted(self):
        bus, state, c = _setup()
        e = _user_post(bus, "hello room")
        plan = plan_with_required(
            ["loom", "claude_code"],
            routing_case="multi_opinion",
            target_event_ids=[e.id], reason="broadcast",
            max_responses=2,
        )
        c.open_user_turn(e, plan)
        l1 = c.acquire_lease("loom", e.id)
        c.on_stream_end(l1, "committed",
                        committed_text="hi from loom", cost_tokens=1)
        # 1 committed + 0 outstanding < 2 — claude_code grants.
        l2 = c.acquire_lease("claude_code", e.id)
        self.assertIsNotNone(l2)

    def test_committed_one_outstanding_one_rejects_third(self):
        # max_responses=2; loom committed (drafted), claude has live
        # lease. A third actor (gemini_cli) trying to acquire would
        # exceed the cap.
        bus, state, c = _setup()
        e = _user_post(bus, "hello room")
        plan = plan_with_required(
            ["loom", "claude_code", "gemini_cli"],
            routing_case="multi_opinion",
            target_event_ids=[e.id], reason="broadcast",
            max_responses=2,
        )
        c.open_user_turn(e, plan)
        l1 = c.acquire_lease("loom", e.id)
        c.on_stream_end(l1, "committed",
                        committed_text="hi from loom", cost_tokens=1)
        l2 = c.acquire_lease("claude_code", e.id)
        self.assertIsNotNone(l2)
        # Third agent must be rejected — committed=1 + outstanding=1 = 2
        # which equals the cap.
        l3 = c.acquire_lease("gemini_cli", e.id)
        self.assertIsNone(l3)

    def test_open_chat_unaffected_when_cap_equals_count(self):
        # OpenChatPolicy sets max_responses == count(active+capable).
        # The gate ``committed + outstanding >= cap`` only fires when
        # all slots are spoken-for, which is the desired semantic for
        # broadcast plans.
        bus, state, c = _setup()
        e = _user_post(bus, "hello room")
        plan = plan_with_required(
            ["loom", "claude_code", "gemini_cli"],
            routing_case="multi_opinion",
            target_event_ids=[e.id], reason="open_chat",
            max_responses=3,
        )
        c.open_user_turn(e, plan)
        l1 = c.acquire_lease("loom", e.id)
        l2 = c.acquire_lease("claude_code", e.id)
        l3 = c.acquire_lease("gemini_cli", e.id)
        self.assertIsNotNone(l1)
        self.assertIsNotNone(l2)
        self.assertIsNotNone(l3)

    def test_two_threads_racing_max_one_only_one_wins(self):
        import threading
        bus, state, c = _setup()
        e = _user_post(bus, "hello room")
        plan = plan_with_required(
            ["loom", "claude_code"],
            routing_case="multi_opinion",
            target_event_ids=[e.id], reason="broadcast",
            max_responses=1,
        )
        c.open_user_turn(e, plan)

        results: list = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def attempt(holder: str) -> None:
            barrier.wait(timeout=1.0)
            l = c.acquire_lease(holder, e.id)
            with results_lock:
                results.append((holder, l))

        t1 = threading.Thread(target=attempt, args=("loom",))
        t2 = threading.Thread(target=attempt, args=("claude_code",))
        t1.start()
        t2.start()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        granted = [r for r in results if r[1] is not None]
        rejected = [r for r in results if r[1] is None]
        self.assertEqual(len(granted), 1,
                         f"exactly one must win; got {results}")
        self.assertEqual(len(rejected), 1,
                         f"exactly one must lose; got {results}")


class WaitForUserAfter(unittest.TestCase):
    def test_directed_plan_sets_wait_for_user_on_close(self):
        bus, state, c = _setup()
        e = _user_post(bus, "@loom teach", addressees=["loom"])
        plan = plan_with_required(
            ["loom"], routing_case="direct_mention",
            target_event_ids=[e.id], reason="direct",
            wait_for_user_after=True,
        )
        c.open_user_turn(e, plan)
        lease = c.acquire_lease("loom", e.id)
        c.on_stream_end(lease, "committed",
                        committed_text="hi", cost_tokens=1)
        self.assertTrue(state.control.wait_for_user)

    def test_broadcast_plan_does_not_set_wait_for_user(self):
        bus, state, c = _setup()
        e = _user_post(bus, "hi room")
        plan = plan_with_required(
            ["loom"], routing_case="multi_opinion",
            target_event_ids=[e.id], reason="broadcast",
            wait_for_user_after=False,
        )
        c.open_user_turn(e, plan)
        lease = c.acquire_lease("loom", e.id)
        c.on_stream_end(lease, "committed",
                        committed_text="hi", cost_tokens=1)
        self.assertFalse(state.control.wait_for_user)

    def test_new_user_post_clears_wait_for_user(self):
        bus, state, c = _setup()
        state.set_wait_for_user(True)
        e = _user_post(bus, "go on")
        # An open_user_turn for a new user post should clear the wait
        # gate so the new turn is free to dispatch.
        plan = plan_with_required(
            ["loom"], routing_case="multi_opinion",
            target_event_ids=[e.id], reason="broadcast",
        )
        c.open_user_turn(e, plan)
        self.assertFalse(state.control.wait_for_user)


class RoundRobinPlanHooks(unittest.TestCase):
    """``set_turn_taking_mode`` / ``set_turn_order`` / ``advance_turn_pointer``
    on :class:`UserTurnPlan` propagate through the coordinator."""

    def test_set_turn_taking_mode_applies_on_open(self):
        bus, state, c = _setup()
        e = _user_post(bus, "lets play")
        plan = plan_with_required(
            ["loom", "claude_code", "gemini_cli"],
            routing_case="multi_opinion",
            target_event_ids=[e.id], reason="game_start",
            set_turn_taking_mode="round_robin",
            set_turn_order=["loom", "claude_code", "gemini_cli"],
        )
        c.post_user_event_and_open_turn(_user_post(bus, "lets play"),
                                        lambda _e: plan)
        self.assertEqual(state.control.turn_taking_mode, "round_robin")
        self.assertEqual(state.control.turn_order,
                         ["loom", "claude_code", "gemini_cli"])

    def test_set_turn_taking_mode_applies_on_acknowledgement(self):
        # Game-end phrase: plan is acknowledgement (no turn) but still
        # carries a mode flip back to broadcast.
        bus, state, c = _setup()
        state.set_turn_taking_mode("round_robin")
        state.set_turn_order(["loom", "claude_code"])
        plan = plan_for_acknowledgement(target_event_ids=[],
                                        rationale="game-end")
        plan.set_turn_taking_mode = "broadcast"
        plan.set_turn_order = []
        e = _user_post(bus, "good game")
        c.post_user_event_and_open_turn(e, lambda _e: plan)
        self.assertEqual(state.control.turn_taking_mode, "broadcast")
        self.assertEqual(state.control.turn_order, [])
        # No turn opened (acknowledgement plan).
        self.assertIsNone(c.user_turn)

    def test_advance_turn_pointer_on_close(self):
        bus, state, c = _setup()
        state.set_turn_taking_mode("round_robin")
        state.set_turn_order(["loom", "claude_code", "gemini_cli"])
        e = _user_post(bus, "ask me")
        plan = plan_with_required(
            ["loom"], routing_case="direct_mention",
            target_event_ids=[e.id], reason="round_robin",
            allowed_speakers={"loom"}, max_responses=1,
            wait_for_user_after=True,
            advance_turn_pointer=True,
        )
        c.open_user_turn(e, plan)
        lease = c.acquire_lease("loom", e.id)
        c.on_stream_end(lease, "committed",
                        committed_text="my question", cost_tokens=1)
        # Pointer advanced from 0 to 1.
        self.assertEqual(state.control.next_speaker_idx, 1)

    def test_advance_skipped_when_flag_false(self):
        # @-mention during round_robin keeps the pointer.
        bus, state, c = _setup()
        state.set_turn_taking_mode("round_robin")
        state.set_turn_order(["loom", "claude_code", "gemini_cli"])
        state.control.next_speaker_idx = 1
        e = _user_post(bus, "@gemini_cli aside")
        plan = plan_with_required(
            ["gemini_cli"], routing_case="direct_mention",
            target_event_ids=[e.id], reason="direct_mention",
            allowed_speakers={"gemini_cli"}, max_responses=1,
            wait_for_user_after=True,
            advance_turn_pointer=False,
        )
        c.open_user_turn(e, plan)
        lease = c.acquire_lease("gemini_cli", e.id)
        c.on_stream_end(lease, "committed",
                        committed_text="aside reply", cost_tokens=1)
        # Pointer untouched.
        self.assertEqual(state.control.next_speaker_idx, 1)

    def test_advance_skipped_when_mode_no_longer_round_robin(self):
        # If something flipped mode mid-turn, the closing rotation
        # advance should not run.
        bus, state, c = _setup()
        state.set_turn_taking_mode("round_robin")
        state.set_turn_order(["loom", "claude_code", "gemini_cli"])
        e = _user_post(bus, "ask me")
        plan = plan_with_required(
            ["loom"], routing_case="direct_mention",
            target_event_ids=[e.id], reason="round_robin",
            allowed_speakers={"loom"}, max_responses=1,
            advance_turn_pointer=True,
        )
        c.open_user_turn(e, plan)
        # External flip back to broadcast (e.g. /cancel-style change).
        state.set_turn_taking_mode("broadcast")
        lease = c.acquire_lease("loom", e.id)
        c.on_stream_end(lease, "committed",
                        committed_text="hi", cost_tokens=1)
        # Pointer was reset on mode flip → 0; no advance applied.
        self.assertEqual(state.control.next_speaker_idx, 0)


# ---------------------------------------------------------------------------
# Policy watchdog (slow + error modes)
# ---------------------------------------------------------------------------

class PolicyWatchdog(unittest.TestCase):
    """Coordinator-side watchdog for ``classify_fn`` (the policy)."""

    def test_unknown_policy_error_mode_rejected(self):
        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        with self.assertRaises(ValueError):
            RoomCoordinator(bus, state, policy_error_mode="bogus")

    def test_default_policy_error_mode_is_close_turn(self):
        bus, state, c = _setup(default_responder="loom")
        self.assertEqual(c.policy_error_mode, "close_turn")

    def test_policy_slow_event_emitted(self):
        # The coordinator emits ``policy_slow`` when classify_fn exceeds
        # the threshold (~100ms). We monkeypatch the threshold to ~0 so a
        # negligible call still trips it without slowing the test.
        from loom.kernel import coordinator as coord
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        plan = plan_for_default("loom", reason="t",
                                target_event_ids=[e.id])
        original = coord._POLICY_SLOW_THRESHOLD_MS
        coord._POLICY_SLOW_THRESHOLD_MS = -1.0
        try:
            c.post_user_event_and_open_turn(e, lambda _e: plan)
        finally:
            coord._POLICY_SLOW_THRESHOLD_MS = original
        slow = [x for x in bus.snapshot()
                if ev.control_type_of(x) == "policy_slow"]
        self.assertEqual(len(slow), 1)
        self.assertEqual(slow[0].body["user_event_id"], e.id)
        self.assertIn("elapsed_ms", slow[0].body)

    def test_policy_error_close_turn_emits_event_and_skips_open(self):
        bus, state, c = _setup(default_responder="loom")
        # close_turn is the default; verify behavior on a raising policy.
        def classifier(_ev):
            raise RuntimeError("policy boom")

        e = _user_post(bus, "hi")
        c.post_user_event_and_open_turn(e, classifier)
        # No turn opened (fail-closed).
        self.assertIsNone(c.user_turn)
        # ``policy_error`` recorded.
        errors = [x for x in bus.snapshot()
                  if ev.control_type_of(x) == "policy_error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].body["exception_class"], "RuntimeError")
        self.assertEqual(errors[0].body["user_event_id"], e.id)

    def test_policy_error_default_responder_falls_back(self):
        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        for i, pid in enumerate(("loom", "claude_code")):
            state.add_participant(ParticipantInfo(id=pid, cost_tier=i))
        state.set_default_responder("loom")
        c = RoomCoordinator(bus, state,
                            policy_error_mode="default_responder")

        def classifier(_ev):
            raise RuntimeError("policy boom")

        e = _user_post(bus, "hi")
        c.post_user_event_and_open_turn(e, classifier)
        # Turn opened against the default responder.
        ut = c.user_turn
        self.assertIsNotNone(ut)
        self.assertEqual(ut.required_participants, {"loom"})
        # ``policy_error`` still recorded.
        errors = [x for x in bus.snapshot()
                  if ev.control_type_of(x) == "policy_error"]
        self.assertEqual(len(errors), 1)

    def test_policy_error_raise_mode_propagates(self):
        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="loom"))
        c = RoomCoordinator(bus, state, policy_error_mode="raise")

        def classifier(_ev):
            raise RuntimeError("policy boom")

        e = _user_post(bus, "hi")
        with self.assertRaises(RuntimeError):
            c.post_user_event_and_open_turn(e, classifier)
        # Even on raise, the event was logged before the throw.
        errors = [x for x in bus.snapshot()
                  if ev.control_type_of(x) == "policy_error"]
        self.assertEqual(len(errors), 1)


class ResolveObligationExpectedHolder(unittest.TestCase):
    """P3.2 / audit C2 — ``_resolve_obligation_locked`` rejects holder mismatch.

    The current public path through ``on_stream_end`` always passes the
    correct ``expected_holder`` (the lease's own holder), so today the
    check is a no-op assertion. This test locks in the regression-guard
    intent: a future caller that loses the holder check (or a tampered
    journal that fakes a stream_end with a forged holder) must not be
    able to resolve another participant's obligation.
    """

    def test_mismatched_holder_raises_value_error(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        # Open a turn requiring loom AND claude_code so two obligations
        # exist; pick claude_code's obligation id and try to resolve it
        # while claiming loom is the holder — the holder mismatch must
        # raise.
        _open_with(c, e, required=("loom", "claude_code"))
        ut = c.user_turn
        assert ut is not None
        cc_oblig = ut.obligation_for("claude_code")
        assert cc_oblig is not None
        # Acquire the lock the same way the production path does, then
        # call the private helper directly with the wrong expected holder.
        with c._lock:
            with self.assertRaises(ValueError) as cm:
                c._resolve_obligation_locked(
                    cc_oblig.id,
                    by_event_id=None,
                    expected_holder="loom",
                )
        self.assertIn("claude_code", str(cm.exception))
        self.assertIn("loom", str(cm.exception))
        # The obligation is still pending — the guard fired BEFORE the
        # mark-resolved side effect.
        ut2 = c.user_turn
        assert ut2 is not None
        self.assertFalse(ut2.obligations[cc_oblig.id].resolved)

    def test_matched_holder_resolves_normally(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_with(c, e, required=("loom",))
        ut = c.user_turn
        assert ut is not None
        loom_oblig = ut.obligation_for("loom")
        assert loom_oblig is not None
        with c._lock:
            c._resolve_obligation_locked(
                loom_oblig.id,
                by_event_id=42,
                expected_holder="loom",
            )
        # Marked resolved; ``obligation_resolved`` event posted.
        ut2 = c.user_turn
        assert ut2 is not None
        self.assertTrue(ut2.obligations[loom_oblig.id].resolved)
        resolved_events = [x for x in bus.snapshot()
                           if ev.control_type_of(x) == "obligation_resolved"]
        self.assertEqual(len(resolved_events), 1)
        self.assertEqual(resolved_events[0].body["participant_id"], "loom")

    def test_no_expected_holder_is_a_no_op_check(self):
        # Passing ``expected_holder=None`` (the default) skips the
        # guard — backwards-compatible for the rare caller that hasn't
        # been migrated.
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        _open_with(c, e, required=("loom", "claude_code"))
        ut = c.user_turn
        assert ut is not None
        cc_oblig = ut.obligation_for("claude_code")
        assert cc_oblig is not None
        with c._lock:
            # Does NOT raise even though we don't pass expected_holder;
            # the helper is permissive when the parameter is absent.
            c._resolve_obligation_locked(cc_oblig.id, by_event_id=None)
        ut2 = c.user_turn
        assert ut2 is not None
        self.assertTrue(ut2.obligations[cc_oblig.id].resolved)


if __name__ == "__main__":
    unittest.main()
