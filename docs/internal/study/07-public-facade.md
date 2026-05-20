# 07 — Public facade & runtime

This is **Session 7** of the Loom kernel deep-study curriculum. Phase
C concludes with the **public glue**: how `LoomRoom` and
`build_loom_session` wire kernel + policy + agents into a runnable
system, what the projection layer (`Message`, `TurnResult`) hides,
and what test scaffolding ships for adapter / policy authors.

This is the surface library users actually touch. Everything below
this layer (kernel + policy + adapters) is implementation detail
relative to a consumer importing `from loom import LoomRoom`.

State as of Loom v0.1.2 (2026-05-08).

## Files covered

| File | LOC | Role | Imports from kernel |
|---|---:|---|---|
| `loom/__init__.py` | 129 | Public surface re-exports (15 primary + 4 advanced symbols) | `kernel.room.RoomConfig/RoomStateView` (re-exports) |
| `loom/room.py` | 686 | `LoomRoom` facade; `_agent_to_wiring`; `_warn_on_typoed_agent_attrs` | `events`, `obligations`, `room` |
| `loom/runtime.py` | 840 | `LoomSession`, `build_loom_session`, `run_loom_console`, `handle_slash_command`, `post_user_text`, `_make_draft_handler`, `_format_control`, `_make_console_subscriber`, `ParticipantWiring`, `SlashResult` | `events`, `actor`, `addressees`, `bus`, `coordinator`, `journal`, `obligations`, `prompt`, `room`, `streaming` (the runtime is THE place that imports nearly every kernel module) |
| `loom/messages.py` | 138 | `Message`, `TurnResult`, `TurnClosedReason`, `_project_closure_reason` | `events` (only) |
| `loom/errors.py` | 86 | `LoomError` base + lazy `__getattr__` re-exports of typed exceptions | (none at runtime; `TYPE_CHECKING` only) |
| `loom/testing.py` | 366 | `make_test_state`, `make_test_event`, `FakeProxy`, `assert_no_state_mutation`, `RecordReplayProxy`, `ParticipantSpec` | `events`, `room` |

`loom/runtime.py` is the **only kernel-aware top-level module by
necessity** — it composes the bus, coordinator, journal, actors, and
streaming/prompt callbacks. Everything else in `loom/*` either consumes
the runtime through the `LoomRoom` facade or stays at the
`Message`/`TurnResult` projection layer.

## Mental model

```
                    ┌───────────────────────────────────────┐
                    │ User code                              │
                    │   from loom import LoomRoom, …         │
                    └─────────────────┬─────────────────────┘
                                      │
                                      ▼
              ┌───────────────────────────────────────────────────┐
              │ loom/__init__.py — public surface                  │
              │   re-exports: LoomRoom, Agent, agent_from_*,       │
              │   ConversationPolicy, DefaultPolicy/OpenChat/…,    │
              │   RoomConfig, RoomStateView, Message, TurnResult,  │
              │   LoomError, ParticipantWiring, SendProxyAdapter,  │
              │   build_loom_session, run_loom_console             │
              └───────────────────────┬───────────────────────────┘
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        ▼                             ▼                             ▼
   ┌─────────┐                  ┌──────────┐                  ┌────────┐
   │LoomRoom │ — facade ─────►  │LoomSession│ ◄── wiring ──── │run_loom_console│
   │room.py  │  (the door)      │runtime.py │                  │runtime.py│
   └────┬────┘                  └─────┬────┘                  └────────┘
        │                             │
        │  _agent_to_wiring           │ build_loom_session(wirings, …):
        │  (Agent → Wiring)            │   - bus, state, coord
        │  warns on typos             │   - DefaultPolicy if policy=None
        │  uses .stream as proxy      │   - Journal (if dir given) + 3 callbacks
        │  else SendProxyAdapter      │   - register all participants
        │                             │   - validate anchor/responder ids exist
        │  post_and_wait(text, …):    │   - construct ParticipantActors
        │   - snapshot bus length     │   - if auto_start: start actors
        │   - post_user_text(…)       │
        │   - check coordinator.user_turn:
        │       None or mismatch → no turn opened, return ack TurnResult
        │   - loop bus.wait_after(last_seen, timeout)
        │       until turn closes or timeout
        │   - collect chat events for this turn → Message[]
        │   - project closure_reason via _CLOSURE_REASON_MAP
        │   - return TurnResult                                    │
        │                                                          │
        ▼                                                          ▼
   ┌─────────────┐                                           ┌────────────┐
   │ kernel +    │  (Sessions 1–5)                           │ Slash cmds │
   │ policy +    │  (Session 6)                              │  /topic    │
   │ adapters    │                                           │  /who etc  │
   └─────────────┘                                           └────────────┘

   Projection layer (loom/messages.py):
       Message ◄── from_event(ev)                Event hides ─┐
       TurnResult (iterable, len(), bool(), [])               │
                                                              │
   Test scaffolding (loom/testing.py):                        │
       make_test_state, make_test_event, FakeProxy,           │
       RecordReplayProxy, assert_no_state_mutation            │

   Errors (loom/errors.py):                                   │
       LoomError base + lazy re-export of typed kernel errors │
       (avoid circular: kernel imports LoomError from here)   │
```

---

## loom/__init__.py — public surface (recap)

Already covered in `00-orientation.md`. To recap:

**Primary** (15 symbols): `LoomRoom`, `Agent`, `agent_from_send` /
`agent_from_stream` / `agent_from_object`, `ConversationPolicy`,
`DefaultPolicy` / `OpenChatPolicy` / `SingleResponderPolicy` /
`RoundRobinPolicy`, `RoomConfig`, `RoomStateView`, `Message`,
`TurnResult`, `LoomError`.

**Advanced** (4 symbols): `ParticipantWiring`, `SendProxyAdapter`,
`build_loom_session`, `run_loom_console`.

`__all__` enumerates exactly these 19. Anything else in `loom.*` (e.g.
`loom.contracts`, `loom.adapters._FunctionAgent`,
`loom.runtime.LoomSession`) is implementation detail and may shift.

---

## loom/room.py — `LoomRoom`

The single canonical door for library users. All the moving parts of
`LoomSession` are wrapped behind a small surface with sensible
defaults.

### Module-level

- **`_NOTIFY_LOCK = threading.Lock()`** — module-level lock so the
  default `notify` doesn't shear streamed output across actor
  threads.
- **`_KNOWN_OPTIONAL_AGENT_ATTRS`** — tuple of recognised optional
  Agent attribute names (`persona`, `capability_block`, `cost_tier`,
  `capable`, `cancel`, `id`, `stream`, `send`). Used by typo
  detection.
- **`_thread_safe_print(msg)`** — default `notify`. Wraps `print` in
  `_NOTIFY_LOCK`.
- **`_default_prompt()`** — `input("you ▸ ")`.

### `_warn_on_typoed_agent_attrs(agent)` — F3.2 / P0.7 hardening

```python
for cand in {dir(agent) - dunders - private - exact-matches}:
    if len(cand) > 24: continue   # cheap pre-filter
    matches = difflib.get_close_matches(cand, _KNOWN_OPTIONAL_AGENT_ATTRS,
                                        n=1, cutoff=0.75)
    if matches:
        warnings.warn(
            f"Agent {agent.id!r}: attribute {cand!r} looks like a typo "
            f"of {matches[0]!r} (an Loom optional). The expected "
            f"attribute will fall back to its default value, which may "
            f"not be what you meant.",
            UserWarning, stacklevel=3,
        )
```

The `cutoff=0.75` empirically catches `personality` (0.778 vs
`persona`), `cost_tiers` (0.947 vs `cost_tier`), but does NOT match
common adapter attrs like `model`, `client`, `api_key`,
`temperature`, `max_tokens`. Tunable if false positives surface.

### `_agent_to_wiring(agent: Agent) -> ParticipantWiring`

The canonical Agent → ParticipantWiring conversion. Used by
`LoomRoom.__init__` and `LoomRoom.add_agent`.

```python
if not hasattr(agent, "id") or not isinstance(agent.id, str) or not agent.id:
    raise TypeError("Agent must have a non-empty string `id` attribute")

stream_method = getattr(agent, "stream", None)
if callable(stream_method):
    proxy = agent                                    # use agent directly
else:
    send_method = getattr(agent, "send", None)
    if not callable(send_method):
        raise TypeError(f"Agent {agent.id!r} exposes neither stream() nor send()")
    proxy = SendProxyAdapter(agent, send_method="send")

_warn_on_typoed_agent_attrs(agent)

return ParticipantWiring(
    id=agent.id,
    proxy=proxy,
    persona=getattr(agent, "persona", ""),
    capability_block=getattr(agent, "capability_block", ""),
    cost_tier=int(getattr(agent, "cost_tier", 1)),
    capable=bool(getattr(agent, "capable", True)),
)
```

Notes:

- **The agent itself is used as the streaming proxy** when it has a
  `stream` method — no extra wrapping. The `Agent` Protocol contract
  matches the `StreamingProxy` Protocol exactly for the `stream`
  method, so this is type-safe.
- **`SendProxyAdapter` wraps `.send`** when `.stream` is missing.
- **Typo warning fires AFTER successful wiring** — so a hard error
  about missing `stream`/`send` doesn't get drowned out by a
  typo warning about a near-miss.
- **`cost_tier` is coerced via `int()`**, `capable` via `bool()` —
  defensive against agents that return wrong types from `getattr`.

### `class LoomRoom`

Public-facing facade. Constructor + 11 methods + 5 control-state
setters + REPL.

#### `__init__(agents, *, topic=None, anchor_id=None, default_responder_id=None, journal_dir=None, policy=None, policy_error_mode="close_turn", room_config=None)`

Validation:

1. `agents` non-empty (else `ValueError("LoomRoom requires at least one agent")`).
2. Walk `agents`: convert each via `_agent_to_wiring`; reject duplicate ids.
3. Default `anchor_id` = first agent's id (NOT `None` — the room
   always has an anchor unless explicitly `None` later).
4. Build `LoomSession` via `build_loom_session(wirings, …,
   auto_start=False)` — **the room owns lifecycle**, not the runtime.

Then store the session and journal_dir. Actor threads are NOT yet
started.

Sensible defaults from the docstring:
- `policy` → `DefaultPolicy` (set inside `build_loom_session`)
- `policy_error_mode` → `"close_turn"` (fail-closed library default)
- `anchor_id` → first agent's id
- `default_responder_id` → `None`
- `topic` → `None`
- `journal_dir` → `None` (no audit trail)
- `room_config` → `RoomConfig()` defaults
- `notify` (in `run_console`) → `_thread_safe_print`
- `prompt_fn` (in `run_console`) → `_default_prompt`

#### Lifecycle

| Method | Behaviour |
|---|---|
| `start()` | `self._session.start()`. Idempotent. |
| `stop(*, timeout=1.0)` | `self._session.stop(timeout=timeout)`. Idempotent. |
| `__enter__()` | `self.start(); return self` |
| `__exit__(exc_type, exc, tb)` | `self.stop()` |

**Required**: posting to a room that hasn't started returns nothing
because no actor is listening. The `with` block is the recommended
form.

#### Membership

- **`add_agent(agent)`** — `self._session.add_agent(_agent_to_wiring(agent))`.
- **`remove_agent(agent_id)`** — `self._session.remove_agent(agent_id)`.
  Raises `KeyError` if unknown (propagated from session).
- **`participants`** (property) — `sorted(self._session.state.participants.keys())`.
- **`topic`** (property) — `self._session.state.topic`.
- **`session`** (property) — escape hatch to `LoomSession`.
- **`journal_dir`** (property) — the path passed in (or `None`).

#### Posting

##### `post(text, *, channel="main") -> int`

Validates non-empty text. Calls `post_user_text(self._session, text,
channel=channel)`. Returns the bus event id (non-blocking).

##### `post_and_wait(text, *, timeout=30.0, channel="main") -> TurnResult`

The main user-facing call. Posts and blocks until the turn closes or
timeout.

```python
bus = self._session.bus
last_seen_len = len(bus)         # snapshot BEFORE post

started_at = time.monotonic()
event = post_user_text(self._session, text, channel=channel)
ut = self._session.coordinator.user_turn
if ut is None or ut.user_event_id != event.id:
    # Policy returned an acknowledgement plan — no turn opened.
    return TurnResult(messages=[], turn_id=-1,
                      routing_case="acknowledgement",
                      closed_reason="no_turn_opened",
                      participant_responses={},
                      elapsed_s=time.monotonic() - started_at)
target_turn_id = ut.id
routing_case = getattr(ut.frozen_plan, "routing_case", "")

deadline = started_at + timeout
timed_out = True
while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0: break
    new_len = bus.wait_after(last_seen_len, timeout=remaining)
    if new_len <= last_seen_len: break       # timed out / bus stopped
    last_seen_len = new_len
    ut = self._session.coordinator.user_turn
    if ut is None or ut.id != target_turn_id or ut.state != "open":
        timed_out = False
        break                                # turn closed (or replaced)

# Collect chat events for THIS turn.
messages = []
for ev in bus.snapshot():
    if ev.kind != "chat" or ev.sender == "user":
        continue
    if ev.user_turn_id != target_turn_id:
        continue
    if channel == "main" and ev.channel != "main":
        continue
    messages.append(Message.from_event(ev))

# Project closure reason via _CLOSURE_REASON_MAP.
ut_final = self._session.coordinator.user_turn
if timed_out:
    closed_reason = "timeout"
elif ut_final is not None and ut_final.id == target_turn_id:
    closed_reason = _project_closure_reason(ut_final.closure_reason)
else:
    closed_reason = "new_user_post"          # turn slot replaced

responses = {}
for m in messages:
    responses.setdefault(m.sender, []).append(m)

return TurnResult(messages, turn_id=target_turn_id,
                  routing_case=routing_case, closed_reason=closed_reason,
                  participant_responses=responses,
                  elapsed_s=time.monotonic() - started_at)
```

Critical details:

- **Snapshots `len(bus)` BEFORE post** so the wait loop measures
  against the pre-post cursor. After `post_user_text` the bus has
  grown by 1 (user chat) + N (control events for opened turn).
- **Acknowledgement detection**: if `coordinator.user_turn` is `None`
  or doesn't match the new event's id, no turn opened — return an
  empty `TurnResult` with `closed_reason="no_turn_opened"`.
- **Wait loop**: iterates `bus.wait_after(last_seen_len, timeout=remaining)`
  in a loop. Exit conditions: (a) deadline expired (`timed_out=True`);
  (b) `wait_after` returned without new events (bus stopped or
  timeout); (c) the open turn either closed or has been replaced by
  a new user post.
- **Per-iteration check**: re-reads `coordinator.user_turn` to check
  if THIS turn (`target_turn_id`) is still open. A new user post in
  parallel would replace the slot.
- **Filter chat events** to: (a) sender ≠ "user"; (b)
  `ev.user_turn_id == target_turn_id` (kernel correlates replies via
  this); (c) `channel == "main"` if asking on main (lets DM
  callers see DM replies).
- **Closure reason**: timeout > kernel reason via `_CLOSURE_REASON_MAP` >
  fallback to "new_user_post" if slot was replaced.
- **`_monotonic` is a `@staticmethod` indirected so tests can patch it.**

##### `dm(participant_id, text) -> int`

Direct message to one participant. Validates participant exists,
constructs a `chat` event on `dm:<pid>` channel with the recipient as
the sole addressee, then opens a single-responder turn:

```python
e = _ev.chat(sender="user", body=text, addressees=[pid],
             channel=f"dm:{pid}", room_epoch=state.room_epoch)

def _dm_plan(posted_event):
    return plan_for_default(pid, reason="dm",
                            target_event_ids=[posted_event.id],
                            rationale="direct DM")

self._session.coordinator.post_user_event_and_open_turn(e, _dm_plan)
return e.id
```

Notes:
- **`post_user_event_and_open_turn` race-free atomic** (Session 5).
- **`plan_for_default` from `loom.kernel.obligations`** (top-level
  `room.py` is one of the few non-policy callers that imports a
  plan-builder directly).
- **Other participants do not see the message** — `visible_to`
  (Session 2) filters DMs at `audience` time.

#### Room control facade methods (P1.3 — typed equivalents of slash commands)

Each is a thin validating wrapper over the coordinator. Library
authors should prefer these over routing through
`handle_slash_command`.

| Method | Validation | Coordinator call |
|---|---|---|
| `set_topic(topic)` | `len(topic) <= 500` (else `ValueError`); `topic or None` | `coordinator.set_topic` |
| `set_anchor(participant_id)` | `_require_participant` | `coordinator.set_anchor` |
| `set_default_responder(participant_id)` | `_require_participant` | `coordinator.set_default_responder` |
| `set_roles(roles)` | each key via `_require_participant` | `coordinator.set_roles(dict(roles))` |
| `set_floor(participant_ids)` | `None` or empty → open; else each via `_require_participant` | `coordinator.set_floor_owner` |
| `set_style(style)` | `style in {"brief","normal","detailed"}` (else `ValueError`) | `coordinator.set_style` |
| `cancel_turn()` | none | `coordinator.close_user_turn("cancelled")` |

`_require_participant(pid)` raises `KeyError` with the members list
(audit principle 3.7 — teaching errors).

`_MAX_TOPIC_CHARS = 500` is a class attribute — same cap as the
slash-command `/topic` handler.

#### `run_console(*, prompt_fn=None, notify=None) -> None`

Interactive REPL.

1. Defaults: `prompt_fn = _default_prompt`, `notify = _thread_safe_print`.
2. `self.start()` (actors begin processing).
3. `unsubscribe = self._session.bus.subscribe(_make_console_subscriber(notify))`.
4. Loop:
   - `text = prompt_fn()` (catches `EOFError` / `KeyboardInterrupt`).
   - `text.strip()`; skip empty.
   - If starts with `/`: `result = handle_slash_command(text,
     self._session, console=notify)`; print result.message; break on
     `result.quit`.
   - Else: `self.post(text)`.
5. `finally`: `unsubscribe()`; `self.stop()`.

---

## loom/runtime.py

The kernel-facing wiring layer. Despite being 840 LOC, conceptually
small: build a session from wirings, run a console loop, parse slash
commands.

### Module-level

- **`_SLASH_RE = re.compile(r"^/(\w+)(?:\s+(.*))?$")`** — captures
  `cmd` and optional rest-of-line `args`.
- **`_VALID_CHANNEL_RE = re.compile(r"^(main|dm:[A-Za-z][\w-]*)$")`**
  — P3.1 / audit T3 hardening. Defense in depth against future
  call sites that pull `channel=` from untrusted text.
- Re-export: `from loom.adapters import SendProxyAdapter` (P1.4
  back-compat for `from loom.runtime import SendProxyAdapter`).

### `class ParticipantWiring` (mutable dataclass)

| Field | Type | Default | Notes |
|---|---|---|---|
| `id` | `str` | required | Participant id. |
| `proxy` | `StreamingProxy` | required | Anything with `stream(prompt) -> Iterator[str]`. |
| `persona` | `str` | `""` | Rendered into prompt (fenced — Session 3). |
| `capability_block` | `str` | `""` | Rendered into prompt (fenced). |
| `cost_tier` | `int` | `1` | Default `1` (matches `_FunctionAgent`; differs from `ParticipantInfo` default of `0` — see Session 6 invariant 104). |
| `capable` | `bool` | `True` | Slot fallback eligibility. |

### `class LoomSession` (mutable dataclass)

| Field | Type | Notes |
|---|---|---|
| `bus` | `MessageBus` | The shared bus. |
| `state` | `RoomState` | The shared state. |
| `coordinator` | `RoomCoordinator` | The single mutator. |
| `journal` | `Optional[Journal]` | None if no journal_dir. |
| `actors` | `list[ParticipantActor]` | Session-owned mutable registry. |
| `wirings` | `dict[str, ParticipantWiring]` | id → wiring. **Closure capture by reference** in the draft handler. |
| `policy` | `ConversationPolicy` | Default factory: `DefaultPolicy`. |
| `_stop_event` | `threading.Event` | Idempotent shutdown signal. |
| `_draft_handler` | `Optional[Callable]` | Closure shared across actors; set by `build_loom_session`. **Required for `add_agent`**. |
| `_started` | `bool` | Tracks whether actors are running. |
| `_membership_lock` | `threading.Lock` | Serializes `add_agent`/`remove_agent`/`start`. |

#### Methods

##### `add_agent(wiring)`

```python
if self._stop_event.is_set():
    raise RuntimeError("session is stopped")
if self._draft_handler is None:
    raise RuntimeError("session was constructed without a draft handler; "
                       "use build_loom_session() to create sessions")
with self._membership_lock:
    if wiring.id in self.wirings:
        raise ValueError(f"participant {wiring.id!r} already in room")
    # Order: wire proxy first (closure captures wirings dict by ref);
    # register kernel-side; spin up actor.
    self.wirings[wiring.id] = wiring
    self.coordinator.register_participant(ParticipantInfo(
        id=wiring.id, capable=wiring.capable,
        cost_tier=wiring.cost_tier, active=True))
    actor = ParticipantActor(wiring.id, self.bus, self.coordinator,
                             self._draft_handler)
    self.actors.append(actor)
    if self._started:
        actor.start()
```

Order is **load-bearing**:

1. **Wire the proxy first** so the draft handler closure (captured
   over `self.wirings` by reference) can find it.
2. **Register kernel-side** (`participant_added` event fires).
3. **Spin up the actor** — only after both above complete; otherwise
   the actor could wake on a bus event and try to look up its proxy
   before it's wired.

Actor only `start`s if `_started=True` — sessions constructed with
`auto_start=False` (the `LoomRoom` path) defer actor start to
`start()`.

##### `remove_agent(agent_id, *, actor_stop_timeout=0.5)`

```python
with self._membership_lock:
    actor = next((a for a in self.actors if a.id == agent_id), None)
    if actor is None and agent_id not in self.wirings \
            and agent_id not in self.state.participants:
        raise KeyError(f"unknown participant: {agent_id}")
    if actor is not None:
        actor.stop(timeout=actor_stop_timeout)
        self.actors = [a for a in self.actors if a.id != agent_id]
    self.coordinator.unregister_participant(agent_id)
    self.wirings.pop(agent_id, None)
```

The 3-step KeyError check is forgiving: only raises if the
participant isn't known to ANY of {actors, wirings, state.participants}.
This handles partial-removal recovery without raising redundantly.

`coordinator.unregister_participant` does the 7-step cascade
(Session 5) — slot re-resolution, dead-letter rerouting, obligation
transfer. The session's role is to also stop the actor thread.

##### `start()` / `stop(timeout=1.0)`

`start()`:
- `RuntimeError` if `_stop_event` is set (cannot restart a stopped
  session — must rebuild).
- Under lock: `for a in self.actors: a.start()` (idempotent per
  `ParticipantActor`).
- `_started = True`.

`stop(*, timeout=1.0)`:
- Set `_stop_event`.
- Stop each actor with timeout.
- If journal: try `journal.snapshot(state)` (best-effort, swallow
  exceptions); then `journal.close()`.
- `bus.stop()` (Session 2 — wakes all `wait_after` waiters).

### `_make_draft_handler(wirings, policy)`

The closure that bridges actor → streaming.

```python
def handler(actor, trigger, lease):
    wiring = wirings[actor.id]
    prompt = build_prompt(actor.id, trigger, actor.coordinator,
                          persona=wiring.persona,
                          capability_block=wiring.capability_block,
                          policy=policy)
    run_streaming_call(wiring.proxy, prompt, lease,
                       actor.bus, actor.coordinator)
```

**Critical**: `wirings[actor.id]` reads the dict **on every call**, so
mid-session `add_agent` / `remove_agent` are visible to the closure.
Captured by reference.

### `build_loom_session(wirings, *, config=None, default_responder_id=None, anchor_id=None, topic=None, journal_dir=None, auto_start=True, policy=None, policy_error_mode="close_turn") -> LoomSession`

The factory. Steps:

1. `cfg = config or RoomConfig()`.
2. `bus = MessageBus()`; `state = RoomState(config=cfg)`;
   `coord = RoomCoordinator(bus, state, policy_error_mode=...)`.
3. `policy = policy or DefaultPolicy()`.
4. **If `journal_dir` is set**: create `Journal`, `open()`, register
   THREE callbacks via the bus subscription pattern:
   - **`bus.subscribe(journal.on_event)`** — appends every event.
   - **`journal.set_snapshot_due_callback(lambda: Journal._state_to_dict(state))`**
     — runs on poster thread; cheap dict construction.
   - **`journal.set_failure_callback(_on_journal_failure)`** — posts
     `journal_error` via `bus.post_internal` (sender="system";
     post_internal because the callback runs on a bound actor
     thread).
   - **`journal.set_snapshot_drop_callback(_on_snapshot_drop)`** —
     posts `snapshot_dropped` via `bus.post_internal`.
5. **For each wiring**: `coord.register_participant(ParticipantInfo(id,
   capable, cost_tier, active=True))`; build `by_id` dict.
6. **Phase 0 audit fix — fail loud on unknown ids**:
   - If `default_responder_id` is set AND not in `by_id`: raise
     `ValueError` with sorted known ids.
   - Same for `anchor_id`.
7. Apply slot setters: `coord.set_default_responder`, `coord.set_anchor`,
   `coord.set_topic`.
8. `handler = _make_draft_handler(by_id, policy)`.
9. `actors = [ParticipantActor(w.id, bus, coord, handler) for w in wirings]`.
10. `if auto_start: for a in actors: a.start()`.
11. Return `LoomSession(bus, state, coord, journal, actors, by_id,
    policy, _draft_handler=handler, _started=auto_start)`.

The validation in step 6 is the audit Phase 0 fix: a typo in
`default_responder_id` used to silently set the slot to a nonexistent
id, breaking obligations later. Now fails loud.

### `class SlashResult` (dataclass)

| Field | Type | Default |
|---|---|---|
| `handled` | `bool` | (required) |
| `quit` | `bool` | `False` |
| `message` | `Optional[str]` | `None` |

### `handle_slash_command(text, session, *, console=None) -> SlashResult`

Match `_SLASH_RE`; if no match → `SlashResult(handled=False)` (caller
treats as user input).

Commands:

| Command | Behaviour |
|---|---|
| `/leave` `/quit` `/exit` | `quit=True`, message="leaving session" |
| `/who` | members, topic, floor, style, roles |
| `/mode` | informative removal notice (v0 = group chat only) |
| `/topic [text]` | cap 500; `coord.set_topic(args or None)` |
| `/add` | unsupported; tells user to use programmatic add |
| `/remove <id>` | `session.remove_agent(args)` |
| `/cancel` | `coord.close_user_turn("cancelled")` |
| `/dm <id> <body>` | DM channel + plan_for_default + post_user_event_and_open_turn |
| `/summary` | last main-channel summary event |
| `/anchor [id]` | show or `coord.set_anchor` |
| `/responder [id]` | show or `coord.set_default_responder` |
| `/roles [pid=role …]` | show or `coord.set_roles` via `_parse_roles_args` |
| `/floor [ids …]` | show or `coord.set_floor_owner(ids)` |
| `/release` | `coord.set_floor_owner(None)` |
| `/quiet <pid> [<pid> …]` | floor = everyone EXCEPT silenced; refuses if all silenced |
| `/goal [text]` | alias for `/topic` (P2.3 collapse) |
| `/brief` `/normal` `/detailed` | `coord.set_style(...)` |
| `/control` | diagnostic dump |
| (unknown) | helpful error listing valid commands |

`_parse_roles_args(args, participants) -> tuple[dict, error_msg]`:
parses `pid=role pid=role ...` tokens; unknown ids OR missing `=` produce
errors; on any error returns `({}, error_str)` so the call site can
present a usage hint without partial application.

### `post_user_text(session, text, *, channel="main") -> Event`

The standard user-input entry point.

```python
if not _VALID_CHANNEL_RE.match(channel):
    raise ValueError(f"channel must match …, got {channel!r}")
addressable = list(session.state.participants.keys())
addressees = parse_addressees(text, addressable, exclude="user")
e = ev.chat(sender="user", body=text,
            addressees=addressees, channel=channel,
            room_epoch=session.state.room_epoch)

def _classify_after_post(posted_event):
    return session.policy.plan_user_turn(posted_event,
                                         session.state.view())

session.coordinator.post_user_event_and_open_turn(e, _classify_after_post)
return e
```

Notes:
- **Channel validation FIRST** (P3.1) — failed validation raises
  before reaching the bus.
- **Addressees populated via `parse_addressees`** (Session 2) — the
  user's @-mentions are extracted at post time so visibility filters
  + `is_direct_mention` work correctly.
- **`_classify_after_post` is the closure** the coordinator runs
  under its lock. Sees `session.state.view()` (read-only, fresh).
- **`post_user_event_and_open_turn` is the race-free wrapper**
  (Session 5 — atomic post + classify + open).

### `_format_control(event) -> Optional[str]`

Pretty one-liner per control event. Returns `None` to suppress
(e.g. `user_turn_opened`, `obligation_recorded`, `obligation_resolved`
are silent in console — internal accounting). `topic_changed`,
`dead_letter`, `default_responder_changed`, `anchor_changed`,
`chair_changed`, `default_summarizer_changed`, `participant_added/removed`,
`user_turn_closed` (with reason filtering — `completed` is silent)
all have specific renderings. **Unknown control_types return `None`**
so the dict repr never leaks to the console.

### `_make_console_subscriber(notify) -> Callable[[Event], None]`

Returns the bus subscriber for console rendering:

```python
def _on_event(event):
    if event.kind == "chat":
        if event.sender == "user":
            if event.channel.startswith("dm:"):
                target = event.channel[len("dm:"):]
                notify(f"\n(dm → {target}) ▸ {event.body}")
            return                       # echo only DM user posts
        if event.channel != "main":
            return                       # agent-to-agent DMs stay private
        notify(f"\n{event.sender} ▸ {event.body}")
        return
    if event.kind == "control":
        msg = _format_control(event)
        if msg:
            notify(f"\n· {msg}")
        return
    return                               # stream events: silent in v0
```

This is THE place where stream events are dropped — the chat event
is the canonical render. The `notify` callable is whatever the user
passed to `run_console` (default: `_thread_safe_print`).

### `run_loom_console(wirings, *, …) -> None`

The pre-facade entry. Builds session, subscribes the console
subscriber, runs the REPL, stops the session in `finally`. Same shape
as `LoomRoom.run_console` but takes `wirings` directly (no Agent
adapter conversion).

---

## loom/messages.py — projection layer

Two-tier event surface (UX spec §5.3): full `Event` stays
kernel-internal; user-facing APIs return `Message` and `TurnResult`.

### `TurnClosedReason` (Literal, 7 values)

`"all_obligations_resolved"`, `"timeout"`, `"no_turn_opened"`,
`"cancelled"`, `"obligation_unresolved"`, `"new_user_post"`,
`"topic_changed"`.

### `_CLOSURE_REASON_MAP` — kernel reason → user-facing

| Kernel `UserTurn.closure_reason` | User-facing `TurnClosedReason` |
|---|---|
| `"completed"` | `"all_obligations_resolved"` |
| `"idle_timeout"` | `"timeout"` |
| `"no_responder"` | `"no_turn_opened"` |
| `"cancelled"` | `"cancelled"` |
| `"obligation_unresolved"` | `"obligation_unresolved"` |
| `"new_user_post"` | `"new_user_post"` |
| `"topic_changed"` | `"topic_changed"` |

`_project_closure_reason(kernel_reason)`:
- `None` → `"timeout"` (only path: the wait loop in `post_and_wait`
  exited via timeout without observing closure).
- Known kernel reason → user-facing label.
- **Unknown kernel reason → pass through verbatim** (so a future
  kernel reason is still observable from a `TurnResult` without an
  immediate breakage at the projection layer).

### `class Message` (frozen, slots dataclass)

```python
sender:    str
body:      str
channel:   str
timestamp: float
kind:      str

@classmethod
def from_event(cls, ev: Event) -> "Message":
    body = ev.body if isinstance(ev.body, str) else str(ev.body)
    return cls(sender=ev.sender, body=body, channel=ev.channel,
               timestamp=ev.ts, kind=ev.kind)
```

Fields hidden vs `Event`:
- `addressees` — implementation detail (visibility / mention parsing)
- `room_epoch` — kernel bookkeeping
- `user_turn_id` — kernel bookkeeping
- `meta` — sidecar (cost tokens, lease id) — must not render to LLM
- `id` — bus position

### `class TurnResult` (frozen, slots dataclass)

```python
messages:              list[Message] = []
turn_id:               int = -1
routing_case:          str = ""
closed_reason:         str = "no_turn_opened"
participant_responses: dict[str, list[Message]] = {}
elapsed_s:             float = 0.0

# Iterable + len + bool + indexing over messages.
def __iter__(self): return iter(self.messages)
def __len__(self):  return len(self.messages)
def __bool__(self): return bool(self.messages)
def __getitem__(self, idx): return self.messages[idx]
```

Designed so existing patterns continue to work:
- `for r in result: print(r.sender, r.body)` ✓
- `{r.sender for r in result}` ✓
- `len(result)` ✓
- `if result:` ✓
- `result[0]`, `result[:3]` ✓

`turn_id == -1` indicates "no turn opened" (acknowledgement plan or
empty plan).

`routing_case` is the policy classification string — `"broadcast"`,
`"direct_mention"`, `"multi_opinion"`, `"single_responder"`, `"floor"`,
`"round_robin"`, `"acknowledgement"`, etc. Promoted to a typed
`Literal` in P2.6 (the `RoutingCase` from Session 1).

`elapsed_s` is wall-clock seconds from post to close (or to timeout)
— measured via `time.monotonic`.

---

## loom/errors.py — single error import surface

A leaf module. The kernel imports `LoomError` from here at module-load
time; this module only resolves the kernel-side exception classes when
a caller asks for them.

### `class LoomError(Exception)`

Tag class. Library authors who want a single `except` clause covering
every kernel-raised error import this and catch it. The concrete
kernel exception types inherit from this AND from `ValueError`, so
legacy `except ValueError` keeps working (back-compat with v0.0).

### Lazy re-export of typed exceptions

```python
if TYPE_CHECKING:                          # for static checkers only
    from loom.kernel.bus import BodyOversizeError, SenderMismatchError
    from loom.kernel.events import EventShapeError

_LAZY_RE_EXPORTS = {
    "EventShapeError":     ("loom.kernel.events", "EventShapeError"),
    "BodyOversizeError":   ("loom.kernel.bus", "BodyOversizeError"),
    "SenderMismatchError": ("loom.kernel.bus", "SenderMismatchError"),
}

def __getattr__(name):
    target = _LAZY_RE_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'loom.errors' has no attribute {name!r}")
    module_name, attr = target
    import importlib
    return getattr(importlib.import_module(module_name), attr)
```

The lazy `__getattr__` resolves the typed exceptions on first access
without import-time recursion. **Circular import avoidance**: the
kernel modules import `LoomError` from this module at module-load
time. If `errors.py` eagerly imported `loom.kernel.bus`,
`loom.kernel.bus` couldn't import `loom.errors` (which it does for
the `LoomError` base). The lazy pattern breaks the cycle.

`__all__` includes both the always-available `LoomError` and the
lazy-loaded exceptions — IDEs / static analysers see them via the
`TYPE_CHECKING` block.

---

## loom/testing.py — test scaffolding

Public fixtures for policy and adapter authors. Authors who want to
write tests for their `ConversationPolicy` subclass or `Agent` adapter
can import from here without reaching into `loom.kernel.*`.

### `ParticipantSpec` Union

```python
ParticipantSpec = Union[
    str,                           # "pid"  → all defaults
    ParticipantInfo,               # passthrough
    Mapping[str, Any],             # {"id": "pid", "active": False, ...}
    Sequence,                      # ("pid", active, capable[, cost_tier])
]
```

Four flavors of compactness for ergonomic test fixtures.

### `_build_participant(spec) -> ParticipantInfo`

Dispatches on spec type; positional sequence fields are
`(active, capable, cost_tier)` after `pid`.

### `make_test_state(*participants, config=None, topic=None, default_responder=None, anchor=None) -> RoomStateView`

Build a `RoomStateView` from compact specs. **Returns a view, not the
live `RoomState`** — policies receive views, so this matches the
production contract.

```python
state = RoomState(config=config or RoomConfig())
for spec in participants:
    state.add_participant(_build_participant(spec))
if topic is not None: state.topic = topic
if default_responder is not None:
    state.set_default_responder(default_responder)
if anchor is not None:
    state.anchor_id = anchor                  # bypasses set_anchor (no event)
return state.view()
```

Note: `default_responder` goes through `set_default_responder` (which
validates), but `anchor` is assigned directly to bypass the
event-emission side. Idiomatic for tests — we don't want spurious
control events on a view-construction call.

### `make_test_event(body="hi", *, sender="user", id=1, channel="main", addressees=None, user_turn_id=None, room_epoch=0) -> Event`

Wrap `chat(...)` with test-friendly defaults.

**Key detail**: `id` defaults to `1` (NOT `0`), set explicitly so
policies that branch on `user_event.id` work out of the box. Pass
`id=None` to leave unassigned, or `id=0` to exercise the
first-post-on-bus boundary.

### `class FakeProxy`

Minimal `StreamingProxy` for tests:

```python
def __init__(self, chunks=(), *, raises=None, raises_at=None):
    self.chunks = list(chunks)
    self.raises = raises
    self.raises_at = raises_at
    self.cancelled = False
    self.last_prompt = None

def stream(self, prompt):
    self.last_prompt = prompt
    if self.raises is not None and self.raises_at is None:
        raise self.raises               # raise immediately on stream()
    for i, chunk in enumerate(self.chunks):
        if self.cancelled: return
        if self.raises_at is not None and i == self.raises_at:
            raise self.raises or RuntimeError(f"FakeProxy: raise at chunk {i}")
        yield chunk

def cancel(self):
    self.cancelled = True
```

Three modes: yield chunks (default), raise immediately
(`raises=...`, `raises_at=None`), raise at index (`raises=...,
raises_at=N`). `last_prompt` records what the proxy was called with —
useful for asserting the prompt content in tests.

### `assert_no_state_mutation(view)` — context manager

Defensive check for the documented soft leak (Session 1 invariant 15):
`ParticipantInfo` values inside the view are still mutable, so a
buggy policy could mutate `info.active = False` through a captured
alias.

```python
@contextmanager
def assert_no_state_mutation(view):
    before = _snapshot_view(view)
    yield
    after = _snapshot_view(view)
    if before != after:
        diff = [f"{k}: {before[k]!r} -> {after[k]!r}"
                for k in before if before[k] != after[k]]
        raise AssertionError("RoomStateView mutated under …\n  " + "\n  ".join(diff))
```

`_snapshot_view(view)` captures: topic, slot ids, per-participant
`(active, capable, cost_tier)` tuple, control floor / wait_for_user /
style / turn_taking_mode / turn_order / next_speaker_idx / roles.

Use:

```python
view = make_test_state("a", "b")
with assert_no_state_mutation(view):
    plan = MyPolicy().plan_user_turn(make_test_event(), view)
```

### `class RecordReplayProxy`

Adapter test harness — record real provider chunks once, replay
forever. JSONL file keyed by literal prompt string.

```python
def __init__(self, path, *, inner=None, mode="auto"):
    # mode ∈ {auto, record, replay}; auto picks based on path.exists().
    # record: requires inner; appends to path.
    # replay: requires path; raises if missing.
```

**Modes**:
- `"auto"` (default) — replay if file exists, else record.
- `"record"` — always re-record (overwrites existing).
- `"replay"` — replay only; `FileNotFoundError` if path missing,
  `KeyError` if prompt unrecorded.

**Format**: one JSON object per line: `{"prompt": "...", "chunks":
[...]}`.

**Key drift caveat**: keyed on **literal prompt string**. If your
prompt embeds a timestamp, random nonce, or `room_epoch`, normalize
before recording or replays will miss. This is documented in the
docstring.

Use:

```python
# First CI run: wrap a real proxy, record.
proxy = RecordReplayProxy(
    "tests/fixtures/openai_smoke.jsonl",
    inner=OpenAIProxy(api_key=...),
)
# Subsequent runs: replays from disk; no inner needed.
proxy = RecordReplayProxy("tests/fixtures/openai_smoke.jsonl")
```

---

## Invariants (this session's additions)

130. **`LoomRoom` is the canonical door** — every other entry point
     (`build_loom_session`, `run_loom_console`,
     `LoomSession.add_agent` direct) is documented as advanced. The
     facade owns lifecycle (`auto_start=False` is passed to
     `build_loom_session` from `LoomRoom.__init__`) and provides the
     `with room:` context manager.
131. **The `with room:` block is required** — calling `post` /
     `post_and_wait` on an un-started room returns nothing because
     no actor is listening. `post_and_wait` will time out without
     observing closure.
132. **`anchor_id` defaults to the first agent's id** in
     `LoomRoom.__init__` if not specified. Passing `anchor_id=None`
     explicitly leaves it unset. The room ALWAYS has an anchor unless
     the caller explicitly opts out.
133. **`_warn_on_typoed_agent_attrs` uses `difflib.get_close_matches`
     with `cutoff=0.75`** — catches `personality`/`cost_tiers` but
     not common adapter attrs (`model`, `api_key`, etc.). Tunable.
134. **`_agent_to_wiring` uses the agent itself as the streaming
     proxy** when it has a `stream` method — no extra wrapping. Falls
     back to `SendProxyAdapter` only if `.stream` is missing.
135. **`post_and_wait` snapshots `len(bus)` BEFORE posting** so the
     wait loop measures against the pre-post cursor. The bus grows by
     1 (user chat) + N (control events for opened turn) immediately
     after `post_user_text` returns.
136. **`post_and_wait` returns acknowledgement-shaped TurnResult** if
     `coordinator.user_turn` is `None` or doesn't match the new
     event's id (i.e. policy returned an ack plan). `turn_id=-1`,
     `closed_reason="no_turn_opened"`.
137. **`post_and_wait` filter: `ev.user_turn_id == target_turn_id`** is
     how replies are correlated to the originating turn. The kernel
     stamps each chat event with the turn id at commit time
     (Session 3 — `run_streaming_call` includes `user_turn_id` in
     the chat event constructor).
138. **`post_and_wait` closure-reason precedence**: timeout > kernel
     reason via `_CLOSURE_REASON_MAP` > `"new_user_post"` (slot
     replaced).
139. **`LoomRoom._monotonic` is `@staticmethod` indirected for test
     patching.** The wait loop uses it for `started_at`/`deadline`/
     `elapsed_s`.
140. **`LoomRoom.dm` uses `plan_for_default` directly**, not the
     room's policy. DM routing is a kernel-mechanism concern, not a
     policy concern (Session 6 comparison-table footnote).
141. **`LoomRoom.set_topic` caps at 500 chars** (`_MAX_TOPIC_CHARS`)
     — same cap as `/topic` slash command.
142. **`LoomSession.add_agent` order is load-bearing**: wire proxy
     first → register kernel-side → spin up actor. The closure
     captures `wirings` by reference, so the actor MUST not start
     before its wiring is in the dict.
143. **`LoomSession.add_agent` requires `_draft_handler` is set**
     (must construct via `build_loom_session`). Manual `LoomSession`
     construction without a draft handler is rejected at
     `add_agent` time, not at construction.
144. **Sessions cannot be restarted** — `start()` raises
     `RuntimeError` if `_stop_event` is set. Rebuild the session
     instead.
145. **`LoomSession.stop` does best-effort `journal.snapshot(state)`**
     before `journal.close()`. Snapshot exceptions are swallowed —
     shutdown must succeed.
146. **`build_loom_session` validates `default_responder_id` and
     `anchor_id` exist** in the registered participant set — Phase 0
     audit fix. Raises `ValueError` with sorted known ids.
147. **The journal's three callbacks all run on the poster's thread**
     (which for state-mutating posts is the bound-actor thread);
     they all use `bus.post_internal` to bypass sender authentication.
148. **`_VALID_CHANNEL_RE = ^(main|dm:[A-Za-z][\w-]*)$`** validated
     in `post_user_text` — defense in depth (P3.1) against
     untrusted-input call sites in the future.
149. **`post_user_text` populates `addressees` at user-post time**
     via `parse_addressees` (Session 2). Visibility filters and
     `is_direct_mention` rely on this being done early.
150. **`handle_slash_command` returns `handled=False` for non-slash
     input** so the caller can forward as user input. Unknown
     commands return `handled=True` with a helpful message listing
     valid commands.
151. **`/quiet` refuses to silence ALL participants** — directs the
     user to `/release` instead. Otherwise the floor would be
     unreachable.
152. **`/goal` is an alias for `/topic`** (P2.3 collapse). The two
     used to track separate fields (`state.topic` and
     `control.active_goal`); they collapsed into `state.topic`.
153. **`_format_control` returns `None` for unknown control_types**
     so dict reprs never leak to the console.
154. **`_make_console_subscriber` drops stream events in v0** — the
     chat event is the canonical render. Stream-deltas are bus-only,
     consumed by other subscribers (UIs that render mid-stream).
155. **`_make_console_subscriber` drops agent-to-agent DMs** — only
     main-channel agent posts and DM user posts are echoed to the
     console.
156. **`Message.from_event` coerces non-string body via `str()`** —
     defensive against control/stream events accidentally being
     projected.
157. **`TurnResult` is iterable, sized, truthy, and indexable** over
     `messages` — all four protocols delegate. Existing
     `for r in replies` patterns keep working.
158. **`_project_closure_reason(None)` returns `"timeout"`**, since
     the only path that produces `None` is `post_and_wait`'s wait
     loop exiting without observing closure.
159. **`_project_closure_reason` passes unknown values through
     verbatim** — a kernel reason added in v0.2 stays observable in
     `TurnResult.closed_reason` even before the projection map is
     updated.
160. **`LoomError` is the single import surface for typed exceptions**;
     the actual exception classes live in `loom.kernel.bus` and
     `loom.kernel.events`. Lazy `__getattr__` avoids circular import.
161. **`make_test_state` returns a `RoomStateView`, not a `RoomState`**
     — matches the production contract that policies receive views.
162. **`make_test_event(id=1)` defaults to a non-zero, non-None
     id** so policies that include `target_event_ids=[user_event.id]`
     produce a non-empty list out of the box.
163. **`FakeProxy` records `last_prompt`** — useful for asserting
     prompt content in tests without fully running streaming.
164. **`RecordReplayProxy` is keyed on the literal prompt string**.
     Tests with embedded timestamps / nonces / `room_epoch` need
     normalization or replays will miss. Documented caveat.
165. **`assert_no_state_mutation` snapshots `(active, capable,
     cost_tier)` per participant** — catches the documented soft
     leak (Session 1 invariant 15).

---

## Verification

> *Write the four-line "30-second start" from README from memory, and
> explain what each line does at the kernel level.*

```python
from loom import LoomRoom, agent_from_send, OpenChatPolicy

def gpt_send(prompt):    ...   # call OpenAI; return string
def claude_send(prompt): ...   # call Anthropic; return string

room = LoomRoom(
    agents=[
        agent_from_send("gpt",    gpt_send),
        agent_from_send("claude", claude_send),
    ],
    policy=OpenChatPolicy(),
    topic="design review",
)

with room:
    replies = room.post_and_wait("what do you think of this plan?")
    for ev in replies:
        print(f"{ev.sender}: {ev.body}")

    room.add_agent(agent_from_send("gemma", gemma_send))
    room.remove_agent("gpt")
```

What each section does at the kernel level:

**Imports**:
- `LoomRoom` from `loom/__init__.py` re-exports `loom.room.LoomRoom`.
- `agent_from_send` from `loom/__init__.py` re-exports
  `loom.adapters.agent_from_send` (Session 6 — the canonical
  adapter factory).
- `OpenChatPolicy` from `loom/__init__.py` re-exports
  `loom.policy.open_chat.OpenChatPolicy` (Session 6 — the simplest
  non-trivial broadcast policy).

**`agent_from_send("gpt", gpt_send)`**:
- Validates `gpt_send` is callable.
- Builds an inner `_stream(prompt)` closure that calls `gpt_send(prompt)`,
  extracts text via `_extract_text` (None → "", str passes through,
  duck-types `.text/.body/.content/.output`, else `str()`), and
  yields the full string as a single chunk.
- Wraps in `_FunctionAgent("gpt", _stream, persona="",
  capability_block="", cost_tier=1, capable=True)`. The agent
  satisfies the `Agent` Protocol structurally.

**`LoomRoom(...)` — construction**:
- Validates `agents` non-empty and ids unique.
- For each agent, calls `_agent_to_wiring(agent)`:
  - Validates `agent.id` is non-empty string.
  - Reads `agent.stream` — `_FunctionAgent` has it, so the agent
    itself becomes the proxy. (No `SendProxyAdapter` wrap — the
    adapter already wrapped at the `agent_from_send` step.)
  - `_warn_on_typoed_agent_attrs` runs — `_FunctionAgent` only has
    documented attrs, no warnings fire.
  - Builds `ParticipantWiring(id="gpt", proxy=<the agent>, persona="",
    capability_block="", cost_tier=1, capable=True)`.
- `anchor_id` defaults to `"gpt"` (first agent id).
- Calls `build_loom_session(wirings, config=None,
  default_responder_id=None, anchor_id="gpt", topic="design review",
  journal_dir=None, auto_start=False, policy=OpenChatPolicy(),
  policy_error_mode="close_turn")`:
  - Constructs `MessageBus`, `RoomState(config=RoomConfig())`,
    `RoomCoordinator(bus, state, policy_error_mode="close_turn")`.
  - No journal (skip).
  - Registers `gpt` and `claude` via
    `coord.register_participant(ParticipantInfo(...))`. Each emits
    `participant_added`. `room_epoch` bumps to 2.
  - Validates `anchor_id="gpt"` exists ✓; calls `coord.set_anchor("gpt")`
    — emits `anchor_changed`. Bumps epoch to 3.
  - Validates default_responder_id is None — skip.
  - Calls `coord.set_topic("design review")` — emits `topic_changed`.
  - Builds the draft handler closure capturing `by_id` dict +
    `OpenChatPolicy` instance.
  - Constructs two `ParticipantActor` instances (one per wiring).
    **Does NOT start them** (auto_start=False).
  - Returns `LoomSession(bus, state, coord, journal=None, actors,
    wirings=by_id, policy=OpenChatPolicy(), _draft_handler=handler,
    _started=False, _stop_event=Event(), _membership_lock=Lock())`.

At this point the room exists but no actors are running.

**`with room:` — `__enter__`**:
- Calls `room.start()` → `self._session.start()`:
  - RuntimeError check (not stopped).
  - Under `_membership_lock`: for each actor, `a.start()` — spawns
    daemon thread `loom-actor-gpt` and `loom-actor-claude`. Each
    actor's `_loop` runs:
    1. `self.bus.bind_actor(self.id)` (P1 sender authentication —
       Session 2).
    2. Loops on `bus.wait_after(self._cursor, timeout=...)`.
  - Sets `_started = True`.

**`replies = room.post_and_wait("what do you think of this plan?")`**:
- Validates non-empty text.
- `last_seen_len = len(bus)` — say current bus length is 4 (events:
  participant_added×2, anchor_changed, topic_changed).
- `started_at = time.monotonic()`.
- `event = post_user_text(self._session, text, channel="main")`:
  - Channel validation passes.
  - `addressable = ["gpt", "claude"]`.
  - `parse_addressees(text, ["gpt", "claude"], exclude="user")` →
    `[]` (no @-mentions in "what do you think of this plan?").
  - Constructs `e = chat(sender="user", body="what do you think...",
    addressees=[], channel="main", room_epoch=3)`.
  - `_classify_after_post = lambda posted_event:
    OpenChatPolicy().plan_user_turn(posted_event, state.view())`.
  - Calls
    `coordinator.post_user_event_and_open_turn(e, _classify_after_post)`
    — atomic under coord lock (Session 5):
    - `bus.post_internal(e)` — assigns `e.id = 4`, notifies actors.
      Both actors wake but block on `coordinator.user_turn` (lock).
    - `_run_policy_under_lock`: `OpenChatPolicy.plan_user_turn`
      classifies — returns `plan_with_required(["claude", "gpt"],
      routing_case="broadcast", target_event_ids=[4],
      reason="open_chat", rationale="open chat: broadcast to 2
      agent(s)", allowed_speakers={"claude", "gpt"},
      max_responses=2, wait_for_user_after=False, instruction="Open
      group chat. Reply with substance or [PASS]."`)`. Watchdog: <1ms,
      no `policy_slow`.
    - `_apply_plan_state_changes_locked`: no-op (no
      `set_turn_taking_mode`/`set_turn_order`).
    - `routing_case != "acknowledgement"` → `open_user_turn`:
      - Debounce: first user post, OK to open.
      - No prior turn to close.
      - `wait_for_user` is False, skip.
      - `make_user_turn(turn_id=0, user_event_id=4, plan)` →
        allocates obligation ids 1 (claude) and 2 (gpt) — sorted by
        `BasicPolicy`. Returns turn with `obligations={1:RO(claude,must),
        2:RO(gpt,must)}`.
      - `state.current_user_turn_id = 0`.
      - Emits `user_turn_opened(turn_id=0,
        routing_case="broadcast", required=["claude","gpt"], ...)` —
        bus id 5.
      - Emits `obligation_recorded(obligation_id=1, claude, must,
        ...)` — bus id 6. Same for gpt → bus id 7.
      - Plan has required → no auto-close.
- Returns `e` (with `id=4`).
- `ut = coordinator.user_turn` → the turn we just opened.
- `ut.user_event_id == event.id` ✓ → don't return ack TurnResult.
- `target_turn_id = 0`, `routing_case = "broadcast"`.
- Wait loop:
  - `bus.wait_after(last_seen_len=4, timeout=remaining)` — returns
    when bus length > 4. Currently 8 (after the user_turn_opened +
    2× obligation_recorded). Returns 8. `last_seen_len = 8`.
  - Re-check `coordinator.user_turn` → still open (turn 0). Continue.
  - Meanwhile actor threads have woken:
    - `claude` actor: snapshot since cursor sees events 4-7. Decides
      DRAFT (priority 3 — user post for current turn with
      obligation). Acquires lease. Runs `draft_handler(self,
      trigger=ev4, lease)` → `build_prompt` → `proxy.stream(prompt)`
      via `run_streaming_call` → posts `stream_start` (id 8),
      `stream_delta`s, `chat` event (id ~10), `stream_end` (id 11).
      `coordinator.on_stream_end(committed)`: marks drafted,
      resolves obligation 1 (emits `obligation_resolved` id 12).
    - `gpt` actor does the same in parallel: lease granted (cap=2,
      already 1 outstanding, room for 1 more). Posts events ~13-16.
      `obligation_resolved` (id 17).
    - `_maybe_close_user_turn_locked` after each: when both must
      obligations are resolved AND `committed_count=2 >= cap=2`,
      closes the turn.
    - Emits `user_turn_closed(turn_id=0, reason="completed")` (id 18).
  - Wait loop iteration: `wait_after` returns again with new events.
    `last_seen_len` advances. Re-check `coordinator.user_turn` —
    `ut.id == 0` AND `ut.state == "closed"` → `timed_out = False`,
    break.
- `messages = []`. Iterate `bus.snapshot()`. Filter `ev.kind == "chat"`
  AND `ev.sender != "user"` AND `ev.user_turn_id == 0` AND `ev.channel
  == "main"`. Two matches: claude's reply (id ~10), gpt's reply (id
  ~14). Convert each via `Message.from_event`.
- `ut_final = coordinator.user_turn` → the closed turn (still
  visible — `_close_user_turn_locked` doesn't unset `_user_turn`).
  `ut_final.id == 0` AND timed_out is False →
  `closed_reason = _project_closure_reason("completed") =
  "all_obligations_resolved"`.
- `responses = {"claude": [Message...], "gpt": [Message...]}`.
- Returns `TurnResult(messages=[claude_msg, gpt_msg], turn_id=0,
  routing_case="broadcast",
  closed_reason="all_obligations_resolved",
  participant_responses={"claude": [...], "gpt": [...]},
  elapsed_s=...)`.

**`for ev in replies: print(...)`**:
- `TurnResult.__iter__` returns `iter(self.messages)`. Each `ev` is
  a `Message` with `sender`, `body`, `channel`, `timestamp`, `kind`
  fields.

**`room.add_agent(agent_from_send("gemma", gemma_send))`**:
- Builds `_FunctionAgent("gemma", ...)`.
- `room.add_agent(agent)` → `_agent_to_wiring(agent)` → ParticipantWiring.
- `self._session.add_agent(wiring)`:
  - Under `_membership_lock`: id not in wirings ✓.
  - `self.wirings["gemma"] = wiring` (closure picks up next dispatch).
  - `coordinator.register_participant(ParticipantInfo("gemma",
    capable=True, cost_tier=1, active=True))` — emits
    `participant_added(id="gemma", role_hints={})`. Epoch bumps.
  - Constructs `ParticipantActor("gemma", bus, coord, draft_handler)`.
  - Appends to `actors`.
  - `_started = True`, so `actor.start()` — spawns `loom-actor-gemma`
    daemon. The actor binds, then enters its `wait_after` loop.

**`room.remove_agent("gpt")`**:
- `self._session.remove_agent("gpt", actor_stop_timeout=0.5)`:
  - Under `_membership_lock`: find actor `loom-actor-gpt`.
  - `actor.stop(timeout=0.5)` — sets `_stopped`, joins thread.
  - Pops from `actors`.
  - `coordinator.unregister_participant("gpt")` → 7-step cascade
    (Session 5):
    1. `state.remove_participant("gpt")` → returns slot_changes
       (anchor was gpt → re-resolves to cheapest active capable;
       likely "claude" or "gemma" depending on cost tiers).
    2. Emits `participant_removed("gpt")`.
    3. Emits `anchor_changed(old="gpt", new=<resolved>)`.
    4. Invalidate gpt's leases (none in flight if turn 0 already
       closed).
    5. `_transfer_required_obligations_locked` — no open turn (turn
       0 closed) → no-op.
    6. Resolve gpt's obligations administratively — turn 0 closed,
       obligations already resolved.
    7. `_dead_letter_pending_mentions` — no open turn → no-op.
    8. `_maybe_close_user_turn_locked` — no open turn → no-op.
  - Pops from `wirings`.

**`__exit__`**:
- `room.stop(timeout=1.0)` → `self._session.stop(timeout=1.0)`:
  - `_stop_event.set()`.
  - For each actor, `a.stop(timeout=1.0)` — gemma and claude stop.
  - Journal is None — skip.
  - `bus.stop()` — wakes any remaining waiters; subsequent `post`
    returns -1.

End of session.

---

## Cross-references

- depends on: `00-orientation.md` (public surface, threading model),
  `01-kernel-primitives.md` (all the dataclasses Message/TurnResult
  project from), `02-kernel-bus.md` (`bus.subscribe`,
  `post_internal`, `wait_after`), `03-kernel-prompt-streaming.md`
  (the draft handler calls `build_prompt` + `run_streaming_call`),
  `04-kernel-actor-journal.md` (ParticipantActor lifecycle, Journal
  on_event subscription pattern), `05-kernel-coordinator.md`
  (`post_user_event_and_open_turn`, `set_*` setters, `close_user_turn`,
  every coordinator surface used by the facade), `06-contracts-policies.md`
  (`Agent` protocol, `ConversationPolicy` ABC, the four bundled
  policies, plan-builders).
- depended on by:
  - All 4 example scripts (`examples/two_agents.py`,
    `openai_two_agents.py`, `round_robin_classroom.py`,
    `single_responder_qa.py`) — use `LoomRoom` + an adapter
    factory + a bundled policy.
  - All system tests (Session 8) — drive `LoomRoom` end-to-end.
  - The `weave-repo` next door (sibling project that consumes Loom).

## Open questions / things to revisit

1. **`ParticipantWiring.cost_tier` default `1`** vs
   `ParticipantInfo.cost_tier` default `0` — the conversion in
   `_agent_to_wiring` reads `getattr(agent, "cost_tier", 1)` and the
   wiring stores 1, but `coordinator.register_participant` constructs
   `ParticipantInfo(id=w.id, capable=w.capable, cost_tier=w.cost_tier,
   active=True)` from the wiring, so the participant ends up with
   tier `1` not `0`. The `0` default on `ParticipantInfo` only fires
   for a direct `ParticipantInfo()` construction (e.g. tests). Worth
   normalizing.
2. **`_warn_on_typoed_agent_attrs` cutoff is 0.75** — empirically
   tuned. If false positives surface (e.g. a domain-specific attr
   that happens to be 75% similar), this becomes a footgun. Consider
   making it configurable.
3. **`post_and_wait` timeout default is 30s**. For long-running
   policies or high-cost models, callers must override. Consider
   adding a session-level default that wins when the caller doesn't
   specify.
4. **`post_and_wait` returns silently on timeout** — `closed_reason
   == "timeout"` is the only signal. Some callers may want a
   `TimeoutError` exception. Today's design favors easier control flow
   over raising.
5. **`LoomRoom.dm` does not validate the recipient is active+capable**
   — only that the participant exists. A DM to a paused participant
   becomes an obligation that idle-times out. Worth flagging in the
   docstring.
6. **`LoomSession.add_agent` / `remove_agent` are synchronous** — the
   `_membership_lock` serializes them but doesn't help if the caller
   wants concurrent membership management. Today's contract is
   "membership operations are serialized; use a separate thread for
   concurrent state changes" — works, but worth documenting.
7. **`run_console` doesn't redirect `stderr`** — slash-command errors
   and warnings still go to stderr, mixing with `notify`'s stdout.
   For TUI integrations this is a known quirk.
8. **`/add` slash command is unsupported** — adapters can't be
   constructed from a name string alone. The supported path is
   programmatic `room.add_agent(agent_from_send(...))`. A future
   `/add gpt` that pulls from a registry of pre-configured proxies
   is a v0.2 nice-to-have.
9. **`LoomSession.bus` / `coordinator` / `journal` are public
   attributes** (audit D3 — Session 0 open question 2). Tightening
   to underscored privates with facade methods is a v0.2 work item.
   Today the public-ness lets `LoomRoom.session` provide an escape
   hatch.
10. **`_make_console_subscriber` drops stream events in v0** —
    consumers wanting stream-rendering UIs subscribe directly to the
    bus and filter on `kind == "stream"`. Worth providing a typed
    `StreamProjection` analog to `Message` for v0.2.
11. **`Message.timestamp` is wall-clock** (`ev.ts`). UI consumers
    that want elapsed-since-room-open should compute against
    `room.start()` time themselves. Worth documenting.
12. **`TurnResult.routing_case` is `str` not `Literal`** — P2.6
    promotion is documented but not yet implemented. The `RoutingCase`
    Literal exists in `loom.kernel.obligations`; importing it here
    requires a kernel reach-through. Solution: re-export
    `RoutingCase` from `loom.messages` or `loom`.
13. **`testing.RecordReplayProxy` is keyed on the literal prompt
    string** — a known caveat for tests that include timestamps /
    nonces. A v0.2 normaliser kwarg (e.g. `prompt_normalize=lambda
    p: re.sub(r"ts=\d+", "ts=0", p)`) would cover most cases.
14. **`testing.assert_no_state_mutation` doesn't snapshot
    `participants[pid].role_hints`** — if a policy mutates role_hints
    on a captured `ParticipantInfo`, this passes. Cosmetic; the
    snapshot is "key fields a buggy policy would most plausibly
    write to".
