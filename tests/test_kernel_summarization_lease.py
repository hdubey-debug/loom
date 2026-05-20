"""Tests for v0.3.x PR 5 — SUMMARIZATION lease + Path A/B + backoff.

Doctrine: P22, §3, §6, §7
(`docs/internal/study/14-context-compaction-doctrine.md`).
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.capabilities import CapabilityGrant, CapabilityName
from loom.kernel.context import ContextScope, SummaryRecord
from loom.kernel.coordinator import RoomCoordinator, SchedulingResult
from loom.kernel.effects import (
    DefaultSummarizerSetEffect,
)
from loom.kernel.leases import (
    LeaseKind,
    SummarizationContext,
)
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState
from loom.slash_commands import dispatch_slash_command


def _coord_with_summarizer(
    n_chats: int = 12,
    threshold: int = 3,
    grant_summarize: bool = True,
    grant_emit_summary: bool = True,
) -> RoomCoordinator:
    bus = MessageBus()
    state = RoomState(
        config=RoomConfig(
            summarizer_max_consecutive_failures=threshold,
        )
    )
    state.add_participant(ParticipantInfo(id="loom"))
    state.add_participant(ParticipantInfo(id="claude_code"))
    coord = RoomCoordinator(bus, state)
    # Install loom as the default summariser so the slot check passes.
    coord.set_default_summarizer("loom")
    # Grant the necessary capabilities (kernel grants from sender
    # ``"user"`` bypass anti-escalation per P15).
    cap_state = coord.kernel_state.capabilities
    if cap_state is not None:
        if grant_emit_summary:
            cap_state.add_grant(
                CapabilityGrant(
                    grant_id="g_emit",
                    grantor_id="user",
                    grantee_id="loom",
                    capability=CapabilityName.EMIT_SUMMARY.value,
                    granted_at=0.0,
                )
            )
        if grant_summarize:
            cap_state.add_grant(
                CapabilityGrant(
                    grant_id="g_sum",
                    grantor_id="user",
                    grantee_id="claude_code",
                    capability=CapabilityName.SUMMARIZE.value,
                    granted_at=0.0,
                )
            )
    for i in range(n_chats):
        bus.post(ev.chat(sender="loom", body=f"m{i}"))
    return coord


class LeaseKindAndContext(unittest.TestCase):
    def test_summarization_is_sixth_lease_kind(self):
        self.assertEqual(LeaseKind.SUMMARIZATION.value, "summarization")

    def test_summarization_context_fields(self):
        ctx = SummarizationContext(
            scope=ContextScope(room_id="main"),
            covers_event_range=(0, 9),
            triggered_by="policy",
            triggering_event_id=42,
            required_capability=CapabilityName.EMIT_SUMMARY.value,
            thread_id="main",
        )
        self.assertEqual(ctx.triggered_by, "policy")
        self.assertEqual(ctx.thread_id, "main")


class CapabilityEnumExtensions(unittest.TestCase):
    def test_summarize_and_emit_summary_present(self):
        self.assertIn("SUMMARIZE", [c.name for c in CapabilityName])
        self.assertIn("EMIT_SUMMARY", [c.name for c in CapabilityName])

    def test_meta_verbs_present(self):
        names = [c.name for c in CapabilityName]
        self.assertIn("GRANT_CAPABILITY_SUMMARIZE", names)
        self.assertIn("GRANT_CAPABILITY_EMIT_SUMMARY", names)
        self.assertIn("REVOKE_CAPABILITY_SUMMARIZE", names)
        self.assertIn("REVOKE_CAPABILITY_EMIT_SUMMARY", names)


class SchedulePathA(unittest.TestCase):
    def test_path_a_acquires_lease_and_emits_audit(self):
        coord = _coord_with_summarizer(20)
        scope = ContextScope(room_id="main")
        before = len(coord.bus.snapshot())
        result = coord.schedule_summarization(scope, trigger_pressure_ratio=0.85)
        self.assertTrue(result.scheduled, msg=f"reason={result.denial_reason!r}")
        self.assertEqual(result.summarizer_id, "loom")
        emitted = [ev.control_type_of(e) for e in coord.bus.snapshot()[before:]]
        self.assertIn("summarization_scheduled", emitted)

    def test_path_a_rejects_without_default_summarizer(self):
        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="loom"))
        coord = RoomCoordinator(bus, state)
        result = coord.schedule_summarization(ContextScope(room_id="main"))
        self.assertFalse(result.scheduled)
        self.assertEqual(result.denial_reason, "no_default_summarizer")

    def test_path_a_rejects_when_scope_disabled(self):
        coord = _coord_with_summarizer(20)
        scope = ContextScope(room_id="main")
        # Manually mark scope disabled.
        coord.kernel_state.context.disabled_scopes.add(("loom", scope.as_tuple()))
        result = coord.schedule_summarization(scope)
        self.assertFalse(result.scheduled)
        self.assertEqual(result.denial_reason, "scope_disabled")

    def test_path_a_denied_when_holder_lacks_emit_summary(self):
        coord = _coord_with_summarizer(20, grant_emit_summary=False)
        result = coord.schedule_summarization(ContextScope(room_id="main"))
        self.assertFalse(result.scheduled)
        self.assertEqual(result.denial_reason, "lease_denied")


class RequestPathB(unittest.TestCase):
    def test_path_b_for_non_slot_holder_with_summarize_cap(self):
        coord = _coord_with_summarizer(20)
        scope = ContextScope(room_id="main")
        # claude_code is NOT the default summariser, but has SUMMARIZE.
        # Path B does not enforce the slot check.
        result = coord.request_summarization("claude_code", scope)
        self.assertTrue(result.scheduled, msg=f"reason={result.denial_reason!r}")
        self.assertEqual(result.summarizer_id, "claude_code")

    def test_path_b_denied_without_summarize_cap(self):
        coord = _coord_with_summarizer(20, grant_summarize=False)
        result = coord.request_summarization("claude_code", ContextScope(room_id="main"))
        self.assertFalse(result.scheduled)

    def test_path_b_user_bypasses_capability_check(self):
        # P15: holder == "user" skips _CapabilityCheck.
        coord = _coord_with_summarizer(20, grant_summarize=False)
        result = coord.request_summarization("user", ContextScope(room_id="main"))
        # P15 capability bypass applies; participant_registered may
        # still reject because "user" isn't a participant — that's
        # the next gate to clear in v0.4. For now we accept either
        # outcome but assert it's NOT a capability denial.
        if not result.scheduled:
            self.assertNotIn("capability", (result.denial_reason or ""))


class BackoffAndDisablement(unittest.TestCase):
    def _bad_record(self, summary_id: str = "s_bad") -> SummaryRecord:
        # Lineage-gap → always-rejecting structural failure.
        return SummaryRecord(
            summary_id=summary_id,
            scope=ContextScope(room_id="main"),
            covers_event_range=(0, 9),
            text="bad",
            input_event_ranges=((0, 3), (5, 9)),  # gap → reject
            summarizer_id="loom",
        )

    def test_threshold_failures_emit_compaction_disabled(self):
        coord = _coord_with_summarizer(20, threshold=2)
        before = len(coord.bus.snapshot())
        for i in range(2):
            coord.submit_summary_proposed(self._bad_record(f"s{i}"))
        emitted = [ev.control_type_of(e) for e in coord.bus.snapshot()[before:]]
        self.assertIn("compaction_disabled", emitted)
        scope = ContextScope(room_id="main")
        self.assertIn(
            ("loom", scope.as_tuple()),
            coord.kernel_state.context.disabled_scopes,
        )

    def test_anchor_conflict_does_not_count(self):
        coord = _coord_with_summarizer(20, threshold=2)
        rec1 = SummaryRecord(
            summary_id="s_ok",
            scope=ContextScope(room_id="main"),
            covers_event_range=(0, 9),
            text="first",
            input_event_ranges=((0, 9),),
            summarizer_id="loom",
        )
        self.assertTrue(coord.submit_summary_proposed(rec1).committed)
        # Now produce anchor-conflicting records; they should not count.
        rec_conflict = SummaryRecord(
            summary_id="s_conflict",
            scope=ContextScope(room_id="main"),
            covers_event_range=(0, 9),
            text="conflict",
            input_event_ranges=((0, 9),),
            summarizer_id="loom",
        )
        for i in range(5):
            coord.submit_summary_proposed(rec_conflict)
        scope = ContextScope(room_id="main")
        self.assertNotIn(
            ("loom", scope.as_tuple()),
            coord.kernel_state.context.disabled_scopes,
        )

    def test_successful_commit_resets_failure_count(self):
        coord = _coord_with_summarizer(20, threshold=10)
        coord.submit_summary_proposed(self._bad_record("s_bad_1"))
        scope = ContextScope(room_id="main")
        key = ("loom", scope.as_tuple())
        self.assertEqual(coord.kernel_state.context.failure_count[key], 1)
        good = SummaryRecord(
            summary_id="s_good",
            scope=scope,
            covers_event_range=(0, 9),
            text="good",
            input_event_ranges=((0, 9),),
            summarizer_id="loom",
        )
        self.assertTrue(coord.submit_summary_proposed(good).committed)
        self.assertEqual(coord.kernel_state.context.failure_count.get(key, 0), 0)

    def test_default_summarizer_change_clears_backoff_state(self):
        coord = _coord_with_summarizer(20, threshold=10)
        scope = ContextScope(room_id="main")
        # Pre-populate failure_count + disabled_scopes for the OLD slot.
        coord.kernel_state.context.failure_count[("loom", scope.as_tuple())] = 5
        coord.kernel_state.context.disabled_scopes.add(("loom", scope.as_tuple()))
        # Change the default summariser via the reducer; the state
        # mutator clears entries for "loom".
        with coord._lock:
            coord._apply_effect(DefaultSummarizerSetEffect(participant_id="claude_code"))
        self.assertNotIn(
            ("loom", scope.as_tuple()),
            coord.kernel_state.context.failure_count,
        )
        self.assertNotIn(
            ("loom", scope.as_tuple()),
            coord.kernel_state.context.disabled_scopes,
        )


class SlashSummarize(unittest.TestCase):
    def test_slash_summarize_routes_to_request_summarization(self):
        coord = _coord_with_summarizer(20)
        result = dispatch_slash_command(coord, "/summarize")
        self.assertIsInstance(result, SchedulingResult)
        # "user" requester bypasses capability; participant_registered
        # may still gate.
        if result.scheduled:
            self.assertIsNotNone(result.lease_id)

    def test_slash_summarize_parses_thread_param(self):
        from loom.slash_commands import parse_slash_command

        parsed = parse_slash_command("/summarize thread=debate-1")
        self.assertEqual(parsed.action_name, "SUMMARIZE")
        self.assertEqual(parsed.params.get("thread_id"), "debate-1")


if __name__ == "__main__":
    unittest.main()
