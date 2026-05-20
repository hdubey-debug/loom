"""Tests for ``loom.slash_commands`` — v0.3 PR 11 human root actions.

Doctrine: P15 (human root actions use the control-action path).
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState
from loom.slash_commands import (
    ParsedCommand,
    SlashCommandError,
    build_default_registry,
    dispatch_slash_command,
    is_slash_command,
    parse_slash_command,
)


def _coord() -> RoomCoordinator:
    bus = MessageBus()
    state = RoomState(config=RoomConfig())
    state.add_participant(ParticipantInfo(id="loom"))
    state.add_participant(ParticipantInfo(id="claude_code"))
    # User must be a registered participant so the universal
    # _ParticipantRegisteredCheck passes for proposer_id="user".
    state.add_participant(ParticipantInfo(id="user"))
    return RoomCoordinator(bus, state)


class Parser(unittest.TestCase):
    def test_is_slash_command(self):
        self.assertTrue(is_slash_command("/topic foo"))
        self.assertFalse(is_slash_command("hello"))
        self.assertFalse(is_slash_command(""))

    def test_topic_parse(self):
        p = parse_slash_command("/topic derivatives lesson")
        self.assertEqual(p.action_name, "SET_TOPIC")
        self.assertEqual(p.params, {"topic": "derivatives lesson"})

    def test_anchor_parse(self):
        p = parse_slash_command("/anchor loom")
        self.assertEqual(p.action_name, "SET_ANCHOR")
        self.assertEqual(p.params, {"participant_id": "loom"})

    def test_responder_parse(self):
        p = parse_slash_command("/responder claude_code")
        self.assertEqual(p.action_name, "SET_DEFAULT_RESPONDER")
        self.assertEqual(p.params, {"participant_id": "claude_code"})

    def test_floor_single_speaker(self):
        p = parse_slash_command("/floor loom")
        self.assertEqual(p.action_name, "GRANT_FLOOR")
        self.assertEqual(p.params, {"speakers": ["loom"]})

    def test_floor_multiple_speakers(self):
        p = parse_slash_command("/floor loom claude_code")
        self.assertEqual(p.params, {"speakers": ["loom", "claude_code"]})

    def test_grant_minimal(self):
        p = parse_slash_command("/grant claude_code SET_TOPIC")
        self.assertEqual(p.action_name, "GRANT_CAPABILITY")
        self.assertEqual(p.params["grantee_id"], "claude_code")
        self.assertEqual(p.params["capability"], "SET_TOPIC")
        self.assertIsNone(p.params["expires_at"])

    def test_grant_with_expires(self):
        p = parse_slash_command("/grant claude_code SET_TOPIC expires_in=120")
        self.assertEqual(p.params["expires_at"], 120.0)

    def test_revoke_parse(self):
        p = parse_slash_command("/revoke g1")
        self.assertEqual(p.action_name, "REVOKE_CAPABILITY")
        self.assertEqual(p.params, {"grant_id": "g1"})

    def test_policy_parse(self):
        p = parse_slash_command("/policy round_robin")
        self.assertEqual(p.action_name, "SWITCH_POLICY")
        self.assertEqual(p.params, {"policy_name": "round_robin"})

    def test_unknown_command_raises(self):
        with self.assertRaises(SlashCommandError):
            parse_slash_command("/somethingmadeup x")

    def test_malformed_arguments_raises(self):
        with self.assertRaises(SlashCommandError):
            parse_slash_command("/anchor")  # missing arg

    def test_non_slash_text_raises(self):
        with self.assertRaises(SlashCommandError):
            parse_slash_command("hello")


class RoutingThroughKernel(unittest.TestCase):
    """dispatch_slash_command produces the same control_action_applied
    envelope as an agent-issued action.
    """

    def test_topic_dispatch_user_bypasses_capability_check(self):
        coord = _coord()
        # No capability granted to "user" — slash command still works
        # because _CapabilityCheck allows proposer_id="user".
        result = dispatch_slash_command(coord, "/topic hello")
        self.assertTrue(result.granted)
        self.assertEqual(coord.state.topic, "hello")
        # Same control_action_applied shape as an agent dispatch.
        applied = [
            x for x in coord.bus.snapshot() if ev.control_type_of(x) == "control_action_applied"
        ]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].body["applier_id"], "user")
        self.assertEqual(applied[0].body["action_name"], "SET_TOPIC")

    def test_anchor_dispatch(self):
        coord = _coord()
        result = dispatch_slash_command(coord, "/anchor loom")
        self.assertTrue(result.granted)
        self.assertEqual(coord.state.anchor_id, "loom")

    def test_floor_dispatch(self):
        coord = _coord()
        result = dispatch_slash_command(coord, "/floor claude_code")
        self.assertTrue(result.granted)
        # FloorOverrideEffect appended an ADD/ONE_LEASE override.
        overrides = coord.state.control.active_overrides
        self.assertEqual(overrides[-1].speakers, ("claude_code",))

    def test_unknown_command_does_not_dispatch(self):
        coord = _coord()
        with self.assertRaises(SlashCommandError):
            dispatch_slash_command(coord, "/nothere")
        # No control_action events emitted.
        events = [
            x
            for x in coord.bus.snapshot()
            if ev.control_type_of(x) in ("control_action_proposed", "control_action_applied")
        ]
        self.assertEqual(events, [])

    def test_user_action_journaled_as_user(self):
        coord = _coord()
        dispatch_slash_command(coord, "/topic foo")
        proposed = [
            x for x in coord.bus.snapshot() if ev.control_type_of(x) == "control_action_proposed"
        ]
        self.assertEqual(proposed[0].body["proposer_id"], "user")


class RegistryExtension(unittest.TestCase):
    def test_custom_command_via_registry(self):
        reg = build_default_registry()

        def _parse_ping(args: str) -> ParsedCommand:
            return ParsedCommand(action_name="PING", params={"msg": args.strip()})

        reg.register("/ping", _parse_ping)
        p = parse_slash_command("/ping hello", registry=reg)
        self.assertEqual(p.action_name, "PING")
        self.assertEqual(p.params, {"msg": "hello"})

    def test_default_registry_commands(self):
        reg = build_default_registry()
        cmds = reg.commands()
        self.assertIn("/topic", cmds)
        self.assertIn("/grant", cmds)
        self.assertIn("/floor", cmds)


if __name__ == "__main__":
    unittest.main()
