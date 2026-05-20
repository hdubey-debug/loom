# Loom Kernel — v0.3.x Context Compaction Implementation Roadmap

## 1. Purpose & gating

This roadmap sequences the implementation work for the context
compaction doctrine (`14-context-compaction-doctrine.md`, principles
P16–P22, locked 2026-05-16).

**Gating:** v0.3 session 1 must be complete (14 PRs landed
2026-05-16; 975 kernel tests pass). The structural prerequisites are
in place:

- `KernelState` transactional root with reserved subsystem slots
  (PR 1) — adding `context: ContextState` is a non-breaking
  extension.
- Unified `Lease` abstraction with `LeaseKind` and `LeaseContext`
  tagged union (PR 7) — adding `SUMMARIZATION` kind and
  `SummarizationContext` follows the established pattern.
- Typed effect registry (PR 3) — new compaction effects register
  alongside existing 16.
- Typed causal_refs + `TraceContext` (PR 4) — summary events
  populate both.
- `CapabilityState` ledger (PR 5) — adds `EMIT_SUMMARY` and
  `SUMMARIZE` verbs.
- `BudgetLedger` three-way accounting (PR 6) — SUMMARIZATION leases
  reserve normally.
- `ControlAction` dispatch (PR 9) — `SummarizeControlAction`
  follows the established pattern.
- `EventPlane` taxonomy + `lease_closed` unification (PR 8) — new
  events live in `CONTROL`.
- Slash-command parser (PR 11) — `/summarize` routes through
  `propose_control_action("user", ...)`.

**Estimated scope:** ~2,150 LOC across 5 code PRs + 1 doc PR (already
landed as `14-context-compaction-doctrine.md`).

## 2. Principle → PR mapping

| Principle | One-line | Satisfied by |
|---|---|---|
| P16 — Bus never compacted | structural invariant | all PRs preserve |
| P17 — View-layer compaction | derived ContextState | PR 2, PR 4 |
| P18 — Three typed events | summary_*  lifecycle | PR 3 |
| P19 — Structural validation, off-lock pre + under-lock commit | coordinator flow | PR 3, PR 5 |
| P20 — Summary ≠ workflow state | schema design | PR 2 |
| P21 — Thread membership first-class | Event.thread_id | PR 1 |
| P22 — Unified at SUMMARIZATION lease | dual-path commit | PR 5 |

## 3. Pre-v0.3.x state — what already exists

Verified 2026-05-16 against `/mmfs1/scratch/jacks.local/hdubey/07-LLM/loom-repo`:

- `RoomConfig.compact_threshold: int = 50` (`room.py:41`) — manual
  trigger; never auto-fires.
- `RoomState.last_compacted_event_id: int = -1` (`room.py:188`) —
  declared field; no production code currently mutates it (only
  snapshot serialise/restore at `journal.py:509, 798`).
- `RoomState.default_summarizer_id: Optional[str] = None`
  (`room.py:184`) — slot exists, set via
  `DefaultSummarizerSetEffect` from v0.3 PR 3.
- `build_prompt(...)` (`prompt.py:412`) — renders latest `summary`
  event as `<<<PRIOR ROOM SUMMARY (canonical compaction)>>>`
  (lines 526–537).
- Manual `/summary` slash command (`runtime.py:488`) — **reads**
  the latest `summary` event off the bus and returns its body;
  does NOT produce a new summary. The summary-event constructor
  itself lives at `events.py:1485` (`events.summary(body, ...)`);
  there is no scheduler or trigger that calls it today.
- `LeaseKind` enum (`leases.py:39`) — 5 members; SUMMARIZATION is the
  6th to add.
- `LeaseContext` tagged union (`leases.py:71–114`) — 5 context
  dataclasses; SummarizationContext is the 6th.
- 16 effect subclasses registered (`effects.py`); compaction adds 2
  more.
- 27 capability names (`capabilities.py:69`); compaction adds 2.
- `EventPlane` enum with `_KIND_TO_PLANE` mapping (`events.py`);
  compaction events add 5 entries.

## 4. PR sequence (5 PRs)

### PR 1 — Thread_id + ContextScope (~250 LOC)

**Goal:** Add `Event.thread_id` field with propagation rules; add
`ContextScope` dataclass; no other behavior change.

**Why first:** every downstream PR's events carry `thread_id`;
schema change must land before compaction events start populating it.

**Changes:**

- `loom/kernel/events.py`:
  - Add `Event.thread_id: str = "main"` field (default for backward
    compat).
  - Update `to_jsonl` / `from_jsonl` to round-trip the field;
    missing field loads as `"main"`.
  - Add `_EVENT_FIELDS` entry.
- `loom/kernel/leases.py`:
  - Add `thread_id: str = "main"` to each of the 5 existing
    `LeaseContext` subclasses.
- `loom/kernel/coordinator.py`:
  - Add `_emit_under_lease(lease, event)` helper that populates
    `event.thread_id = lease.context.thread_id` if not set.
  - Add `_emit_system(event, scope)` helper for coordinator-emitted
    events.
  - Refactor existing emit sites to route through these helpers (or
    pass explicit thread_id).
- `loom/kernel/bus.py`:
  - `EventBus.post` asserts `event.thread_id is not None`
    (after PR 1 default makes this trivially true; the assertion
    catches future bugs).
- `loom/kernel/context.py` (new, ~30 LOC):
  - `@dataclass(frozen=True) class ContextScope` with `room_id`,
    `thread_id="main"`, `actor_id=None`.

**Tests:**

- New `tests/test_kernel_context_scope.py:ContextScopeBasics`
  (6 tests): construction, equality, hashability, JSON round-trip.
- Extend `tests/test_kernel_events.py:EventThreadId` (8 tests):
  default `"main"`, round-trip, legacy load without field, explicit
  override.
- Extend `tests/test_kernel_coordinator.py:ThreadIdPropagation`
  (10 tests): each existing emit path propagates the correct
  thread_id; system events get coordinator-assigned value.
- Boundary: `tests/test_kernel_kernel_boundary.py:ThreadIdInvariant`
  (grep-based) asserts no `bus.post(...)` call constructs an event
  without thread_id passing through one of the two helpers.

**Risk:** MED. Touches `Event` envelope (serialized everywhere) and
every emit site in the coordinator. Mitigation: default
`thread_id="main"` makes the schema change non-breaking; existing
tests pin behavior.

**LOC:** ~250 (events.py +40; leases.py +20; coordinator.py +80;
bus.py +10; context.py +30; tests +70).

### PR 2 — ContextState + SummaryRecord + validators (~400 LOC)

**Goal:** Add `ContextState` subsystem to `KernelState`; define
`SummaryRecord` schema; implement structural validators.

**Why second:** PR 3's events depend on this schema; PR 4's prompt
builder reads `ContextState.active_summary_by_scope`.

**Changes:**

- `loom/kernel/context.py` (extend):
  - `@dataclass(frozen=True) class SummaryRecord` per doctrine §3.2.
  - `class SummaryFailureReason(str, Enum)` per doctrine §3.4.
  - `@dataclass class ContextState` per doctrine §3.3.
  - `def new_context_state() -> ContextState`.
  - `def validate_summary_record(record, bus_snapshot) -> tuple[bool,
    SummaryFailureReason | None, str | None]` — the off-lock
    pre-validator.
  - `def validate_lineage(record) -> tuple[bool, str | None]` —
    invariant check (range union, contiguity, no overlap).
- `loom/kernel/state.py`:
  - Add `context: ContextState` field to `KernelState`
    (`field(default_factory=new_context_state)`).
  - Bump the module-level constant
    `KERNEL_STATE_SCHEMA_VERSION = 6 → 7` at `state.py:51` (the
    `KernelState.schema_version` field defaults from it at
    `state.py:91`, so no field-default literal needs editing).
  - Update `KernelStateView` to expose `context` read-only.
- `loom/kernel/journal.py`:
  - `SNAPSHOT_VERSION` at `journal.py:84` aliases the kernel
    constant, so the bump propagates automatically; extend
    `_SUPPORTED_SNAPSHOT_VERSIONS` to include `7`.
  - Add `_migrate_v6_to_v7(state_dict) -> dict` — v6 snapshots load
    with empty `ContextState`.
  - Update `_state_to_dict` / `restore_kernel_state` for the new
    block.

**Tests:**

- New `tests/test_kernel_context_state.py:SummaryRecordShape`
  (8 tests): construction, lineage invariants, JSON round-trip,
  schema_version.
- `tests/test_kernel_context_state.py:LineageValidation` (12 tests):
  contiguous range ok; gap rejected; overlap rejected; cross-scope
  rejected; covers != union rejected.
- `tests/test_kernel_context_state.py:ContextStateBasics`
  (10 tests): empty default, active_summary_by_scope pointer
  semantics, supersession_edges, failure_count.
- `tests/test_kernel_context_state.py:StructuralValidator`
  (15 tests): each `SummaryFailureReason` produced by the right
  input.
- Extend `tests/test_kernel_journal.py` for v6→v7 migration
  (4 tests).
- Extend `tests/test_kernel_state.py:KernelStateContextSlot`
  (5 tests): slot wired; view exposes it; replay restores it.

**Risk:** MED. Touches `KernelState` (the v0.3 transactional root)
and snapshot schema. Mitigation: v6 → v7 migration is purely additive
(empty `ContextState`); v0.3.x deployments without compaction usage
get identical behavior.

**LOC:** ~400 (context.py +180; state.py +40; journal.py +60;
tests +120).

### PR 3 — Summary events + coordinator commit lifecycle (~450 LOC)

**Goal:** Add `summary_proposed` / `summary_committed` /
`summary_failed` event constructors, register effects, wire
coordinator commit flow with off-lock pre-validation + under-lock
commit.

**Why third:** events depend on PR 2's schema; PR 4 depends on
`summary_committed` actually firing to populate
`active_summary_by_scope`.

**Changes:**

- `loom/kernel/events.py`:
  - Add `summary_proposed(*, summary_id, scope, covers_event_range,
    proposed_text, retained_event_ids, input_summary_ids,
    input_event_ranges, model_id, prompt_hash, summarizer_id, ...)`.
  - Add `summary_committed(*, ..., supersedes_summary_ids,
    committed_at_event_id)`.
  - Add `summary_failed(*, proposed_summary_id, scope, reason,
    details, failed_validator, proposed_text, summarizer_id, ...)`.
  - Register all 3 in `_CONTROL_PAYLOAD_VALIDATORS`.
  - Add to `_KIND_TO_PLANE` mapping (all `CONTROL`).
  - Extend `CONTROL_TYPES`.
- `loom/kernel/effects.py`:
  - Add `SummaryProposedEffect` (no state mutation; audit only).
  - Add `SummaryCommittedEffect` (mutates
    `state.context.active_summary_by_scope`,
    `state.context.summaries`, `state.context.supersession_edges`).
  - Add `SummaryFailedEffect` (mutates
    `state.context.failure_count`).
  - Register all 3 reducers in `build_kernel_registry()`.
- `loom/kernel/coordinator.py`:
  - Add `_pending_summary_proposals: dict[str, SummaryProposal]` —
    in-flight proposals awaiting commit.
  - Add `submit_summary_proposed(record: SummaryRecord) ->
    SummaryCommitResult` — called by summarizer agent:
    1. Off-lock pre-validation via
       `validate_summary_record(record, bus_snapshot)`.
    2. On fail → emit `summary_failed`, return result.
    3. On pass → acquire lock briefly, check anchor conflict, emit
       `summary_committed` via `_apply_effect`.
  - Both emit paths route through `_emit_system(event,
    record.scope)` for thread_id assignment.

**Tests:**

- New `tests/test_kernel_summary_events.py:Constructors` (12 tests):
  each event shape + round-trip + validator coverage.
- New `tests/test_kernel_summary_lifecycle.py:HappyPath` (8 tests):
  propose → validate → commit; `active_summary_by_scope` updates;
  `kernel_state.version` bumps once.
- `:StructuralFailures` (10 tests): each failure reason produces
  correct `summary_failed`; no anchor advance on failure.
- `:AnchorConflict` (6 tests): second proposal racing the first
  loses with `ANCHOR_CONFLICT`; first commits cleanly.
- `:Supersession` (5 tests): re-summarization of overlapping range
  advances pointer; `supersession_edges` records old → new.
- `:OffLockPreValidation` (4 tests): pre-validation work happens
  without holding lock (introspection via
  `_assert_not_holding_lock`).
- Extend `tests/test_kernel_effects.py:ReducerBehavior` for the 3
  new effects.

**Risk:** HIGH. Lock discipline (off-lock pre + under-lock commit)
must be exact; race-condition path needs careful testing. Mitigation:
extensive race tests + replay determinism check.

**LOC:** ~450 (events.py +120; effects.py +80; coordinator.py +130;
tests +120).

### PR 4 — PromptBuilder integration + ContextManager (~400 LOC)

**Goal:** PromptBuilder reads `active_summary_by_scope` and renders
summary + retained events + tail. ContextManager exposes
`estimate_context_pressure()`.

**Why fourth:** depends on PR 3's events actually firing; replaces
the existing manual `summary` block.

**Changes:**

- `loom/kernel/prompt.py`:
  - Modify `build_prompt(...)` to consult `ContextState`:
    - Look up `scope = ContextScope(room_id, thread_id)`.
    - If `active_summary_by_scope[scope]` exists, render that
      SummaryRecord's text + retained events; truncate tail to
      events after `covers_event_range.end`.
    - If absent, fall back to current legacy rendering.
  - Existing `<<<PRIOR ROOM SUMMARY>>>` block becomes
    `<<<PRIOR SUMMARY>>>`; legacy `summary` events still render for
    backward compat during the transition.
- `loom/kernel/context.py` (extend):
  - `def estimate_context_pressure(view, participant_id, scope,
    kernel_state_version, prompt_template_hash) -> ContextPressure` —
    pure function; computes estimated tokens.
  - `@dataclass(frozen=True) class ContextPressure` with
    `estimated_tokens`, `max_context_tokens`, `threshold`,
    `pressure_ratio`, `needs_compaction`,
    `suggested_compaction_range`.
  - LRU cache keyed on
    `(participant_id, scope, kernel_state_version,
    prompt_template_hash)`.
  - `def select_compaction_range(state, scope) -> tuple[int, int]` —
    given current anchor, recommends the range to compact.
- `loom/kernel/room.py`:
  - Add `RoomConfig.context_pressure_threshold_ratio: float = 0.7`.
  - Add `RoomConfig.context_pressure_check_interval_events:
    int = 10`.
  - Add `RoomConfig.summarizer_max_consecutive_failures: int = 3`.

**Tests:**

- New `tests/test_kernel_prompt_compaction.py:RendersActiveSummary`
  (8 tests): commit a summary; build_prompt picks it up; tail
  truncates correctly; retained events appear.
- `:LegacyFallback` (4 tests): no `ContextState` summary → renders
  raw events; old `summary` events still render.
- `:RetainedEventsOrdering` (5 tests): retained events render in
  event-id order; summary block precedes tail.
- New `tests/test_kernel_context_pressure.py:Estimator` (12 tests):
  pure function; same inputs → same output; cache invalidation on
  version bump; `needs_compaction` threshold respected.
- New `tests/test_kernel_context_pressure.py:CacheKey` (5 tests):
  four-component key; participant or template change → cache miss.

**Risk:** MED. PromptBuilder is hot path; rendering regressions affect
every agent prompt. Mitigation: comprehensive golden-prompt tests.

**LOC:** ~400 (prompt.py +120; context.py +130; room.py +20;
tests +130).

### PR 5 — SUMMARIZATION lease + policy hook + control action + backoff (~650 LOC)

**Goal:** Wire end-to-end: SUMMARIZATION lease, EMIT_SUMMARY /
SUMMARIZE capabilities, policy-triggered Path A,
SummarizeControlAction Path B, backoff and disablement.

**Why last:** depends on all prior PRs (state, events, prompt
integration); largest coordination surface.

**Changes:**

- `loom/kernel/leases.py`:
  - Add `LeaseKind.SUMMARIZATION = "summarization"`.
  - Add `@dataclass(frozen=True) class SummarizationContext` with
    `scope`, `covers_event_range`, `triggered_by`,
    `triggering_event_id`, `thread_id` (inherited).
  - Update `Lease.__post_init__` invariant for the new kind.
- `loom/kernel/capabilities.py`:
  - Add `CapabilityName.EMIT_SUMMARY` and
    `CapabilityName.SUMMARIZE`.
  - Add corresponding meta-capabilities
    (`GRANT_CAPABILITY_EMIT_SUMMARY`, etc.).
- `loom/kernel/coordinator.py`:
  - New `_SummarizerSlotCheck` lease check with
    `applies_to = {SUMMARIZATION}` — verifies acquirer matches
    `RoomState.default_summarizer_id`.
  - Extend `_CapabilityCheck.applies_to` to include
    `SUMMARIZATION` (reads `required_capability` from context;
    populated as `EMIT_SUMMARY`).
  - Extend `_BudgetCheck.applies_to` to include `SUMMARIZATION`.
  - Update `_ParticipantRegistered/ActiveCheck.applies_to` to
    include `SUMMARIZATION`.
  - New `schedule_summarization(scope) -> SchedulingResult` —
    Path A entry; called from policy pre-turn hook:
    1. Check `scope not in disabled_scopes` and
       `default_summarizer_id is not None`.
    2. Acquire SUMMARIZATION lease for `default_summarizer_id`.
    3. On grant → emit `summarization_scheduled`.
  - Wire failure counting in `submit_summary_proposed`:
    - On `summary_failed` (reason ≠ ANCHOR_CONFLICT) → increment
      `failure_count[(summarizer_id, scope)]`.
    - On commit → reset count.
    - On count ≥ threshold → emit `compaction_disabled`, add to
      `disabled_scopes`.
  - Wire reset on `DefaultSummarizerSetEffect`: when slot changes,
    clear `disabled_scopes` and `failure_count` entries for the
    affected scope.
- `loom/kernel/events.py`:
  - Add `summarization_scheduled(*, scope, lease_id, summarizer_id,
    trigger_pressure_ratio)` constructor.
  - Add `compaction_disabled(*, scope, summarizer_id, failure_count,
    last_failed_summary_id, reason)`.
  - Register both in `_CONTROL_PAYLOAD_VALIDATORS` and
    `_KIND_TO_PLANE`.
- `loom/kernel/effects.py`:
  - Add `CompactionDisabledEffect` reducer.
- `loom/kernel/control_actions.py`:
  - Add `SummarizeControlAction` (kernel action #6):
    - `required_capability = CapabilityName.SUMMARIZE`.
    - `propose_effect(...)` acquires SUMMARIZATION lease internally;
      returns the resulting `summary_committed` effect chain.
- `loom/slash_commands.py`:
  - Add `/summarize [scope]` parser entry, routes through
    `propose_control_action("user", "summarize", {...})`.
- `loom/contracts.py` or policy base:
  - Add `ConversationPolicy.should_compact(view, scope) -> bool`
    optional hook (default returns `view.context.estimate_pressure(...)
    .needs_compaction`).
  - Add `ConversationPolicy.pre_turn_action(view) ->
    PreTurnAction | None`, where `PreTurnAction` includes
    `ScheduleSummarization(scope)`.

**Tests:**

- New `tests/test_kernel_summarization_lease.py:LeaseKindBasics`
  (8 tests): construction, context shape, applies_to filtering.
- `:CapabilityCheck` (6 tests): missing EMIT_SUMMARY → denied;
  present → granted.
- `:SlotCheck` (5 tests): non-default summarizer → denied; default
  → granted; slot=None → denied.
- `:BudgetCheck` (4 tests): SUMMARIZATION lease reserves budget;
  refunds on failure.
- New `tests/test_kernel_compaction_pathA.py:PolicyTriggered`
  (10 tests): pressure high → policy returns
  ScheduleSummarization → lease acquired → `summarization_scheduled`
  emitted → summarizer runs (mocked) → commit.
- New `tests/test_kernel_compaction_pathB.py:ControlAction`
  (10 tests): user `/summarize` → control_action_proposed →
  SUMMARIZE capability check → lease → commit.
- `:AgentInitiated` (4 tests): agent with SUMMARIZE proposes;
  works identically.
- New `tests/test_kernel_compaction_backoff.py:FailureCounting`
  (12 tests): each failure type increments correctly; commit
  resets; reassignment resets; ANCHOR_CONFLICT does NOT count.
- `:DisablementAndClear` (8 tests): 3 failures → disabled; Path A
  skips; Path B still works; reassignment clears.
- Extend `tests/test_runtime_slash_commands.py:SummarizeRouting`
  (5 tests): `/summarize` routes through control action; bypasses
  agent capability check per P15.

**Risk:** HIGH. Largest coordination surface; couples 5 v0.3
subsystems (lease, capability, budget, control action,
slash-command). Mitigation: each path tested independently;
integration tests cover Path A + Path B convergence at the lease.

**LOC:** ~650 (leases.py +50; capabilities.py +30;
coordinator.py +180; events.py +60; effects.py +30;
control_actions.py +70; slash_commands.py +20; contracts.py +30;
tests +180).

## 5. Sequencing dependency graph

```
PR 1 — thread_id + ContextScope
  │  [blocks all downstream PRs — every event must carry thread_id]
  │
PR 2 — ContextState + SummaryRecord + validators
  │  [blocks 3 (events need schema), 4 (prompt reads ContextState)]
  │
PR 3 — Summary events + commit lifecycle
  │  [blocks 4 (prompt reads committed summaries), 5 (lease invokes commit)]
  │
PR 4 — PromptBuilder integration + ContextManager
  │  [blocks 5 (policy hook reads estimate_context_pressure)]
  │
PR 5 — SUMMARIZATION lease + policy + control action + backoff
        [closes v0.3.x compaction]
```

**Hard constraints:** strict sequential. No parallel windows in
v0.3.x (compaction is a single tightly-coupled subsystem). Each PR
should land + be reviewed before the next opens.

**Total:** 5 code PRs. ~2,150 LOC. Estimated wall-time: 2–3 weeks
sequential.

## 6. Critical files

| File | PRs touching | Nature |
|---|---|---|
| `loom/kernel/context.py` | 1, 2, 4 | new in 1; extended in 2, 4 |
| `loom/kernel/events.py` | 1, 3, 5 | thread_id field; 5 new event ctors |
| `loom/kernel/leases.py` | 1, 5 | thread_id on contexts; SUMMARIZATION kind |
| `loom/kernel/state.py` | 2 | ContextState slot |
| `loom/kernel/coordinator.py` | 1, 3, 5 | emit helpers; commit lifecycle; lease wiring |
| `loom/kernel/effects.py` | 3, 5 | 4 new effects |
| `loom/kernel/capabilities.py` | 5 | 2 new verbs + meta |
| `loom/kernel/journal.py` | 2 | v6 → v7 migration |
| `loom/kernel/prompt.py` | 4 | reads ContextState |
| `loom/kernel/control_actions.py` | 5 | SummarizeControlAction |
| `loom/kernel/room.py` | 4 | 3 new config fields |
| `loom/kernel/bus.py` | 1 | thread_id invariant |
| `loom/slash_commands.py` | 5 | /summarize parser entry |
| `loom/contracts.py` | 5 | policy hook |
| `tests/test_kernel_context_scope.py` | 1 | new |
| `tests/test_kernel_context_state.py` | 2 | new |
| `tests/test_kernel_summary_events.py` | 3 | new |
| `tests/test_kernel_summary_lifecycle.py` | 3 | new |
| `tests/test_kernel_prompt_compaction.py` | 4 | new |
| `tests/test_kernel_context_pressure.py` | 4 | new |
| `tests/test_kernel_summarization_lease.py` | 5 | new |
| `tests/test_kernel_compaction_pathA.py` | 5 | new |
| `tests/test_kernel_compaction_pathB.py` | 5 | new |
| `tests/test_kernel_compaction_backoff.py` | 5 | new |
| `tests/test_kernel_kernel_boundary.py` | 1 | ThreadIdInvariant added |
| `CHANGELOG.md` | all PRs | `[Unreleased]` entries |
| `docs/internal/study/14-context-compaction-doctrine.md` | — | already landed |

## 7. Cross-cutting concerns

1. **Effect registry as single mutation path** (inherited from v0.3
   PR 3): every compaction state mutation goes through the registry.
   PR 3 adds 3 effects; PR 5 adds 1; none bypass.

2. **Lock discipline** (inherited from v0.3 PR 2): every new emit
   path goes through `_emit_under_lease` or `_emit_system`; every
   off-lock pre-validation path calls `_assert_not_holding_lock`.

3. **KernelStateView immutability** (inherited from v0.3 PR 1):
   `ContextState` exposed as frozen view in policy reads.

4. **Schema versioning**: PR 2 bumps snapshot v6 → v7;
   `SummaryRecord.schema_version = 1`; future v2 reducers ship
   alongside.

5. **Backward-compat journals**: v0.2.x and v0.3 journals without
   `summary_*` or `summarization_scheduled` events load cleanly
   producing empty `ContextState`. v0.3 journals without `thread_id`
   load with default `"main"`.

6. **Trace span scope**: SUMMARIZATION lease begins a span;
   `summary_proposed`/`committed`/`failed` events inherit it.
   `summarization_scheduled` carries a fresh span (kernel-side
   trigger).

## 8. Load-bearing invariants (inherited + new)

Must NOT regress through any v0.3.x PR. Boundary tests enforce.

1. (v0.2) Kernel does not import `loom.policy`.
2. (v0.2) `bus.post_internal` requires `_KERNEL_AUTH` token.
3. (v0.2) `ev.id == position` — append-only log.
4. (v0.2) `build_prompt` always renders `LOOM_PROTOCOL_INSTRUCTIONS`.
5. (v0.2) Replay is deterministic — no real-time clock calls in
   replay path. **Extended for v0.3.x:** replay never calls LLMs;
   `summary_committed` events carry canonical text used as-is.
6. (v0.2) Old v0.2.x `events.jsonl` files load cleanly.
7. (v0.3 PR 2) No long-running I/O under coordinator lock.
   **Extended:** off-lock pre-validation; under-lock commit only.
8. (v0.3 PR 3) Every applied event has a registered reducer for
   `(effect_type, schema_version)`.
9. **NEW (v0.3.x PR 1)**: Every event has `thread_id`. Default
   "main"; lease-produced events inherit from `LeaseContext`.
10. **NEW (v0.3.x PR 2)**: `ContextState` is derivable from
    `summary_*` events on the bus.
11. **NEW (v0.3.x PR 3)**: `summary_committed` is gated by
    structural validation only; no semantic checks.

## 9. v0.3.x completion gate

After all 5 PRs land, before tagging:

- [ ] All 7 doctrine principles (P16–P22) structurally satisfied.
- [ ] `KernelState.context: ContextState` slot wired.
- [ ] Snapshot v7 migration tested (v6 → v7, empty ContextState).
- [ ] Path A (policy-triggered) and Path B (control-action) both
      produce identical `summary_committed` payloads given the same
      input.
- [ ] `LockDisciplineBoundary` test extended for compaction paths.
- [ ] `ThreadIdInvariant` boundary test green.
- [ ] Backoff: 3 consecutive failures disable scope;
      `DefaultSummarizerSetEffect` clears.
- [ ] No regression on the 975 v0.3 session 1 kernel tests.
- [ ] CHANGELOG `[v0.3.x]` section ready; entries per PR.
- [ ] `docs/internal/study/00-orientation.md` updated with
      compaction subsystem.

## 10. Verification plan (end-to-end)

After all 5 PRs land:

1. **Boundary tests**: `python -m unittest
   tests.test_kernel_kernel_boundary` — clock + lock +
   effect-registry-coverage + thread_id invariant.
2. **Round-trip determinism**: populate `ContextState` with N
   summaries; snapshot v7; clear; restore; verify identical state.
3. **Backward-compat**: load a v0.3 session 1 `events.jsonl`;
   verify empty `ContextState`; no errors.
4. **Path convergence**: same scope, same summarizer; trigger Path
   A and Path B; assert resulting `summary_committed` payloads
   match field-for-field (except trigger metadata).
5. **Race condition**: two summarizers race on same scope; verify
   one commits, one fails with `ANCHOR_CONFLICT`.
6. **Backoff matrix**: 3 consecutive `SCHEMA_ERROR` →
   `compaction_disabled`; `DefaultSummarizerSetEffect` clears;
   Path A resumes.
7. **Rolling compaction**: commit summary 1 (events 1–100); commit
   summary 2 inputting summary 1 + events 101–200; verify
   `active_summary_by_scope` points to summary 2; verify
   `supersession_edges[summary_1] == summary_2`.
8. **Prompt rendering**: golden-prompt tests for each scope/summary
   combination.
9. **Thread_id propagation**: emit events from each lease kind;
   verify thread_id matches lease.context.thread_id.
10. **Slash command parity**: `/summarize` produces the same final
    `summary_committed` as a manual control-action invocation.

## 11. Out of scope (deferred to v0.4+)

Per doctrine §11:

- Pinning subsystem (PIN_EVENT + pin/unpin events).
- Per-actor summaries (`ContextScope.actor_id`).
- `summary_reviewed` step.
- Summary redaction for compliance.
- Selectable summary input filters.
- Cross-thread summary propagation.
- Workflow subsystem + CollaborativePlanWorkflow (v0.5+).
- Summarizer quality review / quorum commit.

Two open doctrinal questions to resolve before v0.4:

- Path B slot strictness (allow `/summarize <alt-agent>`?).
- `NO_FORWARD_PROGRESS` failure for identical-range re-summarization.
- Rolling compaction depth limit (default 5?).
- `PromptViewSpec` formalization.

## 12. Provenance

This roadmap was derived from:

- **Doctrine** (`14-context-compaction-doctrine.md`, locked
  2026-05-16) — four rounds of design dialogue between implementing
  engineer and external reviewer.
- **Pre-state survey** (2026-05-16) — `grep`-based read of
  compaction-adjacent code in `loom/kernel/` confirming existing
  manual `/summary` path and the `default_summarizer_id` slot.
- **v0.3 session 1 completion** (memory entry
  `[[loom-v0-3-session1-pr0-pr13]]`) — confirms the structural
  prerequisites (KernelState, Lease, EffectRegistry,
  CapabilityState, BudgetLedger, ControlAction, EventPlane, slash
  commands) are in place.
- **v0.3 implementation roadmap shape**
  (`13-v03-implementation-roadmap.md`) — used as template for
  per-PR detail tables, sequencing graph, and verification
  sections.

**Previous related plans:**
- v0.2 refactor (12 PRs, complete) — `[[project_loom_v02_refactor]]`.
- v0.2.1 hardening (5 PRs, complete) —
  `[[loom-v0-2-1-hardening-complete]]`.
- v0.3 session 1 (14 PRs, complete) —
  `[[loom-v0-3-session1-pr0-pr13]]`.
- This roadmap (v0.3.x, 5 PRs, planning) supersedes the
  collaborative-plan-mode design dialogue that produced
  `14-context-compaction-doctrine.md`.
