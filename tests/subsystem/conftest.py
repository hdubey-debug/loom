"""Shared fixtures for ``tests/subsystem/``.

These fixtures support component-level and stress tests of the Loom
kernel + room. Everything is stdlib-only — no pytest-timeout / no
hypothesis / no pytest-xdist required.

Key fixtures:

- ``watchdog_timer`` (autouse) — caps every test at 45 s walltime and
  fails (does not skip) on overruns. Uses ``signal.alarm`` on Unix and
  falls back to a ``threading.Timer`` watchdog elsewhere.
- ``assert_no_thread_leak`` (autouse) — snapshots ``loom-actor-*``
  threads at start; teardown asserts none survived.
- ``thread_harness`` — spawns N threads with a captured exception queue
  and joins on teardown.
- ``temp_journal`` — ``tmp_path``-backed journal directory; teardown
  verifies every line of ``events.jsonl`` is well-formed JSON.
- ``InMemoryFaultJournal`` — a ``Journal`` subclass that fails the Nth
  write. Beats fragile filesystem chmod on shared filesystems.
- ``adversarial_agent`` — factory for hostile agent variants
  (HangAfterFirstDelta, SlowFirstDelta, InfiniteStream,
  GarbagePayload, YieldsNone, RaisesAfterChunks, FloodChunks).
- ``room_factory`` — builds a started ``LoomRoom`` with N agents and
  stops it on teardown.
- ``bus_recorder`` — bus subscriber capturing every Event with
  ``by_kind`` / ``by_control_type`` / ``count`` helpers.
- ``policy_throwing`` — factory for policies that raise on the Nth
  ``plan_user_turn`` call.
- ``fake_clock`` — opt-in monotonic-clock fake.

All tests in ``tests/subsystem/`` run by default. The ``stress``,
``timing``, ``disk``, and ``breakpoint`` markers are informational
labels for filtering, not skip filters.
"""
from __future__ import annotations

import json
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import pytest

from loom.adapters import agent_from_send, agent_from_stream
from loom.contracts import ConversationPolicy
from loom.kernel.bus import MessageBus
from loom.kernel.events import Event
from loom.kernel.journal import Journal
from loom.kernel.obligations import plan_for_acknowledgement
from loom.policy.open_chat import OpenChatPolicy
from loom.room import LoomRoom


# ---------------------------------------------------------------------------
# Marker registration — labels, not skip-by-default gates.
# ---------------------------------------------------------------------------

def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "stress: heavier workload; bounded by the 45s watchdog")
    config.addinivalue_line(
        "markers",
        "timing: timing-sensitive; uses real wall-clock — generous margins")
    config.addinivalue_line(
        "markers",
        "disk: touches the filesystem (always via tmp_path)")
    config.addinivalue_line(
        "markers",
        "breakpoint: probes the threshold at which behavior degrades")
    config.addinivalue_line(
        "markers",
        "watchdog(seconds): override the default 45s watchdog ceiling")


# ---------------------------------------------------------------------------
# Watchdog — signal.alarm on Unix, threading.Timer fallback.
# ---------------------------------------------------------------------------

_DEFAULT_WATCHDOG_SECONDS = 45


class _WatchdogFired(Exception):
    """Raised by the SIGALRM handler to fail an over-running test."""


def _signal_alarm_available() -> bool:
    return hasattr(signal, "SIGALRM") and hasattr(signal, "alarm") \
        and threading.current_thread() is threading.main_thread()


@pytest.fixture(autouse=True)
def watchdog_timer(request: pytest.FixtureRequest):
    """Fail any test that runs longer than the configured watchdog window.

    Per-test override: ``@pytest.mark.watchdog(seconds=N)``.
    """
    marker = request.node.get_closest_marker("watchdog")
    seconds = _DEFAULT_WATCHDOG_SECONDS
    if marker and marker.kwargs.get("seconds") is not None:
        seconds = int(marker.kwargs["seconds"])
    elif marker and marker.args:
        seconds = int(marker.args[0])

    if _signal_alarm_available():
        prev_handler = signal.getsignal(signal.SIGALRM)

        def _handler(signum, frame):  # noqa: ARG001
            raise _WatchdogFired(
                f"watchdog fired after {seconds}s in {request.node.nodeid}")

        signal.signal(signal.SIGALRM, _handler)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, prev_handler)
        return

    # Fallback: Timer that interrupts the main thread after ``seconds``.
    fired = threading.Event()

    def _interrupt():
        fired.set()
        # Best-effort interrupt — works on cpython main thread.
        try:
            import ctypes  # noqa: PLC0415
            tid = threading.main_thread().ident
            if tid is not None:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_long(tid),
                    ctypes.py_object(_WatchdogFired))
        except Exception:  # pragma: no cover
            pass

    timer = threading.Timer(seconds, _interrupt)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        if fired.is_set():
            pytest.fail(f"watchdog fired after {seconds}s "
                        f"in {request.node.nodeid}")


# ---------------------------------------------------------------------------
# Actor-thread leak guard — autouse.
# ---------------------------------------------------------------------------

def _actor_thread_names() -> set[str]:
    return {t.name for t in threading.enumerate()
            if t.name.startswith("loom-actor-") and t.is_alive()}


@pytest.fixture(autouse=True)
def assert_no_thread_leak(request: pytest.FixtureRequest):
    """Catch tests that leave actor threads alive after teardown.

    Scoped to threads named ``loom-actor-*`` (the ParticipantActor
    convention). Ad-hoc test threads use other names and are ignored.
    """
    before = _actor_thread_names()
    yield
    # Allow up to 2 s for daemon actors to wind down on their own.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        leaked = _actor_thread_names() - before
        if not leaked:
            break
        time.sleep(0.05)
    leaked = _actor_thread_names() - before
    if leaked:
        pytest.fail(
            f"actor threads leaked from {request.node.nodeid}: "
            f"{sorted(leaked)}")


# ---------------------------------------------------------------------------
# Thread harness — spawn + join with exception capture.
# ---------------------------------------------------------------------------

class _ThreadHarness:
    def __init__(self) -> None:
        self._threads: list[threading.Thread] = []
        self._errors: list[BaseException] = []
        self._lock = threading.Lock()

    def spawn(self, target: Callable[[], Any], *,
              name: Optional[str] = None) -> threading.Thread:
        def _runner():
            try:
                target()
            except BaseException as exc:
                with self._lock:
                    self._errors.append(exc)

        t = threading.Thread(
            target=_runner,
            name=name or f"harness-{len(self._threads)}",
            daemon=True,
        )
        self._threads.append(t)
        t.start()
        return t

    def join_all(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        for t in self._threads:
            remaining = max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)

    @property
    def errors(self) -> list[BaseException]:
        with self._lock:
            return list(self._errors)


@pytest.fixture
def thread_harness():
    h = _ThreadHarness()
    yield h
    h.join_all(timeout=5.0)
    alive = [t.name for t in h._threads if t.is_alive()]
    if alive:
        pytest.fail(f"thread_harness threads still alive after join: {alive}")


# ---------------------------------------------------------------------------
# Journal fixtures.
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_journal(tmp_path):
    """Yield a fresh ``Journal`` rooted at ``tmp_path/journal``.

    Teardown opens ``events.jsonl`` and verifies every non-empty line
    parses as JSON. A torn line fails the test loudly.
    """
    journal_dir = tmp_path / "journal"
    journal = Journal(journal_dir)
    journal.open()
    try:
        yield journal
    finally:
        journal.close()
        events_path = journal_dir / "events.jsonl"
        if events_path.exists():
            for line in events_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    pytest.fail(
                        f"journal contains malformed line: {line!r} "
                        f"({exc})")


class InMemoryFaultJournal(Journal):
    """Journal subclass that fails its Nth ``write`` call.

    The fault is injected at the file-write layer so the surrounding
    journal logic (degraded flag, failure callback, single
    ``journal_error`` emission) runs end-to-end. Beats fragile
    filesystem chmod on shared filesystems like Lustre.
    """

    def __init__(self, *args, fail_at: int = 1, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._fault_at = fail_at
        self._fault_calls = 0

    def open(self) -> None:  # type: ignore[override]
        super().open()
        if self._events_file is None:
            return
        real_file = self._events_file
        outer = self

        class _FailingFile:
            def write(self, s: str) -> int:
                outer._fault_calls += 1
                if outer._fault_calls == outer._fault_at:
                    raise OSError("simulated journal write failure")
                return real_file.write(s)

            def flush(self) -> None:
                real_file.flush()

            def close(self) -> None:
                real_file.close()

        self._events_file = _FailingFile()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Adversarial agent factory.
# ---------------------------------------------------------------------------

class _AdversarialAgentFactory:
    """Build agents with various hostile shapes for stress tests.

    Each method returns a ``_FunctionAgent`` produced by the canonical
    adapter, so the streaming pipeline treats them indistinguishably
    from real agents.
    """

    @staticmethod
    def hang_after_first_delta(agent_id: str, *, hang_seconds: float = 5.0,
                               first_chunk: str = "first delta"):
        """Yield one delta, then sleep for ``hang_seconds``."""
        def gen(_p):
            yield first_chunk
            time.sleep(hang_seconds)
            yield "never reached in bounded tests"

        return agent_from_stream(agent_id, gen)

    @staticmethod
    def slow_first_delta(agent_id: str, *, seconds: float = 5.0,
                         delta: str = "delayed first delta"):
        """Sleep, then yield a single delta."""
        def gen(_p):
            time.sleep(seconds)
            yield delta

        return agent_from_stream(agent_id, gen)

    @staticmethod
    def infinite_stream(agent_id: str, *, cap_chunks: int = 10000,
                        chunk: str = "x"):
        """Yield ``chunk`` up to ``cap_chunks`` times — always bounded."""
        def gen(_p):
            for _ in range(cap_chunks):
                yield chunk

        return agent_from_stream(agent_id, gen)

    @staticmethod
    def garbage_payload(agent_id: str, *,
                        payload: str = "\x00\x01\x02 � binary noise"):
        """Yield non-printable / encoding-edge chunks."""
        def gen(_p):
            yield payload

        return agent_from_stream(agent_id, gen)

    @staticmethod
    def yields_none(agent_id: str):
        """Yield ``None`` chunks — adapter must filter them."""
        def gen(_p):
            yield None
            yield "real content sufficient to bypass loop guard length"
            yield None

        return agent_from_stream(agent_id, gen)

    @staticmethod
    def raises_after_chunks(agent_id: str, *, n: int = 1,
                            exc: type[BaseException] = RuntimeError,
                            message: str = "adversarial raise"):
        """Yield ``n`` chunks then raise ``exc(message)``."""
        def gen(_p):
            for i in range(n):
                yield f"chunk {i} long enough so far for the buffer to flush "
            raise exc(message)

        return agent_from_stream(agent_id, gen)

    @staticmethod
    def flood_chunks(agent_id: str, *, n_chunks: int = 1000,
                     chunk: str = "flood "):
        """Yield ``n_chunks`` small chunks in rapid succession."""
        def gen(_p):
            for _ in range(n_chunks):
                yield chunk

        return agent_from_stream(agent_id, gen)


@pytest.fixture
def adversarial_agent():
    return _AdversarialAgentFactory()


# ---------------------------------------------------------------------------
# Room factory.
# ---------------------------------------------------------------------------

@pytest.fixture
def room_factory():
    """Build an ``LoomRoom`` with the requested configuration.

    Returns a callable that itself returns a started ``LoomRoom``. The
    fixture stops every room it produced on teardown (10 s timeout).
    """
    rooms: list[LoomRoom] = []

    def _build(*, agents: Iterable, policy=None, topic: Optional[str] = None,
               journal_dir: Optional[Path] = None, anchor_id: Optional[str] = None,
               default_responder_id: Optional[str] = None,
               policy_error_mode: str = "close_turn",
               room_config=None) -> LoomRoom:
        kwargs = {
            "agents": list(agents),
            "topic": topic,
            "policy": policy if policy is not None else OpenChatPolicy(),
            "policy_error_mode": policy_error_mode,
        }
        if anchor_id is not None:
            kwargs["anchor_id"] = anchor_id
        if default_responder_id is not None:
            kwargs["default_responder_id"] = default_responder_id
        if journal_dir is not None:
            kwargs["journal_dir"] = journal_dir
        if room_config is not None:
            kwargs["room_config"] = room_config
        room = LoomRoom(**kwargs)
        room.start()
        rooms.append(room)
        return room

    yield _build
    for room in rooms:
        try:
            room.stop(timeout=10.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Bus recorder.
# ---------------------------------------------------------------------------

class _BusRecorder:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._lock = threading.Lock()
        self._unsubscribe: Optional[Callable[[], None]] = None

    def attach(self, bus: MessageBus) -> "_BusRecorder":
        def _on_event(event: Event) -> None:
            with self._lock:
                self._events.append(event)

        self._unsubscribe = bus.subscribe(_on_event)
        return self

    def detach(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    @property
    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def by_kind(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]

    def by_control_type(self, control_type: str) -> list[Event]:
        out = []
        for e in self.events:
            if e.kind == "control" and isinstance(e.body, dict):
                if e.body.get("control_type") == control_type:
                    out.append(e)
        return out

    def count(self) -> int:
        return len(self.events)


@pytest.fixture
def bus_recorder():
    rec = _BusRecorder()
    yield rec
    rec.detach()


# ---------------------------------------------------------------------------
# Throwing-policy factory.
# ---------------------------------------------------------------------------

@pytest.fixture
def policy_throwing():
    """Build a policy that raises on the Nth ``plan_user_turn`` call.

    Subsequent calls return an acknowledgement plan.
    """

    def _build(*, raise_on_call: int = 1,
               exc: type[BaseException] = RuntimeError,
               message: str = "policy throwing fixture"):

        class _Throwing(ConversationPolicy):
            name = "throwing"

            def __init__(self):
                self.calls = 0

            def plan_user_turn(self, user_event, state, *, prior_speaker=None):
                self.calls += 1
                if self.calls == raise_on_call:
                    raise exc(message)
                return plan_for_acknowledgement(rationale="post-throw ack")

        return _Throwing()

    return _build


# ---------------------------------------------------------------------------
# Fake clock — opt-in.
# ---------------------------------------------------------------------------

class _FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    """Patch ``time.monotonic`` and ``time.time`` to a controllable clock.

    Tests that do not use this fixture continue to use the real clock.
    """
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.now)
    monkeypatch.setattr(time, "time", clock.now)
    return clock


# ---------------------------------------------------------------------------
# Convenience: simple agent pool builders.
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_agents():
    """Factory for N healthy agents with a long-enough varying reply.

    Each call produces a slightly different reply (prompt-dependent +
    a per-call counter) so the LoopGuard does not de-dup replies
    across consecutive turns.
    """

    def _build(n: int, *, prefix: str = "agent",
               base: str = ("agent {i} reply that is sufficiently long to "
                            "exceed both the pass buffer and the loop "
                            "guard short-text threshold for canonical "
                            "commit. Counter: {c}.")):
        counter = [0]

        def _make(i: int):
            def _send(prompt):
                counter[0] += 1
                return base.format(i=i, c=counter[0])
            return _send

        return [
            agent_from_send(f"{prefix}{i}", _make(i))
            for i in range(n)
        ]

    return _build
