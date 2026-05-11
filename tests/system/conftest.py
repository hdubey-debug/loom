"""Shared fixtures for ``tests/system/``.

System-level tests drive the assembled Loom kernel through its public
surface only (``LoomRoom``, adapters, ``RoomConfig``, the four bundled
policies, ``Journal`` / ``restore_state`` for observation). These
fixtures extend the subsystem tier's primitives (watchdog, thread-leak
guard, adversarial agents, fault journal) with longer ceilings and
system-tier helpers for multi-turn workloads, restart scenarios,
scripted console sessions, mixed-agent rooms, and break-point probes.

Inherits and re-exports the subsystem ``_AdversarialAgentFactory`` and
``InMemoryFaultJournal`` so we don't duplicate adversarial-agent
machinery across tiers.

Stdlib only — no pytest-timeout / no hypothesis.
"""

from __future__ import annotations

import json
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Tuple

import pytest

from loom.adapters import agent_from_send
from loom.contracts import ConversationPolicy
from loom.kernel.events import Event
from loom.kernel.journal import Journal
from loom.kernel.room import RoomConfig
from loom.policy.open_chat import OpenChatPolicy
from loom.room import LoomRoom

# Re-export the adversarial / fault primitives from the subsystem tier so
# system tests can build mixed-agent rooms without duplication.
from tests.subsystem.conftest import (  # noqa: F401  (re-exports)
    InMemoryFaultJournal,
    _AdversarialAgentFactory,
)


# ---------------------------------------------------------------------------
# Marker registration
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "stress: heavier multi-turn workload; bounded by the system watchdog"
    )
    config.addinivalue_line(
        "markers", "timing: timing-sensitive; uses real wall-clock with generous margins"
    )
    config.addinivalue_line("markers", "disk: touches the filesystem (always via tmp_path)")
    config.addinivalue_line(
        "markers", "breakpoint: probes the threshold at which behavior degrades"
    )
    config.addinivalue_line(
        "markers", "watchdog(seconds): override the default 90s watchdog ceiling"
    )


# ---------------------------------------------------------------------------
# Watchdog — same shape as subsystem's, 90s default ceiling.
# ---------------------------------------------------------------------------

_DEFAULT_SYSTEM_WATCHDOG_SECONDS = 90


class _WatchdogFired(Exception):
    """Raised by the SIGALRM handler to fail an over-running test."""


def _signal_alarm_available() -> bool:
    return (
        hasattr(signal, "SIGALRM")
        and hasattr(signal, "alarm")
        and threading.current_thread() is threading.main_thread()
    )


@pytest.fixture(autouse=True)
def system_watchdog(request: pytest.FixtureRequest):
    """Cap every system test at 90s; override via @pytest.mark.watchdog(seconds=N)."""
    marker = request.node.get_closest_marker("watchdog")
    seconds = _DEFAULT_SYSTEM_WATCHDOG_SECONDS
    if marker and marker.kwargs.get("seconds") is not None:
        seconds = int(marker.kwargs["seconds"])
    elif marker and marker.args:
        seconds = int(marker.args[0])

    if _signal_alarm_available():
        prev_handler = signal.getsignal(signal.SIGALRM)

        def _handler(signum, frame):  # noqa: ARG001
            raise _WatchdogFired(f"system_watchdog fired after {seconds}s in {request.node.nodeid}")

        signal.signal(signal.SIGALRM, _handler)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, prev_handler)
        return

    fired = threading.Event()

    def _interrupt():
        fired.set()
        try:
            import ctypes  # noqa: PLC0415

            tid = threading.main_thread().ident
            if tid is not None:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_long(tid), ctypes.py_object(_WatchdogFired)
                )
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
            pytest.fail(f"system_watchdog fired after {seconds}s in {request.node.nodeid}")


# ---------------------------------------------------------------------------
# Thread-leak guard — strict superset of subsystem's.
# ---------------------------------------------------------------------------


def _system_thread_names() -> set[str]:
    """Names of Loom-owned threads currently alive.

    Captures both ``loom-actor-*`` (ParticipantActor) and
    ``loom-journal-snapshot`` (Journal background writer).
    """
    out: set[str] = set()
    for t in threading.enumerate():
        if not t.is_alive():
            continue
        name = t.name
        if name.startswith("loom-actor-") or name == "loom-journal-snapshot":
            out.add(name)
    return out


@pytest.fixture(autouse=True)
def assert_no_thread_leak_extended(request: pytest.FixtureRequest):
    """Strict superset of subsystem's leak guard.

    Catches leaked actor threads AND leaked ``loom-journal-snapshot``
    background writers (the latter is unique to system-tier tests
    because the subsystem tier's tests rarely open a journal directly).
    """
    before = _system_thread_names()
    yield
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        leaked = _system_thread_names() - before
        if not leaked:
            break
        time.sleep(0.05)
    leaked = _system_thread_names() - before
    if leaked:
        pytest.fail(f"Loom threads leaked from {request.node.nodeid}: {sorted(leaked)}")


# ---------------------------------------------------------------------------
# ThrottleConfig lifter — same seam used by subsystem stress tests.
# ---------------------------------------------------------------------------


def _lift_room_throttle(room: LoomRoom) -> None:
    """Replace the coordinator's throttle with a 10k/min ceiling.

    This is the same documented test-only seam used in the subsystem
    tier. It exists so multi-turn tests don't fight the default 10/min
    per-participant rate limiter (the throttle behavior is exercised
    on its own in ``test_resource_pressure.py`` without lifting).
    """
    from loom.kernel.coordinator import ThrottleConfig

    room.session.coordinator._throttle = ThrottleConfig(
        per_participant_per_min=10_000,
        per_channel_per_min=10_000,
    )


# ---------------------------------------------------------------------------
# multi_turn_session — primary long-haul fixture.
# ---------------------------------------------------------------------------


@pytest.fixture
def multi_turn_session():
    """Build and start an ``LoomRoom`` for multi-turn workloads.

    Returns a callable accepting the same kwargs as ``LoomRoom`` plus
    ``lift_throttle: bool`` (default True). Each room produced is
    auto-stopped on teardown.
    """
    rooms: list[LoomRoom] = []

    def _build(
        *,
        agents: Iterable,
        policy=None,
        topic: Optional[str] = None,
        anchor_id: Optional[str] = None,
        default_responder_id: Optional[str] = None,
        journal_dir: Optional[Path] = None,
        policy_error_mode: str = "close_turn",
        room_config: Optional[RoomConfig] = None,
        lift_throttle: bool = True,
    ) -> LoomRoom:
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
        if lift_throttle:
            _lift_room_throttle(room)
        rooms.append(room)
        return room

    yield _build
    for room in rooms:
        try:
            room.stop(timeout=10.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# journaled_room — tmp_path-backed, validates JSONL on teardown.
# ---------------------------------------------------------------------------


def _validate_journal_jsonl(events_path: Path) -> None:
    """Verify every non-empty line of ``events.jsonl`` parses as JSON."""
    if not events_path.exists():
        return
    for i, line in enumerate(events_path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            pytest.fail(f"journal line {i} malformed: {line!r} ({exc})")


@pytest.fixture
def journaled_room(tmp_path):
    """Yield a started ``LoomRoom`` bound to ``tmp_path / 'session'``.

    Returns a callable accepting the same kwargs as ``LoomRoom`` minus
    ``journal_dir`` (which is fixed). Optional ``lift_throttle=True``
    lifts the throttle so multi-turn journal tests don't stall.

    Auto-stops on teardown and validates the JSONL was well-formed.
    """
    journal_dir = tmp_path / "session"
    rooms: list[LoomRoom] = []

    def _build(
        *,
        agents: Iterable,
        policy=None,
        topic: Optional[str] = None,
        anchor_id: Optional[str] = None,
        default_responder_id: Optional[str] = None,
        policy_error_mode: str = "close_turn",
        room_config: Optional[RoomConfig] = None,
        lift_throttle: bool = True,
    ) -> LoomRoom:
        kwargs = {
            "agents": list(agents),
            "topic": topic,
            "policy": policy if policy is not None else OpenChatPolicy(),
            "policy_error_mode": policy_error_mode,
            "journal_dir": journal_dir,
        }
        if anchor_id is not None:
            kwargs["anchor_id"] = anchor_id
        if default_responder_id is not None:
            kwargs["default_responder_id"] = default_responder_id
        if room_config is not None:
            kwargs["room_config"] = room_config
        room = LoomRoom(**kwargs)
        room.start()
        if lift_throttle:
            _lift_room_throttle(room)
        rooms.append(room)
        return room

    _build.journal_dir = journal_dir  # type: ignore[attr-defined]
    yield _build

    for room in rooms:
        try:
            room.stop(timeout=10.0)
        except Exception:
            pass
    _validate_journal_jsonl(journal_dir / "events.jsonl")


# ---------------------------------------------------------------------------
# restart_helper — simulate process restart against the same journal dir.
# ---------------------------------------------------------------------------


@pytest.fixture
def restart_helper():
    """Stop ``old_room``, then construct a fresh second ``LoomRoom`` over
    the same journal_dir.

    Returns a callable: ``restart(old_room, *, agents, policy=..., **kwargs)``
    which stops the old room (10s timeout, draining the snapshot
    queue), waits for ``room_state.json`` to flush (Lustre/NFS
    safety), and constructs a NEW room over the same journal dir.
    Returns ``(new_room, restored_state)`` where ``restored_state`` is
    the ``Journal.load_state()`` payload visible immediately after
    stop.
    """
    rooms: list[LoomRoom] = []

    def _restart(
        old_room: LoomRoom,
        *,
        agents: Iterable,
        policy=None,
        topic: Optional[str] = None,
        anchor_id: Optional[str] = None,
        default_responder_id: Optional[str] = None,
        policy_error_mode: str = "close_turn",
        room_config: Optional[RoomConfig] = None,
        lift_throttle: bool = True,
    ) -> Tuple[LoomRoom, Optional[dict]]:
        journal_dir = old_room.journal_dir
        if journal_dir is None:
            raise ValueError("restart_helper requires the old room to have a journal_dir")
        old_room.stop(timeout=10.0)
        # Poll for state file flush — Lustre/NFS may lag the close.
        state_path = journal_dir / "room_state.json"
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if state_path.exists():
                break
            time.sleep(0.05)
        # Read snapshot via the public Journal surface (observation only).
        loader = Journal(journal_dir)
        restored_state = loader.load_state()

        kwargs = {
            "agents": list(agents),
            "topic": topic,
            "policy": policy if policy is not None else OpenChatPolicy(),
            "policy_error_mode": policy_error_mode,
            "journal_dir": journal_dir,
        }
        if anchor_id is not None:
            kwargs["anchor_id"] = anchor_id
        if default_responder_id is not None:
            kwargs["default_responder_id"] = default_responder_id
        if room_config is not None:
            kwargs["room_config"] = room_config
        new_room = LoomRoom(**kwargs)
        new_room.start()
        if lift_throttle:
            _lift_room_throttle(new_room)
        rooms.append(new_room)
        return new_room, restored_state

    yield _restart
    for room in rooms:
        try:
            room.stop(timeout=10.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# event_recorder — bus subscriber with rich query helpers.
# ---------------------------------------------------------------------------


class _EventRecorder:
    """Bus subscriber. Captures every event; offers query helpers."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._lock = threading.Lock()
        self._unsubscribe: Optional[Callable[[], None]] = None

    def attach(self, room: LoomRoom) -> "_EventRecorder":
        """Subscribe to ``room.session.bus``. Idempotent on re-attach
        only via teardown — the test should construct a fresh recorder
        per attached bus.
        """
        bus = room.session.bus

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
        out: list[Event] = []
        for e in self.events:
            if e.kind == "control" and isinstance(e.body, dict):
                if e.body.get("control_type") == control_type:
                    out.append(e)
        return out

    def by_sender(self, sender: str) -> list[Event]:
        return [e for e in self.events if e.sender == sender]

    def count(self) -> int:
        with self._lock:
            return len(self._events)

    def snapshot_at(self, predicate: Callable[[Event], bool]) -> list[Event]:
        """Return all events matching ``predicate`` at this instant."""
        return [e for e in self.events if predicate(e)]


@pytest.fixture
def event_recorder():
    rec = _EventRecorder()
    yield rec
    rec.detach()


# ---------------------------------------------------------------------------
# mixed_agent_room — healthy + adversarial in one room.
# ---------------------------------------------------------------------------

# Healthy reply long enough to clear the loop guard and pass buffer.
_LONG_REPLY = (
    "healthy reply long enough to bypass both the pass buffer "
    "and the loop guard short-text threshold for canonical commit."
)


def _healthy_send_for(pid: str, counter: list[int]):
    def _send(prompt):
        counter[0] += 1
        return f"{pid} reply {counter[0]}: {_LONG_REPLY}"

    return _send


@pytest.fixture
def mixed_agent_room():
    """Build a room mixing N healthy agents with a list of adversarial agents.

    Usage:
        room = mixed_agent_room(
            healthy=3,
            adversarial=[("hang", 1), ("garbage", 1)],
            policy=OpenChatPolicy(),
        )

    Adversarial kinds map onto ``_AdversarialAgentFactory`` methods:
    ``"hang"`` → ``hang_after_first_delta``, ``"slow"`` →
    ``slow_first_delta``, ``"infinite"`` → ``infinite_stream``,
    ``"garbage"`` → ``garbage_payload``, ``"none"`` → ``yields_none``,
    ``"raises"`` → ``raises_after_chunks``, ``"flood"`` →
    ``flood_chunks``. Each adversarial gets a distinct id like
    ``adv_hang_0``.

    Auto-stops on teardown.
    """
    rooms: list[LoomRoom] = []
    adv_factory = _AdversarialAgentFactory()

    def _build(
        *,
        healthy: int = 3,
        adversarial: Optional[List[Tuple[str, int]]] = None,
        policy=None,
        policy_error_mode: str = "close_turn",
        default_responder_id: Optional[str] = None,
        room_config: Optional[RoomConfig] = None,
        journal_dir: Optional[Path] = None,
        lift_throttle: bool = True,
    ) -> LoomRoom:
        counter = [0]
        agents: list = []
        for i in range(healthy):
            pid = f"healthy_{i}"
            agents.append(agent_from_send(pid, _healthy_send_for(pid, counter)))
        adversarial = adversarial or []
        for kind, count in adversarial:
            for j in range(count):
                pid = f"adv_{kind}_{j}"
                if kind == "hang":
                    agents.append(adv_factory.hang_after_first_delta(pid, hang_seconds=2.0))
                elif kind == "slow":
                    agents.append(adv_factory.slow_first_delta(pid, seconds=1.0))
                elif kind == "infinite":
                    agents.append(adv_factory.infinite_stream(pid, cap_chunks=200, chunk="x"))
                elif kind == "garbage":
                    agents.append(adv_factory.garbage_payload(pid))
                elif kind == "none":
                    agents.append(adv_factory.yields_none(pid))
                elif kind == "raises":
                    agents.append(adv_factory.raises_after_chunks(pid, n=1))
                elif kind == "flood":
                    agents.append(adv_factory.flood_chunks(pid, n_chunks=50))
                else:
                    raise ValueError(f"unknown adversarial kind: {kind!r}")
        kwargs = {
            "agents": agents,
            "policy": policy if policy is not None else OpenChatPolicy(),
            "policy_error_mode": policy_error_mode,
        }
        if default_responder_id is not None:
            kwargs["default_responder_id"] = default_responder_id
        if room_config is not None:
            kwargs["room_config"] = room_config
        if journal_dir is not None:
            kwargs["journal_dir"] = journal_dir
        room = LoomRoom(**kwargs)
        room.start()
        if lift_throttle:
            _lift_room_throttle(room)
        rooms.append(room)
        return room

    yield _build
    for room in rooms:
        try:
            room.stop(timeout=10.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# scripted_console — driver for run_console.
# ---------------------------------------------------------------------------


class _ConsoleScript:
    """Build a ``(prompt_fn, captured_lines)`` pair from a list of inputs.

    A non-string entry signals an exit:
    - ``EOFError`` (the type) raises EOFError on the next call.
    - ``KeyboardInterrupt`` (the type) raises that on the next call.

    After all inputs are exhausted, ``EOFError`` is raised so
    ``run_console`` exits cleanly. ``captured_lines`` is the list of
    every notify message (thread-safe append).
    """

    def __init__(self, lines: List[Any]) -> None:
        self._lines = list(lines)
        self._idx = 0
        self.captured_lines: list[str] = []
        self._lock = threading.Lock()

    def prompt_fn(self) -> str:
        if self._idx >= len(self._lines):
            raise EOFError
        nxt = self._lines[self._idx]
        self._idx += 1
        if nxt is EOFError:
            raise EOFError
        if nxt is KeyboardInterrupt:
            raise KeyboardInterrupt
        return str(nxt)

    def notify(self, msg: str) -> None:
        with self._lock:
            self.captured_lines.append(msg)


@pytest.fixture
def scripted_console():
    def _build(lines: List[Any]) -> _ConsoleScript:
        return _ConsoleScript(lines)

    return _build


# ---------------------------------------------------------------------------
# varied_agents — long-haul-friendly agent pool builder.
# ---------------------------------------------------------------------------


@pytest.fixture
def varied_agents():
    """Factory for N healthy agents whose replies vary by user_event_id
    + per-call counter, so the LoopGuardConfig doesn't dedup near-identical
    short replies across many turns.

    Usage:
        agents = varied_agents(5)
        agents = varied_agents(3, prefix="bot")
    """

    def _build(n: int, *, prefix: str = "agent"):
        counter = [0]

        def _make(i: int):
            def _send(prompt):
                counter[0] += 1
                return (
                    f"{prefix}{i} turn {counter[0]} reply "
                    "with sufficient length to bypass both the pass "
                    "buffer and the loop guard short-text threshold "
                    "for canonical commit."
                )

            return _send

        return [agent_from_send(f"{prefix}{i}", _make(i)) for i in range(n)]

    return _build


# ---------------------------------------------------------------------------
# slow_policy_factory — policies that sleep / raise on the Nth call.
# ---------------------------------------------------------------------------


@pytest.fixture
def slow_policy_factory():
    """Build a policy that sleeps and/or raises on selected calls.

    Args:
        sleep_ms: ms to sleep before returning the plan (every call).
        raise_on_call: if not None, raise on the N-th call (1-indexed);
            subsequent calls return acknowledgement plans.
        exc / message: exception class + text raised on raise_on_call.
        delegate: if not None, base policy whose plan_user_turn drives
            the non-raising path. Default: broadcast every active capable
            participant via plan_with_required.
    """

    def _build(
        *,
        sleep_ms: int = 0,
        raise_on_call: Optional[int] = None,
        exc: type[BaseException] = RuntimeError,
        message: str = "slow_policy_factory raise",
        delegate: Optional[ConversationPolicy] = None,
        name: str = "slow_policy",
    ) -> ConversationPolicy:
        base = delegate if delegate is not None else OpenChatPolicy()

        class _SlowPolicy(ConversationPolicy):
            def __init__(self) -> None:
                self.calls = 0

            def plan_user_turn(self, user_event, state):
                self.calls += 1
                if sleep_ms > 0:
                    time.sleep(sleep_ms / 1000.0)
                if raise_on_call is not None and self.calls == raise_on_call:
                    raise exc(message)
                return base.plan_user_turn(user_event, state)

        _SlowPolicy.name = name
        return _SlowPolicy()

    return _build


# ---------------------------------------------------------------------------
# config_factory — DSL for RoomConfig deltas.
# ---------------------------------------------------------------------------


@pytest.fixture
def config_factory():
    """Build a ``RoomConfig`` with selected fields overridden.

    Defaults match ``RoomConfig()``: ``compact_threshold=50``,
    ``user_turn_idle_timeout_s=20``, ``user_turn_debounce_ms=250``,
    ``pass_buffer_chars=16``, ``lease_ttl_s=60``,
    ``max_drafts_per_participant=1``.
    """

    def _build(
        *,
        compact_threshold: int = 50,
        user_turn_idle_timeout_s: int = 20,
        user_turn_debounce_ms: int = 250,
        pass_buffer_chars: int = 16,
        lease_ttl_s: int = 60,
        max_drafts_per_participant: int = 1,
    ) -> RoomConfig:
        return RoomConfig(
            compact_threshold=compact_threshold,
            user_turn_idle_timeout_s=user_turn_idle_timeout_s,
            user_turn_debounce_ms=user_turn_debounce_ms,
            pass_buffer_chars=pass_buffer_chars,
            lease_ttl_s=lease_ttl_s,
            max_drafts_per_participant=max_drafts_per_participant,
        )

    return _build


# ---------------------------------------------------------------------------
# multi_room_factory — spawn N concurrent rooms.
# ---------------------------------------------------------------------------


@pytest.fixture
def multi_room_factory(tmp_path):
    """Create N independent ``LoomRoom`` instances with distinct journals.

    Usage:
        rooms = multi_room_factory(n=3, agents_per_room=2,
                                   policy=OpenChatPolicy())
    """
    rooms: list[LoomRoom] = []

    def _build(
        *,
        n: int = 3,
        agents_per_room: int = 2,
        policy=None,
        with_journal: bool = True,
        lift_throttle: bool = True,
    ) -> list[LoomRoom]:
        out: list[LoomRoom] = []
        for r in range(n):
            counter = [0]

            def _make(idx: int, room_idx: int = r):
                def _send(prompt):
                    counter[0] += 1
                    return (
                        f"r{room_idx}-a{idx} turn {counter[0]} "
                        "reply long enough to bypass both buffers "
                        "of the Loom kernel for canonical commit."
                    )

                return _send

            agents = [agent_from_send(f"r{r}_a{i}", _make(i)) for i in range(agents_per_room)]
            kwargs: dict = {
                "agents": agents,
                "policy": policy if policy is not None else OpenChatPolicy(),
            }
            if with_journal:
                kwargs["journal_dir"] = tmp_path / f"room_{r}"
            room = LoomRoom(**kwargs)
            room.start()
            if lift_throttle:
                _lift_room_throttle(room)
            out.append(room)
            rooms.append(room)
        return out

    yield _build
    for room in rooms:
        try:
            room.stop(timeout=10.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# binary_search — shared bisection helper for break-point probes.
# ---------------------------------------------------------------------------


def _binary_search_impl(
    measure: Callable[[int], float],
    *,
    lo: int,
    hi: int,
    threshold: float,
    floor: int,
    label: str = "metric",
) -> int:
    """Find the smallest N in [lo, hi] with measure(N) > threshold.

    Doubles N from ``lo`` until either the threshold is exceeded or
    ``hi`` is reached. Prints the measured threshold and asserts the
    sanity floor. Returns the breakpoint N (or ``hi`` if never crossed).
    """
    n = lo
    breakpoint_n = hi
    while n <= hi:
        val = measure(n)
        if val > threshold:
            breakpoint_n = n
            break
        n *= 2
    print(f"BREAKPOINT: {label}={breakpoint_n}")
    assert breakpoint_n >= floor, f"breakpoint {breakpoint_n} < floor {floor} for {label}"
    return breakpoint_n


@pytest.fixture(scope="session")
def binary_search():
    return _binary_search_impl


# ---------------------------------------------------------------------------
# fake_clock — re-export of subsystem's monotonic-clock fake.
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
    """Patch ``time.monotonic`` and ``time.time`` to a controllable clock."""
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.now)
    monkeypatch.setattr(time, "time", clock.now)
    return clock


# ---------------------------------------------------------------------------
# Public-API discipline check — fails collection if a test mutates state.
# ---------------------------------------------------------------------------

_FORBIDDEN_API_PATTERNS = (
    # Disallowed: state mutators reached via ``room.session.*``.
    # Allowed: ``bus.subscribe``, ``bus.snapshot``, ``journal.load_state``,
    # ``journal.load_events``, ``state.participants`` (read), etc.
    "session.coordinator.set_",
    "session.coordinator.register_",
    "session.coordinator.unregister_",
    "session.coordinator.acquire_lease",
    "session.coordinator.release_lease",
    "session.coordinator.open_user_turn",
    "session.coordinator.close_user_turn",
    "session.coordinator.handle_skip",
    "session.coordinator.on_stream_end",
    "session.state.set_",
    "session.state.add_participant",
    "session.state.remove_participant",
    "session.state.advance_round_robin",
    "session.bus.post(",
    "session.bus.stop(",
    "session.add_agent(",
    "session.remove_agent(",
    "session.start(",
    "session.stop(",
)


def pytest_collection_modifyitems(config, items):
    """Fail collection if any system-tier test file uses a forbidden pattern.

    System tests must drive the kernel through ``LoomRoom`` only;
    ``room.session.*`` is for observation. This guard catches drift
    before tests run rather than letting subtle violations land.
    """
    here = Path(__file__).resolve().parent
    offenders: list[str] = []
    for path in here.glob("test_*.py"):
        text = path.read_text()
        for pat in _FORBIDDEN_API_PATTERNS:
            if pat in text:
                offenders.append(f"{path.name}: {pat}")
    if offenders:
        raise pytest.UsageError(
            "tests/system/ violates public-API discipline:\n  " + "\n  ".join(offenders)
        )
