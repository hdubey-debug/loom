"""Targeted coverage of loom/runtime.py — console + slash command branches.

Drives `run_loom_console` via injected `prompt_fn`/`notify` so the entire
loop body is exercised without an interactive TTY. Covers SendProxyAdapter
edge cases (None result, attribute extraction, cancel error swallow),
the slash-command branches that the four bundled policies' happy paths
don't hit (e.g. /control, /quiet with unknown ids, /floor with empty
args), and the user_turn_closed → format_control branches.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from loom.kernel import events as ev
from loom.kernel.events import Event
from loom.policy.open_chat import OpenChatPolicy
from loom.runtime import (
    LoomSession,
    ParticipantWiring,
    SendProxyAdapter,
    _format_control,
    _make_console_subscriber,
    build_loom_session,
    handle_slash_command,
    post_user_text,
    run_loom_console,
)


# ---------------------------------------------------------------------------
# SendProxyAdapter
# ---------------------------------------------------------------------------


def test_send_proxy_adapter_yields_nothing_when_cancelled():
    """Covers: runtime.py:65-67 — cancelled stream yields nothing."""

    class P:
        def send(self, prompt):
            return "hi"

    adapter = SendProxyAdapter(P())
    adapter.cancel()
    assert list(adapter.stream("p")) == []


def test_send_proxy_adapter_yields_nothing_when_text_empty():
    """Covers: runtime.py:71->exit — empty text yields no chunks."""

    class P:
        def send(self, prompt):
            return ""

    adapter = SendProxyAdapter(P())
    assert list(adapter.stream("p")) == []


def test_send_proxy_adapter_cancel_handles_missing_cancel_attr():
    """Covers: runtime.py:76-77 — proxy without cancel() attr."""

    class P:
        def send(self, prompt):
            return "ok"

    adapter = SendProxyAdapter(P())
    adapter.cancel()  # must not raise


def test_send_proxy_adapter_cancel_swallows_proxy_cancel_exception():
    """Covers: runtime.py:78-81 — proxy.cancel() raising is swallowed."""

    class P:
        def send(self, prompt):
            return "ok"

        def cancel(self):
            raise RuntimeError("cancel hates us")

    adapter = SendProxyAdapter(P())
    adapter.cancel()  # must not raise


def test_send_proxy_adapter_extract_text_none_returns_empty():
    """Covers: runtime.py:85-86 — None result → empty string."""
    assert SendProxyAdapter._extract_text_static(None) == ""


def test_send_proxy_adapter_extract_text_from_object_attribute():
    """Covers: runtime.py:89-92 — attribute extraction (.text/.body/.content/.output)."""

    class WithText:
        text = "hello"

    class WithBody:
        body = "hello"

    class WithContent:
        content = "hello"

    class WithOutput:
        output = "hello"

    for cls in (WithText, WithBody, WithContent, WithOutput):
        assert SendProxyAdapter._extract_text_static(cls()) == "hello"


def test_send_proxy_adapter_extract_text_falls_through_to_str():
    """Covers: runtime.py:93 — no recognized attr → str(result)."""

    class Obj:
        def __str__(self):
            return "stringified"

    assert SendProxyAdapter._extract_text_static(Obj()) == "stringified"


# ---------------------------------------------------------------------------
# Session-stop journal snapshot exception
# ---------------------------------------------------------------------------


def test_session_stop_swallows_journal_snapshot_exception(tmp_path):
    """Covers: runtime.py:220-223 — journal.snapshot raising on stop."""
    wiring = ParticipantWiring(
        id="alice",
        proxy=SendProxyAdapter(_DummyAgent("hi")),
    )
    session = build_loom_session(
        [wiring],
        journal_dir=tmp_path,
        policy=OpenChatPolicy(),
    )

    # Patch the journal to raise on snapshot during stop.
    def raising(*a, **k):
        raise RuntimeError("snapshot raised")

    session.journal.snapshot = raising  # type: ignore[method-assign]
    session.stop()  # must not raise


# ---------------------------------------------------------------------------
# Add/remove agent edge cases
# ---------------------------------------------------------------------------


def test_session_add_agent_after_stop_raises_runtimeerror(tmp_path):
    """Covers: runtime.py:152-153 — add_agent after session.stop() rejects."""
    wiring = ParticipantWiring(
        id="alice",
        proxy=SendProxyAdapter(_DummyAgent("hi")),
    )
    session = build_loom_session([wiring], policy=OpenChatPolicy())
    session.stop()
    with pytest.raises(RuntimeError, match="stopped"):
        session.add_agent(
            ParticipantWiring(
                id="bob",
                proxy=SendProxyAdapter(_DummyAgent("hi")),
            )
        )


def test_session_add_agent_without_draft_handler_raises(tmp_path):
    """Covers: runtime.py:154-157 — add_agent on session without draft handler."""
    # Directly construct an LoomSession with _draft_handler=None.
    wiring = ParticipantWiring(
        id="alice",
        proxy=SendProxyAdapter(_DummyAgent("hi")),
    )
    session = build_loom_session([wiring], policy=OpenChatPolicy())
    session._draft_handler = None  # simulate the no-handler condition
    with pytest.raises(RuntimeError, match="without a draft handler"):
        session.add_agent(
            ParticipantWiring(
                id="bob",
                proxy=SendProxyAdapter(_DummyAgent("hi")),
            )
        )
    session.stop()


# ---------------------------------------------------------------------------
# Slash commands: branches not exercised by happy-path tests
# ---------------------------------------------------------------------------


class _DummyAgent:
    """Minimal proxy that returns a fixed reply."""

    def __init__(self, reply: str = "hi"):
        self._reply = reply

    def send(self, prompt):
        return self._reply


def _new_session(tmp_path: Path = None) -> LoomSession:
    wirings = [
        ParticipantWiring(
            id="alice",
            proxy=SendProxyAdapter(_DummyAgent()),
        ),
        ParticipantWiring(
            id="bob",
            proxy=SendProxyAdapter(_DummyAgent()),
        ),
    ]
    return build_loom_session(
        wirings,
        default_responder_id="alice",
        policy=OpenChatPolicy(),
        journal_dir=tmp_path,
    )


def test_slash_unknown_command_returns_help(tmp_path):
    """Covers: runtime.py:603-609 — unknown /xyz returns help text."""
    session = _new_session()
    try:
        r = handle_slash_command("/notarealcommand", session)
        assert r.handled is True
        assert "unknown command" in r.message
    finally:
        session.stop()


def test_slash_non_slash_input_unhandled():
    """Covers: runtime.py:358-360 — non-/ text → handled=False."""
    session = _new_session()
    try:
        r = handle_slash_command("hello world", session)
        assert r.handled is False
    finally:
        session.stop()


def test_slash_who_with_roles(tmp_path):
    """Covers: runtime.py — /who output with roles set.

    v0.2: ``floor_owner`` was removed; ``/who`` no longer renders it.
    """
    session = _new_session()
    try:
        session.coordinator.set_roles({"alice": "writer"})
        r = handle_slash_command("/who", session)
        assert "members:" in r.message
        assert "alice=writer" in r.message
    finally:
        session.stop()


def test_slash_mode_returns_removed_notice():
    """Covers: runtime.py:384-390 — /mode returns the v0 removal notice."""
    session = _new_session()
    try:
        r = handle_slash_command("/mode council", session)
        assert "removed in Loom v0" in r.message
    finally:
        session.stop()


def test_slash_topic_with_no_args_clears():
    """Covers: runtime.py:392-397 — /topic with no args clears."""
    session = _new_session()
    try:
        session.coordinator.set_topic("first")
        r = handle_slash_command("/topic", session)
        assert "(cleared)" in r.message
        assert session.state.topic is None
    finally:
        session.stop()


def test_slash_add_returns_unsupported_message():
    """Covers: runtime.py:399-409 — /add via slash returns unsupported notice."""
    session = _new_session()
    try:
        r = handle_slash_command("/add charlie", session)
        assert "not supported" in r.message
    finally:
        session.stop()


def test_slash_remove_with_no_args_returns_usage():
    """Covers: runtime.py:411-413 — /remove with no args."""
    session = _new_session()
    try:
        r = handle_slash_command("/remove", session)
        assert "usage:" in r.message
    finally:
        session.stop()


def test_slash_remove_unknown_id_returns_not_in_room():
    """Covers: runtime.py:414-416 — /remove <unknown>."""
    session = _new_session()
    try:
        r = handle_slash_command("/remove charlie", session)
        assert "charlie not in room" in r.message
    finally:
        session.stop()


def test_slash_remove_succeeds_with_valid_id():
    """Covers: runtime.py:417-421 — /remove happy path."""
    session = _new_session()
    try:
        r = handle_slash_command("/remove alice", session)
        assert "removed alice" in r.message
        assert "alice" not in session.state.participants
    finally:
        session.stop()


def test_slash_cancel_closes_open_user_turn():
    """Covers: runtime.py:423-426 — /cancel closes the active turn."""
    session = _new_session()
    try:
        post_user_text(session, "hello @alice")
        r = handle_slash_command("/cancel", session)
        assert "cancelled" in r.message
    finally:
        session.stop()


def test_slash_dm_with_no_body_returns_usage():
    """Covers: runtime.py:428-432 — /dm with missing body."""
    session = _new_session()
    try:
        r = handle_slash_command("/dm alice", session)
        assert "usage" in r.message
    finally:
        session.stop()


def test_slash_dm_unknown_target():
    """Covers: runtime.py:433-436 — /dm <unknown> <body>."""
    session = _new_session()
    try:
        r = handle_slash_command("/dm charlie hi", session)
        assert "unknown participant" in r.message
    finally:
        session.stop()


def test_slash_dm_happy_path_posts_to_target():
    """Covers: runtime.py:437-449 — /dm happy path."""
    session = _new_session()
    try:
        r = handle_slash_command("/dm alice some body text", session)
        assert "DM → alice" in r.message
    finally:
        session.stop()


def test_slash_summary_no_summary_yet():
    """Covers: runtime.py:451-455 — /summary when none exists."""
    session = _new_session()
    try:
        r = handle_slash_command("/summary", session)
        assert "no summary yet" in r.message
    finally:
        session.stop()


def test_slash_anchor_no_args_shows_current():
    """Covers: runtime.py:458-461 — /anchor query."""
    session = _new_session()
    try:
        r = handle_slash_command("/anchor", session)
        assert "anchor:" in r.message
    finally:
        session.stop()


def test_slash_anchor_unknown_id():
    """Covers: runtime.py:462-464 — /anchor <unknown>."""
    session = _new_session()
    try:
        r = handle_slash_command("/anchor nobody", session)
        assert "unknown participant" in r.message
    finally:
        session.stop()


def test_slash_anchor_happy_path():
    """Covers: runtime.py:465-466 — /anchor <known>."""
    session = _new_session()
    try:
        r = handle_slash_command("/anchor alice", session)
        assert "anchor → alice" in r.message
    finally:
        session.stop()


def test_slash_responder_no_args_shows_current():
    """Covers: runtime.py:468-473 — /responder query."""
    session = _new_session()
    try:
        r = handle_slash_command("/responder", session)
        assert "default responder:" in r.message
    finally:
        session.stop()


def test_slash_responder_unknown_id():
    """Covers: runtime.py:474-476 — /responder <unknown>."""
    session = _new_session()
    try:
        r = handle_slash_command("/responder nobody", session)
        assert "unknown participant" in r.message
    finally:
        session.stop()


def test_slash_responder_happy_path():
    """Covers: runtime.py:477-479 — /responder <known>."""
    session = _new_session()
    try:
        r = handle_slash_command("/responder bob", session)
        assert "default_responder → bob" in r.message
    finally:
        session.stop()


def test_slash_roles_no_args_no_roles_set():
    """Covers: runtime.py:489-491 — /roles when none set."""
    session = _new_session()
    try:
        r = handle_slash_command("/roles", session)
        assert "(no roles set)" in r.message
    finally:
        session.stop()


def test_slash_roles_no_args_displays_existing():
    """Covers: runtime.py:492-494 — /roles displays existing."""
    session = _new_session()
    try:
        session.coordinator.set_roles({"alice": "writer"})
        r = handle_slash_command("/roles", session)
        assert "alice=writer" in r.message
    finally:
        session.stop()


def test_slash_roles_assignment_with_unknown_pid():
    """Covers: runtime.py:495-501 — /roles with unknown pid."""
    session = _new_session()
    try:
        r = handle_slash_command("/roles ghost=writer", session)
        assert "bad tokens" in r.message or "usage:" in r.message
    finally:
        session.stop()


def test_slash_roles_assignment_with_bad_token_no_equals():
    """Covers: runtime.py:622-625 — bad token (no =)."""
    session = _new_session()
    try:
        r = handle_slash_command("/roles bareword", session)
        assert "bad tokens" in r.message or "usage:" in r.message
    finally:
        session.stop()


def test_slash_roles_assignment_with_empty_pid_or_role():
    """Covers: runtime.py:629-631 — empty pid or role token."""
    session = _new_session()
    try:
        r = handle_slash_command("/roles =writer", session)
        assert "bad tokens" in r.message or "usage:" in r.message
    finally:
        session.stop()


def test_slash_roles_clear_via_empty_dict():
    """Covers: runtime.py:502-503 — /roles clears when set returns empty."""
    session = _new_session()
    try:
        # First set a role.
        session.coordinator.set_roles({"alice": "writer"})
        # Apply empty (no parseable tokens) — covered by non-empty input
        # that resolves to {} (we instead clear via direct API for this
        # branch; the slash path with bad tokens hits the error branch).
        session.coordinator.set_roles({})
        r = handle_slash_command("/roles", session)
        assert "(no roles set)" in r.message
    finally:
        session.stop()


def test_slash_roles_assignment_happy_path():
    """Covers: runtime.py:504-506 — /roles success message."""
    session = _new_session()
    try:
        r = handle_slash_command("/roles alice=writer", session)
        assert "alice=writer" in r.message
    finally:
        session.stop()


def test_slash_floor_returns_removed_notice():
    """v0.2: /floor, /release, /quiet were removed with the floor_owner field."""
    session = _new_session()
    try:
        r = handle_slash_command("/floor alice", session)
        assert "removed in v0.2" in r.message
    finally:
        session.stop()


def test_slash_release_returns_removed_notice():
    session = _new_session()
    try:
        r = handle_slash_command("/release", session)
        assert "removed in v0.2" in r.message
    finally:
        session.stop()


def test_slash_quiet_returns_removed_notice():
    session = _new_session()
    try:
        r = handle_slash_command("/quiet alice", session)
        assert "removed in v0.2" in r.message
    finally:
        session.stop()


def test_slash_goal_no_args_shows_current():
    """Covers: runtime.py — /goal query (now an alias for /topic)."""
    session = _new_session()
    try:
        r = handle_slash_command("/goal", session)
        assert "topic:" in r.message
    finally:
        session.stop()


def test_slash_goal_set():
    """Covers: runtime.py:571-572 — /goal <text>."""
    session = _new_session()
    try:
        r = handle_slash_command("/goal find a trick", session)
        assert "find a trick" in r.message
    finally:
        session.stop()


def test_slash_brief_normal_detailed():
    """Covers: runtime.py:574-584 — /brief, /normal, /detailed."""
    session = _new_session()
    try:
        for cmd in ("brief", "normal", "detailed"):
            r = handle_slash_command(f"/{cmd}", session)
            assert cmd in r.message
    finally:
        session.stop()


def test_slash_control_dump_with_roles():
    """Covers: runtime.py:586-601 — /control with roles set."""
    session = _new_session()
    try:
        session.coordinator.set_roles({"alice": "writer"})
        r = handle_slash_command("/control", session)
        assert "alice=writer" in r.message
    finally:
        session.stop()


def test_slash_control_dump_no_roles():
    """Covers: runtime.py:599-600 — /control roles: (none) branch."""
    session = _new_session()
    try:
        r = handle_slash_command("/control", session)
        assert "roles: (none)" in r.message
    finally:
        session.stop()


# ---------------------------------------------------------------------------
# Format-control branches
# ---------------------------------------------------------------------------


def test_format_control_dead_letter():
    """Covers: runtime.py:686-688 — dead_letter formatting."""
    e = ev.dead_letter(original_mention_event_id=5, reason="participant_removed", reroute_to="bob")
    out = _format_control(e)
    assert "dead-lettered" in out
    assert "bob" in out


def test_format_control_anchor_chair_summarizer_changed():
    """Covers: runtime.py:692-696 — anchor/chair/summarizer changed."""
    for ct in ("anchor_changed", "chair_changed", "default_summarizer_changed"):
        e = ev._control(ct, old_id="alice", new_id="bob")
        msg = _format_control(e)
        assert msg is not None
        assert "alice" in msg
        assert "bob" in msg


def test_format_control_participant_added_removed():
    """Covers: runtime.py:697-700 — participant_added/removed formatting."""
    e_add = ev.participant_added("zelda")
    e_rem = ev.participant_removed("zelda")
    assert "+ zelda" in _format_control(e_add)
    assert "- zelda" in _format_control(e_rem)


def test_format_control_user_turn_closed_completed_returns_none():
    """Covers: runtime.py:702-704 — user_turn_closed completed → None (silent)."""
    e = ev.user_turn_closed(user_turn_id=1, reason="completed")
    assert _format_control(e) is None


def test_format_control_user_turn_closed_no_responder():
    """Covers: runtime.py:705-706 — user_turn_closed no_responder."""
    e = ev.user_turn_closed(user_turn_id=1, reason="no_responder")
    assert "no agent responded" in _format_control(e)


def test_format_control_user_turn_closed_obligation_unresolved():
    """Covers: runtime.py:707-708 — user_turn_closed obligation_unresolved."""
    e = ev.user_turn_closed(user_turn_id=1, reason="obligation_unresolved")
    assert "did not reply" in _format_control(e)


def test_format_control_user_turn_closed_other_reason():
    """Covers: runtime.py:709 — user_turn_closed for other reasons."""
    e = ev.user_turn_closed(user_turn_id=1, reason="topic_changed")
    assert "topic_changed" in _format_control(e)


def test_format_control_internal_kinds_silent():
    """Covers: runtime.py:710-714 — user_turn_opened / obligation_* are silent."""
    e = ev.user_turn_opened(
        user_turn_id=1, routing_case="direct_mention", required_participants=["a"], rationale="x"
    )
    assert _format_control(e) is None
    assert _format_control(ev.obligation_recorded(1, "a", "must", [0], "x")) is None
    assert _format_control(ev.obligation_resolved(1, "a", resolved_by_event_id=2)) is None


def test_format_control_unknown_kind_silent():
    """Covers: runtime.py:715 — registered control_type without format mapping → None.

    ``roles_assigned`` and ``floor_updated`` are valid control types but
    are not rendered by the console (they're internal accounting-style
    events). _format_control falls through to the trailing ``return None``.
    """
    assert _format_control(ev.roles_assigned({"alice": "writer"})) is None
    assert _format_control(ev.floor_updated(wait_for_user=True)) is None
    assert _format_control(ev.style_changed(old="brief", new="normal")) is None


def test_format_control_non_dict_body_returns_none():
    """Covers: runtime.py:681-682 — non-dict body → None."""
    e = Event(
        kind="control",
        body="just a string",
        sender="kernel",
        channel="main",
        addressees=[],
        room_epoch=0,
    )
    e.id = 0
    e.ts = 0.0
    assert _format_control(e) is None


# ---------------------------------------------------------------------------
# _make_console_subscriber
# ---------------------------------------------------------------------------


def test_console_subscriber_user_dm_message_emits_dm_line():
    """Covers: runtime.py:723-728 — user DM channel emits dm prefix."""
    captured: List[str] = []
    sub = _make_console_subscriber(captured.append)
    e = ev.chat(sender="user", body="hi", addressees=["alice"], channel="dm:alice")
    e.id = 0
    e.ts = 0.0
    sub(e)
    assert any("dm → alice" in line for line in captured)


def test_console_subscriber_agent_to_agent_dm_silent():
    """Covers: runtime.py:729-730 — agent DM stays silent."""
    captured: List[str] = []
    sub = _make_console_subscriber(captured.append)
    e = ev.chat(sender="bob", body="hey", addressees=["alice"], channel="dm:alice")
    e.id = 0
    e.ts = 0.0
    sub(e)
    assert captured == []


def test_console_subscriber_agent_main_channel_emits():
    """Covers: runtime.py:731-732 — agent message on main channel emits."""
    captured: List[str] = []
    sub = _make_console_subscriber(captured.append)
    e = ev.chat(sender="bob", body="hello", addressees=[])
    e.id = 0
    e.ts = 0.0
    sub(e)
    assert any("bob ▸ hello" in line for line in captured)


def test_console_subscriber_drops_user_main_channel():
    """Covers: runtime.py:723-728 — user's main-channel post is silent (echo)."""
    captured: List[str] = []
    sub = _make_console_subscriber(captured.append)
    e = ev.chat(sender="user", body="self", addressees=[])
    e.id = 0
    e.ts = 0.0
    sub(e)
    assert captured == []


def test_console_subscriber_stream_events_silent():
    """Covers: runtime.py:738-739 — stream events drop silently in v0."""
    captured: List[str] = []
    sub = _make_console_subscriber(captured.append)
    e = ev.stream_start(lease_id=1, participant_id="alice", trigger_event_id=0)
    sub(e)
    assert captured == []


# ---------------------------------------------------------------------------
# run_loom_console — end-to-end via injected prompt_fn
# ---------------------------------------------------------------------------


class _ScriptedPrompt:
    """Iterator-like prompt_fn driver with EOFError sentinel."""

    def __init__(self, lines: list[str]):
        self._iter = iter(lines)

    def __call__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise EOFError("scripted input exhausted")


def test_run_loom_console_eof_clean_exit():
    """Covers: runtime.py:790-794, 806-807 — EOFError ends the loop."""
    captured: List[str] = []
    wirings = [
        ParticipantWiring(
            id="alice",
            proxy=SendProxyAdapter(_DummyAgent()),
        ),
    ]
    run_loom_console(
        wirings,
        prompt_fn=_ScriptedPrompt([]),
        notify=captured.append,
        policy=OpenChatPolicy(),
    )
    # Loop exited cleanly without a prompt_fn invocation succeeding.


def test_run_loom_console_keyboard_interrupt_clean_exit():
    """Covers: runtime.py:790-794 — KeyboardInterrupt ends the loop."""
    captured: List[str] = []

    def kb_interrupting():
        raise KeyboardInterrupt()

    wirings = [
        ParticipantWiring(
            id="alice",
            proxy=SendProxyAdapter(_DummyAgent()),
        ),
    ]
    run_loom_console(
        wirings,
        prompt_fn=kb_interrupting,
        notify=captured.append,
        policy=OpenChatPolicy(),
    )


def test_run_loom_console_blank_lines_skipped():
    """Covers: runtime.py:795-797 — blank input is dropped."""
    captured: List[str] = []
    wirings = [
        ParticipantWiring(
            id="alice",
            proxy=SendProxyAdapter(_DummyAgent()),
        ),
    ]
    run_loom_console(
        wirings,
        prompt_fn=_ScriptedPrompt(["", "  "]),
        notify=captured.append,
        policy=OpenChatPolicy(),
    )


def test_run_loom_console_quit_command_breaks_loop():
    """Covers: runtime.py:798-803 — /quit ends the loop and prints message."""
    captured: List[str] = []
    wirings = [
        ParticipantWiring(
            id="alice",
            proxy=SendProxyAdapter(_DummyAgent()),
        ),
    ]
    run_loom_console(
        wirings,
        prompt_fn=_ScriptedPrompt(["/quit"]),
        notify=captured.append,
        policy=OpenChatPolicy(),
    )
    assert any("leaving session" in line for line in captured)


def test_run_loom_console_user_text_post_path():
    """Covers: runtime.py:804-805 — non-/ text is forwarded as user input."""
    captured: List[str] = []
    wirings = [
        ParticipantWiring(
            id="alice",
            proxy=SendProxyAdapter(_DummyAgent("hello back")),
        ),
    ]
    run_loom_console(
        wirings,
        prompt_fn=_ScriptedPrompt(["hello @alice", "/quit"]),
        notify=captured.append,
        policy=OpenChatPolicy(),
    )


def test_run_loom_console_default_notify_is_print(capsys):
    """Covers: runtime.py:773-774 — default notify is print()."""
    wirings = [
        ParticipantWiring(
            id="alice",
            proxy=SendProxyAdapter(_DummyAgent()),
        ),
    ]
    run_loom_console(
        wirings,
        prompt_fn=_ScriptedPrompt(["/who", "/quit"]),
        # notify left at default
        policy=OpenChatPolicy(),
    )
    captured = capsys.readouterr()
    assert "members:" in captured.out


# ---------------------------------------------------------------------------
# post_user_text edge case
# ---------------------------------------------------------------------------


def test_post_user_text_returns_event(tmp_path):
    """Covers: post_user_text returns the posted event (sanity)."""
    session = _new_session()
    try:
        e = post_user_text(session, "hi @alice")
        assert e.kind == "chat"
        assert "alice" in e.addressees
    finally:
        session.stop()
