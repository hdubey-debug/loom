"""Loom scenario benchmark — ``benchmarks/perf.py``.

The canonical performance harness for the kernel. Runs five families of
workloads, captures ``time.perf_counter_ns`` percentiles, ``tracemalloc``
peaks, ``psutil`` RSS, and per-axis cross products. Writes one JSON
file (machine-readable, fed to ``scripts/bench_diff.py``) and one
Markdown file (human-readable, committed under ``docs/``).

Usage::

    ./venv/bin/python -m benchmarks.perf --output docs/perf-baseline.json
    ./venv/bin/python -m benchmarks.perf --quick      # smaller axes, ~30s
    ./venv/bin/python -m benchmarks.perf --scenario chat history

Scenarios:

- ``chat``     end-to-end LoomRoom turns, N agents × M turns
- ``burst``    bus.post throughput, no subscribers and 1 subscriber
- ``stream``   stream_delta posting rate (proxy for high-token-rate)
- ``history``  bus.snapshot cost across log sizes 100 → 100k
- ``cursor``   bus.snapshot(since=k) at fresh / tail / stale cursor
- ``micro``    direct microbench of bus.post / Event.to_jsonl /
               UserTurn.obligation_for / actor._lookup_event /
               coordinator._find_recent_chat_event_id

Cross-product axes recorded (when applicable):

- participants ∈ {1, 2, 5, 10, 25, 50}
- event log size ∈ {100, 1000, 10000, 50000, 100000}
- policy ∈ {OpenChat, Default, SingleResponder, RoundRobin}
- streaming chunks ∈ {PASS, 1, 10, 100}
- journal ∈ {off, append-only, snapshot-every-100}
- actor cursor ∈ {fresh, tail, stale}

The output JSON has a stable schema:

    {
      "host": {...},
      "ts": "2026-05-08T12:00:00Z",
      "git_rev": "...",
      "scenarios": {
        "<scenario>": {
          "<case_name>": {
            "p50_ns": ..., "p99_ns": ..., "mean_ns": ...,
            "iters": ..., "inner": ...,
            "extras": {...}
          },
          ...
        },
        ...
      }
    }
"""
from __future__ import annotations

import argparse
import gc
import json
import platform
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterator, Optional

# psutil is in the dev extras — but the bench is dev-only so it's available
# on the bench-running box. Wrap import to keep error messaging clean.
try:
    import psutil
except ImportError:  # pragma: no cover - dev dep
    psutil = None  # type: ignore[assignment]

from loom import (
    LoomRoom,
    OpenChatPolicy,
    DefaultPolicy,
    SingleResponderPolicy,
    RoundRobinPolicy,
    agent_from_stream,
)
from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.journal import Journal
from loom.kernel.obligations import ResponseObligation, UserTurnPlan
from loom.kernel.user_turn import UserTurn


# ---------------------------------------------------------------------------
# Stable result schema
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    name: str
    iters: int
    inner: int
    p50_ns: float
    p99_ns: float
    mean_ns: float
    min_ns: float
    extras: dict[str, Any] = field(default_factory=dict)

    def per_op_ns(self) -> float:
        return self.p50_ns / self.inner if self.inner else 0.0


# ---------------------------------------------------------------------------
# Timing core
# ---------------------------------------------------------------------------

def _time_callable(
    fn: Callable[[], Any],
    *,
    iters: int,
    inner: int,
    warmup: int = 5,
) -> tuple[float, float, float, float]:
    """Run ``fn`` ``iters`` times (inner repeats per call); return p50/p99/mean/min ns."""
    samples: list[int] = []
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(warmup):
            fn()
        for _ in range(iters):
            t0 = time.perf_counter_ns()
            for _ in range(inner):
                fn()
            samples.append(time.perf_counter_ns() - t0)
    finally:
        if gc_was_enabled:
            gc.enable()
        gc.collect()
    samples.sort()
    p50 = statistics.median(samples)
    p99 = samples[int(0.99 * (len(samples) - 1))]
    mean = statistics.fmean(samples)
    return float(p50), float(p99), float(mean), float(samples[0])


def _bench(name: str, fn: Callable[[], Any], *,
           iters: int = 100, inner: int = 1, warmup: int = 5,
           extras: Optional[dict[str, Any]] = None) -> CaseResult:
    p50, p99, mean, mn = _time_callable(fn, iters=iters, inner=inner, warmup=warmup)
    return CaseResult(name=name, iters=iters, inner=inner,
                      p50_ns=p50, p99_ns=p99, mean_ns=mean, min_ns=mn,
                      extras=extras or {})


# ---------------------------------------------------------------------------
# Stub agents — for end-to-end LoomRoom scenarios
# ---------------------------------------------------------------------------

_BENCH_REPLY_TAIL = (
    "This is a benchmark reply with enough words to exceed the kernel's "
    "loop-guard short-text threshold (50 chars), so successive turns "
    "with otherwise-identical bodies are not suppressed by the IoU "
    "duplicate detector. Token N: "
)


def _varying_stream(prefix: str):
    """Streamer that yields a per-call-unique reply > short-text threshold.

    The kernel's :class:`LoopGuard` dedupes near-duplicate short replies
    (under 50 chars) at IoU > 0.8 — without this the second consecutive
    "ack from a0" gets suppressed and the bench stalls. We bypass that
    by appending a monotonic counter and ensuring length > threshold.
    """
    counter = [0]
    def _stream(_prompt: object) -> Iterator[str]:
        counter[0] += 1
        yield f"{prefix}: {_BENCH_REPLY_TAIL}{counter[0]}"
    return _stream


def _chunked_stream(text: str, chunk_size: int):
    """Yield ``text`` in fixed-size chunks (for stream-rate benchmarks)."""
    def _stream(_prompt: object) -> Iterator[str]:
        for i in range(0, len(text), chunk_size):
            yield text[i:i + chunk_size]
    return _stream


def _make_room(
    n_agents: int,
    *,
    policy_name: str = "OpenChat",
    journal_dir: Optional[Path] = None,
) -> tuple[LoomRoom, list[str]]:
    """Build an LoomRoom with N stub agents that all reply instantly.

    Each agent yields a varying body (counter-suffixed, > 50 chars) so
    consecutive turns aren't loop-guard-deduped.
    """
    agent_ids = [f"a{i}" for i in range(n_agents)]
    agents = [agent_from_stream(pid, _varying_stream(pid))
              for pid in agent_ids]
    policy: Any
    if policy_name == "OpenChat":
        policy = OpenChatPolicy()
    elif policy_name == "Default":
        policy = DefaultPolicy()
    elif policy_name == "SingleResponder":
        policy = SingleResponderPolicy(responder_id=agent_ids[0])
    elif policy_name == "RoundRobin":
        policy = RoundRobinPolicy()
    else:
        raise ValueError(f"unknown policy: {policy_name}")
    room = LoomRoom(
        agents=agents,
        anchor_id=agent_ids[0],
        default_responder_id=agent_ids[0],
        policy=policy,
        journal_dir=journal_dir,
    )
    return room, agent_ids


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

class Bench:
    """Container that runs scenarios and accumulates :class:`CaseResult`."""

    def __init__(self, *, quick: bool, only: Optional[set[str]] = None) -> None:
        self.quick = quick
        self.only = only
        self.results: dict[str, dict[str, CaseResult]] = {}

    def _record(self, scenario: str, case: CaseResult) -> None:
        self.results.setdefault(scenario, {})[case.name] = case

    def _enabled(self, scenario: str) -> bool:
        return self.only is None or scenario in self.only

    # ---- micro ---------------------------------------------------------

    def run_micro(self) -> None:
        if not self._enabled("micro"):
            return
        # 1) bus.post (no subscribers)
        bus = MessageBus()
        self._record("micro", _bench(
            "bus.post / 0 sub",
            lambda: bus.post(ev.chat(sender="user", body="x")),
            iters=300, inner=200,
        ))
        # 2) bus.post (1 subscriber)
        bus2 = MessageBus()
        seen: list[int] = []
        bus2.subscribe(lambda e: seen.append(e.id))
        self._record("micro", _bench(
            "bus.post / 1 sub",
            lambda: bus2.post(ev.chat(sender="user", body="x")),
            iters=300, inner=200,
        ))
        # 3) Event.to_jsonl chat
        e = ev.chat(sender="alice", body="hello world", addressees=["bob"],
                    room_epoch=5)
        e.id = 7
        e.ts = 1_700_000_000.0
        self._record("micro", _bench(
            "Event.to_jsonl chat",
            lambda: e.to_jsonl(),
            iters=300, inner=200,
        ))
        # 4) Event.to_jsonl control
        c = ev.user_turn_opened(
            user_turn_id=1, routing_case="direct_mention",
            required_participants=["bob"], rationale="@bob")
        c.id = 9
        c.ts = 1_700_000_000.0
        self._record("micro", _bench(
            "Event.to_jsonl control",
            lambda: c.to_jsonl(),
            iters=300, inner=200,
        ))
        # 5) UserTurn.obligation_for hit & miss at varying N
        for n in (5, 25) if self.quick else (5, 10, 25, 50):
            ut = self._make_user_turn(n)
            target = f"p{n // 2}"
            self._record("micro", _bench(
                f"obligation_for hit N={n}",
                lambda ut=ut, t=target: ut.obligation_for(t),
                iters=300, inner=200,
            ))
            self._record("micro", _bench(
                f"obligation_for miss N={n}",
                lambda ut=ut: ut.obligation_for("nope"),
                iters=300, inner=200,
            ))
        # 6) actor._lookup_event-style snapshot+scan
        for size in (1_000, 10_000) if self.quick else (1_000, 10_000, 50_000):
            bus3 = self._seed_bus(size)
            target = size // 2
            def fetch(bus3=bus3, target=target):
                snap = bus3.snapshot()
                if 0 <= target < len(snap) and snap[target].id == target:
                    return snap[target]
                for e in snap:
                    if e.id == target:
                        return e
                return None
            self._record("micro", _bench(
                f"actor._lookup_event-style E={size}",
                fetch, iters=100, inner=10,
            ))
        # 7) _find_recent_chat_event_id-style
        for size in (1_000, 10_000) if self.quick else (1_000, 10_000, 50_000):
            bus4 = MessageBus()
            for i in range(size):
                bus4.post(ev.chat(sender="alice" if i % 2 == 0 else "bob",
                                   body=f"line {i}"))
            def find(bus4=bus4):
                snap = bus4.snapshot(channel="main", kinds=["chat"])
                for e in reversed(snap):
                    if e.sender == "alice":
                        return e.id
                return None
            self._record("micro", _bench(
                f"_find_recent_chat_event_id E={size}",
                find, iters=100, inner=5,
            ))

    @staticmethod
    def _make_user_turn(n: int) -> UserTurn:
        required = {f"p{i}" for i in range(n)}
        plan = UserTurnPlan(
            requires_response=True,
            routing_case="direct_mention",
            required_participants=required,
            rationale="bench",
        )
        obs: dict[int, ResponseObligation] = {}
        for i in range(n):
            ob = ResponseObligation(
                id=i + 1, participant_id=f"p{i}", level="must",
                target_event_ids=[0], reason="bench",
            )
            obs[ob.id] = ob
        return UserTurn(
            id=1, user_event_id=0, started_at=0.0,
            frozen_plan=plan, obligations=obs,
        )

    @staticmethod
    def _seed_bus(n: int) -> MessageBus:
        bus = MessageBus()
        for i in range(n):
            bus.post(ev.chat(sender="user", body=f"msg {i}"))
        return bus

    # ---- burst ---------------------------------------------------------

    def run_burst(self) -> None:
        if not self._enabled("burst"):
            return
        # Sustained post throughput, no subscribers
        bus = MessageBus()
        n_post = 1_000 if self.quick else 10_000
        def burst():
            for i in range(n_post):
                bus.post(ev.chat(sender="user", body=f"x{i}"))
        # Warm + measure: only one big iteration since each posts 10k events
        t0 = time.perf_counter_ns()
        burst()
        elapsed = time.perf_counter_ns() - t0
        per_op = elapsed / n_post
        self._record("burst", CaseResult(
            name=f"sustained-post no-sub N={n_post}",
            iters=1, inner=n_post,
            p50_ns=elapsed, p99_ns=elapsed, mean_ns=elapsed, min_ns=elapsed,
            extras={"per_op_ns": per_op, "events": n_post},
        ))
        # With 1 subscriber
        bus2 = MessageBus()
        seen: list[int] = []
        bus2.subscribe(lambda e: seen.append(e.id))
        def burst2():
            for i in range(n_post):
                bus2.post(ev.chat(sender="user", body=f"x{i}"))
        t0 = time.perf_counter_ns()
        burst2()
        elapsed = time.perf_counter_ns() - t0
        per_op = elapsed / n_post
        self._record("burst", CaseResult(
            name=f"sustained-post 1-sub N={n_post}",
            iters=1, inner=n_post,
            p50_ns=elapsed, p99_ns=elapsed, mean_ns=elapsed, min_ns=elapsed,
            extras={"per_op_ns": per_op, "events": n_post},
        ))

    # ---- history -------------------------------------------------------

    def run_history(self) -> None:
        if not self._enabled("history"):
            return
        sizes = [100, 1_000, 10_000] if self.quick else [100, 1_000, 10_000, 50_000, 100_000]
        for size in sizes:
            bus = self._seed_bus(size)
            self._record("history", _bench(
                f"snapshot full E={size}",
                lambda bus=bus: bus.snapshot(),
                iters=50 if size >= 50_000 else 100, inner=1,
            ))
            self._record("history", _bench(
                f"snapshot audience=bob E={size}",
                lambda bus=bus: bus.snapshot(audience="bob"),
                iters=50 if size >= 50_000 else 100, inner=1,
            ))

    # ---- cursor --------------------------------------------------------

    def run_cursor(self) -> None:
        if not self._enabled("cursor"):
            return
        sizes = [1_000, 10_000] if self.quick else [1_000, 10_000, 50_000]
        for size in sizes:
            bus = self._seed_bus(size)
            cases = [
                ("fresh", size - 10),    # cursor near head
                ("tail", size // 2),     # cursor at midpoint
                ("stale", size // 10),   # cursor at 10% — lots of new events
            ]
            for label, cursor in cases:
                self._record("cursor", _bench(
                    f"snapshot since={label} E={size} k={cursor}",
                    lambda bus=bus, k=cursor: bus.snapshot(since=k),
                    iters=100, inner=5,
                    extras={"cursor": cursor, "log_size": size, "tag": label},
                ))

    # ---- chat ----------------------------------------------------------

    def run_chat(self) -> None:
        if not self._enabled("chat"):
            return
        n_agents_axis = [1, 5, 10] if self.quick else [1, 2, 5, 10, 25]
        n_turns = 5 if self.quick else 20
        policies = ["OpenChat"] if self.quick else ["OpenChat", "Default", "SingleResponder"]

        for policy_name in policies:
            for n_agents in n_agents_axis:
                room, agent_ids = _make_room(n_agents, policy_name=policy_name)
                # Mention all agents to get every one of them into the obligation set
                mention = " ".join(f"@{a}" for a in agent_ids)
                with room:
                    # Warm
                    room.post_and_wait(f"{mention} hi", timeout=5.0)
                    samples: list[int] = []
                    for _ in range(n_turns):
                        t0 = time.perf_counter_ns()
                        room.post_and_wait(f"{mention} hi", timeout=10.0)
                        samples.append(time.perf_counter_ns() - t0)
                samples.sort()
                p50 = statistics.median(samples)
                p99 = samples[int(0.99 * (len(samples) - 1))]
                mean = statistics.fmean(samples)
                self._record("chat", CaseResult(
                    name=f"{policy_name} turn N_agents={n_agents}",
                    iters=len(samples), inner=1,
                    p50_ns=float(p50), p99_ns=float(p99),
                    mean_ns=mean, min_ns=float(samples[0]),
                    extras={"policy": policy_name, "n_agents": n_agents,
                            "turns": n_turns},
                ))

    # ---- chat with history ---------------------------------------------

    def run_history_chat(self) -> None:
        """Steady-state turn cost as a function of pre-existing log size."""
        if not self._enabled("history-chat"):
            return
        n_agents = 5
        sizes = [1_000, 10_000] if self.quick else [1_000, 10_000, 50_000]
        for size in sizes:
            room, agent_ids = _make_room(n_agents, policy_name="OpenChat")
            # Pre-load log via direct bus post (bypassing the policy/coord)
            bus = room.session.bus
            for i in range(size):
                bus.post(ev.chat(sender="user", body=f"prior {i}"))
            mention = " ".join(f"@{a}" for a in agent_ids)
            samples: list[int] = []
            with room:
                room.post_and_wait(f"{mention} hi", timeout=10.0)  # warm
                for _ in range(5):
                    t0 = time.perf_counter_ns()
                    room.post_and_wait(f"{mention} hi", timeout=10.0)
                    samples.append(time.perf_counter_ns() - t0)
            samples.sort()
            p50 = statistics.median(samples)
            p99 = samples[int(0.99 * (len(samples) - 1))]
            mean = statistics.fmean(samples)
            self._record("history-chat", CaseResult(
                name=f"OpenChat turn N_agents={n_agents} prior_log={size}",
                iters=len(samples), inner=1,
                p50_ns=float(p50), p99_ns=float(p99),
                mean_ns=mean, min_ns=float(samples[0]),
                extras={"prior_log": size, "n_agents": n_agents},
            ))

    # ---- stream --------------------------------------------------------

    def run_stream(self) -> None:
        if not self._enabled("stream"):
            return
        # We measure raw stream_delta post throughput here.
        # End-to-end token latency is covered by the chat scenario.
        bus = MessageBus()
        n_chunks = 1_000 if self.quick else 10_000
        # No-op subscriber so we measure the lock+notify path realistically
        seen: list[int] = []
        bus.subscribe(lambda e: seen.append(e.id))
        def post_burst():
            for _ in range(n_chunks):
                bus.post(ev.stream_delta(lease_id=1, participant_id="alice",
                                          text="x"))
        t0 = time.perf_counter_ns()
        post_burst()
        elapsed = time.perf_counter_ns() - t0
        per_op = elapsed / n_chunks
        self._record("stream", CaseResult(
            name=f"stream_delta post-rate 1-sub N={n_chunks}",
            iters=1, inner=n_chunks,
            p50_ns=elapsed, p99_ns=elapsed, mean_ns=elapsed, min_ns=elapsed,
            extras={"per_op_ns": per_op, "events": n_chunks,
                    "throughput_per_s": 1e9 / per_op},
        ))

    # ---- journal -------------------------------------------------------

    def run_journal(self) -> None:
        if not self._enabled("journal"):
            return
        sizes = [100, 1_000] if self.quick else [100, 1_000, 10_000]
        for size in sizes:
            with TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                j = Journal(session_dir=tmp_path)
                # Pre-populate events.jsonl
                with j.events_path.open("w") as f:
                    for i in range(size):
                        e = ev.chat(sender="alice" if i % 2 == 0 else "bob",
                                    body=f"msg {i}")
                        e.id = i
                        e.ts = 1_700_000_000.0 + i * 0.001
                        f.write(e.to_jsonl() + "\n")
                # Time the load
                self._record("journal", _bench(
                    f"Journal.load_events E={size}",
                    lambda j=j: j.load_events(),
                    iters=10, inner=1,
                ))
                # Memory peak via tracemalloc
                gc.collect()
                tracemalloc.start()
                _ = j.load_events()
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                self._record("journal", CaseResult(
                    name=f"Journal.load_events peak_bytes E={size}",
                    iters=1, inner=1,
                    p50_ns=0.0, p99_ns=0.0, mean_ns=0.0, min_ns=0.0,
                    extras={"peak_bytes": peak},
                ))

    # ---- memory --------------------------------------------------------

    def run_memory(self) -> None:
        if not self._enabled("memory"):
            return
        # Bytes per Event — chat and full
        sample_chat = ev.chat(sender="alice", body="hi", addressees=["bob"])
        sample_chat.id = 1
        sample_chat.ts = 1_700_000_000.0
        sample_full = ev.chat(
            sender="alice",
            body="hello world from alice with a bit more body",
            addressees=["bob", "carol"],
            channel="main",
            user_turn_id=42,
            room_epoch=7,
            meta={"lease_id": 99, "cost_tokens": 12},
        )
        sample_full.id = 2
        sample_full.ts = 1_700_000_001.0

        for label, sample in (("chat-simple", sample_chat),
                              ("chat-full", sample_full)):
            tracemalloc.start()
            instances = [ev.chat(sender=sample.sender, body=sample.body,
                                 addressees=list(sample.addressees),
                                 channel=sample.channel,
                                 user_turn_id=sample.user_turn_id,
                                 room_epoch=sample.room_epoch,
                                 meta=dict(sample.meta))
                         for _ in range(1_000)]
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            bytes_per = peak / len(instances)
            self._record("memory", CaseResult(
                name=f"bytes_per_event {label}",
                iters=1, inner=len(instances),
                p50_ns=0.0, p99_ns=0.0, mean_ns=0.0, min_ns=0.0,
                extras={"bytes_per_event": bytes_per,
                        "alloc_total_bytes": peak,
                        "n": len(instances)},
            ))
            del instances

        # RSS slope across a small chat session
        if psutil is not None:
            proc = psutil.Process()
            room, agent_ids = _make_room(2, policy_name="OpenChat")
            mention = f"@{agent_ids[0]} @{agent_ids[1]}"
            n_turns = 50 if self.quick else 200
            with room:
                room.post_and_wait(f"{mention} hi", timeout=5.0)  # warm
                gc.collect()
                rss_start = proc.memory_info().rss
                threads_start = threading.active_count()
                for _ in range(n_turns):
                    room.post_and_wait(f"{mention} hi", timeout=5.0)
                gc.collect()
                rss_end = proc.memory_info().rss
                threads_end = threading.active_count()
            self._record("memory", CaseResult(
                name=f"chat-session RSS slope turns={n_turns}",
                iters=1, inner=n_turns,
                p50_ns=0.0, p99_ns=0.0, mean_ns=0.0, min_ns=0.0,
                extras={
                    "rss_start_bytes": rss_start,
                    "rss_end_bytes": rss_end,
                    "rss_delta_bytes": rss_end - rss_start,
                    "rss_delta_per_turn": (rss_end - rss_start) / n_turns,
                    "threads_start": threads_start,
                    "threads_end": threads_end,
                },
            ))

    # ---- driver --------------------------------------------------------

    def run_all(self) -> None:
        self.run_micro()
        self.run_burst()
        self.run_history()
        self.run_cursor()
        self.run_journal()
        self.run_chat()
        self.run_history_chat()
        self.run_stream()
        self.run_memory()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _host_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
    }
    if psutil is not None:
        try:
            info["cpu_count_logical"] = psutil.cpu_count(logical=True)
            info["cpu_count_physical"] = psutil.cpu_count(logical=False)
            mem = psutil.virtual_memory()
            info["mem_total_bytes"] = mem.total
            info["mem_available_bytes"] = mem.available
        except Exception:  # pragma: no cover
            pass
    return info


def _git_rev() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        return "unknown"


def _to_dict(bench: Bench) -> dict[str, Any]:
    return {
        "host": _host_info(),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_rev": _git_rev(),
        "scenarios": {
            scen: {name: asdict(r) for name, r in cases.items()}
            for scen, cases in bench.results.items()
        },
    }


def _render_markdown(data: dict[str, Any]) -> str:
    lines = [
        "# Loom performance baseline",
        "",
        "Captured by `benchmarks/perf.py`. The JSON sibling file is the",
        "machine-readable record. `scripts/bench_diff.py` reads two such",
        "JSON files and prints a delta table; CI fails on >15% regression",
        "against the committed baseline.",
        "",
        "## Host",
        "",
    ]
    for k, v in data["host"].items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append(f"- ts: {data['ts']}")
    lines.append(f"- git: {data['git_rev']}")
    lines.append("")
    for scen, cases in data["scenarios"].items():
        lines.append(f"## {scen}")
        lines.append("")
        lines.append("| case | iters | inner | p50 | p99 | mean | extras |")
        lines.append("|---|--:|--:|--:|--:|--:|---|")
        for name, r in cases.items():
            extras = ", ".join(
                f"{k}={v if not isinstance(v, float) else round(v, 2)}"
                for k, v in r["extras"].items()
            )
            lines.append(
                f"| {name} | {r['iters']} | {r['inner']} | "
                f"{_fmt_ns(r['p50_ns'])} | {_fmt_ns(r['p99_ns'])} | "
                f"{_fmt_ns(r['mean_ns'])} | {extras} |"
            )
        lines.append("")
    return "\n".join(lines)


def _fmt_ns(ns: float) -> str:
    if ns >= 1_000_000_000:
        return f"{ns / 1_000_000_000:.2f} s"
    if ns >= 1_000_000:
        return f"{ns / 1_000_000:.2f} ms"
    if ns >= 1_000:
        return f"{ns / 1_000:.2f} μs"
    return f"{ns:.0f} ns"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--output", default="docs/perf-baseline.json",
                   help="Output JSON path (Markdown sibling next to it)")
    p.add_argument("--quick", action="store_true",
                   help="Use smaller axes (~30s vs full ~3-5min)")
    p.add_argument("--scenario", nargs="*",
                   help=("Restrict to specific scenarios "
                         "(micro burst history cursor chat history-chat "
                         "stream journal memory)"))
    args = p.parse_args()

    only = set(args.scenario) if args.scenario else None
    bench = Bench(quick=args.quick, only=only)
    bench.run_all()

    out_json = Path(args.output)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    data = _to_dict(bench)
    out_json.write_text(json.dumps(data, indent=2))

    out_md = out_json.with_suffix(".md")
    out_md.write_text(_render_markdown(data))

    # Console preview
    print(f"[perf] wrote {out_json} and {out_md}")
    for scen, cases in bench.results.items():
        print(f"\n=== {scen} ({len(cases)} cases) ===")
        for name, r in cases.items():
            print(f"  {name:<55} p50={_fmt_ns(r.p50_ns)}  p99={_fmt_ns(r.p99_ns)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
