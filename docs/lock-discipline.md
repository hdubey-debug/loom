# Lock discipline in the Loom kernel

**Audience**: kernel contributors and reviewers.
**Status**: enforced by `tests/test_kernel_kernel_boundary.py::LockDisciplineBoundary` (v0.3 PR 2; doctrine P4 / §2).

## The rule

The coordinator's lock (`RoomCoordinator._lock`) is the single critical
section that gates all mutable kernel state. It guards only **cheap**
operations:

- Lease registration and termination.
- State validation (capability checks, schema validation, invariant
  checks).
- Effect application (calling reducers; emitting journaled events).
- Budget reservation/commit/refund (in-memory ledger updates).

It must **never** be held during:

- LLM streaming-proxy iteration (`streaming.run_streaming_call` body).
- Tool calls (post-v0.4 `_invoke_tool` body).
- File I/O (`open(...)`, `read()`, `write()`, `fsync()`).
- Network I/O (`requests`, `urllib`, socket reads).
- `time.sleep(...)`.
- Any other call whose latency depends on something outside the room
  (NTP, disk, kernel scheduler quanta beyond ~microseconds).

Holding the coordinator lock across any of the above serializes the
whole room on a single external dependency — every actor blocks until
the slow operation returns. Doctrine P4 elevates this from a
performance hint to a correctness invariant: tests that assert on
parallel scheduling assume short critical sections.

## Mechanism

`RoomCoordinator._lock` is a `_TrackedRLock` (defined at
`loom/kernel/coordinator.py`). It wraps a `threading.RLock` with an
owner-thread record so `RoomCoordinator._assert_not_holding_lock(where)`
can fail loudly if any I/O entry point is invoked while the current
thread already holds the lock.

Every long-running entry point in the kernel must call
`_assert_not_holding_lock` as its first action:

```python
def run_streaming_call(..., coordinator, ...):
    coordinator._assert_not_holding_lock("streaming.run_streaming_call")
    # ... long-running iteration ...
```

The naming convention (below) makes lock-affinity visible at the call
site, so reviewers don't need to trace ancestry to know whether a
method is on the under-lock side.

## Naming convention

Under-lock helpers (caller MUST hold `self._lock`):

- `_apply_*` — apply a reducer / mutate state through the effect
  registry (post-PR 3).
- `_validate_*` — read-only consistency check.
- `_reserve_*` / `_commit_*` / `_refund_*` — budget-ledger operations
  (post-PR 6).
- `_close_*_locked` / `_apply_*_locked` — legacy v0.2 helpers; the
  `_locked` suffix is explicit.

I/O entry points (caller MUST NOT hold `self._lock`):

- `_call_llm`, `_invoke_tool`, `_write_file`, `_post_to_remote`, etc.
  These must call `coordinator._assert_not_holding_lock(where)` first.

When in doubt, prefer the assertion over inspection — the assertion's
cost is one attribute compare.

## How to extend

- **Adding a new under-lock helper**: name it with one of the
  reserved prefixes (or the explicit `_locked` suffix); do not
  introduce any blocking call (no `time.sleep`, no file/network I/O,
  no `bus.subscribe` semantics that could call user code synchronously
  on a slow path).
- **Adding a new I/O entry point**: place
  `coordinator._assert_not_holding_lock("module.entry_point_name")` as
  the first statement. The `where` argument is a free-form identifier
  used in the error message — make it grep-able.
- **Adding a new `with self._lock:` block**: keep the body short.
  Anything that calls into user code (policy hooks, custom actions)
  needs careful review because user code can hide unbounded latency;
  the doctrine's v0.3 PR 12 work explicitly moves policy classification
  off-lock for exactly this reason.

## Related

- v0.3 doctrine: `docs/internal/study/11-orchestration-os-doctrine.md`
  P4 (no long-running operation under coordinator lock), §2 (lock
  discipline).
- v0.3 PR 12 (`docs/internal/study/13-v03-implementation-roadmap.md`
  Phase E) — off-lock policy classification + streaming-stall
  watchdog, the largest application of this rule to a v0.2 hot path.
- Existing inline anchors in code: `coordinator.py` `_TrackedRLock`
  class; `coordinator.py` `_assert_not_holding_lock` method;
  `streaming.py` `run_streaming_call` entry.
- Companion timing doc: `docs/timing-discipline.md` (the analogous
  v0.2.1-era invariant for monotonic vs. wall-clock; same structural
  enforcement pattern).
