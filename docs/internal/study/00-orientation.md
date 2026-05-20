# 00 — Repo orientation & mental model

This is the entry point of the Loom kernel-modification study curriculum.
The full curriculum, session list, and verification checkpoints live in
`~/.claude/plans/can-you-see-my-zesty-dolphin.md`. This file is what we
internalise in **Session 0** so every later session has the same anchor.

State as of: Loom v0.1.2 (2026-05-08, first public release).

## Files covered

- `README.md` — public-facing intro, 30-second start, mental model
- `docs/README.md` — docs index
- `docs/loom-ux-spec.md` — UX contract for developer audiences
- `docs/security-model.md` — kernel-level threat model + posture
- `docs/writing-a-policy.md` — policy author tutorial
- `docs/writing-an-adapter.md` — adapter author tutorial
- `CHANGELOG.md` — what landed in 0.1.2; known limitations
- `CONTRIBUTING.md` — setup, test commands, "where to start"
- `pyproject.toml` — Python ≥3.11, **zero runtime deps**, 98% branch
  coverage gate, mutmut + ruff + mypy + hypothesis dev tooling
- `Makefile` — 15 named targets across test / bench / security / ux tiers
- `.github/workflows/ci.yml` — Python 3.11 + 3.12 matrix; ruff lint +
  format check, mypy, pytest -q --maxfail=5

## Mental model

```
        USER (program / REPL)
              │
              ▼
       ┌─────────────┐
       │  LoomRoom   │  loom/room.py (686 LOC) — public facade
       │   (facade)  │  post / post_and_wait / add_agent / set_topic / …
       └──────┬──────┘
              │ 1 : 1
              ▼
       ┌─────────────┐
       │ LoomSession │  loom/runtime.py — built by build_loom_session()
       │   (wiring)  │  owns: bus, state, coordinator, journal, actors
       └──┬─────┬─┬──┘
          │     │ │
   ┌──────┘     │ └─────────────┐
   ▼            ▼               ▼
┌───────┐  ┌────────┐    ┌────────────┐
│KERNEL │  │ POLICY │    │   AGENTS   │
│       │  │  (pure │    │  (your LLM │
│bus,   │◄─┤  plan) │    │   clients) │
│coord, │  └────────┘    └────────────┘
│state, │           ▲           ▲
│jrnl,  │           │           │
│actors │  reads RoomStateView  │
│       │  via plan_user_turn   │
│  ▲    │                       │
│  └────┼──────── stream() ─────┘
│       │
└───────┘
        events.jsonl  ← append-only ledger (audit + future replay)
        room_state.json ← advisory snapshot
```

Three **owners**, three responsibilities:

- **Kernel** — *mechanism only*. Owns `RoomState` (mutation), the
  `MessageBus` (single source of truth), leases, obligations, the
  charter, the journal. The only mutator of state.
- **Policy** — *who may speak*. A pure function from
  `(user_event, RoomStateView) → UserTurnPlan`. Read-only. Sub-10ms.
  Errors fail closed by default.
- **Agents** — *what to say*. Anything satisfying the `Agent` Protocol
  (`id: str`, `stream(prompt) -> Iterator[str]`).

The room facade glues them and exposes a small, opinionated API.

## Glossary (terms that recur in every kernel file)

| Term | Definition | Where defined |
|---|---|---|
| **Event** | Immutable record on the bus. Kinds: `chat`, `control`, `stream_start`, `stream_delta`, `stream_end`, `system`, `topic`, `presence`, `summary`. | `loom/kernel/events.py` |
| **Bus** | Append-only thread-safe event log + pub/sub. Only mutator: `post()` (actor) / `post_internal()` (kernel). | `loom/kernel/bus.py` |
| **MessageBus.bind_actor** | Thread-local actor identity binding. `post()` rejects `sender ≠ bound id` with `SenderMismatchError` (P1 hardening). Privileged callers use `post_internal`. | `loom/kernel/bus.py` |
| **Channel** | `"main"` for broadcast, `"dm:<pid>"` for direct messages. Visibility: a recipient on `dm:bob` sees only `main` + `dm:bob` events. | `bus.visible_to` |
| **Participant** | State + identity term. Lives in `RoomState.participants[pid]`. | `loom/kernel/room.py` |
| **Actor** | Kernel-runtime term: the daemon thread driving one participant. `actor_id` only legal inside `loom/kernel/actor.py`. | `loom/kernel/actor.py` |
| **Agent** | Public Protocol — what user passes into `LoomRoom(agents=[...])`. | `loom/contracts.py` |
| **User turn** | Window between two consecutive user posts. Owns a set of obligations and zero-or-more agent drafts. | `loom/kernel/user_turn.py` |
| **UserTurnPlan** | Frozen dataclass returned by `plan_user_turn`. Names required participants, routing case, floor, declarative state mutations (`set_turn_taking_mode`, `set_turn_order`, `advance_turn_pointer`). | `loom/kernel/obligations.py` |
| **Obligation** | A required/should/may response from a specific participant for a specific user turn. Tracked individually; resolved on commit. Levels: `must` / `should` / `may`. | `loom/kernel/obligations.py` |
| **Lease** | Time-bounded permit issued by the coordinator giving an actor exclusive right to draft. Counts toward `max_responses`. | `loom/kernel/coordinator.py:TurnLease` |
| **Charter** | Fixed kernel-owned system-prompt section. Includes PASS protocol, visibility rules, "do not impersonate kernel/system". Cannot be overridden by policy. Always rendered. | `loom/kernel/prompt.py:LOOM_PROTOCOL_INSTRUCTIONS` |
| **PASS protocol** | An agent declines the floor by streaming `[PASS]` (regex `^\s*\[PASS\](\s|$)`) within the first 16 chars (configurable). Stream is suppressed; no chat event posted. | `loom/kernel/streaming.py` |
| **Floor** | Tuple of participant ids permitted to speak. Empty tuple = open floor. Set via `UserTurnPlan.set_floor_owner` or `LoomRoom.set_floor`. | `RoomControlState.floor_owner` |
| **Anchor** | Participant designated as the synthesiser; in `DefaultPolicy`, receives `ANCHOR_SYNTHESIS_INSTRUCTIONS` via `role_prompt`. | `RoomState.anchor_id` |
| **Default responder** | Participant chosen as the fallback target when policy is silent or routing fails. | `RoomState.default_responder_id` |
| **Turn-taking mode** | `"broadcast"` (default), `"round_robin"`, `"closed_floor"`. Set declaratively via plan. | `RoomControlState.turn_taking_mode` |
| **Routing case** | Short string label naming why a turn was opened (e.g. `"direct_mention"`, `"vocative"`, `"multi_opinion"`, `"acknowledgement"`, `"game_start"`, `"closed_floor"`). Recorded in events for audit. | `UserTurnPlan.routing_case` |
| **Dead-letter** | Required obligation whose holder was removed mid-turn. v0.1.2 reroutes to a fallback (default-responder slot, then cheapest active capable); v0.1.0/0.1.1 only emitted a trace event. | `coordinator.py` dead-letter path |
| **Journal** | Append-only `events.jsonl` (authoritative) + `room_state.json` (advisory). v0 audit-only; restart-recovery wiring lands in v0.2. | `loom/kernel/journal.py` |
| **Watchdog** | `policy_slow` control event emitted when `plan_user_turn` exceeds ~100ms. | `coordinator.py` |
| **Charter "fenced field"** | Non-transcript prompt input (topic, persona, capability_block, instruction). Wrapped `<name>...</name>` and the closing tag + `<<<...>>>` markers neutralised. The kernel charter teaches the LLM to treat fenced fields as data, not instructions. | `prompt._render_system_field` |

## Public surface (15 primary + 4 advanced)

From `loom/__init__.py`. Anything outside `__all__` is implementation
detail and may shift between minor versions.

**Primary** (`from loom import …`):
- `LoomRoom` — the facade
- `Agent` — Protocol
- `agent_from_send` / `agent_from_stream` / `agent_from_object` — adapter factories
- `ConversationPolicy` — ABC for custom policies
- `DefaultPolicy` / `OpenChatPolicy` / `SingleResponderPolicy` / `RoundRobinPolicy` — bundled
- `RoomConfig` — boot-time config
- `RoomStateView` — read-only state for policies
- `Message` / `TurnResult` — UX projections (no kernel internals)
- `LoomError` — exception base

**Advanced** (`from loom import …` but documented as power-user):
- `ParticipantWiring` — actor wiring record
- `SendProxyAdapter` — direct streaming proxy
- `build_loom_session` — pre-facade entry
- `run_loom_console` — REPL bootstrap

UX targets enforced by `make ux-check`: ≤12 primary symbols (currently
15; CI threshold is 20), 100% docstring coverage on public `LoomRoom`
methods, lines-to-hello-world ≤5.

## Boundary import surfaces

| Path | Audience | Stability |
|---|---|---|
| `loom.*` | Library author | **Public.** Stable. |
| `loom.policy.*` | Policy author | **Public extension.** |
| `loom.adapters.*` | Adapter author | **Public extension.** |
| `loom.kernel.*` | Internal contributor | **Advanced.** Examples + docs MUST NOT import. CI enforces. |

## Architectural invariants (load-bearing)

These are enforced by `tests/test_kernel_kernel_boundary.py` (5 tests)
plus several property tests. Every kernel modification must preserve
all of them:

1. **Kernel does not import `loom.policy`** — static grep.
2. **Kernel may import `loom.contracts`** — neutral ABC layer (where
   `ConversationPolicy` and `Agent` live).
3. **Policy does not mutate state or post to bus** — static grep for
   `state.add_*`, `state.set_*`, `state.remove_*`, `state.control =`,
   `bus.post(`.
4. **Policy does not import `loom.kernel.coordinator` or `loom.kernel.journal`.**
5. **Policy errors fail closed** — coordinator + throwing policy ⇒
   `policy_error` event + turn closes (default mode).
6. **`build_prompt` always renders the kernel charter** — even with a
   stub policy.
7. **Coordinator is the only mutator of `RoomState`** and the only
   writer to bus's authoritative slots (besides `post_internal` for
   kernel-emitted control events).
8. **`max_responses` enforced at lease-grant time** (v0.1.2 fix; counts
   committed drafts + outstanding valid leases).
9. **Dead-letter rerouting transfers required obligations** (v0.1.2; was
   trace-only in 0.1.0/0.1.1).
10. **Policy is synchronous, deterministic, <10ms typical** — coordinator
    holds its lock across the call. >100ms ⇒ `policy_slow` watchdog event.
11. **`MessageBus.post` authenticates `sender` against thread-local actor
    binding** (P1 hardening). Mismatches raise `SenderMismatchError`.
    `post_internal` is the documented privileged bypass.
12. **Subscribers run synchronously, inline, under the bus lock.** A
    blocking subscriber blocks every actor for that duration. Off-thread
    dispatch with timeout is deferred to v0.1+.

## Security model summary

**Kernel defends against** (already remediated, in code as of 2026-05-08):

- Untrusted LLM-generated content — chat bodies + addressee strings
  walled at transcript layer (charter + whitelist parser at `addressees.py`).
- Tampered/corrupt journal lines — `Event.from_jsonl` per-kind shape
  validation; `journal_corruption` / `journal_truncated` control events
  (T1 / P0.1 / P0.2 / P0.4).
- Tampered snapshot files — `restore_state` defensively coerces scalars,
  skips malformed participants (T2 / P0.3).
- Filesystem-level confidentiality — `events.jsonl` + `room_state.json`
  are 0o600; session_dir is 0o700 (T6 / P0.6).
- Secret leakage via error events — `redact_error_text` at kernel
  boundary (`stream_end`, `actor_error`, `journal_error`). Default
  patterns scrub OpenAI / Bearer / AWS / JWT / Google OAuth shapes.
  Adapters can add provider-specific scrubbers via
  `register_secret_scrubber` (OBS1 / P0.7).
- Prompt injection on non-transcript surfaces — `topic`, `persona`,
  `capability_block`, `instruction` rendered through
  `prompt._render_system_field` with `<name>...</name>` fence; closing
  tag + `<<<...>>>` markers neutralised (PI1 / PI2 / P0.8).
- Sender forgery — `bus.post` checks thread-local actor binding;
  privileged callers (coordinator, runtime, replay) bypass via
  `post_internal` (P1).
- Resource exhaustion — `MessageBus(max_body_bytes=256 KB)`; bounded
  snapshot queue; `snapshot_dropped` event on overflow (P2).

**Kernel does NOT defend against** (deployment owns these):

- API key acquisition / storage / rotation / encryption-at-rest.
- Network attacks (no kernel network surface).
- Encryption at rest (`events.jsonl` is plaintext on disk).
- Multi-tenant isolation (v0 = single-room-per-process).
- Compromised LLM weights, Python-runtime exploits, hardware threats,
  supply-chain compromise.

## Threading model

- Each agent runs on its own daemon thread via `ParticipantActor`. Wakes
  on bus events, evaluates triggers by priority, acquires a coordinator
  lease, streams into the bus.
- `prompt_fn` runs on the main thread (typically `room.run_console`).
- `notify` is called from agent threads concurrently. Default `notify`
  wraps `print` in a module-level lock so streamed output doesn't shear.
- `room.start` / `room.stop` are idempotent and safe.
- `room.add_agent` / `room.remove_agent` serialise through a session
  lock so concurrent membership changes don't race.
- The coordinator holds its own lock across `policy.plan_user_turn` —
  this is why policies must be fast.

## v0.1.2 limitations (the v0.2 work list)

**Status update (2026-05-16)**: the v0.2 refactor (12 PRs, landed
2026-05-10) closed most items below; the v0.2.1 hardening audit
(`12-v02-hardening-audit.md`, landed 2026-05-16, 5 PRs) closed the
remaining latent gaps in lease TTL authority, cursor advance
discipline, event-envelope versioning, and clock discipline. The
list below is annotated with current state.

- No async / off-lock policies. **Deferred to v0.3+** per the
  orchestration-OS doctrine (`11-orchestration-os-doctrine.md`).
- No policy state persistence across restart. **Still v0.2-deferred**;
  the v0.3 doctrine treats it as policy concern, not kernel.
- No automatic restart-recovery from the journal. **Still v0.2-deferred**;
  `build_loom_session` does not call `replay_into` / `restore_state`
  on startup. The journal is clock-agnostic and structurally
  enforced (`docs/timing-discipline.md`) so future replay work has
  a stable foundation.
- `RoomStateView` shallow mutation. **Closed in v0.2**.
- No per-message rate limiting or per-participant cost budgets in the
  public API. **Hooks shipped** in v0.2 (`RoomConfig.lease_checks`);
  facade exposure still deferred.
- Off-thread subscriber dispatch with timeout. **Still deferred** (CON1 / P2.5).
- Hash chain over the journal. **Still deferred** (R1 / P3).
- Stream-delta batching. **Still deferred** (RES6 / P2.4).
- Standalone PyPI package. **Still deferred**.

**v0.2.1 hardening additions** (gating v0.3 work):

- **Lease TTL is authoritative** — proactive watchdog sweep
  reaps leases past TTL and emits a `lease_expired` control event.
  No nominally-valid expired leases (PR 1).
- **All control events have typed constructors in `events.py`** —
  `policy_slow` and `policy_error` were promoted from inline
  `_control(...)` calls; per-control-type validator dispatch table
  seeded (PR 2).
- **Event envelope carries `schema_version` and `causal_refs`** —
  reserved foundations for v0.3 doctrine P7 / P11. Old journals load
  cleanly via defaults (PR 3).
- **Actor cursor advance is dispatch-outcome-aware** — denied
  triggers are re-pended into the replay LRU and gated by a
  `_denied_trigger_ids` set so they retry on eligibility change
  (a fresh user post) without tight-looping on their own
  `lease_denied` emissions. `AgentDecision.considered_event_ids`
  removed (PR 4).
- **Clock discipline is structurally enforced** —
  `ClockDisciplineBoundary` test class rejects any `time.time()`
  call outside the whitelisted `MessageBus.post` event-ts
  assignment, and any `time.*` call in the journal replay path
  (PR 5; see `docs/timing-discipline.md`).

## Test tiers + Makefile targets

| Make target | Tier | Purpose | ~Time |
|---|---|---|---|
| `test` | fast suite + 98% coverage gate | default CI | ~30s |
| `test-quick` | unit only, no perf | inner loop | <30s |
| `test-property` | Hypothesis fuzz | nightly | ~1m |
| `test-coverage` | rare-path tier | quarterly | ~30s |
| `test-full` | fast + repeated + mutation | release | ~2h |
| `bench` | microbench + scenario suite | perf gate | ~3-5m |
| `bench-quick` | scenario suite, smaller axes | smoke | ~30s |
| `bench-micro` | pytest microbench only | perf debug | ~30s |
| `bench-diff` | vs committed baseline | CI gate | seconds |
| `bench-baseline` | capture fresh baseline | manual | ~3-5m |
| `bench-soak` | long-run reliability | release | ~1h |
| `security-test` | property + fuzz security | per-PR | ~30s |
| `security-bench` | adversarial scenarios | per-PR | ~1m |
| `ux-check` | UX contracts + symbol count | per-PR | seconds |
| `lint` | ruff + mypy | per-PR | seconds |

## Cross-references

- depends on: nothing — Session 0 is the entry.
- depended on by: every later session reads this file's invariants and
  glossary as background.

## Verification

> *In 3 sentences, explain what the kernel owns vs what the policy owns
> vs what the room facade owns.*

The **kernel** owns mechanism: the append-only event bus, the mutable
`RoomState`, the coordinator state machine that issues leases and
applies plans, the journal, the prompt sandbox (charter + fenced
non-transcript fields), and the actor threads — it is the only mutator
of state and the only writer of authoritative bus events.

The **policy** owns routing: a single pure synchronous callback
`plan_user_turn(user_event, RoomStateView) → UserTurnPlan` that names
who must respond and declares state transitions on the returned plan
(it cannot import `coordinator` or `journal`, cannot post to the bus,
cannot mutate state directly, and any error fails the turn closed).

The **room facade** (`LoomRoom`) is the public glue: it constructs the
session via `build_loom_session`, exposes a small opinionated surface
(`post`, `post_and_wait`, `set_topic`, `add_agent`, `run_console`),
projects kernel events into user-friendly `Message`/`TurnResult` types,
and serialises membership changes through a session lock — it owns no
mechanism of its own beyond convenience and threading discipline.

## Open questions / things to revisit

1. The `topic` ↔ `active_goal` collapse mentioned in the UX spec
   (§4.4) — confirm whether the v0.1 merge has shipped (CHANGELOG
   doesn't mention it explicitly; need to check `RoomControlState` in
   Session 1).
2. The "deferred D3" item: `LoomSession.bus` / `coordinator` / `journal`
   are public attributes today (acknowledged as a leak). When we modify
   kernel internals, we may want to fix this — flag for Session 7
   (facade dive).
3. `ux-check` says target ≤20 symbols in `loom.__all__`; current count
   is 19 (15 primary + 4 advanced) but spec aspirational target is ≤12
   primary. Worth tracking if our changes add to the surface.
4. The "deep-frozen `RoomStateView`" v0.2 item — the implementation
   shape (probably `ParticipantInfoView` mirroring the `ParticipantInfo`
   shape) will be one of our first concrete change candidates. Revisit
   in Session 1 once we've read `room.py`.
