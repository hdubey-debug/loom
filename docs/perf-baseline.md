# Loom performance baseline

This is the canonical performance reference for the Loom kernel. The
numbers below were captured by `benchmarks/perf.py` with the
`--quick` axis set on a quiescent box and committed alongside this
document. Subsequent perf-pass runs feed the same script's output to
`scripts/bench_diff.py`, which prints per-case deltas and exits
non-zero on any > 15 % regression.

The scenarios are run sequentially: micro → burst → history → cursor →
journal → chat → history-chat → stream → memory. The companion JSON
file (`docs/perf-baseline.json`) is the machine-readable version that
the diff tool consumes.

## Methodology

- Hot loops disable the GC for the duration of the timed window and
  re-enable + collect afterwards. Single-shot scenarios (`burst`,
  `stream`, `memory.RSS-slope`) record one elapsed delta each;
  iterated scenarios capture p50 / p99 / mean across a configurable
  number of samples (default 100–500).
- Every microbench multiplies an `inner` repeat count into a single
  sample to amortize the `perf_counter_ns` and warm up branch
  predictors. The reported p50 / p99 is the time for `inner`
  consecutive ops (per-op cost = `p50 / inner`).
- `tracemalloc` provides the peak allocation watermark for memory
  cases; `psutil` provides the resident-set delta across a 50-turn
  chat session.
- The harness records host / Python / git revision so apples-to-apples
  comparisons are obvious. Diffs across hosts are not meaningful — the
  CI gate is *relative* (delta vs the committed baseline) only.

Re-capture the baseline locally with `make bench-baseline`. Compare a
post-change run with `make bench` then `make bench-diff` (or
`./venv/bin/python scripts/bench_diff.py docs/perf-baseline.json
/tmp/perf-current.json`). A non-zero exit means at least one tracked
metric regressed by > 15 %.

## What the baseline measures (axes)

The harness sweeps these axes when running scenarios:

- `participants ∈ {1, 5, 10}` (`--quick`); `{1, 2, 5, 10, 25}` (full).
- `event_log_size ∈ {100, 1000, 10000}` (`--quick`); plus `{50000,
  100000}` (full).
- `policy ∈ {OpenChat}` (`--quick`); `{OpenChat, Default,
  SingleResponder, RoundRobin}` (full).
- `actor_cursor ∈ {fresh, tail, stale}` — captures the bus.snapshot
  pathology where a stale-cursor wakeup currently re-walks the entire
  log.
- `journal ∈ {off, append-only}` — implicit in the chat / journal
  scenarios.

## Operations covered

| Operation | Pre-perf-pass complexity | Targeted complexity |
|---|---|---|
| `MessageBus.post` | O(1 + S) | unchanged |
| `MessageBus.snapshot()` (no `since`) | O(E) | unchanged |
| `MessageBus.snapshot(since=k)` | **O(E)** (filtered post-copy) | **O(E - k)** (Phase 1.1) |
| `MessageBus.get(id)` | n/a | O(1) (Phase 1.2) |
| `actor._lookup_event(id)` | O(E) | O(1) (Phase 1.2) |
| `coord._find_recent_chat_event_id` | O(E) | gone (Phase 1.3 threads `committed_event_id`) |
| `Event.to_jsonl` | O(F) deep-copy | O(F) direct (Phase 2.2) |
| `build_prompt` (per actor) | O(E) per actor × P actors = O(P × E) | O(P × E_uncached) (Phase 4.1 memo) |
| `journal.replay` peak RSS | O(E) | O(1) (Phase 2.4 streaming) |

## Regression policy

`make bench-diff` fails on any > 15 % regression on any tracked
metric, where "tracked" means:

- `p50_ns` for every case.
- `per_op_ns` for `burst` / `stream` cases.
- `peak_bytes` for `journal.load_events`.
- `bytes_per_event` for `memory.bytes_per_event`.
- `rss_delta_per_turn` for `memory.RSS-slope`.

We deliberately do **not** gate on absolute thresholds because bench
numbers depend on hardware. The baseline is captured on a quiescent
box and committed; re-running on the same box is the apples-to-apples
comparison.

## Soak workloads (Phase 5; separate target)

`make bench-soak` exercises long-haul / failure-injected workloads:

- 1-hour synthetic chat room — RSS slope, thread drift, journal rotation.
- Slow journal disk (synthetic 100 ms write latency) — snapshot queue
  depth, `snapshot_dropped` event count.
- High stream rate (10 k tokens/s) — `stream_delta` post rate, lock
  contention on `bus.post`.
- Membership churn (5 participants joining/leaving every 30 s).
- Subscriber exception storm (10 % failure rate).
- Replay after crash-like shutdown.

These workloads are slow (~1 h aggregate); they don't run on the per-
PR diff path but live alongside `make test-full` as the release-gate
cron.

## Optional perf extras

Some optimizations are opt-in via the `perf` extra:

```
pip install -e .[perf]
```

This pulls in:

- `orjson` — drop-in faster `Event.to_jsonl`. The kernel falls back to
  stdlib `json` if `orjson` is missing, so CI runs both modes.
- `pyinstrument` — sampling profiler for ad-hoc deep dives on the
  bench scenarios.
- `viztracer` — flame-graph viewer for the same.

CI never depends on these; they're for the perf-engineer's local box.
