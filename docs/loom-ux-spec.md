# Loom UX specification

This document is the in-repo reference for the kernel's user-experience
posture. The full audit, decision log, and remediation roadmap live in
`~/.claude/plans/virtual-cuddling-lantern.md`; this file is the short,
authoritative summary that ships with the code.

The audit was conducted post-security-pass on 2026-05-08; line
references in this document target the kernel state at that time.

Loom is a kernel — its users are *developers*, not chatroom end-users.
This document defines the conventions the kernel maintains so that
those developers can build on top of it without friction.

## 1. Audience

Three personas. Friction here is in scope.

| # | Persona | What they do |
|---|---|---|
| **A** | **Library author** | Imports `loom`; instantiates an `LoomRoom` with their own agents and a chosen policy; builds an application (CLI, web, IDE plugin) on top |
| **B** | **Policy author** | Subclasses `BasicPolicy` (or `ConversationPolicy` for advanced cases); implements `plan_user_turn`; ships under `loom.policy.*` or as a third-party module |
| **C** | **Adapter author** | Wraps a new LLM provider into something satisfying the `Agent` Protocol; uses `agent_from_*` or rolls a bespoke proxy class |

End-user UX (typing into a terminal, clicking through Loom) is owned
by whoever builds the front-end on top of Loom. Out of scope here.

## 2. The seven principles

Each principle is concrete enough to settle a design dispute.

### 2.1 One canonical door per task

Every user-facing task has ONE recommended call path. Other paths may
exist as power-user escape hatches but must be documented as such.

`LoomRoom` is the public facade. Methods on it (`set_topic`,
`post_and_wait`, `dm`, ...) are canonical. `room.session.coordinator.*`
is an explicit escape hatch — reaching it means you know you're past
the supported surface.

**Anti-pattern.** Two paths exist; both work; neither says "use the
other." Users pick by accident.

### 2.2 Names earn their existence

Two names for the same concept ⇒ a real distinction is documented and
load-bearing, OR one name is canonical and the other is a deprecation
alias. No middle ground.

The kernel's three near-synonyms are layered (§4.2):

- **`participant`** — state + identity term. User-facing API
  surfaces use `participant_id`.
- **`actor`** — kernel-thread-runtime term. Internal to
  `loom/kernel/actor.py` only.
- **`agent`** — public Protocol (`Agent`) and the API affordance
  for "the thing you pass into `LoomRoom(agents=[...])`."

### 2.3 Hide kernel internals from the user surface

`room_epoch`, `user_turn_id`, `lease_id`, `addressees`, `meta` exist
because the kernel needs them. Users handed an event object should
not need to understand any of them to do useful work.

Loom enforces this via a two-tier event surface (§4.3):

- `Event` — kernel-internal full-fidelity record. Lives under
  `loom/kernel/events.py`. Library authors do not import it.
- `Message` — user-facing transcript projection. Returned from
  `LoomRoom.transcript()` and `LoomRoom.subscribe()`.
- `TurnResult` — typed return from `LoomRoom.post_and_wait`.

### 2.4 Conventions ship as code (policy side)

A long ABC docstring with multiple contracts is a sign that the
contract is too implicit. Replace prose with code:

- `BasicPolicy` (template-method base class) encapsulates the standard
  "filter active+capable participants, return a `plan_with_required`
  plan or an acknowledgement" pattern. New policies subclass
  `BasicPolicy` and override one or two hooks.
- `loom.testing` ships canonical fixtures (`make_test_state`,
  `FakeProxy`, `assert_no_state_mutation`, `RecordReplayProxy`).

The convention becomes the path of least resistance — not a rule a
new author has to remember.

**Adapter side: documentation, not a base class.** `agent_from_send` /
`agent_from_stream` / `agent_from_object` already cover the common
cases. `agent_from_send` is annotated as canonical;
`docs/writing-an-adapter.md` is the tutorial. There is no
`BasicProxy` base class — the adapter surface is well-shaped without
one.

### 2.5 Defaults that work, escape hatches that exist

The four-line quickstart in `loom/__init__.py` works end-to-end.
Every config object has a default that produces a useful room.
Required arguments are exactly those without a sensible default
(e.g., `agents` — there is no default agent set; everything else has
one).

### 2.6 Discoverability over documentation

The single most useful docstring is the one on the public class the
user holds. `LoomRoom.__init__.__doc__` answers ~80% of "what can I
do?" without forcing the user to read 5 other modules.

**Operational rule.** When a user has an `LoomRoom`, `dir(obj)` plus
`help(obj)` is enough to learn the API. Reach-throughs via
`room.session.*` should fail this test on purpose (they're escape
hatches), but the facade methods should not.

### 2.7 Errors are the primary teaching surface

Every `ValueError` / `KeyError` on user input includes (a) what was
wrong, (b) what was expected, (c) the closest valid alternatives.
Errors are docs that fire when the user needs them.

Concrete: `build_loom_session(default_responder_id="typoed_id", ...)`
raises with the list of registered ids. `/topic <500-char-paste>`
rejects the input with the size cap mentioned in the message.

## 3. Public / private boundary

The kernel has four named import surfaces. Each layer has a defined
audience and stability promise.

| Import path | Audience | Stability |
|---|---|---|
| **`loom.*`** | Library author | **Public.** The `LoomRoom` facade, `Agent` Protocol, `agent_from_*` adapters, `ConversationPolicy` ABC, bundled policies (`DefaultPolicy`, `OpenChatPolicy`, `SingleResponderPolicy`, `RoundRobinPolicy`), `RoomConfig`, `Message`, `TurnResult`, `RoomStateView`. Stable (modulo v0→v0.1 break). |
| **`loom.policy.*`** | Policy author | **Public extension surface.** Re-exports `ConversationPolicy`, `BasicPolicy`, `plan_for_acknowledgement`, `plan_for_default`, `plan_with_required`. Add a new policy module here when it's bundled-quality. |
| **`loom.adapters.*`** | Adapter author | **Public extension surface.** Re-exports `agent_from_*` and `SendProxyAdapter`. Add a new bundled-quality adapter module here. |
| **`loom.kernel.*`** | Internal contributor | **Advanced; do not import unless you know what you're doing.** Internals (bus, coordinator, journal, prompt, events). Library examples and bundled docs MUST NOT import from `loom.kernel.*`. |

**Rule.** Code under `examples/` and `docs/` MUST NOT import from
`loom.kernel.*`. CI enforces this (P3.5 / `make ux-check`). The kernel
is not the surface library authors should touch.

**Implication for `LoomSession`.** It is documented as advanced — power
users importing from `loom.runtime` can still reach it, but it is
removed from `loom.__all__`. Loom and other front-ends continue to use
`build_loom_session` directly.

## 4. Locked decisions

These are the load-bearing UX decisions for v0.1. They are stable —
revisit only with a corresponding spec-doc bump.

### 4.1 Renames land as hard breaking changes

v0 is pre-stable. One clean cut beats deprecation aliases for half a
release. The migration path between v0 and v0.1 is documented in
`CHANGELOG.md` (when one exists); existing call sites are updated
in-tree as part of the change.

### 4.2 Naming canonicalization

| Layer | Canonical name | Used at |
|---|---|---|
| State + identity | `participant` | `RoomState`, `ParticipantInfo`, all string-id parameters at user-facing surfaces (`participant_id`). The kernel charter already uses "participant". |
| Kernel runtime | `actor` | `ParticipantActor` (`loom/kernel/actor.py`), `actor_id` (only inside that module). The threading-runtime term. |
| Public Protocol / API | `agent` | `Agent` Protocol; `LoomRoom(agents=[...])`. Reads more naturally than "participants" in API position. |

Migration: every public surface that takes an id parameter is named
`participant_id`. `actor_id` outside `loom/kernel/actor.py` is a CI
violation.

### 4.3 Two-tier event surface

`Event` (kernel-internal) carries full fidelity for bus, journal,
coordinator, and replay. Library authors never import it.

`Message` (user-facing) is a frozen projection: `sender`, `body`,
`channel`, `timestamp`, `kind`. Returned from `LoomRoom.transcript()`
and passed to subscriber callbacks the user registers via
`LoomRoom.subscribe(...)`.

`TurnResult` (user-facing) is the typed return of
`LoomRoom.post_and_wait`:

```python
@dataclass(frozen=True)
class TurnResult:
    messages: list[Message]
    turn_id: int
    routing_case: str           # promoted to Literal in v0.1
    closed_reason: Literal["all_obligations_resolved", "timeout",
                            "acknowledgement", "no_turn_opened"]
    participant_responses: dict[str, list[Message]]
    elapsed_s: float
```

Users who want to correlate replies to a specific user turn read
`TurnResult.turn_id` (typed, documented), not `event.user_turn_id`
(kernel-internal). The `LoomRoom` surface never exposes raw `Event`.

### 4.4 `topic` and `active_goal` collapse

`RoomState.topic` and `RoomControlState.active_goal` carry the same
semantics ("short natural-language description of current focus").
`active_goal` is the legacy name from before topic was lifted out of
control state. The two collapse to a single `topic` field on
`RoomControlState` in v0.1; snapshot bumps to v3 with a v2-restore
shim for one release.

### 4.5 `BasicPolicy` ships; `BasicProxy` does not

Policy authoring is the higher-friction surface. `BasicPolicy`
(template-method ABC) encapsulates the common shape:

```python
class BasicPolicy(ConversationPolicy):
    def plan_user_turn(self, user_event, state):
        target_event_ids = [user_event.id] if user_event.id is not None else []
        responders = self._choose_responders(user_event, state)
        if not responders:
            return plan_for_acknowledgement(
                target_event_ids=target_event_ids,
                rationale=self._no_responders_rationale(state))
        return plan_with_required(
            list(responders),
            routing_case=self._routing_case(),
            target_event_ids=target_event_ids,
            reason=self.name,
            rationale=self._rationale(responders, state),
            allowed_speakers=set(responders),
            max_responses=len(responders),
            wait_for_user_after=self._wait_for_user_after(),
            instruction=self._instruction(state))

    @abstractmethod
    def _choose_responders(self, user_event, state) -> set[str]: ...
    def _routing_case(self) -> str: return "multi_opinion"
    def _wait_for_user_after(self) -> bool: return False
    def _instruction(self, state) -> str: return ""
    def _rationale(self, responders, state) -> str: return self.name
    def _no_responders_rationale(self, state) -> str:
        return "no responders chosen"
```

`OpenChatPolicy` and `SingleResponderPolicy` rebase onto this in
v0.1. `RoundRobinPolicy` and `DefaultPolicy` keep the full ABC — they
need state mutation hooks (`set_turn_taking_mode`, floor narrowing)
that don't fit the template.

Adapter authoring is well-shaped today: `agent_from_send` /
`agent_from_stream` / `agent_from_object` cover the cases.
`agent_from_send` is annotated as the canonical reference;
`docs/writing-an-adapter.md` is the tutorial. No `BasicProxy`.

## 5. Policy capability matrix

Library authors choosing a bundled policy read this table.

| Policy | Anchor / default-responder respected | DM routing | Floor narrowing | Round-robin / turn order | Vocative + @-mention | `prior_speaker` aware | Stateless (no journal hooks needed) |
|---|---|---|---|---|---|---|---|
| **`OpenChatPolicy`** | No (broadcasts to all active+capable) | DM events bypass policy classification (kernel-handled) | No | No | No (every active+capable participant gets a `must`) | No | Yes |
| **`SingleResponderPolicy(responder_id)`** | No (configured responder always wins) | Bypassed | No | No | No (configured `responder_id` is the only target) | No | Yes |
| **`RoundRobinPolicy(order)`** | No (rotation is configured order) | Bypassed | No | Yes (sets `turn_taking_mode="round_robin"` + `turn_order` on first user post) | No | No | Yes (rotation pointer stored in `RoomControlState`, journaled) |
| **`DefaultPolicy`** (bundled v0.0 floor-aware) | Yes — anchor + default-responder receive `ANCHOR_SYNTHESIS_INSTRUCTIONS` via `role_prompt` | Bypassed | Yes (Case 4 narrows to `floor_owner ∩ active+capable`) | Yes (Case 5 game-start phrase auto-arms; Path A handles rotation) | Yes (Cases 1, 3 handle `@id` and natural-language vocatives) | Accepted but unused (signature stability) | Yes (mode flips travel via `UserTurnPlan` to coordinator) |

**Bypassed** in the DM column means the kernel's `/dm` handler builds
a `plan_for_default(target, ...)` directly without calling
`policy.plan_user_turn`. DM routing is a kernel-mechanism concern,
not a policy concern.

**Stateless** means the policy holds no journal-relevant state of its
own. `RoundRobinPolicy.order` and `SingleResponderPolicy.responder_id`
are configuration, not session state — restarting the process
re-instantiates the policy with the same configuration.

If you need a behavior the matrix doesn't cover (e.g., debate-phase
state machine, classroom turn-taking by raised hand), subclass
`BasicPolicy` for the simple shape or `ConversationPolicy` for the
full surface. See `docs/writing-a-policy.md` for the tutorial.

## 6. Cross-references

- `docs/writing-a-policy.md` — tutorial for policy authors.
- `docs/writing-an-adapter.md` — tutorial for adapter authors.
- `docs/security-model.md` — security audit + posture.
- `docs/perf-baseline.md` — performance baseline.
- `loom/__init__.py` — public surface index.
- `loom/contracts.py` — `Agent` Protocol + `ConversationPolicy` ABC.
- `loom/policy/single_responder.py` — canonical reference policy
  (smallest non-trivial).
- `loom/adapters.py` — `agent_from_send` (canonical reference adapter).
- `tests/property/test_ux_contracts.py` — CI guards on the
  conventions in this document. `make ux-check` runs them.

## 7. UX measurement

UX is mostly subjective, but a meaningful subset is measurable. These
metrics are wired under `make ux-check`.

| Metric | Captures | Target |
|---|---|---|
| Lines-to-hello-world | Quickstart simplicity | ≤ 5 |
| Public symbols in `loom.__all__` (primary tier) | Cognitive load | ≤ 12 |
| Docstring coverage of public methods on `LoomRoom` | Discoverability | 100% |
| `actor_id` outside `loom/kernel/actor.py` | Naming drift | 0 |
| `time.time()` in duration-math sites | Time-handling drift | 0 (Event.ts excepted) |
| `\| None` mixed with `Optional[X]` | Type-style drift | 0 (preserve current `Optional[X]` consistency) |
| Examples / docs importing `loom.kernel.*` | Boundary discipline | 0 |
| `runtime_checkable isinstance(a, Agent)` for every bundled adapter | Adapter fidelity | 100% |

What measurement does NOT capture: whether `set_topic` is the right
name, whether the topic/active_goal merge feels natural, whether the
`TurnResult` projection reads well. Those answer themselves once a
real author tries to build something.

## 8. Audit anchor

All `file_path:line_number` references in this document target the
kernel state at 2026-05-08, post-security-pass. Re-anchor before any
remediation PR ships.
