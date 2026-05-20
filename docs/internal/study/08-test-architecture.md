# 08 — Test architecture

This is **Session 8** of the Loom kernel deep-study curriculum. Phase
D begins: validation. The repo ships **~20,175 LOC of tests across 6
tiers + 67 test files** — a 2.2:1 test-to-source ratio. Understanding
the tier structure, fixture vocabulary, and patterns is necessary
before we can confidently change kernel internals.

State as of Loom v0.1.2 (2026-05-08).

## Files covered

- All 5 conftests + `tests/property/strategies.py` (the shared
  Hypothesis generators).
- Sampled tests from each tier:
  `tests/test_kernel_bus.py`, `tests/test_kernel_coordinator.py`,
  `tests/test_kernel_default_policy.py`, `tests/test_kernel_journal.py`,
  `tests/subsystem/test_full_room.py`,
  `tests/property/test_lease_invariants.py`,
  `tests/property/test_security_fuzz.py`,
  `tests/perf/test_bench_bus.py`.

## Mental model

```
                            tests/
                              │
        ┌─────────────────────┼─────────────────────────────────┐
        ▼                     ▼                                 ▼
   ┌─────────┐         ┌──────────────┐                  ┌──────────────┐
   │ KERNEL  │         │ SUBSYSTEM    │                  │ SYSTEM       │
   │ ~21 files│        │ 6 files      │                  │ 9 files      │
   │ unit    │         │ component +  │                  │ E2E via      │
   │ tests   │         │ stress       │                  │ LoomRoom only│
   │ unittest│         │ pytest mix   │                  │ pytest, 90s  │
   │  default│         │  45s watchdog │                  │  watchdog +  │
   │  suite  │         │ thread-leak  │                  │  public-API  │
   │         │         │  guard auto  │                  │  discipline  │
   └─────────┘         └──────────────┘                  └──────────────┘
                              │
                              │ same machinery + extra
                              ▼
                     ┌──────────────┐
                     │ PROPERTY     │
                     │ 11 files     │
                     │ Hypothesis   │
                     │ ci/fast/     │
                     │  nightly     │
                     │ profiles     │
                     │ 60s watchdog │
                     └──────────────┘
                              │
                              │ smaller, focused
                              ▼
                     ┌──────────────┐         ┌──────────────┐
                     │ COVERAGE     │         │ PERF         │
                     │ 6 files      │         │ 5 files      │
                     │ rare paths   │         │ pytest -m perf│
                     │ 30s watchdog │         │  excluded by │
                     │              │         │  default     │
                     └──────────────┘         └──────────────┘

       ↓ default ``make test`` runs:
              kernel + subsystem + property + system + coverage
       ↓ ``make test-quick`` excludes perf
       ↓ ``make test-property`` only Hypothesis tier
       ↓ ``make test-coverage`` only rare-path tier
       ↓ ``make bench`` only perf tier (microbench + scenario suite)
       ↓ ``make test-full`` ~2h: fast + repeated + mutation
       ↓ ``make security-test`` security property + fuzz
```

---

## Tier purposes & file inventory

### Top-level kernel tests — 21 files (`tests/test_kernel_*.py`)

Pure-unit tests of kernel modules. Use **stdlib `unittest.TestCase`**
exclusively (NOT pytest functions). `setUp` for shared fixtures.
Imports go straight into `loom.kernel.*`; no facade indirection.

| File | Covers |
|---|---|
| `test_kernel_actor.py` | `ParticipantActor`, `_decide_once`, `_dispatch_decision`, trigger priority |
| `test_kernel_addressees.py` | `parse_addressees`, `last_responsible_speaker`, `_MENTION_RE` |
| `test_kernel_agent_adapter.py` | `agent_from_send/stream/object`, `SendProxyAdapter`, `_FunctionAgent` |
| `test_kernel_bus.py` | `MessageBus.post/snapshot/wait_after`, body cap, sender auth, subscribe |
| `test_kernel_coordinator.py` | `RoomCoordinator` end-to-end (1386 LOC — the largest test file) |
| `test_kernel_default_policy.py` | `DefaultPolicy` per-case branches (broadcast/mention/floor/game) |
| `test_kernel_events.py` | `Event` factories, validation, `redact_error_text` |
| `test_kernel_journal.py` | `Journal.open/snapshot/load/iter_events`, version compat |
| `test_kernel_kernel_boundary.py` | The 5 boundary invariants (Session 6) |
| `test_kernel_kernel_integration.py` | Multi-module integration without the facade |
| `test_kernel_obligations.py` | `UserTurnPlan.__post_init__`, plan-builders |
| `test_kernel_open_chat_policy.py` | `OpenChatPolicy` |
| `test_kernel_policy_base.py` | `BasicPolicy` template-method dispatch |
| `test_kernel_prompt.py` | `build_prompt` section assembly, fence rendering |
| `test_kernel_room.py` | `RoomState` + `RoomConfig` + view freeze |
| `test_kernel_room_facade.py` | `LoomRoom` (kernel-level — uses `RoomCoordinator` directly for assertions) |
| `test_kernel_round_robin_policy.py` | `RoundRobinPolicy` declarative state mutation |
| `test_kernel_runtime.py` | `build_loom_session`, slash commands, `post_user_text` |
| `test_kernel_single_responder_policy.py` | `SingleResponderPolicy` |
| `test_kernel_streaming.py` | `run_streaming_call` lifecycle, PASS, post-stream filters |
| `test_kernel_user_turn.py` | `UserTurn` lifecycle, debounce, `is_user_turn_complete` |

Style: no fixtures, no markers, lightweight; `unittest.TestCase`
classes group related assertions; helper module-level functions like
`_setup`, `_user_post`, `_open_default` shared inside the file.

### Subsystem — 6 files (`tests/subsystem/`)

Component-level + stress tests. Use **pytest functions + class
methods**, share rich fixtures, have a **45s watchdog** and an
**autouse actor-thread-leak guard**.

| File | Covers |
|---|---|
| `test_drafting.py` | `run_streaming_call` end-to-end with adversarial agents |
| `test_event_pipeline.py` | Bus event propagation + delivery guarantees |
| `test_full_room.py` | End-to-end `LoomRoom` with up to 30+ agents under stress |
| `test_invariants_under_load.py` | Concurrent multi-turn stress conditions |
| `test_routing.py` | Mention routing, addressee resolution, dead-letter fallback |
| `test_turn_control.py` | Turn transitions, cursor movement, lease mechanics |

### Property — 11 files (`tests/property/`)

Hypothesis-driven fuzz tests. Use `@given` / `@settings`. Profile
selection via env var `HYPOTHESIS_PROFILE` (default `ci`).

| File | Invariant tested |
|---|---|
| `strategies.py` | Shared `@composite` strategies (events, streams, participant_ids) |
| `test_bus_concurrent.py` | Concurrent `bus.post` ordering / contiguous ids |
| `test_capability_invariants.py` | Capable-agent fallback routing |
| `test_event_meta_no_render.py` | `meta` never renders to LLM prompts (Session 1 invariant) |
| `test_event_roundtrip.py` | `Event.to_jsonl ∘ from_jsonl == identity` |
| `test_journal_replay.py` | Journal append + replay idempotency |
| `test_lease_invariants.py` | Released/expired leases never validate; epoch-bump invalidates |
| `test_policy_plans.py` | Policy state-mutation invariants |
| `test_prompt_fence_fuzz.py` | `_render_system_field` fence integrity (Session 3) |
| `test_round_robin.py` | RoundRobin invariants under randomized inputs |
| `test_security_fuzz.py` | `Event.from_jsonl` / `restore_state` / `parse_addressees` / `redact_error_text` defensive paths |
| `test_throttle_fairness.py` | Throttle queue ordering, FIFO fairness |
| `test_ux_contracts.py` | Public surface stability (`loom.__all__`, no kernel reach-through) |

### System — 9 files (`tests/system/`)

End-to-end integration via `LoomRoom` only — driven through the
public surface. **90s watchdog**, **extended thread-leak guard**
(catches `loom-actor-*` AND `loom-journal-snapshot`), and a
**collection-time public-API discipline check** that fails if any
test file uses forbidden patterns like `session.coordinator.set_*`,
`session.bus.post(`, etc.

| File | Covers |
|---|---|
| `test_capacity_and_limits.py` | `max_responses`, lease cap, body size cap |
| `test_config_matrix.py` | All 4 policies × 2 streaming modes × 2 journal modes |
| `test_console_e2e.py` | Interactive console input/output |
| `test_lifecycle_and_recovery.py` | start, stop, restart, journal replay |
| `test_long_haul_sessions.py` | Multi-hour-equivalent stress |
| `test_mixed_agents_and_routing.py` | Dynamic add/remove agents, routing updates |
| `test_observability.py` | Control events, metrics, logging |
| `test_persistence_and_replay.py` | Journal append + replay, state snapshot |
| `test_resource_pressure.py` | Throttle, budget, body cap under load |

### Coverage — 6 files (`tests/coverage/`)

Targeted rare-path / branch-coverage tests. Smaller fixtures (just a
**30s watchdog**); tests use `monkeypatch` heavily to expose specific
code paths.

| File | Covers |
|---|---|
| `test_actor_recursive_failure.py` | Nested exception handling in actor threads |
| `test_coordinator_rare_states.py` | Edge transitions, race condition exposures |
| `test_journal_oserror_paths.py` | Disk I/O failures (permissions, full disk) |
| `test_misc_branches.py` | Boundary conditions, type coercions |
| `test_runtime_console_branches.py` | Console signal handling, keyboard interrupt |
| `test_user_turn_pathologicals.py` | Malformed user events, empty fields |

### Perf — 5 files (`tests/perf/`)

Microbench tier. **Excluded from default suite via `perf` marker**
(`pytestmark = pytest.mark.perf` at the top of each file). Run via
`make bench`. The tier ships with the kernel — no `pytest-benchmark`
dependency.

| File | Covers |
|---|---|
| `test_bench_actor.py` | Actor cursor position, event lookup latency |
| `test_bench_bus.py` | `bus.post` throughput (no/1 subscriber); `snapshot` full / tail-fresh / tail-stale / audience filter |
| `test_bench_coordinator.py` | `plan_user_turn` call latency through coord |
| `test_bench_events.py` | Event creation, `Event.to_jsonl` serialisation |
| `test_bench_journal.py` | Journal append, recovery replay cost |

---

## Fixture inventory by tier

### Watchdog architecture (all 5 tiers)

The same shape — `signal.SIGALRM` on Unix-main-thread, `threading.Timer`
fallback elsewhere. Tier-specific defaults:

| Tier | Default ceiling | Marker override |
|---|---:|---|
| Coverage | 30 s | `@pytest.mark.watchdog(seconds=N)` |
| Property | 60 s | `@pytest.mark.watchdog(seconds=N)` |
| Subsystem | 45 s | `@pytest.mark.watchdog(seconds=N)` |
| System | 90 s | `@pytest.mark.watchdog(seconds=N)` |
| Perf | (none — bench fixture controls iters/timing) | n/a |

Watchdogs are autouse — every test in the tier picks them up. The
SIGALRM handler raises `_WatchdogFired(...)` from inside the test;
the `signal.Timer` fallback uses `ctypes.PyThreadState_SetAsyncExc`
to inject the same exception onto the main thread.

### `tests/perf/conftest.py` (139 LOC)

| Item | Purpose |
|---|---|
| `BenchResult` (dataclass) | `name, iters, inner, ns_p50, ns_p99, ns_mean, ns_min, extras`; `per_op_ns()` method. |
| `_bench(fn, *, name, iters=200, inner=1, warmup=10, record=None)` | Times `fn`. **GC disabled** for the duration to avoid stop-the-world spikes; re-enabled + collected after. Returns `BenchResult`; attaches to `record.user_properties` if set. |
| `bench` (fixture) | Curried `_bench` that auto-records onto `request.node.user_properties` |
| `pytest_runtest_makereport` (hookwrapper) | Collects bench results across the run |
| `pytest_terminal_summary` | Prints a summary table at session end (name, iters, p50/p99/mean ns) |

`pytestmark = pytest.mark.perf` at the top of every perf test file is
how they get excluded from the default suite (default `pytest -q`
reads `pyproject.toml`'s implicit `-m "not perf"` from the
`addopts = "--strict-markers -ra"` semantic + the `perf` marker
description).

### `tests/coverage/conftest.py` (60 LOC)

Just the watchdog (30s default). The targeted tests don't need the
heavy adversarial / multi-room fixtures from subsystem and system —
they exercise specific code paths via `monkeypatch`.

### `tests/property/conftest.py` (77 LOC)

| Item | Purpose |
|---|---|
| `settings.register_profile("fast", max_examples=25, deadline=1000)` | Inner-loop runs |
| `settings.register_profile("ci", max_examples=100, deadline=2000)` | Default; runs in fast suite |
| `settings.register_profile("nightly", max_examples=2000, deadline=10_000, suppress_health_check=[HealthCheck.too_slow])` | Release-rhythm |
| `settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))` | Profile selection |
| `property_watchdog` (autouse) | 60s default |

Add `HYPOTHESIS_PROFILE=fast` for inner loop, `=nightly` for release.

### `tests/property/strategies.py` (198 LOC)

Shared `@composite` Hypothesis strategies — central so adding a new
event kind means updating one file.

| Atom / strategy | Type | Purpose |
|---|---|---|
| `participant_alphabet` | str | `lowercase + digits + "_"` |
| `participant_ids` | `text` | non-empty, max 12, NOT all digits |
| `channels` | `one_of` | `"main"` or `"dm:<pid>"` |
| `bodies_chat` | `text` | max 80 chars |
| `short_text` | `text` | max 20 chars |
| `chat_events()` | `@composite` | A `chat` Event (id/ts left at zero) |
| `control_events()` | `@composite` | One of 14 control_types built via the factory |
| `stream_events()` | `@composite` | `start`/`delta`/`end` |
| `events` | `one_of(chat,control,stream)` | Any kind |
| `event_streams(min_size, max_size)` | `@composite` | List of events with monotonic ids/ts assigned |
| `equal_ignoring_id_ts(a, b)` | helper | Compare two events ignoring bus-assigned fields |

### `tests/subsystem/conftest.py` (576 LOC)

The subsystem tier's fixture toolbox.

| Fixture | Type | Purpose |
|---|---|---|
| `watchdog_timer` | autouse | 45s default; SIGALRM + Timer fallback |
| `assert_no_thread_leak` | autouse | Snapshots `loom-actor-*` thread names before, asserts none survived after (2s grace) |
| `thread_harness` | factory | `_ThreadHarness.spawn(target, name=...)` + `join_all(timeout=5)`; captures exceptions; fails test if any thread alive after teardown |
| `temp_journal` | tmp_path-backed | Yields a `Journal` rooted at `tmp_path/journal`; teardown verifies every line of `events.jsonl` parses as JSON |
| `InMemoryFaultJournal(Journal)` | class | Subclass that fails its Nth `write` call. Beats fragile filesystem chmod on shared FS |
| `adversarial_agent` | factory | `_AdversarialAgentFactory` — 7 hostile agent variants: `hang_after_first_delta`, `slow_first_delta`, `infinite_stream`, `garbage_payload`, `yields_none`, `raises_after_chunks`, `flood_chunks` |
| `room_factory` | factory | Builds a started `LoomRoom`; auto-stops every produced room on teardown (10s timeout) |
| `bus_recorder` | recorder | Bus subscriber capturing every Event; `events`, `by_kind`, `by_control_type`, `count` query helpers |
| `policy_throwing` | factory | Build a `ConversationPolicy` that raises on the Nth `plan_user_turn` call; subsequent calls return ack |
| `fake_clock` | monkeypatch | Patches `time.monotonic` and `time.time` to a controllable `_FakeClock`; `now()`, `advance(seconds)` |
| `simple_agents(n)` | factory | N healthy agents with reply text long enough to bypass loop-guard short-text threshold |

Markers registered: `stress`, `timing`, `disk`, `breakpoint`,
`watchdog(seconds)` — labels for filtering, not skip-by-default
gates.

### `tests/system/conftest.py` (884 LOC)

System-tier fixtures. Inherits + re-exports `InMemoryFaultJournal`
and `_AdversarialAgentFactory` from `tests.subsystem.conftest`.

| Fixture | Type | Purpose |
|---|---|---|
| `system_watchdog` | autouse | 90s default; same shape as subsystem's 45s |
| `assert_no_thread_leak_extended` | autouse | Strict superset — catches `loom-actor-*` AND `loom-journal-snapshot` (3s grace) |
| `multi_turn_session` | factory | Builds + starts an `LoomRoom` for multi-turn workloads; `lift_throttle=True` default replaces `_throttle` with 10k/min |
| `journaled_room` | tmp_path-backed | Builds an `LoomRoom` bound to `tmp_path/session`; auto-stops; validates JSONL on teardown |
| `restart_helper` | factory | Stops `old_room`, polls for state file flush, builds a NEW room over the same journal_dir; returns `(new_room, restored_state)` |
| `event_recorder` | recorder | Like `bus_recorder` but with `by_sender`, `snapshot_at(predicate)` |
| `mixed_agent_room` | factory | Build a room mixing N healthy agents with adversarial: `("hang", N), ("garbage", N), …` mapping onto `_AdversarialAgentFactory` |
| `scripted_console` | factory | `_ConsoleScript(lines)` — drives `run_console`'s `prompt_fn` with a list; non-string entries (`EOFError`, `KeyboardInterrupt`) signal exit |
| `varied_agents(n)` | factory | Healthy agents whose replies vary by per-call counter so loop-guard doesn't dedup |
| `slow_policy_factory` | factory | Build a policy that sleeps `sleep_ms` per call and/or raises on N-th call (1-indexed) |
| `config_factory` | factory | DSL for `RoomConfig` deltas — every field overridable as kwarg |
| `multi_room_factory` | factory | Spawn N concurrent `LoomRoom` instances with distinct journals (under `tmp_path/room_<i>`) |
| `binary_search` (session-scoped) | helper | Bisection helper for break-point probes (find smallest N exceeding threshold) |
| `fake_clock` | monkeypatch | Re-export of subsystem's |

**Critical**: `pytest_collection_modifyitems` runs at collection time
and **fails the collection** if any system-tier test file uses a
**forbidden API pattern**:

```python
_FORBIDDEN_API_PATTERNS = (
    "session.coordinator.set_",  "session.coordinator.register_",
    "session.coordinator.unregister_",  "session.coordinator.acquire_lease",
    "session.coordinator.release_lease",  "session.coordinator.open_user_turn",
    "session.coordinator.close_user_turn",  "session.coordinator.handle_skip",
    "session.coordinator.on_stream_end",  "session.state.set_",
    "session.state.add_participant",  "session.state.remove_participant",
    "session.state.advance_round_robin",  "session.bus.post(",
    "session.bus.stop(",  "session.add_agent(",  "session.remove_agent(",
    "session.start(",  "session.stop(",
)
```

System tests must drive the kernel through `LoomRoom` only;
`room.session.*` is for **observation**. Allowed: `bus.subscribe`,
`bus.snapshot`, `journal.load_state`, `journal.load_events`,
`state.participants` (read), etc.

`_lift_room_throttle(room)` is a documented test-only seam that
replaces `room.session.coordinator._throttle` with a 10k/min
ceiling — so multi-turn tests don't fight the default 10/min
per-participant rate limiter. The throttle behavior is exercised on
its own in `test_resource_pressure.py` without lifting.

---

## Style patterns

### Kernel tier — `unittest.TestCase` with module-level helpers

```python
"""Tests for ``loom.kernel.coordinator`` — RoomCoordinator + TurnLease."""
from __future__ import annotations
import unittest

from loom.kernel.coordinator import RoomCoordinator, ...

def _setup(*, default_responder=None, members=("loom","claude_code","gemini_cli"), config=None):
    bus = MessageBus()
    state = RoomState(config=config or RoomConfig(...))
    for i, pid in enumerate(members):
        state.add_participant(ParticipantInfo(id=pid, cost_tier=i))
    if default_responder:
        state.set_default_responder(default_responder)
    return bus, state, RoomCoordinator(bus, state)

def _user_post(bus, body="hi", addressees=None): ...
def _open_with(c, e, *, required, ...): ...

class LoopGuardTests(unittest.TestCase):
    def test_first_reply_passes(self):
        g = LoopGuardConfig()
        self.assertFalse(g.is_idle_dup("a", "standing by"))
    ...
```

### Property tier — Hypothesis `@given` + tier conftest fixture

```python
"""Property: lease lifecycle invariants under arbitrary action sequences."""
from hypothesis import given
from hypothesis import strategies as st

@given(n_acquire_release=st.integers(min_value=1, max_value=10))
def test_released_lease_never_validates(n_acquire_release):
    bus, coord = _make_coord()
    try:
        for _ in range(n_acquire_release):
            e = _open_turn_for(coord, "alice")
            lease = coord.acquire_lease("alice", e.id, is_direct_mention=True)
            assert lease is not None
            assert coord.validate_lease(lease) is True
            coord.release_lease(lease)
            assert coord.validate_lease(lease) is False
            coord.close_user_turn("cancelled")
    finally:
        bus.stop()
```

### Subsystem tier — pytest classes with markers + conftest fixtures

```python
class TestManyAgentRoom:

    @pytest.mark.stress
    def test_30_agents_one_turn_completes_within_watchdog(
            self, room_factory, simple_agents):
        agents = simple_agents(30)
        room = room_factory(agents=agents, policy=OpenChatPolicy())
        replies = room.post_and_wait("broadcast question", timeout=20.0)
        senders = {r.sender for r in replies}
        assert len(senders) >= 25
```

Notes on the style:
- **Class-grouped** for related tests (e.g. `TestManyAgentRoom`,
  `TestConsoleAndNotify`).
- **Fixtures injected** into each method (`room_factory`, `simple_agents`).
- **Markers** (`@pytest.mark.stress`, `.timing`, `.disk`, `.breakpoint`)
  are **labels for filtering**, not skip-by-default — every subsystem
  test runs by default.

### System tier — ONLY through `LoomRoom`

```python
class TestLifecycleAndRecovery:

    def test_journal_state_after_many_turns_restorable(
            self, journaled_room, varied_agents, restart_helper):
        agents = varied_agents(2)
        room = journaled_room(agents=agents, policy=OpenChatPolicy())
        for i in range(5):
            room.post_and_wait(f"turn {i}", timeout=5.0)
        # Reads via observation surface only:
        live_topic = room.session.state.topic        # ALLOWED — read
        live_pids = sorted(room.session.state.participants.keys())  # ALLOWED — read
        # FORBIDDEN: room.session.coordinator.set_*, room.session.bus.post(...)
        new_room, restored = restart_helper(
            room, agents=agents, policy=OpenChatPolicy())
        assert restored["topic"] == live_topic
```

### Coverage tier — monkeypatch to hit specific branches

```python
def test_journal_write_failure_emits_journal_error(
        coverage_watchdog, monkeypatch, tmp_path):
    j = Journal(tmp_path)
    j.open()
    real_write = j._events_file.write

    def _fail_write(s):
        raise OSError("disk full")

    monkeypatch.setattr(j, "_events_file", types.SimpleNamespace(
        write=_fail_write, flush=lambda: None, close=lambda: None,
    ))
    j.set_failure_callback(lambda exc: ...)
    j.on_event(make_test_event())
    assert j.degraded
```

### Perf tier — `bench` fixture from conftest

```python
pytestmark = pytest.mark.perf

def test_bus_post_hot(bench):
    bus = MessageBus()
    bench(lambda: bus.post(ev.chat(sender="user", body="x")),
          name="bus.post / no-subscribers", iters=500, inner=200)
```

The `bench` fixture is the curried `_bench`. Recorded results are
auto-attached to `request.node.user_properties` and printed in the
session-end summary table.

---

## Markers (from `pyproject.toml`)

```toml
[tool.pytest.ini_options]
markers = [
    "stress: heavy multi-turn or large-N load",
    "timing: timing-sensitive (sleeps, monotonic clock)",
    "disk: writes to on-disk journal",
    "breakpoint: limit-finding probe (prints measured threshold)",
    "watchdog: per-test watchdog override",
    "property: Hypothesis-driven fuzz test",
    "slow: > 5s wall-clock",
    "perf: opt-in microbench; excluded from default suite (run via make bench)",
]
```

`addopts = "--strict-markers -ra"` — undeclared markers fail
collection. So new markers must be added here AND registered via
`pytest_configure` in the relevant tier's conftest.

---

## Mutation testing — `mutmut`

```toml
[tool.mutmut]
paths_to_mutate = ["loom/kernel/", "loom/policy/", "loom/adapters.py"]
tests_dir = ["tests/", "tests/subsystem/"]
pytest_add_cli_args = ["-x", "-q", "-p", "no:cacheprovider",
                       "-m", "not stress and not breakpoint"]
```

- **Paths mutated**: kernel + policy + adapters (NOT runtime/room/
  errors/messages/contracts/testing).
- **Tests run**: top-level + subsystem (NOT property / coverage /
  perf — those are too slow / probabilistic for mutation).
- **`-x -q`**: stop at first failure, quiet.
- **Exclusions**: `stress` and `breakpoint` (too slow / inherently
  CPU-bound).

Run via `make mutation` (long-running); `make mutation-show` for last
results. Baseline: `docs/internal/mutation-baseline.txt` (43.6%
kill rate on pilot per Session 0). Survivors triaged in
`docs/internal/mutation-survivors.md`.

---

## Coverage gate

```toml
[tool.coverage.run]
source = ["loom"]
branch = true
data_file = "/tmp/.coverage_loom"

[tool.coverage.report]
fail_under = 98
exclude_lines = [
    "pragma: no cover",
    "if TYPE_CHECKING:",
    "raise NotImplementedError",
    "if __name__ == .__main__.:",
]
```

**Branch coverage 98%** is the gate. `make test` runs the suite +
gate. `make coverage-html` writes an HTML report to
`/tmp/coverage_html/`. The `if TYPE_CHECKING:` exclusion covers
modules like `loom/errors.py` (the static-checker import block isn't
run at runtime).

Tests under `tests/coverage/` exist specifically to push hard-to-hit
branches over the line.

---

## Makefile target → tier mapping

| Makefile target | What runs | Time |
|---|---|---|
| `test` | Default suite (kernel + subsystem + property + system + coverage) + 98% branch gate | ~30s |
| `test-quick` | Same but no perf | <30s |
| `test-property` | Only `tests/property/` | ~1m (`ci` profile) |
| `test-coverage` | Only `tests/coverage/` | ~30s |
| `test-full` | Default + repeated + mutation | ~2h |
| `bench` | Microbench (`tests/perf/`) + scenario suite (`benchmarks/perf.py`) | ~3-5m |
| `bench-quick` | `benchmarks/perf.py --quick` | ~30s |
| `bench-micro` | Only `tests/perf/` | ~30s |
| `bench-diff` | `scripts/bench_diff.py BASELINE CURRENT` | seconds |
| `bench-baseline` | Capture fresh baseline at `docs/perf-baseline.{json,md}` | ~3-5m |
| `bench-soak` | Long-run reliability (`tests/perf/ -k soak`) | ~1h |
| `security-test` | Security property + fuzz suite (4 specific files) | ~30s |
| `security-bench` | `bench/adversarial/` (DoS, tampered replay) | ~1m |
| `ux-check` | `tests/property/test_ux_contracts.py` + symbol count | seconds |
| `lint` | `ruff check` + `ruff format --check` + `mypy` | seconds |
| `mutation-show` | Last `mutmut` results | instant |
| `coverage-html` | Render HTML report under `/tmp/coverage_html/` | seconds |

---

## How to add a test in each tier

The decision tree:

```
Is the change in: kernel internal (e.g. one method on RoomCoordinator)?
    → Add a test class in tests/test_kernel_coordinator.py
    → unittest.TestCase + the file's existing _setup/_user_post helpers

Is the change in a policy?
    → Add to tests/test_kernel_<policy_name>_policy.py

Did the change involve cross-module composition (e.g. actor + bus + coord)?
    → tests/subsystem/test_<area>.py (use room_factory, simple_agents,
      assert_no_thread_leak runs autouse)

Does the change have an INVARIANT property over arbitrary inputs?
    → tests/property/test_<area>.py with @given + strategies.events

Should the change be observable from the public LoomRoom surface only?
    → tests/system/test_<area>.py
    → driven through LoomRoom; cannot use forbidden patterns (will
      fail collection)

Is the change a rare-path / branch you specifically need to cover for
the 98% gate?
    → tests/coverage/test_<area>.py with monkeypatch

Did the change affect a hot-path operation (post, snapshot, plan_user_turn)?
    → tests/perf/test_bench_<area>.py with the bench fixture
    → pytestmark = pytest.mark.perf at the top
```

---

## Invariants (this session's additions)

166. **Top-level kernel tests use `unittest.TestCase` exclusively**.
     Subsystem and below use pytest functions/classes. The kernel
     tests pre-date the pytest fixture infrastructure and intentionally
     keep the unittest style for "no fixtures, no markers,
     lightweight" feel.
167. **Watchdog ceilings are tier-specific**: 30s coverage, 45s
     subsystem, 60s property, 90s system. Override per-test with
     `@pytest.mark.watchdog(seconds=N)`. Perf has no watchdog —
     bench fixture controls timing.
168. **`assert_no_thread_leak` is autouse in subsystem; the
     extended version in system also catches `loom-journal-snapshot`**.
     Both have a 2-3s grace period for daemon threads to wind down
     on their own before failing.
169. **`pyproject.toml`'s `addopts = "--strict-markers"`** means
     undeclared markers fail collection. Adding a marker requires
     updating both `pyproject.toml` AND the relevant conftest's
     `pytest_configure`.
170. **System tier enforces public-API discipline at collection
     time** via `pytest_collection_modifyitems` + a forbidden-pattern
     list. System tests CANNOT call `session.coordinator.set_*`,
     `session.bus.post(`, etc. Only `LoomRoom` methods + observation
     of `session.state` / `session.bus.subscribe`.
171. **`_lift_room_throttle(room)` is a documented test-only seam**
     in system + subsystem tiers that replaces the throttle with a
     10k/min ceiling. Used by every multi-turn fixture by default
     (`lift_throttle=True`). The throttle's behavior itself is
     tested without lifting in `test_resource_pressure.py`.
172. **Hypothesis profiles**: `fast` (25 examples, 1s deadline),
     `ci` (100, 2s — default), `nightly` (2000, 10s,
     `suppress_health_check=[too_slow]`). Selected via
     `HYPOTHESIS_PROFILE` env var.
173. **Strategies are centralised in `tests/property/strategies.py`**
     so adding a new event kind means updating ONE file. `events =
     one_of(chat_events, control_events, stream_events)`;
     `event_streams` assigns monotonic ids/ts so they compose with
     bus tests.
174. **Perf tier is excluded from default suite via the `perf`
     marker** (`pytestmark = pytest.mark.perf` at file top). `make
     bench` and `make bench-micro` are the only entry points.
     Bench results are recorded onto `request.node.user_properties`
     and printed in the session-end terminal summary.
175. **`_bench` disables GC for the duration of timing** to avoid
     stop-the-world spikes. Re-enables + collects after — preserves
     test isolation.
176. **Mutation testing mutates `loom/kernel/`, `loom/policy/`,
     `loom/adapters.py`** but runs only top-level + subsystem
     tests (excludes property / coverage / perf / stress /
     breakpoint as too slow or probabilistic).
177. **Coverage gate is 98% branch** (`fail_under = 98` in
     `pyproject.toml`). `tests/coverage/` exists specifically to
     push hard-to-hit branches over the line. `if TYPE_CHECKING:`
     blocks are excluded.
178. **`InMemoryFaultJournal` beats filesystem chmod for fault
     injection** on shared filesystems (Lustre, NFS). It's a
     `Journal` subclass that monkeypatches `_events_file` with a
     `_FailingFile` that raises on the Nth `write`.
179. **`_AdversarialAgentFactory` has 7 hostile shapes**:
     `hang_after_first_delta`, `slow_first_delta`, `infinite_stream`,
     `garbage_payload`, `yields_none`, `raises_after_chunks`,
     `flood_chunks`. Re-exported in system conftest from subsystem.
180. **The `bench` fixture in perf curries `_bench` AND records
     onto `request.node.user_properties`** — so the same call site
     produces both an immediate `BenchResult` return value AND
     terminal-summary output without extra plumbing.
181. **`fake_clock` patches BOTH `time.monotonic` AND `time.time`**
     to a controllable `_FakeClock`. Any test that doesn't use
     this fixture continues to use the real clock. Re-exported in
     system from subsystem.
182. **The forbidden patterns list in
     `tests/system/conftest.py:_FORBIDDEN_API_PATTERNS`** enumerates
     19 specific reach-throughs that fail collection. Adding a new
     `LoomRoom` method that wraps a coordinator call REQUIRES
     updating this list.
183. **`pytest_configure` per-conftest declares tier markers**
     redundantly** (the markers also live in `pyproject.toml`). The
     redundancy is intentional — `--strict-markers` fails fast on
     undeclared markers, so the tier conftest's declaration acts
     as a safety net.

---

## Verification

> *Given a hypothetical change to coordinator's dead-letter logic
> (`_transfer_required_obligations_locked`), name (a) which existing
> tests would fail, (b) which tier the new regression test belongs to,
> (c) what fixture you'd reuse.*

**Hypothetical change**: we modify
`_transfer_required_obligations_locked` to transfer ALL of a removed
participant's must/should obligations rather than just the first
(open question 3 from Session 5 — current behavior collapses
multiple obligations onto the same fallback).

### (a) Tests that would fail

Walking the cross-references and grep targets:

1. **`tests/test_kernel_coordinator.py`** — the largest file. Look
   for `unregister_participant`, `dead_letter`, `transfer`,
   `obligation_recorded`, `rerouted_from_`. The `UserTurnLifecycle`
   class probably has tests like
   `test_remove_participant_transfers_obligation` /
   `test_remove_participant_dead_letters`. Anything that asserts the
   COUNT of transferred obligations (likely "exactly one") would
   fail because we now transfer N.

2. **`tests/subsystem/test_routing.py`** — covers "mention routing,
   addressee resolution, dead-letter fallback" per its docstring.
   End-to-end multi-mention scenarios with mid-turn removal would
   produce different `obligation_recorded` event counts; tests
   asserting the count would fail.

3. **`tests/property/test_lease_invariants.py`** has
   `test_epoch_bump_invalidates_existing_leases` which exercises
   add+remove cycles. Doesn't assert on obligation count, but if a
   property test like `test_unregister_resolves_or_transfers_all_required`
   exists in the property tier, it might now have a different
   "always exactly one transfer per removal" property failing.

4. **`tests/system/test_mixed_agents_and_routing.py`** — the
   docstring mentions "dynamic add/remove agents, routing updates".
   System tests observing `event_recorder.by_control_type("obligation_recorded")`
   counts after an unregister would fail.

5. **`tests/coverage/test_coordinator_rare_states.py`** likely has
   a "transfer obligation when removed participant has multiple
   must obligations" test — that's exactly the rare path our change
   alters.

6. **Mutation testing**: any mutant that comments out the
   `return` after the first transfer will now be killed (good — it
   was a survivor previously). Update
   `docs/internal/mutation-survivors.md`.

### (b) Tier for the new regression test

The new behavior is **a kernel-internal contract change**: how the
coordinator handles multiple obligations on a removed participant.

- **Primary location**: `tests/test_kernel_coordinator.py` — add a
  new `unittest.TestCase` class like
  `class MultipleObligationTransfer(unittest.TestCase)` with cases
  covering (i) two must obligations, (ii) must + should, (iii) all
  fallbacks unavailable, (iv) one fallback already drafted.
- **Property test**: `tests/property/test_policy_plans.py` or a new
  `tests/property/test_dead_letter_invariants.py` with a Hypothesis
  test like "after `unregister_participant(pid)`, every must
  obligation held by `pid` has either resolved OR been transferred
  to a live fallback in `allowed_speakers`."
- **System test (defensive)**: `tests/system/test_mixed_agents_and_routing.py`
  — add a scenario where a multi-mention turn has one mentioned
  agent removed mid-turn; assert ALL its must obligations are
  rerouted (visible via the recorded `obligation_recorded` events
  with `reason="rerouted_from_<pid>"`).

### (c) Fixtures to reuse

For the unit tier (kernel test): the file's `_setup`, `_user_post`,
`_open_with` helpers — already established the room with 3
participants, lets you `c.unregister_participant(pid)` directly.

For the subsystem tier:
- `room_factory` (build a started room with N agents)
- `bus_recorder` (capture every `obligation_recorded` event with the
  `rerouted_from_*` reason)
- `simple_agents` (N healthy agents)

For the property tier: import strategies from
`tests.property.strategies` — `participant_ids`, `chat_events`. Build
the test as "for any plan with N must obligations on a single agent,
after remove that agent, count the obligation_recorded events with
`reason.startswith('rerouted_from_')` — must equal N (or 0 if no
fallback)".

For the system tier:
- `multi_turn_session` (gives you a room with the throttle lifted)
- `event_recorder` (with `by_control_type("obligation_recorded")`
  helper)
- `varied_agents(n)` (N healthy agents)
- The autouse `assert_no_thread_leak_extended` will catch any
  side-effect leak.

For the coverage tier:
- The `coverage_watchdog` (autouse).
- A `monkeypatch` to inject test-controlled state (e.g. force the
  `cheapest_active_capable` to return None mid-removal to exercise
  the "no fallback" branch).

---

## Cross-references

- depends on: every prior session — tests exercise the kernel/policy
  surfaces from Sessions 1–7. The boundary tests (Session 0
  invariants 1–7) live in `tests/test_kernel_kernel_boundary.py`.
- depended on by:
  - `Makefile` (Session 9) — drives the tiers via the targets above.
  - `.github/workflows/ci.yml` (Session 9) — runs `pytest -q
    --maxfail=5` across Python 3.11 / 3.12 matrix.
  - `docs/internal/coverage-baseline.txt` (98% target).
  - `docs/internal/mutation-baseline.txt` and `mutation-survivors.md`.

## Open questions / things to revisit

1. **`tests/test_kernel_coordinator.py` is 1386 LOC**. Largest test
   file by far. Some classes are genuinely shared-state, but
   refactoring into per-area files (lease/, obligation/,
   user_turn/, dead_letter/) might help navigability. Cosmetic.
2. **`InMemoryFaultJournal` only fails the Nth WRITE.** No coverage
   for failures during `_write_snapshot_dict` (snapshot writes go
   through a different path). Worth adding a `fail_at_snapshot`
   variant.
3. **Mutation testing excludes property / coverage / perf** —
   property tests are probabilistic so a mutant that breaks one
   Hypothesis example might pass another. Coverage tests are too
   targeted to be representative. Perf tests don't assert
   correctness. The exclusion is correct but means kernel paths
   only-tested by property tier (e.g. some `Event.from_jsonl` edge
   cases) might have undetected mutants.
4. **System tier's `_FORBIDDEN_API_PATTERNS` is grep-based** — a
   future test that uses `s = room.session; s.coordinator.set_topic(...)`
   would slip through (the alias breaks the substring match).
   Worth strengthening to AST-based detection in v0.2.
5. **The `bench` fixture's `_bench` disables GC for the entire
   `iters * inner` window**. For very long benches (default 200 *
   1 = 200 calls), this is fine. For pathological cases (10k *
   100 = 1M calls), GC-disabled may exhaust memory. Consider a
   GC-on-warmup-only mode.
6. **Hypothesis `nightly` profile (2000 examples, 10s deadline)** is
   not run in CI today (CI uses `ci` profile = 100 examples). When
   we tighten security guarantees, consider running `nightly` in a
   weekly schedule.
7. **Coverage tier is 6 files but `pyproject.toml`'s `fail_under =
   98`** suggests the bulk of branches are hit by the kernel +
   subsystem tier. Verify the marginal branches the coverage tier
   covers — they are the ones a kernel-modification PR most likely
   regresses.
8. **`tests/perf/conftest.py:_collected` is module-global** and
   accumulates across the session. Multiple pytest invocations within
   the same process (uncommon but possible) would double-count. Worth
   refactoring to a session-scoped fixture, but cosmetic.
9. **`assert_no_thread_leak` and `assert_no_thread_leak_extended`
   have 2s and 3s grace periods**. A test that legitimately spawns
   actors and stops them within the window passes; a test that leaks
   for less than the grace period passes too. Tightening would
   require slower tests but tighter guarantees.
10. **No conftest at `tests/conftest.py` (root)**. All fixtures are
    tier-scoped. If a future fixture needs to be shared across all
    tiers, the natural location would be `tests/conftest.py`
    — there's no precedent in the current structure.
