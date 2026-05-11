"""End-to-end tests for ``loom.runtime``.

Exercises the wiring (build_loom_session + slash commands +
post_user_text) using FakeProxy actors, without hitting any real LLM.
The actors are driven via :meth:`ParticipantActor.step` (synchronous)
so tests don't depend on thread timing.
"""

from __future__ import annotations

import tempfile
import unittest

from loom.kernel import events as ev
from loom.runtime import (
    ParticipantWiring,
    SendProxyAdapter,
    _format_control,
    _make_console_subscriber,
    build_loom_session,
    handle_slash_command,
    post_user_text,
)


class FakeSendProxy:
    """Non-streaming proxy: ``send(prompt) -> str``."""

    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def send(self, prompt):
        self.calls += 1
        return self.response


def _wirings(*specs):
    """Each spec is (id, response_text, cost_tier)."""
    return [
        ParticipantWiring(
            id=pid,
            proxy=SendProxyAdapter(FakeSendProxy(resp)),
            cost_tier=tier,
        )
        for pid, resp, tier in specs
    ]


def _drive(actors):
    """Step every actor once."""
    for a in actors:
        a.step()


class WiringSmoke(unittest.TestCase):
    def test_session_assembled(self):
        session = build_loom_session(
            _wirings(("loom", "ack", 0)),
            default_responder_id="loom",
            auto_start=False,
        )
        try:
            self.assertEqual(set(session.state.participants.keys()), {"loom"})
            self.assertEqual(session.state.default_responder_id, "loom")
        finally:
            session.stop()


class BroadcastFlow(unittest.TestCase):
    def test_plain_user_message_broadcasts_to_all_actives(self):
        session = build_loom_session(
            _wirings(("loom", "I am loom.", 0), ("claude_code", "I am claude.", 2)),
            default_responder_id="loom",
            auto_start=False,
        )
        try:
            post_user_text(session, "hi room")
            _drive(session.actors)
            chats = [e for e in session.bus.snapshot() if e.kind == "chat" and e.sender != "user"]
            senders = {c.sender for c in chats}
            # v0 broadcast model: every active capable participant gets
            # a must-obligation and replies (or [PASS]es). Both agents
            # speak here because both are wired with non-PASS replies.
            self.assertEqual(senders, {"loom", "claude_code"})
        finally:
            session.stop()


class DirectMentionFlow(unittest.TestCase):
    def test_mention_drafts_addressed_only(self):
        session = build_loom_session(
            _wirings(("loom", "loom always answers.", 0), ("claude_code", "claude here.", 2)),
            default_responder_id="loom",
            auto_start=False,
        )
        try:
            post_user_text(session, "@claude_code hi")
            _drive(session.actors)
            chats = [e for e in session.bus.snapshot() if e.kind == "chat" and e.sender != "user"]
            senders = {c.sender for c in chats}
            # claude_code is required (direct-mention plan); loom has no
            # obligation in this turn and should NOT draft.
            self.assertEqual(senders, {"claude_code"})
        finally:
            session.stop()

    def test_multi_mention_drafts_each(self):
        session = build_loom_session(
            _wirings(
                ("loom", "loom.", 0), ("claude_code", "claude.", 2), ("gemini_cli", "gemini.", 1)
            ),
            default_responder_id="loom",
            auto_start=False,
        )
        try:
            post_user_text(session, "@claude_code @gemini_cli weigh in")
            _drive(session.actors)
            chats = [e for e in session.bus.snapshot() if e.kind == "chat" and e.sender != "user"]
            senders = {c.sender for c in chats}
            self.assertEqual(senders, {"claude_code", "gemini_cli"})
        finally:
            session.stop()


class AcknowledgementFlow(unittest.TestCase):
    def test_thanks_does_not_open_user_turn(self):
        session = build_loom_session(
            _wirings(("loom", "ack", 0)),
            default_responder_id="loom",
            auto_start=False,
        )
        try:
            post_user_text(session, "thanks")
            self.assertIsNone(session.coordinator.user_turn)
            # No agent should reply.
            _drive(session.actors)
            chats = [e for e in session.bus.snapshot() if e.kind == "chat" and e.sender != "user"]
            self.assertEqual(chats, [])
        finally:
            session.stop()


class PassSuppressionEndToEnd(unittest.TestCase):
    def test_pass_response_emits_passed_status(self):
        session = build_loom_session(
            _wirings(("loom", "[PASS]", 0)),
            default_responder_id="loom",
            auto_start=False,
        )
        try:
            post_user_text(session, "hi")
            _drive(session.actors)
            chats = [e for e in session.bus.snapshot() if e.kind == "chat" and e.sender == "loom"]
            self.assertEqual(chats, [])
            ends = [
                e
                for e in session.bus.snapshot()
                if e.kind == "stream"
                and isinstance(e.body, dict)
                and e.body.get("stream_event") == "end"
            ]
            self.assertEqual(ends[-1].body["status"], "passed")
        finally:
            session.stop()


class SlashCommands(unittest.TestCase):
    def setUp(self):
        self.session = build_loom_session(
            _wirings(("loom", "ack", 0), ("claude_code", "ack", 2)),
            default_responder_id="loom",
            auto_start=False,
        )

    def tearDown(self):
        self.session.stop()

    def test_who(self):
        r = handle_slash_command("/who", self.session)
        self.assertTrue(r.handled)
        self.assertIn("loom", r.message)
        self.assertIn("claude_code", r.message)

    def test_mode_returns_deprecation(self):
        r = handle_slash_command("/mode council", self.session)
        self.assertTrue(r.handled)
        self.assertIn("removed in Loom v0", r.message)

    def test_topic_set_and_clear(self):
        handle_slash_command("/topic the moon", self.session)
        self.assertEqual(self.session.state.topic, "the moon")
        handle_slash_command("/topic", self.session)
        self.assertIsNone(self.session.state.topic)

    def test_add_via_slash_returns_guidance(self):
        # /add via slash command can't construct a proxy; the command
        # now points users at the programmatic add path.
        r = handle_slash_command("/add new_agent", self.session)
        self.assertTrue(r.handled)
        self.assertIn("not supported", r.message)
        self.assertNotIn("new_agent", self.session.state.participants)

    def test_remove_via_slash(self):
        # /remove still works for participants wired at session bootstrap.
        r = handle_slash_command("/remove claude_code", self.session)
        self.assertTrue(r.handled)
        self.assertNotIn("claude_code", self.session.state.participants)

    def test_remove_default_responder_falls_back(self):
        r = handle_slash_command("/remove loom", self.session)
        self.assertTrue(r.handled)
        self.assertEqual(self.session.state.default_responder_id, "claude_code")

    def test_cancel_closes_turn(self):
        post_user_text(self.session, "anyone home")
        handle_slash_command("/cancel", self.session)
        self.assertEqual(self.session.coordinator.user_turn.state, "closed")
        self.assertEqual(self.session.coordinator.user_turn.closure_reason, "cancelled")

    def test_cancel_marks_obligations_resolved(self):
        post_user_text(self.session, "anyone home")
        ut = self.session.coordinator.user_turn
        self.assertTrue(any(not o.resolved for o in ut.obligations.values()))
        handle_slash_command("/cancel", self.session)
        ut = self.session.coordinator.user_turn
        for ob in ut.obligations.values():
            self.assertTrue(ob.resolved)

    def test_dm(self):
        r = handle_slash_command("/dm claude_code psst", self.session)
        self.assertTrue(r.handled)
        dms = [e for e in self.session.bus.snapshot() if e.channel == "dm:claude_code"]
        self.assertEqual(len(dms), 1)
        self.assertEqual(dms[0].body, "psst")

    def test_unknown_command_returns_helpful_error(self):
        r = handle_slash_command("/notreal", self.session)
        self.assertTrue(r.handled)
        self.assertIn("unknown command", r.message)
        self.assertIn("/notreal", r.message)
        chats = [e for e in self.session.bus.snapshot() if e.kind == "chat" and e.sender == "user"]
        self.assertEqual(chats, [])

    def test_quit(self):
        r = handle_slash_command("/leave", self.session)
        self.assertTrue(r.quit)


class FloorControlSlashCommands(unittest.TestCase):
    """v0 deterministic floor controls — /roles /floor /release /quiet /style /goal."""

    def setUp(self):
        self.session = build_loom_session(
            _wirings(("loom", "ack", 0), ("claude_code", "ack", 2), ("OAI", "ack", 1)),
            default_responder_id="loom",
            auto_start=False,
        )

    def tearDown(self):
        self.session.stop()

    def test_roles_set(self):
        r = handle_slash_command(
            "/roles loom=teacher claude_code=quizzer OAI=grader",
            self.session,
        )
        self.assertTrue(r.handled)
        self.assertEqual(
            self.session.state.control.roles,
            {"loom": "teacher", "claude_code": "quizzer", "OAI": "grader"},
        )

    def test_roles_clear_with_empty_arg(self):
        handle_slash_command(
            "/roles loom=teacher",
            self.session,
        )
        # Display current
        r = handle_slash_command("/roles", self.session)
        self.assertIn("loom=teacher", r.message)

    def test_roles_unknown_pid_rejected_atomically(self):
        r = handle_slash_command(
            "/roles loom=teacher ghost=quizzer",
            self.session,
        )
        # Unknown id rejects the whole assignment.
        self.assertEqual(self.session.state.control.roles, {})
        self.assertIn("usage", r.message.lower())

    def test_floor_set_returns_removed_notice(self):
        # v0.2: /floor, /release, /quiet were removed along with the
        # ``RoomControlState.floor_owner`` field. The runtime now
        # returns a removed-feature notice rather than mutating state.
        r = handle_slash_command("/floor loom", self.session)
        self.assertTrue(r.handled)
        self.assertIn("removed in v0.2", r.message)

    def test_release_returns_removed_notice(self):
        r = handle_slash_command("/release", self.session)
        self.assertTrue(r.handled)
        self.assertIn("removed in v0.2", r.message)

    def test_quiet_returns_removed_notice(self):
        r = handle_slash_command("/quiet OAI claude_code", self.session)
        self.assertTrue(r.handled)
        self.assertIn("removed in v0.2", r.message)

    def test_brief_normal_detailed(self):
        handle_slash_command("/brief", self.session)
        self.assertEqual(self.session.state.control.style, "brief")
        handle_slash_command("/normal", self.session)
        self.assertEqual(self.session.state.control.style, "normal")
        handle_slash_command("/detailed", self.session)
        self.assertEqual(self.session.state.control.style, "detailed")

    def test_goal_set_and_show(self):
        # P2.3: /goal is now an alias for /topic. Both write to
        # ``state.topic``.
        r = handle_slash_command("/goal teach derivatives", self.session)
        self.assertTrue(r.handled)
        self.assertEqual(self.session.state.topic, "teach derivatives")
        r = handle_slash_command("/goal", self.session)
        self.assertIn("teach derivatives", r.message)

    def test_control_dump(self):
        handle_slash_command("/roles loom=teacher", self.session)
        handle_slash_command("/brief", self.session)
        r = handle_slash_command("/control", self.session)
        self.assertIn("loom=teacher", r.message)
        self.assertIn("style: brief", r.message)

    def test_directed_turn_sets_wait_for_user_after_close(self):
        session = build_loom_session(
            _wirings(("loom", "loom reply", 0), ("claude_code", "claude reply", 2)),
            default_responder_id="loom",
            auto_start=False,
        )
        try:
            post_user_text(
                session,
                "@loom hi",
            )
            for a in session.actors:
                a.step()
            self.assertTrue(session.state.control.wait_for_user)
        finally:
            session.stop()


class JournalIntegration(unittest.TestCase):
    def test_session_writes_events_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session = build_loom_session(
                _wirings(("loom", "ok", 0)),
                default_responder_id="loom",
                journal_dir=tmpdir,
                auto_start=False,
            )
            try:
                post_user_text(session, "hi")
                _drive(session.actors)
            finally:
                session.stop()
            events_path = __import__("pathlib").Path(tmpdir) / "events.jsonl"
            self.assertTrue(events_path.exists())
            content = events_path.read_text()
            self.assertGreater(len(content.splitlines()), 0)


class ConsoleSubscriber(unittest.TestCase):
    """Console rendering: sender labels, pretty controls, no dict-repr leak."""

    def setUp(self):
        self.notes: list[str] = []
        self.sub = _make_console_subscriber(self.notes.append)

    def test_chat_event_from_agent_renders_with_sender_label(self):
        e = ev.chat(sender="loom", body="hello world")
        self.sub(e)
        self.assertEqual(self.notes, ["\nloom ▸ hello world"])

    def test_chat_event_from_user_is_suppressed(self):
        e = ev.chat(sender="user", body="hi")
        self.sub(e)
        self.assertEqual(self.notes, [])

    def test_dm_chat_is_not_echoed_in_console(self):
        e = ev.chat(sender="loom", body="psst", channel="dm:claude_code")
        self.sub(e)
        self.assertEqual(self.notes, [])

    def test_topic_changed_pretty_print(self):
        self.sub(ev.topic_changed(None, "the moon"))
        self.assertEqual(self.notes, ["\n· topic → the moon"])

    def test_topic_cleared_pretty_print(self):
        self.sub(ev.topic_changed("foo", ""))
        self.assertEqual(self.notes, ["\n· topic → (cleared)"])

    def test_user_turn_closed_completed_is_suppressed(self):
        self.sub(ev.user_turn_closed(user_turn_id=0, reason="completed"))
        self.assertEqual(self.notes, [])

    def test_user_turn_closed_no_responder_renders_friendly_hint(self):
        self.sub(ev.user_turn_closed(user_turn_id=0, reason="no_responder"))
        self.assertEqual(self.notes, ["\n· (no agent responded)"])

    def test_user_turn_closed_obligation_unresolved_renders_hint(self):
        self.sub(ev.user_turn_closed(user_turn_id=0, reason="obligation_unresolved"))
        self.assertEqual(self.notes, ["\n· (required participant did not reply)"])

    def test_user_turn_closed_cancelled_renders(self):
        self.sub(ev.user_turn_closed(user_turn_id=0, reason="cancelled"))
        self.assertEqual(self.notes, ["\n· user turn closed (cancelled)"])

    def test_outbound_user_dm_renders_with_target(self):
        e = ev.chat(
            sender="user", body="psst quiet", addressees=["claude_code"], channel="dm:claude_code"
        )
        self.sub(e)
        self.assertEqual(self.notes, ["\n(dm → claude_code) ▸ psst quiet"])

    def test_main_channel_user_chat_still_suppressed(self):
        e = ev.chat(sender="user", body="hi room")
        self.sub(e)
        self.assertEqual(self.notes, [])

    def test_agent_to_agent_dm_remains_private(self):
        e = ev.chat(sender="loom", body="psst", channel="dm:claude_code")
        self.sub(e)
        self.assertEqual(self.notes, [])

    def test_user_turn_opened_is_suppressed(self):
        self.sub(
            ev.user_turn_opened(
                user_turn_id=0,
                routing_case="question",
                required_participants=["loom"],
            )
        )
        self.assertEqual(self.notes, [])

    def test_obligation_recorded_silent(self):
        self.sub(
            ev.obligation_recorded(
                obligation_id=1,
                participant_id="loom",
                level="must",
                target_event_ids=[1],
                reason="x",
            )
        )
        self.assertEqual(self.notes, [])

    def test_obligation_resolved_silent(self):
        self.sub(
            ev.obligation_resolved(
                obligation_id=1,
                participant_id="loom",
                resolved_by_event_id=42,
            )
        )
        self.assertEqual(self.notes, [])

    def test_stream_events_produce_no_console_output(self):
        self.sub(ev.stream_start(lease_id=0, participant_id="loom", trigger_event_id=0))
        self.sub(ev.stream_delta(lease_id=0, participant_id="loom", text="hi"))
        self.sub(ev.stream_end(lease_id=0, participant_id="loom", status="committed"))
        self.assertEqual(self.notes, [])

    def test_unknown_control_type_does_not_leak_dict(self):
        e = ev.Event(
            kind="control", sender="system", body={"control_type": "made_up_event", "x": 1}
        )
        self.sub(e)
        self.assertEqual(self.notes, [])

    def test_format_control_returns_none_for_non_dict_body(self):
        e = ev.Event(kind="control", sender="system", body="oops")
        self.assertIsNone(_format_control(e))

    def test_legacy_mode_changed_is_silently_dropped(self):
        """Old v1 ``mode_changed`` events come through unrecognized — the
        subscriber must drop them, not crash or leak."""
        e = ev.Event(
            kind="control",
            sender="system",
            body={"control_type": "mode_changed", "old": "normal", "new": "council"},
        )
        self.sub(e)
        self.assertEqual(self.notes, [])


class DynamicMembership(unittest.TestCase):
    """LoomSession.add_agent / remove_agent — mid-session membership ops."""

    def test_add_agent_mid_session_drafts_on_next_post(self):
        session = build_loom_session(
            _wirings(("loom", "loom reply", 0)),
            default_responder_id="loom",
            auto_start=False,
        )
        try:
            new_wiring = ParticipantWiring(
                id="newcomer",
                proxy=SendProxyAdapter(FakeSendProxy("newcomer reply")),
                cost_tier=3,
            )
            session.add_agent(new_wiring)

            self.assertIn("newcomer", session.state.participants)
            self.assertIn("newcomer", session.wirings)
            self.assertIn("newcomer", {a.id for a in session.actors})

            post_user_text(session, "hello room")
            _drive(session.actors)

            chats = [e for e in session.bus.snapshot() if e.kind == "chat" and e.sender != "user"]
            senders = {c.sender for c in chats}
            self.assertEqual(senders, {"loom", "newcomer"})
        finally:
            session.stop()

    def test_add_agent_duplicate_id_raises(self):
        session = build_loom_session(
            _wirings(("loom", "x", 0)),
            auto_start=False,
        )
        try:
            with self.assertRaises(ValueError):
                session.add_agent(
                    ParticipantWiring(id="loom", proxy=SendProxyAdapter(FakeSendProxy("dup")))
                )
        finally:
            session.stop()

    def test_add_agent_after_stop_raises(self):
        session = build_loom_session(
            _wirings(("loom", "x", 0)),
            auto_start=False,
        )
        session.stop()
        with self.assertRaises(RuntimeError):
            session.add_agent(ParticipantWiring(id="x", proxy=SendProxyAdapter(FakeSendProxy("y"))))

    def test_remove_agent_drops_from_participants_and_actors(self):
        session = build_loom_session(
            _wirings(("loom", "x", 0), ("claude_code", "y", 2)),
            default_responder_id="loom",
            auto_start=False,
        )
        try:
            session.remove_agent("claude_code")
            self.assertNotIn("claude_code", session.state.participants)
            self.assertNotIn("claude_code", session.wirings)
            self.assertNotIn("claude_code", {a.id for a in session.actors})
        finally:
            session.stop()

    def test_remove_agent_unblocks_obligation_closure(self):
        # Open turn requires both agents → remove one → turn should
        # close cleanly without that agent's draft.
        long_loom = (
            "loom answers with a sufficiently long reply that bypasses "
            "the loop guard short-text duplicate detector for the test."
        )
        session = build_loom_session(
            _wirings(("loom", long_loom, 0), ("claude_code", "claude reply", 2)),
            default_responder_id="loom",
            auto_start=False,
        )
        try:
            post_user_text(session, "hi")
            # Drive only loom; claude_code's required obligation stays open.
            for a in session.actors:
                if a.id == "loom":
                    a.step()
            ut = session.coordinator.user_turn
            self.assertIsNotNone(ut)
            self.assertEqual(ut.state, "open")

            session.remove_agent("claude_code")

            ut = session.coordinator.user_turn
            self.assertEqual(ut.state, "closed")
        finally:
            session.stop()

    def test_remove_unknown_agent_raises(self):
        session = build_loom_session(
            _wirings(("loom", "x", 0)),
            auto_start=False,
        )
        try:
            with self.assertRaises(KeyError):
                session.remove_agent("not_in_room")
        finally:
            session.stop()

    def test_add_agent_then_post_with_auto_start(self):
        # Same as the first test but using auto_start=True so add_agent
        # has to start the new actor itself.
        session = build_loom_session(
            _wirings(("loom", "loom reply", 0)),
            auto_start=True,
        )
        try:
            session.add_agent(
                ParticipantWiring(
                    id="newcomer",
                    proxy=SendProxyAdapter(FakeSendProxy("nc reply")),
                    cost_tier=3,
                )
            )
            new_actor = next(a for a in session.actors if a.id == "newcomer")
            self.assertIsNotNone(new_actor._thread)
            self.assertTrue(new_actor._thread.is_alive())
        finally:
            session.stop()

    def test_session_start_idempotent(self):
        session = build_loom_session(
            _wirings(("loom", "x", 0)),
            auto_start=False,
        )
        try:
            session.start()
            session.start()  # idempotent
            actor = session.actors[0]
            self.assertIsNotNone(actor._thread)
        finally:
            session.stop()

    def test_session_start_after_stop_raises(self):
        session = build_loom_session(
            _wirings(("loom", "x", 0)),
            auto_start=False,
        )
        session.stop()
        with self.assertRaises(RuntimeError):
            session.start()


if __name__ == "__main__":
    unittest.main()
