# 02 — Bus + addressees

This is **Session 2** of the Loom kernel deep-study curriculum. The bus
is the single source of truth — every actor reads via
`snapshot()`/`wait_after()` and posts back via `post()`. Addressees are
the @-mention parser used at two distinct lifecycle points.

State as of Loom v0.1.2 (2026-05-08).

## Files covered

| File | LOC | Role | Imports from kernel |
|---|---:|---|---|
| `loom/kernel/bus.py` | 486 | `MessageBus`: thread-safe append-only event ledger + pub/sub | `events` only |
| `loom/kernel/addressees.py` | 71 | `_MENTION_RE`, `parse_addressees`, `last_responsible_speaker` | `bus` only |

`addressees.py` imports `MessageBus` (for `last_responsible_speaker`),
which is why these two files are this session's pair: nothing else can
sit between them.

## Mental model

```
                   one global Condition (self._cond)
                              │
   ┌──────────────────────────┴──────────────────────────────┐
   │   MessageBus internal state — all guarded by _cond     │
   │                                                          │
   │     _log: list[Event]   (append-only, ev.id == index)   │
   │     _subscribers: tuple[Callable[[Event], None], ...]   │
   │     _thread_actors: dict[thread_id, actor_id]  (P1)     │
   │     _stopped: bool                                      │
   └──────────────────────────────────────────────────────────┘
                              │
   POSTERS                    │                     READERS
   (any thread)               │                     (any thread)
                              │
   post(ev)  ──► sender check ──► _post_unchecked       wait_after(idx, timeout)
        │              │                │                       │ blocks on _cond
        │       (raises               body cap                  ▼
        │   SenderMismatchError)        │              returns len(_log) when
        │                               ▼                len > idx OR stopped
   post_internal(ev) ─► (skip check) ──► append id+ts        OR timeout
                                          │
                                          ▼               snapshot(since=cursor,
                                       notify_all          channel=…, audience=…,
                                          │                kinds=…)
                                          ▼
                                  for cb in subs:    ───►  get(event_id)  O(1)
                                      try cb(ev)
                                      except: pass    ───►  render_chat_line(...)
                                                              render_control_line(...)
                                                              (memoized)

   bind_actor(id) ─► returns unbind handle. While bound, post() requires
                     ev.sender == id; mismatch raises SenderMismatchError.

   stop() ─► sets stopped, notify_all. After stop: post → -1, wait_after
            returns immediately, subscribers NOT notified by stop itself.
```

Memo of "single source of truth": `ev.id` *is* the position in `_log`.
That equality is exploited by `get` (O(1) lookup), `snapshot(since=…)`
(slice not scan), and the journal (line N of `events.jsonl` ≈ event id
N) — Session 4 will use it heavily.

---

## bus.py — full reference

### Module-level

```python
SubscriberCallback   = Callable[[Event], None]
UnsubscribeHandle    = Callable[[], None]
UnbindHandle         = Callable[[], None]

DEFAULT_MAX_BODY_BYTES = 256 * 1024   # 256 KB (RES4 / P2.1)
```

### Errors

- **`BodyOversizeError(LoomError, ValueError)`** — raised by
  `MessageBus.post` when a `chat` / `system` / `summary` event's string
  body exceeds `max_body_bytes`. Stream-delta bodies are NOT capped here
  (bounded at the proxy boundary instead). The cap is applied in
  `_post_unchecked`, so privileged callers (`post_internal`, replay,
  coordinator) get the same defence.
- **`SenderMismatchError(LoomError, ValueError)`** — raised by
  `MessageBus.post` when a thread bound to actor `X` posts an event
  with `sender != X`. Catches three concrete forgery vectors from the
  C1 audit:
  - **C1.a**: bound thread tries to post `stream_end` carrying another
    lease's `lease_id`/`participant_id`.
  - **C1.b**: bound thread tries to post a control event with
    `sender="system"`.
  - **C1.c**: bound thread tries to post a chat event with
    `sender="user"`.
  Privileged callers bypass via `post_internal`. Unbound threads (test
  code, runtime entry, coordinator) skip the check entirely.

### `visible_to(ev, audience) -> bool`

The DM-privacy filter, applied at the actor/prompt boundary (NOT
in-process access control — anyone with a `MessageBus` reference can
call `snapshot()` without `audience` and see every DM).

| `ev.channel` | Visible to |
|---|---|
| `"main"` | everyone |
| `"dm:<target>"` | `target`, `"user"`, `"system"` |
| anything else | nobody (defensive) |

### `class MessageBus`

#### `__init__(*, max_body_bytes=DEFAULT_MAX_BODY_BYTES)`

Initialises:
- `_cond: threading.Condition` — **the** lock (everything is guarded by it)
- `_log: list[Event]` — append-only
- `_max_body_bytes: int` — body cap (default 256 KB; runtime can raise to ~4 MB for high-context use)
- `_subscribers: tuple[SubscriberCallback, ...]` — **immutable tuple**, rebuilt on subscribe/unsubscribe
- `_stopped: bool`
- `_render_chat_main_cache: dict[int, str]` — keyed by `ev.id`
- `_render_chat_dm_cache: dict[int, str]`
- `_render_control_cache: dict[int, str]`
- `_thread_actors: dict[int, str]` — thread_ident → bound actor_id

#### Posting

- **`post(ev) -> int`** — public posting path.
  1. Check `_thread_actors[get_ident()]`; if bound and `ev.sender !=
     bound`, raise `SenderMismatchError`.
  2. Defer to `_post_unchecked(ev)`. Returns `ev.id` after assignment, or
     `-1` if bus stopped.
- **`post_internal(ev) -> int`** — **privileged**: skips sender check.
  Same body cap + lock + notify + subscriber-iteration semantics
  otherwise. Documented call sites:
  - **Coordinator** — emits control events with `sender="system"`.
  - **Runtime** — posts user input with `sender="user"`.
  - **Journal replay** — re-injects events with their original sender;
    `Event.from_jsonl` already validated the disk content.
  - **Journal failure callback** — posts `journal_error` with
    `sender="system"` from whichever thread tripped the failure (which
    may be a bound actor thread).
  - **Actor crash handler** — posts `actor_error` with `sender="system"`
    from the actor's own bound thread.
  Calling from non-kernel code is permitted but the name makes
  review-time auditing easy.
- **`_post_unchecked(ev) -> int`** — the core append path.
  1. **Body cap** (chat/system/summary only): if `body` is `str` and
     `len(body) > _max_body_bytes`, raise `BodyOversizeError`. Stream
     deltas exempt.
  2. Acquire `_cond`. If `_stopped`, return `-1`.
  3. Assign `ev.id = len(_log)` (so `id == position`); assign
     `ev.ts = time.time()` (wall-clock, NOT monotonic — see Session 1
     Invariant 14).
  4. `_log.append(ev)`.
  5. `_cond.notify_all()` — wakes every `wait_after`.
  6. Iterate `_subscribers` (the immutable tuple snapshot taken at
     lock-acquire time). Each callback runs **inline, on the poster's
     thread, under the bus lock**. Exceptions silently swallowed.
  7. Return `ev.id`.

#### Sender authentication (P1)

- **`bind_actor(actor_id) -> UnbindHandle`** — bind current thread.
  Re-binding to the **same** id is a no-op; re-binding to a **different**
  id raises `RuntimeError` (treated as a thread-reuse bug). Returns a
  callable that idempotently unbinds (only removes if still bound to
  this actor — defensive against double-unbind in `finally`).
- **`unbind_actor()`** — idempotent; convenience. The handle from
  `bind_actor` is the preferred call style in `finally`.
- **`bound_actor_for(thread_ident=None) -> Optional[str]`** — defaults
  to current thread.

#### Reading

- **`wait_after(idx, timeout=None) -> int`** — blocks on `_cond` until
  `len(_log) > idx` OR `_stopped` OR `timeout`. Returns the current
  `len(_log)` after waking. **Returns even on stop without new events**
  — callers must re-check `stopped` (or `since` cursor result) to know
  why they woke.
- **`snapshot(*, channel=None, audience=None, kinds=None, since=None) -> list[Event]`**
  — filtered slice.
  - `channel` — restrict to events on this exact channel string.
  - `audience` — drop events the audience cannot see (`visible_to`
    filter; combinable with `channel`).
  - `kinds` — restrict to events whose `kind` is in the iterable.
  - `since` — drop events with `id <= since`. **Performance-critical**:
    because `ev.id == position in _log`, `since` collapses to a slice
    `_log[since+1:]` — O(E - since) instead of O(E) full copy +
    post-filter. **This is the dominant per-actor path** (every wakeup
    calls `snapshot(since=cursor)`).
  - Filters apply *after* the slice; ordering: channel → audience →
    kinds.
- **`get(event_id) -> Optional[Event]`** — O(1) by position. Cheap
  alternative to `snapshot` + scan when looking up a single id.
- **`render_chat_line(ev, *, scope) -> str`** — cached JSON render for
  prompt assembly. Memo key: `(ev.id, scope)` where `scope ∈ {"main",
  "dm"}`. **The memo dicts are written WITHOUT holding the bus lock**
  — single-key dict ops are GIL-atomic, and a race producing two
  identical strings then storing the same key is harmless. Keeps the
  prompt-build hot path off the bus lock.
- **`render_control_line(ev) -> str`** — same shape, keyed by `ev.id`
  only (one render shape per control event). Drops the inner
  `control_type` from `body` to avoid duplication (it's promoted to a
  top-level field in the render).
- **`__len__() -> int`** — `len(_log)` under lock.

#### Subscriptions

- **`subscribe(callback) -> UnsubscribeHandle`** — append to
  `_subscribers` tuple (rebuilt under lock). Returns idempotent
  unsubscribe (`list.remove(callback)` semantics — removes at most one
  matching entry).

#### Lifecycle

- **`stop()`** — sets `_stopped=True` and `notify_all()`. After:
  - `post`/`post_internal` return `-1`.
  - `wait_after` returns immediately.
  - **Subscribers are NOT notified by `stop()` itself.** If you want a
    final event delivered, post it before stopping.
- **`stopped`** (property) — under lock.

### Threading-model summary

| Resource | Lock | Mutator(s) | Reader(s) |
|---|---|---|---|
| `_log` | `_cond` | `_post_unchecked` only | `wait_after`, `snapshot`, `get`, `__len__` |
| `_subscribers` | `_cond` for write; iteration is on a stable tuple captured at post-time | `subscribe`, returned unsubscribe | `_post_unchecked` |
| `_stopped` | `_cond` | `stop` | `_post_unchecked`, `wait_after`, `stopped` property |
| `_thread_actors` | `_cond` | `bind_actor`, `unbind_actor`, returned unbind handle | `post`, `bound_actor_for` |
| `_render_*_cache` | **none** | `render_chat_line`, `render_control_line` | same | (relies on GIL atomicity; idempotent races OK) |

A single condition variable does duty as the lock AND the wakeup
signal. There are no per-actor queues — every wakeup re-reads the log
from the cursor.

---

## addressees.py — full reference

### Module-level regex

```python
_MENTION_RE = re.compile(r"@([A-Za-z][\w-]*)")
```

- Anchor: `@`
- Capture group: `[A-Za-z]` (must START with a letter) + `[\w-]*`
  (word chars or hyphen, repeating). `\w` includes underscore + digits.

So:
- `@gpt`, `@claude-3`, `@gpt_v2`, `@Bob` → match.
- `@123`, `@_foo`, `@-x` → **no** match (must start with a letter).
- `email@example.com` → captures `@example` (could be a participant id).

The regex is **module-level so tests can monkeypatch it**. Don't inline
it inside the functions.

### `parse_addressees(text, addressable, *, exclude=None) -> list[str]`

Order-preserving, deduplicated, pool-filtered list of @-mentioned ids.

Algorithm:
1. Build `pool = set(addressable)` (O(1) membership).
2. Iterate `_MENTION_RE.findall(text)` (preserves text order).
3. Skip if `m == exclude` (self-mention) OR `m not in pool` OR
   `m in seen` (already added).
4. Add to `seen`; append to `out`.

Used at TWO lifecycle points:

| When | Caller | Purpose |
|---|---|---|
| **User-post time** | runtime, before any policy classification | Populate `Event.addressees` so visibility filters (`visible_to`) and `is_direct_mention` work. |
| **Draft-commit time** | `loom.kernel.streaming` | Decorate the agent's reply with implicit @-mentions (so a follow-up reply that names another participant in prose is also routed). |

The same parser at both points means a participant who writes "I think
@bob has a point" gets `bob` into the chat event's `addressees` list,
which `is_direct_mention(ev, "bob")` then picks up to wake bob.

### `last_responsible_speaker(bus, *, channel="main", exclude_user=True) -> Optional[str]`

Walks `bus.snapshot(channel=channel, kinds=["chat"])` in reverse and
returns the first chat sender that is **not** `"user"` (when
`exclude_user`) and **not** `"system"`. Returns `None` if no eligible
chat exists yet.

- Used by the runtime to thread `prior_speaker` into the policy's
  `plan_user_turn` (signature stability — the v0 deterministic
  classifier ignores it; UI consumers and a future LLM-backed
  classifier can use it).
- Cost: one full `snapshot(channel, kinds)` per call. For long-running
  rooms this is non-trivial; consider `since=` cursors if perf becomes
  a problem (currently fine because it's only called once per user
  post).

---

## Invariants (this session's additions)

20. **`ev.id == position in _log`.** Assigned in `_post_unchecked`
    under the bus lock. Code anywhere may rely on this:
    `bus.get(id)` is O(1), `snapshot(since=k)` is a slice, replay
    expects line-N to deserialise to id-N (Session 4 will exploit).
21. **Subscribers run inline, under the bus lock, on the poster's
    thread.** A subscriber that blocks for N ms freezes every
    posting/reading thread for N ms. Subscriber exceptions are
    silently swallowed but **subscriber latency is not bounded**.
    Long-running subscriber work belongs on a background thread (the
    journal's snapshot writer is the canonical example).
22. **`_post_unchecked` is the chokepoint.** Any change to event
    posting (additional auth, throttling, observability) goes here so
    `post` and `post_internal` both inherit it.
23. **Body cap applies before lock acquisition.** `BodyOversizeError`
    is raised before `_cond` is taken — a hostile body cannot DoS the
    bus by parking on the lock.
24. **`post_internal` is the kernel's privileged escape hatch.** Use
    only from documented kernel-internal call sites (coordinator,
    runtime, journal replay/failure callback, actor crash handler).
    Adding a new privileged call site is a change worth flagging in
    review.
25. **Re-binding a thread to a different actor raises
    `RuntimeError`.** This catches thread-reuse bugs at the bind
    point rather than silently corrupting sender identity.
26. **`stop()` does not notify subscribers.** If you want a "session
    closed" subscriber event, post it BEFORE calling `stop()`.
27. **DM privacy is enforced at the actor/prompt boundary, not at the
    bus.** Anyone with a `MessageBus` reference can call `snapshot()`
    without `audience` and see every DM. Process-level isolation
    requires routing callers through audience-gated facade methods.
    `LoomSession.bus` is currently public — D3 audit finding,
    deferred to v0.2.
28. **The render memo is keyed by `(ev.id, scope)` for chat and
    `ev.id` for control.** If a future change introduces
    audience-specific render variants for chat, the cache key MUST
    include audience or DMs will silently misroute (audit D2).
29. **`_subscribers` is an immutable tuple.** subscribe/unsubscribe
    rebuild it under the lock. The post loop iterates the snapshot
    captured at lock-acquire time, so concurrent subscribe doesn't
    add a callback to the in-flight notification round.
30. **`Event.id` and `Event.ts` are bus-mutated.** Construct events
    with `id=0`, `ts=0.0`; the bus writes both in `_post_unchecked`.
    This is why `Event` cannot be `frozen=True` (Session 1 Invariant
    13).

---

## Verification

> *Explain how two actors waking on the same event don't produce two
> responses (cursor + lease + subscriber-callback interaction).*

Two actor threads may both be parked in `bus.wait_after(cursor)` on
the same `MessageBus._cond`. When a third thread calls `bus.post(ev)`,
`_post_unchecked` does — under the lock — `_log.append(ev)` and then
`notify_all()`. **Both** waiters wake. Each calls
`bus.snapshot(since=cursor)` and **each sees the new event** with id =
its own cursor + 1. So the bus alone gives both actors visibility of
the event.

The bus does not, by itself, prevent two responses. What stops them is
**not** at the bus layer — it's the coordinator's lease (Session 5).
Specifically:

1. Each actor's wakeup loop classifies the new event into a trigger
   (e.g. "user post addressed to me"), then calls
   `coordinator.acquire_lease(self.id, trigger_event_id, …)`.
2. The coordinator holds its own lock across `policy.plan_user_turn`
   and lease bookkeeping. It enforces `max_responses` from the
   `UserTurnPlan` and counts already-committed drafts plus
   outstanding valid leases for the turn (the v0.1.2 race fix —
   invariant 8 in `00-orientation.md`). For a directed turn with
   `max_responses=1`, the second `acquire_lease` call returns
   `None`/denied, so that actor never enters the streaming path.
3. The actor's other gating: `participant_is_eligible(turn,
   self.id)` (Session 1) — pids not in `required ∪ optional` and not
   directly mentioned aren't eligible at all.
4. **Subscribers** (the third party in the bus's `notify_all` round)
   are not actors; they're things like the journal writer, the
   notify printer, the cost tracker. They don't compete for leases —
   they just observe every event. Their inline execution under the
   bus lock means actor wakeups and subscriber dispatch are
   interleaved deterministically (subscribers always run before the
   post call returns).

Summary in one sentence: **the bus broadcasts** (via `notify_all` +
`snapshot(since=cursor)`); **the coordinator gates** (via lease grant
+ `max_responses` + `allowed_speakers`). Two actors seeing the same
event is normal and expected; two actors *committing a draft* is what
the lease prevents.

---

## Cross-references

- depends on: `00-orientation.md` (glossary, invariants),
  `01-kernel-primitives.md` (Event mutation contract, `is_direct_mention`,
  `addressees`, `RoomConfig.pass_buffer_chars`).
- depended on by:
  - `prompt.py` (Session 3) — uses `bus.render_chat_line`,
    `bus.render_control_line`, `bus.snapshot(channel, audience, kinds,
    since)` to build LLM context.
  - `streaming.py` (Session 3) — uses `bus.post` for stream events;
    `parse_addressees` at draft-commit time; bound to actor identity.
  - `actor.py` (Session 4) — the canonical `wait_after` + `snapshot`
    consumer; calls `bind_actor` in `_loop`'s entry / unbinds on exit.
  - `journal.py` (Session 4) — registers as a subscriber (so
    `events.jsonl` mirrors the bus); uses `post_internal` for
    `journal_error`, `journal_corruption`, `journal_truncated`,
    `snapshot_dropped`.
  - `coordinator.py` (Session 5) — uses `post_internal` for ALL
    coordinator-emitted control events (`user_turn_opened/closed`,
    `obligation_recorded/resolved`, `dead_letter`, `policy_error`,
    `policy_slow`, slot-change events, etc.). Calls
    `last_responsible_speaker` to thread `prior_speaker`.
  - `loom/runtime.py` (Session 7) — uses `post_internal` for the
    user post (sender="user").

## Open questions / things to revisit

1. **Subscriber off-thread dispatch with timeout** — CON1 audit finding,
   v0.2 work. Today, a buggy subscriber that doesn't raise but does
   block for 10s freezes every actor for 10s. The contract is
   documented; the implementation isn't here yet. When tackled,
   touches `_post_unchecked` + likely a per-subscriber thread pool +
   timeout-and-evict policy.
2. **Render-memo unbounded growth** — `_render_*_cache` are bounded
   only by what's in `_log`. A multi-day session with no compaction
   accumulates O(events) memo strings. A future log-compaction pass
   should prune the memos in lockstep.
3. **Render-memo audience leak risk** — if a future change adds an
   audience-specific render variant for chat, the cache keys must
   incorporate audience. Audit D2 explicitly flags this.
4. **`LoomSession.bus` is a public attribute** (audit D3, deferred to
   v0.2). DM privacy is enforced at the prompt-build boundary; any
   in-process caller can call `bus.snapshot()` without an audience
   filter and see every DM. If we tighten this in v0.2 via facade
   methods, it touches `loom/runtime.py` and probably `loom/room.py`.
5. **Body cap is per-event.** A flood of 256-KB chat events still
   lets a hostile actor consume gigabytes of bus log + journal lines
   over time. Per-actor / per-turn rate limiting (RES7-class) is
   roadmapped under "per-message rate limiting + per-participant
   cost budgets" (Session 0 v0.2 list).
6. **Sender authentication is post-time only.** A malicious posthoc
   modification of `_log[i]` (in-process attacker with arbitrary
   Python) defeats every defence. The threat model excludes this
   (Session 0 — "Python-runtime exploits / sandbox escape").
7. **`addressees.py` regex tolerates email-address fragments.**
   `email@example.com` matches `@example`. If `example` is a
   participant id, that's an unintentional address. If the room
   admits real-world prose with embedded emails, consider tightening
   the regex (require leading whitespace or BOL).
8. **`last_responsible_speaker` re-snapshots every call.** O(events) per
   call. Today only called on user-post; if we ever call it per-stream
   or per-actor wakeup it needs a `since=` cursor or precomputed
   per-channel last-speaker map.
