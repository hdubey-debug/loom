# 09 — Benchmarks, adversarial, CI

This is **Session 9** of the Loom kernel deep-study curriculum. Phase
D concludes: performance and the gates that protect it. We map the
benchmark harness, the regression-diff tool, the adversarial DoS /
tampering scenarios, the perf / coverage / mutation baselines, and
the CI pipeline that ties them together.

State as of Loom v0.1.2 (2026-05-08).

## Files covered

| File | LOC | Role |
|---|---:|---|
| `benchmarks/perf.py` | 792 | Scenario harness — 9 families, JSON+MD output, CLI |
| `benchmarks/__init__.py` | 0 | Package marker |
| `scripts/bench_diff.py` | 231 | 15% regression gate — reads two JSON, prints delta table, exits non-zero on regression |
| `bench/adversarial/conftest.py` | 104 | Marker + `_bench` helper (copy of perf-tier helper) |
| `bench/adversarial/test_large_body.py` | 64 | RES4/P2.1 — 1 MB body rejected pre-log-grow |
| `bench/adversarial/test_tampered_replay.py` | 98 | T1/P0.1+P0.2 — tampered journal lines surface as `journal_corruption` |
| `docs/internal/perf-baseline.md` | 117 | Methodology, axes, regression policy, soak workloads |
| `docs/internal/perf-baseline.json` | 466 | Machine-readable baseline (consumed by `bench_diff.py`) |
| `docs/internal/coverage-baseline.txt` | 26 | 98% branch coverage breakdown |
| `docs/internal/mutation-baseline.txt` | 41 | Pilot 43.6% on `open_chat.py`; 90% target on full kernel+policy |
| `docs/internal/mutation-survivors.md` | 36 | Triage of surviving mutants (test gap / equivalent / intentional) |

## Mental model

```
  Performance pipeline:
                                                                    
   ┌─────────────────────┐         capture                 ┌─────────────┐
   │ benchmarks/perf.py  │─────────────────────────────────►│ JSON + MD   │
   │  9 scenario families│  (host info, git rev, ts,        │ baseline    │
   │  CLI: --output, --quick,│ p50/p99/mean ns, extras)    │ docs/perf-  │
   │  --scenario)        │                                  │ baseline.{json,md}│
   └─────────────────────┘                                  └─────┬───────┘
                                                                  │
                                                                  │ committed
                                                                  ▼
   ┌─────────────────────┐         current run             ┌─────────────┐
   │ ./venv/bin/python   │─────────────────────────────────►│ /tmp/perf-  │
   │   -m benchmarks.perf │ (after change)                  │ current.json│
   └─────────────────────┘                                  └─────┬───────┘
                                                                  │
                                                                  ▼
                                                          ┌──────────────┐
                                                          │ scripts/     │
                                                          │ bench_diff.py│
                                                          │              │
                                                          │ default 15%  │
                                                          │ threshold on │
                                                          │ p50_ns +     │
                                                          │ gated extras │
                                                          │ exits 1 on   │
                                                          │ regression   │
                                                          └──────────────┘
                                                                  │
                                                                  ▼
                                                          ┌──────────────┐
                                                          │  CI gate     │
                                                          │  make bench-diff │
                                                          └──────────────┘

  Adversarial scenarios (opt-in via make security-bench):
                                                                    
   bench/adversarial/test_large_body.py    — 1 MB body rejection (RES4/P2.1)
   bench/adversarial/test_tampered_replay.py — journal corruption surfacing (T1)
   (future: slow_subscriber, slow_disk, token_flood, lease_burst)
                                                                    
  Quality baselines (committed under docs/internal/):
                                                                    
   coverage-baseline.txt    — 98% TOTAL branch coverage (per-module breakdown)
   mutation-baseline.txt    — pilot: 43.6% kill on open_chat.py; 90% target
   mutation-survivors.md    — triage (test gap | equivalent | intentional)
```

---

## benchmarks/perf.py — the canonical scenario harness

### `class CaseResult` (dataclass)

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | E.g. `"bus.post / 1 sub"`. |
| `iters` | `int` | Outer iterations recorded. |
| `inner` | `int` | Inner per-call repeats (amortizes `perf_counter_ns`). |
| `p50_ns` / `p99_ns` / `mean_ns` / `min_ns` | `float` | Computed from `iters` samples. |
| `extras` | `dict[str, Any]` | Scenario-specific (e.g. `peak_bytes`, `per_op_ns`, `bytes_per_event`, `rss_delta_per_turn`). |

`per_op_ns()` method: `p50_ns / inner` for amortized cost.

### Timing core

`_time_callable(fn, *, iters, inner, warmup=5) -> (p50, p99, mean, min)`:

- **GC disabled** for the duration to avoid stop-the-world spikes.
  Re-enabled + `gc.collect()` afterward — preserves test isolation.
- `warmup` calls discarded (warm caches, branch predictors).
- `iters` outer iterations; each times `inner` inner repeats.
- Returns sorted-quantile tuple.

`_bench(name, fn, *, iters=100, inner=1, warmup=5, extras=None) -> CaseResult`
wraps `_time_callable` with naming + extras.

### `class Bench` — scenario container

`__init__(*, quick: bool, only: Optional[set[str]] = None)`:
- `quick=True` uses smaller axes (~30s total vs ~3-5min full).
- `only` filters to specific scenarios.
- `results: dict[str, dict[str, CaseResult]]` — `scenario → name → CaseResult`.
- `_record(scenario, case)` and `_enabled(scenario)` helpers.

### The 9 scenarios

#### 1. `micro` — direct microbench

| Case | Iters × Inner | What's measured |
|---|---|---|
| `bus.post / 0 sub` | 300 × 200 | Hot post path with no subscribers |
| `bus.post / 1 sub` | 300 × 200 | Hot post + inline subscriber dispatch |
| `Event.to_jsonl chat` | 300 × 200 | Chat event serialisation |
| `Event.to_jsonl control` | 300 × 200 | Control event serialisation |
| `obligation_for hit/miss N=5..50` | 300 × 200 | UserTurn obligation lookup at 4 sizes (quick: N=5,25; full: N=5,10,25,50) |
| `actor._lookup_event-style E={1k, 10k, 50k}` | 100 × 10 | Snapshot+scan with id-position fast path |
| `_find_recent_chat_event_id E={1k, 10k, 50k}` | 100 × 5 | Reverse scan for last chat by sender |

#### 2. `burst` — sustained `bus.post` throughput

Sustained-post benchmark, no warmup mid-burst, measured as one big
elapsed delta:

| Case | N posts | Subscribers |
|---|---:|---|
| `sustained-post no-sub N={1k or 10k}` | quick=1k, full=10k | 0 |
| `sustained-post 1-sub N={1k or 10k}` | same | 1 |

`extras` includes `per_op_ns` (gated metric) and `events`.

#### 3. `history` — `bus.snapshot` cost

For sizes ∈ {100, 1k, 10k} (quick) or +{50k, 100k} (full):
- `snapshot full E={size}` — full log copy + filter
- `snapshot audience=bob E={size}` — same with `visible_to` filter

#### 4. `cursor` — `bus.snapshot(since=k)` at three positions

For sizes ∈ {1k, 10k} (quick) or +{50k} (full):
- `snapshot since=fresh E={size} k={size-10}` — cursor near head (10 events to scan)
- `snapshot since=tail E={size} k={size//2}` — cursor at midpoint
- `snapshot since=stale E={size} k={size//10}` — cursor at 10% (lots of new events)

`extras` includes `cursor`, `log_size`, `tag`. This scenario directly
measures the slice optimization from Session 2 (`since` collapses to
`_log[since+1:]` instead of full copy + post-filter).

#### 5. `journal` — load + memory peak

For sizes ∈ {100, 1k} (quick) or +{10k} (full):
- `Journal.load_events E={size}` — replay-into-list time
- `Journal.load_events peak_bytes E={size}` — `tracemalloc` peak
  (extras: `peak_bytes` — gated metric)

#### 6. `chat` — end-to-end LoomRoom turns

For each policy in {OpenChat} (quick) or {OpenChat, Default,
SingleResponder} (full):
- For each `n_agents` in {1, 5, 10} (quick) or {1, 2, 5, 10, 25} (full):
  - Build a room, mention all agents, measure `n_turns=20` (quick=5)
    consecutive `room.post_and_wait` calls.
  - Records `p50`, `p99`, `mean`, `min` across the turn samples.
  - extras: `policy`, `n_agents`, `turns`.

#### 7. `history-chat` — turn cost vs pre-existing log

Pre-loads N events directly via `bus.post` (bypassing policy/coord),
then times 5 turns. Sizes: {1k, 10k} (quick) or +{50k} (full).
`extras: prior_log, n_agents`.

#### 8. `stream` — `stream_delta` post throughput

One subscriber + N=10k (quick=1k) `stream_delta` posts. Records
`per_op_ns` and `throughput_per_s`.

#### 9. `memory` — bytes-per-event + RSS slope

- `bytes_per_event {chat-simple, chat-full}` via `tracemalloc` over
  1000 instances. extras: `bytes_per_event` (gated), `alloc_total_bytes`, `n`.
- `chat-session RSS slope turns={50 or 200}` via `psutil` over a
  multi-turn session. extras: `rss_start_bytes`, `rss_end_bytes`,
  `rss_delta_bytes`, `rss_delta_per_turn` (gated), `threads_start`,
  `threads_end`.

`psutil` is in dev extras; the bench is dev-only so it's available.
`tracemalloc` is stdlib.

### Output

`_to_dict(bench) -> dict`:

```python
{
    "host": {"platform", "python", "machine", "processor",
             "cpu_count_logical", "cpu_count_physical",
             "mem_total_bytes", "mem_available_bytes"},
    "ts": "2026-05-08T12:00:00Z",
    "git_rev": "<short hash>",
    "scenarios": {
        "<scenario>": {
            "<case_name>": CaseResult-as-dict (asdict),
            ...
        },
        ...
    },
}
```

`_render_markdown(data) -> str` produces a per-scenario table:
`| case | iters | inner | p50 | p99 | mean | extras |` (formatted ns
via `_fmt_ns` — auto-units).

### CLI

```
python -m benchmarks.perf [--output PATH] [--quick] [--scenario S [S...]]
```

- `--output` defaults to `docs/perf-baseline.json`.
- `--quick` uses smaller axes (~30s).
- `--scenario S1 S2 ...` filters to specific scenarios (`micro`,
  `burst`, `history`, `cursor`, `chat`, `history-chat`, `stream`,
  `journal`, `memory`).

`main()` writes JSON + sibling Markdown, then prints a per-scenario
console preview.

---

## scripts/bench_diff.py — 15% regression gate

```bash
scripts/bench_diff.py BASELINE_JSON AFTER_JSON
scripts/bench_diff.py --threshold 0.10 …       # tighten gate
scripts/bench_diff.py --json …                 # delta JSON output
scripts/bench_diff.py --quiet-pass …           # suppress on no-regression
```

### Gated metrics

```python
_GATED_EXTRAS = ("per_op_ns", "peak_bytes", "bytes_per_event",
                 "rss_delta_per_turn")
```

Plus **`p50_ns`** for every case (the primary gate).

`p99_ns` is **NOT gated** — too noisy. Diff is reported for
informational purposes but doesn't fail.

### Algorithm

```python
def _delta(old, new):
    if old == 0:
        return (0.0, "n/a") if new == 0 else (float("inf"), "+inf")
    ratio = (new - old) / old
    return ratio, f"{'+' if ratio >= 0 else ''}{ratio*100:.1f}%"

def _compare_case(scen, name, old, new, threshold):
    rec = {"scenario": scen, "case": name}
    if old["p50_ns"] > 0 or new["p50_ns"] > 0:
        ratio, label = _delta(old["p50_ns"], new["p50_ns"])
        rec["p50"] = {"old_ns", "new_ns", "ratio", "label",
                      "regression": ratio > threshold,
                      "improvement": ratio < -threshold}
    # p99 reported, NOT gated.
    for key in _GATED_EXTRAS:
        if key in old_x and key in new_x:
            ratio, label = _delta(float(o), float(n))
            rec[key] = {"old", "new", "ratio", "label",
                        "regression", "improvement"}
    return rec
```

Cases present in only one file are reported as `only_in_baseline` /
`only_in_after` (informational).

### Exit codes

- **0** if no regressions (`len(regressions) == 0`).
- **1** if any gated metric exceeds threshold.

### Output format (table mode, default)

```
scenario       case                                     metric              old            new      delta
----------------------------------------------------------------------------------------------------------
micro          bus.post / 0 sub                         p50              1.65us         1.62us      -1.8% ✓
micro          bus.post / 0 sub                         p99              2.10us         2.20us      +4.7%
history        snapshot full E=10000                    p50            285.00us       290.00us      +1.7%
history        snapshot since=stale E=10000 k=1000      p50             45.00us       150.00us    +233.3% ⚠
...

FAIL: 1 regressions (threshold 15%):
  - history/snapshot since=stale E=10000 k=1000 p50: +233.3%
```

Tags: `⚠` for regression, `✓` for improvement.

### `--json` mode

Emits the full delta as JSON: threshold, baseline_ts, baseline_git,
after_ts, after_git, diffs (per-case dicts), only_in_baseline,
only_in_after, regressions (list of `"scen/case metric: label"`).

---

## bench/adversarial/ — opt-in security scenarios

NOT in default `testpaths` (the `tests/` directory). Run via:

```bash
pytest bench/adversarial/ -v          # direct invocation
make security-bench                   # wired target
```

### `bench/adversarial/conftest.py`

- Registers two markers: `adversarial`, `perf`.
- Provides a local copy of the `_bench` helper (avoids importing test
  files from the perf tier, which would re-collect them).
- `bench` fixture returns `_bench` directly.

### `test_large_body.py` — RES4 / P2.1

Two tests:

- **`test_oversize_body_is_rejected_before_log_grows`** — 1 MB body
  posted to a `MessageBus(max_body_bytes=256*1024)`:
  - Every attempt raises `BodyOversizeError`.
  - Bus log length **unchanged** after all rejected attempts (`assert
    len(bus) == initial_len`).
  - p99 `< 5 ms` ceiling (very generous; would catch a regression
    that started serializing the body to count bytes).
- **`test_within_cap_body_is_accepted`** — 256KB-1 byte payload
  accepts; bus log grows; p99 `< 50 ms` ceiling.

### `test_tampered_replay.py` — T1 / P0.1+P0.2

Four tests:

- **`test_replay_completes_with_corruption_surfaced`** — 1k-line
  journal with 5% tampered (every 20th line is `chat` with a
  list body). `j.iter_events(emit_corruption_events=True)` yields
  ≥45 corruption events AND ≥950 valid chats. **Replay completes;
  no actor crash.**
- **`test_silent_mode_preserves_legacy_skip_semantics`** — default
  `emit_corruption_events=False` silently skips bad lines, no
  corruption events emitted.
- **`test_replay_into_bus_completes`** — `replay_into(stub_coord)`
  posts every event including corruption surfaces; ≥280 of 300 land
  on the stub bus.
- **`test_from_jsonl_raises_typed_exception_on_tamper`** —
  `from_jsonl` raises `EventShapeError` (not `TypeError`) on shape
  mismatch.

### Future scenarios (per `bench/adversarial/conftest.py` docstring)

Not yet implemented; tracked in security-model plan:
- `test_slow_subscriber.py` — CON1: latency under slow callback.
- `test_slow_disk.py` — RES3: snapshot-queue depth at sustained 100ms write latency.
- `test_token_flood.py` — RES6: bus.post rate at 10k tokens/s.
- `test_subscriber_exceptions.py` — F: throughput at 50% failing callbacks.
- `test_lease_burst.py` — C3: 16-actor concurrent acquire under cap=1.

---

## docs/internal/perf-baseline.md — methodology summary

### Methodology

- **GC disabled** in hot loops; re-enabled + collected after.
- Single-shot scenarios (`burst`, `stream`, `memory.RSS-slope`)
  record one elapsed delta each; iterated scenarios capture
  p50/p99/mean across 100-500 samples.
- Microbenches multiply `inner` repeats per sample to amortize
  `perf_counter_ns` overhead and warm branch predictors. Per-op cost
  = `p50 / inner`.
- `tracemalloc` provides peak allocation watermarks for memory cases.
- `psutil` provides RSS delta across the 50-turn chat session.
- Host/Python/git revision recorded for traceability.
- **Diffs across hosts are not meaningful** — the CI gate is
  *relative* (delta vs the committed baseline) only.

### Operations covered (with target complexity)

| Operation | Pre-perf-pass | Targeted |
|---|---|---|
| `MessageBus.post` | O(1 + S) | unchanged |
| `MessageBus.snapshot()` no `since` | O(E) | unchanged |
| `MessageBus.snapshot(since=k)` | **O(E)** (filter post-copy) | **O(E - k)** ✅ Phase 1.1 |
| `MessageBus.get(id)` | n/a | O(1) ✅ Phase 1.2 |
| `actor._lookup_event(id)` | O(E) | O(1) ✅ Phase 1.2 |
| `coord._find_recent_chat_event_id` | O(E) | gone — Phase 1.3 threads `committed_event_id` directly |
| `Event.to_jsonl` | O(F) deep-copy | O(F) direct ✅ Phase 2.2 |
| `build_prompt` per actor | O(E) per actor × P = O(P × E) | O(P × E_uncached) ✅ Phase 4.1 memo (Session 2 — `bus.render_chat_line`) |
| `journal.replay` peak RSS | O(E) | O(1) ✅ Phase 2.4 streaming |

These optimisations all landed; the perf-baseline measures the
**post-pass** behaviour. Future work that regresses any of these
should be flagged.

### Regression policy

`make bench-diff` fails on any > 15% regression on:
- `p50_ns` for **every** case.
- `per_op_ns` for `burst` / `stream` cases.
- `peak_bytes` for `journal.load_events`.
- `bytes_per_event` for `memory.bytes_per_event`.
- `rss_delta_per_turn` for `memory.RSS-slope`.

We **deliberately do not gate on absolute thresholds** because bench
numbers depend on hardware. The baseline is captured on a quiescent
box and committed; re-running on the same box is the apples-to-apples
comparison.

### Soak workloads (Phase 5; separate target)

`make bench-soak` (~1h aggregate; release-gate cron, not per-PR):
- 1-hour synthetic chat — RSS slope, thread drift, journal rotation.
- Slow journal disk (synthetic 100ms write latency) — snapshot queue
  depth, `snapshot_dropped` event count.
- High stream rate (10k tokens/s) — `stream_delta` post rate, lock
  contention.
- Membership churn (5 participants joining/leaving every 30s).
- Subscriber exception storm (10% failure rate).
- Replay after crash-like shutdown.

### Optional perf extras

```bash
pip install -e .[perf]
```

- `orjson` — faster `Event.to_jsonl`. Kernel falls back to stdlib
  `json` if missing; CI runs both modes.
- `pyinstrument` — sampling profiler.
- `viztracer` — flame-graph viewer.

---

## docs/internal/coverage-baseline.txt — 98% TOTAL branch

Per-module breakdown (current snapshot):

| Module | Stmts | Miss | Branch | BrPart | Cover |
|---|---:|---:|---:|---:|---:|
| `loom/__init__.py` | 10 | 0 | 0 | 0 | **100%** |
| `loom/adapters.py` | 75 | 0 | 38 | 0 | **100%** |
| `loom/contracts.py` | 17 | 0 | 0 | 0 | **100%** |
| `loom/kernel/__init__.py` | 0 | 0 | 0 | 0 | **100%** |
| `loom/kernel/actor.py` | 162 | 6 | 66 | 3 | **96%** |
| `loom/kernel/addressees.py` | 24 | 0 | 10 | 0 | **100%** |
| `loom/kernel/bus.py` | 72 | 0 | 16 | 0 | **100%** |
| `loom/kernel/coordinator.py` | 451 | 6 | 206 | 14 | **97%** |
| `loom/kernel/events.py` | 101 | 0 | 22 | 0 | **100%** |
| `loom/kernel/journal.py` | 207 | 0 | 56 | 1 | **99%** |
| `loom/kernel/obligations.py` | 52 | 0 | 10 | 0 | **100%** |
| `loom/kernel/prompt.py` | 140 | 0 | 66 | 3 | **99%** |
| `loom/kernel/room.py` | 189 | 1 | 40 | 3 | **98%** |
| `loom/kernel/streaming.py` | 95 | 1 | 32 | 1 | **98%** |
| `loom/kernel/user_turn.py` | 95 | 0 | 26 | 0 | **100%** |
| `loom/policy/__init__.py` | 5 | 0 | 0 | 0 | **100%** |
| `loom/policy/default.py` | 152 | 0 | 68 | 0 | **100%** |
| `loom/policy/open_chat.py` | 15 | 0 | 2 | 0 | **100%** |
| `loom/policy/round_robin.py` | 52 | 0 | 20 | 0 | **100%** |
| `loom/policy/single_responder.py` | 19 | 0 | 4 | 0 | **100%** |
| `loom/room.py` | 133 | 5 | 44 | 6 | **94%** |
| `loom/runtime.py` | 397 | 1 | 194 | 3 | **99%** |
| **TOTAL** | **2463** | **20** | **920** | **34** | **98%** |

The lowest are `loom/room.py` (94%), `loom/kernel/actor.py` (96%),
`loom/kernel/coordinator.py` (97%), `loom/kernel/streaming.py` (98%),
`loom/kernel/room.py` (98%). The exact missing branches are not
named here; they're surfaced by `make coverage-html` →
`/tmp/coverage_html/index.html`.

`pyproject.toml` enforces `fail_under = 98` so the TOTAL must stay at
98%+. New code that drops the average below trips the gate.

---

## docs/internal/mutation-baseline.txt — pilot status

**Pilot**: `loom/policy/open_chat.py` only (smallest module),
captured 2026-05-08:

```
Mutants generated:  39
Killed:             17  (43.6%)
Survived:           22  (56.4%)
Timed out:           0
Suspicious:          0
Throughput:         97 mut/s
```

Acceptance threshold: **≥ 90% kill rate on `loom/kernel/` +
`loom/policy/`**. The pilot's 43.6% indicates **real test gaps** —
not yet meeting the public-release threshold; the
assertion-strengthening pass is the path to closing them.

`make test-full` runs the full kernel + policy mutation baseline
(~1.5–2h on a quiescent box) and overwrites the file.
`scripts/run_full_quality.sh` diffs new survivors against the
captured set.

To inspect / kill survivors:

```bash
./venv/bin/mutmut show loom.policy.open_chat.<mutant_id>
# 1. Read the mutant body.
# 2. Identify the assertion that would catch it.
# 3. Add a regression test in tests/test_kernel_<module>.py.
# 4. Re-run mutmut --paths-to-mutate=<one_file> to verify.
```

### `mutation-survivors.md` — triage buckets

Three classifications:
- **test gap** → a new test should kill this mutant.
- **equivalent** → semantically identical behavior; mark `# pragma:
  no mutate` upstream OR skip via mutmut filter.
- **intentional** → original behavior is permissive on purpose; rationale here.

`scripts/run_full_quality.sh` fails on any **new** survivor not
listed in this file — so adding a code path with new equivalent
mutants requires updating this file.

Pilot survivors:
- mutmut_7, _8, _9 — boolean-flip mutations in routing branches; likely killable.
- mutmut_16–_21 — string-literal / integer-constant mutations; probably equivalents.

Triage status: **DEFERRED** to first full baseline run.

---

## CI workflow — `.github/workflows/ci.yml`

Already covered in Session 0. To recap the gates that run on push +
PR:

```yaml
matrix:
  python-version: ["3.11", "3.12"]   # both Pythons in parallel
steps:
  - actions/checkout@v4
  - actions/setup-python@v5 (cache: pip)
  - pip install -e .[dev]
  - ruff check loom tests          # lint
  - ruff format --check loom tests # format check
  - mypy loom                      # type check
  - pytest -q --maxfail=5          # default suite (with 98% coverage gate)
```

What CI does NOT run (today):
- `make bench` / `make bench-diff` — manual, captured on perf-engineer's box.
- `make security-bench` (adversarial) — manual.
- `make test-property HYPOTHESIS_PROFILE=nightly` — manual / weekly.
- `make test-full` (mutation) — manual / release rhythm.
- `make bench-soak` — manual / release rhythm.

The CI runs the **fast suite** (kernel + subsystem + property@ci +
system + coverage), enforces the 98% coverage gate, and verifies
ruff + mypy. Heavier gates are "captured locally + committed
baseline + manual diff" today.

---

## Invariants (this session's additions)

184. **The perf baseline is captured on a quiescent box and
     committed.** Re-running on the same box is the apples-to-apples
     comparison. Cross-host comparisons are not meaningful — the CI
     gate is *relative* delta only.
185. **Default regression threshold is 15%** on `p50_ns` and 4
     gated extras (`per_op_ns`, `peak_bytes`, `bytes_per_event`,
     `rss_delta_per_turn`). `--threshold` overrides at the CLI.
186. **`p99_ns` is reported but NOT gated** — too noisy. Use it for
     human-eyeballed signal only.
187. **Improvements (delta < -threshold) are tagged with ✓** in the
     table output. The gate doesn't fail on improvements (obviously)
     but they're flagged for attention.
188. **`benchmarks/perf.py` writes BOTH JSON and Markdown** sibling
     files. JSON is for `bench_diff.py`; Markdown is for human
     review and committing under `docs/`.
189. **Bench host info recorded** — `platform`, `python`, `machine`,
     `processor`, `cpu_count_logical/physical`, `mem_total/available`.
     Plus `git_rev` and `ts`. Diffs across hosts are noted via host
     metadata but not enforced — operators must know not to compare
     across hosts.
190. **`memory.RSS-slope` records `threads_start` AND
     `threads_end`** — catches thread leaks alongside RSS growth.
191. **Adversarial scenarios assert log INVARIANT plus latency
     ceiling**. E.g. `test_oversize_body_is_rejected_before_log_grows`
     asserts BOTH `len(bus) == initial_len` AND `p99 < 5ms`. Just
     a latency assertion would let a "slow rejection" slip through;
     just an invariant assertion would let a "fast accept" slip.
192. **`bench/adversarial/conftest.py` provides its own `_bench`
     copy** rather than importing from `tests/perf/conftest.py` —
     because importing test files would trigger pytest re-collection.
193. **`bench/adversarial/` is NOT in default `testpaths`**. Run via
     `make security-bench` or `pytest bench/adversarial/ -v`. Marker:
     `pytest.mark.adversarial`.
194. **`tracemalloc` peak is the canonical memory metric**, not RSS
     (RSS includes Python runtime overhead, garbage). RSS slope is
     used only for the long-run chat session because tracemalloc has
     overhead that distorts long benches.
195. **GC is disabled in `_time_callable` / `_bench`** to keep p99
     clean. Re-enabled + collected after — preserves test
     isolation.
196. **Coverage gate is 98% TOTAL branch** (`fail_under` in
     `pyproject.toml`). Per-module floors are not enforced — a 90%
     module can offset a 100% module if the total stays ≥ 98%.
     `loom/room.py` at 94% is the lowest today.
197. **Mutation acceptance threshold is 90% kill** on
     `loom/kernel/` + `loom/policy/`. Pilot at 43.6% on
     `open_chat.py` is below threshold; assertion-strengthening
     pass is the path to public-release readiness.
198. **`scripts/run_full_quality.sh` fails on NEW survivors** not
     in `mutation-survivors.md`. Adding a code path with new
     equivalent mutants requires updating this file (same
     pattern as the perf baseline — capture, classify, commit).
199. **CI runs fast suite + lint + type check + format check** on
     Python 3.11 AND 3.12 in parallel matrix. Heavier gates
     (mutation, soak, scenario bench, adversarial, nightly
     Hypothesis) are manual / release-rhythm.
200. **`pyproject.toml`'s perf extras (`orjson`, `pyinstrument`,
     `viztracer`) are dev-only**. CI never depends on them; the
     kernel falls back to stdlib `json` when `orjson` is missing.
201. **`benchmarks/perf.py:_make_room` builds rooms with anchor +
     default_responder set to the first agent** so the chat scenario
     exercises the slot-fallback path without empty-slot edge cases.
202. **Bench reply text is appended with a counter** (`_BENCH_REPLY_TAIL`
     + counter) AND deliberately exceeds the 50-char short-text
     threshold so the loop guard doesn't dedup successive replies.
     Without this, multi-turn benches stall after the first reply.

---

## Verification

> *Given a kernel change that adds a `dict` lookup in
> `bus.snapshot`, predict which scenarios in `perf-baseline.md`
> would shift and by how much — and whether the change would trip
> the gate.*

**Hypothetical change**: add a per-call `dict[int, Event]` lookup
inside `bus.snapshot` (e.g. cache the result of `_log[since:]` keyed
on `since`). Each call performs an extra dict access (O(1) lookup, a
few ns) plus a dict insert (also O(1), but with hash + bucket cost).

### Scenarios that would shift

#### `micro` tier

- **`bus.post / 0 sub`** and **`bus.post / 1 sub`** — these don't
  call `snapshot`, so **NO shift**.
- **`actor._lookup_event-style E={1k, 10k, 50k}`** — does call
  `snapshot()`. Per-call adds ~50–100ns of dict overhead. Currently
  the snapshot at E=50k takes some milliseconds (~3.7ms p50 in the
  baseline JSON). Adding 100ns is **<0.003% — well under the 15%
  gate**. ✓ No regression triggered.
- **`obligation_for hit/miss N=*`** — pure UserTurn lookup; doesn't
  touch the bus. **No shift**.
- **`Event.to_jsonl`** — pure event method; **no shift**.

#### `history` tier — DIRECTLY MEASURES `snapshot`

- **`snapshot full E={100, 1k, 10k, 50k, 100k}`** — full-log copy.
  At E=100k the p50 is ~3ms+; adding 100ns is 0.003%. **No regression
  triggered.**
- **`snapshot audience=bob E={size}`** — same shape with audience
  filter; same conclusion.

But wait — the question said "adds a dict lookup". If the lookup is
**per-event** (not per-call) — e.g. `_log_index_by_id` rebuilt on
every snapshot — then E=100k means 100k dict ops added per call.
That's roughly 10ms added; on top of ~3ms baseline that's a **300%
regression** at E=100k. Would absolutely trip the gate.

The Q is ambiguous, so let's consider both:

#### Per-call lookup (cheap, O(1))

| Scenario | Baseline p50 | Added cost | New p50 | % shift | Gate? |
|---|---:|---:|---:|---:|:---:|
| `bus.post / 0 sub` | ~1.6μs | 0 | 1.6μs | 0% | OK |
| `snapshot full E=10k` | ~285μs | ~100ns | 285.1μs | +0.04% | OK |
| `snapshot full E=100k` | ~3ms | ~100ns | 3.0001ms | <0.01% | OK |
| `snapshot since=stale E=50k k=5k` | ~14μs | ~100ns | 14.1μs | +0.7% | OK |
| `chat OpenChat N=10` | ~50ms | ~100ns × ~30 calls/turn | +3μs | <0.01% | OK |

**No gate trip.** A pure O(1) dict lookup added per `snapshot` call
is well under noise.

#### Per-event lookup (e.g. rebuild index every call)

| Scenario | Baseline p50 | Added cost | New p50 | % shift | Gate? |
|---|---:|---:|---:|---:|:---:|
| `snapshot full E=1k` | ~30μs | ~30μs (dict ops × 1k) | 60μs | **+100%** | **REGRESSION ⚠** |
| `snapshot full E=10k` | ~285μs | ~300μs | 585μs | **+105%** | **REGRESSION ⚠** |
| `snapshot full E=100k` | ~3ms | ~3ms | 6ms | **+100%** | **REGRESSION ⚠** |
| `snapshot since=fresh E=10k k=9990` | ~5μs | ~30μs (still touches all) | 35μs | **+600%** | **REGRESSION ⚠** |
| `actor._lookup_event-style E=50k` | a few ms | ~1.5ms | +50% | **REGRESSION ⚠** | |
| `chat OpenChat N=10` | ~50ms | ~3ms × ~30 calls = ~90ms | 140ms | **+180%** | **REGRESSION ⚠** |
| `history-chat OpenChat prior_log=10k` | depends on per-actor snapshot pattern; 4 actors × 5 calls × 300μs = ~6ms added | likely 20%+ | **REGRESSION ⚠** | |
| `journal.load_events E=10k peak_bytes` | ~baseline | + maybe O(E) extra dict bytes if cached | could trip `peak_bytes` gate | **REGRESSION ⚠** |
| `memory.RSS-slope` | + (depending on cache lifetime) cumulative dict growth across turns | could trip `rss_delta_per_turn` | **REGRESSION ⚠** | |

**Multiple gates trip.** `make bench-diff` would print a
multi-line FAIL.

### What `bench-diff` would output

For the per-event variant:

```
FAIL: 7 regressions (threshold 15%):
  - history/snapshot full E=1k p50: +100.0%
  - history/snapshot full E=10k p50: +105.0%
  - history/snapshot full E=100k p50: +100.0%
  - cursor/snapshot since=fresh E=10k k=9990 p50: +600.0%
  - cursor/snapshot since=stale E=10k k=1000 p50: +233.3%
  - chat/OpenChat turn N_agents=10 p50: +180.0%
  - history-chat/OpenChat turn N_agents=5 prior_log=10000 p50: +120.0%
```

### Which adversarial scenarios shift

`test_large_body.py` doesn't measure `snapshot` — it measures
`post`. **No shift**.

`test_tampered_replay.py` calls `j.iter_events` and `j.replay_into`
which post to a stub bus. The bus's `snapshot` is called by the
`replay_into` consumer? No — the replay path posts events. The test
only asserts replay completes. Possibly some marginal effect from
the bus's internal log-mutation cost. **Negligible shift**.

### Other gates that wouldn't directly catch this

- **Coverage gate**: a per-event dict lookup likely adds new
  branches that need test coverage. If the change adds a "if cache
  hit return cached / else compute" branch and tests don't hit both
  paths, coverage drops below 98%. CI might catch via the coverage
  gate before bench-diff runs.
- **Mutation tier**: a mutant that disables the cache (e.g. `if
  False: return cached`) would be killed by the perf characteristics
  changing — but mutmut doesn't run perf tests. So this would
  surface as a mutation **survivor** (mutant that passes all tests
  because the test suite doesn't measure perf). Worth flagging in
  `mutation-survivors.md` as "intentional — perf optimisation, not
  correctness gated".

### Summary

**Per-call (O(1)) dict lookup**: ~0% shift — well under the 15%
gate. CI passes.

**Per-event (O(E)) dict rebuild**: ~50–600% shift on `history`,
`cursor`, `chat`, `history-chat` scenarios. CI fails loudly with
~7 regressions.

The granularity of the change matters enormously. The lesson: when
adding caching to a hot path, **measure with the per-event scenarios
at the largest sizes** (`history` E=100k, `cursor` E=50k, `chat`
N_agents=25). The CI gate is a safety net, but the perf-engineer
should run `make bench-diff` locally on every kernel-internal
change to those modules.

---

## Cross-references

- depends on: `00-orientation.md` (Makefile target table, security
  model), `02-kernel-bus.md` (`snapshot(since=)` slice optimisation
  measured by the `cursor` scenario), `04-kernel-actor-journal.md`
  (`Journal.load_events` measured by the `journal` scenario;
  `iter_events(emit_corruption_events=True)` by `test_tampered_replay.py`),
  `05-kernel-coordinator.md` (`max_responses` enforcement and
  `_loop_guard` measured indirectly by the chat scenario),
  `08-test-architecture.md` (the perf tier's `bench` fixture mirrors
  the `_bench` helper here).
- depended on by:
  - Future kernel-modification PRs — every change that touches a
    measured operation should run `make bench-diff` locally before
    landing.
  - `Makefile`: `bench`, `bench-quick`, `bench-micro`, `bench-diff`,
    `bench-baseline`, `bench-soak`, `security-bench`, `mutation`.
  - `scripts/run_full_quality.sh` (the `test-full` driver).

## Open questions / things to revisit

1. **Per-PR bench gate** — today CI doesn't run `make bench` /
   `make bench-diff` on every PR. The bench is "captured locally +
   committed baseline + manual diff". Wiring it into CI requires a
   stable bench host (GitHub-hosted runners are noisy); options:
   self-hosted runner OR a "perf-baseline" workflow that runs on
   a `perf` label.
2. **`p99` not gated** is correct for the noise-sensitive cases
   but means a regression that doubles tail latency without
   changing p50 slips through. Worth a `--p99-threshold` (looser,
   e.g. 50%) to catch the gross cases.
3. **`peak_bytes` gate uses `tracemalloc`** which has its own
   overhead — running with vs without tracemalloc gives different
   absolute numbers. The baseline is captured WITH tracemalloc, so
   diffs are consistent; just don't compare to non-tracemalloc
   runs.
4. **Soak workloads run only manually** (~1h). Some failure modes
   (RSS slope, thread drift) only surface here. Worth a weekly
   cron in addition to release-gate.
5. **Adversarial conftest duplicates `_bench`** rather than
   importing from `tests/perf/conftest.py` (which would trigger
   re-collection). Worth extracting the helper to a non-test
   module that both can import — `loom/_bench.py` or similar.
6. **No perf assertion on `replay_into` linearity** — the
   adversarial test asserts replay completes but doesn't measure
   the per-line cost. A future change that made replay O(N²)
   wouldn't trip a gate. Worth adding `test_bench_journal.py` cases
   for replay at increasing E.
7. **Mutation pilot is on 1 file**, kill rate 43.6%. The full
   baseline pending. Until the full baseline lands, the
   `run_full_quality.sh` "no new survivors" check is moot — there's
   no captured set to diff against.
8. **`scripts/bench_diff.py --json` output is not currently
   consumed** by any tooling. Useful for local scripting; consider
   wiring into a release-notes generator that summarises
   improvements.
9. **`memory.RSS-slope` is wall-clock-sensitive** — a system under
   load will report a higher slope. The benchmark assumes a
   quiescent box; if run on a busy machine, the metric is
   meaningless. Worth documenting more loudly OR refusing to record
   if `psutil.cpu_percent()` is high.
10. **Mutation `paths_to_mutate`** doesn't include `loom/runtime.py`,
    `loom/room.py`, `loom/messages.py`, `loom/errors.py`,
    `loom/contracts.py`, `loom/testing.py`. Defensive choice
    (those are facade / projection layers), but `loom/runtime.py`
    has 397 statements + 194 branches and is the wiring code — a
    surviving mutant there could silently break room construction.
    Worth re-evaluating the scope.
