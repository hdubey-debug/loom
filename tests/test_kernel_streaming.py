"""Tests for ``loom.kernel.streaming`` — PASS prefix + stream events."""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.obligations import plan_for_default
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
)
from loom.kernel.streaming import (
    PASS_RE,
    parse_addressees,
    run_streaming_call,
)


class FakeProxy:
    def __init__(self, chunks, *, raises_at=None):
        self.chunks = list(chunks)
        self.raises_at = raises_at
        self.cancelled = False

    def stream(self, prompt):
        for i, chunk in enumerate(self.chunks):
            if self.cancelled:
                return
            if self.raises_at is not None and i == self.raises_at:
                raise RuntimeError("network blip")
            yield chunk

    def cancel(self):
        self.cancelled = True


def _setup(*, default_responder="loom"):
    bus = MessageBus()
    state = RoomState(
        config=RoomConfig(
            pass_buffer_chars=16,
            lease_ttl_s=60,
        )
    )
    for i, pid in enumerate(("loom", "claude_code", "gemini_cli")):
        state.add_participant(ParticipantInfo(id=pid, cost_tier=i))
    if default_responder:
        state.set_default_responder(default_responder)
    coord = RoomCoordinator(bus, state)
    user_event = ev.chat(sender="user", body="hi")
    bus.post(user_event)
    plan = plan_for_default(default_responder, reason="fallback", target_event_ids=[user_event.id])
    coord.open_user_turn(user_event, plan)
    lease = coord.acquire_lease(default_responder, user_event.id)
    assert lease is not None
    return bus, state, coord, lease


def _stream_events(bus):
    return [e for e in bus.snapshot() if e.kind == "stream"]


def _chat_events(bus):
    return [e for e in bus.snapshot() if e.kind == "chat" and e.sender != "user"]


class PassRegex(unittest.TestCase):
    def test_matches_at_start(self):
        self.assertIsNotNone(PASS_RE.match("[PASS]"))
        self.assertIsNotNone(PASS_RE.match("[PASS] "))
        self.assertIsNotNone(PASS_RE.match("[PASS]\n"))
        self.assertIsNotNone(PASS_RE.match("  [PASS]"))
        self.assertIsNotNone(PASS_RE.match("\n\n[PASS]"))

    def test_does_not_match_mid_text(self):
        self.assertIsNone(PASS_RE.match("Hello [PASS]"))
        self.assertIsNone(PASS_RE.match("[PASSED]"))
        self.assertIsNone(PASS_RE.match("[PASSING]"))
        self.assertIsNone(PASS_RE.match("[passing]"))

    def test_partial_buffer_does_not_match(self):
        self.assertIsNone(PASS_RE.match("[PA"))
        self.assertIsNone(PASS_RE.match("[PASS"))


class CommittedHappyPath(unittest.TestCase):
    def test_full_response_committed(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["This ", "is a ", "sufficient ", "long reply."])
        committed = run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        self.assertEqual(committed, "This is a sufficient long reply.")

        # stream_start, then deltas, then stream_end.
        sevs = _stream_events(bus)
        self.assertEqual(sevs[0].body["stream_event"], "start")
        self.assertEqual(sevs[-1].body["stream_event"], "end")
        self.assertEqual(sevs[-1].body["status"], "committed")

        # Canonical chat event posted.
        chats = _chat_events(bus)
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0].sender, "loom")
        self.assertEqual(chats[0].body, "This is a sufficient long reply.")

    def test_short_response_under_buffer_committed(self):
        bus, state, coord, lease = _setup()
        # 8 chars total — never reaches 16-char flush threshold but is
        # not PASS, so we commit at end-of-stream.
        proxy = FakeProxy(["Yes.", " OK."])
        committed = run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        self.assertEqual(committed, "Yes. OK.")
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "committed")
        # One delta posted (the buffered final-buffer flush).
        deltas = [e for e in sevs if e.body["stream_event"] == "delta"]
        self.assertEqual(len(deltas), 1)


class PassSuppression(unittest.TestCase):
    def test_pass_prefix_emits_passed_status(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["[PASS]"])
        committed = run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        self.assertEqual(committed, "")
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "passed")
        # No deltas, no chat event.
        deltas = [e for e in sevs if e.body["stream_event"] == "delta"]
        self.assertEqual(deltas, [])
        self.assertEqual(_chat_events(bus), [])
        self.assertTrue(proxy.cancelled)

    def test_pass_with_leading_whitespace_emits_passed(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["\n\n[PASS]"])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "passed")

    def test_pass_split_across_chunks_still_caught(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["[PA", "SS]"])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "passed")

    def test_pass_after_visible_does_not_suppress(self):
        bus, state, coord, lease = _setup()
        # 16+ chars pre-PASS — buffer flushes, message commits as is.
        proxy = FakeProxy(["here is a real reply ", "[PASS]"])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "committed")


class PostStreamFilter(unittest.TestCase):
    def test_idle_phrase_suppressed(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["standing", " by"])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "suppressed")

    def test_empty_response_suppressed(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy([])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "suppressed")

    def test_duplicate_suppressed_via_loop_guard(self):
        bus, state, coord, lease = _setup()
        # Prime loop guard with a prior reply from loom.
        coord.loop_guard.record("loom", "ack received")
        proxy = FakeProxy(["ack received"])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "suppressed")


class LeaseInvalidationMidStream(unittest.TestCase):
    def test_default_responder_change_invalidates(self):
        bus, state, coord, lease = _setup()
        # Flip the default responder mid-stream — that bumps room_epoch
        # and invalidates outstanding leases.
        flipped = [False]

        def chunks():
            yield "first chunk that is plenty long enough so far "
            if not flipped[0]:
                coord.set_default_responder("claude_code")
                flipped[0] = True
            yield "second chunk"

        class GenProxy:
            def stream(self, prompt):
                yield from chunks()

            def cancel(self):
                pass

        run_streaming_call(GenProxy(), "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "lease_expired")


class ProviderError(unittest.TestCase):
    def test_proxy_exception_emits_error(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["partial reply that is long enough", "more"], raises_at=1)
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "error")
        self.assertIn("network blip", sevs[-1].body.get("error", ""))


class AgentExceptionPropagation(unittest.TestCase):
    """Cover the error paths the streaming layer handles for hostile agents.

    ``ProviderError`` above already verifies a generator that raises mid-
    iteration. These tests cover the harder edges:

    - exception before the first chunk is yielded;
    - a send-shaped agent (no ``stream``) whose ``send`` raises;
    - a generator that yields ``None`` instead of strings.
    """

    def test_stream_raises_before_first_chunk_emits_error(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["irrelevant"], raises_at=0)
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "error")
        self.assertIn("network blip", sevs[-1].body.get("error", ""))
        # Nothing was committed.
        self.assertEqual(_chat_events(bus), [])

    def test_stream_raises_immediately_no_partial_chat(self):
        # An exception on the first iteration must not produce any
        # canonical chat event — partial state should not leak.
        bus, state, coord, lease = _setup()

        class ImmediateRaise:
            def stream(self, prompt):
                raise RuntimeError("boom on entry")
                yield  # unreachable, makes this a generator function

            def cancel(self):
                pass

        run_streaming_call(ImmediateRaise(), "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "error")
        self.assertEqual(_chat_events(bus), [])

    def test_send_proxy_adapter_propagates_send_exception(self):
        # ``SendProxyAdapter`` wraps a non-streaming callable; when send
        # raises, the streaming layer must surface it as ``status=error``.
        from loom.runtime import SendProxyAdapter

        bus, state, coord, lease = _setup()

        class SendRaiser:
            id = "loom"

            def send(self, prompt):
                raise RuntimeError("send blew up")

        proxy = SendProxyAdapter(SendRaiser(), send_method="send")
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "error")
        self.assertIn("send blew up", sevs[-1].body.get("error", ""))

    def test_generator_yielding_none_does_not_crash(self):
        # An agent that produces ``None`` chunks must not crash the
        # streaming proxy — it either tolerates them or surfaces error
        # status. Either way, no partial canonical chat should leak.
        bus, state, coord, lease = _setup()

        class NoneYielder:
            def stream(self, prompt):
                yield None
                yield "real content goes here at some sufficient length"

            def cancel(self):
                pass

        run_streaming_call(NoneYielder(), "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        last = sevs[-1].body["status"]
        self.assertIn(last, ("committed", "suppressed", "error"))
        # Whichever terminal status was produced, the stream loop did
        # not propagate an unhandled exception out of run_streaming_call.


class AddresseeParser(unittest.TestCase):
    def test_extracts_known_ids(self):
        out = parse_addressees(
            "@claude_code agree, @gemini_cli disagree",
            addressable=["claude_code", "gemini_cli"],
        )
        self.assertEqual(out, ["claude_code", "gemini_cli"])

    def test_filters_unknown(self):
        out = parse_addressees("@nobody hi", ["claude_code"])
        self.assertEqual(out, [])

    def test_excludes_self(self):
        out = parse_addressees(
            "@claude_code self-ref", ["claude_code", "gemini_cli"], exclude="claude_code"
        )
        self.assertEqual(out, [])

    def test_dedup_preserves_first_occurrence(self):
        out = parse_addressees("@a hi @a again", ["a"])
        self.assertEqual(out, ["a"])


class CanonicalChatEvent(unittest.TestCase):
    def test_committed_chat_event_carries_addressees(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["good point @gemini_cli, but ", "what about latency?"])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        chats = _chat_events(bus)
        self.assertEqual(len(chats), 1)
        self.assertIn("gemini_cli", chats[0].addressees)

    def test_committed_chat_event_carries_lease_metadata(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["sufficient length reply here"])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        chats = _chat_events(bus)
        self.assertEqual(chats[0].meta["lease_id"], lease.id)
        self.assertGreater(chats[0].meta["cost_tokens"], 0)

    def test_chat_posted_before_terminal_stream_end(self):
        # Subscribers that switch on stream_end(committed) must see the
        # canonical chat event already on the bus when stream_end arrives.
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["a sufficiently long reply for canonical commit"])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        log = bus.snapshot()
        chat_idx = next(
            i for i, e in enumerate(log) if e.kind == "chat" and e.sender == lease.holder
        )
        end_idx = next(
            i
            for i, e in enumerate(log)
            if e.kind == "stream" and e.body.get("stream_event") == "end"
        )
        self.assertLess(chat_idx, end_idx)

    def test_stream_end_committed_carries_committed_event_id(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["a sufficiently long reply for canonical commit"])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        chats = _chat_events(bus)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "committed")
        self.assertEqual(
            sevs[-1].body.get("committed_event_id"),
            chats[0].id,
        )

    def test_stream_end_suppressed_omits_committed_event_id(self):
        # PASS / filter-suppressed terminal events do not carry the field.
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["[PASS]"])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertNotIn("committed_event_id", sevs[-1].body)


class ChairSpeakStrip(unittest.TestCase):
    """Defense-in-depth against agents hallucinating legacy chair-speak."""

    def test_chair_speak_only_floor_grant_suppressed(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["@gemini_cli you have the floor"])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "suppressed")
        self.assertEqual(_chat_events(bus), [])

    def test_chair_speak_only_raised_hand_suppressed(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["(claude_code raised hand: off-by-one in swap)"])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        sevs = _stream_events(bus)
        self.assertEqual(sevs[-1].body["status"], "suppressed")
        self.assertEqual(_chat_events(bus), [])

    def test_chair_speak_line_stripped_useful_content_kept(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(
            [
                "@gemini_cli you have the floor\n",
                "The bug is the off-by-one in n - i; use n - 1 - i.",
            ]
        )
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        chats = _chat_events(bus)
        self.assertEqual(len(chats), 1)
        self.assertNotIn("you have the floor", chats[0].body)
        self.assertIn("off-by-one", chats[0].body)

    def test_raised_hand_line_stripped_useful_content_kept(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(
            [
                "(claude_code raised hand: off-by-one bug)\n",
                "The swap index is wrong; use n - 1 - i.",
            ]
        )
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        chats = _chat_events(bus)
        self.assertEqual(len(chats), 1)
        self.assertNotIn("raised hand", chats[0].body)
        self.assertIn("swap index is wrong", chats[0].body)

    def test_floor_is_yours_phrase_stripped(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(
            [
                "the floor is yours, friend.\n",
                "Real content goes here for the room.",
            ]
        )
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        chats = _chat_events(bus)
        self.assertEqual(len(chats), 1)
        self.assertNotIn("floor is yours", chats[0].body)
        self.assertIn("Real content", chats[0].body)

    def test_clean_message_with_no_chair_speak_unchanged(self):
        bus, state, coord, lease = _setup()
        proxy = FakeProxy(["This is a perfectly clean reply with no issues."])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        chats = _chat_events(bus)
        self.assertEqual(len(chats), 1)
        self.assertEqual(
            chats[0].body,
            "This is a perfectly clean reply with no issues.",
        )


class ShouldPostResponseHook(unittest.TestCase):
    """v0.2 ``ConversationPolicy.should_post_response`` veto hook."""

    def _stub_policy(self, allow_fn):
        from loom.contracts import ConversationPolicy
        from loom.kernel.obligations import plan_for_default

        class _Stub(ConversationPolicy):
            name = "stub"

            def plan_user_turn(self, user_event, state):
                return plan_for_default("loom", reason="fallback", target_event_ids=[user_event.id])

            def should_post_response(self, *, body, state, participant_id):
                return allow_fn(body, participant_id)

        return _Stub()

    def _setup_with_policy(self, policy):
        bus = MessageBus()
        state = RoomState(
            config=RoomConfig(
                pass_buffer_chars=16,
                lease_ttl_s=60,
            )
        )
        for i, pid in enumerate(("loom", "claude_code")):
            state.add_participant(ParticipantInfo(id=pid, cost_tier=i))
        state.set_default_responder("loom")
        coord = RoomCoordinator(bus, state, policy=policy)
        user_event = ev.chat(sender="user", body="hi")
        bus.post(user_event)
        from loom.kernel.obligations import plan_for_default as _pfd

        plan = _pfd("loom", reason="fallback", target_event_ids=[user_event.id])
        coord.open_user_turn(user_event, plan)
        lease = coord.acquire_lease("loom", user_event.id)
        return bus, coord, lease

    def test_default_returns_true_does_not_suppress(self):
        # Policy without overriding should_post_response → default True.
        policy = self._stub_policy(lambda body, pid: True)
        bus, coord, lease = self._setup_with_policy(policy)
        proxy = FakeProxy(["A long enough clean reply to commit."])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        chats = _chat_events(bus)
        self.assertEqual(len(chats), 1)

    def test_returning_false_suppresses_response(self):
        policy = self._stub_policy(lambda body, pid: False)
        bus, coord, lease = self._setup_with_policy(policy)
        proxy = FakeProxy(["A long enough clean reply that gets vetoed."])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        chats = _chat_events(bus)
        self.assertEqual(len(chats), 0)
        # stream_end status is suppressed.
        sevs = [e for e in bus.snapshot() if e.kind == "stream"]
        self.assertEqual(sevs[-1].body["status"], "suppressed")

    def test_buggy_hook_falls_through_to_commit(self):
        def _raises(_body, _pid):
            raise RuntimeError("boom")

        policy = self._stub_policy(_raises)
        bus, coord, lease = self._setup_with_policy(policy)
        proxy = FakeProxy(["A long enough clean reply to commit."])
        run_streaming_call(proxy, "<prompt>", lease, bus, coord)
        # Buggy hook is treated as ``allowed=True`` so the response commits.
        chats = _chat_events(bus)
        self.assertEqual(len(chats), 1)


if __name__ == "__main__":
    unittest.main()
