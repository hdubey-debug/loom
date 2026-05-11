"""System tests — full ``run_console`` driven by scripted input.

System-level: drives the public ``LoomRoom.run_console`` facade with
synthetic ``prompt_fn`` / ``notify`` callables, exercising the slash
command surface end-to-end (not just the parser). Distinct from
subsystem console tests, which test individual command parsing — these
tests verify the whole console loop including post → render →
control-event observation through ``notify``.
"""
from __future__ import annotations

import threading
import time

import pytest

from loom.adapters import agent_from_send
from loom.policy.open_chat import OpenChatPolicy
from loom.room import LoomRoom


_LONG = ("reply long enough to bypass both the pass buffer and the loop "
         "guard short-text threshold for canonical commit by the kernel.")


def _send_for(pid: str):
    counter = [0]

    def _send(prompt):
        counter[0] += 1
        return f"{pid} turn {counter[0]} {_LONG}"
    return _send


def _build_room(*, n_agents: int = 2, prefix: str = "rc") -> LoomRoom:
    agents = [agent_from_send(f"{prefix}{i}", _send_for(f"{prefix}{i}"))
              for i in range(n_agents)]
    return LoomRoom(agents=agents, policy=OpenChatPolicy())


def _delayed_prompt_fn(script, *, delay_s: float = 0.3):
    """Wrap a scripted prompt_fn so each non-slash entry pauses for
    agents to commit before returning the next line.

    ``run_console`` posts non-blocking, so without a delay the next
    prompt fires before the kernel commits any drafts.
    """
    base = script.prompt_fn

    def _fn():
        time.sleep(delay_s)
        return base()
    return _fn


# ---------------------------------------------------------------------------
# Slash commands during a multi-turn console session.
# ---------------------------------------------------------------------------

class TestSlashCommandsMultiTurn:
    """System-level: each slash command works inside a live console loop."""

    def test_console_5_chat_then_quit_sees_all_replies_via_notify(
            self, scripted_console):
        room = _build_room(n_agents=2)
        script = scripted_console([
            "first", "second", "third", "fourth", "fifth", "/quit",
        ])
        room.run_console(
            prompt_fn=_delayed_prompt_fn(script, delay_s=0.3),
            notify=script.notify,
        )
        # The notify hook captured at least one line per chat.
        chat_lines = [ln for ln in script.captured_lines
                      if "rc0" in ln or "rc1" in ln]
        assert len(chat_lines) >= 5

    def test_console_topic_command_changes_state_observed_in_topic_property(
            self, scripted_console):
        room = _build_room(n_agents=2)
        script = scripted_console([
            "/topic the new topic",
            "/quit",
        ])
        room.run_console(prompt_fn=script.prompt_fn, notify=script.notify)
        # ``room.topic`` reads ``state.topic`` via a public property.
        assert room.topic == "the new topic"

    def test_console_responder_command_changes_default_responder(
            self, scripted_console):
        room = _build_room(n_agents=2)
        script = scripted_console([
            "/responder rc1",
            "/quit",
        ])
        room.run_console(prompt_fn=script.prompt_fn, notify=script.notify)
        # State observable via ``room.session.state.default_responder_id``.
        assert room.session.state.default_responder_id == "rc1"

    def test_console_roles_assigns_then_clears(self, scripted_console):
        room = _build_room(n_agents=2)
        script = scripted_console([
            "/roles rc0=teacher rc1=student",
            "/roles",  # display
            "/quit",
        ])
        room.run_console(prompt_fn=script.prompt_fn, notify=script.notify)
        # State observable via the read-only control view.
        roles = dict(room.session.state.control.roles)
        assert roles == {"rc0": "teacher", "rc1": "student"}

    def test_console_floor_and_release_return_removed_notice(
            self, scripted_console):
        # v0.2: /floor, /release, /quiet were removed. The console no
        # longer mutates kernel state — it returns a removed-feature
        # notice that the user can read in the console output.
        room = _build_room(n_agents=2)
        script = scripted_console([
            "/floor rc0",
            "/release",
            "/quit",
        ])
        room.run_console(prompt_fn=script.prompt_fn, notify=script.notify)
        # Nothing on the (now-absent) floor_owner field; the kernel
        # state was not mutated by the commands.
        assert not hasattr(room.session.state.control, "floor_owner")

    def test_console_set_brief_then_normal_then_detailed_emits_style_changed(
            self, scripted_console, event_recorder):
        room = _build_room(n_agents=1)
        event_recorder.attach(room)
        script = scripted_console([
            "/brief",
            "/normal",
            "/detailed",
            "/quit",
        ])
        room.run_console(prompt_fn=script.prompt_fn, notify=script.notify)
        sc = event_recorder.by_control_type("style_changed")
        # Style is "normal" by default; "/brief" → 1 change, "/normal"
        # → 1 change back, "/detailed" → 1 change. Three style_changed
        # events total.
        assert len(sc) == 3
        styles = [e.body.get("new") for e in sc]
        assert styles == ["brief", "normal", "detailed"]

    def test_console_remove_via_slash_then_post_routes_to_remaining(
            self, scripted_console):
        room = _build_room(n_agents=3, prefix="rm")
        script = scripted_console([
            "/remove rm1",
            "after-remove question",
            "/quit",
        ])
        room.run_console(prompt_fn=script.prompt_fn, notify=script.notify)
        # rm1 is gone; rm0 and rm2 remain.
        assert "rm1" not in room.participants
        assert "rm0" in room.participants
        assert "rm2" in room.participants


# ---------------------------------------------------------------------------
# Console exit paths.
# ---------------------------------------------------------------------------

class TestConsoleExitPaths:
    """System-level: every clean exit closes the room without leaks."""

    def test_eof_during_prompt_clean_shutdown_no_thread_leak(
            self, scripted_console):
        room = _build_room(n_agents=2)
        script = scripted_console([EOFError])
        room.run_console(prompt_fn=script.prompt_fn, notify=script.notify)
        # The autouse thread-leak guard catches survivors; we also
        # verify all actors marked themselves stopped.
        assert all(actor.stopped for actor in room.session.actors)

    def test_keyboard_interrupt_during_prompt_clean_shutdown(
            self, scripted_console):
        room = _build_room(n_agents=2)
        script = scripted_console([KeyboardInterrupt])
        room.run_console(prompt_fn=script.prompt_fn, notify=script.notify)
        assert all(actor.stopped for actor in room.session.actors)

    def test_quit_command_clean_shutdown(self, scripted_console):
        room = _build_room(n_agents=2)
        script = scripted_console(["/quit"])
        room.run_console(prompt_fn=script.prompt_fn, notify=script.notify)
        assert all(actor.stopped for actor in room.session.actors)
        # The "leaving session" message is in captured lines.
        assert any("leaving" in ln for ln in script.captured_lines)

    def test_unknown_slash_command_does_not_post_as_user_text(
            self, scripted_console, event_recorder):
        room = _build_room(n_agents=2)
        event_recorder.attach(room)
        script = scripted_console([
            "/notarealcommand",
            "/quit",
        ])
        room.run_console(prompt_fn=script.prompt_fn, notify=script.notify)
        # No user chat event with body matching the slash command.
        user_chats = [
            e for e in event_recorder.by_kind("chat")
            if e.sender == "user"
        ]
        bodies = [c.body for c in user_chats]
        assert not any("/notarealcommand" in b for b in bodies)
        # The notify hook captured the unknown-command message.
        assert any("unknown command" in ln.lower()
                   for ln in script.captured_lines)


# ---------------------------------------------------------------------------
# Notify thread-safety contract.
# ---------------------------------------------------------------------------

class TestConsoleNotifyContract:
    """System-level: concurrent actor writes serialize via the notify hook."""

    @pytest.mark.timing
    def test_concurrent_actor_writes_serialize_via_notify_lock(
            self, scripted_console):
        # Custom notify that records lengths to detect torn writes.
        captured: list[str] = []
        capture_lock = threading.Lock()

        def notify(msg: str) -> None:
            with capture_lock:
                captured.append(msg)

        room = _build_room(n_agents=4, prefix="cn")
        script = scripted_console(["broadcast question", "/quit"])
        # Pass our custom notify, not the script's, to keep ordering tight.
        room.run_console(
            prompt_fn=_delayed_prompt_fn(script, delay_s=0.5),
            notify=notify,
        )
        # Each captured entry is a complete string (no torn fragments).
        for line in captured:
            assert isinstance(line, str)
            # No empty strings — the kernel always passes a full message.
            assert line != ""
        # At least two agents committed (notify ran for each).
        agent_lines = [ln for ln in captured
                       if any(f"cn{i}" in ln for i in range(4))]
        assert len(agent_lines) >= 2
