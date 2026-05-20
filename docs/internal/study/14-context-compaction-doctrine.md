# Loom Kernel — Context Compaction Doctrine (v0.3.x)

## Status

Locked 2026-05-16 after four rounds of design dialogue. This doctrine
extends the v0.3 design contract (`11-orchestration-os-doctrine.md`,
P1–P15) with seven additional principles (P16–P22) covering view-layer
context compaction. The v0.3.x implementation roadmap is in
`15-v03x-compaction-implementation-roadmap.md`.

## 1. Problem statement

Long-running rooms produce unbounded event logs. Each new agent turn
re-renders most of that log into the LLM prompt. For collaborative
scenarios (planner ↔ reviewer convergence loops, multi-turn workflow
deliberation, extended user-agent dialogs) the prompt grows past the
model's context window long before the conversation converges.

The v0.2.x prompt builder already supports manual compaction
(`RoomState.last_compacted_event_id` advances when a user types
`/summary` and the prompt builder renders a `summary` event in place of
the truncated tail). What v0.3.x adds:

1. **Auto-trigger** based on policy-observable context pressure.
2. **First-class typed events** (`summary_proposed`, `summary_committed`,
   `summary_failed`) so compaction is auditable and replayable.
3. **Lineage-preserving rolling compaction** so long-lived rooms can
   compact summaries themselves without losing audit trail.
4. **Thread membership** as a first-class scope, so private
   sub-conversations (collaborative plan loops, workflow steps) can be
   compacted independently of the main timeline.
5. **Capability and lease integration** so summarization fits the v0.3
   single-mutation-path doctrine instead of being a side channel.

## 2. The principles (P16–P22)

### P16 — MessageBus is canonical and never compacted

The bus is the ledger. It is append-only, replay-deterministic, and
complete. Compaction never deletes, rewrites, or merges bus events.
Any view that wants a compressed picture of history derives it from the
bus + projected state.

**Why:** v0.3 P5 (transactional KernelState) and the v0.2 replay
invariant ("old `events.jsonl` files load cleanly") both depend on the
bus being immutable. Compacting the bus would break replay, audit, and
v0.2.x backward compatibility.

**Implication:** "Compaction" in this doctrine is shorthand for
"compressing the prompt view," never "shrinking the log."

### P17 — Compaction is a view-layer concern

The prompt view rendered for each agent turn is a derived projection
of `(MessageBus, ContextState)`. `ContextState` is itself a typed
projection of `summary_*` events on the bus. The bus stays canonical;
`ContextState` is a replay cache.

**Why:** mirrors the v0.3 pattern where `CapabilityState` is derived
from `capability_*` events and `BudgetLedger` is derived from
`budget_*` events. Snapshots may persist `ContextState` for warm
restart, but replay from `events.jsonl` must be able to reconstruct it.

**Implication:** if `ContextState` is ever lost or corrupted, full
replay regenerates it. There is no "true" compaction state outside the
event log.

### P18 — Three typed compaction events

Compaction is mediated by three typed events:

- `summary_proposed` — a summarizer agent's draft, emitted under its
  SUMMARIZATION lease.
- `summary_committed` — the coordinator's durable acceptance after
  structural validation; updates `ContextState.active_summary_by_scope`.
- `summary_failed` — coordinator's rejection with a typed
  `SummaryFailureReason` and the proposed draft as payload.

**Why:** the three-event lifecycle mirrors v0.3 PR 9's
`control_action_proposed/applied/denied` shape. It separates "what the
summarizer drafted" from "what the coordinator committed" so a future
review step (a `summary_reviewed` event between propose and commit)
can be inserted without schema rework.

**Replay invariant:** replay uses recorded summaries; it never
regenerates them. LLM calls in the replay path would break determinism.

### P19 — Structural deterministic validation; off-lock pre-validation; under-lock commit

`summary_proposed` validation runs in two phases:

- **Off-lock pre-validation** (potentially long, but deterministic):
  parse payload, schema check, compute range metadata, verify causal
  refs reference real events, verify lineage invariants.
- **Under-lock commit validation** (cheap, fast): verify the anchor
  hasn't moved (no `ANCHOR_CONFLICT`), verify no conflicting summary
  committed in the same scope, emit `summary_committed`, advance
  `ContextState.active_summary_by_scope`.

**Forbidden:** any validation that requires another LLM call. The
coordinator cannot grade summary quality, accuracy, or completeness.
Semantic judgment is a downstream agent/policy concern (potentially a
future `summary_reviewed` step) and runs outside the coordinator.

**Why:** the under-lock commit path runs the same constraints as v0.3
PR 12's off-lock policy refactor: cheap, deterministic, replay-safe.
Heavy LLM-call validation would either block the coordinator
(violating P4) or be nondeterministic (violating replay invariant 5).

### P20 — Workflow state is not summary state

Summaries carry text + retained_event_ids + lineage (input summary
ids, input event ranges). They do **not** carry authoritative routing
state, plan versions, convergence flags, or "next expected speaker."

Authoritative coordination state lives in `KernelState.workflow.*`
(reserved v0.5 slot). Policy reads workflow state through
`KernelStateView`; summaries provide prose memory for agent prompts.

**Why:** if a summary tells the policy who speaks next, the summary
is doing policy's job — violating v0.3 P13 (pure policy with frozen
view). The summary can include non-authoritative semantic digest text
("earlier rounds established constraints A and B; objection C remained
unresolved") as long as the kernel never reads that text as routing
truth.

### P21 — Thread membership is a first-class scope

Every `Event` carries a `thread_id: str` field on its envelope.
Assignment rules:

- **Lease-produced events** inherit `thread_id` from
  `LeaseContext.thread_id`.
- **System events** (presence join/leave, lease lifecycle, policy
  errors, capability/budget/summary commits) are assigned `thread_id`
  by the coordinator at emit time.
- **Workflow-produced events** inherit from the workflow run's thread
  (v0.5+).
- **User events** default to `thread_id="main"` in v0.3.x. Explicit
  thread routing (`/in <thread> <message>`) is v0.5+.
- **Agents never set `thread_id` directly.** There is no API surface
  that exposes it; it is an attribute of the scope they were granted.

`thread_id`, `channel`, and `causal_refs` are three orthogonal concerns:

- `channel` = visibility scope (main / control / dm / scratch / …).
- `thread_id` = membership scope (which logical conversation).
- `causal_refs` = semantic dependency graph (typed v0.3 PR 4).

A thread is **not** derivable from causal traversal alone: two
subthreads can share the same root cause but belong to separate
branches; a workflow's publish-to-main event has cross-thread
causality but lives in main.

**Default in v0.3.x:** `thread_id="main"` for all existing flows. The
field is added now so prompt-view scoping has a sound foundation;
real thread creation lands with v0.5 workflows.

### P22 — Unified at the SUMMARIZATION lease

All summarization runs under a **SUMMARIZATION** `LeaseKind` (the 6th
kind, joining USER_TURN / CONTROL_ACTION / TOOL_INVOCATION /
WORKFLOW_STEP / REACTIVE from v0.3 PR 7). The lease requires:

1. The acquirer holds the **`EMIT_SUMMARY`** capability (added to the
   v0.3 capability enum).
2. The acquirer matches `RoomState.default_summarizer_id` for the
   target scope. Compaction is opt-in: an unassigned slot disables
   auto-compaction for that scope.

There are two trigger paths, both converging on the same lease:

- **Path A — policy-triggered (auto):** Policy hook
  `should_compact(view) → bool` returns true; coordinator acquires the
  SUMMARIZATION lease directly on behalf of `default_summarizer_id`;
  emits lightweight `summarization_scheduled` audit event. No
  `control_action_proposed` ceremony.
- **Path B — explicit (`/summarize` or agent-initiated):** A
  `SummarizeControlAction` flows through the standard v0.3 PR 9
  `propose → check → apply` lifecycle; the apply step acquires the
  SUMMARIZATION lease.

**Why:** the lease is the canonical "summarization is happening now"
mechanism. The proposal ceremony is optional based on trigger source,
but the lease + capability + slot checks are mandatory in both paths.
This preserves the v0.3 single-mutation-path doctrine without forcing
control-action ceremony on every auto-compaction.

## 3. Schemas

### 3.1 ContextScope

```python
@dataclass(frozen=True)
class ContextScope:
    room_id: str
    thread_id: str = "main"
    actor_id: str | None = None   # reserved v0.6+ for per-actor compaction
```

Hashable, JSON-serializable. Mirrors the `BudgetScope` pattern from
v0.3 PR 6. `actor_id` is reserved; do not populate in v0.3.x.

`ContextScope` describes *where the conversation lives*. It does **not**
describe visibility or channel filters; that is `PromptViewSpec`
territory and lives at the prompt-render layer.

### 3.2 SummaryRecord

```python
@dataclass(frozen=True)
class SummaryRecord:
    summary_id: str                     # ULID
    scope: ContextScope
    covers_event_range: tuple[int, int] # (start_event_id, end_event_id) inclusive
    text: str                           # canonical compaction prose
    retained_event_ids: tuple[int, ...] # events too important to summarize away
    input_summary_ids: tuple[str, ...]  # superseded summaries folded into this one
    input_event_ranges: tuple[tuple[int, int], ...]  # raw event ranges newly summarized
    created_by: str                     # participant_id of summarizer
    model_id: str                       # which LLM produced it
    prompt_hash: str                    # hash of the summarization prompt template used
    committed_at_event_id: int          # set by coordinator on commit
    causal_refs: tuple[CausalRef, ...]  # typed v0.3 PR 4 refs
    trace: TraceContext | None
    schema_version: int = 1
```

**Lineage invariants (checked at commit):**

- `covers_event_range == union(input_summary_ids.covers_event_range,
  input_event_ranges)`.
- All `input_event_ranges` belong to the same `ContextScope`.
- All `input_summary_ids` belong to the same `ContextScope`.
- Ranges are contiguous (no gaps), non-overlapping.
- `retained_event_ids ⊆ covers_event_range`.

Gaps are forbidden in v0.3.x. A future `skipped_event_ranges` field
(v0.5+) could allow explicit redaction.

### 3.3 ContextState

```python
@dataclass
class ContextState:
    summaries: dict[str, SummaryRecord]                  # summary_id → record
    active_summary_by_scope: dict[ContextScope, str]     # scope → summary_id
    supersession_edges: dict[str, str]                   # old_id → new_id
    failure_count: dict[tuple[str, ContextScope], int]   # (summarizer_id, scope) → n
    disabled_scopes: dict[ContextScope, str]             # scope → last_failed_summary_id

    schema_version: int = 1
```

All fields are derived from `summary_*` and `compaction_disabled`
events. Snapshots may persist `ContextState` for warm restart, but
full replay must reconstruct it deterministically.

`active_summary_by_scope` is the **explicit pointer** the prompt
builder reads. There is no "latest summary covering range X" implicit
selection — the coordinator decides which summary is active at commit
time and writes it to the pointer.

### 3.4 SummaryFailureReason

```python
class SummaryFailureReason(str, Enum):
    INVALID_RANGE                    # start > end or out of bounds
    MISSING_EVENTS                   # range references events not in log
    SCHEMA_ERROR                     # required field missing/malformed
    INVALID_CAUSAL_REF               # ref points at non-existent event
    INVALID_LINEAGE                  # covers != union(inputs) or overlap/gap
    ANCHOR_CONFLICT                  # anchor moved between propose and commit
    DROPPED_REQUIRED_RETAINED_EVENT  # only fires when v0.4 pinning adds requirements
    BUDGET_EXHAUSTED                 # summarizer ran out mid-call
    LEASE_EXPIRED                    # took longer than lease TTL
    KERNEL_ERROR                     # bug; coordinator detected internal inconsistency
```

Ten reasons. `DROPPED_REQUIRED_RETAINED_EVENT` is reserved for v0.4
pinning; never fires in v0.3.x.

## 4. Event taxonomy

### 4.1 `summary_proposed`

Plane: `CONTROL`. Emitted by summarizer agent under SUMMARIZATION lease.

Payload:
- `summary_id`, `scope`, `covers_event_range`, `proposed_text`,
  `retained_event_ids`, `input_summary_ids`, `input_event_ranges`,
  `model_id`, `prompt_hash`.
- `causal_refs` reference the trigger (either the
  `summarization_scheduled` audit event or the `control_action_applied`
  for explicit `/summarize`).
- `trace` inherits from the lease's span.

### 4.2 `summary_committed`

Plane: `CONTROL`. Emitted by coordinator after successful structural
validation. Updates `ContextState.active_summary_by_scope`.

Payload:
- All fields of the proposed summary plus `committed_at_event_id`,
  `supersedes_summary_ids`.
- `causal_refs` reference the `summary_proposed` event.

### 4.3 `summary_failed`

Plane: `CONTROL`. Emitted by coordinator when structural validation
fails. Carries proposed draft for audit.

Payload:
- `proposed_summary_id`, `scope`, `reason: SummaryFailureReason`,
  `details: str`, `failed_validator: str`, `proposed_text`,
  `summarizer_id`.
- `causal_refs` reference the `summary_proposed` event.

### 4.4 `summarization_scheduled`

Plane: `CONTROL`. Emitted by coordinator when policy-triggered
compaction acquires a SUMMARIZATION lease (Path A only). Lightweight
audit event; not emitted on Path B (the `control_action_applied`
serves the same purpose).

Payload:
- `scope`, `lease_id`, `summarizer_id`, `trigger_pressure_ratio`.

### 4.5 `compaction_disabled`

Plane: `CONTROL`. Emitted by coordinator when
`failure_count[(summarizer_id, scope)] ≥ max_consecutive_failures`.
Subsequent auto-compaction attempts for that scope are skipped until
the slot reassigns or operator clears.

Payload:
- `scope`, `summarizer_id`, `failure_count`, `last_failed_summary_id`,
  `reason`.

## 5. SUMMARIZATION lease

A new `LeaseKind.SUMMARIZATION` joins the five v0.3 kinds. Its
`LeaseContext` is:

```python
@dataclass(frozen=True)
class SummarizationContext:
    scope: ContextScope
    covers_event_range: tuple[int, int]
    triggered_by: str                # "policy" or "control_action"
    triggering_event_id: int
```

Lease checks that apply:

- `_ParticipantRegisteredCheck.applies_to ⊇ {SUMMARIZATION}`
- `_ParticipantActiveCheck.applies_to ⊇ {SUMMARIZATION}`
- `_CapabilityCheck.applies_to ⊇ {SUMMARIZATION}` — verifies acquirer
  holds `EMIT_SUMMARY` capability. Reuses the v0.3 PR 9 check, which
  reads `required_capability` from the lease context.
- A new `_SummarizerSlotCheck.applies_to = {SUMMARIZATION}` — verifies
  acquirer matches `RoomState.default_summarizer_id`.
- `_BudgetCheck.applies_to ⊇ {SUMMARIZATION}` — reserves estimated
  summarization cost.

Lease lifetime: same default TTL as other kinds (configurable per
v0.3.1). On expiry → `lease_closed(reason="expired")` and counts as a
`LEASE_EXPIRED` failure for backoff purposes.

## 6. Validation

### 6.1 Off-lock pre-validation

Runs in the coordinator-internal thread that receives
`summary_proposed`, before lock acquisition.

Checks:
- JSON schema (all required fields present, types correct).
- `covers_event_range` bounds (start ≤ end, both ≥ 0).
- All referenced event IDs exist in the bus (read-only scan; off-lock
  safe).
- `causal_refs` reference real events.
- Lineage invariants (range union, contiguity, scope match).
- `retained_event_ids ⊆ covers_event_range`.

If any check fails → emit `summary_failed` with the appropriate reason;
do not acquire lock.

### 6.2 Under-lock commit

Once pre-validation passes, acquire the coordinator lock briefly:

1. Re-read `ContextState.active_summary_by_scope[scope]`.
2. Verify `covers_event_range.start ==
   current_active_summary.covers_event_range.end + 1` (or `== 0` if
   no active summary).
3. Verify no conflicting `summary_committed` event has been posted
   since pre-validation began (compare `kernel_state.version`).
4. If conflict → emit `summary_failed(reason=ANCHOR_CONFLICT)`;
   summarizer will retry with fresh anchor.
5. If no conflict → apply `SummaryCommittedEffect` via the v0.3 effect
   registry, emit `summary_committed`, bump `kernel_state.version`,
   release lock.

### 6.3 Race conditions

The window between `summary_proposed` and `summary_committed` is
typically microseconds, but in heavy concurrency two summarizers
could race. The `ANCHOR_CONFLICT` path handles this: the loser of the
race fails with that reason; the winning summary commits cleanly.

Backoff (§7.3 below) does not increment for `ANCHOR_CONFLICT`
failures because they are not summarizer-quality issues.

## 7. Triggers, backoff, and disablement

### 7.1 Policy-triggered (Path A)

Each pre-turn pass, coordinator computes:

```
pressure = ContextManager.estimate_context_pressure(
    participant_id, scope, kernel_state.version, prompt_template_hash
)
```

Cache key: `(participant_id, scope, kernel_state.version,
prompt_template_hash)`. The `kernel_state.version` bump on every
applied effect (v0.3 PR 1 invariant) provides automatic cache
invalidation.

If `pressure.needs_compaction` and `scope` not in
`ContextState.disabled_scopes` and `default_summarizer_id` is set:

1. Policy returns `pre_turn_action = ScheduleSummarization(scope)`.
2. Coordinator acquires SUMMARIZATION lease for `default_summarizer_id`.
3. On grant → emit `summarization_scheduled`.
4. Summarizer agent's turn: produces summary, emits `summary_proposed`.
5. Coordinator validates and commits (or fails).

### 7.2 Control-action-triggered (Path B)

Triggered by:
- User: `/summarize [scope]` slash command (Path B mandatory because
  user-initiated actions must route through control action per v0.3
  P15).
- Agent: explicit `SummarizeControlAction.propose(...)` if the agent
  holds `SUMMARIZE` capability (separate from `EMIT_SUMMARY`).

Standard v0.3 PR 9 lifecycle: `control_action_proposed` →
`_CapabilityCheck` (for `SUMMARIZE`) → acquire CONTROL_ACTION lease →
apply effect, which itself acquires SUMMARIZATION lease for the
designated summarizer → summarizer runs → `summary_proposed` →
commit/fail.

Two capabilities exist:
- `SUMMARIZE` — authority to *request* compaction (held by some agents
  and by user).
- `EMIT_SUMMARY` — authority to *produce* a summary (held by the
  designated summarizer only).

### 7.3 Backoff and disablement

State: `ContextState.failure_count[(summarizer_id, scope)] → int`.

Rules:
- On `summary_failed(reason ≠ ANCHOR_CONFLICT)` → increment count for
  `(summarizer_id, scope)`.
- On `summary_committed` → reset count for `(summarizer_id, scope)` to 0.
- On `DefaultSummarizerSetEffect` → reset count for all `(*, scope)`
  whose summarizer matches the old slot value.
- When count ≥ `RoomConfig.summarizer_max_consecutive_failures`
  (default 3) → emit `compaction_disabled(scope, summarizer_id,
  count, last_failed_summary_id)`; add `scope` to
  `ContextState.disabled_scopes`.

While `scope` is in `disabled_scopes`:
- Policy's `should_compact(view)` returns false for that scope.
- Path B (explicit) continues to work — operator-initiated compaction
  bypasses backoff.
- `DefaultSummarizerSetEffect` (slot reassignment) clears the scope
  from `disabled_scopes` and resets counts.

## 8. Replay invariants

1. Replaying `summary_*` events deterministically reconstructs
   `ContextState`.
2. Replay never makes an LLM call. `summary_committed` events carry
   the canonical text; replay uses them as-is.
3. Snapshot v6 (from v0.3 PR 1) is extended to v7: `KernelState`
   serializer includes `context: ContextState` block. v6 snapshots
   migrate to v7 with empty `ContextState`.
4. Old `events.jsonl` files without `thread_id` load with default
   `thread_id="main"` on every event.
5. Old `events.jsonl` files without `summary_*` events replay cleanly
   producing empty `ContextState`.

## 9. Cross-cutting concerns

### 9.1 Thread_id propagation

After v0.3.x PR 1, every event has `thread_id`. The propagation rules
in P21 are enforced at emit sites:

- `coordinator._emit_under_lease(lease, event)` populates
  `event.thread_id = lease.context.thread_id` if event has no explicit
  thread_id set.
- `coordinator._emit_system(event, scope)` populates
  `event.thread_id = scope.thread_id`.
- `EventBus.post()` rejects events with `thread_id is None` to enforce
  the invariant.

### 9.2 Lock discipline

The `_assert_not_holding_lock` discipline from v0.3 PR 2 extends to
the summary path:

- `ContextManager.estimate_context_pressure(...)` must not hold the
  coordinator lock (potentially scans many events).
- `summarizer.produce_summary(...)` is an LLM call — must not hold
  the lock (already enforced by being inside an agent's
  SUMMARIZATION lease).
- Off-lock pre-validation must not hold the lock.
- Only the under-lock commit step (~5 cheap field comparisons) holds
  the lock.

### 9.3 Capability and slot requirements

For auto-compaction (Path A) to fire, all three must be true:

1. `RoomState.default_summarizer_id is not None`.
2. The designated agent holds `EMIT_SUMMARY` capability.
3. `scope not in ContextState.disabled_scopes`.

If (1) is absent, auto-compaction silently never fires. If (2) is
absent, lease acquisition fails with `_CapabilityCheck`; backoff
counts apply.

For explicit compaction (Path B):

1. Proposer holds `SUMMARIZE` capability (the *request* verb).
2. Designated summarizer holds `EMIT_SUMMARY`.
3. Designated summarizer matches the slot (if Path B is configured to
   require the slot match; default yes).

## 10. PromptBuilder integration

After v0.3.x lands, `build_prompt(...)` reads:

```
1. KernelStateView (current state, frozen).
2. ContextState.active_summary_by_scope[ContextScope(room, thread)]
3. The SummaryRecord pointed to (if any).
4. Bus events in the scope where event_id > summary.covers_event_range.end
   (the "tail").
5. Retained events from summary.retained_event_ids (rendered alongside
   the summary block).
```

Rendering order:
1. Kernel charter + protocol instructions.
2. Persona.
3. Active KernelStateView projections (capabilities, budget, roles, …).
4. Latest committed summary text (rendered as `<<<PRIOR SUMMARY>>>` —
   replaces the existing `<<<PRIOR ROOM SUMMARY>>>` block).
5. Retained events (rendered as ordinary chat lines with a
   `[retained]` marker).
6. Tail events since the summary anchor.
7. Current turn card.

If `active_summary_by_scope[scope]` is absent: render all events in
scope (no summary block). This is the legacy v0.2.x behavior.

## 11. Out of scope (deferred to v0.4+)

- **Pinning** (`PIN_EVENT` capability, `pin_event`/`unpin_event`
  effects, `ContextState.pinned_events`). Authority story not
  designed; defer.
- **Per-actor summaries** (`ContextScope.actor_id != None`). Field
  reserved; not used.
- **Summary review step** (`summary_reviewed` event between propose
  and commit). Schema supports it; not implemented.
- **Summary redaction** for compliance / right-to-erasure. Hard
  problem; defer.
- **Selectable summary input filters** (currently hard-coded to "chat
  events only"). Add filter spec in v0.4+ when there's a concrete
  use case.
- **Cross-thread summary propagation** (publish thread summary to
  main). v0.5+ workflow concern.
- **Workflow-backed CollaborativePlanWorkflow** with planner/reviewer
  alternation and convergence detection. v0.5+ (depends on workflow
  subsystem).
- **Summarizer quality review / quorum-based commit**. v0.4+.

## 12. Open questions (resolve before v0.4)

1. **Path B slot requirement strictness.** Currently spec says
   summarizer must match `default_summarizer_id` even for
   user-initiated `/summarize`. Should `/summarize <agent>` allow
   designating an alternate summarizer for a one-off compaction?
   Lean: yes, with `EMIT_SUMMARY` still required.
2. **Re-summarization of the same range.** Spec allows it via
   `supersession_edges`. Should `summary_committed` reject if the
   new summary covers the *exact same range* as the active one
   (no progress)? Lean: yes, fail with new reason
   `NO_FORWARD_PROGRESS`.
3. **Rolling compaction depth limit.** Should there be a maximum
   compaction depth (`summary_of_summary_of_summary_of_…`) before
   forcing a full re-summarization of raw events? Lean: yes,
   configurable; default depth ≤ 5.
4. **`PromptViewSpec` formalization.** v0.3.x hard-codes "chat
   events only" for summarizer input and "chat + control" for prompt
   rendering. v0.4 needs a proper spec.

## 13. Relation to v0.3 doctrine (P1–P15)

This doctrine extends, never overrides:

- P1 (atomic capability verbs) — adds `EMIT_SUMMARY`, `SUMMARIZE`.
- P2 (three event planes) — all new events live in `CONTROL`.
- P4 (no I/O under lock) — preserved; LLM call happens in the
  summarizer's lease, off-lock; off-lock pre-validation;
  micro-commit under lock.
- P5 (transactional KernelState) — `KernelState.context: ContextState`
  becomes the 8th subsystem slot (joining room, capabilities, budget,
  actors, plus reserved workflow, tools).
- P6 / P7 (event-sourced replay, versioned effects) — new effects
  (`SummaryCommittedEffect`, `CompactionDisabledEffect`) registered
  with schema_version=1.
- P8 (unified Lease) — adds 6th `LeaseKind.SUMMARIZATION`.
- P9 (three-way budget) — SUMMARIZATION leases reserve budget
  normally.
- P11 / P12 (typed causal_refs, trace) — summary events carry both.
- P13 (pure policy) — preserved; policy reads `ContextState` and
  pressure stats but never mutates either.
- P14 (custom actions return built-in effects only) — preserved;
  `SummarizeControlAction` is a built-in action.
- P15 (human root actions) — `/summarize` slash command routes through
  Path B.

## 14. Verification (locked at PR completion)

The v0.3.x compaction implementation is complete when:

- All 7 principles structurally satisfied.
- `KernelState.context: ContextState` slot wired.
- `summary_proposed` / `summary_committed` / `summary_failed` /
  `summarization_scheduled` / `compaction_disabled` events round-trip
  through journal.
- Path A (policy-triggered) and Path B (explicit) both produce
  identical `summary_committed` payloads given the same input.
- Replay determinism boundary: snapshot a populated `ContextState`,
  truncate, replay from v6 events — get identical `ContextState`.
- `LockDisciplineBoundary` test extended for compaction code paths.
- `thread_id` propagation invariant enforced at every emit site
  (boundary test).
- v0.2.x and v0.3.x journals load cleanly with empty `ContextState`.
- Backoff: 3 consecutive failures disable scope; reassignment clears.

---

## Provenance

This doctrine was derived from a four-round design dialogue
(2026-05-16) between the implementing engineer (Claude) and an
external reviewer (GPT). The dialogue is preserved in conversation
memory; key resolutions:

- Round 1: framing — "is policy still needed? what about multiple
  buses?" Established: yes policy; one bus per room; compaction not
  via bus changes.
- Round 2: GPT proposed ContextManager subsystem; pushback on
  stateful policy; agreement on three-event lifecycle.
- Round 3: convergence on validation discipline, lineage preservation,
  thread_id orthogonality to channel/causal_refs.
- Round 4: final tightening — explicit `active_summary_by_scope`
  pointer (not implicit latest-summary), `thread_id` on Event
  envelope (not just LeaseContext), per-scope backoff, dual capability
  model (`SUMMARIZE` for request authority vs `EMIT_SUMMARY` for
  production authority).
