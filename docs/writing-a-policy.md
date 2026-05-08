# Writing a custom Loom policy

A `ConversationPolicy` decides who may speak in a room when a user
posts a message. The kernel handles every other mechanism — events,
bus, journal, leases, throttling — so a policy is small and pure: a
function from `(user_event, state) → UserTurnPlan`.

This tutorial walks through `loom/policy/single_responder.py`, the
smallest non-trivial policy in-tree, and then shows how to extend it.

## 1. The shape

```python
from loom.contracts import ConversationPolicy
from loom.kernel.events import Event
from loom.kernel.room import RoomStateView
from loom.policy import plan_for_acknowledgement, plan_with_required


class MyPolicy(ConversationPolicy):
    name = "my_policy"

    def plan_user_turn(
        self,
        user_event: Event,
        state: RoomStateView,
    ) -> "UserTurnPlan":
        # ... return a plan ...
```

`name` is a short identifier used in event rationales (and for
debug). `plan_user_turn` is the only required method.

## 2. The contract

A policy is constrained by five rules. The kernel relies on each.

1. **Synchronous + non-blocking.** Return in <10 ms typical. The
   coordinator holds its lock across this call to prevent the
   actor-cursor race; a slow policy blocks every actor thread for
   the duration. The coordinator emits a `policy_slow` control event
   when the call exceeds ~100 ms.

2. **Pure.** Read from `state` (a `RoomStateView` — read-only).
   Return a `UserTurnPlan`. Do not post to the bus, do not mutate
   `state`, do not call out to LLMs or the network. State changes
   are expressed declaratively on the returned plan
   (`set_turn_taking_mode`, `set_turn_order`, `advance_turn_pointer`).

3. **Stateless across restarts (v0).** Policy instances are not
   journaled. `__init__` runs again on every fresh process; any
   in-process state (e.g., a debate-phase counter) resets across
   restart. v0.1 will add `snapshot()/restore()` hooks.

4. **Errors mean the policy didn't decide.** If `plan_user_turn`
   raises, the coordinator emits a `policy_error` control event and
   dispatches on its `policy_error_mode`:
   - `"close_turn"` (library default; fail-closed): the turn closes
     with no response.
   - `"default_responder"`: fall back to
     `plan_for_default(default_responder_id, ...)`. Loom uses this
     for v0.0 behavior compat.
   - `"raise"`: re-raise (dev mode).

5. **Charter is owned by the kernel.** The kernel charter
   (visibility rules, PASS protocol, "do not impersonate
   kernel/system", stream/final separation) is rendered before
   `system_prompt` and `role_prompt` and CANNOT be overridden by a
   policy. You may *append* via `system_prompt` and `role_prompt`.

## 3. Walkthrough — `SingleResponderPolicy`

The full source is `loom/policy/single_responder.py`.

### 3.1 Constructor

```python
class SingleResponderPolicy(ConversationPolicy):
    name = "single_responder"

    def __init__(self, responder_id: str) -> None:
        if not responder_id or not isinstance(responder_id, str):
            raise ValueError(
                "SingleResponderPolicy requires a non-empty responder_id")
        self.responder_id = responder_id
```

Configuration in `__init__`. Validation here means a misconfigured
policy fails at construction, not at the first user post — the user
sees the error immediately.

### 3.2 `plan_user_turn`

```python
    def plan_user_turn(self, user_event, state):
        target_event_ids = (
            [user_event.id] if user_event.id is not None else []
        )
        info = state.participants.get(self.responder_id)
        if info is None or not info.active or not info.capable:
            return plan_for_acknowledgement(
                target_event_ids=target_event_ids,
                rationale=(f"configured responder "
                           f"{self.responder_id!r} not active/capable"),
            )
        return plan_with_required(
            [self.responder_id],
            routing_case="direct_mention",
            target_event_ids=target_event_ids,
            reason="single_responder",
            rationale=f"single responder: {self.responder_id}",
            allowed_speakers={self.responder_id},
            max_responses=1,
            wait_for_user_after=True,
            instruction=(f"You ({self.responder_id}) are the configured "
                         "responder for this room."),
        )
```

Two cases. Either the configured responder is unavailable (returns
an acknowledgement — no turn opens, no error) or it is (returns a
plan with one `must` obligation).

`target_event_ids` is the user-event id (or empty) — the kernel uses
it to correlate replies to the original user post. Always include
`user_event.id` when it's not `None`.

`plan_with_required` is the canonical helper for the common
"required participant(s)" case. It builds the `UserTurnPlan` with
the right defaults; you supply the routing case, the rationale, the
floor-control fields, and the per-turn instruction.

`plan_for_acknowledgement` is the canonical helper for "no response
needed" (the user said "thanks" / responder unavailable / etc.). It
returns a plan with `requires_response=False`; the runtime skips
opening a turn entirely.

### 3.3 The optional methods

`system_prompt` and `role_prompt` are appended after the kernel
charter. Default both return `""`. Override `role_prompt` if your
policy distinguishes roles (anchor, debater, teacher) and wants
extra instructions sent only to those participants.

`SingleResponderPolicy` doesn't override either — its instruction
travels through the per-turn `UserTurnPlan.instruction` field, which
the kernel renders into the speaker's TURN CARD.

## 4. Two helpers you'll use a lot

Imported from `loom.policy`:

- `plan_for_acknowledgement(*, target_event_ids, rationale)` —
  no-response plan. The runtime skips `open_user_turn`.
- `plan_with_required(required, *, routing_case, target_event_ids,
  reason, rationale, allowed_speakers, max_responses,
  wait_for_user_after, instruction, ...)` — the most common case.
- `plan_for_default(default_responder_id, *, reason, ...)` — fall back
  to a single configured responder. Used as the
  `policy_error_mode="default_responder"` fallback.

Read the source — `loom/kernel/obligations.py` — for the full field
list on `UserTurnPlan` and the optional knobs on each helper.

## 5. Reading `RoomStateView`

`state` is a frozen, read-only view onto `RoomState`. The fields
your policy will most often read:

```python
state.participants              # MappingProxy[str, ParticipantInfoView]
state.participants[pid].active  # bool
state.participants[pid].capable # bool
state.participants[pid].cost_tier  # int
state.anchor_id                 # Optional[str]
state.default_responder_id      # Optional[str]
state.topic                     # Optional[str]
state.control.floor_owner       # tuple[str, ...]  (empty = open floor)
state.control.turn_taking_mode  # "broadcast" | "round_robin" | ...
state.control.turn_order        # tuple[str, ...]
state.control.next_speaker_idx  # int
state.control.roles             # MappingProxy[str, str]
state.control.style             # "brief" | "normal" | "detailed"
state.control.active_goal       # Optional[str]  (merging with `topic` in v0.1)
state.control.wait_for_user     # bool
```

Mutating `state.participants[pid]` raises `TypeError`; you can't
even attempt it. The view is your guarantee that the policy is
pure.

## 6. Mutating control state declaratively

If your policy wants to flip `turn_taking_mode`, set `turn_order`,
or advance the rotation pointer, do it via `UserTurnPlan` fields:

```python
return plan_with_required(
    [first_speaker],
    routing_case="direct_mention",
    target_event_ids=[user_event.id],
    reason="round_robin_start",
    rationale="rotation begins",
    allowed_speakers={first_speaker},
    max_responses=1,
    wait_for_user_after=True,
    instruction="You are up first.",
    set_turn_taking_mode="round_robin",   # <-- declarative
    set_turn_order=["alice", "bob", "carol"],
    advance_turn_pointer=True,
)
```

The coordinator applies the requested transitions when opening /
closing the turn. Your policy never touches the bus or the state
directly. `RoundRobinPolicy` is the in-tree reference for this
pattern.

## 7. Testing

Drop a `tests/test_kernel_<name>_policy.py` file alongside the existing
ones. The pattern is:

```python
import unittest
from loom.kernel.events import chat
from loom.policy.my_policy import MyPolicy
from loom.testing import make_test_state  # v0.1


class MyPolicyTests(unittest.TestCase):
    def test_routes_to_responder(self):
        state = make_test_state(("alice", True), ("bob", True))
        policy = MyPolicy(...)
        plan = policy.plan_user_turn(
            chat(sender="user", body="hi"), state)
        self.assertIn("alice", plan.required_participants)
```

Until `loom.testing` ships in v0.1, copy the inline `_state(...)`
helper from `tests/test_kernel_open_chat_policy.py` (or any of the
sibling policy test files).

## 8. When to subclass `ConversationPolicy` vs `BasicPolicy`

`BasicPolicy` (v0.1) is a template-method base class for the common
shape: choose responders, return one plan or an acknowledgement.
Override `_choose_responders` and you're done.

Use the full `ConversationPolicy` ABC when you need:

- More than one plan shape across user turns (e.g., game-phase
  branches that issue different `set_turn_taking_mode` values).
- Floor narrowing or `wait_for_user_after` logic that depends on
  state cases the template can't express simply.
- Custom `system_prompt` / `role_prompt` rendering.

`OpenChatPolicy` and `SingleResponderPolicy` rebase onto
`BasicPolicy` in v0.1; `RoundRobinPolicy` and `DefaultPolicy` keep
the full ABC.

## 9. Cross-references

- `loom/policy/single_responder.py` — canonical reference (this
  tutorial).
- `loom/policy/open_chat.py` — broadcast every turn.
- `loom/policy/round_robin.py` — declarative state mutation reference.
- `loom/policy/default.py` — multi-case classifier (vocative, floor,
  game-start).
- `loom/contracts.py` — `ConversationPolicy` ABC.
- `loom/kernel/obligations.py` — `UserTurnPlan`, `plan_*` helpers.
- `loom/kernel/room.py` — `RoomState`, `RoomStateView`.
- `docs/loom-ux-spec.md` — the kernel-wide UX contract.
