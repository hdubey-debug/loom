"""v0.3.x demo — context compaction (Path B / ``/summarize``).

Run::

    PYTHONPATH=. python examples/summarize_demo.py

What you'll see:

1. We post ~12 chat events into the bus.
2. ``/summarize`` (Path B) acquires a SUMMARIZATION lease for the
   ``user`` proposer and emits ``summarization_scheduled``.
3. We hand-roll a ``SummaryRecord`` covering events 0..11 and submit
   it via ``coordinator.submit_summary_proposed`` — the off-lock
   pre-validator runs, then the under-lock anchor check, then the
   summary is committed.
4. The room's ``ContextState.active_summary_by_scope`` now points at
   the summary id. A future ``build_prompt(...)`` would render this
   as the ``<<<PRIOR ROOM SUMMARY>>>`` block.
5. Bus events involved in the lifecycle are printed.

In a production room the SummaryRecord would be produced by an LLM
call inside an off-lock summariser actor; we hand-roll the record
here purely for documentation purposes.
"""

from __future__ import annotations

from loom import LoomRoom, OpenChatPolicy, agent_from_send
from loom.kernel import events as ev
from loom.kernel.context import ContextScope, SummaryRecord
from loom.kernel.room import ParticipantInfo
from loom.slash_commands import dispatch_slash_command


def loom_send(prompt: str) -> str:
    return "loom: noted."


def main() -> None:
    agent = agent_from_send("loom", loom_send, persona="summariser")
    room = LoomRoom(agents=[agent], policy=OpenChatPolicy(), topic="long debate")

    with room:
        coord = room._session.coordinator
        bus = room._session.bus

        # Register the "user" pseudo-participant so /summarize can
        # dispatch with proposer_id="user".
        coord.register_participant(ParticipantInfo(id="user", capable=False))

        # The summariser slot must be set before request_summarization.
        with coord._lock:
            coord.state.set_default_summarizer("loom")

        # 1. Seed the bus with 12 chat events.
        for i in range(12):
            bus.post(ev.chat(sender="loom", body=f"turn {i}: …"))

        bus_len = len(bus.snapshot())
        print(f"[1] bus has {bus_len} events; topic={room.topic!r}")

        # 2. Dispatch /summarize (Path B). The result is a
        # SchedulingResult, not a ControlActionResult — see the
        # comment in `dispatch_slash_command` for why.
        sched = dispatch_slash_command(coord, "/summarize")
        print(
            f"\n[2] /summarize: scheduled={sched.scheduled} "
            f"lease={sched.lease_id} summarizer={sched.summarizer_id} "
            f"denial={sched.denial_reason}"
        )

        # 3. Hand-roll the SummaryRecord and submit it. In a real
        # room the summariser actor would produce this after an LLM
        # call. We pick covers=(0, bus_len-1) so the validator's
        # range invariants are satisfied.
        scope = ContextScope(room_id=coord.config.room_id, thread_id="main")
        record = SummaryRecord(
            summary_id="sum-1",
            scope=scope,
            covers_event_range=(0, bus_len - 1),
            text="the room debated the design of recursion lessons.",
            retained_event_ids=(0, bus_len - 1),
            input_summary_ids=(),
            input_event_ranges=((0, bus_len - 1),),
            model_id="demo-model",
            prompt_hash="hash-demo",
            summarizer_id="loom",
            proposed_at_event_id=bus_len,
        )
        result = coord.submit_summary_proposed(record)
        print(f"\n[3] submit_summary_proposed: committed={result.committed} reason={result.reason}")

        # 4. Show the active summary.
        active = coord.kernel_state.context.active_summary_by_scope
        print("\n[4] active_summary_by_scope:")
        for s, sid in active.items():
            print(f"     {s.as_tuple()} → {sid}")

        # 5. Relevant bus events.
        print("\n[5] compaction-related bus events:")
        wanted = {
            "summarization_scheduled",
            "summary_proposed",
            "summary_committed",
            "summary_failed",
        }
        for e in bus.snapshot():
            ctype = e.body.get("control_type", "") if isinstance(e.body, dict) else ""
            if ctype in wanted:
                sid = e.body.get("summary_id", "")
                print(f"  [{e.id:>3}] {ctype:<26} summary={sid}")


if __name__ == "__main__":
    main()
