# 05 — Coordinator

This is **Session 5** of the Loom kernel deep-study curriculum.
`RoomCoordinator` is the **single mutator** of `RoomState`, the
**only** writer of authoritative bus control events, the holder of the
policy lock and the lease ledger, and the focal point where the
primitives from Sessions 1–4 actually compose into a working room.

State as of Loom v0.1.2 (2026-05-08).

## Files covered

| File | LOC | Role | Imports from kernel |
|---|---:|---|---|
| `loom/kernel/coordinator.py` | 1097 | `RoomCoordinator`, `TurnLease`, `LoopGuardConfig`, `ThrottleConfig`, `BudgetConfig` | `events`, `bus`, `obligations`, `room`, `user_turn` |

This is the **largest single file in the kernel**. There are no
runtime dependencies on `actor.py` (the cycle is broken — coordinator
forward-references actors via parameter passing only) or
`streaming.py`/`prompt.py` (those import the coordinator for type
checking only). Everything below this file in the dependency graph is
free of coordinator logic.

## Mental model

```
                       SINGLE RLock (re-entrant: public methods may call each other)
   ┌───────────────────────────────────────────────────────────────────────────────┐
   │  RoomCoordinator                                                              │
   │  ────────────────────────────────────────────────────────────────────────     │
   │  state: RoomState  ◄── mutates only here                                     │
   │  bus:   MessageBus ◄── emits only via post_internal                          │
   │                                                                               │
   │  _user_turn:  Optional[UserTurn]   (one open turn at a time)                 │
   │  _leases:     dict[int, TurnLease] (in-flight ledger)                        │
   │                                                                               │
   │  _loop_guard:  LoopGuardConfig  ── bag-of-words IoU short-text dup detector  │
   │  _throttle:    ThrottleConfig   ── per-pid + per-channel sliding 60s         │
   │  _budget:      BudgetConfig     ── per-UserTurn cumulative tokens            │
   │                                                                               │
   │  policy_error_mode: "close_turn" (default) | "default_responder" | "raise"   │
   │                                                                               │
   │  ┌─ Membership ──────┐  ┌─ Slots ──────────┐  ┌─ Control state ─────┐       │
   │  │ register_part'    │  │ set_topic        │  │ set_roles            │       │
   │  │ unregister_part'  │  │ set_anchor       │  │ set_floor_owner      │       │
   │  │   ↓ rerouting +   │  │ set_chair        │  │ set_style            │       │
   │  │   ↓ dead-letter + │  │ set_default_*    │  │                      │       │
   │  │   ↓ obligation    │  └──────────────────┘  └──────────────────────┘       │
   │  │     transfer      │                                                       │
   │  └───────────────────┘                                                       │
   │                                                                               │
   │  ┌─ UserTurn lifecycle ──────────────────────────────────────────────┐      │
   │  │  post_user_event_and_open_turn(user_event, classify_fn)           │      │
   │  │     │ atomic under lock so actors can't race past the open        │      │
   │  │     ├─ bus.post_internal(user_event)                               │      │
   │  │     ├─ _run_policy_under_lock(classify_fn, user_event)             │      │
   │  │     │     timing watchdog → policy_slow                            │      │
   │  │     │     exceptions     → policy_error + mode-dispatch fallback   │      │
   │  │     ├─ _apply_plan_state_changes_locked (mode/order side effects)  │      │
   │  │     └─ open_user_turn(user_event, plan) (unless ack)               │      │
   │  │                                                                    │      │
   │  │  open_user_turn:                                                   │      │
   │  │     - debounce within user_turn_debounce_ms → add to               │      │
   │  │       debounced_event_ids, bump activity, return existing          │      │
   │  │     - else close any open turn ("new_user_post"); clear            │      │
   │  │       wait_for_user; make_user_turn; emit user_turn_opened +       │      │
   │  │       obligation_recorded(*); empty plan → close "no_responder"    │      │
   │  │                                                                    │      │
   │  │  close_user_turn(reason):                                          │      │
   │  │     - "cancelled" resolves remaining obligations administratively  │      │
   │  │     - emit user_turn_closed                                        │      │
   │  │     - apply plan.wait_for_user_after / advance_turn_pointer        │      │
   │  │                                                                    │      │
   │  │  check_idle_timeout (called by actor timeouts):                    │      │
   │  │     - unresolved_required → "obligation_unresolved"                │      │
   │  │     - else → "idle_timeout"                                        │      │
   │  └────────────────────────────────────────────────────────────────────┘      │
   │                                                                               │
   │  ┌─ Lease ledger ─────────────────────────────────────────────────────┐     │
   │  │  acquire_lease(holder, trigger_event_id, is_direct_mention):       │     │
   │  │     rejection chain (return None on any):                          │     │
   │  │       no open turn / holder unknown / holder inactive              │     │
   │  │       allowed_speakers gate (direct-mention bypass)                │     │
   │  │       per-participant cap (max_drafts_per_participant)             │     │
   │  │       per-turn max_responses cap (committed + outstanding leases)  │     │
   │  │       throttle (per-pid + per-channel)                             │     │
   │  │       budget (per-UserTurn tokens)                                 │     │
   │  │     allocate TurnLease with monotonic timestamps                   │     │
   │  │  validate_lease: valid AND in ledger AND epoch matches AND not     │     │
   │  │     past expires_at                                                │     │
   │  │  release_lease: pop ledger, mark invalid                           │     │
   │  └────────────────────────────────────────────────────────────────────┘     │
   │                                                                               │
   │  ┌─ Stream callbacks (called by streaming.py) ───────────────────────┐      │
   │  │  on_stream_end(lease, status, committed_text, cost_tokens,        │      │
   │  │                 committed_event_id):                              │      │
   │  │    always: budget.record(turn_id, cost_tokens)                    │      │
   │  │    "committed":  mark_drafted, loop_guard.record, resolve oblig   │      │
   │  │                  with by_event_id=committed_event_id              │      │
   │  │    "passed":     resolve oblig administratively (no draft mark)   │      │
   │  │    suppressed/cancelled/error/lease_expired: leave oblig intact   │      │
   │  │    finally: _maybe_close_user_turn_locked                         │      │
   │  │                                                                    │      │
   │  │  handle_skip(holder, trigger_event):                              │      │
   │  │    bump ut.last_activity_at so empty-batch wakeups don't fire     │      │
   │  │    idle immediately                                                │      │
   │  └────────────────────────────────────────────────────────────────────┘     │
   └───────────────────────────────────────────────────────────────────────────────┘
```

The coordinator is just a state machine over `RoomState` + `UserTurn`
+ `_leases`, with a fixed set of input methods and a fixed vocabulary
of emitted control events. Everything else (actors, streaming, prompt,
journal, runtime) is just a well-behaved client of this surface.

---

## Module-level

- **`_POLICY_SLOW_THRESHOLD_MS = 100.0`** — watchdog. Calls to
  `classify_fn` that exceed this trigger a `policy_slow` control event
  (no interruption — Python can't safely cancel arbitrary code).
- **`PolicyErrorMode = str`** — type alias for
  `Literal["close_turn", "default_responder", "raise"]`.

---

## `class TurnLease` (mutable dataclass)

| Field | Type | Notes |
|---|---|---|
| `id` | `int` | Monotonic, allocated by coordinator. |
| `holder` | `str` | Participant id. |
| `user_turn_id` | `int` | The turn this lease was acquired in. |
| `trigger_event_id` | `int` | The event id that triggered the actor's wakeup. |
| `room_epoch` | `int` | Snapshot of `state.room_epoch` at acquire-time. |
| `acquired_at` | `float` | **`time.monotonic`** — TIME1 / P3.3 hardening. Wall-clock would let an NTP step backward widen the validity window or forward shrink it. |
| `expires_at` | `float` | **`time.monotonic`** — `acquired_at + lease_ttl_s` (default 60). |
| `valid` | `bool` | Defaults `True`. Coordinator flips on epoch bump, expiry, release, or removal. |

Validation flow (`validate_lease`):

```
valid AND id in _leases AND room_epoch == current AND now <= expires_at
```

Any failure: `lease.valid = False` and return `False`. Streaming's
per-chunk `validate_lease` check (Session 3) catches mode/membership
changes mid-stream and aborts with status="lease_expired".

---

## `class LoopGuardConfig` (frozen, internal mutable dict)

Bag-of-words IoU duplicate detector. Suppresses **short** near-duplicate
replies that would otherwise form idle chains (e.g. "standing by",
"waiting for argument"). Keyed per-participant.

| Field | Default | Meaning |
|---|---|---|
| `iou_threshold` | `0.8` | IoU above this → duplicate. |
| `short_text_chars` | `50` | Only short replies are subject to suppression. |
| `_last` | `dict[str, str]` | Per-participant most-recent committed reply. **Mutated in-place** despite `frozen=True` — frozen prevents attribute reassignment, not dict mutation (audit F4.4 / P2.2). |

Methods:

- **`is_idle_dup(participant_id, new_text) -> bool`**:
  - Returns `False` if no prior recorded.
  - Returns `False` if `len(new_text) >= short_text_chars` (long
    replies are never dups — content is presumed substantive).
  - Returns `_iou(prev, new_text) > iou_threshold`.
- **`record(participant_id, text)`** — overwrites the per-participant
  last text. Called from `on_stream_end` on `"committed"`.
- **`_iou(a, b) -> float`** (static) — `len(set_a & set_b) /
  len(set_a | set_b)`. Words are lowercase whitespace-split. Both
  empty → 1.0; one empty → 0.0.

The streaming code (Session 3) calls this in the post-stream filter
chain: `coordinator.loop_guard.is_idle_dup(holder, cleaned)` is the
third filter (after chair-speak strip, empty check, idle-phrase
check). A `True` result flips status to `"suppressed"`.

---

## `class ThrottleConfig` (frozen, internal mutable dicts)

Per-participant + per-channel rate buckets, sliding 60-second window.

| Field | Default | Meaning |
|---|---|---|
| `per_participant_per_min` | `10` | Cap for a single pid across all channels. |
| `per_channel_per_min` | `60` | Cap for all participants on one channel. |
| `_participant_hist` | `dict[str, list[float]]` | Per-pid timestamp history. |
| `_channel_hist` | `dict[str, list[float]]` | Per-channel timestamp history. |

Method: **`try_consume(participant_id, channel, now=None) -> bool`**.
Sliding window:

```python
now = now or time.monotonic()
cutoff = now - 60.0
ph[:] = [t for t in ph if t >= cutoff]   # in-place GC
ch[:] = [t for t in ch if t >= cutoff]
if len(ph) >= per_participant_per_min: return False
if len(ch) >= per_channel_per_min: return False
ph.append(now); ch.append(now)
return True
```

Always called for the channel `"main"` from `acquire_lease` — DM
channel throttling isn't wired up at lease time today.

---

## `class BudgetConfig` (frozen, internal mutable dict)

Cumulative-cost tracker, scoped per UserTurn.

| Field | Default | Meaning |
|---|---|---|
| `max_tokens_per_user_turn` | `200_000` | Soft per-turn budget. |
| `_per_turn` | `dict[int, int]` | turn_id → tokens consumed so far. |

Methods:

- **`can_acquire(user_turn_id, estimated_cost=0) -> bool`** — `True` if
  `user_turn_id is None` (no enforcement); else
  `(used + estimated_cost) <= max_tokens_per_user_turn`.
- **`record(user_turn_id, cost)`** — additive bookkeeping. No-op if
  `user_turn_id is None`. Called from `on_stream_end` on every status
  (committed, passed, suppressed, etc.) — even rejected drafts cost
  tokens to generate.
- **`used(user_turn_id) -> int`** — current consumption.

The cumulative-per-turn scope means a single bad-faith agent burning
through tokens can't permanently freeze a participant; once the turn
closes, the next turn starts fresh.

---

## `class RoomCoordinator`

### `__init__(bus, state, *, policy_error_mode="close_turn")`

| Field | Type | Notes |
|---|---|---|
| `bus` | `MessageBus` | Shared. |
| `state` | `RoomState` | Shared, single-mutator. |
| `config` | `RoomConfig` | `state.config` — boot-time. |
| `_lock` | `threading.RLock` | **Re-entrant**. Public methods may call each other. |
| `policy_error_mode` | `str` | Validated against `{"close_turn", "default_responder", "raise"}`. Raises `ValueError` on unknown. |
| `_leases` | `dict[int, TurnLease]` | In-flight lease ledger. |
| `_next_lease_id` | `int = 0` | Monotonic. |
| `_user_turn` | `Optional[UserTurn]` | At most one open turn at a time. |
| `_next_user_turn_id` | `int = 0` | Monotonic. |
| `_next_obligation_id` | `int = 1` | Monotonic; `0` is reserved for unallocated `ResponseObligation` placeholders (Session 1 invariant 17). |
| `_last_user_post_ts` | `Optional[float]` | `time.monotonic`. Drives debounce. |
| `_loop_guard` | `LoopGuardConfig` | Instance. |
| `_throttle` | `ThrottleConfig` | Instance. |
| `_budget` | `BudgetConfig` | Instance. |
| `_compaction_in_flight` | `bool = False` | Placeholder; not used in v0.1.2. |

### Properties

- **`user_turn`** — read under lock; `Optional[UserTurn]`.
- **`loop_guard`** — direct ref (no lock). Streaming reads this.
- **`budget`** — direct ref (no lock).

### Membership

#### `register_participant(info)`

```python
state.add_participant(info)                # raises ValueError if id exists
bus.post_internal(participant_added(info.id, info.role_hints))
```

Atomic under lock. Bumps `room_epoch` (Session 1 epoch-bump table).

#### `unregister_participant(pid)` — the dead-letter path

This is one of the most intricate methods in the kernel. It does seven
things in order, all under the lock:

1. **`state.remove_participant(pid)`** → `slot_changes` dict naming
   any slot that previously pointed at `pid` and the new resolution
   (`cheapest_active_capable()`).
2. **Emit `participant_removed(pid)`**.
3. **Emit slot-change events** for every slot in `slot_changes`:
   - `default_responder_id` → `default_responder_changed(pid, new)`
   - `anchor_id`/`chair_id`/`default_summarizer_id` → corresponding
     `*_changed` control events via `_control(factory, old_id=pid,
     new_id=...)`.
4. **Invalidate `pid`'s in-flight leases** — set `lease.valid = False`
   for any lease where `lease.holder == pid`.
5. **`_transfer_required_obligations_locked(pid, slot_changes)`** —
   the v0.1.2 dead-letter rerouting fix (invariant 9). For each
   unresolved must/should obligation held by `pid`, allocate a NEW
   obligation on a fallback (`default_responder_id` slot, then
   `cheapest_active_capable`) via `ut.add_obligation` (Session 1).
   The fallback is also added to `frozen_plan.allowed_speakers` so it
   can acquire a lease (the plan was scoped before the removal).
   Emits `obligation_recorded`. **Only the first obligation
   transfers** — additional must/should obligations from the removed
   participant collapse onto the same fallback rather than duplicating.
6. **Resolve `pid`'s own open obligations administratively** —
   `_resolve_obligation_locked(ob.id, by_event_id=None)` for each.
   Emits `obligation_resolved` per. (This is in addition to step 5;
   the rerouted fallback gets a fresh obligation, the original gets
   marked resolved.)
7. **`_dead_letter_pending_mentions(pid, slot_changes)`** — for any
   `chat` event since the current turn started where `pid` is in
   `addressees` AND `pid` hasn't already replied to it (event id >
   last response from pid), emit a `dead_letter` event with
   `reroute_to` = same fallback chain.
8. **`_maybe_close_user_turn_locked()`** — the cascade may have
   resolved the last unresolved required obligation; if so, close
   the turn cleanly.

Caller responsibility: stopping the participant's actor thread is
separate (handled by the runtime in Session 7).

#### Helper: `_transfer_required_obligations_locked(removed_pid, slot_changes)`

Algorithm:

1. No-op if no open turn.
2. Pick `reroute_to`:
   - First: `slot_changes.get("default_responder_id", state.default_responder_id)`
   - If `None` or `== removed_pid`: `state.cheapest_active_capable()`
   - If still `None` or `== removed_pid`: return (no fallback exists).
3. Skip if reroute candidate already drafted in this turn (no need to
   double-obligate them) or already holds an unresolved obligation.
4. Iterate `ut.obligations`; for the first unresolved must/should
   held by `removed_pid`:
   - `ut.add_obligation(reroute_to, level, target_event_ids,
     reason=f"rerouted_from_{removed_pid}",
     next_obligation_id=self._next_obligation_id)` (returns new id).
   - **Add `reroute_to` to `ut.frozen_plan.allowed_speakers`** —
     mutates the frozen plan because the lease gate would otherwise
     reject (the plan was scoped before removal). This is the **only
     legal mutation** of a "frozen" plan in the kernel.
   - Emit `obligation_recorded` with the new id and the
     `rerouted_from_<pid>` reason — actors recognise this reason as a
     priority-2 trigger (Session 4 trigger priority table).
   - Return after first transfer.

#### Helper: `_dead_letter_pending_mentions(removed_pid, slot_changes)`

1. No-op if no open turn.
2. Snapshot `bus.snapshot(channel="main", kinds=["chat"], since=ut.user_event_id - 1)`.
3. Find `last_response_id` = max id of any chat sent by `removed_pid`
   in the snapshot (so we don't dead-letter what they already
   answered).
4. For each chat where `removed_pid in addressees` AND `e.id >
   last_response_id`:
   - Pick `reroute_to` (same fallback chain as transfer).
   - Emit `dead_letter(original_mention_event_id=e.id,
     reroute_to=reroute_to, reason="participant_removed")`.

### Slots (one method per slot — all under lock)

| Method | Behaviour | Event emitted | Special |
|---|---|---|---|
| `set_topic(new_topic)` | No-op if `old == new`. **Closes any open turn with `"topic_changed"`** before mutating. | `topic_changed(old, new or "")` | The only slot setter that closes the open turn. |
| `set_default_responder(pid)` | No-op if `old == pid`. **Invalidates ALL leases** (not just `pid`'s). | `default_responder_changed(old, pid)` | The only slot setter that invalidates EVERY lease. |
| `set_anchor(pid)` | No-op if `old == pid`. | `anchor_changed(old_id=old, new_id=pid)` (via `_control`) | |
| `set_chair(pid)` | No-op if `old == pid`. | `chair_changed(...)` | |
| `set_default_summarizer(pid)` | No-op if `old == pid`. | `default_summarizer_changed(...)` | |

### Room control state setters

| Method | Behaviour | Event |
|---|---|---|
| `set_roles(roles)` | Replaces map; unknown ids filtered. No-op if dict equals old. | `roles_assigned(new_dict)` |
| `set_floor_owner(floor_owner, *, wait_for_user=None)` | Sets floor and optionally `wait_for_user`. Empty/`None` opens floor. Emits with only changed fields. | `floor_updated(floor_owner=…?, wait_for_user=…?)` |
| `set_style(style)` | No-op if `old == new`. | `style_changed(old, new)` |

### UserTurn lifecycle

#### `post_user_event_and_open_turn(user_event, classify_fn) -> UserTurnPlan`

The **race-free** post-and-open path. Atomic under lock:

```python
with self._lock:
    self.bus.post_internal(user_event)              # assign id, notify actors
    plan = self._run_policy_under_lock(classify_fn, user_event)
    self._apply_plan_state_changes_locked(plan)     # mode/order BEFORE open
    if plan.routing_case != "acknowledgement":
        self.open_user_turn(user_event, plan)
return plan
```

**Why holding the lock matters**: without it, `bus.post_internal`
notifies waiters via `_cond.notify_all()` and an actor thread can
race in to call `coordinator.user_turn` (which acquires this same
lock) BEFORE `open_user_turn` runs. The actor would see
`user_turn = None`, decide SKIP, advance its cursor past the user
event, and never wake on it again when the turn does open. Holding
`self._lock` here forces actor threads to block on
`coordinator.user_turn` until this method returns.

`_apply_plan_state_changes_locked` runs **before** the
acknowledgement check so an acknowledgement plan that flips mode (e.g.
the game-end phrase exit returning `set_turn_taking_mode="broadcast"`)
still applies the mode change even though no turn opens.

#### `_run_policy_under_lock(classify_fn, user_event) -> UserTurnPlan`

Watchdog wrapper around `classify_fn`. Caller holds `self._lock`.

```python
t0 = time.monotonic()
try:
    plan = classify_fn(user_event)
except Exception as exc:
    elapsed_ms = (time.monotonic() - t0) * 1000
    bus.post_internal(_control("policy_error",
        exception_class=type(exc).__name__,
        message=str(exc)[:500],
        elapsed_ms=round(elapsed_ms, 3),
        user_event_id=user_event.id,
    ))
    if policy_error_mode == "raise": raise
    if policy_error_mode == "default_responder":
        return plan_for_default(state.resolve_default_responder(),
                                reason="policy_error",
                                target_event_ids=[user_event.id],
                                rationale="policy raised; falling back...")
    # "close_turn" (default, fail-closed):
    return plan_for_acknowledgement(target_event_ids=[user_event.id],
                                    rationale="policy raised; turn closed")

elapsed_ms = (time.monotonic() - t0) * 1000
if elapsed_ms > 100:
    bus.post_internal(_control("policy_slow",
        elapsed_ms=..., threshold_ms=100, user_event_id=user_event.id))
return plan
```

Key behaviours:

- The **`policy_error` event is always emitted** regardless of mode.
  The mode controls only what plan we return.
- **`"close_turn"` (default, fail-closed)** returns an acknowledgement
  plan so the outer caller's `routing_case != "acknowledgement"`
  guard skips `open_user_turn` — the turn closes silently.
- **`"default_responder"`** falls back to `plan_for_default(state.resolve_default_responder(), ...)`.
- **`"raise"`** re-raises after the `policy_error` event has been
  recorded — useful in dev mode to surface stack traces.
- **`policy_slow`** is observability only; no interruption (Python
  can't safely cancel arbitrary code).
- The exception's `str(exc)` is truncated to 500 chars before being
  put on the event payload (the `_control` constructor doesn't run
  it through `redact_error_text` because `policy_error` isn't in the
  event-factory list that scrubs — worth filing as an open question).

#### `_apply_plan_state_changes_locked(plan)`

```python
if plan.set_turn_taking_mode is not None:
    state.set_turn_taking_mode(cast(TurnTakingMode, plan.set_turn_taking_mode))
if plan.set_turn_order is not None:
    state.set_turn_order(plan.set_turn_order)
```

`advance_turn_pointer` is read at turn-close time, NOT here.

#### `open_user_turn(user_event, plan) -> UserTurn`

Atomic under lock:

1. **Debounce**: if `not should_open_new_user_turn(_last_user_post_ts,
   now, debounce_ms)` AND there's an open turn:
   - Add `user_event.id` to `_user_turn.debounced_event_ids` (so
     actors with open obligations still wake on the new post —
     Session 4 priority 3 trigger uses this).
   - Update `_user_turn.last_activity_at = now`.
   - Update `_last_user_post_ts = now`.
   - Return existing turn (no new turn opens).
2. **Close any open turn** with reason `"new_user_post"`.
3. **Clear `wait_for_user`** if it was set — the user has spoken so
   the room may resume. Emits `floor_updated(wait_for_user=False)`.
4. **`make_user_turn`** (Session 1) — allocates obligation ids, sets
   `started_at`. Increments `_next_user_turn_id`, advances
   `_next_obligation_id`.
5. **`state.current_user_turn_id = turn.id`**.
6. **Emit `user_turn_opened`** with sorted required/optional ids,
   routing_case, rationale.
7. **Emit `obligation_recorded` per obligation** in the turn.
8. **Empty plan auto-close**: if `plan.required_participants` AND
   `plan.optional_participants` are both empty, close the turn
   immediately with reason `"no_responder"`.
9. Return turn.

#### `close_user_turn(reason="cancelled")`

Public closure entry. If `reason == "cancelled"`, resolve all open
obligations administratively (so closure checks see a clean turn).
Then `_close_user_turn_locked(reason)`.

#### `_close_user_turn_locked(reason)`

1. No-op if no turn or already closed.
2. Capture `plan = _user_turn.frozen_plan` (need it after close).
3. `_user_turn.close(reason)` (sets state="closed", closure_reason).
4. `state.current_user_turn_id = None`.
5. Emit `user_turn_closed(turn_id, reason)`.
6. **Apply `plan.wait_for_user_after`**: if set AND not already
   `wait_for_user`, set state.wait_for_user=True and emit
   `floor_updated(wait_for_user=True)`. **Fires for `cancelled`
   turns too** — the user explicitly stopped the floor.
7. **Apply `plan.advance_turn_pointer`**: if set AND mode is still
   `"round_robin"`, call `state.advance_round_robin_pointer()`.

#### `_maybe_close_user_turn_locked()`

Auto-closure check called from `unregister_participant` (after
transfer/resolve), `on_stream_end` (after resolution).

```python
ut = self._user_turn
if not ut or ut.state != "open": return
committed_count = len(ut.drafted)
cap = ut.frozen_plan.max_responses
cap_reached = cap > 0 and committed_count >= cap
if cap_reached or is_user_turn_complete(ut):
    self._close_user_turn_locked("completed")
```

Two close paths:

- **`is_user_turn_complete(ut)`** (Session 1): every `must` obligation
  resolved.
- **`cap_reached`**: `len(ut.drafted) >= plan.max_responses`. Closes
  the turn early even if optional participants haven't spoken. This is
  what enforces directed-turn `max_responses=1` without depending on
  every other participant emitting a clean `[PASS]`.

#### `check_idle_timeout(*, now=None)`

Called by actor thread when `wait_after` returns the timeout (Session
4 — `_loop` no-new-events branch).

```python
with self._lock:
    if not self._user_turn or self._user_turn.state != "open": return
    if self._user_turn.is_idle(idle_timeout_s=config.user_turn_idle_timeout_s, now=now):
        if self._user_turn.unresolved_required():
            self._close_user_turn_locked("obligation_unresolved")
        else:
            self._close_user_turn_locked("idle_timeout")
```

This is how idle-turn closure happens without a separate scheduler —
each actor's `wait_after` timeout doubles as a polling tick.
TODO comment in source: v0.1 may add retry / fallback synthesis for
unresolved required participants before closing.

### Obligation helpers

- **`obligation_for(holder, trigger_event_id=None) -> Optional[ResponseObligation]`**
  — read under lock; delegates to `_user_turn.obligation_for(holder)`.
  `trigger_event_id` is reserved for future per-mention obligations
  (informational today).
- **`_resolve_obligation_locked(obligation_id, *, by_event_id, expected_holder=None)`**:
  - **P3.2 / audit C2 defensive check**: if `expected_holder` is set,
    raises `ValueError` if the obligation belongs to a different
    participant. Today the only public path through `on_stream_end`
    already gates on `lease.holder` before reaching this helper, so
    today the check is a no-op assertion. Future callers that lose
    the holder check would otherwise resolve obligations for arbitrary
    participants.
  - `ut.mark_obligation_resolved(obligation_id, by_event_id)` (Session 1).
  - On success, emit `obligation_resolved(obligation_id,
    participant_id, resolved_by_event_id)`.

### Lease ledger

#### `acquire_lease(holder, trigger_event_id, *, is_direct_mention=False) -> Optional[TurnLease]`

The full rejection chain (any `None` return aborts):

```
1. no open turn                                       → None
2. holder not in state.participants                   → None
3. holder.active is False                             → None
4. allowed_speakers gate:
   if plan.allowed_speakers (non-empty):
       holder not in allowed AND not is_direct_mention → None
   else: (legacy fallback — empty allowed_speakers)
       has_obligation OR is_optional OR is_direct_mention → required
5. per-participant cap (only if not is_direct_mention):
   ut.speaker_counts.get(holder, 0) >= config.max_drafts_per_participant → None
6. per-turn max_responses cap (only if not is_direct_mention AND cap > 0):
   committed = len(ut.drafted)
   outstanding = sum(1 for L in _leases.values()
                       if L.user_turn_id == ut.id AND L.valid
                       AND L.holder not in ut.drafted)
   committed + outstanding >= cap → None
7. throttle.try_consume(holder, "main") → None on rejection
8. budget.can_acquire(ut.id) → None on rejection
9. allocate TurnLease with monotonic acquired_at + (acquired_at + lease_ttl_s)
   _leases[lease.id] = lease
   return lease
```

Key carve-outs for `is_direct_mention=True`:
- **Bypasses `allowed_speakers` gate** (the user explicitly addressed
  the holder; the policy's narrowing was implicit).
- **Bypasses per-participant `max_drafts_per_participant` cap** (a
  user can re-address the same agent multiple times in the same turn).
- **Bypasses per-turn `max_responses` cap** (mention is more specific
  than the floor narrowing).

Critically, throttle and budget are **NOT** bypassed even for direct
mentions — those are operator-installed safety rails.

`max_responses` enforcement (step 6) is the **v0.1.2 race fix**
(invariant 8 in `00-orientation.md`). Without counting outstanding
valid leases (whose holders haven't yet committed), two actors waking
on the same trigger could both pass step 6 and both commit, exceeding
the cap. By counting `committed + outstanding`, the second actor sees
itself reflected and is denied.

#### `validate_lease(lease) -> bool`

Under lock:
- `lease.valid` → False if not.
- `lease.id in _leases` → False if not.
- `lease.room_epoch != state.room_epoch` → set valid=False, return
  False. This is how membership/slot changes invalidate in-flight
  leases.
- `time.monotonic() > lease.expires_at` → set valid=False, return
  False. Wall-clock skew can't bypass this.
- Else `True`.

Streaming's per-chunk call is the canonical client (Session 3).

#### `release_lease(lease)`

Under lock: `_leases.pop(lease.id, None)`; `lease.valid = False`.
Idempotent. Called from the actor's `finally` block (Session 4).

### Stream / decision callbacks

#### `on_stream_end(lease, status, *, committed_text=None, cost_tokens=0, committed_event_id=None)`

The single point where actor activity resolves obligations. Called by
`streaming.run_streaming_call` after it emits its own `stream_end`
event on the bus (Session 3 invariant 44).

Under lock:

```python
self._budget.record(lease.user_turn_id, cost_tokens)   # ALWAYS — even rejected
ut = self._user_turn
if not ut: return

triggering = self._lookup_event(lease.trigger_event_id)
is_direct = bool(triggering and lease.holder in triggering.addressees)

if status == "committed":
    ut.mark_drafted(lease.holder, count_toward_cap=not is_direct)
    if committed_text:
        self._loop_guard.record(lease.holder, committed_text)
    ob = ut.obligation_for(lease.holder)
    if ob is not None:
        self._resolve_obligation_locked(ob.id,
            by_event_id=committed_event_id,
            expected_holder=lease.holder)
elif status == "passed":
    # PASS = valid completion, no draft mark, but obligation resolves.
    ob = ut.obligation_for(lease.holder)
    if ob is not None:
        self._resolve_obligation_locked(ob.id,
            by_event_id=None,
            expected_holder=lease.holder)
# suppressed/cancelled/error/lease_expired:
#   leave obligation intact (idle timeout will close as
#   "obligation_unresolved" if holder was required)

self._maybe_close_user_turn_locked()
```

Critical semantics (mirrors Session 3 status table):

- **Always record cost** (even rejected drafts cost tokens to
  generate).
- **`committed`**: draft mark (with `count_toward_cap=False` for
  direct mention so re-addressing the same agent doesn't burn cap),
  loop-guard record, obligation resolved with `committed_event_id`.
- **`passed`**: obligation resolved administratively (no draft mark,
  no loop-guard record). The PASS protocol's purpose: a required
  agent can decline cleanly without the turn idle-timing-out on them.
- **`suppressed`/`cancelled`/`error`/`lease_expired`**: obligation
  stays open. Required holders will trigger
  `"obligation_unresolved"` close at idle timeout.
- **Always check `_maybe_close_user_turn_locked`** at the end —
  any resolution might be the last one needed.

#### `handle_skip(holder, trigger_event=None)`

The SKIP path. Called by actor when `decide()` returns `"SKIP"` OR
when `acquire_lease` returns `None` (Session 4 invariant 56).

```python
with self._lock:
    ut = self._user_turn
    if not ut or ut.state != "open": return
    ut.last_activity_at = time.monotonic()
```

Soft no-op for state. Just bumps last_activity_at so empty-batch
wakeups don't immediately re-fire idle timeout. v0 has no debate path
that would need different SKIP semantics.

### Helpers

- **`_lookup_event(event_id) -> Optional[Event]`** — `bus.get(event_id)`.
- **`in_flight_lease_count() -> int`** — `len(_leases)` under lock.
  Useful for tests / observability.

---

## All control events the coordinator emits

For convenience, the complete list (every `bus.post_internal(...)`
call in this file):

| Event | Emitted from | When |
|---|---|---|
| `participant_added` | `register_participant` | New participant registered |
| `participant_removed` | `unregister_participant` | Participant removed |
| `default_responder_changed` | `set_default_responder`, `unregister_participant` (slot re-resolution) | Slot changed |
| `anchor_changed` (via `_control`) | `set_anchor`, `unregister_participant` | Slot changed |
| `chair_changed` (via `_control`) | `set_chair`, `unregister_participant` | Slot changed |
| `default_summarizer_changed` (via `_control`) | `set_default_summarizer`, `unregister_participant` | Slot changed |
| `topic_changed` | `set_topic` | Topic changed (also closes open turn first) |
| `roles_assigned` | `set_roles` | Roles changed |
| `floor_updated` | `set_floor_owner`, `open_user_turn` (clear wait_for_user), `_close_user_turn_locked` (set wait_for_user) | Floor or wait_for_user changed |
| `style_changed` | `set_style` | Style changed |
| `user_turn_opened` | `open_user_turn` | Turn opens |
| `user_turn_closed` | `_close_user_turn_locked` | Turn closes |
| `obligation_recorded` | `open_user_turn` (initial set), `_transfer_required_obligations_locked` (rerouting) | Obligation created |
| `obligation_resolved` | `_resolve_obligation_locked` (called from `on_stream_end`, `unregister_participant`, `close_user_turn(cancelled)`) | Obligation resolved |
| `dead_letter` | `_dead_letter_pending_mentions` | Pending mention to removed participant |
| `policy_slow` (via `_control`) | `_run_policy_under_lock` | `classify_fn` exceeded 100ms |
| `policy_error` (via `_control`) | `_run_policy_under_lock` | `classify_fn` raised |

The coordinator does **not** emit `chat`, `system`, `summary`, `topic`
(the kind, not the control_type), `presence`, `stream_*`,
`actor_error`, `journal_error`, `journal_corruption`,
`journal_truncated`, or `snapshot_dropped` — those originate elsewhere
(actors, runtime, journal).

---

## Invariants (this session's additions)

72. **`RoomCoordinator` is the single mutator of `RoomState`.** Every
    `state.add_*`/`set_*`/`remove_*`/`advance_*` call in the kernel
    is inside this file. Other modules may only call coordinator
    methods to mutate state.
73. **The coordinator uses an `RLock` (re-entrant), not a `Lock`.**
    Public methods may call each other (e.g.
    `unregister_participant` → `_close_user_turn_locked` →
    `_resolve_obligation_locked`); a non-re-entrant lock would
    deadlock.
74. **All bus emissions from coordinator use `post_internal`.** The
    coordinator may run on any thread (actor, runtime, test) and
    posts events with `sender="system"` or `"user"` regardless. This
    is one of the five documented `post_internal` call sites
    (Session 2 invariant 24).
75. **`post_user_event_and_open_turn` is atomic under lock** to fix
    the actor-cursor race: actors that wake on the user post block
    on `coordinator.user_turn` (which acquires the same lock) until
    the open completes, so they see the correct trigger context.
76. **Plan state changes (`set_turn_taking_mode`, `set_turn_order`)
    apply BEFORE the open check** — acknowledgement plans with mode
    flips (game-end phrase) still apply the change.
77. **`policy_error` is emitted in ALL three modes** ("close_turn",
    "default_responder", "raise"). The mode controls only the
    returned plan, not whether the event fires.
78. **`policy_slow` is observability only.** No interruption — Python
    can't safely cancel arbitrary code. Threshold: 100ms.
79. **`policy_error_mode="close_turn"` is the library default
    (fail-closed).** "default_responder" is a Loom-specific concept
    that breaks for debate / classroom / 20-questions policies.
    "raise" is dev mode only.
80. **Debounce returns the EXISTING turn** with the new event id
    added to `debounced_event_ids` and `last_activity_at` bumped.
    No new turn opens; the priority-3 trigger in actor's
    `_trigger_priority` checks `debounced_event_ids` so required
    participants still wake.
81. **`open_user_turn` clears `wait_for_user`** automatically — a
    user post is implicit consent for the room to resume. Emits
    `floor_updated(wait_for_user=False)`.
82. **Empty plan (no required, no optional) auto-closes with
    `"no_responder"`** at open time. Avoids hanging on a turn with
    nothing to wait for.
83. **`close_user_turn("cancelled")` resolves all obligations
    administratively before closing.** Other reasons leave
    obligations intact.
84. **`_close_user_turn_locked` applies `plan.wait_for_user_after`
    even on `cancelled` close** — the user explicitly stopped the
    floor.
85. **`advance_turn_pointer` is read at close time**, not at apply
    time, AND only fires if the mode is still `"round_robin"`. A
    plan that flips back to broadcast mid-turn won't advance the
    pointer.
86. **`check_idle_timeout` distinguishes `"obligation_unresolved"`
    from `"idle_timeout"`** based on whether any required
    obligation is still open.
87. **Turn auto-closes on `cap_reached` OR
    `is_user_turn_complete`** (whichever fires first). The
    `max_responses` cap is what enforces directed-turn `=1` without
    depending on every other participant emitting `[PASS]`.
88. **Direct user mention bypasses 3 lease gates**:
    `allowed_speakers`, `max_drafts_per_participant`, and
    `max_responses`. It does NOT bypass throttle or budget (those
    are operator-installed safety rails).
89. **`max_responses` is enforced at lease grant time** counting
    `committed + outstanding valid leases for this turn whose holder
    hasn't committed yet`. This is the v0.1.2 race fix (invariant 8
    in `00-orientation.md`).
90. **Lease bookkeeping uses `time.monotonic`** for `acquired_at` and
    `expires_at` (P3.3 / TIME1). Wall-clock skew cannot widen or
    shrink the validity window.
91. **Lease invalidation flips `lease.valid = False`** but does NOT
    remove from `_leases` — only `release_lease` does. A
    pop-then-re-validate by the actor would still see invalid (the
    `validate_lease` check covers this).
92. **`set_default_responder` invalidates EVERY lease**, not just
    leases held by the new responder. Any draft in flight may have
    been routing-dependent; invalidating all is the simple safe
    choice.
93. **`set_topic` closes any open turn** with `"topic_changed"`
    BEFORE mutating the topic. The new topic context shouldn't
    leak into an in-flight turn.
94. **`unregister_participant` performs 7 ordered steps** (transfer
    obligations → resolve own → dead-letter mentions → maybe-close).
    Mutating one before the other is a correctness bug.
95. **Only the FIRST must/should obligation transfers** in
    `_transfer_required_obligations_locked`. Multiple obligations
    held by a removed participant collapse onto one rerouted
    fallback.
96. **The transfer mutates the FROZEN `plan.allowed_speakers` set**
    to add the rerouted fallback. This is the **only legal mutation**
    of a "frozen" plan in the kernel — required so the new holder
    can pass the lease gate.
97. **`on_stream_end` always records cost** (even on rejected
    drafts). Rejected drafts still cost tokens to generate.
98. **`on_stream_end` `passed` resolves obligation but does NOT
    `mark_drafted`.** Speaker-count caps are unaffected by PASS.
    The `count_toward_cap=not is_direct` branch is `committed`-only.
99. **`on_stream_end` `suppressed/cancelled/error/lease_expired` leave
    obligation INTACT.** Required holders trigger
    `"obligation_unresolved"` at idle timeout.
100. **`_resolve_obligation_locked` accepts an optional
     `expected_holder` defensive guard** (P3.2 / C2). Today's only
     caller already gates on `lease.holder`; the guard catches
     future callers that lose the gate.
101. **`handle_skip` only bumps `last_activity_at`**. v0 has no
     debate path that would track skips for routing decisions.

---

## Verification

> **Q1: Trace `room.post_and_wait("hi @gpt")` through the
> coordinator: every control event emitted, which actor gets the
> lease, what closes the turn.**

Setup: 2-agent room with `gpt` and `claude`, both `OpenChatPolicy()`
(broadcast). Actually wait — the prompt says "hi @gpt" with a direct
mention. Let me use the `DefaultPolicy` for a more realistic trace
because OpenChat would ignore the mention and broadcast to both. With
`DefaultPolicy` and `state.default_responder_id="claude"`, here's
the trace.

The runtime layer constructs `user_event = chat(sender="user",
body="hi @gpt", addressees=["gpt"], …)` (the addressees are populated
by `parse_addressees` at user-post time — Session 2). Then it calls
`coordinator.post_user_event_and_open_turn(user_event,
classify_fn=lambda ev: policy.plan_user_turn(ev, state.view()))`.

**Inside the lock**:

1. `bus.post_internal(user_event)` — assigns `id=4` (assume earlier
   events occupied 0-3 from session start), `ts`. **Bus fires
   `notify_all`** → `gpt` and `claude` actors wake. They both call
   `coordinator.user_turn` to check if there's an open turn — and
   **block on `_lock`** because we're holding it. ✓ Race-free.
2. `_run_policy_under_lock(classify_fn, user_event)`:
   - `t0 = time.monotonic()`.
   - `classify_fn(user_event)` → `DefaultPolicy.plan_user_turn`
     classifies: `@gpt` is a direct mention to the participant
     `gpt`, so returns `plan_with_required(["gpt"],
     routing_case="direct_mention",
     target_event_ids=[4], reason="direct_mention",
     allowed_speakers={"gpt"}, max_responses=1,
     wait_for_user_after=True, instruction="...")`.
   - `__post_init__` validates `routing_case` (passes),
     `requires_response=True` requires non-empty `required` (passes),
     leaves `allowed_speakers` as set, leaves `max_responses=1`.
   - `elapsed_ms` is small; no `policy_slow` event.
3. `_apply_plan_state_changes_locked(plan)`: plan has no
   `set_turn_taking_mode` or `set_turn_order`; no-op.
4. `plan.routing_case == "direct_mention" != "acknowledgement"`, so
   call `open_user_turn(user_event, plan)`:
   - Debounce: assume it's been more than 250ms since last user
     post; new turn opens.
   - No prior open turn (first turn of session).
   - `wait_for_user` is False; nothing to clear.
   - `make_user_turn(turn_id=0, user_event_id=4, plan)`: allocates
     obligation `id=1` for `gpt`'s `must` obligation. Returns
     `(turn, next_oid=2)`. `_next_obligation_id = 2`. Turn:
     `state="open"`, `obligations={1: ResponseObligation(id=1,
     participant_id="gpt", level="must", target_event_ids=[4],
     reason="direct_mention", resolved=False)}`,
     `frozen_plan=plan`.
   - `state.current_user_turn_id = 0`.
   - **Emit `user_turn_opened(user_turn_id=0,
     routing_case="direct_mention", required_participants=["gpt"],
     optional_participants=[], rationale="...")`** → bus id 5.
   - **Emit `obligation_recorded(obligation_id=1,
     participant_id="gpt", level="must", target_event_ids=[4],
     reason="direct_mention")`** → bus id 6.
   - Plan has required participants → no auto-close.
   - Return turn.

**Lock released.** Both blocked actor threads now resume.

5. `claude`'s actor: `_decide_once` reads `bus.snapshot(audience="claude",
   since=cursor)`, gets events 4-6. Calls `decide(...)`.
   `_trigger_priority` for event 4 (chat from "user" with "gpt" in
   addressees): not directly mentioned to claude → not class 1. Not
   a dead_letter or transferred obligation → not class 2. Is
   `event.id == ut.user_event_id (4)`, but
   `ut.obligation_for("claude")` returns `None` → not class 3. None
   actionable. `decide` returns `SKIP("no actionable trigger")`.
   `_dispatch_decision` calls `coordinator.handle_skip("claude",
   trigger=event 4)` → bumps `last_activity_at`. Cursor advances to
   6.
6. `gpt`'s actor: same snapshot, same priority check.
   `_trigger_priority` for event 4: chat from user with "gpt" in
   addressees → **class 1**. `decide` returns
   `DRAFT(trigger_event_id=4, reason="direct_mention")`.
   `_dispatch_decision`: `is_direct=True`. Calls
   `coordinator.acquire_lease("gpt", 4, is_direct_mention=True)`:
   - Open turn ✓; gpt registered ✓; gpt active ✓.
   - Allowed-speakers gate: `gpt` in `{"gpt"}` ✓.
   - Per-participant cap (skipped due to `is_direct_mention`).
   - max_responses cap (skipped due to `is_direct_mention`).
   - `throttle.try_consume("gpt", "main")` ✓.
   - `budget.can_acquire(0)` ✓ (no usage yet).
   - Allocate `TurnLease(id=0, holder="gpt", user_turn_id=0,
     trigger_event_id=4, room_epoch=<current>,
     acquired_at=now, expires_at=now+60)`. `_next_lease_id=1`.
     `_leases[0] = lease`. Return.
7. `gpt`'s actor: `try: draft_handler(self, trigger=ev4, lease)`.
   In production this is the closure that calls `proxy = proxy_for("gpt")`,
   `prompt = build_prompt("gpt", ev4, coordinator, persona=…,
   capability_block=…, policy=DefaultPolicy())`, then
   `run_streaming_call(proxy, prompt, lease, bus, coordinator)`:
   - **Emit `stream_start(lease_id=0, participant_id="gpt",
     trigger_event_id=4)`** → bus id 7.
   - For each chunk: cost +=, `validate_lease` ✓, buffer/flush.
     Suppose total reply is `"hi! good to see you 👋"` — accumulates
     to 23 chars.
   - Phase 2 tail flush: `[PASS]` doesn't match (it's a real reply),
     so `bus.post(stream_delta(text="hi! good to see you 👋"))` →
     bus id 8.
   - Phase 3 filters: chair-speak strip (no chair-speak), empty
     check (non-empty), idle phrase check (not in list), loop_guard
     dup check (no prior reply from gpt) → status stays
     `"committed"`.
   - `parse_addressees("hi! good to see you 👋", ["gpt", "claude"],
     exclude="gpt")` → `[]` (no @-mentions in the reply body).
   - **Emit `chat(sender="gpt", body="hi! good to see you 👋",
     addressees=[], channel="main", user_turn_id=0,
     room_epoch=<lease.room_epoch>, meta={"lease_id": 0,
     "cost_tokens": 6})`** → bus id 9. `committed_event_id = 9`.
   - **Emit `stream_end(lease_id=0, participant_id="gpt",
     status="committed", error=None, committed_event_id=9)`** → bus
     id 10.
   - `coordinator.on_stream_end(lease, "committed",
     committed_text="hi! good to see you 👋", cost_tokens=6,
     committed_event_id=9)`:
     - `budget.record(0, 6)`.
     - Open turn exists.
     - `triggering` = event 4; `is_direct = "gpt" in [4].addressees`
       = True.
     - status="committed":
       - `ut.mark_drafted("gpt", count_toward_cap=not True =
         False)`. Adds to `drafted` set; speaker_counts unchanged.
       - `loop_guard.record("gpt", "hi! good to see you 👋")`.
       - `obligation_for("gpt")` → returns obligation 1.
       - `_resolve_obligation_locked(1, by_event_id=9,
         expected_holder="gpt")`:
         - `expected_holder` matches; check passes.
         - `mark_obligation_resolved(1, by_event_id=9)` → True.
         - **Emit `obligation_resolved(obligation_id=1,
           participant_id="gpt", resolved_by_event_id=9)`** → bus
           id 11.
     - `_maybe_close_user_turn_locked()`:
       - `committed_count = len(ut.drafted) = 1`.
       - `cap = plan.max_responses = 1`.
       - `cap_reached = 1 > 0 and 1 >= 1 = True`. **(Or
         `is_user_turn_complete(ut)` is also True since gpt's must is
         resolved.)**
       - `_close_user_turn_locked("completed")`:
         - `ut.close("completed")`.
         - `state.current_user_turn_id = None`.
         - **Emit `user_turn_closed(user_turn_id=0,
           reason="completed")`** → bus id 12.
         - `plan.wait_for_user_after = True` AND `wait_for_user`
           was False → set state.wait_for_user=True. **Emit
           `floor_updated(wait_for_user=True)`** → bus id 13.
         - `plan.advance_turn_pointer = False` → no rotation.
8. `gpt`'s actor: `finally: coordinator.release_lease(lease)`.
9. `room.post_and_wait` (in `loom/room.py`, Session 7) drains the
   bus until it observes `user_turn_closed(user_turn_id=0)`, then
   returns the projected `TurnResult` containing the one committed
   `Message`.

**Total events emitted (in order): user_event(4), user_turn_opened(5),
obligation_recorded(6), stream_start(7), stream_delta(8), chat(9),
stream_end(10), obligation_resolved(11), user_turn_closed(12),
floor_updated(13).** Lease holder: `gpt` (lease id 0). Closed by:
`max_responses=1` cap-reached AND obligation-complete (both true
simultaneously; either would have sufficed).

> **Q2: Deliberately broken case: policy throws — what events fire,
> in what order.**

Same setup, but suppose `DefaultPolicy.plan_user_turn` raises
`RuntimeError("classifier blew up")` instead of returning a plan.
`policy_error_mode = "close_turn"` (library default).

Inside `post_user_event_and_open_turn`:

1. `bus.post_internal(user_event)` → assigns id=4, notifies actors
   (still blocked on lock).
2. `_run_policy_under_lock`:
   - `t0 = time.monotonic()`.
   - `classify_fn(user_event)` raises `RuntimeError`.
   - `elapsed_ms = (time.monotonic() - t0) * 1000` — say 0.4ms.
   - **Emit `_control("policy_error",
     exception_class="RuntimeError", message="classifier blew up",
     elapsed_ms=0.4, user_event_id=4)`** → bus id 5.
   - `policy_error_mode == "close_turn"`: return
     `plan_for_acknowledgement(target_event_ids=[4],
     rationale="policy raised; turn closed (fail-closed)")`.
3. `_apply_plan_state_changes_locked(ack_plan)`: ack plan has no
   state changes; no-op.
4. `plan.routing_case == "acknowledgement"`: skip
   `open_user_turn`. **No `user_turn_opened`, no
   `obligation_recorded`, no turn opens.**
5. Return ack plan.

**Lock released.** Actors wake, snapshot since their cursor:
- Both see events 4 (user post) and 5 (policy_error).
- `coordinator.user_turn` is `None` (no turn was opened).
- `decide([ev4, ev5], my_id, user_turn=None)` → `SKIP("no open
  user_turn")`.
- Both actors call `handle_skip` → no-op (no open turn).
- Cursors advance.

**Total events emitted (in order): user_event(4), policy_error(5).**
No turn was opened, no draft happened, no closure event. The room
sits idle until the next user post. The runtime layer's
`post_and_wait` sees the acknowledgement-shaped plan returned from
`post_user_event_and_open_turn` and constructs an empty `TurnResult`
(no messages, `closed_reason="no_turn_opened"` projection).

If `policy_error_mode = "default_responder"` (Loom v0.0 compat
mode) and `state.default_responder_id = "claude"`, the same
`policy_error` event fires (id 5), but
`_run_policy_under_lock` returns
`plan_for_default("claude", reason="policy_error",
target_event_ids=[4], rationale="policy raised; falling back")`.
`_apply_plan_state_changes_locked` no-ops, then `open_user_turn`
fires: **`user_turn_opened(routing_case="none",
required_participants=["claude"], …)` (id 6),
`obligation_recorded(participant_id="claude", level="must", reason="policy_error")` (id 7)**.
The flow proceeds as normal from there with `claude` as the holder.

If `policy_error_mode = "raise"` (dev mode), `policy_error` (id 5)
emits, then `_run_policy_under_lock` re-raises. The exception bubbles
out of `post_user_event_and_open_turn` — the runtime layer's caller
(probably `room.post`) sees the exception. The lock releases via
`with` cleanup; no turn opens; subsequent state is consistent.

---

## Cross-references

- depends on: `00-orientation.md` (invariants 8, 9, the boundary
  test list), `01-kernel-primitives.md` (every dataclass it touches:
  `Event`, `RoomState`, `RoomConfig`, `RoomControlState`,
  `ParticipantInfo`, `UserTurnPlan`, `ResponseObligation`,
  `UserTurn`; helpers: `make_user_turn`,
  `is_user_turn_complete`, `should_open_new_user_turn`,
  `plan_for_default`, `plan_for_acknowledgement`),
  `02-kernel-bus.md` (`post_internal` privileged path,
  `bus.snapshot`, `bus.get`), `03-kernel-prompt-streaming.md` (the
  `validate_lease` per-chunk contract, `loop_guard.is_idle_dup`,
  `on_stream_end` semantics, status table), `04-kernel-actor-journal.md`
  (actor's `acquire_lease`/`release_lease`/`on_stream_end`/
  `handle_skip`/`check_idle_timeout` call sites; the `rerouted_from_*`
  reason recognised as priority-2 trigger).
- depended on by:
  - `loom/contracts.py` (Session 6) — `ConversationPolicy` ABC the
    coordinator's `classify_fn` parameter expects.
  - `loom/policy/*` (Session 6) — every bundled policy's
    `plan_user_turn` returns a `UserTurnPlan` the coordinator consumes;
    `RoundRobinPolicy.plan_user_turn` uses `set_turn_taking_mode`,
    `set_turn_order`, `advance_turn_pointer` declarative fields the
    coordinator applies via `_apply_plan_state_changes_locked` /
    `_close_user_turn_locked`.
  - `loom/runtime.py` (Session 7) — `build_loom_session` instantiates
    one `RoomCoordinator`, threads it into the `LoomSession` dataclass,
    wires the policy as `classify_fn`, registers the
    journal's `on_event` subscriber (the journal ↔ coordinator wiring
    is in runtime, not here).
  - `loom/room.py` (Session 7) — `LoomRoom.post_and_wait` drains the
    bus until it observes `user_turn_closed`; uses the projected
    `closed_reason` field on `TurnResult`.

## Open questions / things to revisit

1. **`policy_error.message` is NOT scrubbed via `redact_error_text`.**
   The `_control("policy_error", message=str(exc)[:500], …)` path
   bypasses the scrubbing that `journal_error`/`actor_error` get
   (Session 1). A policy that raises `ValueError("API key sk-…
   bad")` would leak a partial key into the bus + journal. Worth
   adding a scrub at the emission site or building a
   `policy_error()` factory like the other event types.
2. **Mutating `frozen_plan.allowed_speakers`** in
   `_transfer_required_obligations_locked` is the only legal
   mutation of a "frozen" plan. The "frozen" framing is the
   programmer-facing contract; the actual mutation is necessary
   here because the plan was scoped before the removal happened.
   Consider documenting this exception more loudly OR introducing a
   `mutable_plan_overlay` pattern that doesn't require touching the
   frozen object.
3. **Only the FIRST must/should obligation transfers** in
   `_transfer_required_obligations_locked`. If an agent held two
   `must` obligations targeting different user events, only one is
   rerouted; the other resolves administratively. This is an
   intentional simplification but worth flagging — multi-mention
   plans (which the v0 deterministic interpreter doesn't produce
   today, but a future LLM-classified plan might) would lose
   obligations on removal.
4. **Lease ledger growth** — `_leases` only shrinks on
   `release_lease` (called from actor's finally block). If
   `release_lease` somehow fails to be called (actor thread killed
   mid-draft), the lease entry persists with `valid=False` until
   process restart. Today no path leaks like this; future async
   handlers may need an explicit reaper.
5. **`set_default_responder` invalidates ALL leases.** The blast
   radius is broader than necessary — only leases routing-dependent
   on the default responder slot would actually need invalidation.
   Today the simplification is fine because direct-mention turns
   typically only have one lease anyway.
6. **`check_idle_timeout` polling depends on actors waking up.**
   With `wakeup_timeout_s = min(idle_timeout_s, lease_ttl_s)`
   (Session 4), the actor's `wait_after` returns at most every
   ~20s. If all actors are removed mid-turn, no one polls
   `check_idle_timeout` and the turn would never close. Today
   `unregister_participant` triggers a closure check via the
   `_maybe_close_user_turn_locked` cascade, so this is moot. But
   it's a subtle dependency.
7. **`_compaction_in_flight: bool = False`** is initialized but
   never used. Placeholder for future compaction logic. Remove or
   wire up in our v0.2 cleanup.
8. **`_run_policy_under_lock` runs `classify_fn` with the
   coordinator lock held**. This is intentional (race fix) but
   means a slow policy blocks every public method on the
   coordinator, including `acquire_lease`. The 100ms `policy_slow`
   threshold is the operator's signal to investigate. Async / off-
   lock policies are v0.2 work — they'd require separating the
   "open the turn" lock from the "run the policy" execution.
9. **`on_stream_end` always records cost** but the cost on rejected
   drafts (suppressed/cancelled/error/lease_expired) was generated
   by the LLM. Charging the budget for a rejected draft is correct
   (the tokens were spent), but it does mean a flapping policy can
   eat the budget without producing visible drafts.
10. **`obligation_for(holder, trigger_event_id=None)` ignores
    `trigger_event_id`**. The parameter is "reserved for future
    disambiguation" per the docstring. v0 deterministic interpreter
    emits one obligation per pid per turn; future per-mention
    obligations would use this parameter.
11. **`handle_skip` accepts `trigger_event=None`** but never reads
    it. Defensive in case future code wants to log skip-triggers
    for diagnostics.
12. **Empty-plan auto-close emits `"no_responder"`** at open time.
    This means the close-event sequence for an empty plan is:
    `user_turn_opened` → (no obligations) → `user_turn_closed("no_responder")`
    → `floor_updated` (if `wait_for_user_after`). Three events for
    nothing. Could short-circuit to skip the open entirely, but the
    current order keeps the event log consistent ("every turn has an
    open and a close").
