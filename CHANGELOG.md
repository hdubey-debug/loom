# Changelog

All notable changes to **Loom** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] — 2026-05-20

### Added
- **SUMMARIZATION lease + Path A/Path B convergence + backoff**
  (v0.3.x PR 5; doctrine P22 / §3 / §6 / §7 / study/14).
  `LeaseKind.SUMMARIZATION` is the 6th kind; `SummarizationContext`
  is its frozen-dataclass context with `scope`, `covers_event_range`,
  `triggered_by` (``"policy"`` for Path A, ``"control_action"`` for
  Path B), `triggering_event_id`, `required_capability`, and
  `thread_id`. Two new capabilities — `SUMMARIZE` (request authority)
  and `EMIT_SUMMARY` (production authority) — plus their meta verbs
  (`GRANT_/REVOKE_CAPABILITY_*`) bring the enum to 33 members.
  `_SummarizerSlotCheck` (SUMMARIZATION-only) enforces the slot
  match on Path A only; the existing `_CapabilityCheck.applies_to`
  expands to include `SUMMARIZATION` (reading `required_capability`
  off `SummarizationContext` symmetrically with the v0.3 PR 9
  control-action wiring). Two coordinator entry points converge at
  the lease: `schedule_summarization(scope)` is Path A (policy
  trigger, slot-occupant holder, `EMIT_SUMMARY` cap); 
  `request_summarization(requester, scope)` is Path B (user / 
  capability-granted agent, no slot enforcement, `SUMMARIZE` cap).
  Both emit a `summarization_scheduled` audit event. Backoff is
  wired end-to-end: `submit_summary_proposed` increments
  `ContextState.failure_count` (skip `ANCHOR_CONFLICT`), and when
  the count reaches `RoomConfig.summarizer_max_consecutive_failures`
  emits `compaction_disabled` + applies `CompactionDisabledEffect`
  which adds the pair to `ContextState.disabled_scopes`. A
  successful commit clears the counter; the existing
  `DefaultSummarizerSetEffect` reducer also clears
  `failure_count` / `disabled_scopes` for the previous slot
  occupant. New `/summarize [thread=… actor=…]` slash command
  routes through `dispatch_slash_command` to
  `coordinator.request_summarization("user", scope)` (Path B with
  user bypass).
- **Prompt builder reads ContextState + pressure estimator**
  (v0.3.x PR 4; doctrine §6 / §10 / study/14). `build_prompt`
  in `loom/kernel/prompt.py` now consults
  `KernelState.context.active_summary_by_scope` first for the
  room/main-thread scope (constructed from
  `RoomConfig.room_id` + `thread_id="main"`); when present, the
  committed SummaryRecord's text drives the `<<<PRIOR ROOM SUMMARY>>>`
  block. Legacy `summary` events on the bus remain a fallback so
  v0.3 sessions without committed compaction events keep rendering.
  `loom/kernel/context.py` gains `ContextPressure` (dataclass) +
  `estimate_context_pressure(...)` (pure function with a 4-component
  cache key: participant_id, scope, kernel_state_version,
  prompt_template_hash) + `select_compaction_range(state, scope,
  bus_length)` for Path A scheduling hints. Four new `RoomConfig`
  fields land for the compaction subsystem: `room_id` (default
  `"main"`; used as the `ContextScope.room_id` component),
  `context_pressure_threshold_ratio: float = 0.7`,
  `context_pressure_check_interval_events: int = 10`, and
  `summarizer_max_consecutive_failures: int = 3`.

- **Summary event lifecycle + commit pipeline** (v0.3.x PR 3;
  doctrine P18 / P19 / §3 / §6 / study/14). Three new control event
  constructors land in `loom/kernel/events.py`: `summary_proposed`,
  `summary_committed`, `summary_failed` — all three registered in
  `CONTROL_TYPES` and gated by per-control_type body validators.
  Three matching effect subclasses + reducers
  (`SummaryProposedEffect` audit-only, `SummaryCommittedEffect`
  installs the record + supersession edge, `SummaryFailedEffect`
  bumps `failure_count` but skips `ANCHOR_CONFLICT`) register into
  `build_kernel_registry()`. The coordinator gains
  `submit_summary_proposed(record: SummaryRecord) -> SummaryCommitResult`
  implementing the doctrine §6 commit flow: off-lock pre-validation
  via `validate_summary_record`; under-lock anchor check (the
  record's `input_summary_ids` must include whatever is currently in
  `active_summary_by_scope[scope]`, or both must be empty for a
  first-gen summary); commit emits proposed → committed and applies
  the reducer. `ANCHOR_CONFLICT` races emit a journaled
  `summary_failed` without incrementing `failure_count` so genuine
  validator failures remain distinguishable from contention. Replay
  is driven entirely by recorded `summary_committed` events — the
  validator is never re-run.

- **ContextState + SummaryRecord + structural validators** (v0.3.x
  PR 2; doctrine P17 / §3.2 / §3.3 / §3.4 / §6 / study/14).
  `loom/kernel/context.py` now hosts `SummaryRecord` (the lineage-
  preserving compaction payload), `ContextState` (view-layer state:
  `summaries`, `active_summary_by_scope`, `supersession_edges`,
  `failure_count`), `SummaryFailureReason` (9-member enum), and two
  pure validators: `validate_lineage` (range contiguity / overlap /
  union-equals-covers) and `validate_summary_record` (composed with
  bus-bounds + retained-id checks). `KernelState.context` is wired
  with `default_factory=new_context_state`; `KernelStateView`
  exposes it. `KERNEL_STATE_SCHEMA_VERSION` bumped from 6 → 7;
  `Journal._state_to_dict` emits the new `"context"` slot,
  `restore_kernel_state` reads it, and `_migrate_v6_to_v7` provides
  an idempotent in-memory v6→v7 migration (existing v6 snapshots
  load with an empty `ContextState` — fully backward-compatible).
- **Thread membership on every event + ContextScope** (v0.3.x PR 1;
  doctrine P21, §3.1 / study/14). `Event.thread_id: str = "main"` is
  a new envelope field, round-tripped through the journal and
  validated as a non-empty string at `MessageBus._post_unchecked`.
  Each of the 5 v0.3 `LeaseContext` subclasses gains a
  `thread_id: str = "main"` field so a future per-thread compaction
  (PR 5) can drive the lease's scope onto its emitted events.
  Two coordinator emit helpers — `_emit_under_lease(lease, event)`
  inherits `thread_id` from the lease's context, and
  `_emit_system(event, thread_id="main")` stamps a coordinator-
  originated event — are added for use by the PR 3 / PR 5 summary
  emitters; existing v0.3 emit sites are unchanged because the
  `"main"` default is correct for room-wide events. New
  `loom/kernel/context.py` defines the `ContextScope` dataclass
  (`room_id`, `thread_id="main"`, `actor_id=None`) — the addressing
  key that PR 2 will hang `ContextState` off of. Legacy v0.3
  journals without `thread_id` load as `"main"` so old snapshots
  remain forward-compatible.

### Changed
- **Cursor persistence + per-policy threshold** (v0.3 PR 13; closes
  v0.2.1 audit deferrals A3 and D3). New
  `loom/kernel/actor_state.py` defines `ActorStateRecord` and
  `CursorAdvancedEffect` (typed semantic effect, registered via
  `register_cursor_advanced_reducer()`) so actor cursor state can
  persist through the journal — populating the `KernelState.actors`
  slot reserved by PR 1. The full actor-side wiring (have
  `ParticipantActor._advance_cursor` route through the registry) is
  a v0.3.x follow-up; PR 13 ships the data shape, reducer, and the
  coordinator-side registration so the actor can opt in. `cursor_for(state,
  participant_id)` helper returns the persisted cursor or `None`.
  v0.2.1's module-level `_POLICY_SLOW_THRESHOLD_MS` constant now
  falls back to `RoomConfig.policy_slow_threshold_ms` (default
  100.0) at the read site — different policies can tune the
  observability noise floor without kernel edits. The legacy
  module constant remains as the back-compat default so pre-v0.3
  RoomConfig pickles drive the same behavior.
- **Scoped floor overrides with composition precedence** (v0.3 PR 10;
  doctrine §10). New `loom/kernel/floor_overrides.py` adds
  `FloorOverrideMode` (`ADD`/`REPLACE`/`BLOCK`),
  `FloorOverrideScope` (`ONE_LEASE`/`CURRENT_TURN`/`UNTIL_CLEARED`/
  `PERSISTENT_ROOM_CONFIG`), `ActiveOverride` dataclass, and a
  `compute_effective_speakers(base, overrides)` helper applying the
  §10 composition rule (REPLACE wins over ADD; BLOCK strips from
  either). The PR 3-declared `FloorOverrideEffect` gains its reducer
  via `register_floor_override_reducer()` — it lazy-attaches an
  `active_overrides: list[ActiveOverride]` field on
  `RoomControlState` so v0.2 instances forward-load cleanly. Three
  new control actions (`GrantFloorAction` requires `GRANT_FLOOR`,
  `BlockFloorAction` + `OverrideAllowedSpeakersAction` require
  `UPDATE_ALLOWED_SPEAKERS`) register into the PR 9 action registry.
  Lifecycle pruning helpers `prune_overrides_for_lease` and
  `prune_overrides_for_turn` will be wired into the coordinator's
  `release_lease` / `close_user_turn` paths in v0.3.x once
  callers depend on them.
- **Control action dispatch with three registration layers** (v0.3
  PR 9; doctrine P3 / P13 / P14 / §7). New
  `loom/kernel/control_actions.py` defines the `ControlAction`
  protocol, `ControlActionRegistry`, `DenialReason` enum (9 members
  per doctrine §7), `ControlInterest` dataclass (per-participant
  control-event subscription filter), and 5 kernel-built-in actions
  (`SetTopicAction`, `SetAnchorAction`, `SetDefaultResponderAction`,
  `SetRolesAction`, `SetStyleAction`) — remaining 4 (UpdateAllowedSpeakers,
  SwitchPolicy, SendDM, GrantFloor, CancelLease) land with their
  reducer support in PR 10 / PR 12. `RoomCoordinator.__init__`
  hydrates an action registry from kernel built-ins +
  `RoomConfig.custom_control_actions` (read via `getattr` so
  pre-v0.3 RoomConfig instances pass an empty tuple). The new
  `propose_control_action(proposer, action_name, params)` method
  runs the full lifecycle (proposed → validate_params → acquire
  CONTROL_ACTION lease → propose_effect → _apply_effect → release →
  applied) and returns a `ControlActionResult`. Every denial path
  emits `control_action_denied(reason=...)` with the
  `DenialReason` value; the lease + effect path uses PR 7's
  `acquire_typed_lease` so the same `_CapabilityCheck` gates
  authorization. **P14** is enforced by the effect registry's
  `UnknownEffect` raise — a custom action that returns a
  non-registered `ControlEffect` subclass surfaces as
  `CHECK_RAISED`. The `ControlActionContext` carries the action's
  `required_capability` directly so `_CapabilityCheck` looks up
  authority without a name→enum guess.
- **Unified `Lease` abstraction with five `LeaseKind` discriminator**
  (v0.3 PR 7; doctrine P8 / §3). New `loom/kernel/leases.py` defines
  `LeaseKind` (5 members: `USER_TURN`, `CONTROL_ACTION`,
  `TOOL_INVOCATION`, `WORKFLOW_STEP`, `REACTIVE`), `LeaseContext`
  tagged union (5 frozen dataclasses), and `Lease` dataclass with a
  `__post_init__` invariant that `lease.kind` matches `type(context)`.
  The v0.2 `TurnLease` remains in place as the USER_TURN
  specialization at the back-compat boundary; new
  `RoomCoordinator.acquire_typed_lease(kind, holder, context)`
  returns a `Lease` directly and is the entry point PR 9's control
  action dispatch will use. The eight default `LeaseCheck`
  implementations gain `applies_to: frozenset[LeaseKind]` class
  attributes (USER_TURN-only for `_OpenTurnCheck`,
  `_AllowedSpeakerCheck`, `_PerParticipantCapCheck`,
  `_MaxResponsesCheck`, `_ThrottleCheck`, `_BudgetCheck`; all-kinds
  for `_ParticipantRegisteredCheck`, `_ParticipantActiveCheck`); a
  new `_CapabilityCheck` (CONTROL_ACTION-only) enforces P10
  capability gating against `KernelState.capabilities`. The
  `check_applies_to(check)` helper handles legacy custom checks
  (treated as universally-applicable when `applies_to` is absent),
  so existing v0.2 `RoomConfig.lease_checks` overrides keep working.
  Each typed lease carries a `trace_span_id` (PR 4's `TraceContext`)
  so events posted under the lease can inherit observability scope.
- **`BudgetLedger` three-way accounting subsystem on `KernelState`**
  (v0.3 PR 6; doctrine P9 / §9). New `loom/kernel/budgets.py` declares
  `BudgetScope` (frozen 4-tuple: `room_id`, `participant_id`,
  `action_kind`, `time_window` — last reserved for v0.5+),
  `BudgetReservation` (frozen; one per lease), and `BudgetLedger`
  with the full reserve / commit / refund / partial_commit_and_refund
  / can_reserve / outstanding / remaining API. Pure refunds (pre-LLM
  cancellation) drop the hold without touching `commits[scope]` or
  `refunds[scope]`; only `partial_commit_and_refund` populates both
  sides so the running invariant (sum of live reservations + net
  committed ≤ limit) stays load-bearing. Three reducers register via
  `register_budget_reducers()`; the coordinator hydrates
  `KernelState.budget` in `__init__`. New event constructors
  `budget_reserved` / `budget_committed` / `budget_refunded` in
  `events.py` with payload validators. PR 7 wires reservation into
  the unified `acquire_lease` path; PR 8 wires commit/refund into the
  `release_lease(reason=...)` taxonomy. Replay-determinism:
  `BudgetLedger` never reads the clock — `reserve` takes an explicit
  `now` parameter (defaults to 0.0) so the coordinator passes
  `time.monotonic()` in live operation and the journal-replay path
  uses each event's `Event.ts`.
- **`CapabilityState` first-class subsystem on `KernelState`** (v0.3
  PR 5; doctrine P1, P10, §6). New `loom/kernel/capabilities.py`
  declares `CapabilityName` (27-member str-enum: 9 mutation verbs +
  9 `GRANT_CAPABILITY_*` + 9 `REVOKE_CAPABILITY_*`),
  `CapabilityGrant` (frozen; carries grantor, grantee, capability,
  granted_at, expires_at, revoked_at, source_event_id; v0.4+
  reservations for `scope` and `conditions`), and `CapabilityState`
  (the ledger: `grants` dict + `has`/`grants_for`/`effective_capabilities`/
  `find_expired`/`revoke`/`mark_expired` API). Three reducers
  (`_apply_capability_granted`/`_revoked`/`_expired`) register
  themselves onto the coordinator's effect registry via
  `register_capability_reducers()`; the coordinator now hydrates
  `KernelState.capabilities` in `__init__`. New event constructors
  `capability_granted` / `capability_revoked` / `capability_expired`
  in `events.py` with per-control-type shape validators; the three
  control types are added to `CONTROL_TYPES`. **Anti-escalation
  invariant (P1)**: only the user (`grantor_id == "user"`) may grant
  a `GRANT_CAPABILITY_*` / `REVOKE_CAPABILITY_*` meta verb — agent
  attempts raise `EscalationDenied`, surfaced by PR 9's denial path
  as `INSUFFICIENT_CAPABILITY`. Replay-determinism: `find_expired`
  takes an explicit `now` parameter (no clock reads inside the
  ledger) so the watchdog drives it in live operation while replay
  drives it from `Event.ts` of each capability_expired line.
- **Typed `causal_refs` + `TraceContext` on every event envelope**
  (v0.3 PR 4; doctrine P11 / P12 / §8; closes v0.2.1 audit deferral
  C3 typed form). New `loom/kernel/causality.py` declares `EventRef`,
  `CausalRelation` (7 predicates: `RESPONDS_TO`, `TOOL_RESULT_FOR`,
  `CONTROL_ACTION_APPLIED`, `JOINED_FROM`, `TRIGGERED_BY`,
  `REPLAY_OF`, `DEAD_LETTER_REROUTED_FROM`), `CausalRef`, and
  `TraceContext`, plus `new_trace()` / `child_span(parent)` helpers
  built on `secrets.token_hex(16)` (no ULID dependency).
  `Event.causal_refs` is now typed `tuple[CausalRef, ...]` (the
  v0.2.1 reservation tightens to its v0.3 shape) with a
  `coerce_causal_refs` `__post_init__` step that converts JSON-loaded
  list-of-dicts back to the typed tuple. A new `Event.trace:
  TraceContext | None` field rides alongside; both fields round-trip
  through `to_jsonl` / `from_jsonl` with nested dicts. Old v0.2.0 /
  v0.2.1 journal lines without the keys load with `causal_refs=()`
  and `trace=None`. `RoomCoordinator` allocates a root
  `TraceContext` at construction (`coord.trace_root`) and exposes
  `new_child_span()` so PR 7's lease unification can stamp a span on
  every lease — and so PR 8's `control_action_applied` event can
  carry a `CausalRef(CONTROL_ACTION_APPLIED, ...)` to the
  corresponding `control_action_proposed`.
- **Typed, versioned semantic effects routed through a reducer
  registry** (v0.3 PR 3; doctrine P6, P7, §5; closes v0.2.1 audit
  deferral C4). New `loom/kernel/effects.py` defines `ControlEffect`
  (base) + 13 doctrine-required subclasses (`TopicChangedEffect`,
  `AnchorAssignedEffect`, `ChairAssignedEffect`,
  `DefaultResponderSetEffect`, `DefaultSummarizerSetEffect`,
  `RolesAssignedEffect`, `StyleChangedEffect`, `LeaseCancelledEffect`,
  `FloorOverrideEffect`, `CapabilityGrantedEffect`,
  `CapabilityRevokedEffect`, `CapabilityExpiredEffect`,
  `PolicySwitchedEffect`, `BudgetReservedEffect`,
  `BudgetCommittedEffect`, `BudgetRefundedEffect`) + `EffectRegistry`
  with `(effect_type, schema_version) → reducer` dispatch +
  `build_kernel_registry()` bootstrap. PR 3 wires reducers for the
  seven v0.2-backable effects (the slot setters); the capability /
  budget / floor-override / lease-cancelled / policy-switched
  reducers register at their owning PR's load time (5, 6, 8, 9, 10).
  `RoomCoordinator.__init__` constructs the registry; the new
  `_apply_effect(effect)` helper looks up the reducer, runs it under
  the lock, and bumps `KernelState.version`. The v0.2 slot setters
  (`set_topic`, `set_default_responder`, `set_anchor`, `set_chair`,
  `set_default_summarizer`, `set_roles`, `set_style`) now construct
  the corresponding effect and route the state mutation through
  `_apply_effect`; bus emission of their legacy control events is
  preserved byte-identical so v0.2.x replay continues to work. PR 9
  routes these mutations through `control_action_*` events; PR 3 is
  the structural foundation.
- **`KernelState` is now the transactional root of all kernel-owned
  mutable state** (v0.3 PR 1; doctrine P5 / §1). New
  `loom/kernel/state.py` introduces `KernelState` wrapping the v0.2
  `RoomState` under a `room` field, plus reserved sibling fields for
  v0.3 subsystem states (`capabilities` PR 5, `budget` PR 6, `actors`
  PR 13) and post-v0.3 subsystems (`workflow` v0.5+, `tools` v0.4+).
  A `version: int` counter is bumped by `KernelState.bump_version()`
  under the coordinator lock on every applied state mutation — long-
  term call site is PR 3's effect-registry dispatch; PR 1 wires it
  into the core mutation methods (`register_participant`,
  `unregister_participant`, `set_topic`, `set_default_responder`,
  `set_anchor`, `set_chair`, `set_default_summarizer`, `set_roles`,
  `set_wait_for_user_flag`, `set_style`). `KernelStateView` is a
  deep-frozen read-only projection; PR 1 exposes `room` /
  `version` / `schema_version` — subsequent PRs add subsystem
  sub-views. Snapshot envelope bumps `SNAPSHOT_VERSION` 5 → 6: the
  v5 RoomState fields nest under `"room"`, sibling slots
  serialize as `null` until their owning PR lands, and a
  `"kernel_version"` counter rides alongside. v1–v5 snapshots are
  still loadable via the in-memory `_migrate_v5_to_v6` migrator;
  `restore_state` (v0.2 surface) continues returning `RoomState`,
  and a new `restore_kernel_state` returns the full `KernelState`.
  `RoomCoordinator.__init__` accepts either `RoomState` (back-compat)
  or `KernelState`; `coordinator.state` still aliases the underlying
  RoomState and a new `coordinator.kernel_state` exposes the v0.3
  root. Closes doctrine §1; gating foundation for PRs 3, 5, 6, 7,
  13.
- **Actor cursor advance is now dispatch-outcome-aware** (v0.2.1 PR 4,
  addresses audit findings A1, A2, A4; doctrine P6 foundation).
  Pre-fix, ``ParticipantActor._decide_once`` advanced the cursor to
  ``max(snap.id)`` BEFORE dispatch, losing the trigger event on
  lease denial (no subsequent eligibility change could re-pick it
  up). Post-fix:
  - ``_decide_once`` no longer advances the cursor; ``step()`` does
    that explicitly via ``_advance_cursor`` after
    ``_dispatch_decision`` returns the grant outcome.
  - The denied trigger is re-pended into the existing
    ``_pending_direct_mentions`` LRU (now general-purpose, not
    direct-mention-specific) AND added to a new
    ``_denied_trigger_ids`` set.
  - The next ``_decide_once`` short-circuits the trigger to ``SKIP``
    if it's in the denied set; the set is cleared whenever a fresh
    user-posted event arrives in the snap (signalling possible
    eligibility change). Together these prevent the
    ``lease_denied`` → ``wait_after`` → re-decide tight loop.
  - ``AgentDecision.considered_event_ids`` field removed — it was
    unused since v0.2 (kernel-internal dataclass; no public API).
  - Module docstring at ``actor.py:1-32`` rewritten to match the
    actual cursor semantics ("highest event id examined", not
    "next event id to read").

### Added
- **Streaming-stall watchdog** (v0.3 PR 12; closes v0.2.1 audit
  deferral D2). New `RoomCoordinator.on_stream_chunk(lease)` hook
  records monotonic timestamps on each emitted stream delta;
  `check_streaming_stall(now)` reaps leases whose latest chunk is
  older than `RoomConfig.stream_stall_threshold_s` (default 30s).
  Stalled leases get a `stream_stalled` event followed by
  `lease_closed(reason="aborted")`; emission happens off-lock per
  P4. `streaming.run_streaming_call` calls the hook on each chunk.
  Wired into `_watchdog_loop` alongside `check_lease_ttl`. New
  `stream_stalled` event constructor + payload validator added to
  `CONTROL_TYPES`. Off-lock policy classification refactor (the
  other half of PR 12 in the roadmap) is deferred to v0.3.x — the
  required structural changes touch the same hot path as the v0.2.1
  policy_slow watchdog and warrant a separate review window.
- **Slash-command human root actions** (v0.3 PR 11; doctrine P15).
  New `loom/slash_commands.py` defines `parse_slash_command(text)` →
  `ParsedCommand(action_name, params)` for the v0.3 set:
  `/grant <participant> <CAPABILITY> [expires_in=<s>]`,
  `/revoke <grant_id>`, `/topic <text>`, `/anchor <participant>`,
  `/responder <participant>`, `/floor <p1> [p2 ...]`,
  `/policy <name>`. `SlashCommandRegistry` lets runtime add custom
  commands. `dispatch_slash_command(coordinator, text)` calls
  `coordinator.propose_control_action(proposer_id="user", ...)` so
  user-issued commands bypass the agent capability gate (the
  `_CapabilityCheck` special-cases `holder == "user"`) but go
  through the same lease + effect + journal path. Test parity
  verified: `/topic foo` produces a `control_action_applied` event
  identical in shape to an agent's dispatch — only `applier_id`
  differs (`"user"` vs the participant id).
- **Event-plane taxonomy + `lease_closed` unification** (v0.3 PR 8;
  doctrine P2 / §4). New `EventPlane` enum (`CONVERSATION` / `CONTROL`
  / `EXECUTION`) + `plane_of(event)` helper backed by
  `_KIND_TO_PLANE` (kind-level) and `_CONTROL_TYPE_TO_PLANE`
  (per-control_type override slot — v0.4 tool events land in
  EXECUTION). New event constructors `control_action_proposed`,
  `control_action_applied`, `control_action_denied` (the kernel
  side of PR 9's dispatch) and `lease_closed(reason=...)` (unified
  taxonomy: `released`/`denied`/`expired`/`cancelled`/`aborted`/
  `aborted_validation`). All four added to `CONTROL_TYPES` with
  payload validators. `RoomCoordinator.release_lease` and
  `check_lease_ttl` now emit `lease_closed` alongside the v0.2
  `lease_denied` / `lease_expired` legacy events; one v0.3.x release
  later drops the legacy duplicates. v0.2.x journal lines with
  `lease_denied` / `lease_expired` continue to load cleanly.
- **`policy_slow` / `policy_error` typed constructors** + per-control-
  type payload validator dispatch table (v0.2.1 PR 2, addresses audit
  findings C2 and C4 partial; doctrine P7 foundation). The two
  events that previously emitted via inline `ev._control(...)` calls
  at `coordinator.py:824` and 854 now go through typed
  `ev.policy_slow(...)` / `ev.policy_error(...)` constructors. A
  new `_CONTROL_PAYLOAD_VALIDATORS` dispatch table (seeded with
  validators for the two new constructors) lets future PRs extend
  per-control-type field schemas without touching the kind-level
  validator. `policy_error.message` is now run through
  `redact_error_text` at the kernel boundary, matching the
  scrubbing discipline of `actor_error` / `journal_error`. The full
  registry arrives in v0.3 per doctrine P7 (versioned semantic
  effects).
- **`RoomCoordinator.check_lease_ttl()` + `lease_expired` control event**
  (v0.2.1 PR 1, addresses audit finding D1; doctrine §control-plane).
  The coordinator watchdog now proactively reaps leases past their
  TTL each tick (alongside the existing `check_idle_timeout` call),
  emitting a `lease_expired` control event under `post_internal` for
  every reaped lease. Without this, a lease held while no stream is
  active stayed nominally `valid=True` until something hit
  `validate_lease` from the stream path. Distinct from the existing
  `stream_end.body["status"] == "lease_expired"` signal — see audit
  §11 Q2. Worst-case latency: `watchdog_interval_s + 1` seconds.
- **`Event.schema_version: int = 1` envelope field** (v0.2.1 PR 3,
  addresses audit finding C1; doctrine P7). Every event now carries
  an integer envelope version that round-trips through
  `Event.to_jsonl` / `from_jsonl`. Old v0.2.0 journal lines that
  lack the key load cleanly with the default applied. Body-level
  versions arrive in v0.3 per the typed semantic-effect spec.
- **`Event.causal_refs: tuple = ()` envelope field** (v0.2.1 PR 3,
  addresses audit finding C3; doctrine P11). Reserved slot for the
  v0.3 typed causal graph. Always `()` in v0.2.1; round-trips
  through JSON as a list and is coerced back to `tuple` in
  `__post_init__` so `from_jsonl(to_jsonl(e)) == e` holds.
  `_validate_event_dict` rejects non-list shapes.

### Documentation
- **Lock-discipline doc + structural gate** (v0.3 PR 2; doctrine P4 /
  §2). New companion doc `docs/lock-discipline.md` codifies the rule
  ("the coordinator lock guards cheap operations only; LLM calls,
  tool calls, file/network I/O, and `time.sleep` must NEVER run under
  it") and the naming convention that makes lock-affinity visible at
  the call site (`_apply_*`/`_validate_*`/`_reserve_*` are
  under-lock; `_call_llm`-style entry points assert no lock held).
  Enforced by a new
  `tests/test_kernel_kernel_boundary.py::LockDisciplineBoundary`
  test class (4 structural gates: `_TrackedRLock` exists,
  `_assert_not_holding_lock` exists, `streaming.run_streaming_call`
  invokes the assertion, no `time.sleep` inside any `with ..._lock:`
  block). `RoomCoordinator._lock` is now a `_TrackedRLock` wrapper
  that records the owning thread (stdlib RLock has no portable owner
  query); `coordinator._assert_not_holding_lock(where)` raises
  `RuntimeError` with a grep-able `where` label so I/O-under-lock
  regressions surface at the offending call site.
  `streaming.run_streaming_call` (the canonical long-running entry
  point) calls the assertion as its first statement.
- **v0.3 implementation roadmap published** at
  `docs/internal/study/13-v03-implementation-roadmap.md` (v0.3 PR 0).
  Canonical "where are we in v0.3" reference. Maps each of the 15
  doctrine principles (P1–P15) and 10 subsystem specs (§1–§10) to a
  specific PR; sequences the 13 code PRs into five phases (Foundation /
  Metadata / Domain / Control / Closures) with a hard-constraint DAG;
  records the v0.4-readiness checklist that v0.3-completion will be
  measured against. Closes the v0.2.1 → v0.3 transition: §1 confirms
  the v0.2.1 deferrals (A3, C3 typed, C4 full, D2, D3) each have an
  owning v0.3 PR.
- **Clock-discipline structural gate + companion doc** (v0.2.1 PR 5,
  addresses audit findings B1 and B2; doctrine §timing-discipline).
  New test class
  `tests/test_kernel_kernel_boundary.py::ClockDisciplineBoundary`
  enforces two invariants structurally so a future regression fails
  CI: (a) `time.time()` may appear in `loom/kernel/` ONLY at the
  whitelisted `MessageBus.post` event-ts assignment in `bus.py`,
  (b) `loom/kernel/journal.py` (replay path) contains no
  `time.time()` or `time.monotonic()` call. New companion doc
  `docs/timing-discipline.md` documents the rule and the extension
  protocol for future kernel contributors.
- **v0.2.1 hardening audit published** at
  `docs/internal/study/12-v02-hardening-audit.md`. Gates v0.3
  implementation work: ground-truths the v0.2 kernel against the
  v0.3 doctrine (`11-orchestration-os-doctrine.md`) along four
  named axes (actor cursor semantics, monotonic clocks for
  TTL/watchdog, structured control event schemas, watchdog
  completeness), enumerates 13 findings (A1–A4, B1–B2, C1–C4,
  D1–D3) with file:line citations and severity, and lays out a
  five-PR v0.2.1 sequence (~490 LOC). A3 (cursor persistence),
  D2 (streaming-stall watchdog), and the typed
  `tuple[CausalRef, ...]` form of C3 are deferred to v0.3 per
  doctrine principles P6 / P7 / P11.

### Removed
- **`RoomControlState.floor_owner`**, `RoomState.set_floor_owner()`,
  `RoomCoordinator.set_floor_owner()`, and the `LoomRoom.set_floor()`
  facade method. The field was a soft signal with no kernel-enforced
  semantics — `DefaultPolicy` interpreted it to narrow
  `allowed_speakers` across turns. v0.2 removes the kernel-level
  carrier; equivalent UX patterns:
  - `@<id>` an agent directly each turn for direct narrowing.
  - For persistent narrowing, subclass `DefaultPolicy` and keep the
    narrowed set as policy-internal state.
  - The `floor_updated` control event survives (under its legacy name
    for journal back-compat) and now carries only `wait_for_user`;
    `RoomCoordinator.set_wait_for_user_flag()` replaces the
    floor-aware `set_floor_owner()` call.
- **Console commands `/floor`, `/release`, `/quiet`** were removed
  along with the field they wrote to. The runtime now returns a
  removed-feature notice rather than mutating state. `/who` no longer
  renders a `floor:` line; `/control` no longer renders one either.
- **Snapshot schema dropped `control.floor_owner`** in v5. Older v1-v4
  snapshots carrying the field still load cleanly; the field is
  silently discarded at restore time.
- **`RoomControlState.turn_taking_mode`** and
  **`UserTurnPlan.set_turn_taking_mode`**. Round-robin is now
  signalled by ``state.control.turn_order`` being non-empty; entering
  the mode means setting ``turn_order`` to a non-empty list, and
  exiting means setting it to ``[]``. The `TurnTakingMode` Literal
  type alias and `RoomState.set_turn_taking_mode()` method are also
  gone. Snapshot schema bumped to v5; v3/v4 snapshots carrying a
  ``turn_taking_mode`` field are still loadable — the value is
  discarded at restore time.
  - Migration for downstream policies: replace
    ``plan.set_turn_taking_mode = "round_robin"`` with
    ``plan.set_turn_order = […]``; replace
    ``set_turn_taking_mode = "broadcast"`` with
    ``set_turn_order = []``. To convey "round-robin is active" to
    agents in the system prompt, override
    ``ConversationPolicy.charter_text(state)`` — the default
    implementation already emits a rotation advisory whenever
    ``state.control.turn_order`` is non-empty.

### Added
- **`TriggerPriorityFn` Protocol +
  `RoomConfig.trigger_priority` override**. The actor's trigger
  classification (direct mention → dead-letter/reroute →
  required-obligation user post) is now a hook, with the v0.1.2
  classifier exposed as
  ``loom.kernel.actor.DEFAULT_TRIGGER_PRIORITY``. ``RoomConfig``
  gains a ``trigger_priority`` field (``None`` means "use the
  default"); :func:`loom.kernel.actor.decide` and
  :func:`pick_priority_trigger` both accept the hook via a
  keyword-only ``priority_fn=`` parameter for direct testability.
- **Dedicated coordinator watchdog thread**. ``RoomCoordinator`` now
  spawns a daemon thread (``loom-coord-watchdog``) that fires
  ``check_idle_timeout`` every ``RoomConfig.watchdog_interval_s``
  (default 5.0s). ``LoomSession.start()`` / ``stop()`` wire the
  thread's lifecycle automatically. The existing piggybacked call on
  ``ParticipantActor._loop`` is retained as defense-in-depth — both
  paths are idempotent. Exceptions in the watchdog loop are swallowed
  so a single bad tick cannot crash the thread.
- **`ConversationPolicy.should_post_response(body, state, participant_id)
  -> bool`** veto hook. Called by
  :func:`loom.kernel.streaming.run_streaming_call` AFTER the kernel's
  built-in filters (empty / idle-phrase / IoU loop-guard). Returning
  ``False`` suppresses the commit; returning ``True`` lets it
  proceed. Default ``True`` — the kernel filters are unchanged.
  Useful for policy-specific veto rules (semantic similarity,
  off-topic detection, custom rate limits). Buggy hooks that raise
  are treated as ``True`` so a bad policy cannot drop a legitimate
  response.
- **`PromptSection` dataclass + `ConversationPolicy.prompt_sections()`
  hook**. Policies can inject named sections into the system preamble
  immediately after the kernel charter, persona, topic, participant
  id, capabilities, and the legacy `system_prompt` / `role_prompt`
  blocks. Each section renders with an uppercase ``<<<NAME>>>``
  header so prompt diffs are attributable. Default returns ``[]`` —
  bundled policies emit no extra sections. Empty-text sections are
  silently skipped; a buggy hook that raises is caught and skipped so
  ``build_prompt`` never breaks on user error.
- **`LeaseCheck` Protocol + `LeaseCheckResult` NamedTuple** in
  `loom.contracts`. `RoomConfig.lease_checks: tuple[LeaseCheck, ...]`
  defaults to the empty tuple ("use the kernel's built-in 8-step
  chain"); passing a non-empty tuple lets advanced consumers prepend,
  append, or replace gates. The eight default checks
  (`open_turn` → `participant_registered` →
  `participant_active` → `allowed_speaker` →
  `per_participant_cap` → `max_responses` →
  `throttle` → `budget`) ship as `DEFAULT_LEASE_CHECKS` in
  `loom.kernel.coordinator`. Behavior is identical to v0.1.2 —
  rejections were previously silent ``return None``.
- **`lease_denied` control event**. Every rejected
  `acquire_lease` call now emits this event with
  `holder`, `check_name`, `deny_reason`, and `trigger_event_id`.
  Default ``deny_reason`` strings: `"no_open_user_turn"`,
  `"unknown_participant"`, `"participant_inactive"`,
  `"not_in_allowed_speakers"`, `"no_obligation"`,
  `"speaker_cap_reached"`, `"max_responses_reached"`,
  `"throttle_exceeded"`, `"budget_exceeded"`. Buggy custom checks
  that raise emit `"check_raised:<ExceptionClass>"`.
- **`ConversationPolicy.dead_letter_target(state, removed_participant) ->
  pid | None`** hook. Called when a participant is removed mid-turn
  and outstanding @-mentions need a fallback. The default
  implementation preserves v0.1.2 kernel behavior
  (configured ``default_responder_id`` → cheapest active capable).
  Returning ``None`` emits the ``dead_letter`` event with
  ``reroute_to=None``, dropping the mention. Buggy hook
  implementations that raise fall back to the kernel default. To
  wire a custom policy onto this code path, construct
  ``RoomCoordinator(..., policy=my_policy)`` — the runtime layer
  does this automatically.
- **`ConversationPolicy.charter_text(state_view) -> str`** hook
  rendered in the system preamble immediately after the kernel charter
  (`LOOM_PROTOCOL_INSTRUCTIONS`) and BEFORE persona / participant id /
  topic. Default emits a one-line round-robin advisory when
  ``state.control.turn_order`` is non-empty. Use to describe
  policy-specific behavioral rules that should sit alongside the
  protocol-level rules. The kernel charter is unconditional — policy
  text cannot precede or replace it (preserving invariant 5).
- **`SecretShape` Protocol + `register_secret_shape()` API** in
  `loom.kernel.events`. Each of the seven default secret detectors
  (OpenAI/Anthropic `sk-` and `sk-ant-`, Bearer, AWS access key,
  JWT, GCP `AIza`, GCP `ya29` OAuth) is now a named
  `SecretShape` object (`_RegexShape`) with a `.detect(text) ->
  Iterable[(start, end)]` method. Adapters can register new
  shape-based detectors without monkey-patching the kernel; the
  legacy `register_secret_scrubber` callable API continues to work
  unchanged and runs AFTER all `SecretShape` detectors.

### Security
- **`MessageBus.post_internal` now requires a `_KERNEL_AUTH` token**
  (`auth=` keyword-only). The token is a module-private sentinel
  defined in `loom.kernel.bus` and is never re-exported from
  `loom`. Identity is checked at the call boundary so a separately
  constructed `_KernelAuth()` does not unlock the method. This
  promotes the previously convention-based "kernel-internal callers
  only" rule into a structural guarantee — the kernel/policy import
  boundary already prevents policy code from reaching the token. A
  new boundary test asserts that no `loom.policy.*` module
  references `_KERNEL_AUTH` or `_KernelAuth`.

### Changed
- **Bus subscriber fan-out runs OUTSIDE the bus lock**. The append +
  `notify_all` are still protected (preserves `ev.id == position`),
  but each subscriber callback runs after the lock is released so a
  slow subscriber cannot freeze readers (`snapshot`, `get`, `len`)
  or other writers waiting on the lock. Subscribers see a snapshot
  of the subscribers tuple captured at lock-release time, so
  concurrent subscribe/unsubscribe is safe. Across-subscriber
  ordering is relaxed: a subscriber may observe event N before
  another subscriber observes event N-1, but each subscriber still
  sees events in append order.
- **`RoomStateView.participants`** now yields `ParticipantInfoView`
  instances (`@dataclass(frozen=True)` mirroring `ParticipantInfo`'s
  five fields, with `role_hints` wrapped in `MappingProxyType`). The
  previously documented soft leak — a policy capturing a participant
  entry and writing `info.active = False` — now raises
  `FrozenInstanceError`.
- **`RoomState.view()` participants are snapshotted at call time**.
  Prior versions exposed a live `MappingProxyType` over the
  underlying dict; now each entry is materialized as a frozen
  `ParticipantInfoView` at the moment `view()` is called. Adding or
  removing a participant after `view()` is no longer visible through
  that view — callers must invoke `view()` again to see new
  membership. Top-level scalar fields (`room_epoch`, slot ids) were
  already snapshot fields on the frozen `RoomStateView`.

## [0.1.2] — 2026-05-08

First public release.

### Added
- **`LoomRoom`** facade: `post`, `post_and_wait`, `add_agent`,
  `remove_agent`, `run_console`, context-manager start/stop.
- **Bundled policies**: `DefaultPolicy` (floor-aware classifier with
  vocative + game-start detection), `OpenChatPolicy`,
  `SingleResponderPolicy`, `RoundRobinPolicy`.
- **Adapters**: `agent_from_send`, `agent_from_stream`,
  `agent_from_object` for wrapping ordinary callables and clients into
  the `Agent` protocol.
- **Streaming kernel**: bus, coordinator, leases, obligations,
  watchdog, prompt sandbox, throttle, budget primitives.
- **Journal**: append-only `events.jsonl` + advisory
  `room_state.json` snapshot for audit + tooling-grade replay.
- **`max_responses` race fix**: enforced at lease-grant time
  (counts committed drafts plus outstanding valid leases).
- **Dead-letter rerouting**: transfers required obligations to a
  fallback agent (default responder, else cheapest active capable).
- **Tests**: 1170+ tests covering kernel, policies, adapters, race
  conditions, threading, journal, and the policy-purity boundary.

### Known limitations
- No async / off-lock policies.
- No policy state persistence across restart.
- No automatic restart-recovery wiring from the journal.
- `RoomStateView` is shallow at the leaf level. *(Closed in [Unreleased] via `ParticipantInfoView`.)*
- No standalone PyPI package — install from source.

[0.1.2]: https://github.com/hdubey-debug/loom/releases/tag/v0.1.2
