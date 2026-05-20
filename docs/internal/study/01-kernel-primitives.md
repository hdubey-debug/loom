# 01 — Kernel primitives: events, room, obligations, user_turn

This is **Session 1** of the Loom kernel deep-study curriculum. These
four files are the data foundation: every later module composes them.
None of them imports another kernel module (except `user_turn` →
`obligations`), so a change here ripples outward.

State as of Loom v0.1.2 (2026-05-08).

## Files covered

| File | LOC | Role | Imports from kernel |
|---|---:|---|---|
| `loom/kernel/events.py` | 774 | Event dataclass, kinds, factories, JSONL serialise, secret scrubbers | none |
| `loom/kernel/room.py` | 463 | `RoomConfig`, `RoomState`, `RoomControlState`, `ParticipantInfo`, read-only views | none |
| `loom/kernel/obligations.py` | 326 | `ObligationLevel`, `ResponseObligation`, `UserTurnPlan`, plan builders | none |
| `loom/kernel/user_turn.py` | 258 | `UserTurn`, lifecycle helpers (debounce, completion) | `obligations` |

Total: ~1,820 LOC. All four are pure dataclasses + free functions; **no
threading, no I/O, no kernel cross-deps** (`user_turn` → `obligations`
is the only intra-kernel edge).

## Mental model

```
                 RoomConfig (frozen)
                       │
                       ▼
                 ┌──────────┐         RoomStateView (read-only,
                 │RoomState │◄──.view()──── frozen, MappingProxy +
                 │ (mutable │                tuples, given to policies)
                 │  + epoch)│
                 └────┬─────┘
                      │ embeds
                      ▼
                RoomControlState
                (roles, floor, mode,
                 turn_order, …)

Per-turn data (frozen at open, mutated as drafts come in):

   user post ──► classify ──► UserTurnPlan (frozen)
                                    │
                              make_user_turn(plan)
                                    │
                                    ▼
                              ┌──────────┐
                              │ UserTurn │  obligations: dict[id, RO]
                              │  state:  │  drafted: set[pid]
                              │  open    │  speaker_counts: dict[pid,n]
                              │ /closed  │  closure_reason: enum
                              └────┬─────┘
                                   │ contains many
                                   ▼
                          ResponseObligation
                          (id, pid, level=must|
                           should|may, target_evs,
                           resolved, resolved_by_event_id)

Every state read by a policy or actor flows from this skeleton.
Every state write originates in the coordinator (Session 5).
```

Bus + coordinator (Sessions 2 + 5) compose these, but at this layer
nothing knows about threads, locks, or persistence yet.

---

## events.py — full reference

### Type aliases

| Name | Values |
|---|---|
| `EventKind` | `"chat"`, `"control"`, `"stream"`, `"system"`, `"topic"`, `"presence"`, `"summary"` |
| `UserTurnCloseReason` | `"completed"`, `"idle_timeout"`, `"new_user_post"`, `"cancelled"`, `"topic_changed"`, `"no_responder"`, `"obligation_unresolved"` |
| `ObligationLevel` | `"may"`, `"should"`, `"must"` |
| `StreamEndStatus` | `"committed"`, `"suppressed"`, `"cancelled"`, `"error"`, `"lease_expired"`, `"passed"` |

`_VALID_KINDS` (frozenset) mirrors `EventKind`; used by
`_validate_event_dict`. `CONTROL_TYPES` (frozenset, 19 entries) is the
allowlist for `_control()`.

### `class Event` (dataclass, slots=True, NOT frozen)

`slots=True` — ~30% smaller per-instance footprint, ~5-10% faster attr
access. **Cannot be frozen** because `MessageBus.post` writes back into
`id` and `ts` after construction (Session 2).

| Field | Type | Default | Notes |
|---|---|---|---|
| `kind` | `EventKind` | required | One of the 7 strings above. |
| `sender` | `str` | required | participant id, `"user"`, or `"system"`. |
| `body` | `Any` | required | `str` for chat/system/summary/topic; `dict` for control/stream. |
| `channel` | `str` | `"main"` | `"main"` or `"dm:<pid>"`. |
| `addressees` | `list[str]` | `[]` | Direct @-mention targets, used by `is_direct_mention`. |
| `room_epoch` | `int` | `0` | Room epoch at post time; used by leases for self-invalidation. |
| `user_turn_id` | `Optional[int]` | `None` | Bound when posted within a turn. |
| `meta` | `dict` | `{}` | Implementation-only sidecar; **must not render to LLM** (enforced by `tests/property/test_event_meta_no_render.py`). |
| `id` | `int` | `0` | Assigned by bus on post (monotonic). |
| `ts` | `float` | `0.0` | Wall-clock `time.time` assigned by bus. **Do NOT compare to `time.monotonic`** — duration math (idle, debounce, throttle, leases) uses monotonic; `ts` is for journal lines + replay correlation only. |

#### Methods

- **`to_jsonl() -> str`** — direct field-access dict → JSON string. Uses
  optional `orjson` (~5-10x faster) when installed; falls back to
  stdlib `json`. Field order matches dataclass order so `from_jsonl
  ∘ to_jsonl` round-trips bit-stably across releases.
- **`Event.from_jsonl(line: str) -> Event`** (classmethod) — parses,
  validates per-kind body shape via `_validate_event_dict`, filters to
  `_EVENT_FIELDS` so unknown extra keys raise `EventShapeError` rather
  than `TypeError`. The journal replay path catches `EventShapeError`
  and surfaces `journal_corruption` instead of propagating.

### Validation (used by `from_jsonl`)

- **`_is_int(v)`** — `isinstance(v, int) and not isinstance(v, bool)`.
  Critical: JSON `true`/`false` parse to Python `True`/`False`, both
  `int` subclasses; without this, a tampered `"id": true` slips through.
- **`_is_number(v)`** — `int|float` minus `bool`.
- **`_validate_body_for_kind(kind, body)`** — `chat|system|summary|topic`
  require `str`; `control` requires `dict` with non-empty `control_type`
  string; `stream` requires `dict` with `stream_event ∈ {start,delta,end}`
  and integer `lease_id`. `presence` is intentionally permissive.
- **`_validate_event_dict(d)`** — top-level shape: `kind` is a string in
  `_VALID_KINDS`; `sender`/`channel`/`addressees-each` are `str`;
  `addressees` is `list`; `room_epoch`/`id` are int; `user_turn_id` is
  int or `None`; `meta` is `dict`; `ts` is number; then defers to
  `_validate_body_for_kind`.

### Secret scrubbing

- **`_SECRET_PATTERNS`** — 7 regexes covering OpenAI/Anthropic `sk-`,
  explicit `sk-ant-`, generic `Bearer …`, AWS `AKIA…`, JWT
  (`eyJ…\.…\.…`), Google `AIza…`, Google OAuth `ya29.…`.
- **`register_secret_scrubber(callable)`** — adapter-installed scrubber.
  Idempotent. Run AFTER kernel defaults. A buggy scrubber that raises
  is silently skipped (the error path must never fail).
- **`clear_secret_scrubbers()`** — test-only.
- **`redact_error_text(s, max_chars=500) -> str`** — kernel-boundary
  scrub: (a) coerce non-str via `str()`, (b) apply `_SECRET_PATTERNS`,
  (c) apply `_ADAPTER_SCRUBBERS`, (d) length-cap with `…` ellipsis. Used
  in `stream_end`, `actor_error`, `journal_error`, `journal_corruption`,
  `journal_truncated`. Empty/`None` input returns `""`.
- **`_REDACT_PLACEHOLDER`** — `"[redacted-secret]"`.

### Control event constructors (one per `CONTROL_TYPES` entry)

`_control(control_type, **payload) -> Event` is the private
allowlist-guarded constructor; raises `ValueError` on unknown type.

Public factories:

| Factory | control_type | Payload |
|---|---|---|
| `topic_changed(old, new)` | `topic_changed` | `old`, `new` |
| `participant_added(pid, role_hints?)` | `participant_added` | `id`, `role_hints` |
| `participant_removed(pid)` | `participant_removed` | `id` |
| `user_turn_opened(uid, *, routing_case, required_participants, optional_participants?, rationale)` | `user_turn_opened` | `user_turn_id`, `routing_case`, `required_participants`, `optional_participants`, `rationale` |
| `user_turn_closed(uid, reason)` | `user_turn_closed` | `user_turn_id`, `reason` |
| `obligation_recorded(oid, pid, level, target_event_ids, reason)` | `obligation_recorded` | `obligation_id`, `participant_id`, `level`, `target_event_ids`, `reason` |
| `obligation_resolved(oid, pid, resolved_by_event_id)` | `obligation_resolved` | `obligation_id`, `participant_id`, `resolved_by_event_id` |
| `dead_letter(orig_mention_event_id, reason, reroute_to=None)` | `dead_letter` | `original_mention_event_id`, `reroute_to`, `reason` |
| `default_responder_changed(old_id, new_id)` | `default_responder_changed` | `old_id`, `new_id` |
| `roles_assigned(roles)` | `roles_assigned` | `roles` (full new mapping; empty = cleared) |
| `floor_updated(*, floor_owner=None, wait_for_user=None)` | `floor_updated` | only specified fields appear; `floor_owner=[]` clears, `None` = unchanged. **Note**: `active_goal` was removed in P2.3 — topic changes flow through `topic_changed`. |
| `style_changed(old, new)` | `style_changed` | `old`, `new` |
| `journal_error(exc_class, message)` | `journal_error` | `exception_class`, `message` (scrubbed) |
| `actor_error(pid, exc_class, message)` | `actor_error` | `participant_id`, `exception_class`, `message` (scrubbed) |
| `journal_corruption(line_offset, raw_excerpt, error_class, error_message)` | `journal_corruption` | all fields with `redact_error_text` on excerpt + message; excerpt cap 120 |
| `journal_truncated(line_offset, raw_excerpt)` | `journal_truncated` | excerpt cap 120 |
| `snapshot_dropped(dropped_total, queue_depth)` | `snapshot_dropped` | both ints |

Not exposed as standalone functions but allowed in `CONTROL_TYPES`:
`chair_changed`, `anchor_changed`, `default_summarizer_changed`,
`policy_slow`, `policy_error`. The coordinator emits these via
`_control(...)` directly.

### Stream event constructors

- **`stream_start(lease_id, participant_id, trigger_event_id) -> Event`**
- **`stream_delta(lease_id, participant_id, text) -> Event`**
- **`stream_end(lease_id, participant_id, status, error=None,
  committed_event_id=None) -> Event`** — `error` is run through
  `redact_error_text` at the kernel boundary; `committed_event_id`
  appears only when status="committed".

### Chat / system / summary

- **`chat(sender, body, *, addressees=None, channel="main",
  user_turn_id=None, room_epoch=0, meta=None) -> Event`**
- **`system(body, **kwargs) -> Event`** — sender hardcoded `"system"`.
- **`summary(body, *, channel="main", room_epoch=0, meta=None) -> Event`**
  — canonical compaction output.

### Helpers

- **`control_type_of(ev)`** — returns `body["control_type"]` for
  control events, else `None`.
- **`stream_event_of(ev)`** — returns `"start"|"delta"|"end"` for
  stream events.
- **`is_direct_mention(ev, pid)`** — `ev.kind == "chat" and pid in
  ev.addressees`.
- **`is_known_control(ev)`** — true iff control event AND
  `control_type` is in current `CONTROL_TYPES`. Used at journal replay
  to filter retired control types from older sessions (e.g. legacy
  `mode_changed`, `debate_turn`, `forfeit`, `debate_end`).

---

## room.py — full reference

### Type aliases

- `StyleLevel = Literal["brief", "normal", "detailed"]`
- `TurnTakingMode = Literal["broadcast", "round_robin"]`

### `class RoomConfig` (frozen dataclass)

Boot-time constants. Not in `RoomState` because they don't change at
runtime.

| Field | Default | Meaning |
|---|---:|---|
| `compact_threshold` | `50` | Trigger compaction at this many uncompacted events. |
| `user_turn_idle_timeout_s` | `20` | Close UserTurn after this many seconds of no activity. |
| `user_turn_debounce_ms` | `250` | Within this window, a new user post extends the open turn (no new turn opened). |
| `pass_buffer_chars` | `16` | Stream-prefix buffer size for `[PASS]` detection. |
| `lease_ttl_s` | `60` | Coordinator-issued lease lifetime. |
| `max_drafts_per_participant` | `1` | Substantive replies per participant per UserTurn. |

### `class RoomControlState` (mutable dataclass)

The persistent across-turn knobs. Per-turn decisions live on
`UserTurnPlan`; this is what the policy consults *between* user posts.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `roles` | `dict[str, str]` | `{}` | Task-role assignments (e.g. `{"gemini":"teacher"}`). Rendered into every selected speaker's TurnCard. |
| `floor_owner` | `Optional[list[str]]` | `None` | `None` = open floor (broadcast default). Non-empty list narrows `allowed_speakers`. |
| `wait_for_user` | `bool` | `False` | After a turn closes with `wait_for_user_after`, suppress agent wakeups until next user post. |
| `style` | `StyleLevel` | `"normal"` | Brevity preference; renders as max-length hint in TurnCard. |
| `turn_taking_mode` | `TurnTakingMode` | `"broadcast"` | Auto-flipped to `"round_robin"` on game-start phrases ("let's play a game", "20 questions") by `DefaultPolicy`; back to `"broadcast"` on game-end. |
| `turn_order` | `list[str]` | `[]` | Round-robin order; set when entering mode. |
| `next_speaker_idx` | `int` | `0` | Rotation pointer; advanced on close when plan came from rotation. |

### `class ParticipantInfo` (mutable dataclass)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | `str` | required | Participant id. |
| `capable` | `bool` | `True` | Eligibility for fallback slot resolution. `False` = observer-only. |
| `cost_tier` | `int` | `0` | Lower = preferred fallback. `0`=local/free (Gemma), `1`=cheap API, `2`=expensive frontier. |
| `active` | `bool` | `True` | Set `False` for paused or error-backoff. Excluded from fallback. |
| `role_hints` | `dict` | `{}` | Opaque metadata; published with `participant_added`. |

### `class RoomState` (mutable dataclass — single-writer: coordinator)

| Field | Type | Default | Notes |
|---|---|---|---|
| `config` | `RoomConfig` | required | Frozen boot config. |
| `room_epoch` | `int` | `0` | Bumped on membership/slot changes — leases self-invalidate when `event.room_epoch != current`. |
| `topic` | `Optional[str]` | `None` | |
| `participants` | `dict[str, ParticipantInfo]` | `{}` | |
| `anchor_id` | `Optional[str]` | `None` | Synthesiser slot. |
| `chair_id` | `Optional[str]` | `None` | UI-default speaker; no protocol privilege. |
| `default_responder_id` | `Optional[str]` | `None` | Fallback target for `plan_for_default`. |
| `default_summarizer_id` | `Optional[str]` | `None` | |
| `current_user_turn_id` | `Optional[int]` | `None` | |
| `last_compacted_event_id` | `int` | `-1` | |
| `control` | `RoomControlState` | factory | Embedded control knobs. |

#### Mutators (and their epoch behaviour)

| Method | Epoch bump? | Returns | Notes |
|---|:---:|---|---|
| `add_participant(info)` | ✅ | `None` | Raises `ValueError` if id exists. |
| `remove_participant(pid)` | ✅ | `dict[slot_name, new_value]` | Auto-resolves any slot pointing to `pid` to `cheapest_active_capable()`. Caller emits `*_changed` events. |
| `set_active(pid, active)` | ❌ | `None` | Activity changes don't bump epoch. |
| `set_topic(new_topic)` | ❌ | old | Topic changes don't affect routing. |
| `set_default_responder(pid)` | ✅ | old | Validates pid in participants if not `None`. |
| `set_anchor(pid)` | ✅ | old | Same. |
| `set_chair(pid)` | ✅ | old | Bumps despite no protocol meaning, "for consistency". |
| `set_default_summarizer(pid)` | ✅ | old | Same. |
| `set_roles(roles)` | ❌ | old roles | Filters unknown ids silently. |
| `set_floor_owner(list_or_none)` | ❌ | old | `None` or `[]` opens floor; non-empty narrows. Filters unknown ids. |
| `set_wait_for_user(bool)` | ❌ | old | |
| `set_style(level)` | ❌ | old | Validates against `StyleLevel`. |
| `set_turn_taking_mode(mode)` | ❌ | old | Switching to `"broadcast"` clears `turn_order` + `next_speaker_idx`. |
| `set_turn_order(order)` | ❌ | old | Filters unknown ids; resets pointer to 0. |
| `advance_round_robin_pointer()` | ❌ | new idx | Filters to live (active+capable in participants); modulo on filtered length. Returns 0 if no live members. |

#### Resolution helpers (read-only)

- **`cheapest_active_capable() -> Optional[str]`** — sorts by
  `(cost_tier, id)`. Returns `None` if no active+capable participant.
- **`resolve_default_responder() -> Optional[str]`** — configured if
  active+capable, else `cheapest_active_capable()`.
- **`resolve_default_summarizer() -> Optional[str]`** — same pattern.

#### Read-only view

- **`view() -> RoomStateView`** — wraps `participants` in
  `MappingProxyType`, converts `floor_owner`/`turn_order` to tuples,
  builds `RoomControlStateView`. **Cheap, no deep-copy** — mutations to
  the underlying state are visible through the view (live read-only
  window). Callers who need a frozen snapshot must copy themselves.

### `class RoomControlStateView` (frozen, read-only)

| Field | Type |
|---|---|
| `roles` | `Mapping[str, str]` (`MappingProxyType`) |
| `floor_owner` | `Optional[Tuple[str, ...]]` |
| `wait_for_user` | `bool` |
| `style` | `StyleLevel` |
| `turn_taking_mode` | `TurnTakingMode` |
| `turn_order` | `Tuple[str, ...]` |
| `next_speaker_idx` | `int` |

### `class RoomStateView` (frozen, read-only)

Mirrors `RoomState` minus `config` (the view doesn't need it; policies
read config through the kernel separately if at all). Top-level fields
cannot be reassigned. `participants` is a `MappingProxyType`.

**Known soft leak (v0.2 deep-freeze item):** `ParticipantInfo` values
inside `participants` remain mutable dataclasses. A policy that captures
one and writes `info.active = False` mutates live state. The boundary
grep + import-asymmetry test is the practical defence today.

---

## obligations.py — full reference

### Type aliases

- `ObligationLevel = Literal["may", "should", "must"]`
- `RoutingCase = Literal["direct_mention", "question", "challenge",
  "followup", "acknowledgement", "multi_opinion", "single_responder",
  "round_robin", "floor", "broadcast", "dm", "none"]`

`_VALID_ROUTING_CASES` (frozenset) mirrors the Literal at runtime.
`_validate_routing_case(value)` raises `ValueError` listing the valid
set on a bad input. P2.6 hardening — free-form strings used to slip
through and break analytics + `TurnResult.routing_case`.

### `class ResponseObligation` (mutable dataclass)

| Field | Type | Default |
|---|---|---|
| `id` | `int` | required (set by coordinator on record) |
| `participant_id` | `str` | required |
| `level` | `ObligationLevel` | required |
| `target_event_ids` | `list[int]` | required |
| `reason` | `str` | required |
| `resolved` | `bool` | `False` |
| `resolved_by_event_id` | `Optional[int]` | `None` |

Method: `resolve(*, by_event_id)` — sets both fields.

### `class UserTurnPlan` (mutable dataclass — frozen at UserTurn open)

The per-turn contract between policy and coordinator. Floor-control
fields gate lease acquisition + closure inside the coordinator;
required obligations gate clean completion.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `requires_response` | `bool` | required | If `False`, coordinator skips opening a turn entirely. |
| `routing_case` | `RoutingCase` | required | Validated in `__post_init__`. |
| `required_participants` | `set[str]` | `set()` | Ids whose `must` obligation gates closure. |
| `optional_participants` | `set[str]` | `set()` | May but need not respond. |
| `obligations` | `list[ResponseObligation]` | `[]` | Plan emits with `id=0`; coordinator allocates real ids in `make_user_turn`. |
| `target_event_ids` | `list[int]` | `[]` | User-event id(s) the plan answers to. |
| `rationale` | `str` | `""` | Debug string. |
| `confidence` | `float` | `1.0` | Classifier confidence. |
| `allowed_speakers` | `set[str]` | `set()` | Ids permitted to acquire a draft lease this turn. Defaults in `__post_init__` to `required ∪ optional` if empty. Coordinator denies leases for ids not in this set (direct-mention triggers bypass via `is_direct_mention` carve-out). |
| `max_responses` | `int` | `0` | Cap on total committed drafts. `0` defaults in `__post_init__` to `len(allowed_speakers)`. Coordinator closes turn early once cap is hit. |
| `wait_for_user_after` | `bool` | `False` | If `True`, sets `RoomControlState.wait_for_user` after close — suppresses agent wakeups until next user post. |
| `instruction` | `Optional[str]` | `None` | Short hint rendered into selected speaker's TurnCard. |
| `set_turn_taking_mode` | `Optional[str]` | `None` | If set, coordinator switches mode at turn open. |
| `set_turn_order` | `Optional[list[str]]` | `None` | If set, coordinator replaces `turn_order` (filters unknown ids, resets pointer). |
| `advance_turn_pointer` | `bool` | `False` | If `True` AND mode still `round_robin` at close, advance pointer by 1. Set on rotation-derived plans; `False` on @-mention/vocative overrides so rotation slot is preserved across side-questions. |

`__post_init__` invariants:
1. `routing_case` must be in `_VALID_ROUTING_CASES`.
2. `requires_response=True` implies `required_participants` non-empty.
3. Empty `allowed_speakers` defaults to `required ∪ optional`.
4. `max_responses ≤ 0` defaults to `len(allowed_speakers)`.

### Plan builders

| Function | Returns | Purpose |
|---|---|---|
| `plan_for_acknowledgement(*, target_event_ids=None, rationale="user message classified as acknowledgement")` | `UserTurnPlan(requires_response=False, routing_case="acknowledgement", …)` | "thanks", "ok" — runtime skips `open_user_turn`. |
| `plan_for_default(default_responder, *, reason, target_event_ids=None, rationale="fallback to default responder", instruction=None, wait_for_user_after=True)` | `UserTurnPlan` routing to single fallback (`requires_response=False, routing_case="none"` if `default_responder is None`; else single-must plan). | Used by `policy_error_mode="default_responder"` and other fallback paths. `wait_for_user_after=True` because it's a directed turn. |
| `plan_with_required(required, *, routing_case, target_event_ids, reason, rationale="", confidence=1.0, optional=None, allowed_speakers=None, max_responses=None, wait_for_user_after=False, instruction=None, set_turn_taking_mode=None, set_turn_order=None, advance_turn_pointer=False)` | Plan with one `must` obligation per `required` id (order preserved). | Most common builder. Raises `ValueError` if `required` is empty. |

---

## user_turn.py — full reference

### Type aliases

- `UserTurnState = Literal["open", "closing", "closed"]` — note
  `"closing"` exists in the Literal but `close()` jumps directly to
  `"closed"`. Reserved for future async-close work.
- `ClosureReason = Literal["completed", "idle_timeout", "new_user_post",
  "cancelled", "topic_changed", "no_responder",
  "obligation_unresolved"]`

### `class UserTurn` (mutable dataclass)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | `int` | required | Monotonic; allocated by coordinator. |
| `user_event_id` | `int` | required | Bus id of the triggering user post. |
| `started_at` | `float` | required | `time.monotonic()` value. |
| `frozen_plan` | `UserTurnPlan` | required | The plan that opened this turn. |
| `obligations` | `dict[int, ResponseObligation]` | `{}` | Keyed by obligation id. |
| `speaker_counts` | `dict[str, int]` | `{}` | Cap-counted draft counts per pid. |
| `drafted` | `set[str]` | `set()` | All participants who drafted (cap-counted OR not). |
| `state` | `UserTurnState` | `"open"` | |
| `closure_reason` | `Optional[ClosureReason]` | `None` | Set by `close()`. |
| `last_activity_at` | `float` | `0.0` | `__post_init__` defaults to `started_at` if `0.0`. |
| `debounced_event_ids` | `set[int]` | `set()` | Additional user-event ids debounced into this turn (within `user_turn_debounce_ms`). Actors treat these as additional obligation triggers. |

### Properties (read-only delegates to `frozen_plan`)

- `required_participants` — `set(plan.required_participants)`
- `optional_participants` — `set(plan.optional_participants)`
- `routing_case` — `plan.routing_case`

### Methods

- **`obligation_for(pid) -> Optional[ResponseObligation]`** — returns
  the first OPEN (`not resolved`) obligation for `pid`. v0
  deterministic interpreter emits at most one per id.
- **`mark_drafted(pid, *, count_toward_cap=True, now=None)`** — adds
  pid to `drafted`; bumps `speaker_counts[pid]` only if
  `count_toward_cap`. `count_toward_cap=False` is for direct-mention
  replies that bypass the cap. Both branches update `last_activity_at`
  to `now or time.monotonic()`.
- **`mark_obligation_resolved(obligation_id, *, by_event_id, now=None) -> bool`**
  — resolves an open obligation; returns `True` if found-and-open.
  Updates `last_activity_at`.
- **`unresolved_required() -> set[str]`** — pids whose `must`
  obligation is still open.
- **`can_draft(pid) -> bool`** — cap-respecting draft eligibility.
  Direct mentions bypass this (handled in coordinator). `_cap()` returns
  `None` today (cap is enforced at coordinator lease time); the hook is
  reserved for future per-plan caps.
- **`close(reason)`** — sets `state="closed"` and `closure_reason`.
- **`is_idle(*, idle_timeout_s, now=None) -> bool`** — `now -
  last_activity_at >= idle_timeout_s`. `now` defaults to
  `time.monotonic()`.
- **`add_obligation(pid, level, target_event_ids, reason, *,
  next_obligation_id) -> tuple[ResponseObligation, int]`** — appends
  a new obligation to an open turn at runtime; returns `(ob, next_id+1)`.
  **Used by `RoomCoordinator.unregister_participant`** to transfer a
  removed participant's required obligation onto a live fallback (the
  v0.1.2 dead-letter rerouting path — invariant 9 in the orientation
  doc).

### Module-level helpers

- **`is_user_turn_complete(turn) -> bool`** — true iff
  `not unresolved_required()`. Optional/should-level obligations don't
  gate closure in v0. A turn with no required participants is trivially
  complete.
- **`should_open_new_user_turn(prev_user_post_ts, now, debounce_ms) -> bool`**
  — debounce decision. `True` if `prev is None` OR
  `(now - prev) * 1000 >= debounce_ms`. Posts within the window append
  to the previous turn (caller updates `last_activity_at` and treats
  them as a follow-up trigger; also adds to `debounced_event_ids`).
- **`make_user_turn(turn_id, user_event_id, plan, *, started_at=None,
  next_obligation_id=None) -> tuple[UserTurn, int]`** — builds a new
  `UserTurn` from a plan. Mutates `plan.obligations` in place to
  allocate real ids (was `0`). Returns `(turn, next_id_after)` so the
  coordinator advances its monotonic counter. `started_at` defaults to
  `time.monotonic()`; `next_obligation_id` defaults to `1`.
- **`participant_is_eligible(turn, pid) -> bool`** — pid is in
  `required_participants ∪ optional_participants`. Coordinator uses at
  lease-grant time. Direct-mention triggers bypass via `is_direct_mention`.

---

## State-transition diagrams

### `UserTurn` lifecycle

```
                     should_open_new_user_turn = True
   user post ────────────────────────────────────────────► OPEN
        │                                                    │
        │ within debounce window                             │ unresolved_required = ∅
        ▼                                                    │ AND coordinator chooses
   add to debounced_event_ids                                │ to close
   on existing turn                                          ▼
                                                          CLOSED  ◄─── close(reason)
                                                          (closure_reason set)
                                                              ▲
                                                              │
              ┌───────────────┬─────────────┬─────────┬───────┴───────────────────┐
              │               │             │         │                           │
        completed       idle_timeout   new_user_post  cancelled              topic_changed
                                                                                       │
                                                                                no_responder
                                                                                       │
                                                                       obligation_unresolved
```

(`"closing"` state is in the Literal but unused today — direct jump to
`"closed"` via `close()`.)

### Epoch bump map (RoomState mutations)

```
Bumps room_epoch (leases self-invalidate):
    add_participant ✅
    remove_participant ✅
    set_default_responder ✅
    set_anchor ✅
    set_chair ✅
    set_default_summarizer ✅

No bump (state still mutates; just doesn't invalidate leases):
    set_active
    set_topic
    set_roles / set_floor_owner / set_wait_for_user
    set_style / set_turn_taking_mode / set_turn_order
    advance_round_robin_pointer
```

The asymmetry: anything that changes WHO is in the room or which
participant occupies a slot bumps the epoch; in-flight leases tied to
the old layout drop on the next snapshot read. Style/floor/mode
changes don't because they don't change the lease's identity
(participant + turn).

---

## Invariants (this session's additions)

Beyond the load-bearing invariants listed in `00-orientation.md`, this
layer adds:

12. **Event factories are the only legal construction path.** Direct
    `Event(...)` calls can produce a body that fails
    `_validate_body_for_kind` and will be quarantined on replay. Use
    the per-kind factories.
13. **`Event.id` and `Event.ts` are bus-assigned.** Construct with
    defaults (`0` / `0.0`). The bus mutates them in `post()`. This is
    why `Event` is `slots=True` but not `frozen=True`.
14. **`Event.ts` is wall-clock; never compare to `time.monotonic()`.**
    Duration math (idle, debounce, throttle, leases) uses monotonic.
13. **`_is_int` excludes `bool`.** JSON `true`/`false` parses to Python
    `True`/`False` (both `int` subclasses). Skipping this check lets a
    tampered `"id": true` slip past validation.
14. **`UserTurnPlan.requires_response=True` requires non-empty
    `required_participants`.** Enforced in `__post_init__`. Callers
    that mean "no response" use `plan_for_acknowledgement()`.
15. **`RoomState` mutators are coordinator-only.** Outside the
    coordinator, all reads go through `view()` — `RoomStateView` makes
    top-level mutation impossible. Leaf-level mutation through captured
    `ParticipantInfo` aliases is a documented soft leak (deep-freeze =
    v0.2 work).
16. **`room_epoch` bumps on membership/slot changes only**, NOT on
    activity / topic / control-state changes (see map above). In-flight
    `TurnLease` objects compare against epoch to self-invalidate.
17. **`make_user_turn` mutates `plan.obligations[*].id` in place** to
    swap placeholder `0`s for real allocated ids. Plans should not be
    re-used after passing through `make_user_turn`.
18. **`is_known_control` is the replay filter** for retired control
    types. Adding a new control type means: (a) add to `CONTROL_TYPES`
    frozenset, (b) add factory, (c) ensure replay/handler handles it.
    Renaming/removing means: (a) drop from `CONTROL_TYPES` so
    `is_known_control` returns `False`, (b) replay quietly skips old
    journal lines.
19. **Adapter scrubbers run AFTER kernel default scrubbers and must not
    raise.** A buggy scrubber is silently skipped; the error path must
    never be allowed to fail.

## Cross-references

- depends on: `00-orientation.md` (glossary, public surface).
- depended on by:
  - `bus.py` (Session 2) — uses `Event.id/ts` mutation contract, the
    `_VALID_KINDS` shape, the `visible_to(channel)` rules implied by
    `channel="dm:<id>"`.
  - `prompt.py` (Session 3) — reads `Event` for chat history; relies on
    `meta` not rendering.
  - `streaming.py` (Session 3) — emits `stream_start/delta/end`; uses
    `pass_buffer_chars` from `RoomConfig`; relies on `redact_error_text`.
  - `actor.py` (Session 4) — reads `RoomState.participants` (live, not
    view), uses `is_direct_mention`, drives `mark_drafted`,
    `mark_obligation_resolved`.
  - `journal.py` (Session 4) — `Event.to_jsonl/from_jsonl`,
    `is_known_control`, `journal_corruption/truncated/error/snapshot_dropped`
    factories.
  - `coordinator.py` (Session 5) — sole legal `RoomState` mutator;
    consumes `UserTurnPlan` via `make_user_turn`; emits every
    control-event factory; calls `add_obligation` on dead-letter reroute.
  - All policies — read `RoomStateView`, return `UserTurnPlan` via the
    builders.

## Verification

> *Trace a user post end-to-end through these primitives — what `Event`
> is created, how `RoomState` is read, what `UserTurnPlan` fields the
> policy might set, what `UserTurn` looks like at start of drafting.*

1. **The user post.** A facade caller invokes something equivalent to
   `chat(sender="user", body="hi @gpt", addressees=["gpt"])`. This
   builds an `Event(kind="chat", sender="user", body="hi @gpt",
   channel="main", addressees=["gpt"], room_epoch=0, user_turn_id=None,
   meta={}, id=0, ts=0.0)`. The bus will mutate `id` and `ts` on
   `post`; everything else is set at construction.

2. **State read by the policy.** The coordinator (Session 5) calls
   `RoomState.view()`, which produces a `RoomStateView` with
   `participants` wrapped in `MappingProxyType`, `floor_owner` as a
   tuple, `roles` as a `MappingProxyType` inside `RoomControlStateView`.
   The policy reads `view.participants`, `view.anchor_id`,
   `view.default_responder_id`, `view.control.floor_owner`,
   `view.control.turn_taking_mode`, `view.control.turn_order`,
   `view.control.next_speaker_idx`, `view.control.roles`,
   `view.control.style`, `view.control.wait_for_user` — all read-only.

3. **The plan the policy returns.** For `"hi @gpt"`, `DefaultPolicy`
   would emit a `direct_mention` plan via `plan_with_required(["gpt"],
   routing_case="direct_mention", target_event_ids=[<user_event.id>],
   reason="single_responder", rationale="...", allowed_speakers={"gpt"},
   max_responses=1, wait_for_user_after=True, instruction="...")`. The
   `__post_init__` validates `routing_case`, ensures non-empty
   `required_participants`, and (since `allowed_speakers` was set)
   leaves it alone; `max_responses=1` stays. The plan carries one
   `ResponseObligation(id=0, participant_id="gpt", level="must",
   target_event_ids=[<uid>], reason="single_responder")` — `id=0` is
   the placeholder.

4. **The UserTurn at start of drafting.** The coordinator calls
   `make_user_turn(turn_id=N, user_event_id=<uid>, plan)`. This mutates
   `plan.obligations[0].id` from `0` to (say) `1`, builds a `UserTurn`
   with `id=N`, `user_event_id=<uid>`, `started_at=time.monotonic()`,
   `frozen_plan=plan`, `obligations={1: ResponseObligation(id=1, ..., 
   level="must", resolved=False)}`, `speaker_counts={}`, `drafted=set()`,
   `state="open"`, `closure_reason=None`, `last_activity_at=started_at`,
   `debounced_event_ids=set()`. Returns `(turn, 2)` so the coordinator's
   monotonic obligation counter advances. From here, the actor for
   `"gpt"` will eventually `mark_drafted("gpt")` and
   `mark_obligation_resolved(1, by_event_id=<chat_event.id>)`,
   `unresolved_required()` becomes empty,
   `is_user_turn_complete(turn)` returns `True`, and the coordinator
   calls `turn.close("completed")`.

## Open questions / things to revisit

1. The `UserTurnState = "closing"` Literal value is currently unused —
   `close()` jumps from `"open"` to `"closed"` directly. When we work
   on async / off-lock policies (v0.2), `"closing"` may become the
   intermediate state where obligations are resolving asynchronously
   while no new leases can be granted.
2. The `_cap()` hook on `UserTurn` returns `None` — the cap is enforced
   by the coordinator at lease time. If we want per-plan caps (e.g.
   "this debate phase allows at most 2 drafts per side"), wire it here.
3. **Dead-letter via `add_obligation`** is the v0.1.2 fix (invariant 9).
   When we touch the dead-letter path in coordinator (Session 5),
   re-confirm that `add_obligation` correctly re-uses the
   `next_obligation_id` returned from the prior call so coordinator
   keeps a single monotonic counter.
4. **`UserTurnPlan.set_turn_taking_mode`** is typed as
   `Optional[str]`, NOT `Optional[TurnTakingMode]`. `RoomState.set_turn_taking_mode`
   then re-validates against the Literal at apply time. Tightening this
   to `Optional[TurnTakingMode]` would catch typos at policy-write
   time. Candidate cleanup.
5. **Soft mutation leak** through `ParticipantInfo` is the most
   commonly cited v0.2 item. The fix shape is probably an
   `ParticipantInfoView` mirroring the field set, frozen, with
   `MappingProxyType` on `role_hints`. Plan to revisit during the
   first kernel-modification session post-curriculum.
6. **`floor_updated` event** has the `active_goal` field removed (P2.3
   note in the docstring). Confirm there are no remaining references in
   the journal-replay path or in `restore_state` (Session 4).
7. **`debounced_event_ids`** lives on `UserTurn` — actors must consult
   it when deciding which user events count as a wakeup trigger. Verify
   in Session 4 (actor) and Session 5 (coordinator's `handle_user_post`
   debounce branch).
