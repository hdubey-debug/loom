"""Tests for :mod:`loom.adapters` and the :class:`loom.contracts.Agent` Protocol.

Each helper produces an :class:`Agent`-shaped object that:
- has the right ``id`` and metadata fields,
- yields chunks from :meth:`stream`,
- can be drop-in for :class:`loom.kernel.streaming.StreamingProxy`.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from loom.adapters import (
    _extract_text,
    _FunctionAgent,
    agent_from_object,
    agent_from_send,
    agent_from_stream,
)
from loom.contracts import Agent


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------

class ExtractText(unittest.TestCase):

    def test_str_passthrough(self):
        self.assertEqual(_extract_text("hello"), "hello")

    def test_none_yields_empty(self):
        self.assertEqual(_extract_text(None), "")

    def test_object_with_text_attr(self):
        obj = SimpleNamespace(text="ahoy")
        self.assertEqual(_extract_text(obj), "ahoy")

    def test_object_with_body_attr(self):
        self.assertEqual(_extract_text(SimpleNamespace(body="b")), "b")

    def test_object_with_content_attr(self):
        self.assertEqual(_extract_text(SimpleNamespace(content="c")), "c")

    def test_object_with_output_attr(self):
        self.assertEqual(_extract_text(SimpleNamespace(output="o")), "o")

    def test_falls_back_to_str(self):
        # No matching attr → str(result).
        class X:
            def __str__(self) -> str:
                return "fallback"
        self.assertEqual(_extract_text(X()), "fallback")

    def test_text_wins_over_body(self):
        obj = SimpleNamespace(text="t", body="b")
        self.assertEqual(_extract_text(obj), "t")


# ---------------------------------------------------------------------------
# agent_from_send
# ---------------------------------------------------------------------------

class AgentFromSend(unittest.TestCase):

    def test_basic_string_response(self):
        a = agent_from_send("gpt", lambda p: "hello world")
        chunks = list(a.stream("ping"))
        self.assertEqual(chunks, ["hello world"])
        self.assertEqual(a.id, "gpt")

    def test_default_metadata(self):
        a = agent_from_send("gpt", lambda p: "x")
        self.assertEqual(a.persona, "")
        self.assertEqual(a.capability_block, "")
        self.assertEqual(a.cost_tier, 1)
        self.assertTrue(a.capable)

    def test_custom_metadata(self):
        a = agent_from_send(
            "gpt", lambda p: "x",
            persona="GPT-4 helper",
            capability_block="text + tool use",
            cost_tier=2,
            capable=True,
        )
        self.assertEqual(a.persona, "GPT-4 helper")
        self.assertEqual(a.capability_block, "text + tool use")
        self.assertEqual(a.cost_tier, 2)

    def test_object_response_with_text_attr(self):
        a = agent_from_send("x", lambda p: SimpleNamespace(text="hi"))
        self.assertEqual(list(a.stream("p")), ["hi"])

    def test_empty_string_yields_nothing(self):
        a = agent_from_send("x", lambda p: "")
        self.assertEqual(list(a.stream("p")), [])

    def test_none_yields_nothing(self):
        a = agent_from_send("x", lambda p: None)
        self.assertEqual(list(a.stream("p")), [])

    def test_send_called_with_prompt(self):
        seen: list[object] = []

        def send(p):
            seen.append(p)
            return "ok"

        a = agent_from_send("x", send)
        list(a.stream("the-prompt"))
        self.assertEqual(seen, ["the-prompt"])

    def test_rejects_non_callable(self):
        with self.assertRaises(TypeError):
            agent_from_send("x", "not callable")  # type: ignore[arg-type]

    def test_rejects_empty_id(self):
        with self.assertRaises(ValueError):
            agent_from_send("", lambda p: "x")

    def test_cancel_stops_iteration(self):
        a = agent_from_send("x", lambda p: "hello")
        a.cancel()
        self.assertEqual(list(a.stream("p")), [])

    def test_cancel_forwards_to_user_cancel(self):
        called: list[bool] = []

        def cancel():
            called.append(True)

        a = agent_from_send("x", lambda p: "x", cancel_fn=cancel)
        a.cancel()
        self.assertEqual(called, [True])

    def test_cancel_swallows_user_exception(self):
        def cancel():
            raise RuntimeError("boom")
        a = agent_from_send("x", lambda p: "x", cancel_fn=cancel)
        # Must not propagate.
        a.cancel()


# ---------------------------------------------------------------------------
# agent_from_stream
# ---------------------------------------------------------------------------

class AgentFromStream(unittest.TestCase):

    def test_passes_chunks_through(self):
        a = agent_from_stream("x", lambda p: ["a", "b", "c"])
        self.assertEqual(list(a.stream("p")), ["a", "b", "c"])

    def test_drops_empty_chunks(self):
        a = agent_from_stream("x", lambda p: ["a", "", "b"])
        self.assertEqual(list(a.stream("p")), ["a", "b"])

    def test_drops_none_chunks(self):
        a = agent_from_stream("x", lambda p: ["a", None, "b"])
        self.assertEqual(list(a.stream("p")), ["a", "b"])

    def test_coerces_non_string_chunks(self):
        a = agent_from_stream("x", lambda p: [1, 2])
        self.assertEqual(list(a.stream("p")), ["1", "2"])

    def test_generator_callable(self):
        def gen(prompt):
            yield "hello "
            yield prompt  # echo

        a = agent_from_stream("x", gen)
        self.assertEqual(list(a.stream("you")), ["hello ", "you"])

    def test_re_callable(self):
        # The same agent must produce a fresh stream on each call.
        a = agent_from_stream("x", lambda p: ["a", "b"])
        self.assertEqual(list(a.stream("1")), ["a", "b"])
        self.assertEqual(list(a.stream("2")), ["a", "b"])

    def test_cancel_short_circuits_after_first_chunk(self):
        emitted: list[str] = []

        def stream(p):
            for c in "abcd":
                emitted.append(c)
                yield c

        a = agent_from_stream("x", stream)
        out: list[str] = []
        for chunk in a.stream("p"):
            out.append(chunk)
            a.cancel()  # cancel after first chunk
            # The agent should stop on the next loop iteration.
        self.assertEqual(out, ["a"])

    def test_rejects_non_callable(self):
        with self.assertRaises(TypeError):
            agent_from_stream("x", ["not callable"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# agent_from_object
# ---------------------------------------------------------------------------

class AgentFromObject(unittest.TestCase):

    def test_prefers_stream_over_send(self):
        class Both:
            id = "ignored"

            def stream(self, p):
                yield "from-stream"

            def send(self, p):
                return "from-send"

        a = agent_from_object("x", Both())
        self.assertEqual(list(a.stream("p")), ["from-stream"])

    def test_falls_back_to_send(self):
        class OnlySend:
            def send(self, p):
                return "ok"

        a = agent_from_object("x", OnlySend())
        self.assertEqual(list(a.stream("p")), ["ok"])

    def test_picks_up_metadata_from_object(self):
        obj = SimpleNamespace(
            stream=lambda p: ["x"],
            persona="resident",
            capability_block="caps",
            cost_tier=5,
            capable=False,
        )
        a = agent_from_object("x", obj)
        self.assertEqual(a.persona, "resident")
        self.assertEqual(a.capability_block, "caps")
        self.assertEqual(a.cost_tier, 5)
        self.assertFalse(a.capable)

    def test_kwargs_override_object_metadata(self):
        obj = SimpleNamespace(
            stream=lambda p: ["x"],
            persona="resident",
            cost_tier=5,
        )
        a = agent_from_object(
            "x", obj, persona="override", cost_tier=2)
        self.assertEqual(a.persona, "override")
        self.assertEqual(a.cost_tier, 2)

    def test_metadata_defaults_when_absent(self):
        # Bare object with only .send and no metadata.
        class Bare:
            def send(self, p):
                return "x"
        a = agent_from_object("x", Bare())
        self.assertEqual(a.persona, "")
        self.assertEqual(a.capability_block, "")
        self.assertEqual(a.cost_tier, 1)
        self.assertTrue(a.capable)

    def test_neither_stream_nor_send_raises(self):
        class Bad:
            pass
        with self.assertRaises(TypeError):
            agent_from_object("x", Bad())

    def test_forwards_cancel(self):
        called: list[bool] = []

        class Obj:
            def send(self, p):
                return "x"

            def cancel(self):
                called.append(True)

        a = agent_from_object("x", Obj())
        a.cancel()
        self.assertEqual(called, [True])


# ---------------------------------------------------------------------------
# Agent Protocol satisfaction
# ---------------------------------------------------------------------------

class ProtocolSatisfaction(unittest.TestCase):
    """Adapter outputs duck-type as :class:`Agent`."""

    def test_send_agent_satisfies_protocol(self):
        a = agent_from_send("x", lambda p: "ok")
        self.assertIsInstance(a, Agent)

    def test_stream_agent_satisfies_protocol(self):
        a = agent_from_stream("x", lambda p: ["a"])
        self.assertIsInstance(a, Agent)

    def test_object_agent_satisfies_protocol(self):
        class Obj:
            def send(self, p):
                return "ok"

        a = agent_from_object("x", Obj())
        self.assertIsInstance(a, Agent)

    def test_arbitrary_duck_typed_object_passes(self):
        class Custom:
            id = "custom"

            def stream(self, p):
                yield "x"

        # No adapter — runtime_checkable still recognizes the shape.
        self.assertIsInstance(Custom(), Agent)

    def test_object_missing_stream_does_not_satisfy(self):
        class NoStream:
            id = "no"

        self.assertNotIsInstance(NoStream(), Agent)


# ---------------------------------------------------------------------------
# Slot conservation
# ---------------------------------------------------------------------------

class FunctionAgentSlots(unittest.TestCase):
    """`_FunctionAgent` uses ``__slots__`` to keep instances small."""

    def test_no_dict(self):
        a = agent_from_send("x", lambda p: "x")
        self.assertFalse(hasattr(a, "__dict__"))

    def test_cannot_set_arbitrary_attr(self):
        a = agent_from_send("x", lambda p: "x")
        with self.assertRaises(AttributeError):
            a.unrelated = 7  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Scenario tests — drive each adapter end-to-end through the kernel's
# streaming pipeline (run_streaming_call) rather than just inspecting
# attributes.
# ---------------------------------------------------------------------------

class AdapterScenarios(unittest.TestCase):
    """Drive every adapter through the kernel streaming layer.

    The attribute-precedence checks above prove the wrapping is correct;
    these tests prove the wrapped agent actually flows through
    ``run_streaming_call`` like a real proxy: chunks are emitted, errors
    surface as ``status="error"``, ``cancel`` re-entrance is safe, etc.
    """

    @staticmethod
    def _run_through_streaming(agent):
        """Run ``agent`` through ``run_streaming_call`` once and return
        the final stream-end status plus any committed text."""
        from loom.kernel import events as ev
        from loom.kernel.bus import MessageBus
        from loom.kernel.coordinator import RoomCoordinator
        from loom.kernel.obligations import plan_for_default
        from loom.kernel.room import (
            ParticipantInfo, RoomConfig, RoomState,
        )
        from loom.kernel.streaming import run_streaming_call

        bus = MessageBus()
        state = RoomState(config=RoomConfig(
            pass_buffer_chars=16, lease_ttl_s=60,
        ))
        state.add_participant(ParticipantInfo(id=agent.id))
        coord = RoomCoordinator(bus, state)
        user_event = ev.chat(sender="user", body="hi")
        bus.post(user_event)
        plan = plan_for_default(agent.id, reason="adapter-scenario",
                                target_event_ids=[user_event.id])
        coord.open_user_turn(user_event, plan)
        lease = coord.acquire_lease(agent.id, user_event.id)
        committed = run_streaming_call(agent, "<prompt>", lease, bus, coord)
        end = next(
            e for e in reversed(bus.snapshot())
            if e.kind == "stream"
            and e.body.get("stream_event") == "end"
        )
        return end.body["status"], committed, bus

    def test_send_adapter_round_trip_drives_streaming(self):
        agent = agent_from_send(
            "loom",
            lambda prompt: "a sufficiently long reply for canonical commit.",
        )
        status, text, _bus = self._run_through_streaming(agent)
        self.assertEqual(status, "committed")
        self.assertIn("sufficiently long reply", text)

    def test_send_adapter_error_propagates_as_stream_error(self):
        def boom(_p):
            raise RuntimeError("provider explosion")

        agent = agent_from_send("loom", boom)
        status, _text, bus = self._run_through_streaming(agent)
        self.assertEqual(status, "error")
        sevs = [e for e in bus.snapshot() if e.kind == "stream"]
        self.assertIn("provider explosion", sevs[-1].body.get("error", ""))

    def test_object_adapter_metadata_defaults_partial_object(self):
        # Object exposing only ``id`` + ``send``; metadata via getattr
        # falls through to the documented defaults.
        class Bare:
            id = "x"

            def send(self, prompt):
                return "ok"

        agent = agent_from_object("x", Bare())
        self.assertEqual(agent.persona, "")
        self.assertEqual(agent.capability_block, "")
        self.assertEqual(agent.cost_tier, 1)
        self.assertTrue(agent.capable)

    def test_object_adapter_stream_wins_over_send(self):
        # Both methods present — adapter must select ``stream``.
        class Both:
            id = "x"
            stream_called = False
            send_called = False

            def stream(self, prompt):
                self.stream_called = True
                yield "from-stream-method"

            def send(self, prompt):
                self.send_called = True
                return "from-send-method"

        obj = Both()
        agent = agent_from_object("x", obj)
        list(agent.stream("hi"))
        self.assertTrue(obj.stream_called)
        self.assertFalse(obj.send_called)

    def test_cancel_is_reentrant(self):
        # A cancelled agent's ``stream`` must yield nothing on a fresh
        # invocation, and calling cancel twice is harmless.
        agent = agent_from_stream("loom", lambda p: ["one", "two"])
        agent.cancel()
        agent.cancel()  # idempotent
        chunks = list(agent.stream("p"))
        self.assertEqual(chunks, [])

    def test_stream_adapter_filters_none_yields(self):
        # The stream adapter explicitly skips ``None`` chunks while
        # passing strings through. End-to-end the canonical chat carries
        # only the string content.
        def gen(_p):
            yield "first chunk that is plenty long enough so far "
            yield None
            yield "second chunk extends it further"

        agent = agent_from_stream("loom", gen)
        status, text, _bus = self._run_through_streaming(agent)
        self.assertEqual(status, "committed")
        self.assertIn("first chunk", text)
        self.assertIn("second chunk", text)

    def test_send_adapter_with_object_response_uses_extract_text(self):
        # send returning a non-string object — _extract_text pulls the
        # ``.text`` attribute through the streaming pipeline.
        class Reply:
            text = "the long enough reply attached as .text on the result obj"

        agent = agent_from_send("loom", lambda p: Reply())
        status, text, _bus = self._run_through_streaming(agent)
        self.assertEqual(status, "committed")
        self.assertIn("attached as .text", text)

    def test_object_adapter_rejects_neither_stream_nor_send(self):
        class Bare:
            id = "x"

        with self.assertRaises(TypeError):
            agent_from_object("x", Bare())


if __name__ == "__main__":
    unittest.main()
