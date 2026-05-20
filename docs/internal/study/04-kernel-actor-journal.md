# 04 — Actor + journal

This is **Session 4** of the Loom kernel deep-study curriculum.
`actor.py` is the per-participant concurrency loop — one daemon thread
per agent, the canonical bus consumer + coordinator-lease driver.
`journal.py` is the persistence layer — append-only `events.jsonl`
(authoritative) plus advisory `room_state.json` snapshots, with a
defensive replay path that surfaces corruption rather than swallowing
it.

State as of Loom v0.1.2 (2026-05-08).

## Files covered

| File | LOC | Role | Imports from kernel |
|---|---:|---|---|
| `loom/kernel/actor.py` | 419 | `ParticipantActor`, `decide`, `pick_priority_trigger`, `_trigger_priority`, `AgentDecision` | `events`, `bus`, `coordinator`, `user_turn` |
| `loom/kernel/journal.py` | 777 | `Journal`, `restore_state`, `_coerce_int`, `_coerce_str_or_none`, `SNAPSHOT_VERSION=4` | `events`, `room` |

`actor.py` is the **first kernel file that imports `coordinator` at
runtime** (not just for TYPE_CHECKING). The cycle is broken because
`coordinator.py` only forward-references `ParticipantActor` via
parameter passing — it never imports `actor.py`.

## Mental model

```
                  ┌─────────────────────────────────────┐
                  │   bus._cond.notify_all()             │
                  └────────────┬─────────────────────────┘
                               │ wakes
       ┌───────────────────────┼───────────────────────┐
       │ ParticipantActor("a")  │ ParticipantActor("b") │
       │  _loop() on its own    │  _loop() on its own  │
       │  daemon thread         │  daemon thread        │
       │                        │                       │
       │  bus.wait_after(cur)   │  bus.wait_after(cur)  │
       │  bus.snapshot(         │  bus.snapshot(        │
       │    audience="a",       │    audience="b",      │
       │    since=cursor)       │    since=cursor)      │
       │  filter sender==me     │  filter sender==me    │
       │  prepend pending LRU   │  prepend pending LRU  │
       │  decide() → DRAFT|SKIP │  decide() → DRAFT|SKIP│
       │  advance cursor        │  advance cursor       │
       │                        │                       │
       │  if SKIP: handle_skip  │  if SKIP: handle_skip │
       │  if DRAFT:             │  if DRAFT:            │
       │    acquire_lease()     │    acquire_lease()    │
       │    if lease is None:   │    if lease is None:  │
       │      handle_skip       │      handle_skip      │
       │    else:               │    else:              │
       │      draft_handler ────┼──── streaming.run_streaming_call
       │      release_lease     │      release_lease    │
       └────────────────────────┴───────────────────────┘
                                                 │
                                                 ▼ (every bus.post)
                ┌──────────────────────────────────────────────┐
                │   Journal.on_event subscriber                │
                │   (runs INLINE under bus lock — Session 2)   │
                │   - append "{event}\n" to events.jsonl       │
                │   - on OSError: flip degraded, fire callback │
                │   - if event_count % snapshot_every_events:  │
                │       cb = snapshot_due_cb()  (cheap, builds │
                │            dict on poster thread)            │
                │       _snapshot_queue.put_nowait(payload)    │
                │            (drop oldest on overflow,         │
                │             fire snapshot_drop_callback)     │
                └──────────────────────────────────────────────┘
                                                 │
                                                 ▼ (off-thread)
                ┌──────────────────────────────────────────────┐
                │   _snapshot_loop daemon thread               │
                │   while True:                                │
                │     payload = _snapshot_queue.get()          │
                │     if payload is None: return  (sentinel)   │
                │     _write_snapshot_dict(payload)            │
                │     - tmp file with 0o600                    │
                │     - json.dump → fsync → os.replace         │
                │     - on OSError: flip degraded, fire cb     │
                └──────────────────────────────────────────────┘
```

The bus has no notion of "an actor" — it just has subscribers and a
log. Actors are completely external to the bus's contract; they're
just well-behaved threads that happen to read+post via the standard
API and bind their thread identity for sender authentication. The
journal is just another subscriber, but with one twist: it produces
snapshots via a callback the coordinator registers, and writes them
off-thread via a bounded queue.

---

## actor.py — full reference

### Module-level

- `DecisionAction = Literal["SKIP", "DRAFT"]`
- `DraftHandler = Callable[["ParticipantActor", Event, TurnLease], None]`
  — the callback the actor invokes after acquiring a lease. In
  production, `make_default_draft_handler` from
  `streaming.py` (Session 3) supplies this; tests substitute mocks.

### `class AgentDecision` (dataclass)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `action` | `DecisionAction` | required | `"SKIP"` (no draft) or `"DRAFT"` (acquire lease + draft). |
| `trigger_event_id` | `Optional[int]` | `None` | The event id that motivated the decision. May be set even for `"SKIP"` (recorded for diagnostics). |
| `considered_event_ids` | `list[int]` | `[]` | Cursor-advance hint: events in this list have been processed. |
| `reason` | `str` | `""` | One of: `"empty batch"`, `"no open user_turn"`, `"no actionable trigger"`, `"direct_mention"`, `"dead_letter_rerouted"`, `"obligation"`, `"not_eligible"`. |

### Trigger priority — `_trigger_priority(event, my_id, user_turn)`

Returns priority class (lower = higher priority) or `None` (not actionable):

| Class | Condition |
|---|---|
| **1** (highest) | `chat` event from `"user"` with `my_id in event.addressees` — direct user @-mention |
| **2** | `control` event with `control_type=="dead_letter"` and `body["reroute_to"]==my_id` — dead-letter rerouted to me |
| **2** (tied) | `control` event with `control_type=="obligation_recorded"`, `body["participant_id"]==my_id`, AND `body["reason"]` starts with `"rerouted_from_"` — required obligation transferred to me on participant removal (the `_transfer_required_obligations_locked` path in coordinator) |
| **3** | `chat` from `"user"` AND (`event.id == user_turn.user_event_id` OR `event.id in user_turn.debounced_event_ids`) AND `user_turn.obligation_for(my_id) is not None` — required for current turn |
| `None` | none of the above |

**Critical**: Agent-to-agent `@`-mentions are **intentionally NOT** a
top-priority trigger here. Inter-agent addressing flows through the
coordinator's `allowed_speakers` gate via the obligation/allowed path
in `acquire_lease`. Without that, agent-to-agent chains would loop
without `max_responses` enforcement.

### `pick_priority_trigger(events, my_id, user_turn) -> Optional[Event]`

Sort key: `(priority_class_asc, -event.id)`. Tie-break: **newest event
wins** (highest id) inside a priority class. Returns `None` if no
event has a priority.

### `decide(events, my_id, user_turn) -> AgentDecision` — pure

The decision function. **No mutation, no I/O.** Suitable for direct
unit testing.

```python
considered = [e.id for e in events]

if not events or user_turn is None:
    return AgentDecision(action="SKIP", trigger_event_id=None,
                         considered_event_ids=considered,
                         reason="empty batch" or "no open user_turn")

trigger = pick_priority_trigger(events, my_id, user_turn)
if trigger is None:
    return AgentDecision(action="SKIP", ...,
                         reason="no actionable trigger")

is_direct      = (chat from "user" with my_id in addressees)
is_dead_letter = (control with control_type=="dead_letter")
has_obligation = user_turn.obligation_for(my_id) is not None

if is_direct       → DRAFT (reason="direct_mention")
if is_dead_letter  → DRAFT (reason="dead_letter_rerouted")
if has_obligation  → DRAFT (reason="obligation")
                   → SKIP   (reason="not_eligible")
```

### `class ParticipantActor`

#### `__init__(participant_id, bus, coordinator, draft_handler, *, wakeup_timeout_s=None, pending_mention_capacity=100)`

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | `str` | required | Participant id this actor represents. |
| `bus` | `MessageBus` | required | Shared bus. |
| `coordinator` | `RoomCoordinator` | required | Shared coordinator. |
| `draft_handler` | `DraftHandler` | required | Callback invoked after lease grant. |
| `wakeup_timeout_s` | `Optional[float]` | `min(idle_timeout, lease_ttl)` | The `bus.wait_after` timeout. Defaults to the smaller of `RoomConfig.user_turn_idle_timeout_s` (default 20) and `lease_ttl_s` (default 60). |
| `pending_mention_capacity` | `int` | `100` | LRU bound on `_pending_direct_mentions`. |
| `_cursor` | `int` | `-1` | Bus cursor; advances forward only. |
| `_stopped` | `threading.Event` | new | Signals exit. |
| `_thread` | `Optional[Thread]` | `None` | The daemon thread. |
| `_pending_direct_mentions` | `deque[int]` | `maxlen=pending_mention_capacity` | LRU of user-sourced direct mentions seen but not yet drafted-for. |

#### Lifecycle

- **`start()`** — idempotent. Spawns daemon thread named
  `f"loom-actor-{self.id}"`.
- **`stop(*, timeout=1.0)`** — sets `_stopped`, joins with timeout.
  Idempotent. Safe from any thread.
- **`stopped`** (property) — `_stopped.is_set()`.

#### Per-iteration

- **`step() -> AgentDecision`** — synchronous one-shot. `_decide_once()`
  then `_dispatch_decision()`. **Useful in tests**: drive the actor
  without a thread.

#### Main loop — `_loop()`

```python
unbind = self.bus.bind_actor(self.id)        # P1 sender authentication
try:
    while not self._stopped.is_set():
        new_len = self.bus.wait_after(self._cursor, timeout=wakeup_timeout_s)
        if self._stopped.is_set() or self.bus.stopped:
            return
        if new_len <= self._cursor + 1:
            self.coordinator.check_idle_timeout()  # timeout hit; nudge coord
            continue
        self._step_with_error_handling()
finally:
    unbind()                                   # always release binding
```

Notes:

- **`bind_actor` is called BEFORE the loop**, not on every iteration.
  The unbind handle is invoked in `finally` so a stopped or crashed
  actor doesn't leak a binding (Session 2 invariant 25).
- **Timeout-driven idle check**: when `wait_after` returns because of
  the timeout (no new events), the loop calls
  `coordinator.check_idle_timeout()`. This is how idle-turn-closure
  fires even when no actor is producing events.
- **`bus.stopped` is checked after wakeup** — if the room shut down,
  exit cleanly.
- **No retries on the loop body**; errors are caught in
  `_step_with_error_handling`.

#### Error wrapping — `_step_with_error_handling()`

```python
try:
    self.step()
except Exception as exc:
    try:
        self.bus.post_internal(ev.actor_error(
            participant_id=self.id,
            exception_class=type(exc).__name__,
            message=str(exc)[:500],
        ))
    except Exception:
        pass
```

- Uses `post_internal` because `actor_error` carries `sender="system"`
  but we're on the actor's bound thread, so a regular `post` would
  raise `SenderMismatchError` (Session 2 invariant; this is one of the
  five documented `post_internal` call sites).
- The `actor_error` event's message is **NOT** scrubbed here — the
  factory `ev.actor_error` already runs `redact_error_text` on it
  (Session 1).
- Outer `try` swallows callback failures so the loop continues; an
  actor that can't even post a diagnostic still keeps running.

#### Decision pipeline — `_decide_once()`

```python
snap = self.bus.snapshot(audience=self.id, since=self._cursor)
snap = [e for e in snap if e.sender != self.id]   # don't react to self

if self._pending_direct_mentions:
    seen = {e.id for e in snap}
    replays = []
    for ev_id in list(self._pending_direct_mentions):
        if ev_id in seen:
            continue
        hit = self._lookup_event(ev_id)
        if hit is None:
            self._pending_direct_mentions.remove(ev_id)  # GC
            continue
        replays.append(hit)
    snap = replays + snap                          # replays prepended

decision = decide(snap, self.id, self.coordinator.user_turn)

if snap:
    highest = max(e.id for e in snap)
    if highest > self._cursor:
        self._cursor = highest                     # advance forward only

self._update_pending_mentions(decision, snap)
return decision
```

Key behaviours:

- **Audience filter** drops DM events not visible to this actor and
  events on `dm:other_pid` channels (Session 2's `visible_to`).
- **Self-filter** skips events `self.id` posted — an actor doesn't
  react to its own commits.
- **Pending replay**: if there's a queued direct-mention event id NOT
  in this snapshot batch (because the cursor has moved past it),
  fetch it via `bus.get(id)` and prepend. If the event has been
  evicted (shouldn't happen in v0; future log compaction may), drop it
  from pending. This is the mechanism that lets a direct-mention
  trigger persist if a higher-priority event preempted it.
- **Cursor advances forward only** — `if highest > self._cursor`.
  Prevents pending replays from rewinding the cursor.

#### `_update_pending_mentions(decision, considered)`

```python
for e in considered:
    if (e.kind == "chat" and e.sender == "user"
            and self.id in e.addressees
            and e.id != decision.trigger_event_id):
        if e.id not in self._pending_direct_mentions:
            self._pending_direct_mentions.append(e.id)
if decision.trigger_event_id is not None:
    try:
        self._pending_direct_mentions.remove(decision.trigger_event_id)
    except ValueError:
        pass
```

- **Stores ONLY user-sourced direct mentions** — agent-to-agent
  mentions aren't actionable here, so don't track them.
- **Skip the event we just used** as the trigger.
- **Remove the trigger from pending** after dispatch (whether SKIP or
  DRAFT) so we don't re-replay it next wakeup.
- The `deque` is bounded; oldest evicted on append (the LRU effect).

#### Dispatch — `_dispatch_decision(decision)`

```python
trigger = self._lookup_event(decision.trigger_event_id)

if decision.action == "SKIP":
    self.coordinator.handle_skip(self.id, trigger)
    return

# DRAFT
is_direct = bool(
    trigger and trigger.sender == "user"
    and self.id in trigger.addressees
)
assert decision.trigger_event_id is not None
assert trigger is not None

lease = self.coordinator.acquire_lease(
    self.id, decision.trigger_event_id, is_direct_mention=is_direct,
)
if lease is None:
    # Speaker cap, throttle, or budget rejected. Fall back to SKIP
    # so the empty-batch path doesn't loop.
    self.coordinator.handle_skip(self.id, trigger)
    return

try:
    self.draft_handler(self, trigger, lease)
finally:
    self.coordinator.release_lease(lease)
```

- **`is_direct_mention=is_direct`** — the carve-out signal to
  `acquire_lease`. Direct user mentions bypass the
  `allowed_speakers` gate AND the per-turn cap (Session 1
  `mark_drafted(count_toward_cap=False)` for direct mentions).
- **Lease may be denied** for: speaker cap reached, throttle, budget,
  lease-grant `max_responses` race-fix accounting (Session 0
  invariant 8). On denial: fall back to `handle_skip` so the
  coordinator can still update its bookkeeping.
- **`release_lease` in `finally`** — guaranteed to fire even if the
  draft handler raises. The exception then bubbles up through
  `_step_with_error_handling`.
- The draft handler is responsible for posting `stream_*` events and
  calling `coordinator.on_stream_end` (Session 3 — `run_streaming_call`).

#### Helpers

- **`_lookup_event(event_id)`** — `bus.get(event_id)` if id is not
  None; O(1) (Session 2).

---

## journal.py — full reference

### Module-level

- **`SNAPSHOT_VERSION = 4`** — current snapshot dict version.
- **`_SUPPORTED_SNAPSHOT_VERSIONS = frozenset({1, 2, 3, 4})`** — all
  loadable. Forward-compat (older snapshots restore with sensible
  defaults for new fields); no backward-compat with future v5+.

### Snapshot version history

| Version | Notes |
|---|---|
| **v1** (legacy) | Carried `mode` + `debate` keys for retired `normal`/`council`/`debate` modes. `restore_state` ignores unknown keys. |
| **v2** | Drops `mode`/`debate`. Adds nothing new. |
| **v3** | Adds `turn_taking_mode`, `turn_order`, `next_speaker_idx` to control. |
| **v4** (current) | Drops `active_goal` from control. **Topic-merge shim**: pre-v4 snapshots that carry `control.active_goal` have it folded into `state.topic` if `topic` is unset. |

Old `events.jsonl` lines with retired control types
(`mode_changed` / `debate_turn` / `forfeit` / `debate_end`)
deserialize fine but are filtered out at replay time via
`is_known_control` (Session 1).

### `class Journal`

#### `__init__(session_dir, *, snapshot_every_events=100, snapshot_queue_maxsize=8)`

Creates the session dir with mode 0o700 (owner-only). Warns (does NOT
chmod) if it already exists with looser perms — preserves operator
intent.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `session_dir` | `Path` | required | Holds `events.jsonl` + `room_state.json`. |
| `events_path` | `Path` | derived | `session_dir / "events.jsonl"`. |
| `state_path` | `Path` | derived | `session_dir / "room_state.json"`. |
| `snapshot_every_events` | `int` | `100` | Trigger snapshot rotation every N events. |
| `_snapshot_queue_maxsize` | `int` | `8` | Bounded snapshot queue cap. With default `100/snapshot`, holds an 800-event burst. |
| `degraded` | `bool` | `False` | Flips True after any write failure. The journal continues but the audit trail is incomplete. |
| `_in_failure_dispatch` | `bool` | `False` | Recursion guard: the failure callback may post to the bus, which re-enters `on_event`. |

Internals:
- `_lock: threading.Lock` — guards file handles, counters, callbacks
- `_events_file: Optional[file]` — `None` until `open()`
- `_event_count: int` — total successful appends (does not include failed writes)
- `_last_snapshot_count: int` — `_event_count` at the most recent snapshot enqueue / sync write
- `_snapshot_queue: queue.Queue` — payloads queued for off-thread write
- `_snapshot_thread: Optional[Thread]` — daemon background writer
- `_snapshots_dropped: int` — cumulative overflow drops since process start
- Three callbacks: `_snapshot_due_cb`, `_snapshot_drop_callback`, `_failure_callback`

#### Lifecycle

- **`open()`** — opens `events.jsonl` via
  `os.open(O_WRONLY | O_APPEND | O_CREAT, 0o600)` to pin owner-only
  perms (the umask wouldn't); `os.fdopen` wraps for `buffering=1`
  (line-buffered text I/O). Spawns the snapshot thread named
  `loom-journal-snapshot`. Idempotent.
- **`close()`** — pushes `None` sentinel onto the snapshot queue,
  joins the writer thread (5 s timeout), flushes + closes the file.
  Safe to call multiple times.
- **`__enter__` / `__exit__`** — wraps `open` / `close`.

#### Permission helpers

- **`_world_or_group_perm_bits(mode) -> int`** — `mode & (S_IRWXG |
  S_IRWXO)`. Static.
- **`_warn_if_session_dir_world_readable()`** — `warnings.warn` if
  group/world bits set; recommends `chmod 700`. Does NOT chmod.
- **`_warn_if_events_file_world_readable()`** — same for the
  events.jsonl file; recommends `chmod 600`.

#### Event append — `on_event(event)`

The bus subscriber. Runs **inline under the bus lock** on the
poster's thread (Session 2 invariant 21).

Algorithm:

```python
with self._lock:
    if self._events_file is None: return       # silent drop pre-open
    try:
        self._events_file.write(event.to_jsonl() + "\n")
    except OSError as exc:
        self.degraded = True
        cb = self._failure_callback if not self._in_failure_dispatch else None
        if cb is not None:
            self._in_failure_dispatch = True
        _failure_exc = exc
    else:
        _failure_exc = None
        self._event_count += 1
        cb = None
    snap_cb = self._snapshot_due_cb
    due = (self._event_count - self._last_snapshot_count) >= self.snapshot_every_events

# OUTSIDE the lock:
if _failure_exc is not None and cb is not None:
    try: cb(_failure_exc)
    except Exception: pass
    finally:
        with self._lock: self._in_failure_dispatch = False
if _failure_exc is not None:
    return
if snap_cb is not None and due:
    payload = snap_cb()              # cheap: returns dict
    if isinstance(payload, dict):
        with self._lock: self._last_snapshot_count = self._event_count
        self._enqueue_snapshot(payload)
```

Critical details:

- **Pre-open silent drop**: events posted before `open()` are lost.
  The journal can be wired up as a subscriber before `open()` is
  called (so the room's startup events are queued in the bus log but
  not yet on disk); this means **the first events of a session may
  exist in the bus log without an events.jsonl line.** Operators who
  care should call `open()` early.
- **Failed writes do NOT count toward `_event_count`** — snapshot
  rotation arithmetic is based on what made it to disk.
- **Recursion guard `_in_failure_dispatch`**: the failure callback
  typically posts a `journal_error` event to the bus, which re-enters
  `on_event`. Without the guard, a failing journal would infinitely
  recurse. With the guard: the second entry takes the
  `cb is None`/no-op path, so the second-write attempt either
  succeeds (rare; means the underlying issue cleared) or fails
  silently (common; degraded stays True).
- **The lock is dropped before invoking callbacks** — both the
  failure callback and the snapshot due callback run without the
  journal lock so they can post to the bus / call other journal
  methods without deadlock.
- **`snap_cb()` runs synchronously on the poster thread** — must be
  cheap (microseconds); the slow disk write happens off-thread.

#### `_enqueue_snapshot(payload)` (P2.3 / audit RES3)

Bounded queue. On overflow:

```python
try: self._snapshot_queue.put_nowait(payload); return
except queue.Full: pass

try: self._snapshot_queue.get_nowait()        # drop oldest
except queue.Empty: pass

with self._lock:
    self._snapshots_dropped += 1
    total = self._snapshots_dropped
    cb = self._snapshot_drop_callback
    depth = self._snapshot_queue_maxsize

self._snapshot_queue.put_nowait(payload)      # second put (race-tolerant)

if cb is not None:
    try: cb(total, depth)
    except Exception: pass                    # drop callback is observability, not load-bearing
```

- **Snapshots are coalesce-able** — each is a complete state. Keeping
  the newest is correct; the oldest is subsumed.
- **`drop_callback` runs on the poster thread** (which is the
  state-mutating thread for state-mutating posts). Fires `snapshot_dropped`
  via the runtime so consumers observe disk saturation.
- A buggy drop callback is silently swallowed — drops are
  observability, not load-bearing.

#### Callback registration

- **`set_snapshot_due_callback(cb)`** — `cb()` returns
  `Optional[dict]`. Typically `lambda: Journal._state_to_dict(state)`.
  Returning anything other than a dict skips this rotation.
- **`set_snapshot_drop_callback(cb)`** — `cb(dropped_total,
  queue_depth)` for `snapshot_dropped` event emission.
- **`set_failure_callback(cb)`** — `cb(exc: OSError)` runs the FIRST
  time a write fails (and on subsequent failures unless guard active).

#### State snapshot

- **`snapshot(state)`** — synchronous. Used at clean shutdown when
  the caller wants the snapshot durable before returning. Calls
  `_write_snapshot_dict(_state_to_dict(state))`, then under lock
  `_last_snapshot_count = _event_count`.

#### `_write_snapshot_dict(payload)` — atomic

```python
tmp = self.state_path.with_suffix(".json.tmp")
fd  = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(payload, f, indent=2, default=str)
    f.flush()
    try: os.fsync(f.fileno())
    except OSError: pass
os.replace(tmp, self.state_path)
```

- **Tmp file + fsync + `os.replace`** — atomic from a reader's
  perspective. Readers see either the old complete file or the new
  complete file; never a torn write.
- **`mode=0o600` pinned via `os.open`** — `open(tmp, "w")` would
  respect the umask (default 0o644). Room state can include topic,
  role hints, persona memories — owner-only keeps it private on
  shared hosts.
- **`os.fsync` failure is swallowed** — the file is still on disk;
  `os.replace` will succeed; durability is best-effort.
- **OSError flips `degraded` and fires the failure callback** (with
  the same recursion guard as `on_event`).

#### `_snapshot_loop()` — background writer

```python
while True:
    payload = self._snapshot_queue.get()
    if payload is None: return            # sentinel
    try: self._write_snapshot_dict(payload)
    except Exception: pass                # _write_snapshot_dict already logged
```

Keeps draining after errors so a single bad write doesn't strand
later snapshots.

#### `_state_to_dict(state) -> dict` (static)

Serialises a `RoomState` into the v4 snapshot shape:

```python
{
    "version": 4,
    "room_epoch": int,
    "topic": Optional[str],
    "anchor_id": Optional[str],
    "chair_id": Optional[str],
    "default_responder_id": Optional[str],
    "default_summarizer_id": Optional[str],
    "current_user_turn_id": Optional[int],
    "last_compacted_event_id": int,
    "participants": [
        {"id", "capable", "cost_tier", "active", "role_hints"},
        ...
    ],
    "control": {
        "roles": dict[str, str],
        "floor_owner": Optional[list[str]],
        "wait_for_user": bool,
        "style": "brief"|"normal"|"detailed",
        "turn_taking_mode": "broadcast"|"round_robin",
        "turn_order": list[str],
        "next_speaker_idx": int,
    },
}
```

Note: `RoomConfig` is NOT serialised — it's boot config and the caller
supplies it on restore. Per-turn data (`UserTurn`, `obligations`) is
NOT serialised either — those rebuild from journal replay.

#### Load / replay

- **`load_state() -> Optional[dict]`** — reads `room_state.json`;
  returns `None` on read failure or unsupported version. Caller
  then either accepts the snapshot or rebuilds from events.
- **`load_events() -> list[Event]`** — materialises the full log via
  `iter_events(emit_corruption_events=False)`. Legacy "skip
  silently" path; preserved for callers that need a list.
- **`iter_events(*, emit_corruption_events=False)`** — generator. For
  each non-empty line in `events.jsonl`:
  - If parses cleanly via `Event.from_jsonl` → yield the Event.
  - If `EventShapeError` (or any parser exception) AND
    `emit_corruption_events=True`:
    - If this is the **last** line → yield `journal_truncated`
      (interrupted write at crash; benign).
    - Else → yield `journal_corruption` (mid-stream tampering;
      operator-actionable).
    Both events get a 120-char `redact_error_text`-scrubbed excerpt.
  - If `emit_corruption_events=False` → silently skip.
- **`replay_into(coordinator) -> int`** — calls
  `iter_events(emit_corruption_events=True)`. Skips control events
  with retired control_types via `is_known_control` (Session 1
  invariant 18). Posts every other event via
  `coordinator.bus.post_internal` (replay is privileged kernel code;
  shape was already validated). Returns the count posted, **including
  any synthetic corruption/truncation events**.

### Module-level helpers

- **`_coerce_int(v, default=0) -> int`** — `v` if real `int` (not
  `bool`); else `default`. Same `bool`-subclass guard as Session 1
  Invariant 13.
- **`_coerce_str_or_none(v) -> Optional[str]`** — `v` if `str`, `None`
  if `v is None`, else `None` (defensive: tampered snapshots can't
  inject a non-string id that breaks downstream regex).

### `restore_state(state_data, config) -> RoomState`

Rebuilds a `RoomState` from a snapshot dict. **Defensively coerces
every scalar field** (P0.3 hardening). If `state_data` is `None`
(missing / corrupt snapshot), returns a fresh empty state — caller
should then call `replay_into` to repopulate.

Algorithm:

1. Build empty `RoomState(config=config)`.
2. Coerce top-level scalars: `room_epoch`, `topic`, `anchor_id`,
   `chair_id`, `default_responder_id`, `default_summarizer_id`,
   `last_compacted_event_id`, `current_user_turn_id` (must be int or
   None — not bool).
3. For each entry in `participants` list:
   - Skip if not a dict.
   - Skip if `id` is missing or not a non-empty string (would crash
     downstream regex).
   - Coerce `capable`/`active` to `bool` (default True if not bool);
     `cost_tier` via `_coerce_int`; `role_hints` to `dict` (default
     `{}`).
   - **Bypasses `add_participant`** — assigns directly to
     `state.participants[info.id]` so no epoch bump (we're
     restoring, not mutating live state).
4. For `control`:
   - Coerce `roles` to dict (default `{}`); each k/v is `str`.
   - Coerce `floor_owner` to list (default `None`).
   - Coerce `style` to `"brief"|"normal"|"detailed"` (default
     `"normal"`).
   - Coerce `turn_taking_mode` to `"broadcast"|"round_robin"` (default
     `"broadcast"`).
   - Coerce `turn_order` to list of str.
   - Coerce `next_speaker_idx` to int ≥ 0.
   - Coerce `wait_for_user` to bool.
5. **P2.3 topic-merge shim**: pre-v4 snapshots may have
   `control.active_goal`. If `state.topic` is empty/None and
   `legacy_goal` is truthy, set `state.topic = str(legacy_goal)`.
   `state.topic` from step 2 always wins.

---

## Invariants (this session's additions)

49. **One daemon thread per participant.** Spawned in
    `ParticipantActor.start()`; named `loom-actor-{id}`; daemon so
    process exit doesn't block on it.
50. **The actor binds its thread to its participant id ONCE** at the
    top of `_loop`, unbinds in `finally`. All `bus.post(...)` calls
    from the actor / draft handler / proxy run on this bound thread
    — sender is authenticated. Privileged emissions
    (`actor_error` with sender="system") use `post_internal`.
51. **Cursor advances forward only.** `_decide_once` sets
    `_cursor = max(highest_id_in_batch, _cursor)`. Pending replays
    can prepend events with older ids without rewinding the cursor.
52. **Pending direct mentions are user-sourced only.** Agent-to-agent
    @-mentions are not stored (they're not actionable here; the
    coordinator's `allowed_speakers` gate handles them via the
    obligation/lease path).
53. **Direct user mentions are the only Priority-1 trigger.** Inter-
    agent addressing routes through obligations + leases so chains
    close at `max_responses`.
54. **`decide()` is pure.** No mutation, no I/O. Test it directly
    without bus / coordinator setup.
55. **Lease release in `finally`.** `_dispatch_decision` wraps the
    `draft_handler` call in `try/finally release_lease(lease)` —
    guaranteed even if the handler raises.
56. **Lease denial falls back to `handle_skip`.** `acquire_lease` may
    return `None` (cap, throttle, budget). The actor must still
    update coordinator bookkeeping; otherwise the empty-batch path
    would loop.
57. **Idle-timeout nudge from the timeout branch.** When
    `wait_after` returns the timeout (no new events), the actor
    calls `coordinator.check_idle_timeout()`. This is how idle-turn
    closure happens without an external scheduler.
58. **`events.jsonl` is authoritative; `room_state.json` is
    advisory.** Replay from events alone always reconstructs correct
    state. Snapshot is a fast-resume cache. Conflict → events win.
59. **Snapshot writes are atomic via tmp + fsync + os.replace.**
    Readers always see a complete file. A torn write cannot occur.
60. **All journal files are owner-only (0o600 / 0o700).** Pinned
    via `os.open` to override umask. Existing-file looser perms
    produce a warning, not a chmod.
61. **The journal's snapshot queue is bounded (default 8).** On
    overflow, the OLDEST is dropped — snapshots are coalesce-able.
    Drops are surfaced via `snapshot_dropped` control event.
62. **Failed writes flip `degraded` and fire the failure callback.**
    The room continues running but the audit trail is incomplete.
    Recursion guard prevents the failure callback (which posts a
    `journal_error` event) from re-entering `on_event` infinitely.
63. **Snapshot due callback runs SYNCHRONOUSLY on the poster
    thread.** Must be cheap (microseconds). The slow disk write
    happens off-thread on `_snapshot_loop`.
64. **The journal can be wired up before `open()` is called**;
    events posted before `open()` are silently dropped. **Open the
    journal early** if you care about the first events of a session.
65. **`replay_into` skips retired control_types via
    `is_known_control`.** This is how legacy v1
    `mode_changed`/`debate_*` lines coexist with current code.
66. **`replay_into` posts via `bus.post_internal`.** Replay is
    privileged kernel code by definition — the shape was already
    validated by `Event.from_jsonl`.
67. **`restore_state` defensively coerces every scalar field.**
    Tampered snapshots cannot inject a non-string participant id, a
    boolean room_epoch, etc. Bad participants are skipped silently
    rather than propagated.
68. **Snapshot version 4 is current; 1/2/3 are loadable** with
    sensible defaults for new fields. The v4 topic-merge shim folds
    legacy `control.active_goal` into `state.topic` when topic is
    unset.
69. **`RoomConfig` is NOT serialised.** Boot config; caller supplies
    on restore. Per-turn data (`UserTurn`, `obligations`) is also not
    serialised — those rebuild from event replay.
70. **Restore bypasses `add_participant`.** Assigns directly to
    `state.participants` so no epoch bump (we're restoring, not
    mutating live state).
71. **Build_loom_session does NOT call `replay_into` / `restore_state`
    on startup in v0.1.x.** The journal is purely an audit log at
    runtime. Auto-restart wiring lands in v0.2 (orientation doc:
    "v0.1.2 limitations").

---

## Verification

> **Q1: Describe what happens when an actor wakes on a `chat` event
> addressed to it: which functions fire, in which thread, what state
> they read, what they do not write directly.**

Setup: actor `b` is parked in `bus.wait_after(self._cursor=4,
timeout=20.0)` on its own daemon thread (`loom-actor-b`). Some other
thread (e.g. the runtime) calls `bus.post_internal(chat(sender="user",
body="hi @b", addressees=["b"], …))`. Sequence:

1. **(runtime/coordinator thread, under bus lock)** `_post_unchecked`
   assigns `ev.id = 5`, `ev.ts = time.time()`; appends to `_log`;
   calls `_cond.notify_all()`; iterates subscribers (journal among
   them). The journal's `on_event` runs inline: writes
   `event.to_jsonl() + "\n"` to `events.jsonl`, increments
   `_event_count`, possibly enqueues a snapshot. All this happens on
   the poster's thread; bus lock still held throughout.
2. **(actor thread)** `wait_after` returns `len(_log)=6` (>4+1).
   `_stopped` not set, `bus.stopped` False. Falls through to
   `_step_with_error_handling()` → `step()` → `_decide_once()`.
3. **(actor thread)** `bus.snapshot(audience="b", since=4)` returns
   `[ev]` (the user post, visible to b on main). Self-filter doesn't
   drop it (sender="user"). No pending mentions to replay. Calls
   `decide([ev], "b", coordinator.user_turn)`.
4. **(actor thread, pure function)** `decide` calls
   `pick_priority_trigger`. `_trigger_priority` returns 1 (chat from
   user with "b" in addressees). `is_direct=True`; returns
   `AgentDecision(action="DRAFT", trigger_event_id=5,
   considered_event_ids=[5], reason="direct_mention")`.
5. **(actor thread)** `_decide_once` advances `_cursor = 5`.
   `_update_pending_mentions` does NOT add 5 to pending (it equals
   `decision.trigger_event_id`); attempts to `remove(5)` from pending
   but it isn't there.
6. **(actor thread)** `_dispatch_decision`: `_lookup_event(5)` →
   `bus.get(5)` → returns the chat event. `is_direct=True` (recomputed
   from trigger). Calls
   `coordinator.acquire_lease("b", 5, is_direct_mention=True)`.
7. **(actor thread, briefly inside coord lock)** Coordinator validates
   `b`'s eligibility, checks cap (direct mention bypasses
   `allowed_speakers` and the per-turn cap), allocates a `TurnLease`
   with `id=…, holder="b", trigger_event_id=5,
   user_turn_id=<current>, room_epoch=<current>`. Returns the lease.
8. **(actor thread)** `try: draft_handler(self, trigger=ev, lease)` —
   in production this is the closure from
   `make_default_draft_handler` (Session 3) which calls
   `proxy = proxy_for("b")`, `prompt = build_prompt("b", trigger,
   coord, …)`, then `run_streaming_call(proxy, prompt, lease, bus,
   coord)`. The streaming call runs on the actor's bound thread.
   - `bus.post(stream_start(...))` — sender="b" matches binding, OK.
   - For each chunk from `proxy.stream(prompt)`: cost +=, validate
     lease, buffer/flush, post `stream_delta` (sender="b").
   - End-of-stream: filter chain → status. If committed, post `chat`
     (sender="b") then `stream_end`. **Always** call
     `coordinator.on_stream_end(lease, status, …)`.
9. **(actor thread)** `finally: coordinator.release_lease(lease)`.
10. **(actor thread)** `step()` returns. Loop iterates;
    `wait_after(cursor=5, timeout=20)` — and so on.

**State the actor reads directly**: `bus._log` (via `snapshot`,
`get`); `coordinator.user_turn` (a `UserTurn` reference);
`coordinator.config` (only at construction); the trigger event's
fields. **State the actor never writes**: `RoomState` (only the
coordinator); `UserTurn.obligations` (only the coordinator, in
`on_stream_end`); `bus._log` ordering (only `_post_unchecked`); any
cross-actor state. The actor only writes its own `_cursor`,
`_pending_direct_mentions`, and posts events through the bus where the
sender authentication enforces it can only claim its own identity.

**Threads at play**: one of {runtime, coordinator, another actor}
posted the user event on its own thread (under bus lock). The actor's
own daemon thread runs every step from `wait_after` through
`release_lease`. The journal's snapshot writer runs on a third daemon
thread, completely off the actor's hot path.

> **Q2: Explain why `events.jsonl` is authoritative and
> `room_state.json` is advisory.**

`events.jsonl` is the **append-only ledger of every canonical bus
event in the order they were posted**. It mirrors the bus's `_log`
exactly. By construction, replaying every event into a fresh
coordinator (with the same `RoomConfig`) reconstructs the *exact*
sequence of state transitions: every `add_participant`,
`set_topic`, `obligation_recorded`, `obligation_resolved` etc.
happened because of an event, and the event carries enough
information for the coordinator to redo it. This means **the entire
room state is a deterministic function of the journal**.
`replay_into` is the canonical realisation of this — feed events
back to `coordinator.bus.post_internal` and the coordinator's normal
event-handling logic (Session 5) re-derives the state.

`room_state.json` is a **periodic point-in-time snapshot** of
`RoomState` written every 100 events (configurable) or on clean
shutdown. It exists purely as a **fast-resume cache**: instead of
replaying 50,000 events from the start of a long session, restore
the snapshot (covering the first ~49,900 events) and replay only
the tail (~100 events). Equivalent state, 500x cheaper.

But the snapshot is **lossy and second-class** for three reasons:

1. **It can be missing or stale.** A crash between snapshot
   intervals leaves the snapshot up to 100 events behind. The events
   between the snapshot's `last_compacted_event_id` and the journal's
   current tail must be replayed regardless.
2. **It can be corrupt.** Disk corruption, partial writes, or
   tampering can leave `room_state.json` unparseable. `load_state`
   returns `None` on any read failure or unsupported version, and
   `restore_state(None, config)` returns a fresh empty state — the
   journal then rebuilds the entire history.
3. **It cannot represent everything.** Per-turn data (`UserTurn`,
   `obligations`) and policy state are NOT serialised. Snapshots
   only capture `RoomState`. Reconstructing the open turn, its
   obligations, and the routing context requires replay anyway.

So the contract is: **events.jsonl is canonical source of truth;
room_state.json is an opportunistic fast path**. If the two ever
disagree, events win — `_state_to_dict` was always derived from
state that was itself derived from events. The defensive coercions
in `restore_state` (P0.3) treat the snapshot as untrusted input
even though it was written by the same process: a malicious
snapshot cannot inject participants, change topic, or shift
slots — only inform the fresh state of what we *thought* was
true at the snapshot moment, subject to journal replay overriding.

(One caveat: v0.1.2 does NOT call `replay_into` / `restore_state`
in `build_loom_session` — the journal is purely an audit log at
runtime. Auto-restart-recovery wiring is v0.2 work.)

---

## Cross-references

- depends on: `00-orientation.md` (threading model, journal design),
  `01-kernel-primitives.md` (`Event`, `RoomState`, `RoomConfig`,
  `UserTurn`, `RoomControlState`, `ParticipantInfo`, `ResponseObligation`,
  `is_user_turn_complete`, `make_user_turn`, `is_known_control`,
  `_coerce_int` analogue), `02-kernel-bus.md` (`MessageBus.post`,
  `post_internal`, `bind_actor`, `unbind`, `wait_after`, `snapshot`,
  `get`, `subscribe`, the bus-lock-held subscriber contract, the
  `SenderMismatchError` carve-outs).
- depended on by:
  - `coordinator.py` (Session 5) — the actor calls `coordinator.handle_skip`,
    `coordinator.acquire_lease`, `coordinator.release_lease`,
    `coordinator.on_stream_end`, `coordinator.check_idle_timeout`,
    `coordinator.user_turn`, `coordinator.config`,
    `coordinator.bus`. Coordinator uses `Journal` via
    `set_snapshot_due_callback` / `set_snapshot_drop_callback` /
    `set_failure_callback`.
  - `loom/runtime.py` (Session 7) — `build_loom_session` instantiates
    one `ParticipantActor` per agent, registers
    `Journal.on_event` as a bus subscriber, calls `Journal.open` /
    `Journal.close`, calls `Journal.snapshot(state)` at clean shutdown.
  - `streaming.py` (Session 3) — `make_default_draft_handler` returns
    the callable `actor.draft_handler`.

## Open questions / things to revisit

1. **Auto-restart wiring** (v0.2 work). The pieces are there
   (`load_state`, `restore_state`, `replay_into`); `build_loom_session`
   needs to: (a) call `load_state`; (b) call `restore_state(data,
   config)`; (c) build the coordinator with the restored state; (d)
   call `replay_into(coordinator)` for the tail. Order matters: the
   coordinator must be constructed but BEFORE actors start, so events
   can replay without producing real LLM calls.
2. **Policy state snapshot/restore** (v0.2). `_state_to_dict` doesn't
   serialise policy state because policies are stateless across
   restarts in v0. Adding lifecycle hooks (`policy.snapshot()` /
   `policy.restore(blob)`) and including the blob in the v5 snapshot
   is the path. Stateful policies (debate phase, 20Q question count)
   reset across restart today.
3. **Hash chain over the journal** (audit P3 / R1). Defence-in-depth
   for `events.jsonl` integrity beyond per-line shape validation.
   Each line includes a hash of the previous line's hash + its own
   bytes; replay verifies the chain. A tampered line breaks the
   chain at every subsequent line.
4. **Off-thread subscriber dispatch with timeout** (CON1 / P2.5,
   v0.2). Today, journal `on_event` runs inline under the bus lock —
   if the event_file is slow (NFS hiccup), every actor blocks. The
   contract is documented; implementation deferred. Touches both
   `MessageBus._post_unchecked` and `Journal` as an early
   beneficiary.
5. **Pre-`open()` event drop.** If the journal is wired before
   `open()`, events are silently lost. Either: (a) buffer pre-open
   events in memory and flush on `open`; (b) make `open` mandatory
   before `subscribe`; (c) document loudly and accept. Today
   `loom.runtime.build_loom_session` calls `Journal.open()` before
   wiring the subscription, so the issue doesn't bite — but the order
   is load-bearing.
6. **`_pending_direct_mentions` LRU eviction.** Default cap is 100.
   In a high-volume room with many @-mentions to the same agent,
   older mentions silently evict. Today no metric tracks evictions.
   For diagnostics, consider counting them and surfacing via
   `actor_error` or a dedicated control event.
7. **`wakeup_timeout_s` defaults to `min(idle_timeout, lease_ttl)`.**
   With defaults (20s, 60s) → 20s. If the operator raises
   `idle_timeout_s` to e.g. 600 but leaves `lease_ttl_s` at 60,
   wakeup remains at 60s — the idle check fires on every wakeup but
   only acts when the actual idle time has elapsed. Consistent but
   non-obvious.
8. **`assert decision.trigger_event_id is not None` in `_dispatch`**.
   Both asserts encode the "DRAFT implies trigger is set" invariant.
   In Python `-O` mode (asserts stripped) the assertion vanishes; the
   downstream `acquire_lease` would still reject `None`, but with a
   less obvious failure mode. Consider promoting to a real check.
9. **`actor_error` message is truncated to 500 chars in
   `_step_with_error_handling`** before being passed to `ev.actor_error`,
   which then runs it through `redact_error_text` (default cap 500
   chars). Doubly-capped — minor over-engineering, but not wrong.
10. **`_state_to_dict` is `@staticmethod`** but uses no static-method
    behaviours. Could be a free function. Cosmetic.
