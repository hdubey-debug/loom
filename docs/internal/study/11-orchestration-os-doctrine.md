# Loom Orchestration OS — v0.3 Design Doctrine

## Preface

This document is the design contract that the v0.3 implementation will be
written against. It does not describe code that exists today; it
describes the architectural shape Loom is being grown into as it
evolves from a single-room conversation kernel (v0.2) into a
general-purpose agent orchestration OS.

The doctrine emerged from four rounds of architectural review conducted
2026-05-15 through 2026-05-16, prompted by the strategic question
*"how do we turn Loom into an Agent Orchestration OS, not just a
conversation kernel?"* The conversation surveyed the agent OS
landscape (AIOS, Bedrock AgentCore, Microsoft Agent Framework, LangGraph,
Google ADK, AutoGen, OpenAI Agents SDK), examined CEO/topology
trade-offs, and converged on the principle that the **kernel must be a
substrate that expresses any orchestration topology** rather than a
framework that bakes one in.

This document supersedes any earlier informal recommendations about
admin LLMs, agent tools, or workflow primitives. Where the curriculum
artifacts (`00-orientation.md` through `10-synthesis.md`) describe what
Loom v0.1.2/v0.2 IS, this document describes what v0.3 WILL BE.

**How to use this document.** Read Part I (the 15 principles) first;
those are the load-bearing claims that everything else follows from.
Part II elaborates each subsystem. Part III lists exactly what v0.3
ships and what it reserves in schema for later. Part IV catalogs
deferred work so it does not get lost. The glossary defines terms;
the provenance appendix records where each decision was made and why.

Before any v0.3 PR is opened, the PR description must cite which
principle(s) it implements or which subsystem specification it
satisfies. Deviations from this doctrine force an explicit doctrine
revision PR — not a silent design drift.

---

## Part I — The Fifteen Principles

These principles are normative. They constrain what the v0.3 kernel
may and may not do. They are numbered for citation in PR descriptions
and review comments (e.g. *"this violates P4 — LLM call inside
coordinator lock"*).

### P1. Kernel knows no CEO

The kernel has no concept of "admin", "supervisor", "manager", "CEO",
"chair", or any other privileged-identity role. These are *templates*
or *capability bundles* assembled in policy or runtime configuration,
never primitives the kernel itself recognizes. A room with zero
admins, one admin, two co-admins, or a tiered admin/auditor pair is
expressible as a different capability assignment over the same kernel
primitives.

**Why it matters.** Baking a CEO into the kernel would force every
room to adopt one orchestration topology. Loom's v0.2 single-room
conversation kernel is already used in modes with no CEO (open chat),
implicit CEO (single-responder), and rotating chair (round-robin);
v0.3 must extend that range of expressible topologies, not collapse it.

### P2. Three event planes

The kernel emits events on three planes with distinct semantics:

| Plane | Examples | Source of side effects | Replay rule |
|---|---|---|---|
| **Conversation** | `chat`, `stream_*`, `user_turn_opened`, `user_turn_closed` | None outside Loom; pure content exchange | Replay reads recorded text |
| **Control** | `control_action_proposed`, `control_action_applied`, `control_action_denied`, `capability_granted`, `lease_*` | RoomState/KernelState mutations *inside* Loom | Replay applies recorded effects deterministically |
| **Execution** | `tool_call_proposed`, `tool_result`, sandbox lifecycle events | External world (filesystem, APIs, network) | Replay reads recorded results; never re-executes |

The planes share a single bus and a single journal. The distinction is
in event taxonomy and replay treatment, not in transport.

### P3. No scheduler bypass

Speech (any `chat` event) requires a successful `UserTurnLease`
acquisition mediated by the `LeaseCheck` chain. This holds for every
participant, including those with admin capabilities. A participant
with elevated authority does not speak out of turn; instead, they
invoke a control action that mutates scheduling state (e.g.
`grant_floor`, `override_allowed_speakers`), and *then* acquire a
normal lease against the new scheduling state.

**Why it matters.** `allowed_speakers` is the scheduler's authoritative
boundary, not etiquette. A bypass mechanism would surrender the
single-source-of-truth property that makes the journal auditable and
replay deterministic.

### P4. No long-running operation under coordinator lock

The coordinator's lock guards only cheap operations:

- Lease registration and termination
- State validation (capability checks, schema validation, invariant checks)
- Effect application (calling reducers; emitting journaled events)
- Budget reservation/commit/refund (in-memory ledger updates)

It must **never** be held during:

- LLM inference (any model call)
- External tool invocation
- File or network I/O
- Sandboxed code execution
- Any operation that can block for more than ~1ms

Long-running operations happen in off-lock phases of the lease
lifecycle. Lock-held methods are named `_apply_*`, `_validate_*`,
`_reserve_*`, `_commit_*` for grep-ability and reviewability. Every
I/O entry point in the kernel calls
`coordinator._assert_not_holding_lock()` defensively; tests use
mock LLMs and tools that always invoke this assertion.

### P5. Unified KernelState transaction boundary

All kernel state lives under one transactional root, `KernelState`,
mutated exclusively by the coordinator under its lock and snapshotted
atomically. Subsystems (`RoomState`, `CapabilityState`, `BudgetLedger`,
and later `WorkflowState`, `ToolState`) are organizationally separate
for code clarity but share the transaction boundary.

```python
@dataclass
class KernelState:
    room: RoomState
    capabilities: CapabilityState
    budget: BudgetLedger
    workflow: WorkflowState              # v0.5+
    tools: ToolState                     # v0.4+
    version: int                         # increments on every applied event
    schema_version: int                  # bumps on field additions
```

`KernelState.version` increments on every applied event; it is the
optimistic-concurrency token used to detect stale proposals (see §8).

### P6. Event-sourced replay applies committed effects

The journal records what happened, not what reasoning produced it.
Replay applies committed effects in journal order; it never re-runs
policy decisions, control validation, capability checks, veto
decisions, tool calls, or expiry logic. This is the only model that
is invariant to code and policy changes between the original run and
the replay.

| Event class | Replay treatment |
|---|---|
| `lease_granted` | Apply (lease state + budget reservation) |
| `control_action_proposed` | Skip (audit-only; no durable effect) |
| `control_action_applied` | Apply (effect reducer + budget commit + lease termination) |
| `control_action_denied` | Apply (budget refund + lease termination + denial recorded for audit) |
| `lease_closed(reason)` | Apply (lease termination + budget commit/refund per reason) |
| `capability_granted/revoked/expired` | Apply (capability ledger mutation) |
| `tool_call_proposed` | Skip (audit-only) |
| `tool_result` | Apply (result is the durable data; never re-execute the tool) |

### P7. Applied events record semantic, versioned effects

The data carried by `*_applied` events is a typed `ControlEffect`, not
a path-keyed `state_delta` dictionary. Each effect type has a
`schema_version`; replay dispatches to a versioned reducer registry.

```python
@dataclass(frozen=True)
class FloorOverrideEffect:
    schema_version: int = 1
    turn_id: int
    speakers: frozenset[str]
    mode: OverrideMode           # ADD | REPLACE | BLOCK
    scope: ControlEffectScope    # ONE_LEASE | CURRENT_TURN | UNTIL_CLEARED | PERSISTENT_ROOM_CONFIG
```

Old journals replay correctly under new code because the v1 reducer is
preserved even after v2 lands. Internal object layout is free to
change; the effect schema is the stable contract.

`*_applied` events additionally carry `before_state_hash` and
`after_state_hash` for replay drift detection. These are advisory,
not load-bearing, but invaluable for catching reducer bugs or journal
corruption.

### P8. Leases are generalized but typed

One `Lease` abstraction with a discriminator field unifies the
mechanisms for chat permission, control proposal, tool invocation, and
(in v0.5+) workflow step assignment. The shared invariants — TTL,
holder, trigger_event_id, budget reservation, audit trail — are
abstracted; per-kind contexts are typed:

```python
LeaseKind = Literal["user_turn", "control_action", "tool_invocation",
                    "workflow_step", "reactive"]

LeaseContext = Union[UserTurnContext, ControlActionContext,
                     ToolInvocationContext, WorkflowStepContext,
                     ReactiveContext]

@dataclass(frozen=True)
class Lease:
    id: int
    holder: str
    kind: LeaseKind
    trigger_event_id: int
    base_state_version: int      # KernelState.version at acquisition
    ttl_s: float
    budget_scope: BudgetScope
    context: LeaseContext
```

`LeaseCheck` instances declare their applicability:
`AllowedSpeakerCheck.applies_to = frozenset({"user_turn"})`;
`BudgetCheck.applies_to = frozenset({"*"})`. The coordinator filters
the chain by applicability before iterating; no `if kind == ...`
branches inside individual checks.

### P9. Every lease reserves budget

Every lease, regardless of kind, reserves estimated cost at acquisition
time. The reservation is committed at completion or refunded at
cancellation. Three-way accounting for denial:

| Denial point | Model cost | Effect cost | Refund |
|---|---|---|---|
| Lease denied at acquisition (cheap checks fail) | 0 | 0 | Full reservation |
| Proposal denied post-LLM (validation fails, schema invalid, state mismatch) | Spent | 0 | Commit actual model cost; refund unused reservation only |
| Proposal applied | Spent | Spent | Commit both; refund unused reservation |

Without this discipline, a prompt-injected admin can generate infinite
invalid control proposals at zero cost.

### P10. Capabilities are least-privilege and lifecycle-managed

The kernel enforces atomic verb capabilities, not roles. Capability
names are verbs corresponding to specific control actions:
`grant_floor`, `cancel_lease`, `set_topic`, `set_anchor`,
`set_default_responder`, `update_allowed_speakers`,
`grant_capability:<X>`, `revoke_capability:<X>`, `switch_policy`,
`invoke_workflow`, `register_tool`. Templates bundle these into named
roles (`AdminRole`, `AuditorRole`, `ToolMasterRole`) for ergonomic
bootstrap; the kernel never sees the bundle name, only the underlying
capabilities.

`CapabilityState` is the single source of truth. Each grant carries
full lifecycle metadata (grantor, scope, conditions, expires_at,
revoked_at, source_event_id). `ParticipantInfoView.capabilities` is a
derived convenience property; it does not store the canonical list.

### P11. Causality is a graph

Events carry `causal_refs: tuple[CausalRef, ...]` — a typed tuple of
references to other events, each tagged with a relation type. A
single integer parent is insufficient for events with multiple causes
(workflow joins, synthesis events, dead-letter rerouting).

```python
@dataclass(frozen=True)
class EventRef:
    room_id: str           # forward-compat for multi-room (v0.7)
    event_id: int          # position in room's log
    event_type: str        # resilience against journal compaction

@dataclass(frozen=True)
class CausalRef:
    ref: EventRef
    relation: CausalRelation  # enum: responds_to | tool_result_for |
                              # control_action_applied | joined_from |
                              # triggered_by | replay_of | ...
```

Population in v0.3 is optional for most event types; empty tuple is
the default. Mandatory population for specific event classes comes
when the kernel uses causality (replay invariants, audit queries,
debugging tools).

### P12. Trace metadata starts in v0.3

Every event carries minimal trace context:

```python
@dataclass(frozen=True)
class TraceContext:
    trace_id: str              # room-session-scoped
    span_id: str               # per-lease or per-action
    parent_span_id: Optional[str] = None
```

The kernel does not consume this data in v0.3 (no dashboards); it is
populated for v0.4+ tooling. `causal_refs` answers *why* an event
exists (semantic parentage); `trace/span` answers *what execution
path* produced it. The two are orthogonal and both useful.

Lease creation begins a span; lease termination ends it. Events
emitted during a lease carry the lease's `span_id`. Tool calls within
a lease become child spans.

### P13. Policy stays pure

Policy receives a frozen `KernelStateView` and returns a `UserTurnPlan`
or a fragment thereof. Policy does not mutate state, post bus events,
hold mutable internal state across turns, or receive reactive
callbacks. Control plane mutations propagate to policy through state
visible on its next invocation; if policy disagrees with a control
action's effect, it re-narrows the relevant fields in its next plan.

This invariant predates v0.3 (it is one of v0.2's load-bearing boundary
properties) and is preserved unchanged. The v0.3 additions (capability
state, workflow state, tool state) all flow into `KernelStateView` and
are read-only from policy's perspective.

### P14. Custom control actions return typed effects

Room configuration may register custom `ControlAction` classes whose
`propose_effect(params, state_view)` returns one or more *built-in*
effect types. Custom actions cannot introduce new effect types or
reducers — those are kernel-extension territory (v0.4+ trusted
extensions, installed at process boot, audited against invariants).

This boundary preserves the single-mutator property: only
coordinator-invoked reducers from the kernel-defined registry can
mutate `KernelState`. Custom actions are useful compositions
(multi-effect actions, parameter-derived effects), not state-mutation
escape hatches.

### P15. Human root actions use the same control-action path

When a human operator invokes a slash command (e.g. `/topic foo`,
`/grant alice admin`), the runtime emits the same `control_action_*`
events as agent-initiated actions, with `actor_id="user"`. The user
bypasses agent capability checks (the user is root) but does not
bypass the journal, the effect reducer, or replay. There is one
canonical control-action path; the source distinguishes only at the
authorization step.

---

## Part II — Subsystem Specifications

### §1. KernelState architecture

`KernelState` is the transactional root containing all kernel state.
The coordinator's lock guards mutations; snapshots include all fields
atomically; the journal sequences events that the reducers apply to
this state.

**Composition (v0.3):**

```python
@dataclass
class KernelState:
    room: RoomState                  # conversation world model (v0.2 contents)
    capabilities: CapabilityState    # NEW v0.3
    budget: BudgetLedger             # NEW v0.3
    # placeholders reserved for future stages:
    workflow: Optional[WorkflowState] = None    # v0.5
    tools: Optional[ToolState] = None           # v0.4
    version: int = 0                  # increments on every applied event
    schema_version: int = 6           # snapshot schema version

    def view(self) -> KernelStateView:
        """Return a frozen view suitable for policy and adapter inspection."""
        ...

    def snapshot(self) -> Snapshot:
        """Atomic snapshot of all subsystems plus version+schema_version."""
        ...
```

**Migration from v0.2.** Today's `RoomState` becomes `KernelState.room`.
A thin compatibility shim in `loom/runtime.py` aliases legacy
references during the transition; all kernel-internal call sites
migrate during the v0.3 PR sequence. Snapshot schema bumps v5 → v6
with `capabilities`, `budget`, `version`, and the reserved fields
defaulting to empty/None. v0.2 snapshots load with v6 defaults
populated.

**Critical invariants.**

- The coordinator is the only mutator of `KernelState` (extends v0.2's
  single-mutator invariant from `RoomState` to the larger root).
- Snapshots are atomic across all subsystems; readers either see the
  full pre-event state or the full post-event state, never a partial
  mutation.
- `KernelState.version` increments exactly once per applied event;
  monotonically increasing within a single room session.

### §2. Lock discipline

The coordinator's lock is a single `threading.Lock` (or equivalent)
guarding `KernelState` and the bus's authoritative slots. The
discipline is **structural**, not stack-introspecting.

**Rules.**

1. Methods that hold the lock are named with a leading underscore and
   one of the prefixes `_apply_`, `_validate_`, `_reserve_`,
   `_commit_`, `_refund_`. Code review enforces the naming.
2. No I/O entry point may execute while the lock is held. Each I/O
   entry point begins with:
   ```python
   self._coordinator._assert_not_holding_lock()
   ```
   I/O entry points include: LLM adapter `stream()` and
   `stream_with_tools()`; tool registry `invoke()`; file/network
   adapters in the runtime; any sandbox dispatch.
3. Lock-released subscriber fan-out (v0.2 PR 11) is preserved and
   extended: any new event class added in v0.3 is fanned out outside
   the bus lock.
4. Lock acquisition is non-reentrant. Re-acquisition is a bug; the
   assertion will surface it in tests.

**Enforcement layers.**

- *Runtime assertion*: `_assert_not_holding_lock` at every I/O entry.
- *Naming convention*: lock-held methods are reviewable by grep.
- *Test injection*: mock LLMs and tools in the test suite always call
  the assertion. A regression in locking discipline causes test
  failures immediately, not in production.
- *Lint rule (advisory)*: a CI check flags obvious patterns
  (`open()`, `requests.`, `subprocess.`, etc.) inside `_apply_*`
  methods. The lint is a tripwire; the runtime assertion is the
  authoritative check.

### §3. Lease abstraction

One `Lease` class unifies the five lease kinds. The discriminator is
the `kind` field; per-kind context is typed via tagged union.

**Lease lifecycle (all kinds).**

```
1. Actor decides cheaply: classifier returns DRAFT_CHAT,
   PROPOSE_CONTROL, INVOKE_TOOL, or SKIP.
2. Actor requests lease of the corresponding kind.
3. Coordinator (under lock):
   - Flushes due capability expirations (§6).
   - Iterates applicable LeaseChecks (filtered by lease kind).
   - If all pass: reserves budget, registers lease, records
     base_state_version, emits lease_granted, releases lock.
   - If any fails: emits lease_closed(reason="denied:<check_name>"),
     releases lock. (Equivalent semantics to v0.2 lease_denied.)
4. (Lock released) Actor performs off-lock work:
   - DRAFT_CHAT: invokes LLM adapter, streams output, runs PASS
     and idle-dup detection, calls policy.should_post_response.
   - PROPOSE_CONTROL: invokes LLM adapter, parses control action
     proposal.
   - INVOKE_TOOL: invokes tool registry, awaits result.
5. Coordinator (under lock):
   - Re-validates: KernelState.version comparison; capability still
     held; target still exists.
   - If valid: applies effect (control), commits chat (stream_end
     committed path), records tool result (execution), commits
     actual budget, emits *_applied or chat event, emits
     lease_closed(reason="released"), releases lock.
   - If invalid: emits *_denied event with reason, commits actual
     model cost, refunds unused reservation, emits
     lease_closed(reason="aborted_validation"), releases lock.
```

**LeaseCheck applicability.**

```python
class LeaseCheck(Protocol):
    name: str
    applies_to: frozenset[str]    # lease kinds, or frozenset({"*"})

    def check(self, lease: Lease, state: KernelStateView,
              config: RoomConfig) -> LeaseCheckResult: ...
```

The default chain (v0.3):

| Check | Applies to | Purpose |
|---|---|---|
| `OpenTurnCheck` | `user_turn` | Reject if no open UserTurn |
| `ParticipantRegisteredCheck` | `*` | Reject if holder not in participants |
| `ParticipantActiveCheck` | `*` | Reject if holder inactive |
| `AllowedSpeakerCheck` | `user_turn` | Reject if holder not in effective allowed_speakers (post-override; see §10) |
| `PerParticipantCapCheck` | `user_turn` | Reject if speaker hit max_drafts_per_participant |
| `MaxResponsesCheck` | `user_turn` | Reject if turn cap reached |
| `ThrottleCheck` | `*` | Per-kind rate limit |
| `BudgetCheck` | `*` | Reject if budget reservation would exceed scope |
| `CapabilityCheck` | `control_action` | Reject if holder lacks required capability for proposed action |
| `ControlRateLimitCheck` | `control_action` | Bounded control proposals per participant per minute |
| `ToolPermissionCheck` | `tool_invocation` | Reject if holder cannot use this tool |

User-extensible via `RoomConfig.lease_checks` (v0.2 PR 7) with the
applicability discipline enforced.

### §4. Event taxonomy and replay rules

The v0.3 event taxonomy extends v0.2's with the following additions.
The kernel-internal canonical event constructors live in
`loom/kernel/events.py`.

**New event types in v0.3.**

| Event | Plane | Replay |
|---|---|---|
| `control_action_proposed` | Control | Skip (audit-only) |
| `control_action_applied` | Control | Apply (effect reducer + budget commit + lease termination) |
| `control_action_denied` | Control | Apply (budget refund + lease termination + denial reason recorded) |
| `capability_granted` | Control | Apply (insert into CapabilityState) |
| `capability_revoked` | Control | Apply (set revoked_at on grant) |
| `capability_expired` | Control | Apply (set revoked_at; emitted by watchdog at runtime, by reducer on replay) |
| `lease_closed` | Conversation/Control/Execution (depending on lease kind) | Apply (lease termination; budget commit/refund per reason) |

`lease_closed` unifies v0.2's `lease_denied` and a new family of
terminations (`released`, `expired`, `cancelled`, `aborted`,
`aborted_validation`, `denied:<check>`) under one event type with a
`reason` enum. v0.2's `lease_denied` is retired in favor of
`lease_closed(reason="denied:<check_name>")`.

**Event body schemas.** All new events use frozen dataclasses for
their bodies. Constructors include the standard fields:
`causal_refs: tuple[CausalRef, ...] = ()`,
`trace_context: TraceContext`,
`target_room_id: Optional[str] = None` (reserved for v0.7).

**Replay invariants (extending v0.2).**

- `ev.id == position` (preserved).
- The journal is the canonical source of truth; the snapshot is an
  advisory fast-resume cache.
- Replay processes events in journal order, calling the appropriate
  reducer for each `Apply`-class event; skipping each `Skip`-class
  event.
- After a full replay, `KernelState.version` equals the number of
  applied events. Mismatch indicates journal corruption.

### §5. Effect vocabulary and reducer registry

Effects are typed, versioned dataclasses. Each effect has a
registered reducer that mutates `KernelState`. The registry maps
`(effect_type_name, schema_version) → reducer`.

**Kernel-defined effects in v0.3.**

| Effect | Mutates | Notes |
|---|---|---|
| `FloorOverrideEffect` | `RoomState.control` active_overrides | mode ∈ {ADD, REPLACE, BLOCK}; scope per §10 |
| `TopicChangedEffect` | `RoomState.topic` | persistent room config |
| `AnchorAssignedEffect` | `RoomState.slots.anchor` | persistent room config |
| `DefaultResponderSetEffect` | `RoomState.default_responder_id` | persistent room config |
| `RolesAssignedEffect` | `RoomState.slots.roles` | persistent room config |
| `LeaseCancelledEffect` | Lease registry | Terminates a specific lease |
| `CapabilityGrantedEffect` | `CapabilityState.grants` | Inserts new grant |
| `CapabilityRevokedEffect` | `CapabilityState.grants` | Sets revoked_at on grant_id |
| `CapabilityExpiredEffect` | `CapabilityState.grants` | Sets revoked_at on grant_id |
| `PolicySwitchedEffect` | `RoomState.policy_ref` | v0.3 reserves the field; v0.4+ uses it |
| `BudgetReservedEffect` | `BudgetLedger.reservations` | Bookkeeping; emitted by lease_granted |
| `BudgetCommittedEffect` | `BudgetLedger.commits` | Bookkeeping; emitted by lease_closed(released) |
| `BudgetRefundedEffect` | `BudgetLedger.refunds` | Bookkeeping; emitted by lease_closed(denied/aborted/cancelled) |

Budget-related effects are typically *implicit* — emitted as part of
`lease_*` event handlers, not as standalone control actions. The
table lists them as discrete reducer targets because replay applies
them through the same registry.

**Registry contract.**

```python
EffectReducer = Callable[[KernelState, ControlEffect], None]

class EffectRegistry:
    def register(self, effect_type: type[ControlEffect],
                 reducer: EffectReducer) -> None: ...
    def apply(self, state: KernelState, effect: ControlEffect) -> None:
        """Dispatch to registered reducer; raise UnknownEffectError if missing."""
        ...
```

**Versioning.** Each effect type carries `schema_version`. New
versions register additional reducers without removing the old ones:

```python
registry.register(FloorOverrideEffect, v1=apply_floor_override_v1)
registry.register(FloorOverrideEffect, v2=apply_floor_override_v2)  # v0.4 addition
```

Old journals replay through v1; new effects use v2. Migration is
additive; no in-place edits to historical events.

**Custom-action composition (P14).** `ControlAction.propose_effect`
returns either a single effect or a tuple of effects. The coordinator
applies the tuple in order under a single lock acquisition. Custom
actions thus compose built-in effects (e.g.
`dispatch_research_task` returns
`(FloorOverrideEffect(...), TurnInstructionEffect(...))`) without
extending the effect vocabulary. Genuinely new effect types are
v0.4+ trusted-extension territory.

### §6. Capability ledger

`CapabilityState` is the source of truth for all capability grants in
a room. `ParticipantInfo` does **not** store capabilities directly;
`ParticipantInfoView.capabilities` is computed by aggregating
`CapabilityState.active_for(pid)`.

**Grant record.**

```python
@dataclass(frozen=True)
class CapabilityGrant:
    grant_id: str                            # ULID
    grantor_id: str                          # "user" for root grants
    grantee_id: str
    capability_name: str                     # atomic verb
    scope: Optional[str] = None              # future: "room:main", "step:X"
    conditions: Optional[dict] = None        # future: predicate-based
    expires_at: Optional[float] = None       # monotonic clock seconds
    revoked_at: Optional[float] = None       # None = still active
    source_event_id: int                     # the capability_granted event
```

**Atomic verb vocabulary (v0.3).**

Mutation capabilities:
`grant_floor`, `cancel_lease`, `set_topic`, `set_anchor`,
`set_default_responder`, `update_allowed_speakers`, `set_roles`,
`switch_policy`, `send_dm`.

Meta-capabilities (scoped):
`grant_capability:<X>`, `revoke_capability:<X>`. Where `<X>` is any
mutation capability above. Unscoped `grant_capability` /
`revoke_capability` are reserved for human root and are never
agent-grantable.

Reserved for v0.4+:
`register_tool`, `invoke_workflow`, `manage_budget`,
`veto_control_action`.

Anti-escalation rule (P10 + P15): an agent's
`control_action_proposed(action_type="grant_capability:X", ...)` is
denied with `reason="anti_escalation"` if X is `grant_capability` or
`revoke_capability` (with or without scope). Only the human user
emits these via the runtime slash-command path.

**Runtime expiry flush.**

Before any capability-sensitive validation, the coordinator (under
lock) calls `_flush_expired_capabilities(now=time.monotonic())`. This
iterates `CapabilityState.grants`, finds entries with
`expires_at <= now and revoked_at is None`, emits
`capability_expired` events for each, and applies the corresponding
reducers. Then validation proceeds against post-flush state.

Replay does not call `_flush_expired_capabilities`; it only applies
historical `capability_expired` events. Wall-clock independence (P6).

**Bootstrap.** The first `admin`-equivalent capability grant comes
from the human user via a slash command (`/grant alice <capability>`)
or from `RoomConfig.initial_capabilities` for headless rooms. No
agent-mediated bootstrap.

**Capability transfer rules.**

- The human user can grant or revoke any capability.
- An agent with `grant_capability:<X>` can grant `<X>` to other
  participants (but not to themselves to avoid trivial escalation
  loops; self-grants are denied with `reason="anti_escalation"`).
- An agent cannot transfer their own grant authority to another agent
  (no `grant_capability:grant_capability:<X>` chains; depth capped at
  one hop for agents).
- An agent can revoke a capability they themselves hold (graceful
  step-down) but not another agent's capability of the same type
  unless they hold `revoke_capability:<X>`.
- Removing a participant via `remove_agent` drops all their
  capabilities (no inheritance). Re-granting is explicit.

### §7. Control action specification

**Registration.**

Three layers:

1. *Kernel-defined core actions* in `loom/kernel/control_actions.py`.
   Each declares `name`, `requires_capability`, `params_schema` (JSON
   Schema), and `propose_effect`.
2. *Pluggable extensions* via `RoomConfig.custom_control_actions:
   tuple[ControlAction, ...]`. Subject to P14 (typed effects only;
   no custom reducers).
3. *Per-participant advertisement* via
   `ConversationPolicy.control_actions_for_participant(participant_id,
   state) -> tuple[str, ...]`. Default in `BasicPolicy`: return all
   registered actions whose `requires_capability` is held by the
   participant.

**ControlInterest.** Per-participant configuration that determines
when the participant's actor wakes to consider a control proposal.

```python
@dataclass(frozen=True)
class ControlInterest:
    event_types: frozenset[str] = frozenset()
    relations: frozenset[str] = frozenset()
    channels: frozenset[str] = frozenset()
    direct_mentions: bool = False
    capabilities_required: frozenset[str] = frozenset()
```

The actor classifier fires a control proposal consideration iff the
event matches the participant's `ControlInterest` AND the participant
holds at least one capability for which they have an actionable
action. Templates set sensible defaults:

- `SupervisorTemplate.admin_control_interest = ControlInterest(
   event_types={"lease_closed_expired", "idle_timeout_warning",
                "policy_slow_warning", "user_turn_opened"},
   direct_mentions=True)`
- `AuditorTemplate.auditor_control_interest = ControlInterest(
   event_types={"control_action_proposed"},
   capabilities_required={"veto_control_action"})`
- Plain participants: `ControlInterest()` (empty; never wake for
  control proposals).

**Lifecycle.** Per §3, control-action leases follow the unified
lease lifecycle, with the off-lock phase invoking the LLM to produce
a `control_action_proposed` body. Under the second lock acquisition,
the coordinator re-validates by comparing
`lease.base_state_version` to `state.version` and re-running
capability + invariant checks.

**Denial reason taxonomy.**

```python
class ControlActionDenialReason(StrEnum):
    CAPABILITY_MISSING = "capability_missing"
    SCHEMA_INVALID = "schema_invalid"
    INVARIANT_VIOLATION = "invariant_violation"
    STATE_MISMATCH = "state_mismatch"        # base_state_version stale
    TARGET_INVALID = "target_invalid"         # e.g., grant_floor for departed pid
    BUDGET_EXHAUSTED = "budget_exhausted"
    RATE_LIMITED = "rate_limited"
    ANTI_ESCALATION = "anti_escalation"
    LLM_OUTPUT_INVALID = "llm_output_invalid" # proposer's JSON didn't parse
    LLM_NO_PROPOSAL = "llm_no_proposal"       # proposer produced no tool call
    VETOED = "vetoed"                         # v0.4
    KERNEL_ERROR = "kernel_error"             # bug; not policy decision
```

The reason is stable; audit tooling can query by reason without
schema bumps.

**Veto schema reservation.** `control_action_proposed.veto_window_ms`
defaults to 0 in v0.3; the coordinator does not wait. v0.4 sets
non-zero windows and introduces `control_action_vetoed` events; v0.3
journals replay correctly under v0.4 code because zero-window is a
clean no-op.

### §8. Causal references and trace context

Every event in v0.3 onward carries two metadata structures:

**Causal references** (semantic parentage).

```python
@dataclass(frozen=True)
class EventRef:
    room_id: str
    event_id: int
    event_type: str

class CausalRelation(StrEnum):
    RESPONDS_TO = "responds_to"
    TOOL_RESULT_FOR = "tool_result_for"
    CONTROL_ACTION_APPLIED = "control_action_applied"
    JOINED_FROM = "joined_from"
    TRIGGERED_BY = "triggered_by"
    REPLAY_OF = "replay_of"
    DEAD_LETTER_REROUTED_FROM = "dead_letter_rerouted_from"
    # Extensions reserve the prefix "custom:..."

@dataclass(frozen=True)
class CausalRef:
    ref: EventRef
    relation: CausalRelation
```

Population in v0.3 is optional but encouraged. The kernel itself
populates `causal_refs` for:

- `tool_result` events: `CausalRef(ref=tool_call_proposed.ref,
  relation=TOOL_RESULT_FOR)`.
- `control_action_applied/denied` events: `CausalRef(ref=
  control_action_proposed.ref, relation=CONTROL_ACTION_APPLIED)`.
- `chat` events emitted in response to a direct mention:
  `CausalRef(ref=mentioning_event.ref, relation=RESPONDS_TO)`.

Other event types accept user-populated causal refs as future work.

**Trace context** (execution lineage).

```python
@dataclass(frozen=True)
class TraceContext:
    trace_id: str               # session-scoped ULID
    span_id: str                # per-lease ULID
    parent_span_id: Optional[str] = None
```

Trace IDs are generated at session start; span IDs are generated at
lease acquisition. Events emitted during a lease's lifetime carry
that lease's `span_id`. Tool calls within a lease create child spans;
sub-turns (v0.6) also create child spans.

The kernel does not consume trace metadata in v0.3; populating it is
infrastructure for v0.4+ tracing tools.

### §9. Budget reservation/commit semantics

`BudgetLedger` tracks reservations, commits, and refunds against
typed `BudgetScope` keys.

```python
@dataclass(frozen=True)
class BudgetScope:
    room_id: str
    participant_id: Optional[str] = None
    action_kind: Optional[str] = None    # "chat", "control_action",
                                          # "tool_call"
    time_window: Optional[str] = None    # v0.5: "day:2026-05-16"

@dataclass
class BudgetLedger:
    reservations: dict[int, BudgetReservation]   # lease_id → reservation
    commits: dict[BudgetScope, int]               # accumulated actual cost
    refunds: dict[BudgetScope, int]               # accumulated refunds
    limits: dict[BudgetScope, int]                # configured caps

    def can_reserve(self, scope: BudgetScope, estimated: int) -> bool: ...
    def reserve(self, lease_id: int, scope: BudgetScope,
                estimated: int) -> None: ...
    def commit(self, lease_id: int, actual: int) -> None:
        """Commit actual cost; refund (reserved - actual) implicitly."""
    def refund(self, lease_id: int, reason: str) -> None:
        """Full refund (used when lease denied before LLM)."""
    def partial_commit_and_refund(self, lease_id: int,
                                  actual_llm: int) -> None:
        """Commit LLM cost; refund unused reservation. Used when
        proposal is denied post-LLM."""
```

**Reservation/commit flow (refines P9).**

- `lease_granted` → `reserve(lease_id, scope, estimated_max_tokens)`.
- `lease_closed(reason="released")` → `commit(lease_id, actual_tokens)`.
- `lease_closed(reason="denied:<check>")` (denied before LLM) →
  `refund(lease_id, reason="pre_llm")`.
- `lease_closed(reason="aborted_validation")` (denied post-LLM) →
  `partial_commit_and_refund(lease_id, actual_llm_tokens)`.
- `lease_closed(reason="aborted")` (actor crash) →
  `partial_commit_and_refund(lease_id, actual_llm_tokens_so_far)` if
  partial output exists; otherwise `refund(lease_id, reason="crash")`.
- `lease_closed(reason="expired")` → same as aborted; commit any
  consumed cost, refund the rest.

**Scope hierarchy.**

A scope rolls up to its parent: a participant-scoped budget is bounded
by the room-scoped budget; an action-kind-scoped budget within a
participant is bounded by the participant total. Reservations check
all ancestors:

```python
def can_reserve(self, scope, estimated):
    for ancestor in self._walk_scope_parents(scope):
        if self._effective_used(ancestor) + estimated > self.limits.get(ancestor, INF):
            return False
    return True
```

**Configurable scopes in v0.3.**

- `BudgetScope(room_id=R)` — room total.
- `BudgetScope(room_id=R, participant_id=P)` — per participant.
- `BudgetScope(room_id=R, action_kind="control_action")` — total spent
  on control actions across all participants.
- `BudgetScope(room_id=R, participant_id=P, action_kind=K)` — per
  participant per action kind.

`BudgetScope.time_window` is reserved in the schema; v0.5 adds rolling
day/hour caps.

### §10. Policy/control precedence

The effective state visible to a `LeaseCheck` and to the actor's
classifier is computed by layering:

```
effective_state = base_RoomState
                + policy_plan_for_current_turn
                + active_overrides (in journal order)
```

For the canonical case of `allowed_speakers`:

```python
def compute_effective_allowed_speakers(
    policy_plan: dict,
    active_overrides: tuple[FloorOverrideEffect, ...]
) -> frozenset[str]:
    result = frozenset(policy_plan.get("allowed_speakers", []))
    for ov in active_overrides:  # journal order
        if ov.mode == OverrideMode.ADD:
            result = result | ov.speakers
        elif ov.mode == OverrideMode.REPLACE:
            result = ov.speakers
        elif ov.mode == OverrideMode.BLOCK:
            result = result - ov.speakers
    return result
```

**Override mode semantics.**

| Mode | Operation | Use case |
|---|---|---|
| `ADD` | Union with current effective set | `grant_floor(alice)` augments allowed speakers |
| `REPLACE` | Replace effective set entirely | `override_allowed_speakers({alice})` enforces a specific roster |
| `BLOCK` | Set difference (remove) | `block_speaker(bob)` excludes a participant for the scope |

**Override scope semantics.**

| Scope | Lifetime | Cleared by |
|---|---|---|
| `ONE_LEASE` | Until the next successful lease acquisition uses the modified state | Coordinator on lease_granted |
| `CURRENT_TURN` | Until current user turn closes | Coordinator on user_turn_closed |
| `UNTIL_CLEARED` | Until explicit `clear_override(override_id)` control action | Explicit clear action |
| `PERSISTENT_ROOM_CONFIG` | Indefinite; modifies persistent state directly (e.g., `set_default_responder`) | New persistent control action |

Active overrides are tracked in `RoomState.control.active_overrides:
tuple[ActiveOverride, ...]` in creation order. The coordinator
prunes expired overrides at the relevant lifecycle event.

**Composition example.**

```
T0: Policy plan opens turn with allowed_speakers = {worker_a}.
T1: Admin invokes grant_floor(admin_self) with mode=ADD,
    scope=CURRENT_TURN.
T2: effective_allowed_speakers = {worker_a, admin_self}.
T3: Admin invokes override_allowed_speakers({admin_self}) with
    mode=REPLACE, scope=CURRENT_TURN.
T4: effective_allowed_speakers = {admin_self}.
    (REPLACE wipes the prior ADD's effect.)
T5: Turn closes. Both overrides cleared.
T6: Next turn opens with policy plan; no overrides active.
```

Sequential application by journal order gives deterministic replay
and intuitive "latest intent wins" semantics. Two ADDs commute; a
REPLACE wipes prior overrides of the same scope; BLOCK only removes
speakers currently in the effective set.

---

## Part III — v0.3 Scope

What ships in v0.3, what is reserved-in-schema for future stages, and
what is explicitly deferred.

### Ships in v0.3

**Capability ledger.** `CapabilityState`; atomic verb capabilities;
scoped grant authority (`grant_capability:<X>`); anti-escalation;
runtime expiry flush; `capability_granted/revoked/expired` events;
boot-time grants via `RoomConfig.initial_capabilities`; human-user
grants via slash command (`/grant <pid> <capability>`,
`/revoke <pid> <capability>`).

**Control plane.** `ControlActionLease` parallel to `UserTurnLease`;
off-lock proposal generation; under-lock validation with
`base_state_version` check; effect registry with versioned reducers
(kernel-defined effects only); custom `ControlAction` registration via
`RoomConfig.custom_control_actions` (returning built-in effect types
or tuples); kernel-defined control actions per §6 vocabulary;
`control_action_proposed/applied/denied` events; per-participant
`ControlInterest` configuration; veto-window field reserved.

**Override modes and scopes.** `FloorOverrideEffect` with
ADD/REPLACE/BLOCK modes; `ControlEffectScope` with ONE_LEASE,
CURRENT_TURN, UNTIL_CLEARED, PERSISTENT_ROOM_CONFIG values; sequential
journal-order application; coordinator prunes expired overrides.

**Event taxonomy.** Unified `lease_closed(reason)` event replacing
v0.2's `lease_denied`; `causal_refs: tuple[CausalRef, ...]` field on
all event constructors; `TraceContext` field on all event
constructors; stable `ControlActionDenialReason` enum.

**Budget.** `BudgetLedger` with `reserve`/`commit`/`refund`/
`partial_commit_and_refund`; three-way denial accounting; per-room,
per-participant, per-action-kind scopes; `BudgetCheck` extended to
check ancestor scopes.

**Kernel state restructuring.** `KernelState` as transactional root;
`RoomState`, `CapabilityState`, `BudgetLedger` as modular subsystems;
`KernelState.version` counter; `WorkflowState`/`ToolState`
placeholders; snapshot v5 → v6 with v6 defaults for v5 loads.

**Lock discipline.** `_assert_not_holding_lock` at all I/O entry
points; lock-held method naming convention; test injection of
lock-assertion mocks; lint tripwire for obvious I/O patterns.

### Reserved in schema (populated empty/default in v0.3)

- `target_room_id: Optional[str]` on control-action events (v0.7
  multi-room).
- `veto_window_ms` on `control_action_proposed` (v0.4 vetoes).
- `BudgetScope.time_window` (v0.5 day/hour caps).
- `KernelState.workflow: Optional[WorkflowState]` (v0.5).
- `KernelState.tools: Optional[ToolState]` (v0.4).
- `CapabilityGrant.scope`, `conditions` (v0.5+ scoped capabilities).

### Deferred to v0.4+

- External tool registry, tool_call/result events, sandboxing,
  off-lock tool execution, structured `Agent.stream_with_tools`.
- Veto windows; tiered admin/auditor (C2 topology).
- Trusted-extension framework for custom effect types and reducers.
- Workflow runtime state (sequential workflows in v0.5).
- Sub-turns; parallel workflows; scratch channels; reducer pattern
  (v0.6).
- Multi-room; cross-room causal metadata; bridge protocol (v0.7).
- Distributed runtime; A2A on the wire; network identity/auth
  (v1.0+).
- Observability dashboards; rich tracing UI; metrics export. (Trace
  metadata is populated from v0.3; tooling builds on it later.)

---

## Part IV — Open Questions for v0.4+

These are deferred but tracked so they do not disappear from
attention.

1. **Optimistic concurrency contention.** With off-lock LLM phases,
   proposals can be invalidated by intervening state changes
   (`STATE_MISMATCH` denials). v0.3 retries are at the actor's
   discretion (re-propose on the next event). High-contention rooms
   may need pessimistic capability locks, proposal merging, or
   priority queues. Measure under load in v0.3 before designing.
2. **Subscription filter generalization.** v0.3 ships `ControlInterest`
   for control proposals. v0.4 generalizes to a `Subscription`
   primitive applicable to chat triggers, tool triggers, reactive
   triggers. Likely intersects with reactive policy design.
3. **Veto window protocol.** v0.4 must specify: who emits
   `control_action_vetoed`? what happens on multiple vetoes? does the
   actor pay for the LLM call that produced a vetoed proposal? (per
   §9: yes, model cost is real even if vetoed.)
4. **Trusted-extension framework.** When custom effect types and
   reducers become reachable, how are they installed (process boot
   config), audited (invariant-test harness), and isolated
   (sandboxed reducer execution)?
5. **Workflow runtime state shape.** §1 reserves `WorkflowState`;
   v0.5 must define `WorkflowRun`, `WorkflowStep`, `WorkflowGraph`,
   lifecycle events, and lease integration (per-step leases).
6. **Sub-turns.** Parallel workflows require splitting `UserTurn`'s
   accountability scopes. Possible designs: child turns (parent_turn_id
   + branch_id), independent work units, or extending `UserTurn` with
   sub-lease arrays. Defer to v0.6 design phase.
7. **Inter-agent secrets / private channels.** Beyond DMs, do agents
   need shared private channels for coordination? Touches channel
   typing (v0.6 scratch channels).
8. **Agent lifecycle.** Runtime spawning and despawning of
   participants. Connects to multi-room (v0.7 sub-room creation).
9. **Distributed lease arbitration.** When the bus crosses processes
   (v1.0+), lease acquisition needs a distributed consensus protocol
   or a single arbitrator. Lamport timestamps? Raft? Defer to v1.0.
10. **Observability dashboards.** What does a debugger UI for a
    multi-agent room look like? Likely a separate project that
    consumes the journal + trace metadata; not in kernel scope.

---

## Appendix A — Glossary

**Capability.** A typed permission granted to a participant. Verb
names (`grant_floor`, `set_topic`, etc.) correspond one-to-one with
control actions. Stored in `CapabilityState`.

**Control action.** An agent-initiated or human-initiated request to
mutate `KernelState`. Distinguished from chat (which is content) and
from external tool calls (which have side effects outside Loom).

**ControlActionLease.** Lease kind that authorizes a participant to
propose a control action. Atomic from the state-mutation perspective;
the proposal LLM call happens off-lock between lease acquisition and
proposal validation.

**Control effect.** The typed, versioned datum carried by
`control_action_applied`. Mutates `KernelState` via a registered
reducer.

**Control plane.** The event plane for state mutations
(`control_action_*`, `capability_*`, `lease_*`).

**ControlEffectScope.** Lifetime annotation on a control effect:
ONE_LEASE, CURRENT_TURN, UNTIL_CLEARED, or PERSISTENT_ROOM_CONFIG.

**Conversation plane.** The event plane for participant content
exchange (`chat`, `stream_*`, `user_turn_*`).

**Effect registry.** Maps `(effect_type, schema_version) → reducer`.
Reducers mutate `KernelState`. Custom registration is v0.4+
trusted-extension only.

**Execution plane.** The event plane for external tool invocations
(`tool_call_proposed`, `tool_result`). Has different replay semantics
from conversation and control: tools are never re-executed; recorded
results are read directly.

**KernelState.** Transactional root containing all kernel state.
Mutated exclusively by the coordinator under its lock.

**Lease.** Generalized authorization for a participant to perform an
action of a given kind within a TTL and budget reservation.

**Lock discipline.** The rule (P4) that no I/O happens while the
coordinator's lock is held. Enforced structurally via naming
conventions and runtime assertions.

**OverrideMode.** Annotation on a `FloorOverrideEffect` (and similar
override effects): ADD, REPLACE, or BLOCK.

**Reducer.** Pure function `(KernelState, Effect) → None` that
mutates state. Invoked by the coordinator under lock; never invoked
by policy, agent, or tool code.

**ScopedOverride.** An override effect bound to a
`ControlEffectScope`. Tracked in `RoomState.control.active_overrides`
in journal order.

**Template.** A pre-configured bundle of policy, tool set, and
capability assignments shipped as an example. Not a kernel concept;
runtime convenience for instantiating common topologies.

---

## Appendix B — Discussion provenance

This doctrine emerged from four rounds of architectural review:

- **Round 1 (2026-05-15)**: Initial design analysis covering CEO
  topologies (0, 1, 2, N, capability-based), bus collaboration
  patterns (broadcast-and-claim, mention-and-respond, CEO-dispatched,
  workflow-driven, auction/bidding, blackboard, reactive), and policy
  strategies (open chat, round-robin, single-responder,
  manager-dispatch, workflow, debate, reactive, blackboard, hybrid).
  Recommended capability-based design with 1-CEO + OpenChat as
  starter template.

- **Round 2 (2026-05-15)**: External review (GPT) identified several
  issues with Round 1: `AdminBypassCheck` was dangerous; admin
  commands should not be conflated with tools; workflow should be
  runtime state, not policy; reactive mode must not drop leases;
  parallel workflows need sub-turns. Conceded most; added three-plane
  framing.

- **Round 3 (2026-05-15 → 2026-05-16)**: Second external review
  refined the replay model (versioned effects, not raw state deltas),
  control-action scheduling (`ControlActionLease`), capability
  expiry replay rules, scoped grant authority, and required v0.3
  budget. Locked the 15 principles.

- **Round 4 (2026-05-16)**: Third external review nailed
  implementation-level traps: no I/O under lock (`_assert_not_holding_lock`
  pattern), three-way denial accounting, custom-reducer boundary
  (kernel extension, not room config), capability runtime
  pre-validation, scoped grant authority via namespaced capability
  names, unified `lease_closed(reason)`, configurable `ControlInterest`,
  ADD/REPLACE/BLOCK override modes with sequential journal-order
  application.

Each round's full text is in the conversation transcript at
`/home/jacks.local/hdubey/.claude/projects/-mmfs1-scratch-jacks-local-hdubey-07-LLM/`.

---

## Cross-references to study artifacts

This doctrine builds on the curriculum artifacts:

- `00-orientation.md` — repo orientation, glossary,
  v0.1.2 limits + v0.2 roadmap. The "v0.2 roadmap" section is
  superseded by the v0.2 refactor (now complete, see
  `~/.claude/plans/can-you-see-my-zesty-dolphin.md`).
- `01-kernel-primitives.md` — events, room, obligations, user_turn.
  `RoomState` referenced here becomes `KernelState.room` in v0.3.
- `02-kernel-bus.md` — bus + addressees. v0.3 preserves all bus
  invariants including the `_KERNEL_AUTH` token (v0.2 PR 2); the
  control-plane events use `bus.post_internal` for kernel-emitted
  events.
- `04-kernel-actor-journal.md` — actor + journal. v0.3 extends the
  actor's `_decide_once` decision space; preserves journal
  invariants.
- `05-kernel-coordinator.md` — coordinator. The largest expansion
  surface for v0.3; coordinator gains `_flush_expired_capabilities`,
  effect registry, control action validation, and KernelState
  ownership.
- `06-contracts-policies.md` — contracts and policies. v0.3 extends
  the `ConversationPolicy` ABC with
  `control_actions_for_participant`; v0.2 hooks (`charter_text`,
  `dead_letter_target`, `prompt_sections`, `should_post_response`)
  remain unchanged.
- `10-synthesis.md` — invariant index. v0.3 adds new invariants
  (capability state consistency, lock discipline, replay
  determinism for effects) that must be added to the index.
- `aios-architecture-comparison.md` — comparison with AIOS. The
  doctrine takes the kernel-boundary discipline of AIOS (Scheduler,
  Context Manager, Memory Manager, Storage Manager, Tool Manager) and
  reorganizes it into three planes plus modular `KernelState`
  subsystems.

---

## Required reading order for v0.3 implementation

For any PR landing v0.3 work, the author and reviewer must have read:

1. This document (the doctrine).
2. `05-kernel-coordinator.md` (the surface most v0.3 PRs will modify).
3. `06-contracts-policies.md` (the contracts that v0.3 extends).
4. The PR description must cite which principle(s) it implements or
   which subsystem specification(s) it satisfies.

---

*Doctrine v1.0 — frozen 2026-05-16. Future revisions require an
explicit revision PR with rationale; silent design drift is a process
violation.*
