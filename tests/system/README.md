# tests/system — whole-kernel system tests for Loom

This folder holds **system-level** tests — the third tier above
`tests/test_kernel_*.py` (unit) and `tests/subsystem/` (component-pair).
Every test in here drives the assembled kernel as a whole through the
**public API only**: `LoomRoom`, the bundled adapters, `RoomConfig`,
the four bundled policies, and the advanced `build_loom_session` /
`Journal` / `restore_state` surface.

## Run

```bash
$PYTEST tests/system/ -q
```

Every test runs by default. The five markers (`stress`, `timing`,
`disk`, `breakpoint`, `watchdog`) are informational labels; they are
not skip-by-default gates.

## Distinguishing principle from the subsystem tier

Subsystem tests reach into `room.session.coordinator.acquire_lease(...)`,
`room.session.state.set_topic(...)`, etc. to drive specific subsystems.
System tests use only `LoomRoom.post`, `post_and_wait`, `add_agent`,
`remove_agent`, `run_console`, `start`, `stop`. `room.session.*` is
allowed for **observation** (`bus.subscribe`, `bus.snapshot`,
`journal.load_state`) — never for state mutation.

## Files

| File | What it covers | Tests |
|---|---|---|
| `test_long_haul_sessions.py`     | 50–100 turn realistic flows           | 12 |
| `test_lifecycle_and_recovery.py` | cold-start → restart → resume         | 11 |
| `test_persistence_and_replay.py` | journal across all event kinds        | 11 |
| `test_mixed_agents_and_routing.py` | adapters + policies + adversarials  | 12 |
| `test_console_e2e.py`            | scripted `run_console`                | 12 |
| `test_observability.py`          | external bus subscribers + watchdog   | 10 |
| `test_config_matrix.py`          | `RoomConfig` × policy × error_mode    | 11 |
| `test_resource_pressure.py`      | throttle / compaction / loop-guard    | 9  |
| `test_capacity_and_limits.py`    | system-wide break-point probes        | 9  |

97 tests total. 7 break-point probes that print a measured threshold.

## Conventions

- **Watchdog:** every test is capped at 90 s wall time via the autouse
  `system_watchdog` fixture (vs the subsystem tier's 45 s). Override
  per-test with `@pytest.mark.watchdog(seconds=N)`.

- **Thread leak guard:** the autouse `assert_no_thread_leak_extended`
  fixture is a strict superset of the subsystem one — also catches
  leaked `loom-journal-snapshot` threads.

- **Public-API discipline:** system tests do not mutate state through
  `room.session.coordinator` / `room.session.state`. The only allowed
  observation hooks are `room.session.bus.subscribe`, `bus.snapshot`,
  and `journal.load_state` / `journal.load_events`.

- **Process restart:** simulated by constructing a fresh second
  `LoomRoom` over the same `journal_dir`. The first room is fully
  stopped before the second is constructed — this is the real
  cold-load path, not context-manager re-entry.

- **Throttle seam:** long-haul fixtures expose a `lift_throttle=True`
  flag that monkeypatches `coord._throttle` to a 10k/min ceiling so
  multi-turn tests don't bottleneck on the 10/min default. Throttle
  *behavior* is exercised separately in `test_resource_pressure.py`
  without lifting it.

- **Verify-the-limit vs find-the-breaking-point:** ~90 verify tests
  assert bounded behavior. The 7 `@pytest.mark.breakpoint` tests
  binary-search starting small, capped at a hard ceiling, assert only a
  sanity floor, and emit the measured threshold via `print()`.

## Fixtures

See `conftest.py`. The most-used:

- `multi_turn_session` — yields a started `LoomRoom` configured for
  long-haul work. Optional `lift_throttle=True`.
- `journaled_room` — yields a started `LoomRoom` bound to a `tmp_path`
  journal. Teardown validates `events.jsonl`.
- `restart_helper` — stop first room, construct a fresh second room
  over the same journal directory.
- `event_recorder` — bus subscriber with `.by_kind`, `.by_control_type`,
  `.by_sender`, `.count`, `.snapshot_at` helpers.
- `mixed_agent_room` — factory for rooms mixing healthy + adversarial
  agents.
- `scripted_console` — builds `(prompt_fn, captured_lines)` for
  driving `run_console` deterministically.
- `varied_agents` — replies vary by `user_event_id` and per-call
  counter so the loop guard doesn't dedup across long sessions.
- `slow_policy_factory` — policies that sleep / raise on the Nth call.
- `config_factory` — DSL for constructing `RoomConfig` deltas.
- `multi_room_factory` — N rooms with distinct journal directories.
- `binary_search` — shared bisection helper for break-point probes.
- `fake_clock` — re-export of subsystem's monotonic clock fake.
