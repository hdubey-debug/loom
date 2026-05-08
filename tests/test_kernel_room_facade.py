"""Tests for :class:`loom.room.LoomRoom` — the public-facing facade.

Covers:
- Construction from :class:`Agent` objects via the adapters.
- Context manager / start / stop.
- ``post`` returns event id; ``post_and_wait`` blocks until close.
- Mid-session ``add_agent`` / ``remove_agent``.
- Default ``policy_error_mode`` is fail-closed.
- Stdlib-only defaults (``run_console`` does not require rich /
  prompt_toolkit).
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from typing import Iterator

from loom.adapters import agent_from_send, agent_from_stream
from loom.contracts import ConversationPolicy
from loom.kernel.events import Event
from loom.kernel.obligations import plan_for_acknowledgement
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.single_responder import SingleResponderPolicy
from loom.room import LoomRoom, _agent_to_wiring


def _agent(agent_id: str, response: str = "ok",
           cost_tier: int = 1, persona: str = "",
           capability_block: str = ""):
    return agent_from_send(
        agent_id, lambda p: response,
        cost_tier=cost_tier, persona=persona,
        capability_block=capability_block,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class Construction(unittest.TestCase):

    def test_basic_room_construction(self):
        room = LoomRoom(agents=[_agent("a"), _agent("b")])
        try:
            self.assertEqual(room.participants, ["a", "b"])
            self.assertEqual(room.session.state.anchor_id, "a")
        finally:
            room.stop()

    def test_anchor_id_override(self):
        room = LoomRoom(
            agents=[_agent("a"), _agent("b")],
            anchor_id="b",
        )
        try:
            self.assertEqual(room.session.state.anchor_id, "b")
        finally:
            room.stop()

    def test_empty_agents_rejected(self):
        with self.assertRaises(ValueError):
            LoomRoom(agents=[])

    def test_duplicate_agent_ids_rejected(self):
        with self.assertRaises(ValueError):
            LoomRoom(agents=[_agent("dup"), _agent("dup")])

    def test_topic_set(self):
        room = LoomRoom(agents=[_agent("a")], topic="design review")
        try:
            self.assertEqual(room.topic, "design review")
        finally:
            room.stop()

    def test_default_policy_is_fail_closed(self):
        # Default ``policy_error_mode`` is ``"close_turn"``.
        room = LoomRoom(agents=[_agent("a")])
        try:
            self.assertEqual(
                room.session.coordinator.policy_error_mode, "close_turn")
        finally:
            room.stop()

    def test_policy_error_mode_passthrough(self):
        room = LoomRoom(
            agents=[_agent("a")],
            policy_error_mode="default_responder",
        )
        try:
            self.assertEqual(
                room.session.coordinator.policy_error_mode,
                "default_responder")
        finally:
            room.stop()

    def test_metadata_picked_up_from_agent(self):
        room = LoomRoom(agents=[
            _agent("a", cost_tier=5, persona="hello",
                   capability_block="caps"),
        ])
        try:
            wiring = room.session.wirings["a"]
            self.assertEqual(wiring.cost_tier, 5)
            self.assertEqual(wiring.persona, "hello")
            self.assertEqual(wiring.capability_block, "caps")
        finally:
            room.stop()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class Lifecycle(unittest.TestCase):

    def test_start_starts_actors(self):
        room = LoomRoom(agents=[_agent("a")])
        try:
            room.start()
            actor = room.session.actors[0]
            self.assertIsNotNone(actor._thread)
            self.assertTrue(actor._thread.is_alive())
        finally:
            room.stop()

    def test_start_idempotent(self):
        room = LoomRoom(agents=[_agent("a")])
        try:
            room.start()
            room.start()
        finally:
            room.stop()

    def test_context_manager_starts_and_stops(self):
        room = LoomRoom(agents=[_agent("a")])
        with room:
            actor = room.session.actors[0]
            self.assertIsNotNone(actor._thread)
        # After exit, actors are stopped.
        self.assertTrue(actor.stopped)

    def test_double_stop_idempotent(self):
        room = LoomRoom(agents=[_agent("a")])
        room.start()
        room.stop()
        # A second stop must not raise — the session sets _stop_event
        # the first time and short-circuits on subsequent calls.
        room.stop()
        # And again, just to be sure.
        room.stop()
        self.assertTrue(all(a.stopped for a in room.session.actors))

    def test_post_before_start_records_user_event(self):
        # A post issued before start() should still record the user
        # message on the bus. Actors are not yet running so no replies
        # are produced, but the user event is durable.
        room = LoomRoom(agents=[_agent("a")])
        try:
            eid = room.post("hello before start")
            self.assertGreaterEqual(eid, 0)
            chats = [
                e for e in room.session.bus.snapshot()
                if e.kind == "chat" and e.sender == "user"
            ]
            self.assertEqual(len(chats), 1)
            self.assertEqual(chats[0].body, "hello before start")
        finally:
            room.stop()

    def test_post_after_stop_does_not_segfault(self):
        # Posting after stop() must not crash. The bus is stopped so
        # the user message is rejected silently — no new events land
        # on the (stopped) ledger.
        room = LoomRoom(agents=[_agent("a")])
        room.start()
        room.stop()
        bus_len_before = len(room.session.bus)
        # post must not raise; it may return any value (ledger is closed).
        room.post("after stop")
        # Stopped bus refuses appends.
        self.assertEqual(len(room.session.bus), bus_len_before)
        self.assertTrue(room.session.bus.stopped)

    def test_start_after_stop_raises(self):
        room = LoomRoom(agents=[_agent("a")])
        room.start()
        room.stop()
        with self.assertRaises(RuntimeError):
            room.start()

    def test_remove_agent_during_active_turn_does_not_crash(self):
        # Removing a participant mid-turn must not leave the room in a
        # broken state. The lease for the active drafter is invalidated
        # (room_epoch bumps), but the room itself stays usable: bus
        # remains live, state is consistent, the membership change
        # propagated to ``state.participants``.
        import threading

        a_blocked = threading.Event()
        a_release = threading.Event()

        def a_send(_p):
            a_blocked.set()
            a_release.wait(timeout=2.0)
            return "alpha sufficient long reply that bypasses loop guard."

        a = agent_from_send("a", a_send)
        b = agent_from_send("b", lambda p: "bravo")

        with LoomRoom(
            agents=[a, b],
            policy=SingleResponderPolicy("a"),
        ) as room:
            epoch_before = room.session.state.room_epoch
            room.post("hi")
            self.assertTrue(a_blocked.wait(timeout=2.0))
            # Remove b mid-turn — must not raise.
            room.remove_agent("b")
            # Unblock a so its (now-invalid-lease) commit can return.
            a_release.set()
            # Room invariants after the remove:
            self.assertNotIn("b", room.participants)
            self.assertGreater(room.session.state.room_epoch, epoch_before)
            self.assertFalse(room.session.bus.stopped)
            self.assertEqual(room.participants, ["a"])


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

class Posting(unittest.TestCase):

    def test_post_returns_event_id(self):
        with LoomRoom(
            agents=[_agent("a"), _agent("b")],
            policy=OpenChatPolicy(),
        ) as room:
            eid = room.post("hello room")
            self.assertIsInstance(eid, int)
            self.assertGreaterEqual(eid, 0)

    def test_post_empty_text_rejected(self):
        room = LoomRoom(agents=[_agent("a")])
        try:
            with self.assertRaises(ValueError):
                room.post("")
        finally:
            room.stop()

    def test_post_and_wait_returns_replies(self):
        long_a = "alpha replies with a sufficiently long answer to bypass " \
                 "the loop guard's short-text duplicate detector."
        long_b = "bravo replies with a different long enough answer."
        with LoomRoom(
            agents=[_agent("a", long_a), _agent("b", long_b)],
            policy=OpenChatPolicy(),
        ) as room:
            replies = room.post_and_wait("hello room", timeout=5.0)
            senders = {ev.sender for ev in replies}
            self.assertEqual(senders, {"a", "b"})

    def test_post_and_wait_acknowledgement_returns_empty(self):
        # Default policy: "thanks" classified as acknowledgement → no turn.
        with LoomRoom(agents=[_agent("a")]) as room:
            result = room.post_and_wait("thanks", timeout=2.0)
            self.assertEqual(list(result), [])
            self.assertEqual(len(result), 0)
            self.assertFalse(result)
            self.assertEqual(result.closed_reason, "no_turn_opened")
            self.assertEqual(result.turn_id, -1)

    def test_post_and_wait_empty_text_rejected(self):
        room = LoomRoom(agents=[_agent("a")])
        try:
            with self.assertRaises(ValueError):
                room.post_and_wait("")
        finally:
            room.stop()


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

class DynamicMembership(unittest.TestCase):

    def test_add_agent_drafts_on_next_post(self):
        long_a = "alpha sufficient long reply that bypasses loop guard."
        long_b = "bravo sufficient long reply that bypasses loop guard."
        with LoomRoom(
            agents=[_agent("a", long_a)],
            policy=OpenChatPolicy(),
        ) as room:
            room.add_agent(_agent("b", long_b))
            self.assertIn("b", room.participants)
            replies = room.post_and_wait("hi", timeout=5.0)
            self.assertEqual({ev.sender for ev in replies}, {"a", "b"})

    def test_remove_agent_excludes_from_replies(self):
        long_a = "alpha sufficient long reply that bypasses loop guard."
        long_b = "bravo sufficient long reply that bypasses loop guard."
        with LoomRoom(
            agents=[_agent("a", long_a), _agent("b", long_b)],
            policy=OpenChatPolicy(),
        ) as room:
            room.remove_agent("b")
            self.assertNotIn("b", room.participants)
            replies = room.post_and_wait("hi", timeout=5.0)
            self.assertEqual({ev.sender for ev in replies}, {"a"})

    def test_remove_unknown_agent_raises(self):
        with LoomRoom(agents=[_agent("a")]) as room:
            with self.assertRaises(KeyError):
                room.remove_agent("not_in_room")


# ---------------------------------------------------------------------------
# _agent_to_wiring
# ---------------------------------------------------------------------------

class AgentToWiring(unittest.TestCase):

    def test_stream_agent_passes_through(self):
        agent = agent_from_stream(
            "x", lambda p: ["hello"], persona="who")
        wiring = _agent_to_wiring(agent)
        self.assertEqual(wiring.id, "x")
        self.assertIs(wiring.proxy, agent)
        self.assertEqual(wiring.persona, "who")

    def test_send_only_object_wrapped(self):
        class SendOnly:
            id = "x"

            def send(self, p):
                return "ok"

        wiring = _agent_to_wiring(SendOnly())
        # Must be wrapped in a streaming adapter.
        self.assertEqual(wiring.id, "x")
        self.assertTrue(hasattr(wiring.proxy, "stream"))
        chunks = list(wiring.proxy.stream("p"))
        self.assertEqual(chunks, ["ok"])

    def test_missing_id_rejected(self):
        class NoId:
            def stream(self, p):
                yield "x"

        with self.assertRaises(TypeError):
            _agent_to_wiring(NoId())  # type: ignore[arg-type]

    def test_missing_stream_and_send_rejected(self):
        class Bare:
            id = "x"

        with self.assertRaises(TypeError):
            _agent_to_wiring(Bare())  # type: ignore[arg-type]

    def test_metadata_defaults(self):
        class Bare:
            id = "x"

            def stream(self, p):
                yield ""

        wiring = _agent_to_wiring(Bare())
        self.assertEqual(wiring.persona, "")
        self.assertEqual(wiring.capability_block, "")
        self.assertEqual(wiring.cost_tier, 1)
        self.assertTrue(wiring.capable)


# ---------------------------------------------------------------------------
# Run console
# ---------------------------------------------------------------------------

class RunConsole(unittest.TestCase):
    """Verifies :meth:`run_console` works without rich / prompt_toolkit."""

    def test_run_console_with_stdlib_defaults(self):
        # Provide a prompt_fn that ends after one message via EOFError.
        seen_notifications: list[str] = []
        inputs = iter(["thanks"])  # acknowledgement → no turn opens

        def prompt():
            try:
                return next(inputs)
            except StopIteration:
                raise EOFError

        room = LoomRoom(agents=[_agent("a", "hi")])
        # Capture stdout in case _thread_safe_print fires.
        with redirect_stdout(io.StringIO()):
            room.run_console(
                prompt_fn=prompt,
                notify=seen_notifications.append,
            )
        # After run_console returns, the room is stopped.
        self.assertTrue(all(a.stopped for a in room.session.actors))

    def test_run_console_quits_on_slash_quit(self):
        inputs = iter(["/quit"])

        def prompt():
            return next(inputs)

        room = LoomRoom(agents=[_agent("a")])
        room.run_console(prompt_fn=prompt, notify=lambda _: None)
        self.assertTrue(all(a.stopped for a in room.session.actors))


# ---------------------------------------------------------------------------
# Single-responder integration (small end-to-end)
# ---------------------------------------------------------------------------

class SingleResponderIntegration(unittest.TestCase):

    def test_only_responder_drafts(self):
        long_a = "alpha sufficient long reply that bypasses loop guard."
        long_b = "bravo sufficient long reply that bypasses loop guard."
        with LoomRoom(
            agents=[_agent("a", long_a), _agent("b", long_b)],
            policy=SingleResponderPolicy("a"),
        ) as room:
            replies = room.post_and_wait("hello", timeout=5.0)
            senders = {ev.sender for ev in replies}
            self.assertEqual(senders, {"a"})


# ---------------------------------------------------------------------------
# Throwing policy: fail-closed by default
# ---------------------------------------------------------------------------

class _ThrowingPolicy(ConversationPolicy):
    name = "thrower"

    def plan_user_turn(self, user_event, state, *, prior_speaker=None):
        raise RuntimeError("policy boom")


class FailClosedDefault(unittest.TestCase):

    def test_throwing_policy_fails_closed(self):
        with LoomRoom(
            agents=[_agent("a", "x")],
            policy=_ThrowingPolicy(),
        ) as room:
            replies = room.post_and_wait("hi", timeout=2.0)
            # No turn opens, no replies.
            self.assertEqual(list(replies), [])
            # ``policy_error`` event is recorded.
            errors = [
                ev for ev in room.session.bus.snapshot()
                if ev.kind == "control" and isinstance(ev.body, dict)
                and ev.body.get("control_type") == "policy_error"
            ]
            self.assertEqual(len(errors), 1)


class PolicyErrorModeIntegration(unittest.TestCase):
    """End-to-end coverage of the two non-default ``policy_error_mode`` values.

    The default ``"close_turn"`` is covered by ``FailClosedDefault`` above;
    here we verify the integration paths through ``LoomRoom`` for the other
    two modes.
    """

    def test_default_responder_mode_falls_back_to_responder(self):
        long_b = "bravo sufficient long reply that bypasses loop guard."
        with LoomRoom(
            agents=[_agent("a", "alpha"), _agent("b", long_b)],
            policy=_ThrowingPolicy(),
            policy_error_mode="default_responder",
            default_responder_id="b",
        ) as room:
            replies = room.post_and_wait("hi", timeout=5.0)
            senders = {ev.sender for ev in replies}
            # Fallback responder ``b`` actually drafted.
            self.assertEqual(senders, {"b"})

    def test_default_responder_mode_still_emits_policy_error(self):
        long_b = "bravo sufficient long reply that bypasses loop guard."
        with LoomRoom(
            agents=[_agent("a", "alpha"), _agent("b", long_b)],
            policy=_ThrowingPolicy(),
            policy_error_mode="default_responder",
            default_responder_id="b",
        ) as room:
            room.post_and_wait("hi", timeout=5.0)
            errors = [
                ev for ev in room.session.bus.snapshot()
                if ev.kind == "control" and isinstance(ev.body, dict)
                and ev.body.get("control_type") == "policy_error"
            ]
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0].body.get("exception_class"),
                             "RuntimeError")

    def test_raise_mode_propagates_to_caller(self):
        # In raise mode, the policy exception escapes back to whoever
        # triggered the user-turn open — i.e. ``LoomRoom.post``.
        with LoomRoom(
            agents=[_agent("a", "x")],
            policy=_ThrowingPolicy(),
            policy_error_mode="raise",
        ) as room:
            with self.assertRaises(RuntimeError) as ctx:
                room.post("hi")
            self.assertIn("policy boom", str(ctx.exception))
            # And a ``policy_error`` event was still recorded before the
            # re-raise (observability is preserved).
            errors = [
                ev for ev in room.session.bus.snapshot()
                if ev.kind == "control" and isinstance(ev.body, dict)
                and ev.body.get("control_type") == "policy_error"
            ]
            self.assertEqual(len(errors), 1)


# ---------------------------------------------------------------------------
# PASS-as-completion: required participant emitting [PASS] should close
# the turn promptly (well under the idle timeout).
# ---------------------------------------------------------------------------

class PassClosesTurn(unittest.TestCase):

    def test_required_pass_closes_turn_quickly(self):
        import time
        # Single required responder that emits exactly [PASS]. Before the
        # fix, the obligation never resolved and the room idled for 20 s.
        with LoomRoom(
            agents=[_agent("a", "[PASS]")],
            policy=SingleResponderPolicy("a"),
        ) as room:
            t0 = time.monotonic()
            replies = room.post_and_wait("hello", timeout=5.0)
            elapsed = time.monotonic() - t0
        # No chat event (PASS suppresses the body).
        self.assertEqual(list(replies), [])
        # Turn must close cleanly, not by idle timeout.
        self.assertLess(elapsed, 1.0,
                        f"PASS-completion took {elapsed:.2f}s — "
                        "should be near-instant")


# ---------------------------------------------------------------------------
# Dead-letter obligation transfer (v0.1.2): when the only required
# participant is removed mid-turn, their obligation transfers to a live
# fallback who actually drafts.
# ---------------------------------------------------------------------------

class DeadLetterFallbackResponds(unittest.TestCase):

    def test_removed_required_agent_fallback_replies(self):
        import threading
        import time
        long_b = "bravo sufficient long reply that bypasses loop guard."
        a_blocked = threading.Event()
        a_release = threading.Event()

        def a_send(_p):
            a_blocked.set()
            a_release.wait(timeout=2.0)
            return "a's reply"

        a = agent_from_send("a", a_send)
        b = agent_from_send("b", lambda p: long_b)

        with LoomRoom(
            agents=[a, b],
            policy=SingleResponderPolicy("a"),
        ) as room:
            room.post("hi")
            # Wait for A to start drafting (A's send is blocked).
            self.assertTrue(
                a_blocked.wait(timeout=2.0),
                "A's send did not start before timeout",
            )
            # Remove A — obligation transfers to B.
            room.remove_agent("a")
            # Release A's send (A's lease was invalidated; the commit
            # will be rejected when stream_end runs).
            a_release.set()
            # Poll for turn closure.
            for _ in range(200):
                ut = room.session.coordinator.user_turn
                if ut and ut.state == "closed":
                    break
                time.sleep(0.01)
            ut = room.session.coordinator.user_turn
            self.assertEqual(ut.state, "closed")
            # B replied; A's stream did not commit (lease invalidated).
            chats = [
                e for e in room.session.bus.snapshot()
                if e.kind == "chat" and e.sender in ("a", "b")
            ]
            senders = [e.sender for e in chats]
            self.assertIn("b", senders)
            self.assertNotIn("a", senders)


if __name__ == "__main__":
    unittest.main()
