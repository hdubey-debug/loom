"""Tests for ``loom.kernel.actor`` — decision policy + ParticipantActor."""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.actor import (
    ParticipantActor,
    _trigger_priority,
    decide,
    pick_priority_trigger,
)
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


def _setup(*, default_responder=None, members=("loom", "claude_code", "gemini_cli")):
    bus = MessageBus()
    state = RoomState(
        config=RoomConfig(
            user_turn_idle_timeout_s=20,
            user_turn_debounce_ms=200,
        )
    )
    for i, pid in enumerate(members):
        state.add_participant(ParticipantInfo(id=pid, cost_tier=i))
    if default_responder:
        state.set_default_responder(default_responder)
    return bus, state, RoomCoordinator(bus, state)


def _user_post(bus, body="hi", addressees=None):
    e = ev.chat(sender="user", body=body, addressees=list(addressees or []))
    bus.post(e)
    return e


def _open_default(c, e, default_id):
    plan = plan_for_default(default_id, reason="fallback", target_event_ids=[e.id])
    return c.open_user_turn(e, plan)


def _open_required(c, e, required, *, optional=()):
    plan = plan_with_required(
        list(required),
        routing_case="direct_mention",
        target_event_ids=[e.id],
        reason="direct_mention",
        optional=list(optional),
    )
    return c.open_user_turn(e, plan)


class PickPriorityTrigger(unittest.TestCase):
    def test_direct_mention_wins_over_obligation(self):
        bus, state, c = _setup(default_responder="loom")
        e1 = _user_post(bus, "hi everyone")
        ut = _open_default(c, e1, "loom")
        e2 = _user_post(bus, "claude_code, here", addressees=["claude_code"])
        chosen = pick_priority_trigger([e1, e2], "claude_code", ut)
        # claude_code has no obligation but is direct-mentioned.
        self.assertEqual(chosen.id, e2.id)

    def test_newest_direct_mention_in_tie(self):
        bus, state, c = _setup(default_responder="loom")
        e1 = _user_post(bus, "claude_code, first", addressees=["claude_code"])
        ut = _open_default(c, e1, "loom")
        e2 = _user_post(bus, "claude_code, second", addressees=["claude_code"])
        chosen = pick_priority_trigger([e1, e2], "claude_code", ut)
        self.assertEqual(chosen.id, e2.id)

    def test_dead_letter_to_me_picked(self):
        bus, state, c = _setup()
        e = _user_post(bus, "hi")
        ut = _open_required(c, e, required=("loom",))
        bus.post(ev.dead_letter(e.id, reason="participant_removed", reroute_to="loom"))
        chosen = pick_priority_trigger(bus.snapshot(), "loom", ut)
        # dead_letter has priority 2; obligation has priority 3 — dead_letter
        # wins on priority class.
        self.assertEqual(ev.control_type_of(chosen), "dead_letter")

    def test_dead_letter_to_other_ignored(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        ut = _open_default(c, e, "loom")
        bus.post(ev.dead_letter(e.id, reason="participant_removed", reroute_to="loom"))
        chosen = pick_priority_trigger(bus.snapshot(), "claude_code", ut)
        self.assertIsNone(chosen)

    def test_no_actionable_returns_none(self):
        bus, state, c = _setup()
        bus.post(ev.system("session started"))
        chosen = pick_priority_trigger(bus.snapshot(), "loom", None)
        self.assertIsNone(chosen)

    def test_custom_priority_fn_can_invert_order(self):
        # Custom hook treats system messages as the top trigger and
        # ignores everything else — verifies the override path.
        bus, state, c = _setup()
        e_user = _user_post(bus, "claude_code, hi", addressees=["claude_code"])
        ut = _open_required(c, e_user, required=("claude_code",))
        e_sys = ev.system("special signal")
        bus.post(e_sys)

        def _system_first(event, my_id, user_turn):
            del my_id, user_turn
            if event.kind == "system":
                return 0
            return None

        chosen = pick_priority_trigger(bus.snapshot(), "claude_code", ut, priority_fn=_system_first)
        # Without override, the user direct mention would have won;
        # with override the system event wins (everything else is None).
        self.assertEqual(chosen.id, e_sys.id)

    def test_default_priority_fn_unchanged_when_none(self):
        # Passing priority_fn=None falls back to DEFAULT_TRIGGER_PRIORITY.
        bus, state, c = _setup()
        e_user = _user_post(bus, "claude_code, hi", addressees=["claude_code"])
        ut = _open_required(c, e_user, required=("claude_code",))
        chosen_none = pick_priority_trigger(bus.snapshot(), "claude_code", ut, priority_fn=None)
        chosen_default = pick_priority_trigger(bus.snapshot(), "claude_code", ut)
        self.assertEqual(chosen_none.id, chosen_default.id)
        self.assertEqual(chosen_none.id, e_user.id)

    def test_empty_batch_returns_none(self):
        self.assertIsNone(pick_priority_trigger([], "loom", None))


class ObligationDrivenTrigger(unittest.TestCase):
    """The trigger event for a required participant is the user post that
    opened the current turn — only when an obligation is actually held."""

    def test_required_participant_sees_priority_3(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        ut = _open_default(c, e, "loom")
        self.assertEqual(_trigger_priority(e, "loom", ut), 3)

    def test_unrelated_participant_sees_no_trigger(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        ut = _open_default(c, e, "loom")
        self.assertIsNone(_trigger_priority(e, "claude_code", ut))

    def test_direct_mention_priority_beats_obligation(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "@loom hi", addressees=["loom"])
        ut = _open_default(c, e, "loom")
        # loom is both required AND direct-mentioned. The direct
        # mention path is priority 1; obligation alone is priority 3.
        self.assertEqual(_trigger_priority(e, "loom", ut), 1)


class DecideFunction(unittest.TestCase):
    def test_no_user_turn_skips(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        d = decide([e], "loom", user_turn=None)
        self.assertEqual(d.action, "SKIP")
        self.assertEqual(d.reason, "no open user_turn")

    def test_empty_batch_skips(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        ut = _open_default(c, e, "loom")
        d = decide([], "loom", user_turn=ut)
        self.assertEqual(d.action, "SKIP")

    def test_required_participant_drafts(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        ut = _open_default(c, e, "loom")
        d = decide([e], "loom", ut)
        self.assertEqual(d.action, "DRAFT")
        self.assertEqual(d.reason, "obligation")
        self.assertEqual(d.trigger_event_id, e.id)

    def test_non_required_participant_skips(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        ut = _open_default(c, e, "loom")
        d = decide([e], "claude_code", ut)
        self.assertEqual(d.action, "SKIP")
        # No actionable trigger at all — claude_code's plain-user-post
        # is filtered by the obligation gate.
        self.assertIsNone(d.trigger_event_id)

    def test_direct_mention_drafts(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "claude_code please", addressees=["claude_code"])
        ut = _open_required(c, e, required=("claude_code",))
        d = decide([e], "claude_code", ut)
        self.assertEqual(d.action, "DRAFT")
        self.assertEqual(d.reason, "direct_mention")

    def test_dead_letter_drafts(self):
        bus, state, c = _setup(default_responder="loom")
        e = _user_post(bus, "hi")
        ut = _open_default(c, e, "loom")
        bus.post(ev.dead_letter(e.id, reason="participant_removed", reroute_to="loom"))
        events = bus.snapshot(since=-1)
        d = decide(events, "loom", ut)
        self.assertEqual(d.action, "DRAFT")
        # Dead letter wins over obligation (priority 2 vs 3).
        self.assertEqual(d.reason, "dead_letter_rerouted")

    def test_multi_required_each_drafts(self):
        bus, state, c = _setup()
        e = _user_post(bus, "@a @b", addressees=["loom", "claude_code"])
        ut = _open_required(c, e, required=("loom", "claude_code"))
        # Both required participants should DRAFT on the user post.
        # Note: addressees include both, so direct_mention also fires —
        # priority 1 wins.
        for pid in ("loom", "claude_code"):
            d = decide([e], pid, ut)
            self.assertEqual(d.action, "DRAFT", msg=pid)
        # gemini_cli is neither required nor mentioned → SKIP.
        d = decide([e], "gemini_cli", ut)
        self.assertEqual(d.action, "SKIP")


class ParticipantActorIntegration(unittest.TestCase):
    """Drive the actor via :meth:`step` (no thread)."""

    def setUp(self):
        self.bus, self.state, self.coordinator = _setup(default_responder="loom")
        self.draft_calls: list[tuple[str, int]] = []

        def handler(actor, trigger, lease):
            self.draft_calls.append((actor.id, trigger.id))
            self.coordinator.on_stream_end(
                lease,
                "committed",
                committed_text="ok",
                cost_tokens=2,
            )

        self.handler = handler

    def test_actor_drafts_default_responder_on_user_post(self):
        actor = ParticipantActor("loom", self.bus, self.coordinator, self.handler)
        e = _user_post(self.bus, "hi")
        _open_default(self.coordinator, e, "loom")
        d = actor.step()
        self.assertEqual(d.action, "DRAFT")
        self.assertEqual(self.draft_calls, [("loom", e.id)])
        # UserTurn closed (default responder committed).
        self.assertEqual(self.coordinator.user_turn.state, "closed")

    def test_actor_skips_when_not_eligible(self):
        actor = ParticipantActor("claude_code", self.bus, self.coordinator, self.handler)
        e = _user_post(self.bus, "hi")
        _open_default(self.coordinator, e, "loom")
        d = actor.step()
        self.assertEqual(d.action, "SKIP")
        self.assertEqual(self.draft_calls, [])

    def test_actor_filters_self_events(self):
        actor = ParticipantActor("loom", self.bus, self.coordinator, self.handler)
        e1 = _user_post(self.bus, "hi")
        _open_default(self.coordinator, e1, "loom")
        actor.step()
        before = len(self.draft_calls)
        # New chat from loom — should not re-trigger the actor.
        self.bus.post(ev.chat(sender="loom", body="hello user"))
        actor.step()
        self.assertEqual(len(self.draft_calls), before)

    def test_direct_mention_drafts_even_without_obligation(self):
        bus, state, coord = _setup(default_responder="loom")
        actor = ParticipantActor("claude_code", bus, coord, self.handler)
        e = _user_post(bus, "@claude_code reply", addressees=["claude_code"])
        # The plan only requires loom; claude_code has no obligation.
        _open_default(coord, e, "loom")
        d = actor.step()
        self.assertEqual(d.action, "DRAFT")
        self.assertEqual(d.reason, "direct_mention")


class ActorErrorSurface(unittest.TestCase):
    """Loop-level errors must surface as ``actor_error`` control events.

    Pre-fix the actor swallowed every exception silently. The thread
    stayed alive but diagnosis was blind. The fix posts an
    ``actor_error`` to the bus before continuing.
    """

    def test_decision_exception_emits_actor_error(self):
        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="loom"))
        state.set_default_responder("loom")
        coord = RoomCoordinator(bus, state)

        def boom(actor, trigger, lease):
            raise RuntimeError("draft handler exploded")

        actor = ParticipantActor("loom", bus, coord, boom)
        e = ev.chat(sender="user", body="hi")
        bus.post(e)
        plan = plan_for_default("loom", reason="fallback", target_event_ids=[e.id])
        coord.open_user_turn(e, plan)

        # Drive the loop body once via the same code path the loop uses.
        actor._step_with_error_handling()

        errors = [
            x
            for x in bus.snapshot()
            if x.kind == "control" and x.body.get("control_type") == "actor_error"
        ]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].body["participant_id"], "loom")
        self.assertEqual(errors[0].body["exception_class"], "RuntimeError")
        self.assertIn("draft handler exploded", errors[0].body["message"])

    def test_step_with_error_handling_does_not_propagate(self):
        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="loom"))
        state.set_default_responder("loom")
        coord = RoomCoordinator(bus, state)

        def boom(actor, trigger, lease):
            raise ValueError("nope")

        actor = ParticipantActor("loom", bus, coord, boom)
        e = ev.chat(sender="user", body="hi")
        bus.post(e)
        plan = plan_for_default("loom", reason="fallback", target_event_ids=[e.id])
        coord.open_user_turn(e, plan)
        # Must NOT raise.
        actor._step_with_error_handling()


class DebounceTriggerExtension(unittest.TestCase):
    """Debounced user posts must wake actors holding open obligations.

    Pre-fix the trigger predicate gated on ``event.id == user_event_id``
    only, so a second post within the debounce window arrived with a
    different id and did not fire the priority-3 trigger. The fix adds
    the new id to ``user_turn.debounced_event_ids`` and the trigger
    predicate now accepts membership.
    """

    def test_debounced_event_wakes_required_actor(self):
        bus, state, coord = _setup(default_responder="loom")
        # Open a turn requiring loom on the first post.
        e1 = _user_post(bus, "first")
        plan = plan_with_required(
            ["loom"],
            routing_case="multi_opinion",
            target_event_ids=[e1.id],
            reason="open_chat",
        )
        ut = coord.open_user_turn(e1, plan)

        # Second post within the debounce window — same turn.
        e2 = ev.chat(sender="user", body="quick follow-up")
        bus.post(e2)
        e2.ts = e1.ts + 0.05
        coord.open_user_turn(e2, plan)
        self.assertIn(e2.id, ut.debounced_event_ids)

        # Trigger priority for the debounced post must fire.
        prio = _trigger_priority(e2, "loom", ut)
        self.assertEqual(prio, 3)

    def test_debounced_event_does_not_wake_unobligated_actor(self):
        bus, state, coord = _setup(default_responder="loom")
        e1 = _user_post(bus, "first")
        plan = plan_with_required(
            ["loom"],
            routing_case="multi_opinion",
            target_event_ids=[e1.id],
            reason="open_chat",
        )
        ut = coord.open_user_turn(e1, plan)
        e2 = ev.chat(sender="user", body="quick follow-up")
        bus.post(e2)
        e2.ts = e1.ts + 0.05
        coord.open_user_turn(e2, plan)
        # claude_code has no obligation in this turn.
        self.assertIsNone(_trigger_priority(e2, "claude_code", ut))


class IdleTimeoutWakeup(unittest.TestCase):
    """Actor wakeup must fire within ``user_turn_idle_timeout_s`` budget.

    Pre-fix the wakeup fell back to ``lease_ttl_s`` (60s default), which
    is wider than ``user_turn_idle_timeout_s`` (20s default). On a
    silent unresolved turn an actor would sleep up to 60s before
    checking idle.
    """

    def test_wakeup_bounded_by_idle_timeout(self):
        bus = MessageBus()
        cfg = RoomConfig(user_turn_idle_timeout_s=20, lease_ttl_s=60)
        state = RoomState(config=cfg)
        state.add_participant(ParticipantInfo(id="loom"))
        coord = RoomCoordinator(bus, state)
        actor = ParticipantActor(
            "loom",
            bus,
            coord,
            lambda actor, trig, lease: None,
        )
        self.assertLessEqual(
            actor.wakeup_timeout_s,
            cfg.user_turn_idle_timeout_s,
        )

    def test_explicit_override_wins(self):
        bus = MessageBus()
        cfg = RoomConfig(user_turn_idle_timeout_s=20, lease_ttl_s=60)
        state = RoomState(config=cfg)
        state.add_participant(ParticipantInfo(id="loom"))
        coord = RoomCoordinator(bus, state)
        actor = ParticipantActor(
            "loom",
            bus,
            coord,
            lambda actor, trig, lease: None,
            wakeup_timeout_s=5.0,
        )
        self.assertEqual(actor.wakeup_timeout_s, 5.0)


# ---------------------------------------------------------------------------
# v0.2.1 PR 4 — cursor advance discipline (audit findings A1, A2, A4)
# ---------------------------------------------------------------------------


class CursorAdvanceOnDeny(unittest.TestCase):
    """Denied trigger is re-pended for replay (audit A1).

    Pre-fix: ``_decide_once`` advanced the cursor to ``max(snap)`` and
    the trigger was lost on lease denial — no subsequent eligibility
    change could re-pick it up.

    Post-fix: cursor still advances unconditionally (so a kernel-
    emitted ``lease_denied`` event doesn't tight-loop the actor via
    ``bus.wait_after``), but the denied trigger is re-pended into
    ``_pending_direct_mentions``, whose replay path in
    ``_decide_once`` lifts it back into the next snap.
    """

    def _make(self, default_responder="loom"):
        bus, state, coord = _setup(default_responder=default_responder)
        calls: list[tuple[str, int]] = []

        def handler(actor, trig, lease):
            calls.append((actor.id, trig.id))
            coord.on_stream_end(
                lease, "committed", committed_text="ok", cost_tokens=1
            )

        return bus, state, coord, handler, calls

    def test_denied_trigger_is_repended_for_replay(self):
        # Inject a denying acquire_lease so the trigger fails. Verify
        # the trigger lands in the replay LRU AND the denied set so a
        # subsequent step doesn't immediately re-attempt under the same
        # conditions (would tight-loop in async mode).
        bus, state, coord, handler, calls = self._make()
        actor = ParticipantActor("loom", bus, coord, handler)
        original_acquire = coord.acquire_lease

        def denying(*args, **kwargs):
            return None

        coord.acquire_lease = denying  # type: ignore[method-assign]

        e = _user_post(bus, "hi")
        _open_default(coord, e, "loom")
        d1 = actor.step()
        self.assertEqual(d1.action, "DRAFT")
        self.assertEqual(d1.trigger_event_id, e.id)
        # The trigger lives in the replay LRU after denial, and in the
        # denied set so a no-change retry is suppressed.
        self.assertIn(e.id, actor._pending_direct_mentions)
        self.assertIn(e.id, actor._denied_trigger_ids)

        # Without an eligibility change, the next step short-circuits
        # to SKIP — no tight-loop re-attempt.
        d_noop = actor.step()
        self.assertEqual(d_noop.action, "SKIP")

        # A fresh user post signals possible eligibility change and
        # clears the denied set; restoring acquire_lease lets the
        # replayed trigger get picked again. The new turn opens with
        # loom as the default responder, so loom holds obligation on
        # the new user post — that becomes the trigger.
        coord.acquire_lease = original_acquire  # type: ignore[method-assign]
        e2 = _user_post(bus, "still there?")
        _open_default(coord, e2, "loom")
        d2 = actor.step()
        self.assertEqual(d2.action, "DRAFT")
        # The denied set was cleared by the new user post.
        self.assertNotIn(e.id, actor._denied_trigger_ids)

    def test_granted_trigger_is_not_repended(self):
        bus, state, coord, handler, calls = self._make()
        actor = ParticipantActor("loom", bus, coord, handler)
        e = _user_post(bus, "hi")
        _open_default(coord, e, "loom")
        d = actor.step()
        self.assertEqual(d.action, "DRAFT")
        # No re-pending: the grant path advanced cursor and consumed
        # the trigger.
        self.assertNotIn(e.id, actor._pending_direct_mentions)

    def test_skip_does_not_repend(self):
        bus, state, coord, _handler, _calls = self._make()
        actor = ParticipantActor(
            "claude_code", bus, coord, lambda *a, **k: None
        )
        e = _user_post(bus, "hi")  # broadcast → claude_code SKIPs
        _open_default(coord, e, "loom")
        d = actor.step()
        self.assertEqual(d.action, "SKIP")
        self.assertNotIn(e.id, actor._pending_direct_mentions)

    def test_cursor_advances_to_max_snap_on_deny(self):
        # Cursor still advances unconditionally — this guards against
        # the lease_denied tight-loop regression. With cursor at
        # max(snap), bus.wait_after blocks until a GENUINELY new
        # external event arrives.
        bus, state, coord, handler, _calls = self._make()
        actor = ParticipantActor("loom", bus, coord, handler)

        def denying(*args, **kwargs):
            return None

        coord.acquire_lease = denying  # type: ignore[method-assign]

        e = _user_post(bus, "hi")
        _open_default(coord, e, "loom")
        actor.step()
        self.assertGreaterEqual(actor._cursor, e.id)

    def test_cursor_is_monotonic(self):
        bus, state, coord, handler, _calls = self._make()
        actor = ParticipantActor("loom", bus, coord, handler)
        actor._cursor = 999_999
        e = _user_post(bus, "hi")
        _open_default(coord, e, "loom")
        actor.step()
        self.assertEqual(actor._cursor, 999_999)


class AgentDecisionShape(unittest.TestCase):
    """v0.2.1 PR 4 — ``AgentDecision`` dropped the unused
    ``considered_event_ids`` field (audit A2)."""

    def test_field_is_gone(self):
        from dataclasses import fields
        from loom.kernel.actor import AgentDecision

        names = {f.name for f in fields(AgentDecision)}
        self.assertNotIn("considered_event_ids", names)
        # Sanity: the surviving fields are exactly the documented set.
        self.assertEqual(names, {"action", "trigger_event_id", "reason"})


if __name__ == "__main__":
    unittest.main()
