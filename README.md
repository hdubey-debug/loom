<div align="center">

# 🪡 Loom

**A small, opinionated kernel for multi-agent LLM chatrooms.**
_Race-free turn taking. Pluggable policies. Bring your own LLM SDK._

[![CI](https://github.com/hdubey-debug/loom/actions/workflows/ci.yml/badge.svg)](https://github.com/hdubey-debug/loom/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000)](https://github.com/astral-sh/ruff)
[![Tests](https://img.shields.io/badge/tests-1500%2B-success)](#)
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/hdubey-debug/loom/blob/main/examples/colab_demo.ipynb)

</div>

<!-- DEMO GIF: docs/demo.gif (placeholder — record after a public release) -->

Loom is a small library for building multi-agent chatrooms. You provide
a list of agents and (optionally) a routing policy; Loom runs the room.
Race conditions, cursor drift, obligation tracking, mention routing,
prompt sandboxing, and streaming consolidation are the kernel's job —
not yours.

It's the substrate behind [Weave](https://github.com/hdubey-debug/weave),
a terminal multi-agent console. Loom is intended to be reusable for
any LLM-driven chatroom: debate, classroom, 20-questions, a synthesis
council over a corpus, a peer review pair, or whatever else.

## Why Loom?

Most multi-agent code dies in three places: the cursor race when two
agents wake at once, mention parsing that quietly drops valid
addresses, and policy logic tangled with bus posting until nothing is
safe to change. Loom makes those three things the kernel's job and
gives policies one tiny pure callback (`plan_user_turn`). The result:
~5k LOC, 1500+ tests, and a public surface you can read in an hour.

| | Loom | LangGraph | AutoGen | crewAI |
|---|---|---|---|---|
| **Scope** | Kernel only | Framework | Framework | Framework |
| **LLM coupling** | Bring your own SDK | Provider-agnostic | Provider-agnostic | OpenAI-first |
| **Race-safe turn taking** | ✅ obligation + lease | DAG-level | n/a | n/a |
| **Policy as pure callback** | ✅ enforced via boundary test | partial | n/a | n/a |
| **Streaming** | ✅ delta-merged | ✅ | partial | ✅ |
| **Built-in journal** | ✅ append-only `events.jsonl` | partial | ❌ | ❌ |

_Numbers are approximate; corrections welcome via issue or PR._

## 30-second start

```python
from loom import LoomRoom, agent_from_send, OpenChatPolicy

# Each agent only needs an id + a callable that takes a prompt and
# returns text. Provider SDK calls fit cleanly here.
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

    # Membership is dynamic.
    room.add_agent(agent_from_send("gemma", gemma_send))
    room.remove_agent("gpt")
```

`run_console()` works too — interactive REPL with stdlib defaults:

```python
with room:
    room.run_console()   # blocks; type messages, /quit to exit
```

## Mental model

```mermaid
flowchart TD
    User[User / prompt_fn] -->|post| Room[LoomRoom facade]
    Room --> Session[LoomSession]
    Session --> Kernel[Kernel<br/>bus · coordinator · state]
    Session --> Policy[Policy<br/>plan_user_turn]
    Session --> Agents[Agents<br/>your LLM clients]
    Kernel <-->|state read| Policy
    Kernel <-->|stream| Agents
```

<details>
<summary>ASCII fallback</summary>

```
+---------------------------------------------------------------+
| LoomRoom (facade)                                              |
|   .post / .post_and_wait / .add_agent / .remove_agent / ...  |
+----------------------------+----------------------------------+
                             |
                             v
+----------------------------+----------------------------------+
| LoomSession (build_loom_session) — actor pool + journal        |
+----------------------------+----------------------------------+
            |                 |                  |
            v                 v                  v
       +---------+      +-----------+      +-----------+
       | Kernel  |<---> |  Policy   |      |  Agents   |
       | (bus,   |      | (routing) |      | (proxies) |
       |  coord, |      +-----------+      +-----------+
       |  state) |
       +---------+
```

</details>

- **Agents** wrap your LLM calls. Use the bundled adapters
  (`agent_from_send` / `agent_from_stream` / `agent_from_object`) or
  pass any object that satisfies the `Agent` protocol — `id` plus
  `stream(prompt) -> Iterator[str]`.
- **The kernel** owns the mutable state (`RoomState`), the event bus,
  and the coordinator (leases, obligations, slot resolution, watchdog).
  It's the only mutator. Policies and rooms read from it but never
  write.
- **The policy** decides who speaks. It's a single `plan_user_turn`
  callback that returns a `UserTurnPlan` (a small dataclass naming the
  required participants, the rotation, the floor, etc.). Policies must
  be pure: no I/O, no mutation, no bus posting. The kernel watchdog
  flags anything taking >100ms.
- **`LoomRoom`** is the public-facing facade. It glues the above
  together and exposes a small, opinionated surface.

## Bundled policies

| Policy | Behavior |
|---|---|
| `DefaultPolicy` | Loom's classifier: direct mention, vocative addressing, acknowledgement, broadcast. Production-grade. |
| `OpenChatPolicy` | Broadcast every user post to every active capable participant. |
| `SingleResponderPolicy(id)` | Always route to one configured participant. |
| `RoundRobinPolicy(order)` | Rotate through a fixed order, one speaker per user post. |

Pick one and pass it via `policy=...`. To write your own, see
"Writing a policy" below.

## Writing an agent adapter

The `Agent` protocol is structural — anything with `id: str` and
`stream(prompt) -> Iterator[str]` qualifies. The three helpers cover
common shapes:

```python
from loom import agent_from_send, agent_from_stream, agent_from_object

# Most LLM SDKs are non-streaming send/recv:
gpt = agent_from_send("gpt", lambda prompt: openai_chat(prompt))

# If you already have a streaming generator:
def claude_stream(prompt):
    for delta in anthropic_messages_stream(prompt):
        yield delta.text

claude = agent_from_stream("claude", claude_stream)

# Or wrap an existing client object that has .stream / .send:
my_client = MyChatClient(api_key=...)
gemma = agent_from_object("gemma", my_client,
                          persona="local 27B model",
                          cost_tier=0)
```

You can also write a class directly:

```python
class MyAgent:
    id = "mine"
    persona = "research assistant"
    capability_block = "tool use, web fetch"
    cost_tier = 2

    def stream(self, prompt):
        for chunk in my_provider.stream(prompt):
            yield chunk

room = LoomRoom(agents=[MyAgent()])
```

Optional metadata attributes (`persona`, `capability_block`,
`cost_tier`, `capable`) are read by the room when wiring; absent
attributes fall back to the documented defaults (empty strings, tier 1,
capable=True).

## Writing a policy

A policy is a subclass of `ConversationPolicy` with one required
method:

```python
from loom import ConversationPolicy
from loom.kernel.obligations import (
    plan_for_acknowledgement,
    plan_for_default,
    plan_with_required,
)

class MyPolicy(ConversationPolicy):
    name = "mine"

    def plan_user_turn(self, user_event, state, *, prior_speaker=None):
        # Inspect user_event.body and state.participants. Return one of:
        # - plan_for_acknowledgement(...)  — no turn opens.
        # - plan_for_default(pid, ...)     — single responder.
        # - plan_with_required([...], ...) — broadcast or floor-narrowed.
        active = sorted(p for p, info in state.participants.items()
                        if info.active and info.capable)
        return plan_with_required(active, routing_case="multi_opinion",
                                  reason="custom", target_event_ids=[user_event.id])
```

Look at `loom/policy/round_robin.py` for the canonical reference of
declarative state mutation (`set_turn_order`, `advance_turn_pointer`
on the returned plan — round-robin mode is signalled by a non-empty
`turn_order`). Look at `loom/policy/default.py` for a richer
classifier with addressing detection.

**v0.2 added several optional hooks** any policy can override:

- `charter_text(state)` — extra system-preamble text rendered after
  the kernel charter, before persona/topic.
- `prompt_sections(state, participant_id, trigger_event)` — named
  sections injected late in the system preamble, each with a
  `<<<NAME>>>` header for diff attribution.
- `dead_letter_target(state, removed_participant)` — pick the reroute
  target when an `@`-mentioned agent is removed mid-turn.
- `should_post_response(body, state, participant_id)` — veto a draft
  after the kernel's idle / IoU / empty filters have passed.

All four have safe defaults; buggy hook implementations are caught
at the boundary so a bad policy can't crash the room.

The policy contract is intentionally narrow:

- `plan_user_turn` must be **synchronous, deterministic, fast**
  (<10ms typical). The kernel holds its lock across this call to keep
  the actor-cursor race shut. Slow policies trip a `policy_slow`
  control event at ~100ms.
- A raised exception emits `policy_error` and dispatches on
  `policy_error_mode`: `"close_turn"` (default; fail-closed),
  `"default_responder"` (fall back), or `"raise"` (dev mode).
- **Policies must not mutate `RoomState` and must not post to the
  bus** — both are the kernel's responsibility. Declarative requests
  go on the returned `UserTurnPlan` (`set_turn_order`,
  `advance_turn_pointer`, `set_allowed_speakers`, `set_max_responses`,
  `wait_for_user_after`, etc.). A boundary test enforces this with a
  static grep.

`system_prompt(actor_id, state)` and `role_prompt(actor_id, state)`
are optional overrides. They contribute extra prompt sections after
the kernel charter. The charter is rendered immediately after the
`<<<SYSTEM PREAMBLE>>>` header — before persona, participant id, and
topic — and cannot be removed by a policy.

## Threading model

- Each agent runs on its own daemon thread. They wake on bus events,
  evaluate priority triggers, acquire a lease from the coordinator,
  and stream into the bus.
- `prompt_fn` runs on the main thread (typically the caller of
  `room.run_console`).
- `notify` is called from agent threads concurrently. The default
  `notify` wraps `print` in a module-level lock so streamed output
  doesn't shear; if you supply your own (e.g. a rich console renderer),
  make sure it's thread-safe.
- `room.start` / `room.stop` are idempotent and safe.
  `room.add_agent` / `room.remove_agent` serialize through a session
  lock so concurrent membership changes don't race.

## Persistence

Pass `journal_dir=...` to record an audit trail:

```python
room = LoomRoom(agents=[...], journal_dir="/tmp/my-room")
```

The journal writes:

- `events.jsonl` — append-only ledger of every bus event. Authoritative.
- `room_state.json` — fast-resume snapshot. Advisory; rebuilt from
  `events.jsonl` on a missing or corrupt snapshot.

Both files are written for audit + tooling-grade replay
(`Journal.replay_into(coord)` and `Journal.restore_state(...)` work
in tests). **Automatic restart-recovery wiring is still pending** —
`build_loom_session` constructs a fresh `RoomState` and does not call
the recovery helpers; the journal is purely an audit log at runtime.
Auto-restore wiring is on the v0.4 list.

**Snapshot schema is at v7** as of v0.3.x (envelope wraps room +
capabilities + budget + actors + context slots). Older v3–v6 snapshots
load cleanly through registered migrators; retired fields like
`floor_owner` and `turn_taking_mode` are silently discarded.

**Policy state is not journaled in v0** — restart instantiates a
fresh policy. Stateful policies (debate phase, 20Q question count)
work in-process but reset across restart. Lifecycle hooks for policy
snapshot/restore are on the v0.2 list.

## Public surface

```python
from loom import (
    # Primary surface.
    LoomRoom, Agent,
    agent_from_send, agent_from_stream, agent_from_object,
    ConversationPolicy, DefaultPolicy,
    OpenChatPolicy, SingleResponderPolicy, RoundRobinPolicy,
    RoomConfig,

    # Advanced surface (kept for power users).
    ParticipantWiring, SendProxyAdapter,
    build_loom_session, run_loom_console,
)
```

**Extension types** (import from `loom.contracts` when writing a
custom policy that uses v0.2 hooks):

```python
from loom.contracts import (
    PromptSection,        # named system-preamble section
    LeaseCheck,           # gate in the lease-grant chain
    LeaseCheckResult,     # (passed: bool, deny_reason: Optional[str])
    PASSED,               # sentinel for the common path
    TriggerPriorityFn,    # actor's trigger classification hook
)
from loom.kernel.events import register_secret_shape, SecretShape
```

Everything else (the kernel internals under `loom.kernel.*`) is
implementation detail and may shift between minor versions.

## v0 limitations

- No async / off-lock policies. `plan_user_turn` runs under the
  coordinator's lock with a <10ms contract; an LLM-backed policy
  would freeze every actor thread. On the v0.4 list.
- No policy state persistence across restart. Stateful policies
  (debate phase, 20-questions count) work in-process but reset on
  restart.
- No automatic restart-recovery from the journal. `events.jsonl`
  and `room_state.json` are written but `build_loom_session` does
  not currently call `replay_into` / `restore_state` on startup.
  On the v0.4 list.
- Stream deltas are flushed during the streaming loop, before the
  post-stream chair-speak / idle-dup filters and the
  `should_post_response` policy veto run. UI renderers should clear
  pending text on `stream_end(status in {"suppressed", "filtered"})`
  rather than treating already-rendered deltas as the final reply.
- No per-message rate limiting in the public API surface. The
  kernel has `ThrottleConfig` and `BudgetConfig` hooks; the room
  facade doesn't expose them yet.
- No structured `tool_call` / `tool_result` event kinds. Agents that
  use tools today do so inside their own adapter; Loom sees only the
  final text. Tool-event support is on the v0.4 list
  (`KernelState.tools` slot already reserved).
- No standalone PyPI package. Install in-place from source.

**Resolved in v0.2** (from the prior limits list): `RoomStateView` is
now deep-frozen via `ParticipantInfoView`; `RoomState.set_floor_owner`
and `set_turn_taking_mode` were removed (round-robin is now signalled
by a non-empty `turn_order`); subscriber fan-out runs outside the bus
lock; the lease-rejection chain became a pluggable
`RoomConfig.lease_checks` with structured `lease_denied` events.

## Examples

The [`examples/`](examples/) directory has runnable scripts:

| File | What it shows |
|---|---|
| [`colab_demo.ipynb`](examples/colab_demo.ipynb) | **One-click Colab tutorial.** Mock-agent walkthrough of all four policies plus a v0.2 hook, then a real-LLM live chat with OpenAI + Gemini. |
| [`two_agents.py`](examples/two_agents.py) | Two scripted agents on `OpenChatPolicy`. No API key needed. |
| [`openai_two_agents.py`](examples/openai_two_agents.py) | Two real OpenAI-backed agents on `OpenChatPolicy`. Requires `OPENAI_API_KEY`. |
| [`round_robin_classroom.py`](examples/round_robin_classroom.py) | Three scripted agents on `RoundRobinPolicy` with a teacher/student handoff. |
| [`single_responder_qa.py`](examples/single_responder_qa.py) | One agent on `SingleResponderPolicy`. The minimal Q&A skeleton. |

Run any of them straight from the repo root after `pip install -e .`:

```bash
python examples/two_agents.py
```

## Roadmap

**v0.2 — landed** (12-PR refactor program; see `CHANGELOG.md`):

- Deep-frozen `ParticipantInfoView` (closes the soft-leak in
  `RoomStateView.participants`).
- `_KernelAuth` token gates `MessageBus.post_internal`.
- Removed `turn_taking_mode` and `floor_owner` from kernel state
  (along with `/floor /release /quiet` console UX). Round-robin is
  now signalled by a non-empty `turn_order`.
- New optional policy hooks: `charter_text`, `dead_letter_target`,
  `should_post_response`, `prompt_sections`.
- Pluggable `RoomConfig.lease_checks` with structured `lease_denied`
  events. Pluggable `RoomConfig.trigger_priority`. Pluggable
  `RoomConfig.watchdog_interval_s` (dedicated coordinator watchdog
  thread). Shape-based secret scrubber framework
  (`register_secret_shape`).
- Bus subscriber fan-out runs outside the bus lock — slow
  subscribers no longer block readers/writers.

**v0.3 — landed** (kernel doctrine — 22 principles; see
`docs/internal/study/` for the full design dialogue and
`CHANGELOG.md` for the PR list):

- **One unified `Lease` abstraction** with `LeaseKind` discriminator
  (USER_TURN, REACTIVE, CONTROL_ACTION, TOOL_INVOCATION,
  WORKFLOW_STEP, SUMMARIZATION). Lease-check chain gates each kind
  via per-check `applies_to` filtering.
- **Capabilities** — a typed capability registry (`CapabilityName`
  enum with 33 verbs) with grant/revoke/expire effects. User-issued
  control actions bypass the agent capability gate per design.
- **Control actions** — every kernel state mutation goes through a
  registered `ControlAction` → versioned `ControlEffect` →
  registered reducer pipeline. Replay is deterministic.
- **Floor overrides** — `ADD`/`REPLACE`/`BLOCK` modes scoped to one
  lease, current turn, until-cleared, or persistent.
- **Context compaction** — view-layer rolling summarisation with a
  dedicated `SUMMARIZATION` lease, three typed events
  (`summary_proposed`/`committed`/`failed`), off-lock pre-validation
  + under-lock anchor commit, lineage edges, per-scope backoff, and
  policy-pressure (Path A) or `/summarize` (Path B) triggers.
- **Slash commands** — `/grant`, `/revoke`, `/topic`, `/anchor`,
  `/responder`, `/floor`, `/policy`, `/summarize` as the
  human-root-action surface (P15).

**v0.4 — next** (in planning):

- Controller mechanism: `RoomConfig.controller_ids` — chat events from
  privileged participants open chained user turns. The CEO /
  orchestrator pattern, structurally.
- Async / off-lock policies for slow planning logic (LLM-backed
  routing).
- Automatic restart-recovery wiring from the journal.
- Policy-state snapshot/restore lifecycle hooks.
- Structured `tool_call` / `tool_result` event kinds, tool channel
  visibility, multi-step streaming loop, tools-as-participants
  pattern. (`KernelState.tools` slot already reserved.)

**v0.5 — exploratory:** `ClaudeCodeAgent` adapter + worktree-isolation
convention for multi-Claude-Code orchestration under a CEO.

Plus standalone PyPI packaging at any version.

## License

[MIT](LICENSE) — © 2026 Harsh Dubey.

See [`CHANGELOG.md`](CHANGELOG.md) for release notes,
[`CONTRIBUTING.md`](CONTRIBUTING.md) for setup + the policy contract,
and [`docs/`](docs/) for tutorials.
