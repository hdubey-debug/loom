# 06 — Contracts, adapters, policies

This is **Session 6** of the Loom kernel deep-study curriculum. Phase
B (the kernel) is complete; Phase C covers the **extension surface**:
the neutral `Agent` Protocol and `ConversationPolicy` ABC that both
sides of the kernel/policy boundary type against, the adapter
factories that wrap ordinary callables, and the four bundled policies.

These files are the daily-driver authoring surface for anyone building
on Loom. They are also where the boundary contract is operationalised:
nothing in `loom/policy/*` may import `loom.kernel.coordinator` or
`loom.kernel.journal`, mutate `RoomState`, or post to the bus.

State as of Loom v0.1.2 (2026-05-08).

## Files covered

| File | LOC | Role | Imports from kernel |
|---|---:|---|---|
| `loom/contracts.py` | 180 | `Agent` Protocol, `ConversationPolicy` ABC | `events`, `obligations`, `room` |
| `loom/adapters.py` | 293 | `agent_from_send/stream/object`, `SendProxyAdapter`, `_FunctionAgent`, `_extract_text` | (none — stdlib only) |
| `loom/policy/__init__.py` | 58 | Re-exports of policies + plan-builder helpers | `kernel.obligations` (re-exports) |
| `loom/policy/base.py` | 113 | `BasicPolicy` template-method ABC | `events`, `obligations`, `room` |
| `loom/policy/single_responder.py` | 65 | `SingleResponderPolicy` (canonical reference) | `events`, `room` |
| `loom/policy/open_chat.py` | 50 | `OpenChatPolicy` | `events`, `room` |
| `loom/policy/round_robin.py` | 147 | `RoundRobinPolicy` | `events`, `obligations`, `room` |
| `loom/policy/default.py` | 515 | `DefaultPolicy` (v0.0 floor-aware) | `events`, `obligations`, `room`, `addressees` |

`loom/contracts.py` is the **neutral layer** — `loom/kernel/*` imports
it (the kernel charter test asserts this), and `loom/policy/*` imports
it. This is the only kernel-policy bridge.

## Mental model

```
                          loom.contracts                                       
              (neutral — both kernel and policy can import)                    
              ┌────────────────────────────────────────────────┐               
              │ class Agent(Protocol)         (runtime_checkable)│             
              │   id: str                                       │              
              │   stream(prompt) -> Iterator[str]               │              
              │   getattr-defaults: persona, capability_block,  │              
              │     cost_tier, capable, cancel                  │              
              │                                                  │             
              │ class ConversationPolicy(ABC)                    │             
              │   plan_user_turn(user_event, state) [abstract]   │             
              │   system_prompt(pid, state) -> str   [hook]      │             
              │   role_prompt(pid, state) -> str     [hook]      │             
              │   name: str = "unnamed"                          │             
              └────────────┬─────────────────────────┬───────────┘             
                           │                         │                         
              ┌────────────▼──────────┐  ┌───────────▼─────────────┐           
              │  loom.adapters         │  │  loom.policy.*           │          
              │  ─────────────         │  │  ───────────             │          
              │  agent_from_send       │  │  BasicPolicy             │          
              │  agent_from_stream     │  │  ├─ SingleResponderPolicy│          
              │  agent_from_object     │  │  └─ OpenChatPolicy       │          
              │  SendProxyAdapter      │  │  RoundRobinPolicy ←(uses │          
              │  _FunctionAgent (impl) │  │     declarative state    │          
              │  _extract_text         │  │     mutation, not Basic)│          
              │                        │  │  DefaultPolicy ←(complex│          
              │  All produce objects   │  │     per-case classifier)│          
              │  satisfying Agent.     │  │  + plan-builder re-exports│         
              └────────────────────────┘  └──────────────────────────┘          

       loom.kernel.* imports loom.contracts ─────────► safe, asymmetric
       loom.kernel.* imports loom.policy   ─────────► PROHIBITED (boundary test)
       loom.policy.* imports loom.contracts ─────────► safe
       loom.policy.* imports loom.kernel.coordinator ─► PROHIBITED (boundary test)
       loom.policy.* imports loom.kernel.journal    ─► PROHIBITED (boundary test)
       loom.policy.* mutates state.add_*/set_*/etc. ─► PROHIBITED (grep test)
       loom.policy.* posts to bus.post(...)         ─► PROHIBITED (grep test)
```

---

## contracts.py — full reference

### `class Agent(Protocol)` (runtime_checkable)

The public-facing actor shape every Loom room consumes. Adapters in
`loom.adapters` produce values satisfying it from ordinary `send` /
`stream` callables. User-supplied class instances satisfy it
structurally — anything with `id: str` and
`stream(prompt) -> Iterator[str]` qualifies.

| Attribute | Type | Required? | Default (via getattr) | Meaning |
|---|---|:---:|---|---|
| `id` | `str` | ✅ | — | Stable, unique within the room. |
| `stream(prompt) -> Iterator[str]` | method | ✅ | — | Yields one or more text chunks. Empty iter or only `""` = soft pass. |
| `persona` | `str` | ❌ | `""` | Self-description; rendered into prompt (fenced — Session 3). |
| `capability_block` | `str` | ❌ | `""` | Feature/limit summary; rendered into prompt (fenced). |
| `cost_tier` | `int` | ❌ | `1` | Lower = preferred for slot fallback. (Note: docstring says `1`; `_FunctionAgent.__init__` defaults to `1`. `ParticipantInfo` defaults to `0`. Slight inconsistency.) |
| `capable` | `bool` | ❌ | `True` | Slot fallback eligibility. |
| `cancel() -> None` | method | ❌ | (no-op) | Best-effort hard cancel of in-flight stream. |

`runtime_checkable` decorator means `isinstance(x, Agent)` works for
duck-typed objects. The `tests/property/test_ux_contracts.py`
asserts `isinstance(adapter, Agent)` for every bundled adapter (UX
metric in Session 0 orientation).

### `class ConversationPolicy(ABC)`

The pluggable extension layer. Five contracts spelt out in the
docstring (and enforced at runtime / by tests):

#### 1. PERFORMANCE CONTRACT
- **Synchronous, deterministic, non-blocking, local.**
- **Avoid I/O, LLM calls, sleeps, network.**
- **Return in <10ms typical.**
- The coordinator holds its lock across `plan_user_turn` to prevent
  the actor-cursor race (Session 5 invariant 75) — slow policies
  block every actor thread for the duration.
- Coordinator emits `policy_slow` at 100ms threshold (Session 5
  invariant 78). No interruption — Python can't safely cancel
  arbitrary code.

#### 2. ERROR CONTRACT
- Raised exception → coordinator emits `policy_error` (always — in
  every mode).
- Then dispatches on `policy_error_mode`:
  - `"close_turn"` (default, fail-closed): turn closes silently.
  - `"default_responder"`: fall back to `plan_for_default(state.resolve_default_responder(), ...)`.
  - `"raise"`: re-raise (dev mode).

#### 3. STATE CONTRACT (v0)
- Policy instances are **NOT journaled**. Restart instantiates a
  fresh policy.
- Stateful policies (debate phase, 20Q count) work in-process but
  reset across restart.
- v0.1+ will add `snapshot()/restore()` lifecycle hooks.

#### 4. PURITY CONTRACT
- Receives a `RoomStateView` (Session 1) — `participants` and
  `control.roles` are `MappingProxyType`; `control.turn_order` and
  `control.floor_owner` are tuples. Mutation through these surfaces
  raises `TypeError` / `AttributeError` at runtime.
- Policies must not post to the bus.
- Enforced by static grep in `tests/test_kernel_kernel_boundary.py`
  (Session 0 invariant 3).

#### 5. CHARTER CONTRACT
- The kernel charter (`LOOM_PROTOCOL_INSTRUCTIONS` from Session 3)
  is rendered before `system_prompt` and `role_prompt` and CANNOT
  be overridden by a policy. Policy text is appended after.
- Tested by `test_prompt_renders_kernel_charter_with_empty_policy`
  (Session 0 invariant 6).

#### Methods

| Method | Required? | Default | Purpose |
|---|:---:|---|---|
| `plan_user_turn(user_event, state) -> UserTurnPlan` | ✅ (abstract) | — | Classify a user chat event into a `UserTurnPlan`. |
| `system_prompt(participant_id, state) -> str` | ❌ | `""` | Additional system instructions appended after kernel charter. |
| `role_prompt(participant_id, state) -> str` | ❌ | `""` | Extra instructions for actors holding distinguished roles (anchor synthesis, teacher framing, debater stance). |

`name: str = "unnamed"` — class attribute; subclasses override (e.g.
`name = "single_responder"`). Used in event rationales and debug.

#### P2.7 — `prior_speaker` removed

The pre-v0.1 signature took a `prior_speaker` keyword. None of the
bundled policies used it. Removed for signature simplicity. A policy
that needs follow-up detection can compute it from `state` and recent
bus history (via `addressees.last_responsible_speaker` if needed),
or accept it as a constructor argument.

#### Why `contracts.py` lives at `loom/contracts.py` and not under `loom/policy/base.py`

The kernel must NEVER import `loom.policy` (boundary invariant 1).
But `loom/kernel/prompt.py` and `loom/runtime.py` need to type their
`policy:` parameter. Solution: put the ABC in a neutral layer that
both sides may import. `loom/kernel/__init__.py`'s docstring says it
explicitly: "Kernel modules MAY import from `loom.contracts`."

---

## adapters.py — full reference

### `class SendProxyAdapter`

Wraps a proxy object that exposes a `send(prompt) -> str` method into
a streaming proxy. Used when you have a richer object you want to
preserve (extra attrs, `cancel()` semantics) — when you just have a
function, prefer `agent_from_send`.

```python
def __init__(self, proxy: Any, send_method: str = "send") -> None:
    self._proxy = proxy
    self._send_method = send_method
    self._cancelled = False

def stream(self, prompt: str) -> Iterator[str]:
    if self._cancelled: return
    send = getattr(self._proxy, self._send_method)
    result = send(prompt)
    text = self._extract_text_static(result)
    if text:
        yield text

def cancel(self) -> None:
    self._cancelled = True
    cancel = getattr(self._proxy, "cancel", None)
    if cancel is not None:
        try: cancel()
        except Exception: pass

@staticmethod
def _extract_text_static(result: Any) -> str:
    if result is None: return ""
    if isinstance(result, str): return result
    for attr in ("text", "body", "content", "output"):
        v = getattr(result, attr, None)
        if isinstance(v, str): return v
    return str(result)
```

Yields the entire response as a single chunk. PASS detection still
works (Session 3 — the buffer accumulates the first 16 chars before
flushing). True streaming is a v0.1 enhancement.

### `_extract_text(result) -> str` (module-level helper)

Identical logic to `SendProxyAdapter._extract_text_static`. Pull a
plain string out of: `None` → `""`, `str` → the string, `obj` with
`.text` / `.body` / `.content` / `.output` → that attr, else
`str(result)`. Returns `""` for `None` so the caller can detect "no
draft".

### `class _FunctionAgent` (internal — concrete `Agent`)

The concrete class returned by all three `agent_from_*` factories.
Uses `__slots__` for footprint.

```python
__slots__ = (
    "id", "persona", "capability_block", "cost_tier", "capable",
    "_stream_callable", "_cancel_callable", "_cancelled",
)

def __init__(self, agent_id, stream_callable, *,
             persona="", capability_block="",
             cost_tier=1, capable=True, cancel_callable=None):
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("agent id must be a non-empty string")
    # ... assign all fields ...

def stream(self, prompt):
    if self._cancelled: return
    for chunk in self._stream_callable(prompt):
        if self._cancelled: return        # re-check per chunk
        if chunk: yield chunk             # filter empty chunks

def cancel(self):
    self._cancelled = True
    if self._cancel_callable is not None:
        try: self._cancel_callable()
        except Exception: pass
```

Notes:
- **`agent_id` must be non-empty string** — validated at construction.
- **`_cancelled` is re-checked between chunks** so a cancel from
  another thread mid-stream cuts off subsequent yields.
- **Empty chunks are filtered** before yielding — adapters can return
  `""` chunks without polluting the bus.
- **`_cancel_callable` failures are swallowed** — best-effort.

### `agent_from_send(agent_id, send_fn, *, persona, capability_block, cost_tier=1, capable=True, cancel_fn=None) -> _FunctionAgent`

Wrap a non-streaming `send(prompt) -> str | obj-with-.text` callable.
The returned agent yields the full reply as a single chunk.

```python
def _stream(prompt):
    result = send_fn(prompt)
    text = _extract_text(result)
    if text:
        yield text
```

Validates `callable(send_fn)`. PASS detection still works (Session 3
buffer threshold is 16 chars; the full reply lands in the buffer
in one go and is matched against `PASS_RE` before the threshold
flushes).

### `agent_from_stream(agent_id, stream_fn, ..., cancel_fn=None) -> _FunctionAgent`

Wrap a streaming callable that yields chunks. The callable should be
**re-callable** — every `stream(prompt)` call must produce a fresh
iterable.

```python
def _stream(prompt):
    for chunk in stream_fn(prompt):
        if isinstance(chunk, str):  yield chunk
        elif chunk is None:          continue
        else:                        yield str(chunk)
```

Type coercion: strings pass through, `None` is skipped, other types
go through `str()`. `_FunctionAgent` then filters empty chunks again
in its outer loop.

### `agent_from_object(agent_id, obj, *, persona=None, capability_block=None, cost_tier=None, capable=None) -> _FunctionAgent`

Wrap an existing client object exposing `.stream` or `.send`.

Resolution order:
1. **`obj.stream` callable → `agent_from_stream`**.
2. **`obj.send` callable → `agent_from_send`**.
3. Else **`TypeError`**.

Optional metadata kwargs override values pulled from `obj`. Resolution:
- If kwarg is non-None → use it.
- Else `getattr(obj, attr, default)` — `persona=""`, `capability_block=""`,
  `cost_tier=1`, `capable=True`.

`obj.cancel` (if callable) is forwarded — best-effort.

**Difference from the other two factories**: optional kwargs are
typed `Optional[...]` here so `None` means "fall through to obj's
attribute"; in `agent_from_send/stream` they have concrete defaults.
This is the pattern: extract from object first, let kwarg override.

---

## policy/__init__.py — re-exports

The canonical user-facing import path for policy authors. Re-exports:

```python
from loom.kernel.obligations import (
    plan_for_acknowledgement,
    plan_for_default,
    plan_with_required,
)
from loom.policy.base import BasicPolicy
from loom.policy.default import DefaultPolicy
from loom.policy.open_chat import OpenChatPolicy
from loom.policy.round_robin import RoundRobinPolicy
from loom.policy.single_responder import SingleResponderPolicy

__all__ = [
    "BasicPolicy", "DefaultPolicy", "OpenChatPolicy",
    "SingleResponderPolicy", "RoundRobinPolicy",
    "plan_for_acknowledgement", "plan_for_default", "plan_with_required",
]
```

The plan-builder helpers themselves live in `loom.kernel.obligations`
(Session 1). Re-exporting them through `loom.policy` is the supported
import path for policy authors so they don't reach into `loom.kernel.*`
(boundary invariant 1).

`loom/__init__.py` (the top-level public surface, Session 0) re-exports
the policy classes but NOT the plan-builders — those are intermediate
between "policy author" (sees them) and "library user" (doesn't need
them).

---

## policy/base.py — `BasicPolicy`

Template-method base for the common "pick a set of responders, return
either an acknowledgement or a `plan_with_required`" shape.

### When to use `BasicPolicy`

- Your policy maps each user post to a *set* of speakers (possibly
  empty), with no cross-turn state and no floor narrowing.
- You don't need a custom acknowledgement reason, custom
  `allowed_speakers` (defaults to chosen responders), or custom
  `max_responses` (defaults to `len(responders)`).

### When NOT to use `BasicPolicy` — go to `ConversationPolicy` directly

- You mutate cross-turn state (round-robin pointer, debate phase,
  20-questions count) → see `RoundRobinPolicy`.
- You need different routing per case (vocative vs broadcast vs
  game-mode) → see `DefaultPolicy`.
- You set `allowed_speakers` to a strict superset of responders, OR
  emit declarative control-state changes (`set_turn_taking_mode`,
  `set_floor_owner`, etc.) on the plan.

### Body

```python
def plan_user_turn(self, user_event, state):
    target_event_ids = [user_event.id] if user_event.id is not None else []
    responders = self._choose_responders(user_event, state)
    if not responders:
        return obl.plan_for_acknowledgement(
            target_event_ids=target_event_ids,
            rationale=self._no_responders_rationale(state),
        )
    responders_list = sorted(responders)
    return obl.plan_with_required(
        responders_list,
        routing_case=self._routing_case(),
        target_event_ids=target_event_ids,
        reason=self.name,
        rationale=self._rationale(responders_list, state),
        allowed_speakers=set(responders_list),
        max_responses=len(responders_list),
        wait_for_user_after=self._wait_for_user_after(),
        instruction=self._instruction(state),
    )
```

### Hooks

| Method | Required? | Default | Override when |
|---|:---:|---|---|
| `_choose_responders(user_event, state) -> set[str]` | ✅ | — | Always — this is your only required override. |
| `_routing_case() -> str` | ❌ | `"multi_opinion"` | Use a literal from `RoutingCase` (Session 1) when more specific. |
| `_wait_for_user_after() -> bool` | ❌ | `False` | `True` for "talk to one assistant" patterns. |
| `_instruction(state) -> str` | ❌ | `""` | Per-turn instruction rendered into actor prompts (fenced as `<instruction>` — Session 3). |
| `_rationale(responders, state) -> str` | ❌ | `self.name` | Short human-readable string written into the plan for debug. |
| `_no_responders_rationale(state) -> str` | ❌ | `"no responders chosen"` | Reason when `_choose_responders` returns empty. |

### Important detail: `responders_list = sorted(responders)`

The responders set is sorted before being passed to `plan_with_required`.
This makes the order of obligations deterministic (id-sorted). The
order is preserved through `make_user_turn` (Session 1) into the
turn's obligation dict — so emitted `obligation_recorded` events come
in id-sorted order. Important for replay determinism.

---

## policy/single_responder.py — `SingleResponderPolicy`

**Canonical reference for new policy authors.** The smallest
non-trivial policy in-tree. ~30 lines of substantive code.

```python
class SingleResponderPolicy(BasicPolicy):
    name = "single_responder"

    def __init__(self, responder_id: str) -> None:
        if not responder_id or not isinstance(responder_id, str):
            raise ValueError("…requires a non-empty responder_id")
        self.responder_id = responder_id

    def _choose_responders(self, user_event, state):
        info = state.participants.get(self.responder_id)
        if info is None or not info.active or not info.capable:
            return set()
        return {self.responder_id}

    def _routing_case(self):    return "single_responder"
    def _wait_for_user_after(self): return True
    def _instruction(self, state):
        return (f"You ({self.responder_id}) are the configured "
                "responder for this room.")
    def _rationale(self, responders, state):
        return f"single responder: {self.responder_id}"
    def _no_responders_rationale(self, state):
        return f"configured responder {self.responder_id!r} not active/capable"
```

Key details:

- **Validation in `__init__`** — fails at construction, not at first
  user post. The user sees the error immediately.
- **`_choose_responders` returns `set()` when responder unavailable**
  → `BasicPolicy` returns acknowledgement (no turn opens).
- **`_wait_for_user_after = True`** — single-responder is a directed
  pattern; once the configured responder speaks, no other agent
  should chime in. Coordinator sets `state.control.wait_for_user`
  on close.
- **`_instruction` is per-turn** — appears in the TURN CARD, fenced
  as `<instruction>` (Session 3 Phase-0 fix).

---

## policy/open_chat.py — `OpenChatPolicy`

The simplest non-trivial policy. Every user post broadcasts.

```python
class OpenChatPolicy(BasicPolicy):
    name = "open_chat"

    def _choose_responders(self, user_event, state):
        return {pid for pid, info in state.participants.items()
                if info.active and info.capable}

    def _routing_case(self):  return "broadcast"
    def _instruction(self, state):
        return "Open group chat. Reply with substance or [PASS]."
    def _rationale(self, responders, state):
        return f"open chat: broadcast to {len(responders)} agent(s)"
    def _no_responders_rationale(self, state):
        return "no active capable participants"
```

Notes:
- **No `__init__`** — stateless, no configuration.
- **`_wait_for_user_after` defaults to `False`** — broadcast doesn't
  gate subsequent posts.
- **All active capable** participants get a `must` obligation.
  Coordinator's `max_responses` defaults to `len(allowed_speakers)`,
  so the turn closes when everyone has spoken (committed or PASSed).
- **No addressing detection** — agents self-route via prompt, may
  PASS.

---

## policy/round_robin.py — `RoundRobinPolicy`

Reference for **declarative state mutation**. Subclasses
`ConversationPolicy` directly (not `BasicPolicy`) because the
rotation logic needs `set_turn_taking_mode`, `set_turn_order`, and
`advance_turn_pointer` on the plan.

### `__init__(order: list[str])`

```python
if not order:
    raise ValueError("RoundRobinPolicy requires at least one id")
seen: set[str] = set()
self._order: list[str] = []
for pid in order:
    if not isinstance(pid, str) or not pid:
        raise ValueError("…order ids must be non-empty strings")
    if pid not in seen:
        seen.add(pid); self._order.append(pid)
```

- **Validates non-empty** order.
- **Per-id type check** — non-empty string each.
- **Defensive copy + dedupe-while-preserving-order** (no `set(order)`
  because that loses order).

`order` property returns a fresh list copy.

### `plan_user_turn`

```python
active_capable = {pid for pid, info in state.participants.items()
                  if info.active and info.capable}
control = state.control

# First post — arm round-robin.
if control.turn_taking_mode != "round_robin":
    speaker = self._first_live(self._order, active_capable)
    if speaker is None:
        return obl.plan_for_acknowledgement(
            target_event_ids=…,
            rationale="round-robin start: no configured participants are active+capable",
        )
    return obl.plan_with_required(
        [speaker],
        routing_case="direct_mention",
        target_event_ids=…,
        reason="round_robin_start",
        rationale=f"round-robin start: {speaker}",
        allowed_speakers={speaker},
        max_responses=1,
        wait_for_user_after=True,
        instruction=self._instruction(speaker),
        set_turn_taking_mode="round_robin",     # ← declarative state change
        set_turn_order=list(self._order),       # ← declarative state change
        advance_turn_pointer=True,              # ← rotates on close
    )

# Subsequent posts — use kernel's pointer over live subset.
speaker = self._pick_from_rotation(control, active_capable)
if speaker is None:
    return obl.plan_for_acknowledgement(…)
return obl.plan_with_required(
    [speaker],
    routing_case="direct_mention",
    reason="round_robin",
    rationale=f"round-robin: {speaker} (idx {control.next_speaker_idx})",
    allowed_speakers={speaker},
    max_responses=1,
    wait_for_user_after=True,
    instruction=self._instruction(speaker),
    advance_turn_pointer=True,                  # ← still rotate on close
    # Note: NO set_turn_taking_mode / set_turn_order on subsequent posts —
    # the mode is already armed; we just rotate.
)
```

### Static helpers

- **`_first_live(order, active_capable) -> Optional[str]`** — first
  pid in `order` that's in `active_capable`. Used on first post.
- **`_pick_from_rotation(control, active_capable) -> Optional[str]`** —
  filters `control.turn_order` to live, then returns
  `live[control.next_speaker_idx % len(live)]`. Mirrors
  `state.advance_round_robin_pointer` (Session 1) — the pointer math
  is the same so reads are consistent.
- **`_instruction(speaker) -> str`** — "Round-robin mode: you (X) are
  up this turn. Other agents are silent until the next user post.
  Make one move, then stop."

### Why subclasses `ConversationPolicy` directly

`BasicPolicy.plan_user_turn` doesn't know about `set_turn_taking_mode`,
`set_turn_order`, `advance_turn_pointer`, or `routing_case` overrides
per-call. Round-robin needs all of those. Trying to fit it into
`BasicPolicy` would mean overriding `plan_user_turn` itself, defeating
the template.

---

## policy/default.py — `DefaultPolicy`

The v0.0 floor-aware classifier. Used by Loom (the consumer of
this kernel) and as the runtime default if `policy=` is omitted.
Subclasses `ConversationPolicy` directly (not `BasicPolicy`) because
of per-case branching.

### Module-level state

#### `ANCHOR_SYNTHESIS_INSTRUCTIONS` (str)

Anchor / default-responder role prompt. **Same text** as
`prompt.py`'s constant — both are kept because the policy module
*owns* the role-prompt content (returned by `role_prompt`), while
`prompt.py` keeps a copy for the `_FallbackPolicy` stub used when
`build_prompt` is called without a policy. The duplication is a v0.2
cleanup target (see Session 3 open question 3 — fallback policy
removal).

#### Regex constants

| Name | Purpose | Notes |
|---|---|---|
| `_ACK_PHRASES` (frozenset, 12) | Acknowledgement allow-list | "ok", "okay", "k", "kk", "thanks", "thank you", "thx", "ty", "got it", "cool", "nice", "noted", "sounds good", "sgtg" |
| `_VOCATIVE_BLACKLIST` (frozenset, 21) | Generic words that shouldn't be vocatives | "you", "i", "me", "we", "us", "they", "them", "all", "guys", "yall", "everyone", "everybody", "anyone", "anybody", "someone", "team", "folks", "ai", "bot", "model", "user", "assistant", "agent", "llm" |
| `_VOC_START_RE` | `^name[,:] ` | "alice, what do you think?" |
| `_VOC_END_RE` | `[, ]name$` after rstrip punctuation | "what do you think, alice?" |
| `_GAME_START_RE` | game-start phrases | "let's play", "20 questions", "take turns", "round-robin", "twenty questions", "would you rather", "two truths and a lie", "trivia", "riddle", "charades", "story round/together/game" |
| `_GAME_END_RE` | game-end phrases | "good game", "ggg", "end the game", "stop the game", "thanks for playing", "new topic", "moving on", "let's stop", "i'm done", "done with the game", "that's enough" |

All regexes are **module-level** for test monkeypatching.

#### Helpers

- **`_is_acknowledgement(text)`** — strip+lower, ≤3 words guard,
  rstrip terminal punctuation, exact match in `_ACK_PHRASES`.
- **`_aliases_for(participant_ids)`** — `{lowercase: pid}` map for
  vocative detection. Includes the lowercased id AND the first
  underscore segment (so `claude_code` is matched by `claude`).
  Generic blacklist filters out "you", "guys", etc. Min length: 2
  for full id, 3 for the head segment.
- **`_detect_vocative(text, addressable, *, exclude=None)`** — runs
  `_VOC_START_RE.match` and `_VOC_END_RE.search` on the cleaned
  text; returns `[pids_matched]` (deduped, order: start-match first
  then end-match).
- **5 instruction builders** (`_instruction_for_directed/floor/broadcast/round_robin/game_start`)
  — produce the natural-language hint that goes into
  `plan.instruction` (which fenced-renders into the TURN CARD —
  Session 3).
- **`_pick_rotation_speaker(control, active_capable)`** — same as
  `RoundRobinPolicy._pick_from_rotation`.

### `class DefaultPolicy(ConversationPolicy)`

Two top-level paths.

#### Path A — Round-robin mode active

(`state.control.turn_taking_mode == "round_robin"` — auto-enabled by
Path B Case 5.)

| Sub-case | Detection | Plan shape |
|---|---|---|
| **R1** | `_GAME_END_RE.search(text)` | Acknowledgement plan with `set_turn_taking_mode="broadcast"`, `set_turn_order=[]`. Exits round-robin mode. **No turn opens.** |
| **R2** | `mentioned` non-empty (from `parse_addressees`) | `plan_with_required(mentioned, …, advance_turn_pointer=False)` — preserves rotation slot across @-mention side-question. |
| **R3** | `_is_acknowledgement(text)` | `plan_for_acknowledgement` — mode stays active, no rotation advance. |
| **R4** | `_detect_vocative` non-empty | Same as R2 with `reason="vocative"`. |
| **R5** | None of above; `_pick_rotation_speaker` returns a speaker | `plan_with_required([speaker], reason="round_robin", advance_turn_pointer=True)`. |
| (fall-through) | rotation empty (no live members) | Falls through to Path B. |

#### Path B — Broadcast mode (default)

| Case | Detection | Plan shape | `wait_for_user_after` |
|---|---|---|:---:|
| **1** | `mentioned` non-empty | `plan_with_required(mentioned, allowed_speakers=set(mentioned), max_responses=len, instruction=_directed)`. Routing case: `multi_opinion` if ≥2, else `direct_mention`. | True |
| **2** | `_is_acknowledgement(text)` | `plan_for_acknowledgement` — no turn. | n/a |
| **3** | `_detect_vocative` non-empty | Same shape as Case 1, `reason="vocative"`. | True |
| **4** | `control.floor_owner` set AND has live members | `plan_with_required(floor ∩ active_capable, reason="floor_narrowed", allowed_speakers=set(floor_active), instruction=_floor)`. | True |
| **5** | `len(active_capable) >= 2 AND _GAME_START_RE.search(text)` | `plan_with_required(active_capable, reason="game_start", set_turn_taking_mode="round_robin", set_turn_order=sorted(active_capable))`. The opening turn broadcasts (each agent can propose/accept the game), but subsequent posts route through Path A. | True |
| **6** | `active_capable` non-empty (default) | `plan_with_required(active_capable, reason="broadcast", instruction=_broadcast)`. | False |
| (fall-through) | empty `active_capable` | `plan_for_default(None, reason="no active participants")`. | n/a |

### `role_prompt(participant_id, state) -> str`

Returns `ANCHOR_SYNTHESIS_INSTRUCTIONS` if `participant_id in
{state.anchor_id, state.default_responder_id}` (filtered to non-None).
Same condition as the legacy `_FallbackPolicy` in `prompt.py`.

`system_prompt` is not overridden — defaults to `""`.

---

## Comparison table — all four bundled policies

| Policy | `name` | Subclass of | Anchor respected | DM | Floor narrowing | Round-robin | @-mention/vocative | Stateless |
|---|---|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `OpenChatPolicy` | `"open_chat"` | `BasicPolicy` | No | bypassed* | No | No | No | Yes |
| `SingleResponderPolicy(id)` | `"single_responder"` | `BasicPolicy` | No | bypassed* | No | No | No | Yes |
| `RoundRobinPolicy(order)` | `"round_robin"` | `ConversationPolicy` (direct) | No | bypassed* | No | Yes (config + declarative state) | No | Yes (rotation in journaled control state) |
| `DefaultPolicy` | `"default"` | `ConversationPolicy` (direct) | Yes (via `role_prompt`) | bypassed* | Yes (Case 4) | Yes (Case 5 game-start auto-arms; Path A handles rotation) | Yes (Cases 1, 3) | Yes (mode flips via `UserTurnPlan`) |

\* "bypassed" in the DM column means the kernel's `/dm` handler builds
a `plan_for_default(target, ...)` directly without calling
`policy.plan_user_turn`. DM routing is a kernel-mechanism concern,
not a policy concern.

When to choose which:

- **OpenChat**: prototyping, broadcast rooms, no ceremony.
- **SingleResponder**: single-assistant chatbot skeleton; teaches the
  template-method pattern (canonical reference for new authors).
- **RoundRobin**: turn-based games, debate phases with fixed order;
  reference for declarative state mutation.
- **Default**: production-grade Loom v0.0 behavior; vocative +
  floor + game-start. Use when the room is realistic and users may
  use natural addressing.

---

## Boundary cross-check — `tests/test_kernel_kernel_boundary.py`

The 5 boundary tests we listed in Session 0 invariant 1-7. After
reading the policies, here's how each is enforced in this code:

| Invariant | Enforcement | Where |
|---|---|---|
| **1.** Kernel does not import `loom.policy` | Static grep over `loom/kernel/**/*.py` | `test_kernel_does_not_import_policy` |
| **2.** Kernel may import `loom.contracts` | Sanity import test | `test_kernel_may_import_contracts` |
| **3.** Policy does not mutate state or post to bus | Static grep for `state.add_*`, `state.set_*`, `state.remove_*`, `state.control =`, `bus.post(` over `loom/policy/**/*.py` | `test_policy_does_not_mutate_state` |
| **4.** Policy does not import coordinator/journal | Static grep over `loom/policy/**/*.py` | `test_policy_does_not_import_coordinator`, `test_policy_does_not_import_journal` |
| **5.** Policy errors fail closed | Coordinator + throwing policy ⇒ `policy_error` event + turn closes (default mode) | `test_policy_error_fails_closed` |
| **6.** `build_prompt` always renders kernel charter | With stub policy, output contains `LOOM_PROTOCOL_INSTRUCTIONS` | `test_prompt_renders_kernel_charter_with_empty_policy` |

Quick verification by inspection:

- **Invariant 1**: All 5 policies in this session — none import
  `loom.policy.*`. They import `loom.contracts`, `loom.kernel.events`,
  `loom.kernel.obligations`, `loom.kernel.room`, `loom.kernel.addressees`.
- **Invariant 4**: None import `loom.kernel.coordinator` or
  `loom.kernel.journal`. ✓
- **Invariant 3** (state mutation): grep these 5 files for
  `state.add_/set_/remove_/control =` and `bus.post(`:
  - `state.set_*`: `RoundRobinPolicy` and `DefaultPolicy` set
    `set_turn_taking_mode` / `set_turn_order` on the **plan**
    (`plan.set_turn_taking_mode = "broadcast"`), NOT on the state.
    The names match by coincidence. The grep uses `state.set_*`
    (with the `state.` prefix) — these don't trigger.
  - `bus.post(`: zero occurrences.
  - All clean. ✓

---

## Invariants (this session's additions)

102. **`loom/contracts.py` is the neutral layer** that both
     `loom.kernel.*` and `loom.policy.*` may import. The `Agent`
     Protocol AND the `ConversationPolicy` ABC live here — not in
     `loom/policy/base.py` — so the kernel can type-annotate against
     them without violating the import asymmetry.
103. **`Agent` is `runtime_checkable`** — `isinstance(x, Agent)`
     works for duck-typed objects. `tests/property/test_ux_contracts.py`
     asserts this for every bundled adapter.
104. **`Agent` optional attrs use `getattr` defaults**: persona/cap
     block default `""`, cost_tier `1`, capable `True`, cancel
     no-op. The `_FunctionAgent` `cost_tier` default is `1`, but
     `ParticipantInfo.cost_tier` (Session 1) defaults to `0` —
     minor inconsistency worth noting.
105. **`agent_id` must be a non-empty string** — validated in
     `_FunctionAgent.__init__`. Catches misconfiguration at
     wiring time.
106. **`_FunctionAgent` re-checks `_cancelled` per chunk** so a
     `cancel()` from another thread mid-stream cuts off subsequent
     yields. Empty chunks are filtered before yielding.
107. **`agent_from_object` resolution**: `.stream` wins over `.send`
     when both are present. Optional kwargs override; else `getattr`.
108. **The `_extract_text` fallback chain** is `None → ""`,
     `str → str`, then `obj.text/.body/.content/.output → str`,
     else `str(obj)`. Designed to accept the broadest set of
     provider response shapes without bespoke per-provider code.
109. **`SendProxyAdapter._extract_text_static` and `_extract_text`
     are duplicate code.** The static method exists for back-compat
     with the class form (it predates the module-level helper).
     Cosmetic.
110. **`ConversationPolicy.plan_user_turn` must be sync, deterministic,
     <10ms.** Coordinator holds its lock across the call (Session 5
     invariant 75). 100ms threshold triggers `policy_slow`.
111. **The `prior_speaker` keyword was removed in P2.7.** A policy
     that needs follow-up detection can compute it from `state` and
     `bus.snapshot` (e.g. via
     `addressees.last_responsible_speaker`).
112. **`name: str = "unnamed"` is on the ABC** — every subclass
     overrides. Used in event rationales and as the default
     `reason` field on the plan via `BasicPolicy`.
113. **`BasicPolicy` sorts responders before passing to
     `plan_with_required`** so obligation order is deterministic
     across replays (id-sorted).
114. **`BasicPolicy._wait_for_user_after` defaults to `False`**
     because broadcast (the most common shape) does NOT gate
     subsequent posts. Single-responder patterns override to
     `True`.
115. **`SingleResponderPolicy` is the canonical reference for
     new policy authors** (per the module docstring AND
     `docs/writing-a-policy.md`). 30 lines of substantive code.
116. **Inactive / not capable responder in `SingleResponderPolicy`
     yields acknowledgement** (no error), so the user can fix the
     room state without re-instantiating the policy.
117. **`OpenChatPolicy` is stateless and configuration-free** — no
     `__init__`. Choose responders = filter active+capable.
118. **`RoundRobinPolicy` subclasses `ConversationPolicy` directly**,
     not `BasicPolicy`, because it needs declarative state mutation
     (`set_turn_taking_mode`, `set_turn_order`, `advance_turn_pointer`)
     on the plan. Trying to fit it into the template would defeat
     the template.
119. **`RoundRobinPolicy.__init__` defensively dedupes order** while
     preserving order. Per-id type checks.
120. **`RoundRobinPolicy` distinguishes "first post" from
     "subsequent posts"** by checking
     `control.turn_taking_mode != "round_robin"`. The first post
     arms the mode AND picks the first speaker; subsequent posts
     just rotate.
121. **Both `RoundRobinPolicy` and `DefaultPolicy` set
     `advance_turn_pointer=True` on rotation-derived plans and
     `False` on @-mention/vocative overrides** so side-questions
     preserve the rotation slot.
122. **`DefaultPolicy` has TWO copies of
     `ANCHOR_SYNTHESIS_INSTRUCTIONS`** — one in `prompt.py` for
     `_FallbackPolicy`, one here for the policy's `role_prompt`.
     Cleanup target when `_FallbackPolicy` is removed.
123. **All `DefaultPolicy` regexes are module-level** for test
     monkeypatching.
124. **`_aliases_for` maps both the full lowercased pid AND the
     first underscore segment** so `claude_code` is matched by
     `claude`. Generic words (`you`, `guys`, `team`, etc.) are
     filtered via `_VOCATIVE_BLACKLIST`.
125. **`DefaultPolicy` Case 1 (direct mention) has the highest
     priority in broadcast mode** — overrides ack, vocative, floor,
     game-start, broadcast.
126. **Case 5 (game-start) needs `len(active_capable) >= 2`**.
     Single-agent rooms can't enter round-robin via game phrases.
127. **The fall-through (no active capable) returns
     `plan_for_default(None, ...)`** which itself returns a
     `requires_response=False` plan with `routing_case="none"`.
     Coordinator skips opening a turn (Session 5 — empty plan
     auto-close branch).
128. **`DefaultPolicy.role_prompt` returns
     `ANCHOR_SYNTHESIS_INSTRUCTIONS` for actors holding either
     anchor or default_responder slots**. This is appended after
     the kernel charter (Session 3 prompt assembly).
129. **No policy in v0.1.2 overrides `system_prompt`** — only
     `role_prompt`. Custom policies that need additional
     system-level instructions should override `system_prompt`.

---

## Verification

> *Write a 30-line custom policy stub that "first 3 turns broadcast,
> then route to chair only" — using only the legal API surface.*

```python
# my_policy.py
from loom.contracts import ConversationPolicy
from loom.kernel.events import Event
from loom.kernel.room import RoomStateView
from loom.policy import plan_for_acknowledgement, plan_with_required


class FirstThreeBroadcastThenChair(ConversationPolicy):
    """Broadcasts the first 3 user turns; afterwards routes only to
    state.chair_id. If no chair is set when the cap kicks in, falls
    back to acknowledgement (so the room doesn't hang on a missing
    chair)."""

    name = "first_three_then_chair"

    def __init__(self) -> None:
        self._user_turns_seen = 0       # in-process state; resets on restart (v0)

    def plan_user_turn(self, user_event: Event,
                       state: RoomStateView):
        self._user_turns_seen += 1
        target_event_ids = (
            [user_event.id] if user_event.id is not None else []
        )
        active_capable = sorted(
            pid for pid, info in state.participants.items()
            if info.active and info.capable
        )

        if self._user_turns_seen <= 3:                      # broadcast phase
            if not active_capable:
                return plan_for_acknowledgement(
                    target_event_ids=target_event_ids,
                    rationale="no active capable participants")
            return plan_with_required(
                active_capable,
                routing_case="broadcast",
                target_event_ids=target_event_ids,
                reason=self.name,
                rationale=f"broadcast turn {self._user_turns_seen}/3",
                allowed_speakers=set(active_capable),
                max_responses=len(active_capable),
                wait_for_user_after=False,
                instruction="Open chat. Reply with substance or [PASS].",
            )

        chair = state.chair_id                              # chair-only phase
        if chair is None or chair not in state.participants:
            return plan_for_acknowledgement(
                target_event_ids=target_event_ids,
                rationale="no chair set; turn skipped")
        info = state.participants[chair]
        if not info.active or not info.capable:
            return plan_for_acknowledgement(
                target_event_ids=target_event_ids,
                rationale=f"chair {chair!r} not active/capable")
        return plan_with_required(
            [chair],
            routing_case="single_responder",
            target_event_ids=target_event_ids,
            reason=self.name,
            rationale=f"chair-only phase: {chair}",
            allowed_speakers={chair},
            max_responses=1,
            wait_for_user_after=True,
            instruction=f"You ({chair}) are the chair from now on; "
                        "reply directly to the user.",
        )
```

Things this stub does correctly (per the boundary contract):

- **Subclasses `ConversationPolicy`** (not `BasicPolicy`) because the
  per-call branching depends on cross-turn state (`_user_turns_seen`)
  AND state-derived routing (`state.chair_id`) — neither fits the
  template.
- **All imports are legal**: `loom.contracts.ConversationPolicy`,
  `loom.kernel.events.Event` (a kernel module, but the policy CAN
  import it for type hints — only the coordinator/journal modules are
  blacklisted), `loom.kernel.room.RoomStateView`, plan-builders from
  `loom.policy`. **No `loom.kernel.coordinator` or
  `loom.kernel.journal` import.**
- **Reads `state.chair_id`, `state.participants`, `info.active`,
  `info.capable`** — all read-only access on the view.
- **Does not mutate `state.*`** — no `state.add_/set_/remove_/control =`.
- **Does not post to the bus** — no `bus.post(...)`.
- **Returns a `UserTurnPlan` via `plan_for_acknowledgement` or
  `plan_with_required`** — no direct dataclass construction.
- **Synchronous, no I/O, deterministic**: passes the performance
  contract.
- **Validates `chair` exists and is active+capable** before
  returning a plan that obligates them — fail-soft via
  acknowledgement rather than hanging the turn.
- **`wait_for_user_after=True` in chair-only phase** — directed turn,
  no other agents should chime in.
- **Uses real `routing_case` literals** — `"broadcast"`,
  `"single_responder"` — both valid per `_VALID_ROUTING_CASES`
  (Session 1).

State limitation: `_user_turns_seen` resets on restart (Session 6
contract: STATE CONTRACT — policies are not journaled in v0).
Acceptable for this stub; v0.1+ snapshot/restore lifecycle hooks
would let it persist.

To use it in a room:

```python
from loom import LoomRoom, agent_from_send

room = LoomRoom(
    agents=[agent_from_send("a", a_send),
            agent_from_send("b", b_send)],
    policy=FirstThreeBroadcastThenChair(),
)
with room:
    # First call to room.set_chair("b") sets the chair before turn 4.
    room.set_chair("b")
    for _ in range(5):
        room.post_and_wait("...")
```

---

## Cross-references

- depends on: `00-orientation.md` (boundary invariants),
  `01-kernel-primitives.md` (`Event`, `UserTurnPlan`,
  `ResponseObligation`, plan-builders, `RoutingCase` literal),
  `02-kernel-bus.md` (the bus.post `SenderMismatchError` is what
  makes "policies can't post" enforced at runtime; the grep test
  catches it at CI time), `03-kernel-prompt-streaming.md` (the
  fenced rendering of `<persona>`/`<topic>`/`<capabilities>`/`<instruction>`,
  the `_FallbackPolicy` stub that mirrors `DefaultPolicy.role_prompt`),
  `04-kernel-actor-journal.md` (`is_direct_mention` / direct mention
  triggers; the actor reads `state.participants` for trigger
  classification), `05-kernel-coordinator.md` (every `UserTurnPlan`
  field the policies set, the `_apply_plan_state_changes_locked`
  pathway, the `acquire_lease` `allowed_speakers` gate, the
  `policy_error_mode` dispatch).
- depended on by:
  - `loom/runtime.py` (Session 7) — `build_loom_session` accepts a
    `ConversationPolicy` and threads it as `classify_fn` into
    `coordinator.post_user_event_and_open_turn`.
  - `loom/__init__.py` (Session 0) — re-exports `Agent`,
    `ConversationPolicy`, `agent_from_send/stream/object`,
    `DefaultPolicy`, `OpenChatPolicy`, `SingleResponderPolicy`,
    `RoundRobinPolicy`, `SendProxyAdapter`.
  - `loom/testing.py` (Session 7) — provides `make_test_state`,
    `assert_no_state_mutation`, `FakeProxy`, `RecordReplayProxy`
    that policy + adapter authors use to test their work.
  - All 4 example scripts (Session 7/9) consume one of the bundled
    policies and one of the adapter factories.

## Open questions / things to revisit

1. **`SendProxyAdapter._extract_text_static` duplicates
   `_extract_text`.** Cosmetic; could be unified.
2. **`Agent.cost_tier` default is `1` per docstring**, but
   `ParticipantInfo.cost_tier` defaults to `0`. Two different
   defaults for "the same" concept. Worth picking one.
3. **`DefaultPolicy.ANCHOR_SYNTHESIS_INSTRUCTIONS` duplicates
   `prompt.py`'s constant.** Will be deduplicated when
   `_FallbackPolicy` is removed (Session 3 open question 3).
4. **`_VOCATIVE_BLACKLIST` is hard-coded.** Custom policies that
   want to extend the blacklist (e.g. for a domain-specific
   participant id "claude" that overlaps with the model name) would
   need to subclass `DefaultPolicy` and override `_detect_vocative`
   — not exposed as a constructor parameter. Worth flagging if a
   v0.2 author wants to extend.
5. **`_GAME_START_RE` includes "twenty questions" / "20 questions"**
   but rooms in non-English locales don't get this for free.
   Internationalisation is out of scope for v0; if a v0.2 brings
   it, this regex becomes a per-locale lookup.
6. **`RoundRobinPolicy.order` is a list[str] passed once in
   `__init__` and never mutable** through the policy. To rotate the
   order itself (not the pointer), the user must construct a new
   policy. v0.2 might add `RoundRobinPolicy.set_order(new_order)`
   that emits a synthetic plan to update `set_turn_order` on the
   next post.
7. **Path A's R2/R4 (round-robin + @-mention/vocative) emits
   `routing_case="multi_opinion"` for ≥2 mentions** even though
   the rotation slot is preserved. The `routing_case` says nothing
   about preservation; tools that filter by routing_case may want
   a more specific case like `"round_robin_side_question"`.
   Cosmetic.
8. **`BasicPolicy._instruction(state)` only sees `state`**, not
   `responders` or `user_event`. A policy that wants the
   instruction to mention the recipient's id (like
   `SingleResponderPolicy`'s "You ({pid}) are the configured
   responder…") must store the id on the policy instance. Adding
   `_instruction(responders, state)` would be a minor API tweak.
9. **No policy in v0.1.2 overrides `system_prompt`.** Authors who
   want additional system-level rules will add the first
   non-`role_prompt` override. Worth confirming the kernel charter
   stays first when this happens (Session 3 invariant 31 — yes,
   the charter is rendered before policy text).
10. **`_FunctionAgent` is named `_`-prefixed (private).** Adapter
    authors who want to extend it (e.g. add a `name` attribute)
    need to either subclass `_FunctionAgent` (using a private
    name) or write a class from scratch. Promoting to
    `FunctionAgent` would be a public-surface bump; not necessary
    today.
