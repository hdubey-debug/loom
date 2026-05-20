# 12 — v0.2.1 Hardening Audit

**Audit date**: 2026-05-16
**Repo state**: post-v0.2 refactor (12 PRs landed 2026-05-10, ~2,340 LOC)
**Audited against**: `11-orchestration-os-doctrine.md` (frozen 2026-05-16)
**Status**: gating document for v0.3 implementation

---

## 1. Purpose & scope

The v0.3 doctrine in `11-orchestration-os-doctrine.md` reframes Loom
from a conversation kernel into an agent-OS substrate. Several v0.3
principles depend on properties of the v0.2 kernel that have not been
explicitly verified end-to-end:

- **P6** (event-sourced replay) — assumes replay is deterministic and
  clock-agnostic, and that actor state is reconstructible from the
  event stream.
- **P7** (versioned semantic effects) — assumes the event envelope
  carries a version so new effect shapes can land without breaking
  old journals.
- **P11** (causality is a typed graph) — assumes the envelope has a
  `causal_refs` slot reserved so v0.3 PRs add data, not schema.

Before opening v0.3 PR#1, the doctrine requires this audit to
ground-truth the v0.2 code against those assumptions and produce a
**v0.2.1 PR plan** that closes the gaps.

The audit covers four named areas from the doctrine's gating list:

- **A**. Actor cursor semantics.
- **B**. Monotonic clocks for TTL / watchdog.
- **C**. Structured control event schemas.
- **D**. Watchdog completeness.

Plus an **Adjacent gaps log** for anything found outside the four
areas. Scope is the kernel package only (`loom/kernel/*.py`); policy
and runtime layers are out of scope except where they cross the
kernel boundary.

---

## 2. Methodology

Three parallel Explore-agent reports were run on 2026-05-16:

1. **Actor cursor report** — covered `loom/kernel/actor.py`,
   `loom/kernel/bus.py`, `loom/kernel/journal.py`. Surfaced
   findings A1–A4.
2. **Watchdogs + monotonic clocks report** — covered all
   `loom/kernel/*.py` for clock usage; coordinator + actor + streaming
   for watchdog wiring. Surfaced findings B1–B2, D1–D3.
3. **Control event schemas report** — covered `loom/kernel/events.py`
   (30 constructors), coordinator emit sites, journal replay, and
   `CHANGELOG.md` for v0.2 completion check. Surfaced findings C1–C4.

Each finding below carries a fresh `file:line` citation re-derived
during audit-write (2026-05-16 working copy) so any drift between
planning and write-time is caught. Severity ratings:

- **HIGH** — blocks v0.3 PR#1 (must land before doctrine
  implementation starts).
- **MED** — should land in v0.2.1, but a documented workaround exists
  for v0.3 if it slips.
- **LOW** — documentation, structural-gate, or future-proofing only.
- **DOC** — pure documentation.

---

## 3. Pre-audit state — what is already sound

The exploration confirmed several hardening items are already
v0.2-complete. Listing them up front so the audit's scope is clearly
"gap-closure", not "rebuild":

- **`lease_denied` event is fully landed** with `holder`,
  `check_name`, `deny_reason`, `trigger_event_id` —
  `loom/kernel/events.py:692-706` and `CHANGELOG.md:99-107`. PR 7 of
  the v0.2 plan. Default deny strings: `no_open_user_turn`,
  `unknown_participant`, `participant_inactive`,
  `not_in_allowed_speakers`, `no_obligation`, `speaker_cap_reached`,
  `max_responses_reached`, `throttle_exceeded`, `budget_exceeded`;
  buggy custom checks emit `check_raised:<ExceptionClass>`.
- **Watchdog thread exists** — `loom/kernel/coordinator.py:1091-1102`
  (`_watchdog_loop`), with `start_watchdog` / `stop_watchdog` at
  1060 / 1080. Runs every `RoomConfig.watchdog_interval_s` (default
  5s, floor 0.05s). Idempotent start; best-effort tick (exceptions
  swallowed). PR 10 of the v0.2 plan.
- **Monotonic clock used for every TTL / duration computation**.
  Inventory in §5.
- **Replay is clock-agnostic** — `loom/kernel/journal.py:606-633`.
  Events are re-emitted with their original timestamps via
  `bus.post_internal(event, auth=_KERNEL_AUTH)`; no `time.time()` or
  `time.monotonic()` call appears in the replay hot path.
- **Snapshot has schema version** — `SNAPSHOT_VERSION = 5` at
  `loom/kernel/journal.py:73`, with `_SUPPORTED_SNAPSHOT_VERSIONS =
  frozenset({1, 2, 3, 4, 5})` at `journal.py:83` and v1→v5 migration
  shims described in the docstring at lines 75–82.
- **`lease_expired` stream end-status already exists** —
  `StreamEndStatus` literal at `events.py:798-805` includes
  `"lease_expired"`. The status is settable from `streaming.py` when
  reactive validation discovers an expired lease mid-stream. The
  gap is that nothing else *proactively* marks leases expired; see
  D1.

The event stream itself does NOT carry an envelope version
(finding C1). The snapshot versioning does not transfer.

---

## 4. Area A — Actor cursor semantics

**Files**: `loom/kernel/actor.py`.
**Doctrine principles implicated**: P6 (event-sourced replay), P7
(versioned semantic effects), §replay-rules.

### Current behavior (one-iteration trace)

`ParticipantActor._loop()` at `actor.py:332-350` calls
`bus.wait_after(self._cursor, ...)` then `_step_with_error_handling`,
which calls `step()`, which calls `_decide_once()` followed by
`_dispatch_decision()`.

`_decide_once()` at `actor.py:377-406`:

```
snap = bus.snapshot(audience=self.id, since=self._cursor)
snap = [e for e in snap if e.sender != self.id]
...                                              # replay pending mentions
decision = decide(snap, self.id, user_turn, ...) # pure
if snap:
    highest = max(e.id for e in snap)
    if highest > self._cursor:
        self._cursor = highest                   # ADVANCE HERE
_update_pending_mentions(decision, snap)
return decision
```

`_dispatch_decision()` at `actor.py:427-454` then calls
`acquire_lease(...)`. On denial (`lease is None`,
`actor.py:446-450`):

```
self.coordinator.handle_skip(self.id, trigger)
return
```

The cursor has already advanced past the trigger event. The trigger
is *not* added to `_pending_direct_mentions` unless it was a
user-sourced direct mention to this participant
(`actor.py:412-420`); user-sourced mentions to the actor that lose
the lease *are* re-pended, but agent-to-agent triggers and
obligation triggers are not.

### Findings

#### A1 (HIGH) — Cursor advances before dispatch outcome is known

Cursor is bumped to `max(snap)` at `actor.py:402-403` after
`decide()` returns but *before* `_dispatch_decision()` runs. If the
lease is denied at `actor.py:446-450`, the trigger event is lost from
the cursor window — a subsequent eligibility change (throttle reset,
budget release, speaker cap clear) cannot resurrect that trigger via
the wakeup path.

The `_pending_direct_mentions` LRU at `actor.py:282` partially
compensates for user-sourced direct mentions
(`actor.py:412-420`), but obligation-only triggers and rerouted
dead-letter triggers (priorities 2–3 in the docstring at
`actor.py:20-27`) are not re-pended on lease denial.

**Doctrine implication**: P6 requires replay to reconstruct the same
actor decision sequence. Today's replay sees the trigger event and
the eventual lease grant, but cannot tell whether an actor "examined
and was denied" vs. "examined and chose to skip" vs. "did not
examine". The cursor is in-memory only (see A3), so replay always
re-derives, masking the in-flight inconsistency — but as soon as
cursors are persisted (v0.3 P6/P7), the inconsistency surfaces.

**Severity**: HIGH. The lose-the-trigger window exists in production
today; it is rarely observable because most lease denials are
follow-ons of denials the actor would re-encounter on the next user
post, but it is a real latent bug.

**Resolution**: PR 4 — restructure `_decide_once()` to defer cursor
advance until after `_dispatch_decision()` returns. On lease denial,
advance only past events the actor explicitly examined-and-skipped,
leaving the trigger at-or-after the cursor for re-examination.

#### A2 (MED) — `AgentDecision.considered_event_ids` is a phantom field

Defined at `actor.py:56-62`:

```python
@dataclass
class AgentDecision:
    action: DecisionAction
    trigger_event_id: Optional[int]
    considered_event_ids: list[int] = field(default_factory=list)
    reason: str = ""
```

Searched all of `loom/`: the field is populated by `decide(...)` in
`actor.py` but never *read* by the coordinator, the actor, or any
test. The docstring at `actor.py:56-58` says it is "the actor's 'I
have processed these, do not redeliver' cursor advance," which
matches the doctrine's eventual model — but the cursor advance today
(at `actor.py:402-403`) is computed from `snap`, not from
`considered_event_ids`. The field is dead.

**Severity**: MED. Dead fields are a maintenance hazard and confuse
the cursor-discipline conversation in A1/A3 — readers infer there is
a per-event advance protocol when there is not.

**Resolution**: PR 4 — drop the field from `AgentDecision`. (The
alternative — wire it into per-event cursor pruning — is correct
under doctrine P6/P7 but is part of A3, which is too big for v0.2.1.)

#### A3 (MED — deferred to v0.3) — Cursor is not persisted

`self._cursor = -1` at `actor.py:279`. On process restart, every
actor re-scans the entire bus log via `bus.snapshot(audience=self.id,
since=-1)`. The semantics are *safe* (the journal is the source of
truth, replay re-injects committed events with original timestamps),
but:

- Per-actor work in the snapshot is re-done.
- Pending-mention LRU at `actor.py:282` is lost on restart, so direct
  mentions an actor was already throttled on are not re-pended.
- Doctrine P6 (event-sourced replay) requires replay to reconstruct
  examined-but-skipped state. P7 (versioned semantic effects)
  suggests emitting a `cursor_advanced` semantic effect each time
  the cursor moves so replay sees the same skip set as the original
  run.

**Severity**: MED, but **deferred to v0.3** — it requires a new
event type (`cursor_advanced`) reified via doctrine P7, plus a
schema-version foundation (PR 3) and probably the typed `causal_refs`
graph (P11). v0.2.1 is the wrong place.

**Resolution**: explicit defer; tracked in §12 below.

#### A4 (LOW) — Docstring describes cursor inversely to implementation

Docstring at `actor.py:6-12`:

> 1. Reads new events visible to it (`bus.snapshot(audience=self.id,
>    since=cursor)`), filtering out events it sent.
> 2. Picks the highest-priority trigger from the batch …
> 3. Prunes its cursor: events in `considered_event_ids` are not
>    reconsidered; direct mentions to self that were NOT selected as
>    the trigger remain pending in a bounded LRU.

The implementation at `actor.py:402-403` sets `_cursor = max(e.id for
e in snap)` and uses `bus.snapshot(audience=self.id,
since=self._cursor)` at `actor.py:378`. `bus.snapshot(since=cursor)`
returns events with `id > cursor`, so the cursor semantics are
**"highest event id examined,"** not "next event id to read".

The off-by-one is harmless in code (the snapshot call is correctly
strict-greater-than), but the docstring's "prunes its cursor /
`considered_event_ids` … not reconsidered" passage is wrong: there is
no per-event pruning, only a single high-water mark.

**Severity**: LOW.

**Resolution**: PR 4 — fix the module docstring at `actor.py:1-28`
and the `AgentDecision.considered_event_ids` docstring at
`actor.py:56-58` (the latter goes away when the field is dropped).

---

## 5. Area B — Monotonic clocks for TTL / watchdog

**Files**: all `loom/kernel/*.py`.
**Doctrine principle implicated**: §timing-discipline (cross-cutting,
not a numbered principle but invoked by P3.3 / audit TIME1 in the
existing v0.2 codebase).

### Clock-usage inventory (kernel only)

`grep -nE 'time\.(time|monotonic)\(\)' loom/kernel/*.py`:

| File | Line | Call | Purpose | Correct? |
|---|---|---|---|---|
| `bus.py` | 281 | `time.time()` | `ev.ts =` event wall-clock for journal / replay correlation | YES — event metadata is correctly wall-clock |
| `user_turn.py` | 112 | `time.monotonic()` | `last_activity_at` on open | YES |
| `user_turn.py` | 127 | `time.monotonic()` | `last_activity_at` on post-update | YES |
| `user_turn.py` | 164 | `time.monotonic()` | `is_idle()` now-default | YES |
| `user_turn.py` | 233 | `time.monotonic()` | `started_at` default | YES |
| `coordinator.py` | 183 | `time.monotonic()` | `ThrottleConfig.try_consume` now-default | YES |
| `coordinator.py` | 819 | `time.monotonic()` | `_run_classify_watchdog` t0 | YES (duration math) |
| `coordinator.py` | 823 | `time.monotonic()` | error-path elapsed | YES |
| `coordinator.py` | 852 | `time.monotonic()` | happy-path elapsed | YES |
| `coordinator.py` | 900 | `time.monotonic()` | `open_user_turn` debounce now | YES |
| `coordinator.py` | 1224 | `time.monotonic()` | `acquire_lease` `acquired_at` | YES |
| `coordinator.py` | 1250 | `time.monotonic()` | `validate_lease` expiry compare | YES |
| `coordinator.py` | 1340 | `time.monotonic()` | `handle_skip` last_activity_at | YES |

`time.time()` appears in exactly one kernel location: `bus.py:281`,
which is the right semantic (events are correlated to wall-clock for
operator observability and replay debugging).

`time.monotonic()` appears 12 times across `user_turn.py` and
`coordinator.py`, every one of them in a TTL / debounce / elapsed-ms
context. None in `actor.py`, `journal.py`, `bus.py` (other than the
event-ts line), `events.py`, `room.py`, `obligations.py`,
`streaming.py`, `prompt.py`.

### Findings

#### B1 (LOW) — Discipline is correct today but not enforced structurally

Nothing in the test suite prevents a future contributor from sneaking
a `time.time()` into a TTL path. The kernel-boundary tests in
`tests/test_kernel_kernel_boundary.py` enforce other invariants (no
`loom.policy` import in kernel, `bus.post_internal` requires
`_KERNEL_AUTH`, etc.) but do not include a clock-discipline gate.

**Severity**: LOW (no current regression, but a regression in this
area would be silent — NTP-step bugs only manifest under operator
action).

**Resolution**: PR 5 — add a `ClockDisciplineBoundary` test class
that greps the kernel for `time.time()` and asserts only the
whitelisted line at `bus.py:281` appears; greps `journal.py`'s replay
path for any time call and asserts none.

#### B2 (DOC) — Invariant not surfaced in operator-facing docs

The invariant "TTL / duration uses `time.monotonic`; event metadata
uses `time.time()`" is implicit in inline comments
(`coordinator.py:101-107`, `coordinator.py:1222-1223`,
`bus.py:281` neighborhood) but not present in `docs/`. PR reviewers
cannot link to it.

**Severity**: DOC.

**Resolution**: PR 5 — add `docs/timing-discipline.md` (short,
~50 lines): one-paragraph rule, two examples (good / bad), pointer
to the boundary test that enforces it.

---

## 6. Area C — Control event schemas

**Files**: `loom/kernel/events.py`, `loom/kernel/coordinator.py`.
**Doctrine principles implicated**: P7 (versioned semantic effects),
P11 (causality is a typed graph).

### Event catalog (30 constructors)

Constructors in `loom/kernel/events.py` (kind-classified):

**control** (`_control` factory at `events.py:537-544`):

| Constructor | Line | Notes |
|---|---|---|
| `topic_changed` | 547 | |
| `participant_added` | 551 | |
| `participant_removed` | 555 | |
| `user_turn_opened` | 559 | |
| `user_turn_closed` | 597 | |
| `obligation_recorded` | 604 | carries `target_event_ids` |
| `obligation_resolved` | 627 | carries `resolved_by_event_id` |
| `dead_letter` | 644 | carries `original_mention_event_id` |
| `default_responder_changed` | 655 | |
| `roles_assigned` | 659 | |
| `floor_updated` | 670 | back-compat name |
| `style_changed` | 688 | |
| `lease_denied` | 692 | carries `trigger_event_id` |
| `journal_error` | 709 | |
| `actor_error` | 725 | |
| `journal_corruption` | 741 | |
| `journal_truncated` | 764 | |
| `snapshot_dropped` | 779 | |
| `policy_slow` | **inline** | emitted at `coordinator.py:854-862` via `_control(...)` |
| `policy_error` | **inline** | emitted at `coordinator.py:824-833` via `_control(...)` |

(The control types `chair_changed`, `anchor_changed`,
`default_summarizer_changed` are in `CONTROL_TYPES` at
`events.py:490-492` but have no constructor — reserved names with no
v0.2 emitter.)

**stream**:

| Constructor | Line |
|---|---|
| `stream_start` | 808 |
| `stream_delta` | 820 |
| `stream_end` | 828 |

**chat / system / summary**:

| Constructor | Line |
|---|---|
| `chat` | 857 |
| `system` | 879 |
| `summary` | 883 |

### Findings

#### C1 (HIGH) — No envelope `schema_version` field

`Event` dataclass at `events.py:270-308`:

```python
@dataclass(slots=True)
class Event:
    kind: EventKind
    sender: str
    body: Any
    channel: str = "main"
    addressees: list[str] = field(default_factory=list)
    room_epoch: int = 0
    user_turn_id: Optional[int] = None
    meta: dict = field(default_factory=dict)
    id: int = 0
    ts: float = 0.0
```

No version field. `to_jsonl` at `events.py:310-329` round-trips
exactly these fields. `_validate_event_dict` at `events.py:420-468`
validates each field but has no version concept.

The snapshot stream is versioned (`SNAPSHOT_VERSION = 5` at
`journal.py:73`, with `_SUPPORTED_SNAPSHOT_VERSIONS = frozenset({1,
2, 3, 4, 5})` at `journal.py:83`), but the event journal is not.
Adding a required field to any constructor today silently breaks old
journals tomorrow unless every consumer uses `.get(..., default)`.

**Doctrine implication**: P7 (versioned semantic effects) and §replay
discipline both depend on the kernel being able to bump effect
versions independently and on consumers being able to recognize
"old" vs "new" shapes during replay. Today's kernel cannot do either.

**Severity**: HIGH. Every v0.3 PR that adds a field touches this.

**Resolution**: PR 3 — add `schema_version: int = 1` to the
envelope; round-trip in `to_jsonl` / `from_jsonl`; default to 1
when the field is missing from a v0.2.0-shaped line; validate in
`_validate_event_dict`. Envelope-level only — body-level versions
arrive in v0.3 per effect type.

#### C2 (MED) — `policy_slow` / `policy_error` lack typed constructors

`policy_slow` and `policy_error` are listed in `CONTROL_TYPES` at
`events.py:501-502` but emitted inline at the coordinator:

`coordinator.py:824-833` (`policy_error`):

```python
self.bus.post_internal(
    ev._control(
        "policy_error",
        exception_class=type(exc).__name__,
        message=str(exc)[:500],
        elapsed_ms=round(elapsed_ms, 3),
        user_event_id=user_event.id,
    ),
    auth=_KERNEL_AUTH,
)
```

`coordinator.py:854-862` (`policy_slow`):

```python
self.bus.post_internal(
    ev._control(
        "policy_slow",
        elapsed_ms=round(elapsed_ms, 3),
        threshold_ms=_POLICY_SLOW_THRESHOLD_MS,
        user_event_id=user_event.id,
    ),
    auth=_KERNEL_AUTH,
)
```

Every *other* control event has a typed constructor in `events.py`.
The inline pattern bypasses the constructor-level documentation and
field-shape guarantee, and makes a per-control-type validator
dispatch table (see C4) harder to populate.

**Severity**: MED.

**Resolution**: PR 2 — add `policy_slow(*, elapsed_ms, threshold_ms,
user_event_id)` and `policy_error(*, exception_class, message,
elapsed_ms, user_event_id)` constructors; replace inline calls at
`coordinator.py:824-833` and 854-862.

#### C3 (LOW) — No `causal_refs` envelope field

The `Event` envelope at `events.py:270-308` has no field that
expresses inter-event causality. Causality is implicit in journal
order plus three per-body conventions:

- `stream_start.body["trigger_event_id"]` at `events.py:808-817`.
- `lease_denied.body["trigger_event_id"]` at `events.py:692-706`.
- `obligation_recorded.body["target_event_ids"]` at
  `events.py:604-624`.

Doctrine **P11** mandates `causal_refs: tuple[CausalRef, ...]` with
typed relations on every event envelope. Retrofitting in v0.3 will
touch every constructor *unless* the field is reserved on the
envelope in v0.2.1.

**Severity**: LOW (v0.2.1 reserves the field; types arrive in v0.3).

**Resolution**: PR 3 — add `causal_refs: tuple = ()` to the
envelope; round-trip in `to_jsonl` / `from_jsonl`; default to `()`
when the field is missing from a v0.2.0-shaped line; validate
(must be a list/tuple of dicts) in `_validate_event_dict`.

#### C4 (MED) — Validation is kind-aware but not field-schema-aware

`_validate_event_dict` at `events.py:420-468` plus
`_validate_body_for_kind` at `events.py:392-417` check:

- `kind` is one of the registered set.
- `sender`, `channel`, `addressees`, `room_epoch`, `user_turn_id`,
  `meta`, `id`, `ts` have the right Python types.
- For `control`: `body` is a dict with a non-empty `control_type`
  string.
- For `stream`: `body` has `stream_event` in `{start, delta, end}`
  and an int `lease_id`.
- For `chat` / `system` / `summary` / `topic`: `body` is a str.

What is *not* validated: per-control-type field schemas. A
`lease_denied` event missing `holder`, or a `participant_added`
event with `id` set to `None`, deserializes cleanly and only
explodes downstream.

**Doctrine implication**: P7 (versioned semantic effects) wants a
typed effect registry that maps `(control_type, schema_version) →
field schema` so replay can dispatch on the registered shape.
v0.2.1 cannot ship the full registry, but it can seed the dispatch
table.

**Severity**: MED.

**Resolution**: PR 2 — introduce a minimal
`_CONTROL_PAYLOAD_VALIDATORS: dict[str, Callable[[dict], None]]`
dispatch table with entries for `policy_slow` and `policy_error`
(the constructors being added in the same PR). Wire it from
`_validate_body_for_kind` for the `control` branch. Other
control types get a TODO comment pointing at the v0.3 full
registry.

---

## 7. Area D — Watchdog completeness

**Files**: `loom/kernel/coordinator.py`, `loom/kernel/streaming.py`
(referenced).
**Doctrine principles implicated**: §control-plane (authoritative
lease state).

### Watchdog inventory

`RoomCoordinator` watchdog thread at `coordinator.py:1060-1102`:

```python
def _watchdog_loop(self) -> None:
    interval = max(0.05, float(self.config.watchdog_interval_s))
    while not self._watchdog_stop_event.is_set():
        try:
            self.check_idle_timeout()
        except Exception:
            pass
        self._watchdog_stop_event.wait(timeout=interval)
```

The loop calls *only* `check_idle_timeout()` (at
`coordinator.py:1036-1054`). That method closes idle UserTurns; it
does NOT touch leases.

### Lease TTL flow

- **Grant** — `acquire_lease` at `coordinator.py:1224-1232`:
  `expires_at = now + self.config.lease_ttl_s` (monotonic).
- **Reactive check** — `validate_lease` at
  `coordinator.py:1238-1253`: compares `time.monotonic()` to
  `expires_at`; marks `valid=False` and returns False if expired.
  Called from `streaming.py` during mid-stream chunks.
- **Release** — `release_lease` at `coordinator.py:1255-1258`:
  pops the lease.
- **No proactive sweep.** A lease held while no stream is active
  remains in `self._leases` with `valid=True` past its TTL until
  something accesses it.

### Findings

#### D1 (HIGH) — Lease TTL is checked only reactively

The doctrine's §control-plane treats lease state as authoritative —
"a lease is either held or not, observable in `KernelState`, never
lazy". The v0.2 implementation makes lease state **lazy** for the
no-stream case.

Concrete consequence: imagine a participant whose `draft_handler`
acquires a lease, then deadlocks before posting any stream event.
The lease lives forever (until the next grant attempt by the same
holder fails on `per_participant_cap`). No event is emitted; the
operator has nothing to observe.

**Severity**: HIGH for v0.3 (doctrine §control-plane is explicit);
medium-to-high for v0.2 (it has bitten only in adversarial test
runs, but the failure mode is silent).

**Resolution**: PR 1 — add `check_lease_ttl()` to `RoomCoordinator`
near `check_idle_timeout()` (~line 1036). Iterate `self._leases`,
mark any with `expires_at < time.monotonic()` as `valid=False`,
emit a new `lease_expired` control event, drop the lease. Wire
into `_watchdog_loop` alongside `check_idle_timeout()`.

(Note on naming: the existing `StreamEndStatus` literal at
`events.py:798-805` already includes `"lease_expired"` as a stream
end-status. The new event is a separate concern — a control event
emitted when the watchdog discovers a stale lease unattended.
Suggested name: keep `lease_expired` as the control_type since the
stream-end *status* is body content, not a top-level control
event.)

#### D2 (LOW — deferred to v0.3) — No streaming-stall watchdog

If an LLM provider hangs mid-stream and the lease has 50s remaining,
the kernel cannot detect it. The reactive `validate_lease` only
fires when a *new* chunk arrives; a hung stream produces no chunks.

This is a v0.3 concern (depends on off-lock policy execution, still
deferred from v0.2 Session 10 Q2 — see
`docs/internal/study/10-synthesis.md`). v0.2.1 cannot fix it without
restructuring the stream loop.

**Severity**: LOW (v0.2.1); MED (v0.3+).

**Resolution**: explicit defer; tracked in §12 below.

#### D3 (DOC) — `policy_slow` threshold is hard-coded as a kernel constant

`_POLICY_SLOW_THRESHOLD_MS = 100.0` at `coordinator.py:76`. Used at
`coordinator.py:806`, `853`, `858`. The threshold is a policy
observability concern, not a kernel invariant — different policies
may want different thresholds.

Moving it to `RoomConfig` is a v0.3 change (it cuts across the
coordinator / config boundary). The audit notes the coupling.

**Severity**: DOC.

**Resolution**: PR 2 — leave the constant in place but add a docstring
above it explaining the coupling and pointing at the v0.3 plan.

---

## 8. Adjacent gaps log (outside areas A–D)

Items surfaced during exploration that don't fit the four named
areas. Each is noted with a deferral / disposition; none gates v0.3.

- **`default_responder` slot name is hard-coded** in the kernel. The
  string literal `"default_responder"` appears in
  `coordinator.py:79`, `86`, `403`, `673`, `678`, `815`, `836`, `838`
  (the `PolicyErrorMode` Literal + the resolution path). PR 6 of the
  v0.2 plan promoted the slot to a `ConversationPolicy` hook, but
  the *string name* still lives in the kernel as a config value
  (`policy_error_mode`). Disposition: not a gap, by design — the
  kernel needs to dispatch on the operator-chosen mode. No action.

- **`CONTROL_TYPES` reserves three control types with no constructor**:
  `chair_changed`, `anchor_changed`, `default_summarizer_changed` at
  `events.py:490-492`. They predate the v0.2 cleanup and exist only
  for journal back-compat (older sessions may have emitted them).
  Disposition: leave reserved; no harm.

- **`floor_updated` retains its name despite the floor-owner field
  being dropped in v0.2** (`events.py:670-685`,
  `CHANGELOG.md:21-24`). Disposition: by design — journal
  back-compat. No action.

- **`bus.post_internal` allows any kernel-authenticated caller to set
  arbitrary `sender`**. This is intentional (it is the documented
  escape hatch for replay and crash-handler emissions) but the
  doctrine P1 (sender authentication) holds only on `post()`. The
  audit confirms this is correct: replay needs to re-inject events
  with their original senders, and `post_internal` requires the
  `_KERNEL_AUTH` token. Disposition: confirmed correct.

- **`RoomConfig.watchdog_interval_s` default is 5.0s; floor is
  0.05s**. PR 10 of v0.2 set these. The 5.0s default plus the
  default `lease_ttl_s` (verified at `coordinator.py:1232` — read
  from config) means worst-case proactive lease expiry latency is
  `interval + 1` seconds. Disposition: acceptable for v0.2.1; the
  v0.3 doctrine may revisit when distributed-runtime work begins.

---

## 9. Consolidated v0.2.1 PR plan

Five PRs after the audit (PR 0 is the audit itself). All can be
reviewed and merged at one cadence; estimated total effort 3–4 days,
~490 LOC excluding the audit.

### PR 0 — Publish this audit

- **File**: `docs/internal/study/12-v02-hardening-audit.md` (this).
- **No code changes.** Quality gate: doctrine cross-reference must
  cite principle numbers; every finding carries `file:line`.

### PR 1 — Proactive lease TTL sweep (addresses D1)

- Add `check_lease_ttl(self, *, now: Optional[float] = None)` to
  `RoomCoordinator` near `coordinator.py:1036`.
- Wire into `_watchdog_loop` at `coordinator.py:1091-1102`
  alongside `check_idle_timeout()`.
- Add `lease_expired(*, holder, lease_id, trigger_event_id)`
  constructor in `loom/kernel/events.py`; register the control
  type in `CONTROL_TYPES`.
- **Tests**:
  - `tests/test_kernel_coordinator.py:LeaseTTLWatchdog` — grant a
    lease, advance monotonic clock past TTL, run the watchdog,
    assert the lease is expired and a `lease_expired` event was
    emitted.
  - Property: after watchdog sweep, no `lease.valid=True` with
    `expires_at < now`.
- **Risk**: low. Additive; reactive path stays.
- **LOC**: ~80.

### PR 2 — Promote `policy_slow` / `policy_error` to constructors (addresses C2, C4 partial, D3 doc)

- Add typed constructors in `events.py`.
- Replace inline `_control(...)` calls at `coordinator.py:824-833`
  and 854-862.
- Seed `_CONTROL_PAYLOAD_VALIDATORS` dispatch table; wire from
  `_validate_body_for_kind`.
- Add docstring above `_POLICY_SLOW_THRESHOLD_MS` at
  `coordinator.py:76` noting the v0.3 deferral.
- **Tests**:
  - `tests/test_kernel_events.py` — constructor returns
    Event with expected body shape; validator dispatch passes.
  - Property: `is_known_control(ev)` returns True for every
    constructor result.
- **Risk**: low. Mechanical extraction.
- **LOC**: ~60.

### PR 3 — Event envelope `schema_version` + reserved `causal_refs` (addresses C1, C3)

- Extend `Event` dataclass at `events.py:270-308` with
  `schema_version: int = 1` and `causal_refs: tuple = ()`.
- Update `to_jsonl` at `events.py:310-329` and `from_jsonl` at
  `events.py:331-355` to round-trip both fields.
- Update `_EVENT_FIELDS` at `events.py:362-374` and
  `_validate_event_dict` at `events.py:420-468`.
- **Backward-compat**: old `events.jsonl` lines load with
  `schema_version=1` and `causal_refs=()` defaults.
- **Tests**:
  - `tests/test_kernel_events.py:EventEnvelopeVersioning` —
    round-trip with version field; v0.2.0-shaped events load
    as `schema_version=1`.
  - Property: every constructor result has `schema_version >= 1`
    and `causal_refs == ()`.
  - `tests/test_kernel_journal.py` — load a v0.2.0 fixture
    journal; verify replay produces identical state.
- **Risk**: medium. Touches every event constructor (must verify
  default applies). Journal-fixture migration is the main
  validation point.
- **LOC**: ~150 (10 core, ~140 test).

### PR 4 — Actor cursor advance discipline (addresses A1, A2, A4)

- Restructure `_decide_once()` at `actor.py:377-406`: move cursor
  advance to AFTER `_dispatch_decision()` succeeds. On lease denial
  at `actor.py:446-450`, advance the cursor only past events the
  actor explicitly skipped (not past the trigger event itself).
- Drop `considered_event_ids` field from `AgentDecision`
  (`actor.py:60`, 62). Update `decide(...)` callers.
- Fix module docstring at `actor.py:1-28` to match implementation
  ("highest event id examined" / no per-event pruning).
- **Tests**:
  - `tests/test_kernel_actor.py:CursorAdvanceOnDeny` — actor
    examines trigger, lease denied, cursor stays at
    `trigger.id - 1`; next eligibility change re-triggers.
  - Existing tests pass unchanged (no behavior change for the
    happy path).
- **Risk**: medium. Subtle change to a hot path. Verify no
  infinite re-examination of the same trigger.
- **LOC**: ~120.
- **Note**: A3 (cursor persistence) is **deferred to v0.3** per
  doctrine P6/P7. PR 4 leaves the cursor in-memory.

### PR 5 — Clock-discipline structural gate (addresses B1, B2)

- Add `ClockDisciplineBoundary` test class to
  `tests/test_kernel_kernel_boundary.py`:
  - Grep `loom/kernel/*.py` for `time.time()`; assert only the
    whitelisted line at `bus.py:281` (event-ts assignment) appears.
  - Grep `loom/kernel/journal.py:606-633` (replay path) for any
    `time.` call; assert none.
- Add `docs/timing-discipline.md` (new, ~50 lines).
- **Tests**: the new boundary test IS the test.
- **Risk**: low. Test-only change + doc.
- **LOC**: ~80 test + ~50 doc.

### Sequencing graph

```
PR 0 — Publish audit document             [blocks all below]
  │
  ├─→ PR 3 — Schema-version envelope       [touches every event]
  │     │
  │     ├─→ PR 1 — Lease TTL watchdog      [emits new control event]
  │     └─→ PR 2 — Constructor extraction  [emits constructors w/ v1]
  │
  ├─→ PR 4 — Cursor advance discipline     [independent of PR 3]
  │
  └─→ PR 5 — Clock-discipline gate         [merge last; full surface]
```

PR 3 lands before PRs 1 and 2 are CHANGELOG-finalized so their new
constructors emit events with `schema_version=1`. PR 4 is
independent — it touches `actor.py` only. PR 5 merges last so the
boundary test reflects the final clock-call surface.

---

## 10. v0.3-readiness gate

After all five PRs land, the following checklist gates v0.3 PR#1.
Each item maps to a doctrine principle:

- [ ] **Audit document published and reviewed**
      (this file — gates the workflow).
- [ ] **Lease TTL is authoritative** — no nominally-valid expired
      leases. (Doctrine §control-plane.)
- [ ] **All control events have typed constructors** in `events.py`;
      no inline `_control(...)` emissions from outside `events.py`.
      (Doctrine P7 foundation.)
- [ ] **Event envelope carries `schema_version` and `causal_refs`**
      fields, both round-trip-clean. (Doctrine P7, P11.)
- [ ] **Cursor advance is dispatch-outcome-aware** — no trigger
      lost on lease denial. (Doctrine P6 foundation.)
- [ ] **Clock discipline is structurally enforced** — boundary test
      green. (Doctrine §timing-discipline.)
- [ ] **CHANGELOG `[v0.2.1]` section finalized**.
- [ ] **`docs/internal/study/00-orientation.md` v0.1.2-limits table
      updated** (`00-orientation.md:221` — `## v0.1.2 limitations
      (the v0.2 work list)`) to reflect the hardened state.

Items explicitly **deferred to v0.3** (tracked in §12):

- Cursor persistence via `cursor_advanced` events (A3).
- Streaming-stall watchdog (D2).
- Typing `causal_refs` to `tuple[CausalRef, ...]` (C3 — envelope
  reserved here; types in v0.3 per doctrine P11).
- Per-control-type effect-version registry (C4 partial — dispatch
  table seeded in PR 2; full registry in v0.3 per doctrine P7).
- Per-policy `policy_slow` threshold (D3 — kernel constant in
  v0.2.1; `RoomConfig` field in v0.3).

---

## 11. Verification questions + answers

**Q1**: Why does the v0.2.1 audit not propose persisting actor
cursors today, when the v0.3 doctrine clearly requires it?

**A1**: A3 requires three foundations that v0.2.1 doesn't ship in
order: (1) envelope schema versioning (PR 3 here), (2) a new
`cursor_advanced` control event reified as a *semantic effect* per
doctrine P7, and (3) the typed `causal_refs` graph per doctrine P11
so the event's relation to the examined-event set is recorded
typed-not-positional. Trying to land cursor persistence under v0.2.1
would either ship an untyped version (forcing a re-migration in
v0.3) or ship all three foundations at once (which is the v0.3 work
plan). The audit defers explicitly rather than half-shipping.

**Q2**: Why is the `lease_expired` *control* event introduced in
PR 1 separate from the existing `lease_expired` *stream end-status*
at `events.py:798-805`?

**A2**: They model different things. The stream end-status carries
"the stream's terminal disposition was that the lease ran out
mid-stream" — body content on a stream-end event. The control event
in PR 1 carries "the watchdog discovered an unattended lease past
TTL and reaped it" — a kernel-emitted control plane signal with no
corresponding stream lifecycle. Conflating them would conflate the
stream plane (P2) and the control plane (P2), violating the
doctrine's plane-separation invariant.

**Q3**: PR 3 adds two fields with defaults. Why is that
backward-compatible — won't every existing test that asserts on
`Event(...)` field count or `to_jsonl` output break?

**A3**: Two checks. (1) Defaulted dataclass fields are positional-
compatible: existing `Event(kind=…, sender=…, body=…)` keyword
calls still work. (2) `to_jsonl`'s output gains two keys but no
existing test asserts the dict has *only* the documented keys;
the round-trip tests at `tests/test_kernel_events.py` assert
`from_jsonl(to_jsonl(e)) == e`, which holds for the augmented
shape because `__eq__` on a slots-dataclass compares declared
fields. Old `events.jsonl` lines without the new keys load
cleanly via `dict.get(..., default)` paths added in PR 3.

**Q4**: PR 4 removes a public-looking dataclass field
(`AgentDecision.considered_event_ids`). Is that a breaking change?

**A4**: `AgentDecision` is in `loom.kernel.actor`, which is a
kernel-internal module — not re-exported from `loom/__init__.py`.
Policy authors interact with `loom.contracts` and `loom.adapters`,
not the actor dataclass. The field has zero call sites outside the
kernel (verified via grep). It is a clean removal, mirroring v0.2's
removal of `RoomControlState.floor_owner` (CHANGELOG `[Unreleased]
§Removed`).

**Q5**: PR 5's grep-based test will break if a future contributor
adds a `time.time()` call in a perfectly legitimate context that
isn't `bus.py:281`. Isn't that brittle?

**A5**: Brittleness is the *point*. The test forces the contributor
to either (a) move the call to monotonic (the safe default) or
(b) extend the whitelist with a comment explaining why wall-clock is
required at the new site. Either outcome lands a reviewable
decision. The boundary test class is the same pattern as the
existing `tests/test_kernel_kernel_boundary.py` "no `loom.policy`
import in kernel" check.

---

## 12. Open questions deferred to v0.3+

Tracked here so the v0.3 plan picks them up explicitly:

| ID | Topic | Doctrine principle | Why deferred |
|---|---|---|---|
| A3 | Cursor persistence via `cursor_advanced` events | P6, P7 | Needs schema_version (PR 3) + typed effect + typed `causal_refs` (P11). Atomic v0.3 work. |
| C3 (typed) | `causal_refs: tuple[CausalRef, ...]` typed graph | P11 | Envelope reserved in PR 3; types depend on the v0.3 CausalRef dataclass shape, which is still under design (Part IV of the doctrine). |
| C4 (full) | Per-`(control_type, schema_version)` effect-version registry | P7 | Full registry depends on the v0.3 typed-effect spec. Dispatch table is seeded in PR 2 so the v0.3 PR adds entries, not infrastructure. |
| D2 | Streaming-stall watchdog | §control-plane, off-lock policy | Depends on off-lock policy execution (still deferred from v0.2 Session 10 Q2). Can land alongside the v0.3 streaming refactor. |
| D3 (policy) | Per-policy `policy_slow` threshold (move `_POLICY_SLOW_THRESHOLD_MS` to `RoomConfig`) | n/a (ergonomics) | Crosses the coordinator / config boundary; cleaner as a v0.3 RoomConfig pass. |

---

## 13. Cross-references

- **Doctrine**: `docs/internal/study/11-orchestration-os-doctrine.md`
  — principles P6, P7, P11; §control-plane; §replay-rules;
  §timing-discipline.
- **v0.2 refactor plan**:
  `~/.claude/plans/can-you-see-my-zesty-dolphin.md` (now superseded
  by the v0.2.1 plan in the same file).
- **Synthesis (invariant index)**:
  `docs/internal/study/10-synthesis.md` — 202 invariants; the
  cursor / monotonic-clock / lease-TTL invariants used here cite
  the index identifiers (TIME1, P3.3).
- **v0.2 CHANGELOG**: `CHANGELOG.md` `[Unreleased]` — confirms the
  v0.2 surface (lease_denied, watchdog thread, LeaseCheck protocol,
  monotonic-clock discipline).
- **Boundary tests**: `tests/test_kernel_kernel_boundary.py` — PR 5
  adds the `ClockDisciplineBoundary` class here.

---

## 14. Files covered

Kernel modules audited (read end-to-end during the planning
Explore-agent runs):

- `loom/kernel/actor.py` (463 LOC) — full read.
- `loom/kernel/coordinator.py` (1353 LOC) — full read with focus on
  watchdog (1060-1102), lease (1224-1258), policy watchdog
  (819-863).
- `loom/kernel/events.py` (936 LOC) — full read; 30 constructor
  inventory.
- `loom/kernel/journal.py` (767 LOC) — focused read on replay
  (606-633), snapshot version (73-83).
- `loom/kernel/bus.py` (530 LOC) — focused read on event-ts
  assignment (281) and `post_internal` boundary.
- `loom/kernel/user_turn.py` — full clock-usage inventory only.
- `loom/kernel/room.py`, `obligations.py`, `streaming.py`,
  `prompt.py` — clock-usage grep only.

Test modules referenced (not modified by PR 0):

- `tests/test_kernel_actor.py` (PR 4 extension target).
- `tests/test_kernel_coordinator.py` (PR 1 extension target).
- `tests/test_kernel_events.py` (PRs 2 and 3 extension target).
- `tests/test_kernel_journal.py` (PR 3 backward-compat fixture).
- `tests/test_kernel_kernel_boundary.py` (PR 5 extension target).

Documentation referenced:

- `CHANGELOG.md` — `[Unreleased]` confirms v0.2 surface; PR 0
  through PR 5 each add `[v0.2.1]` entries.
- `docs/internal/study/00-orientation.md:221` — `## v0.1.2
  limitations (the v0.2 work list)`; updated after PR 5 lands per
  the §10 gate.
- `docs/internal/study/11-orchestration-os-doctrine.md` — gating
  doctrine cited throughout.
