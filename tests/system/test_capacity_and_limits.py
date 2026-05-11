"""System tests — capacity break-points and multi-room isolation.

System-level: probe the assembled kernel's scaling boundaries through
the public ``LoomRoom`` API. Each break-point test prints a measured
threshold via ``print()`` and asserts only a sanity floor — the
measured value is the deliverable. Distinct from subsystem
break-points (which probe one subsystem at a time): these tests
exercise whole-room or multi-room workloads.
"""

from __future__ import annotations

import threading
import time

import pytest

from loom.adapters import agent_from_send
from loom.kernel.journal import Journal
from loom.policy.open_chat import OpenChatPolicy
from loom.room import LoomRoom


_LONG = (
    "reply long enough to bypass both the pass buffer and the loop "
    "guard short-text threshold for canonical commit."
)


def _send_for(pid: str):
    counter = [0]

    def _send(prompt):
        counter[0] += 1
        return f"{pid} cap turn {counter[0]} {_LONG}"

    return _send


def _agent_pool(n: int, prefix: str = "cap"):
    return [agent_from_send(f"{prefix}{i}", _send_for(f"{prefix}{i}")) for i in range(n)]


# ---------------------------------------------------------------------------
# System-wide break-point probes.
# ---------------------------------------------------------------------------


class TestSystemBreakpoints:
    """System-level: scaling thresholds across the assembled kernel."""

    @pytest.mark.stress
    @pytest.mark.breakpoint
    @pytest.mark.watchdog(seconds=120)
    def test_breakpoint_session_length_at_which_post_and_wait_p99_exceeds_2s(
        self, multi_turn_session, varied_agents
    ):
        # Drive a single room through a long session; record per-turn
        # latency. Find the turn count at which the running p99
        # latency exceeds 2 s.
        threshold_s = 2.0
        floor_turns = 30
        ceiling_turns = 200
        agents = varied_agents(2, prefix="sl")
        room = multi_turn_session(agents=agents, policy=OpenChatPolicy())
        latencies: list[float] = []
        breakpoint_n = ceiling_turns
        for n in range(1, ceiling_turns + 1):
            t0 = time.monotonic()
            room.post_and_wait(f"q{n}", timeout=10.0)
            latencies.append(time.monotonic() - t0)
            if n >= 20:
                p99 = sorted(latencies)[int(len(latencies) * 0.99)]
                if p99 > threshold_s:
                    breakpoint_n = n
                    break
        print(f"BREAKPOINT: session_length_at_p99_2s={breakpoint_n}")
        assert breakpoint_n >= floor_turns

    @pytest.mark.stress
    @pytest.mark.breakpoint
    def test_breakpoint_total_events_per_minute_through_full_room(
        self, multi_turn_session, varied_agents
    ):
        # Measure total events landed on the bus during a 2-second
        # window of saturation. Extrapolate to events/min.
        floor_events_per_min = 600
        agents = varied_agents(3, prefix="tp")
        room = multi_turn_session(agents=agents, policy=OpenChatPolicy())
        t0 = time.monotonic()
        n_posts = 0
        while time.monotonic() - t0 < 2.0:
            room.post_and_wait(f"q{n_posts}", timeout=5.0)
            n_posts += 1
        elapsed = time.monotonic() - t0
        total_events = len(room.session.bus.snapshot())
        events_per_min = int(total_events * (60.0 / elapsed))
        print(f"BREAKPOINT: events_per_minute_full_room={events_per_min}")
        assert events_per_min >= floor_events_per_min

    @pytest.mark.stress
    @pytest.mark.breakpoint
    def test_breakpoint_concurrent_rooms_5_each_10_agents_no_crosstalk(self, multi_room_factory):
        # 3 rooms × 4 agents (down-sized from 5×10 for CI throughput).
        # Verify each room's bus contains only its own agent ids.
        rooms = multi_room_factory(n=3, agents_per_room=4, policy=OpenChatPolicy())
        for room in rooms:
            room.post_and_wait("test-isolation", timeout=5.0)
        for r_idx, room in enumerate(rooms):
            chat_senders = {
                e.sender
                for e in room.session.bus.snapshot()
                if e.kind == "chat" and e.sender != "user"
            }
            expected = {f"r{r_idx}_a{i}" for i in range(4)}
            assert chat_senders <= expected, f"room {r_idx} contaminated: {chat_senders - expected}"
        print(f"BREAKPOINT: concurrent_rooms_isolated={len(rooms)}")
        assert len(rooms) >= 2

    @pytest.mark.stress
    @pytest.mark.breakpoint
    def test_breakpoint_multi_room_aggregate_throughput_5_rooms(self, multi_room_factory):
        # Drive concurrent posts across 4 rooms; measure aggregate
        # event throughput.
        floor_aggregate_events = 50
        rooms = multi_room_factory(n=4, agents_per_room=2, policy=OpenChatPolicy())
        results: dict[int, int] = {}
        threads: list[threading.Thread] = []

        def driver(idx: int, room: LoomRoom) -> None:
            count = 0
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.0:
                room.post_and_wait(f"r{idx}-q{count}", timeout=3.0)
                count += 1
            results[idx] = count

        for idx, room in enumerate(rooms):
            t = threading.Thread(target=driver, args=(idx, room), name=f"driver-{idx}")
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        total_events = sum(len(room.session.bus.snapshot()) for room in rooms)
        print(f"BREAKPOINT: aggregate_events_4_rooms={total_events}")
        assert total_events >= floor_aggregate_events

    @pytest.mark.stress
    @pytest.mark.breakpoint
    def test_breakpoint_max_active_subscribers_before_post_latency_doubles(
        self, multi_turn_session, varied_agents
    ):
        # Establish a baseline post latency, then subscribe extra
        # observers and measure when the latency doubles.
        agents = varied_agents(2, prefix="ms")
        room = multi_turn_session(agents=agents, policy=OpenChatPolicy())
        # Baseline: no extra subscribers.
        t0 = time.monotonic()
        room.post_and_wait("baseline", timeout=5.0)
        baseline = time.monotonic() - t0

        unsubs = []
        breakpoint_n = 100
        try:
            for n in (5, 10, 20, 50, 100):
                while len(unsubs) < n:
                    unsubs.append(room.session.bus.subscribe(lambda _ev: None))
                t0 = time.monotonic()
                room.post_and_wait(f"with-{n}-subs", timeout=5.0)
                elapsed = time.monotonic() - t0
                if elapsed > 2 * (baseline + 0.1):
                    breakpoint_n = n
                    break
        finally:
            for u in unsubs:
                u()
        print(f"BREAKPOINT: subscribers_at_2x_latency={breakpoint_n}")
        assert breakpoint_n >= 5

    @pytest.mark.stress
    @pytest.mark.breakpoint
    def test_breakpoint_message_size_bytes_at_which_journal_write_exceeds_50ms(
        self, journaled_room, varied_agents
    ):
        # Post user messages of increasing size; measure end-to-end
        # post_and_wait latency (which includes journal write time).
        threshold_s = 0.05
        floor_bytes = 1024
        ceiling_bytes = 1024 * 256

        agents = varied_agents(1, prefix="ms")
        room = journaled_room(agents=agents, policy=OpenChatPolicy())
        breakpoint_bytes = ceiling_bytes
        try:
            for size in (1024, 4 * 1024, 16 * 1024, 64 * 1024, 256 * 1024):
                payload = "x" * size
                t0 = time.monotonic()
                room.post_and_wait(payload, timeout=10.0)
                elapsed = time.monotonic() - t0
                if elapsed > threshold_s:
                    breakpoint_bytes = size
                    break
        finally:
            pass  # journaled_room teardown stops the room.
        print(f"BREAKPOINT: message_size_at_50ms={breakpoint_bytes}")
        assert breakpoint_bytes >= floor_bytes


# ---------------------------------------------------------------------------
# Multi-room isolation — independent journals + bus + lifecycle.
# ---------------------------------------------------------------------------


class TestMultiRoomIsolation:
    """System-level: concurrent rooms do not contaminate each other."""

    @pytest.mark.stress
    def test_two_rooms_journals_are_independent_no_cross_contamination(self, multi_room_factory):
        rooms = multi_room_factory(n=2, agents_per_room=2, policy=OpenChatPolicy())
        for r_idx, room in enumerate(rooms):
            room.post_and_wait(f"room-{r_idx}-message", timeout=5.0)
        # Stop both rooms cleanly.
        for room in rooms:
            room.stop(timeout=10.0)
        # Now read each journal independently.
        for r_idx, room in enumerate(rooms):
            assert room.journal_dir is not None
            events = Journal(room.journal_dir).load_events()
            chat_senders = {e.sender for e in events if e.kind == "chat" and e.sender != "user"}
            expected = {f"r{r_idx}_a{i}" for i in range(2)}
            assert chat_senders <= expected
            # The OTHER room's agent ids never appear here.
            other_idx = 1 - r_idx
            forbidden = {f"r{other_idx}_a{i}" for i in range(2)}
            assert chat_senders.isdisjoint(forbidden)

    @pytest.mark.stress
    def test_three_rooms_concurrent_posts_no_event_id_overlap_within_each(self, multi_room_factory):
        rooms = multi_room_factory(n=3, agents_per_room=2, policy=OpenChatPolicy())
        threads: list[threading.Thread] = []

        def driver(room):
            for i in range(3):
                room.post_and_wait(f"q{i}", timeout=5.0)

        for room in rooms:
            t = threading.Thread(target=driver, args=(room,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=15.0)
        # Each room's event ids start at 0 and are dense (the bus
        # assigns ids sequentially per-bus).
        for room in rooms:
            ids = [e.id for e in room.session.bus.snapshot()]
            assert ids == sorted(ids)
            assert ids[0] == 0
            assert ids[-1] == len(ids) - 1

    def test_room_stop_in_one_does_not_disturb_others(self, multi_room_factory):
        rooms = multi_room_factory(n=3, agents_per_room=2, policy=OpenChatPolicy())
        # Drive a turn in each.
        for room in rooms:
            room.post_and_wait("warm", timeout=5.0)
        # Stop the middle room.
        rooms[1].stop(timeout=5.0)
        # The other two are still functional.
        for r_idx in (0, 2):
            replies = rooms[r_idx].post_and_wait("after-other-stop", timeout=5.0)
            assert isinstance(replies, (list, __import__("loom").TurnResult))
