# tests/subsystem — component & stress tests for Loom

This folder holds component-level and stress tests that go beyond the
function-level unit tests in `tests/`. The test files map to subsystem
boundaries (not module names) and exercise whole subsystems at once,
including adversarial-agent scenarios and breaking-point probes.

## Run

```bash
$PYTEST tests/subsystem/ -q
```

Every test in this folder runs by default. The four markers (`stress`,
`timing`, `disk`, `breakpoint`) are informational labels for filtering
— they are not skip-by-default gates.

## Files

| File | Subsystem | Tests | Notes |
|---|---|---|---|
| `test_event_pipeline.py`        | `MessageBus` + `Journal`       | ~12 | ordering under concurrent load, 1 MB payloads, replay correctness |
| `test_turn_control.py`          | `RoomCoordinator` + leases     | ~14 | concurrent posts, lease churn, debounce window |
| `test_drafting.py`              | `ParticipantActor` + streaming | ~12 | hostile agents (hang / garbage / infinite / errors) |
| `test_routing.py`               | All four bundled policies      | ~12 | adversarial inputs across `DefaultPolicy` / `OpenChatPolicy` / `RoundRobinPolicy` / `SingleResponderPolicy` |
| `test_full_room.py`             | `LoomRoom` end-to-end           | ~10 | 50-agent stress, console behavior, lifecycle churn |
| `test_invariants_under_load.py` | Boundary + read-only-view + charter checks at scale | ~6 | architectural invariants under N=50 actors |

## Conventions

- **Watchdog:** every test is capped at 45 s wall time via the autouse
  `watchdog_timer` fixture (uses `signal.alarm`; falls back to a
  `threading.Timer` interrupt). Override per-test with
  `@pytest.mark.watchdog(seconds=N)`.

- **Thread leak guard:** the autouse `assert_no_thread_leak` fixture
  fails any test that leaves a thread named `loom-actor-*` alive after
  teardown. Tests that intentionally spawn helper threads should give
  them distinct names.

- **Disk pressure:** never use `chmod` to simulate write failures. Use
  the `InMemoryFaultJournal` fixture, which subclasses `Journal` and
  injects an exception on the Nth write call. Real-disk tests use
  `tmp_path` only.

- **Verify-the-limit vs find-the-breaking-point:** most tests assert
  bounded behavior tied to documented config values. Tests prefixed
  `test_breakpoint_*` and tagged `@pytest.mark.breakpoint` measure the
  threshold at which behavior degrades — they binary-search starting
  small and capped at a hard ceiling, assert only a sanity floor, and
  emit the measured threshold via `print()` for human review.

- **Hostile agents:** the `adversarial_agent` factory produces
  `_FunctionAgent` instances built via the canonical adapters, so the
  streaming pipeline cannot tell them apart from real ones. Variants
  cover hang-after-first-delta, slow-first-delta, infinite stream
  (always bounded), garbage payloads, `None` yields, post-N-chunks
  raise, and chunk floods.

## Fixtures

See `conftest.py` for the canonical implementations. The most useful
ones in writing new tests:

- `room_factory` — yields a started `LoomRoom`; auto-stops on teardown.
- `bus_recorder` — attaches a subscriber that captures every event and
  exposes `.by_kind` / `.by_control_type` / `.count` helpers.
- `adversarial_agent` — factory with named hostile variants.
- `temp_journal` — `tmp_path`-based journal directory; teardown
  validates the JSONL is well-formed.
- `simple_agents(n)` — returns N `_FunctionAgent`s with long-enough
  replies that bypass the loop guard.
- `policy_throwing(raise_on_call=K)` — policy that raises on the Kth
  `plan_user_turn` call.
- `fake_clock` — opt-in monotonic clock fake (otherwise tests use real
  wall time with generous margins).
