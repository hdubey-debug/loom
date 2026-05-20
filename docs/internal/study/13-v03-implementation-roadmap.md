# 13 — v0.3 Implementation Roadmap

**Roadmap date**: 2026-05-16
**Repo state**: post-v0.2.1 hardening (5 PRs landed 2026-05-16; 1,183 tests pass).
**Gates against**: `11-orchestration-os-doctrine.md` (frozen 2026-05-16).
**Status**: planning artifact; canonical "where are we in v0.3" reference for the
v0.3 implementation cycle.

---

## 1. Purpose & gating from v0.2.1

The v0.3 doctrine in `11-orchestration-os-doctrine.md` reframes Loom from a
conversation kernel into an agent-OS substrate. Per its preface, every v0.3 PR
must cite the doctrine principle(s) it implements or the subsystem specification
it satisfies. This roadmap exists to make those citations unambiguous: it maps
each principle and subsystem onto a specific PR, sequences the PRs into phases
under hard dependency constraints, and records the v0.4-readiness gate that
v0.3-completion will be measured against.

**Gating from v0.2.1** is now satisfied. The eight-item v0.3-readiness
checklist from `12-v02-hardening-audit.md` §10 closed when the v0.2.1 audit + 5
follow-on PRs landed on 2026-05-16:

- Audit document published (`12-v02-hardening-audit.md`).
- Lease TTL authoritative — `RoomCoordinator.check_lease_ttl` proactively
  flushes expired leases; wired into `_watchdog_loop`.
- All control events have typed constructors; `_CONTROL_PAYLOAD_VALIDATORS`
  dispatch table seeded.
- Every event envelope carries `schema_version: int = 1` and
  `causal_refs: tuple = ()` (reserved slot for v0.3 typing).
- Cursor advance is dispatch-outcome-aware (`_denied_trigger_ids` set + LRU
  re-pending of unmet direct mentions).
- Clock discipline structurally enforced (`ClockDisciplineBoundary` test +
  `docs/timing-discipline.md`).
- CHANGELOG entries staged under `[Unreleased]` (release-cut renames to
  `[v0.2.1]`).
- Orientation doc reflects hardened state.

What v0.2.1 explicitly **deferred to v0.3** (audit §12), and which PR closes
each:

| v0.2.1 deferral | Closed by |
|---|---|
| A3 — cursor persistence via `cursor_advanced` events | PR 13 |
| C3 typed — `tuple[CausalRef, ...]` envelope typing | PR 4 |
| C4 full — `(effect_type, schema_version)` reducer registry | PR 3 |
| D2 — streaming-stall watchdog | PR 12 |
| D3 — `policy_slow_threshold_ms` → `RoomConfig` field | PR 13 |

The doctrine is what v0.3 builds; the v0.2.1 audit was what *had to be true
about v0.2* before v0.3 could begin. Both pieces are in place.

---

## 2. Doctrine → PR mapping

### Principles

The 15 normative principles (P1–P15) each map to a single owning PR. Two are
already satisfied as of v0.2.1 and only need invariant-preserving extensions in
v0.3 work; the rest each get concrete code in this cycle.

| Principle | One-line | Satisfied by |
|---|---|---|
| P1 — no CEO; capabilities are atomic verbs | PR 5 |
| P2 — three event planes (Conversation, Control, Execution) | PR 8 |
| P3 — no scheduler bypass | v0.2.1 (extended PRs 7, 9) |
| P4 — no long-running I/O under coordinator lock | PR 2 + PR 12 |
| P5 — unified `KernelState` transactional root | PR 1 |
| P6 — event-sourced replay applies committed effects | PR 3 + PR 13 |
| P7 — applied events record versioned `ControlEffect` instances | PR 3 |
| P8 — one `Lease` abstraction with `kind` discriminator | PR 7 |
| P9 — three-way budget reservation/commit/refund | PR 6 |
| P10 — capabilities are atomic verbs in `CapabilityState` | PR 5 |
| P11 — typed `causal_refs: tuple[CausalRef, ...]` | PR 4 |
| P12 — trace metadata on every event | PR 4 |
| P13 — pure policy with frozen `KernelStateView` | v0.2.1 (extended PR 9) |
| P14 — custom actions return typed built-in effects only | PR 9 |
| P15 — human root actions use control-action path | PR 11 |

### Subsystem specifications

The 10 subsystem specifications (§1–§10) of the doctrine each map to one owning
PR, with several leaning on the foundation set by PRs 1 and 3.

| Subsystem | One-line | Satisfied by |
|---|---|---|
| §1 `KernelState` architecture | transactional root | PR 1 |
| §2 Lock discipline | structural enforcement | PR 2 |
| §3 Lease abstraction | unified five-kind lease | PR 7 |
| §4 Event taxonomy | three planes + `lease_closed` | PR 8 |
| §5 Effect vocabulary & registry | 13 effects + reducer registry | PR 3 |
| §6 Capability ledger | grant/revoke/expire | PR 5 |
| §7 Control action spec | three registration layers + ControlInterest | PR 9 |
| §8 Causal refs & trace | typed CausalRef + TraceContext | PR 4 |
| §9 Budget ledger | reservation/commit/refund | PR 6 |
| §10 Policy/control precedence | scoped overrides | PR 10 |

---

## 3. Phase grouping

The 13 code PRs (plus PR 0, this document) are grouped into five phases:

- **Phase A — Foundation (PRs 1–3)**. Establish the architectural primitives
  every later PR depends on: a transactional state root, structural lock
  discipline, a typed effect registry. Strictly sequenced.
- **Phase B — Metadata (PR 4)**. Finalize causal-ref typing and add trace
  context. Can merge any time after PR 1; intentionally early so subsequent
  control-plane events can populate causality.
- **Phase C — Domain subsystems (PRs 5–7)**. The three first-class subsystems
  on `KernelState`: capability ledger, budget ledger, unified lease
  abstraction. PRs 5 and 6 are independent; PR 7 wires both into lease
  lifecycle.
- **Phase D — Control plane (PRs 8–10)**. Event taxonomy + `lease_closed`
  unification, then control-action dispatch (with custom actions and
  ControlInterest), then scoped floor overrides. PR 9 depends on PRs 5/7;
  PR 10 depends on PRs 8/9.
- **Phase E — Closures (PRs 11–13)**. The remaining doctrine items and the
  audit deferrals that needed earlier subsystems: human root actions through
  the slash-command path, off-lock policy + streaming-stall watchdog, cursor
  persistence + per-policy threshold.

---

## 4. Per-PR detail

For each PR: goal, doctrine principles addressed, primary files touched,
representative test class names, risk rating, LOC estimate. File-line citations
where given are against the working copy as of 2026-05-16; lines may drift as
PRs land — the principle and subsystem citations remain the canonical anchor.

### PR 0 — Publish v0.3 implementation roadmap

- **Goal**: This document.
- **Doctrine**: gating artifact only; cites all P1–P15.
- **File**: `docs/internal/study/13-v03-implementation-roadmap.md` (new).
- **Tests**: none.
- **Risk**: none (docs only).
- **LOC**: ~600.

### Phase A — Foundation

#### PR 1 — `KernelState` restructure

- **Goal**: Migrate the single mutable state object from `RoomState` to a
  transactional `KernelState` root holding all subsystem states. Establishes
  the `KernelStateView` frozen-view discipline.
- **Doctrine**: **P5**, §1.
- **Files**:
  - `loom/kernel/state.py` (new) — `KernelState` dataclass with `room`,
    `capabilities` (placeholder for PR 5), `budget` (placeholder for PR 6),
    reserved `workflow`/`tools` for v0.4+/v0.5+; `version: int = 0` bumped on
    every applied event; `schema_version: int = 6`.
  - `loom/kernel/coordinator.py:389-1403` — every method touching
    `self._state` is rewritten over `KernelState`, delegating to `.room`
    for v0.2 surfaces.
  - `loom/kernel/journal.py:73` — `SNAPSHOT_VERSION = 5 → 6`; add
    `_migrate_v5_to_v6`.
  - `loom/kernel/room.py:150-338` — `RoomState` continues to exist but is
    accessed through `KernelState.room`.
- **Tests**: `tests/test_kernel_state.py:KernelStateBasics` (8 tests);
  `SnapshotMigration` (4 tests); extend `tests/test_kernel_journal.py` for
  v5→v6 backward-compat.
- **Risk**: HIGH — touches every coordinator method and the snapshot path.
  Mitigation: behavior-preservation refactor before any new fields.
- **LOC**: ~300.

#### PR 2 — Lock discipline enforcement

- **Goal**: Structural guarantee that no long-running I/O happens under the
  coordinator lock. Adopt naming convention (`_apply_*`, `_validate_*`,
  `_reserve_*`, `_commit_*`, `_refund_*`) that marks under-lock code; require
  I/O entry points to assert no lock held.
- **Doctrine**: **P4**, §2.
- **Files**:
  - `loom/kernel/coordinator.py` — `_assert_not_holding_lock(where: str)`
    helper added; assertions wired into I/O entry points.
  - `loom/kernel/streaming.py` — `run_streaming_call` entry asserts.
  - `docs/lock-discipline.md` (new) — documents naming convention and
    boundary.
  - `tests/test_kernel_kernel_boundary.py` — `LockDisciplineBoundary` test
    class (grep-based; follows `ClockDisciplineBoundary` pattern).
- **Tests**: `tests/test_kernel_coordinator.py:LockDiscipline` (5 tests);
  boundary class.
- **Risk**: LOW (additive enforcement; no behavior change).
- **LOC**: ~150.

#### PR 3 — Effect vocabulary + registry

- **Goal**: Introduce typed, versioned semantic effects with a reducer
  registry. Closes v0.2.1 deferral C4 (full registry). Foundation for all
  subsequent control-plane work.
- **Doctrine**: **P6**, **P7**, §5.
- **Files**:
  - `loom/kernel/effects.py` (new) — `ControlEffect` base; 13 effect
    subclasses (FloorOverrideEffect, TopicChangedEffect, AnchorAssignedEffect,
    DefaultResponderSetEffect, RolesAssignedEffect, LeaseCancelledEffect,
    CapabilityGrantedEffect, CapabilityRevokedEffect,
    CapabilityExpiredEffect, PolicySwitchedEffect, BudgetReservedEffect,
    BudgetCommittedEffect, BudgetRefundedEffect); `EffectRegistry`;
    `build_kernel_registry()`.
  - `loom/kernel/coordinator.py:671-761` — slot mutations
    (`set_topic`, `set_anchor`, `set_default_responder`, `set_roles`,
    `set_style`) become two-step: construct `ControlEffect`, apply via
    `_apply_effect`.
- **Tests**: `tests/test_kernel_effects.py:EffectRegistry` (10 tests);
  `ReducerBehavior` (13 tests, one per effect). Existing
  `tests/test_kernel_coordinator.py` slot-setter behavior unchanged.
- **Risk**: MED. Wide-touching refactor (every slot mutation through registry).
- **LOC**: ~350.

### Phase B — Metadata

#### PR 4 — Typed `causal_refs` + trace context

- **Goal**: Finalize the `CausalRef` type system reserved by v0.2.1 PR 3, and
  add `TraceContext` for observability. Closes v0.2.1 deferral C3 (typed
  variant).
- **Doctrine**: **P11**, **P12**, §8.
- **Files**:
  - `loom/kernel/causality.py` (new) — `EventRef`, `CausalRelation` enum,
    `CausalRef`, `TraceContext`, helpers (`new_trace`, `child_span`).
  - `loom/kernel/events.py:270-319` — tighten `causal_refs` to
    `tuple[CausalRef, ...]`; add `trace: TraceContext | None = None`;
    round-trip both via `to_jsonl`/`from_jsonl` with defaults for legacy.
  - `loom/kernel/coordinator.py` — inject `TraceContext`: room-scoped
    `trace_id`; lease acquisition begins a span; events posted under lease
    inherit the lease's `span_id`.
- **Tests**: `tests/test_kernel_causality.py` (12 tests);
  `tests/test_kernel_events.py:CausalRefRoundTrip` (6 tests);
  `TraceContextRoundTrip` (4 tests).
- **Risk**: MED. Touches event envelope. Mitigation: defaults applied for
  legacy events.
- **LOC**: ~250.

### Phase C — Domain subsystems

#### PR 5 — `CapabilityState` + capability events

- **Goal**: First-class capability ledger as a `KernelState` subsystem. Atomic
  verb vocabulary, full grant lifecycle, anti-escalation invariant.
- **Doctrine**: **P1**, **P10**, §6.
- **Files**:
  - `loom/kernel/capabilities.py` (new) — `CapabilityName` enum
    (mutation + meta `GRANT_CAPABILITY_*`/`REVOKE_CAPABILITY_*` verbs);
    `CapabilityGrant` dataclass with `grant_id`, `grantor_id`, `grantee_id`,
    `capability`, `granted_at`, `expires_at`, `revoked_at`,
    `source_event_id`; `CapabilityState` with `has()`, `grants_for()`,
    `effective_capabilities()`; `flush_expired()`.
  - `loom/kernel/events.py` — `capability_granted`, `capability_revoked`,
    `capability_expired` constructors; register in
    `_CONTROL_PAYLOAD_VALIDATORS`.
  - `loom/kernel/effects.py` — reducers for capability effects.
  - `loom/kernel/room.py` — `ParticipantInfoView.capabilities` computed
    via `KernelState.capabilities.effective_capabilities(pid)`.
  - `loom/kernel/coordinator.py:1138-1155` — `_watchdog_loop` flushes
    expired grants.
- **Tests**: `tests/test_kernel_capabilities.py:CapabilityLedger` (15 tests);
  `AntiEscalation` (4 tests);
  `tests/test_kernel_events.py:CapabilityEvents` (6 tests); property test
  for replay-determinism.
- **Risk**: MED. New subsystem; coordinates with PR 3 registry.
- **LOC**: ~400.

#### PR 6 — `BudgetLedger` + three-way accounting

- **Goal**: Reserve estimated cost on lease acquisition, commit actual cost on
  completion, refund on denial, partial-commit-and-refund on
  validation-failure-after-LLM. Scope hierarchy with parent-limit guard.
- **Doctrine**: **P9**, §9.
- **Files**:
  - `loom/kernel/budgets.py` (new) — `BudgetScope`, `BudgetReservation`,
    `BudgetLedger` with `can_reserve`, `reserve`, `commit`, `refund`,
    `partial_commit_and_refund`.
  - `loom/kernel/events.py` — `budget_reserved`, `budget_committed`,
    `budget_refunded` constructors.
  - `loom/kernel/effects.py` — reducers for budget effects.
  - `loom/kernel/coordinator.py:1224-1311` — lease lifecycle wires
    reservation on acquire, commit on release(reason="released"),
    partial-commit on `aborted_validation`, refund on
    `denied`/`expired`/`cancelled`.
  - `RoomConfig` — `budget_limits: dict[BudgetScope, float]`.
- **Tests**: `tests/test_kernel_budgets.py:Ledger` (20 tests);
  `ThreeWayAccounting` (10 tests);
  `tests/test_kernel_coordinator.py:LeaseLifecycle` extended (8 tests);
  property: `sum(reservations) ≤ sum(limits)` always.
- **Risk**: HIGH. Tight coupling with every lease termination path.
  Mitigation: lifecycle-matrix tests.
- **LOC**: ~500.

#### PR 7 — Unified `Lease` abstraction

- **Goal**: Generalize v0.2.1's single `TurnLease` to five lease kinds under
  one dataclass with applicability-filtered checks. Drop kind-aware filtering
  from check bodies; encode it as `LeaseCheck.applies_to`.
- **Doctrine**: **P8**, §3.
- **Files**:
  - `loom/kernel/coordinator.py:107-124` — new `LeaseKind` enum
    (`USER_TURN`, `CONTROL_ACTION`, `TOOL_INVOCATION`, `WORKFLOW_STEP`,
    `REACTIVE`); `LeaseContext` tagged union (one frozen dataclass per
    kind); `Lease` dataclass generalizing `TurnLease`; `TurnLease` retained
    as `Lease[USER_TURN]` shim for one release.
  - `loom/kernel/coordinator.py:372-381` — each of the 8 default checks
    gains `applies_to: frozenset[LeaseKind]`; add `_CapabilityCheck` for
    `{CONTROL_ACTION}`; `_BudgetCheck` for all kinds.
  - `acquire_lease` signature → `(kind, holder, context)` returning
    `Optional[Lease]`.
- **Tests**: `tests/test_kernel_coordinator.py:LeaseKindDispatch` (12 tests);
  `LeaseContextRoundTrip` (5 tests); existing tests assert `applies_to` is
  set; property: `lease.kind` always matches `lease.context` tag.
- **Risk**: HIGH. Touches every existing lease site. Mitigation:
  `TurnLease` shim; pinned user_turn tests.
- **LOC**: ~600.

### Phase D — Control plane

#### PR 8 — Event taxonomy + `lease_closed` unification

- **Goal**: Add Control-plane and Execution-plane event types; unify
  `lease_denied`/`lease_expired` under one `lease_closed` event with reason
  discriminator. Add `EventPlane` enum and `_KIND_TO_PLANE` mapping.
- **Doctrine**: **P2**, §4.
- **Files**:
  - `loom/kernel/events.py` — `EventPlane` enum;
    `Event.plane: EventPlane` field; new constructors
    `control_action_proposed`, `control_action_applied`,
    `control_action_denied`, `lease_closed(reason=...)`; deprecate (but
    keep loading)`lease_denied`/`lease_expired`.
  - `loom/kernel/coordinator.py` — `release_lease`, `check_lease_ttl` emit
    `lease_closed` going forward.
- **Tests**: `tests/test_kernel_events.py:EventPlane` (8 tests);
  `LeaseClosedUnification` (12 tests); coordinator tests assert new pattern;
  boundary: v0.2.1 journals with legacy events still replay.
- **Risk**: MED. Renames + plane classification; backward-compat for replay.
- **LOC**: ~200.

#### PR 9 — Control action dispatch + custom actions

- **Goal**: Full control-action lifecycle (propose → capability check → lease
  acquisition → effect application → applied/denied event), three
  registration layers (kernel, `RoomConfig.custom_control_actions`,
  `ConversationPolicy.control_actions_for_participant`), per-participant
  `ControlInterest`, full `DenialReason` taxonomy. **P14**: custom actions
  must return built-in effects only.
- **Doctrine**: **P3** (extended), **P13** (extended), **P14**, §7.
- **Files**:
  - `loom/kernel/control_actions.py` (new) — `ControlAction` protocol; 9
    kernel actions (`SetTopicAction`, `SetAnchorAction`,
    `SetDefaultResponderAction`, `UpdateAllowedSpeakersAction`,
    `SetRolesAction`, `SwitchPolicyAction`, `SendDMAction`,
    `GrantFloorAction`, `CancelLeaseAction`); `ControlActionRegistry`;
    `ControlInterest`; `DenialReason` enum.
  - `loom/policy/base.py` — `control_actions_for_participant` hook.
  - `loom/kernel/coordinator.py` — `propose_control_action(proposer_id,
    action_name, params) → ControlActionResult`.
  - `loom/kernel/events.py` — `control_action_*` constructors.
- **Tests**: `tests/test_kernel_control_actions.py:ActionRegistry` (8
  tests); `ProposalLifecycle` (15 tests); `DenialPath` (10 tests);
  `CustomAction` (5 tests); policy tests for the new hook.
- **Risk**: HIGH. Large new subsystem; tight coupling to PRs 3, 5, 7.
- **LOC**: ~600.

#### PR 10 — Policy/control precedence with `FloorOverrideEffect`

- **Goal**: Scoped, multi-mode floor overrides composing with policy plans in
  journal order. Three modes (ADD/REPLACE/BLOCK) × four scopes
  (ONE_LEASE/CURRENT_TURN/UNTIL_CLEARED/PERSISTENT_ROOM_CONFIG). Coordinator
  prunes expired overrides at lifecycle events.
- **Doctrine**: §10.
- **Files**:
  - `loom/kernel/effects.py` — `FloorOverrideMode`/`FloorOverrideScope`
    enums; full reducer for `FloorOverrideEffect` (declared in PR 3); 
    `ActiveOverride` dataclass appended to
    `state.room.control.active_overrides`.
  - `loom/kernel/coordinator.py` — `_AllowedSpeakerCheck` reads composed
    effective speakers (base ∩ ADD ∖ BLOCK / REPLACE); lifecycle pruning.
  - `loom/kernel/control_actions.py` — `GrantFloorAction`,
    `BlockFloorAction`, `OverrideAllowedSpeakersAction`.
- **Tests**: `tests/test_kernel_coordinator.py:FloorOverrides` (15 tests, one
  per mode×scope minor cell); property: effective speakers deterministic
  function of base plan + override sequence.
- **Risk**: MED. New override logic; tight coupling with check chain.
- **LOC**: ~250.

### Phase E — Closures

#### PR 11 — Human root actions

- **Goal**: Slash-command parser routes user input through the same
  control-action path as agent actions, with `actor_id="user"`. User
  bypasses agent capability checks (P15) but goes through the same lease +
  effect + journal path.
- **Doctrine**: **P15**.
- **Files**:
  - `loom/runtime/slash_commands.py` (new) — parser; recognized commands
    (`/grant`, `/revoke`, `/topic`, `/anchor`, `/floor`, `/responder`,
    `/policy`); extensible via `RoomConfig.custom_slash_commands`.
  - User-input path — if text starts with `/`, parse and dispatch via
    `coordinator.propose_control_action(proposer_id="user", ...)`.
- **Tests**: `tests/test_runtime_slash_commands.py:Parser` (12 tests);
  `RoutingThroughKernel` (8 tests, asserts user slash produces the same
  `control_action_applied` envelope as agent dispatch).
- **Risk**: LOW (additive layer above PR 9).
- **LOC**: ~150.

#### PR 12 — Off-lock policy + streaming-stall watchdog

- **Goal**: Move policy classification off the coordinator lock with
  epoch-revalidation retry; add a streaming-stall watchdog that emits a
  control event when a stream produces no chunks for N seconds despite an
  active lease. Closes v0.2.1 deferral D2.
- **Doctrine**: **P4** (extended).
- **Files**:
  - `loom/kernel/coordinator.py:806-869` — rename
    `_run_policy_under_lock → _run_policy`; release lock during
    `classify_fn` call; re-acquire to validate epoch hasn't shifted; emit
    `policy_aborted` on retry exhaustion.
  - `loom/kernel/coordinator.py:1138-1155` — `_watchdog_loop` gains
    `check_streaming_stall`; `_last_chunk_at: dict[int, float]` tracking.
  - `loom/kernel/streaming.py` — `on_stream_chunk` hook calls back into
    coordinator to update `_last_chunk_at`.
  - `loom/kernel/events.py` — `stream_stalled(lease_id, holder,
    seconds_silent)` constructor.
  - `RoomConfig.stream_stall_threshold_s: float = 30.0`.
- **Tests**: `tests/test_kernel_coordinator.py:OffLockPolicy` (10 tests);
  `StreamingStallWatchdog` (8 tests); boundary: streaming code path
  passes `_assert_not_holding_lock` from PR 2.
- **Risk**: HIGH. Threading change in a hot path. Mitigation: epoch
  validation + retries + stress tests.
- **LOC**: ~400.

#### PR 13 — Cursor persistence + per-policy threshold

- **Goal**: Persist actor cursor via `cursor_advanced` semantic event through
  the registry. Add minimal `ActorStateRecord` on `KernelState.actors`.
  Move `_POLICY_SLOW_THRESHOLD_MS` to `RoomConfig`. Closes v0.2.1 deferrals
  A3 and D3.
- **Doctrine**: **P6** (extended).
- **Files**:
  - `loom/kernel/state.py` — `actors: dict[str, ActorStateRecord]`
    field; `ActorStateRecord(participant_id, cursor,
    last_advanced_at_event_id)`.
  - `loom/kernel/effects.py` — `CursorAdvancedEffect`; reducer.
  - `loom/kernel/actor.py` — `_advance_cursor` builds effect; applies via
    `coordinator.apply_actor_effect`; updates local `_cursor` only after
    confirmation; `__init__` recovers cursor from
    `KernelState.actors` if present.
  - `loom/kernel/coordinator.py:76` — `_POLICY_SLOW_THRESHOLD_MS` moves
    to `RoomConfig.policy_slow_threshold_ms: float = 100.0`.
- **Tests**: `tests/test_kernel_actor.py:CursorPersistence` (12 tests);
  property: replay of `cursor_advanced` sequence reconstructs identical actor
  state; extend coordinator tests for config-driven threshold.
- **Risk**: MED. Effect type integrates with registry; tight-loop checks must
  continue to work.
- **LOC**: ~250.

---

## 5. Sequencing dependency graph

```
PR 0 — Roadmap doc                       [no code]
  │
Phase A (foundation):
  ├─→ PR 1 — KernelState restructure      [blocks 3, 5, 6, 7]
  │     │
  │     ├─→ PR 2 — Lock discipline        [independent of 3]
  │     │
  │     └─→ PR 3 — Effect registry        [blocks 5, 6, 7, 8, 10, 13]
  │           │
Phase B (metadata):
  │           ├─→ PR 4 — Causal refs + trace  [merge anytime after 1]
  │
Phase C (domain):
  │           ├─→ PR 5 — CapabilityState   [blocks 9]
  │           │
  │           ├─→ PR 6 — BudgetLedger      [blocks 7]
  │           │
  │           └─→ PR 7 — Lease unification [needs 6; blocks 9, 10, 12]
Phase D (control plane):
  │
  │           PR 8 — Event taxonomy       [needs 3, 7; blocks 9, 10]
  │             │
  │             ├─→ PR 9 — Control action dispatch  [needs 5, 7, 8]
  │             │
  │             └─→ PR 10 — Floor override precedence  [needs 8, 9]
  │
Phase E (closures):
  │
  │           PR 11 — Human root actions  [needs 9; independent of 10, 12, 13]
  │           PR 12 — Off-lock + stall    [needs 7 (lease kind)]
  │           PR 13 — Cursor persistence  [needs 3, 4]
```

**Hard constraints**:

- PR 0 first (reviewers need roadmap context).
- PR 1 blocks all subsequent state-touching PRs.
- PR 3 blocks all subsystem PRs (they register reducers).
- PR 7 blocks all lease-touching PRs (8, 9, 12).
- PR 5 blocks PR 9 (capability check is the precondition for control-action
  dispatch).

**Parallel windows**:

- PRs 2 and 4 can land any time after PR 1.
- PRs 5 and 6 can run in parallel after PR 3.
- PRs 11, 12, 13 can run in parallel after their respective dependencies.

**Total**: 13 code PRs + 1 doc PR. Estimated **~3,850 LOC**. Wall-time:
4–6 weeks sequential, ~3 weeks parallelized.

---

## 6. Cross-cutting concerns

These rules apply to every PR in the cycle; reviewers should flag any
violation regardless of which PR is in front of them.

1. **Effect registry as the single mutation path**. After PR 3, every state
   mutation goes through `coordinator._apply_effect`. PRs 5–13 add reducer
   entries to the registry; no PR adds inline state mutation.
2. **Lock discipline**. After PR 2, every I/O entry point asserts no lock
   held via `_assert_not_holding_lock`. Every subsequent PR's new I/O entry
   must add the assertion. Naming convention enforced by
   `LockDisciplineBoundary` grep.
3. **`KernelStateView` immutability**. After PR 1, policy receives frozen
   views. PRs 5 and 6 extend views with `capabilities` and `budget`
   read-only projections.
4. **Schema versioning rules**. All new effect types ship with
   `schema_version: int = 1`. v0.4+ can ship v2 reducers alongside v1; the
   registry indexes on `(effect_type, schema_version)`. Envelope
   `schema_version` remains the v0.2.1 baseline (`1`); only effect bodies
   bump.
5. **Backward-compat journals**. PR 1 (snapshot v6 from v5), PR 8 (legacy
   `lease_denied`/`lease_expired` still load), PR 4 (legacy events with
   `causal_refs=()`/`trace=None` still load) — v0.2.x replay capability is
   non-negotiable.
6. **Trace span scope**. After PR 4, every lease has a `span_id`; events
   posted under a held lease inherit it; lease closure ends the span.

---

## 7. v0.3-completion gate (v0.4-readiness checklist)

After all 13 PRs land, the following must be true before v0.4 work can open:

- [ ] All 10 doctrine subsystems implemented (§1–§10).
- [ ] All 15 doctrine principles satisfied (P1–P15).
- [ ] All five v0.2.1 deferrals closed (A3, C3 typed, C4 full, D2, D3).
- [ ] No public code references `RoomState` directly (all through
      `KernelState.room`).
- [ ] No inline state mutations — every mutation goes through the effect
      registry.
- [ ] Every I/O entry point asserts no lock held.
- [ ] Every event carries `causal_refs` (where applicable) and `trace`.
- [ ] CHANGELOG `[v0.3]` section finalized at release-cut.
- [ ] `docs/internal/study/00-orientation.md` updated to describe v0.3
      agent-OS architecture.
- [ ] All boundary tests pass (`ClockDisciplineBoundary`,
      `LockDisciplineBoundary`, `EffectRegistryCoverage`).

---

## 8. Deferrals from v0.3 to v0.4+

Items the doctrine reserves but v0.3 explicitly does not ship:

- **Tool subsystem** (`KernelState.tools`). Reserved field only; v0.4
  introduces tool execution.
- **Trusted-extension framework for custom reducers**. v0.3 custom actions
  return built-in effects only (P14). Custom reducers are v0.4+ territory.
- **Workflow subsystem** (`KernelState.workflow`). Reserved field only;
  v0.5+.
- **Full causal_refs population for workflow/synthesis events**. PR 4
  populates `causal_refs` for known event types; others remain `()` until
  those event types are introduced.
- **Per-tool sandboxing**. Not a v0.3 concern; tracked against the tool
  subsystem above.
- **HA failover / replicated state**. v0.6+.
- **`BudgetScope.time_window`**. Field reserved; semantics v0.5+.
- **`CapabilityScope` / `CapabilityGrant.conditions`**. Fields reserved;
  semantics v0.4+.

---

## 9. Verification plan (end-to-end)

After all 13 PRs land, the following gates run before the v0.3 release-cut:

1. **Boundary tests**: `pytest tests/test_kernel_kernel_boundary.py` — all
   clock + lock + effect-registry-coverage gates.
2. **Property tests**: `pytest tests/property/` — extended for reducer
   idempotency, budget conservation, capability replay determinism.
3. **System tests**: `pytest tests/system/` — end-to-end with capabilities,
   budgets, control actions wired.
4. **Backward-compat**:
   - Load a v0.2.0 `events.jsonl` fixture; verify replay produces identical
     state in `KernelState.room`.
   - Load a v5 snapshot; verify migration to v6 succeeds without data loss.
5. **Capability stress**: grant 1000 capabilities, expire half; verify
   `effective_capabilities` correctness; verify replay reconstructs ledger.
6. **Budget stress**: 1000 lease acquisitions with varied
   reservation/commit/refund patterns; verify ledger conservation invariant.
7. **Lease kind matrix**: each of the 5 kinds × each of the 8 default checks
   produces the expected pass/fail per `applies_to`.
8. **Control action paths**: each of the 9 kernel-defined actions exercised
   through `propose → grant → apply`; each `DenialReason` exercised.
9. **Floor override matrix**: each (mode × scope) combination tested for
   correct `active_overrides` composition + pruning timing.
10. **Slash command parity**: every slash command produces the same
    `control_action_applied` envelope as the equivalent agent action (only
    `actor_id` differs).
11. **Off-lock policy stress**: high-concurrency policy invocations under
    induced epoch shifts; retry-exhaustion path produces `policy_aborted`.
12. **Cursor persistence**: actor restart recovers cursor from
    `KernelState.actors`; new events post-restart processed correctly.
13. **Public surface**: `make ux-check` — `KernelState` / `KernelStateView`
    additions are additive on the public API; no breakage in
    `loom.public`.

---

## 10. Related

- `11-orchestration-os-doctrine.md` — design contract being implemented.
- `12-v02-hardening-audit.md` — v0.2.1 gating audit; §10 readiness
  checklist (now satisfied) and §12 deferrals (closed by PRs 3, 4, 12, 13).
- `docs/timing-discipline.md` — v0.2.1 timing-discipline rule;
  load-bearing for P6.
- `CHANGELOG.md` — `[Unreleased]` section accumulates v0.3 PR entries;
  release-cut renames to `[v0.3]`.
