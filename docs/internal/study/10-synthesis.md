# 10 — End-to-end synthesis

This is **Session 10**, the final session of the Loom kernel
deep-study curriculum. Phase E: **prove the curriculum worked**. We
trace one canonical scenario (`examples/round_robin_classroom.py`)
end-to-end through every layer covered in Sessions 0–9, then
sketch a dry-run modification plan for one v0.2 roadmap item
(deep-frozen `RoomStateView`), and finally answer ad-hoc questions
without re-reading source — verifying that the prior 200+ invariants
are now load-bearing in our heads.

State as of Loom v0.1.2 (2026-05-08).

## Inputs

- All 10 prior artifacts under `docs/internal/study/00–09`.
- `examples/round_robin_classroom.py` (45 LOC) — the canonical trace
  target.

No new source code is read in this session — this synthesises what
we already know.

---

## End-to-end trace: `examples/round_robin_classroom.py`

### The scenario

```python
from loom import LoomRoom, RoundRobinPolicy, agent_from_send

def alice_send(prompt): return "Alice: My take — start from the assumptions ..."
def bob_send(prompt):   return "Bob: Counterpoint — the assumptions usually leak ..."
def carol_send(prompt): return "Carol: Synthesis — name the strongest version of each side ..."

agents = [
    agent_from_send("alice", alice_send, persona="optimist"),
    agent_from_send("bob",   bob_send,   persona="skeptic"),
    agent_from_send("carol", carol_send, persona="synthesiser"),
]
policy = RoundRobinPolicy(["alice", "bob", "carol"])

with LoomRoom(agents=agents, policy=policy, topic="design review") as room:
    room.run_console()
```

User types `"hello, what's first?"` at the prompt. We trace from
`with room:` to the first agent reply (alice).

### Phase 1: Construction (`agent_from_send` × 3, then `LoomRoom(...)`)

**`agent_from_send("alice", alice_send, persona="optimist")`** —
Session 6 (`loom/adapters.py`):
1. Validates `alice_send` is callable.
2. Builds inner `_stream(prompt)` closure that calls `alice_send(prompt)`,
   passes through `_extract_text` (returns the string as-is), yields
   it as a single chunk.
3. Wraps in `_FunctionAgent(agent_id="alice", _stream,
   persona="optimist", capability_block="", cost_tier=1, capable=True,
   cancel_callable=None)`.
4. `_FunctionAgent.__init__` validates `agent_id` is non-empty string,
   stores all fields in `__slots__`. The agent satisfies the `Agent`
   Protocol structurally (Session 6 invariant 103).

Same for `bob` and `carol`. We now have 3 `_FunctionAgent` instances
with personas `"optimist"`, `"skeptic"`, `"synthesiser"`.

**`RoundRobinPolicy(["alice", "bob", "carol"])`** — Session 6:
1. Validates list is non-empty.
2. Per-id check: each is non-empty `str`.
3. Defensive copy + dedupe-while-preserving-order →
   `self._order = ["alice", "bob", "carol"]`.

**`LoomRoom(agents=[alice, bob, carol], policy=RoundRobinPolicy([...]), topic="design review")`** — Session 7:
1. `agent_list = [alice, bob, carol]`, non-empty.
2. For each agent: `_agent_to_wiring(agent)`:
   - Validates `agent.id` is non-empty string ✓.
   - `agent.stream` is callable → use agent itself as proxy
     (Session 7 invariant 134).
   - `_warn_on_typoed_agent_attrs` — `_FunctionAgent` only has
     documented attrs; no warnings.
   - Returns `ParticipantWiring(id="alice", proxy=alice, persona="optimist",
     capability_block="", cost_tier=1, capable=True)`. Same for bob/carol.
3. No duplicate ids (set membership check).
4. `resolved_anchor = "alice"` (first agent).
5. Calls `build_loom_session(wirings, config=None,
   default_responder_id=None, anchor_id="alice", topic="design review",
   journal_dir=None, auto_start=False, policy=RoundRobinPolicy(...),
   policy_error_mode="close_turn")` — Session 7:
   - `cfg = RoomConfig()` (defaults: `compact_threshold=50,
     user_turn_idle_timeout_s=20, user_turn_debounce_ms=250,
     pass_buffer_chars=16, lease_ttl_s=60, max_drafts_per_participant=1`).
   - `bus = MessageBus()` (Session 2): `_log=[]`, `_subscribers=()`,
     `_thread_actors={}`, `_max_body_bytes=256KB`, etc.
   - `state = RoomState(config=cfg)` — Session 1: `room_epoch=0`,
     `topic=None`, `participants={}`, slot ids all None,
     `current_user_turn_id=None`, `last_compacted_event_id=-1`,
     fresh `RoomControlState(roles={}, floor_owner=None,
     wait_for_user=False, style="normal", turn_taking_mode="broadcast",
     turn_order=[], next_speaker_idx=0)`.
   - `coord = RoomCoordinator(bus, state, policy_error_mode="close_turn")`
     — Session 5: `_lock = RLock()`, `_leases={}`, `_user_turn=None`,
     `_loop_guard / _throttle / _budget` instances.
   - `policy = RoundRobinPolicy([...])` (provided).
   - `journal = None` (no `journal_dir`).
   - For each wiring, `coord.register_participant(ParticipantInfo(id,
     capable=True, cost_tier=1, active=True))`:
     - Session 5 — under `_lock`: `state.add_participant(info)` →
       `state.participants["alice"] = info`, `room_epoch += 1` (now 1).
     - Emits `participant_added(id="alice", role_hints={})` via
       `bus.post_internal` — bus assigns `id=0`, `ts=time.time()`.
     - Same for bob (epoch=2, bus id=1), carol (epoch=3, bus id=2).
   - **Validate `anchor_id="alice"` exists in `by_id`** ✓ (Phase 0
     audit fix; Session 7 invariant 146).
     - `coord.set_anchor("alice")`: `state.set_anchor("alice")` (epoch
       += 1 → 4), emit `_control("anchor_changed", old_id=None,
       new_id="alice")` — bus id=3.
   - `default_responder_id` is None — skip.
   - `coord.set_topic("design review")`: under lock, no open turn so
     no close-first; `state.set_topic("design review")` (no epoch
     bump), emit `topic_changed(old=None, new="design review")` — bus
     id=4.
   - `handler = _make_draft_handler(by_id, RoundRobinPolicy(...))`
     — closure that captures `by_id` by reference and `policy` by
     value.
   - `actors = [ParticipantActor("alice", bus, coord, handler),
     ParticipantActor("bob", ...), ParticipantActor("carol", ...)]`.
     Each `__init__` (Session 4): `_cursor=-1`, `_stopped=Event()`,
     `_thread=None`, `_pending_direct_mentions=deque(maxlen=100)`,
     `wakeup_timeout_s = min(20, 60) = 20`.
   - `auto_start=False` → actors NOT started yet (the `LoomRoom`
     facade owns lifecycle).
   - Returns `LoomSession(bus, state, coord, journal=None, actors,
     wirings=by_id, policy=RoundRobinPolicy(...), _draft_handler=handler,
     _started=False, _stop_event=Event(),
     _membership_lock=Lock())`.
6. `LoomRoom.__init__` returns. The bus log is now 5 events long
   (3 participant_added + 1 anchor_changed + 1 topic_changed).
   Nothing is running.

### Phase 2: `with room:` → `__enter__` → `start`

`LoomRoom.__enter__` calls `self.start()` →
`self._session.start()` — Session 7:
1. `RuntimeError` check: `_stop_event` not set ✓.
2. Under `_membership_lock`: for each actor, `a.start()`:
   - `ParticipantActor.start` (Session 4): not None check; spawns
     `threading.Thread(target=self._loop, daemon=True,
     name=f"loom-actor-{self.id}")`; starts.
   - `_loop` begins: `unbind = self.bus.bind_actor(self.id)` (Session
     2 — registers the actor's thread id in `_thread_actors`).
   - Then enters `while not self._stopped.is_set():` loop; calls
     `bus.wait_after(self._cursor=-1, timeout=20)`. **Returns
     immediately** because `len(_log)=5 > -1+1=0`.
   - `new_len=5 > self._cursor + 1=0` → `_step_with_error_handling`:
     - `step()` → `_decide_once()` → `_dispatch_decision(decision)`.
3. Sets `_started = True`.

In **parallel** (3 daemon threads now running), each actor enters
its first iteration:

#### Each actor's first `_decide_once`

For e.g. `alice`:
- `bus.snapshot(audience="alice", since=-1)` returns events 0-4 (all
  visible to alice on main; `visible_to` filter: main = visible to
  everyone).
- Filter `e.sender != "alice"` — events 0-4 are all
  `sender="system"`; pass through.
- `_pending_direct_mentions` empty, no replays.
- `decide(snap, my_id="alice", coord.user_turn=None)`:
  - `events` non-empty, but `user_turn is None` → return
    `AgentDecision(action="SKIP", trigger_event_id=None,
    considered_event_ids=[0,1,2,3,4], reason="no open user_turn")`
    (Session 4 trigger priority).
- `_cursor = max(0,1,2,3,4) = 4`.
- `_update_pending_mentions`: no user mentions in batch → noop.
- Return decision.

Then `_dispatch_decision`: `action == "SKIP"` →
`coord.handle_skip("alice", trigger=None)` — Session 5: under lock,
no open turn → return immediately (no last_activity_at to bump).

Each of the 3 actors does the same thing concurrently. Loop
iteration ends; back to `wait_after(cursor=4, timeout=20)`. **All
three block** waiting for new events.

### Phase 3: `room.run_console()`

`LoomRoom.run_console(prompt_fn=None, notify=None)`:
1. Defaults: `prompt_fn = _default_prompt`, `notify =
   _thread_safe_print`.
2. `self.start()` — already started, idempotent.
3. `unsubscribe = self._session.bus.subscribe(_make_console_subscriber(notify))`
   — Session 2: appends to `_subscribers` tuple under lock.
4. Enters REPL loop:
   ```python
   while True:
       try: text = prompt_fn()                       # blocks on input()
       except (EOFError, KeyboardInterrupt): break
       text = text.strip()
       if not text: continue
       if text.startswith("/"): handle_slash...     # not in this trace
       self.post(text)
   ```
5. **Main thread blocks on `input("you ▸ ")`**.

User types `"hello, what's first?"` and hits Enter.

### Phase 4: User post → `room.post → post_user_text`

`text = "hello, what's first?"`; not a slash command.

`self.post(text)` — Session 7:
1. Validates non-empty.
2. `event = post_user_text(self._session, "hello, what's first?", channel="main")` — Session 7:
   - `_VALID_CHANNEL_RE.match("main")` ✓.
   - `addressable = ["alice", "bob", "carol"]`.
   - `parse_addressees("hello, what's first?", addressable, exclude="user")`
     — Session 2: `_MENTION_RE.findall(text)` returns `[]` (no @-mentions).
     Returns `[]`.
   - `e = chat(sender="user", body="hello, what's first?",
     addressees=[], channel="main", room_epoch=4, user_turn_id=None,
     meta={})` — Session 1 factory; no validation here, just construct.
   - Define `_classify_after_post(posted_event):
     return RoundRobinPolicy([...]).plan_user_turn(posted_event,
     state.view())`.
   - Calls `coordinator.post_user_event_and_open_turn(e,
     _classify_after_post)` — Session 5:
     **Atomic under coord lock**:
     1. `bus.post_internal(e)` — assigns `e.id=5`, `e.ts=time.time()`;
        appends to `_log` (now length 6); `notify_all()` wakes all 3
        actor threads; subscribers (the console subscriber) run
        inline:
        - `_make_console_subscriber._on_event(e)`: `e.kind=="chat",
          e.sender=="user", e.channel=="main"` (NOT DM) → return
          (echo only DM user posts).
     2. **Actor threads wake but block on `coordinator.user_turn`**
        (which acquires this same lock) — race-free per Session 5
        invariant 75.
     3. `_run_policy_under_lock(_classify_after_post, e)`:
        - `t0 = time.monotonic()`.
        - `_classify_after_post(e)` → `RoundRobinPolicy.plan_user_turn(e,
          state.view())`:
          - `state.view()` returns `RoomStateView` (Session 1):
            `participants` is `MappingProxyType({"alice": info,
            "bob": info, "carol": info})`, control fields tupled.
          - `active_capable = {"alice", "bob", "carol"}` (all active+capable).
          - `control.turn_taking_mode == "broadcast"` (initial) →
            **First post — arm round-robin**:
            - `speaker = _first_live(["alice","bob","carol"], {...})`
              = `"alice"` (first in order, in active_capable).
            - Returns `plan_with_required(["alice"],
              routing_case="direct_mention", target_event_ids=[5],
              reason="round_robin_start", rationale="round-robin start: alice",
              allowed_speakers={"alice"}, max_responses=1,
              wait_for_user_after=True,
              instruction="Round-robin mode: you (alice) are up this turn. ...",
              set_turn_taking_mode="round_robin",
              set_turn_order=["alice","bob","carol"],
              advance_turn_pointer=True)`.
          - `__post_init__` validates: `routing_case="direct_mention"` in
            valid set ✓; `requires_response=True` with
            `required_participants={"alice"}` non-empty ✓;
            `allowed_speakers={"alice"}` (already set, not defaulted);
            `max_responses=1` (already set).
        - `elapsed_ms` ~0.3ms; under 100ms threshold; no `policy_slow`.
     4. `_apply_plan_state_changes_locked(plan)`:
        - `set_turn_taking_mode="round_robin"` →
          `state.set_turn_taking_mode("round_robin")` (Session 1).
        - `set_turn_order=["alice","bob","carol"]` →
          `state.set_turn_order([...])` (filters unknown ids; resets pointer to 0).
     5. `plan.routing_case == "direct_mention" != "acknowledgement"` →
        `open_user_turn(e, plan)`:
        - **Debounce**: `_last_user_post_ts is None` →
          `should_open_new_user_turn` returns True.
        - No prior open turn — skip close.
        - `wait_for_user` is False — skip clear.
        - `make_user_turn(turn_id=0, user_event_id=5, plan,
          started_at=time.monotonic(), next_obligation_id=1)` — Session
          1:
          - Allocates obligation id=1 to alice's `must`. Plan's
            obligation list (the one in plan.obligations) gets
            `obligations[0].id=1` (mutates in place).
          - Returns `(turn, next_oid=2)`. `_next_obligation_id=2`.
          - `turn = UserTurn(id=0, user_event_id=5,
            started_at=<monotonic>, frozen_plan=plan,
            obligations={1: ResponseObligation(id=1,
            participant_id="alice", level="must",
            target_event_ids=[5], reason="round_robin_start")},
            speaker_counts={}, drafted=set(), state="open",
            closure_reason=None, last_activity_at=started_at,
            debounced_event_ids=set())`.
        - `state.current_user_turn_id = 0`.
        - `_last_user_post_ts = now`.
        - **Emit `user_turn_opened(user_turn_id=0,
          routing_case="direct_mention",
          required_participants=["alice"], optional_participants=[],
          rationale="round-robin start: alice")`** — bus id=6.
        - **Emit `obligation_recorded(obligation_id=1,
          participant_id="alice", level="must",
          target_event_ids=[5], reason="round_robin_start")`** —
          bus id=7.
        - Plan has required participants → no auto-close.
   - Returns `e` (with `id=5`).
3. `room.post` returns `e.id = 5`. Main thread loops back to
   `prompt_fn()` and blocks on `input()` again.

### Phase 5: Actor wakeups → lease arbitration → drafting

**Lock released at end of `post_user_event_and_open_turn`.** All
3 blocked actor threads can now read `coordinator.user_turn`.

#### Bob's actor wakeup (parallel):

- `bus.snapshot(audience="bob", since=4)` returns events 5-7
  (user post, user_turn_opened, obligation_recorded for alice).
- Filter `e.sender != "bob"` — all pass.
- `_pending_direct_mentions` empty.
- `decide(snap, my_id="bob", user_turn=<turn>)`:
  - `pick_priority_trigger`:
    - Event 5 (chat from "user", addressees=[]): not in addressees →
      not class 1. Not control event → not class 2. Is `event.id ==
      ut.user_event_id (5)` BUT `ut.obligation_for("bob")` is None →
      not class 3. → None.
    - Event 6 (control user_turn_opened): not class 1/2/3. → None.
    - Event 7 (control obligation_recorded for alice): control_type
      is `obligation_recorded`, but `body["participant_id"]=="alice"
      != "bob"` AND reason doesn't start with `"rerouted_from_"` →
      not class 2. → None.
  - Returns None → `AgentDecision(SKIP, trigger=None,
    considered=[5,6,7], reason="no actionable trigger")`.
- `_cursor = 7`. `_update_pending_mentions` — no user mentions in batch.
- `_dispatch_decision`: SKIP → `coord.handle_skip("bob", trigger=None)`
  — under lock, open turn exists → bump `ut.last_activity_at = monotonic()`.

Carol's actor: same outcome.

#### Alice's actor wakeup:

- `bus.snapshot(audience="alice", since=4)` returns events 5-7.
- Filter self-sender: all pass.
- `decide(snap, my_id="alice", user_turn=<turn>)`:
  - `pick_priority_trigger`:
    - Event 5 (chat from "user", addressees=[]): not class 1. Not
      class 2. Is `event.id == ut.user_event_id (5)` AND
      `ut.obligation_for("alice")` returns the must-obligation id=1
      → **class 3** (priority 3).
    - Event 6, 7: None as above.
  - Trigger = event 5 (only candidate).
  - `is_direct = (sender=="user" AND "alice" in addressees)` →
    addressees is `[]` → False.
  - `is_dead_letter = False`.
  - `has_obligation = True`.
  - Returns `AgentDecision(action="DRAFT", trigger_event_id=5,
    considered_event_ids=[5,6,7], reason="obligation")`.
- `_cursor = 7`.
- `_update_pending_mentions`: event 5 is user chat, but
  `"alice" not in addressees=[]` → not added to pending.
- `_dispatch_decision`: DRAFT.
  - `trigger = bus.get(5)` → the chat event.
  - `is_direct = False` (addressees empty).
  - `lease = coord.acquire_lease("alice", 5, is_direct_mention=False)`
    — Session 5 rejection chain:
    1. Open turn ✓.
    2. `"alice" in state.participants` ✓.
    3. `info.active=True` ✓.
    4. Allowed-speakers gate: `plan.allowed_speakers={"alice"}` non-empty;
       `"alice" in {"alice"}` ✓.
    5. `is_direct_mention=False`, but per-participant cap check:
       `ut.speaker_counts.get("alice", 0)=0 < cap=1` ✓.
    6. `is_direct_mention=False`, `max_responses=1`, `committed=0`,
       `outstanding=0` (no other leases yet); `0+0 < 1` ✓.
    7. `throttle.try_consume("alice", "main")` ✓.
    8. `budget.can_acquire(0)` ✓.
    9. Allocate `TurnLease(id=0, holder="alice", user_turn_id=0,
       trigger_event_id=5, room_epoch=4, acquired_at=monotonic,
       expires_at=monotonic+60, valid=True)`. `_leases[0] = lease`.
       Returns lease.
  - **`try: draft_handler(self, trigger=event5, lease)`** — calls
    the closure from `_make_draft_handler`:
    - `wiring = by_id["alice"]` = ParticipantWiring with proxy=alice's
      `_FunctionAgent`.
    - `prompt = build_prompt("alice", event5, coord, persona="optimist",
      capability_block="", policy=RoundRobinPolicy([...]))` —
      Session 3:
      - **System preamble**: `<<<SYSTEM PREAMBLE>>>` then
        `LOOM_PROTOCOL_INSTRUCTIONS` (always first), then
        `<persona>\noptimist\n</persona>` (fenced), then
        `Your participant id: alice`, then
        `<topic>\ndesign review\n</topic>` (fenced).
      - `policy.system_prompt` returns "" → not appended.
      - `policy.role_prompt` returns "" (RoundRobinPolicy doesn't
        override `role_prompt`; default returns "") → not appended.
      - `capability_block` empty → no `<capabilities>`.
      - `Other participants you may @-mention: bob, carol`.
      - **No prior summary**.
      - **Transcript**: `bus.snapshot(audience="alice", channel="main",
        kinds=["chat"])` returns chat events. Just the user post (id=5).
        + interleaved control events 0-7.
        Renders each via `bus.render_chat_line/render_control_line`
        (memoized).
      - **Trigger annotation**: `_render_trigger(event5, "alice", coord)`:
        `_trigger_label` returns `"REQUIRED"` (level=must obligation
        for alice). The trigger text: `"TRIGGER [REQUIRED]: chat
        event id 5 from 'user'."`.
      - **Turn card**: `_render_turn_card("alice", coord, event5)`:
        - `selected = ("alice" in plan.allowed_speakers={"alice"}) OR is_user_mention`
          → True.
        - `Required response: yes`.
        - `Instruction: <instruction>\nRound-robin mode: you (alice) are up this turn.
          Other agents are silent until the next user post. Make one
          move, then stop.\n</instruction>` (fenced — Session 3 Phase-0 fix).
        - `Max length: Keep your reply focused: one short paragraph or up to five short bullets — no lectures.`
          (`_STYLE_LENGTH_HINT["normal"]`).
        - `After responding: stop and wait for the user. Do not invite other agents.`
          (because `wait_for_user_after=True`).
      - Returns the assembled prompt string.
    - `run_streaming_call(alice_proxy, prompt, lease, bus, coord)` —
      Session 3:
      1. `bus.post(stream_start(lease_id=0, participant_id="alice",
         trigger_event_id=5))` — bus id=8. Note: `bus.post` (not
         `post_internal`) — the actor's thread is bound to "alice"
         via `bind_actor`, so sender="alice" matches binding ✓.
      2. **Loop**: `for chunk in alice_proxy.stream(prompt)`:
         - alice's `_FunctionAgent.stream(prompt)` calls
           `_stream(prompt)` which calls `alice_send(prompt)` →
           returns `"Alice: My take — start from the assumptions and
           work outward."` (~67 chars). Yields it as one chunk.
         - Inside `run_streaming_call`:
           - `cost_tokens += ceil(67/4) = 17`.
           - `validate_lease(lease)` ✓ (room_epoch matches, not
             expired, in `_leases`, valid=True).
           - `not flushed` → `buffer = "Alice: My take..."` (67 chars).
           - `PASS_RE.match(buffer)` → no.
           - `len(buffer)=67 >= pass_buffer_chars=16` → flush:
             `bus.post(stream_delta(lease_id=0, participant_id="alice",
             text="Alice: My take..."))` — bus id=9.
             - The console subscriber sees: `chat`? no, it's `stream`
               kind → drop (Session 7 invariant 154).
           - `visible = "Alice: My take..."`. `buffer = ""`. `flushed = True`.
         - Iterator exhausted (single chunk).
      3. Phase 2 tail flush: `flushed=True`, skip.
      4. **Phase 3 filters** (status="committed"):
         - `cleaned = "Alice: My take...".strip()`.
         - `_strip_chair_speak`: no chair-speak phrases → cleaned unchanged.
         - Non-empty.
         - `_is_idle_phrase(cleaned)`: not in `IDLE_PHRASES` → not idle.
         - `coord.loop_guard.is_idle_dup("alice", cleaned)`: no prior
           recorded for alice → False.
         - status stays `"committed"`.
      5. `parse_addressees(cleaned, ["alice","bob","carol"], exclude="alice")`
         → `[]` (no @-mentions in alice's reply).
      6. `bus.post(chat(sender="alice", body=cleaned, addressees=[],
         channel="main", user_turn_id=0, room_epoch=4 (lease's epoch),
         meta={"lease_id": 0, "cost_tokens": 17}))` — bus id=10.
         `committed_event_id = 10`.
         - Console subscriber: `chat`, sender="alice", channel="main"
           → `notify("\nalice ▸ Alice: My take — start from the
           assumptions and work outward.")` — printed via
           `_thread_safe_print` under `_NOTIFY_LOCK`.
      7. `bus.post(stream_end(lease_id=0, participant_id="alice",
         status="committed", error=None, committed_event_id=10))` —
         bus id=11.
      8. `coord.on_stream_end(lease, "committed",
         committed_text=cleaned, cost_tokens=17,
         committed_event_id=10)` — Session 5:
         - `budget.record(0, 17)`.
         - Open turn exists.
         - `triggering = bus.get(5)` (the user post). `is_direct = "alice"
           in event5.addressees=[]` = False.
         - status=="committed":
           - `ut.mark_drafted("alice", count_toward_cap=not False = True)`
             — `speaker_counts["alice"]=1`, `drafted={"alice"}`,
             `last_activity_at=monotonic`.
           - `committed_text` non-empty → `loop_guard.record("alice",
             cleaned)`.
           - `obligation_for("alice")` returns obligation id=1.
           - `_resolve_obligation_locked(1, by_event_id=10,
             expected_holder="alice")`:
             - `expected_holder` matches ✓.
             - `mark_obligation_resolved(1, by_event_id=10)` → True.
             - **Emit `obligation_resolved(obligation_id=1,
               participant_id="alice", resolved_by_event_id=10)`** —
               bus id=12.
         - `_maybe_close_user_turn_locked()`:
           - `committed_count=1`, `cap=plan.max_responses=1`.
           - `cap_reached = 1 > 0 AND 1 >= 1 = True`. ALSO
             `is_user_turn_complete(ut)` is True (only obligation
             resolved).
           - `_close_user_turn_locked("completed")`:
             - `ut.close("completed")` — state="closed",
               closure_reason="completed".
             - `state.current_user_turn_id = None`.
             - **Emit `user_turn_closed(user_turn_id=0,
               reason="completed")`** — bus id=13.
             - `plan.wait_for_user_after = True` AND
               `state.control.wait_for_user = False` →
               `state.set_wait_for_user(True)`. **Emit
               `floor_updated(wait_for_user=True)`** — bus id=14.
             - `plan.advance_turn_pointer = True` AND
               `state.control.turn_taking_mode == "round_robin"` →
               `state.advance_round_robin_pointer()`:
               - `live = ["alice", "bob", "carol"]` (all still
                 active+capable in turn_order).
               - `next_speaker_idx = (0 + 1) % 3 = 1` → bob is up next.
   - `finally: coord.release_lease(lease)` — pops `_leases[0]`,
     marks invalid.

### Phase 6: Other actors finish their wakeups

Bob's actor (was blocked on `wait_after`) was woken by all the new
events posted during alice's draft (8-14). It snapshots since
cursor=7, gets events 8-14. None are class 1/2/3 for bob (he has no
obligation in turn 0; turn 0 is now closed). `decide` returns SKIP.
`handle_skip` no-ops (no open turn). Cursor advances to 14.

Carol's actor: same.

All three actors return to `wait_after(cursor=14, timeout=20)` and
**block** waiting for the next event. The room is in
`wait_for_user=True` mode.

### Phase 7: Console echoes the reply

The console subscriber printed `"alice ▸ Alice: My take — start from
the assumptions and work outward."` to stdout when bus event id=10
(the chat) was posted. The user sees:

```
you ▸ hello, what's first?

alice ▸ Alice: My take — start from the assumptions and work outward.
you ▸ ▎
```

(The `you ▸` prompt re-renders because `prompt_fn = input("you ▸ ")`
was waiting on stdin throughout.)

### What we exercised

Across this single trace:

- **Layer 0** (orientation): the public surface (LoomRoom,
  RoundRobinPolicy, agent_from_send), the four owners (kernel,
  policy, agents, room facade).
- **Layer 1** (primitives): `Event` factories (chat, control, stream),
  `RoomState` mutations (add_participant, set_anchor, set_topic,
  set_turn_taking_mode, set_turn_order, advance_round_robin_pointer,
  set_wait_for_user), `RoomControlState`, `ParticipantInfo`,
  `UserTurnPlan` (frozen-at-open, every declarative field used),
  `UserTurn` (lifecycle from open → drafted → closed("completed")),
  `ResponseObligation` (recorded → resolved with by_event_id).
- **Layer 2** (bus): `MessageBus.post` AND `post_internal` (with
  `bind_actor` sender authentication for actor-thread posts),
  `notify_all` waking 3 daemon threads, `snapshot(audience, since)`
  with O(E-since) slice, `subscribe` for the console renderer,
  `visible_to` filtering DMs (here all main; nothing filtered).
- **Layer 3** (prompt + streaming): `build_prompt` 5 sections,
  fenced `<persona>` / `<topic>` / `<instruction>`, kernel charter
  always first, `RoundRobinPolicy.role_prompt` returning empty.
  `run_streaming_call` 5-phase lifecycle, PASS detection (didn't
  fire), buffer flush at 16-char threshold, post-stream filter chain
  (chair-speak → empty → idle → loop_guard), `parse_addressees` at
  commit time, exactly-one `stream_start`/`stream_end` contract.
- **Layer 4** (actor + journal): one daemon thread per participant,
  `bind_actor` at loop entry, `wait_after(timeout=20)` blocking,
  `decide()` pure decision function with priority-based trigger
  selection, `_dispatch_decision` with `try/finally release_lease`.
  Journal disabled in this trace (no `journal_dir`).
- **Layer 5** (coordinator): `RoomCoordinator` as single mutator,
  `RLock` (re-entrant — `unregister_participant` cascades through
  `_resolve_obligation_locked` etc.; here only `open_user_turn`
  recursed implicitly), `post_user_event_and_open_turn` atomic-under-lock
  race fix, `_run_policy_under_lock` with watchdog (no slow/error
  here), `_apply_plan_state_changes_locked` applying mode + order,
  9-step lease acquisition rejection chain, `on_stream_end` with
  status-specific resolution.
- **Layer 6** (contracts + policies + adapters): `Agent` Protocol via
  `_FunctionAgent`, `ConversationPolicy` ABC via
  `RoundRobinPolicy` (direct subclass — needs declarative state
  mutation), policy purity (read-only `RoomStateView`).
- **Layer 7** (facade + runtime): `LoomRoom.__init__` validation,
  `_agent_to_wiring` typo detection, `build_loom_session` 11-step
  factory, `LoomSession` actor pool, `_make_draft_handler` closure
  capturing `by_id` by reference, `run_console` REPL with
  `_make_console_subscriber` rendering filter.

The trace is fully consistent with all 200+ invariants captured in
Sessions 0–9. **Nothing surprised us.** That's the validation goal of
the curriculum.

---

## Dry-run modification: deep-frozen `RoomStateView` (v0.2)

The first kernel-modification target after the curriculum. Picking a
small, well-contained item is the right opening move — enough to
exercise our understanding without committing to a multi-week refactor.

### Context

**The soft leak** (Session 0 invariant from limitations table; Session
1 invariant 15; Session 6 `assert_no_state_mutation` fixture was
designed to catch it):

> `RoomStateView` is shallow — `participants` and `control.roles` are
> read-only mappings, `control.turn_order` and `floor_owner` are
> tuples, and the view itself is a frozen dataclass. **Leaf-level
> mutation (`participant_info.active = False` through a captured
> alias) is still possible**; full deep-freeze with
> `ParticipantInfoView` is on the v0.2 list.

### Goal

Add `ParticipantInfoView` (frozen, read-only) so policies cannot
mutate `info.active`, `info.capable`, `info.cost_tier`, or
`info.role_hints` through a captured `ParticipantInfo` alias.

### Files to touch

| File | Change |
|---|---|
| `loom/kernel/room.py` | Add `@dataclass(frozen=True) ParticipantInfoView`; modify `RoomState.view()` to wrap each `ParticipantInfo` in a `ParticipantInfoView`; update `RoomStateView.participants` type from `Mapping[str, ParticipantInfo]` to `Mapping[str, ParticipantInfoView]`. |
| `loom/kernel/prompt.py` | If `prompt.py` directly accesses `info.<field>` on a `ParticipantInfo` from a view, change to use the view. (Likely safe — Session 3 shows it goes through `state.participants[...]` which would be auto-converted.) |
| `loom/policy/default.py` | `_aliases_for(participant_ids)` is OK; `info.active` / `info.capable` checks in `plan_user_turn` need the view fields. Same field names → no source change needed if `ParticipantInfoView` mirrors the field set. |
| `loom/policy/round_robin.py` | `info.active and info.capable` reads in `plan_user_turn` — same as above. |
| `loom/policy/single_responder.py` | Same. |
| `loom/policy/open_chat.py` | Same. |
| `loom/policy/base.py` | Doesn't touch participants directly. |
| `loom/contracts.py` | The `ConversationPolicy.plan_user_turn` signature still takes `RoomStateView`; the inner `participants` value type changes. May need to expose `ParticipantInfoView` from `loom.kernel.room` for type-checking purposes. |
| `loom/testing.py` | `make_test_state` uses `state.view()` which auto-converts. `_snapshot_view` reads `info.active/capable/cost_tier` — same field access works on view. |

**Files that should NOT change**:
- `loom/kernel/coordinator.py` — only uses live `RoomState` (mutator),
  not the view.
- `loom/kernel/actor.py` — uses live `state.participants[holder]` via
  coord, not the view.
- `loom/kernel/streaming.py` — uses
  `coordinator.state.participants.keys()` for addressable list, not
  view fields.
- `loom/runtime.py` — `LoomSession.add_agent` constructs
  `ParticipantInfo` for `coord.register_participant`, not view.

### Implementation sketch

```python
# loom/kernel/room.py — add after ParticipantInfo class:

@dataclass(frozen=True)
class ParticipantInfoView:
    """Read-only view of :class:`ParticipantInfo`.

    Frozen dataclass — attribute reassignment raises FrozenInstanceError.
    ``role_hints`` is wrapped in MappingProxyType so dict mutation
    raises TypeError.

    Constructed by :meth:`RoomState.view` from each live
    ParticipantInfo. Field set MIRRORS ParticipantInfo exactly so
    consumers (policies, prompt) read the same names.
    """
    id: str
    capable: bool
    cost_tier: int
    active: bool
    role_hints: Mapping  # MappingProxyType

    @classmethod
    def from_info(cls, info: ParticipantInfo) -> "ParticipantInfoView":
        return cls(
            id=info.id,
            capable=info.capable,
            cost_tier=info.cost_tier,
            active=info.active,
            role_hints=MappingProxyType(info.role_hints),
        )


# Modify RoomStateView.participants type annotation:
class RoomStateView:
    ...
    participants: Mapping[str, ParticipantInfoView]   # was ParticipantInfo
    ...


# Modify RoomState.view() — change just one line:
return RoomStateView(
    ...
    participants=MappingProxyType({
        pid: ParticipantInfoView.from_info(info)
        for pid, info in self.participants.items()
    }),
    ...
)
```

### Invariants to preserve

This change must NOT break:

1. **Boundary invariant 3** (Session 0): policy cannot mutate state
   or post to bus. Strengthened by this change — we close the soft
   leak.
2. **Session 6 invariant 132** (BasicPolicy reads
   `state.participants`): the field set on `ParticipantInfoView`
   MUST mirror `ParticipantInfo` exactly so `info.active`,
   `info.capable`, `info.cost_tier`, `info.id` all keep working.
3. **Session 1 invariant 15** (the soft leak we're closing): after
   this change, the invariant text needs to be updated — leaf-level
   mutation through a captured alias now raises
   `FrozenInstanceError`.
4. **Session 5 invariant 75** (atomic `post_user_event_and_open_turn`):
   the view is taken under the coord lock; we're not changing the
   ordering of view construction, just the per-participant wrap.
5. **Performance**: the `view()` call must remain "cheap" (Session 1
   docstring). Today's `view()` wraps `participants` in
   `MappingProxyType` (no copy). After: it builds a new dict with N
   `ParticipantInfoView` instances. **O(N)** instead of O(1). Bench
   impact: `chat` scenario at `n_agents=25` makes ~5 view() calls
   per turn (policy + prompt + ...) so 125 ParticipantInfoView
   constructions per turn. At ~500ns per dataclass construction =
   62.5μs per turn extra. Baseline turn at n=25 is probably ~100ms,
   so **<<0.1% — well under the 15% gate**.
6. **`tests/property/test_ux_contracts.py`** — currently asserts
   `participants` mapping is read-only. Add asserts that each
   `info.active = False` raises. Will FAIL until the change lands;
   should be added IN THE SAME PR.
7. **`tests/system/conftest.py:_FORBIDDEN_API_PATTERNS`** — doesn't
   reference `ParticipantInfo` mutations, so unchanged.
8. **`tests/test_kernel_kernel_boundary.py`** — the boundary tests
   are static greps; they'd pass either way. The runtime mutation
   defense is what changes.

### Tests to add

In **`tests/test_kernel_room.py`** (new class):

```python
class ParticipantInfoViewIsFrozen(unittest.TestCase):
    def test_attribute_reassignment_raises(self):
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="a", capable=True,
                                              cost_tier=1, active=True))
        view = state.view()
        info_view = view.participants["a"]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            info_view.active = False
        with self.assertRaises(dataclasses.FrozenInstanceError):
            info_view.capable = False
        with self.assertRaises(dataclasses.FrozenInstanceError):
            info_view.cost_tier = 99

    def test_role_hints_mapping_is_read_only(self):
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="a",
                                              role_hints={"role": "teacher"}))
        view = state.view()
        info_view = view.participants["a"]
        with self.assertRaises(TypeError):
            info_view.role_hints["role"] = "student"

    def test_view_field_set_mirrors_participant_info(self):
        # All public fields on ParticipantInfo must exist on ParticipantInfoView.
        info_fields = {f.name for f in dataclasses.fields(ParticipantInfo)}
        view_fields = {f.name for f in dataclasses.fields(ParticipantInfoView)}
        self.assertEqual(info_fields, view_fields)

    def test_live_state_mutation_visible_through_view(self):
        # Live state mutations should be visible to views taken AFTER.
        # Views taken BEFORE are snapshots — the existing live-update
        # contract on RoomStateView documents this.
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="a", active=True))
        view_before = state.view()
        state.set_active("a", False)
        view_after = state.view()
        # view_before is now stale (it captured active=True).
        self.assertTrue(view_before.participants["a"].active)
        self.assertFalse(view_after.participants["a"].active)
```

In **`tests/property/test_ux_contracts.py`** (extend existing):

```python
@given(state=room_state_strategy())
def test_view_participant_info_is_immutable(state):
    view = state.view()
    for pid, info in view.participants.items():
        with pytest.raises(dataclasses.FrozenInstanceError):
            info.active = not info.active
```

Plus update the `assert_no_state_mutation` snapshot to include
`role_hints` (Session 7 open question 14).

### Expected risks

1. **Performance regression** — bench-diff might show a small bump
   on the `chat` scenario at n_agents=25 (the largest axis). Pre-mitigation:
   if the cost is non-trivial, cache the view (or just the
   participants sub-mapping) per `room_epoch` — invalidate when
   `room_epoch` bumps. For v0.2 keep it simple; revisit if bench
   regression > 5%.
2. **Breakage in third-party policies** that captured a
   `ParticipantInfo` and mutated it (intentionally or not). This
   was always a bug per the contract; the fix LOUDLY surfaces it
   via `FrozenInstanceError` instead of silently corrupting state.
   Worth a CHANGELOG note: "v0.2: `RoomStateView.participants`
   values are now frozen `ParticipantInfoView` instances. Policies
   that previously mutated `info.<field>` (which was always against
   the contract) will now raise `FrozenInstanceError`. Fix the
   policy."
3. **Type-checker noise**: any code typing `Mapping[str,
   ParticipantInfo]` will need to update to
   `Mapping[str, ParticipantInfoView]`. mypy would catch these in
   CI.
4. **`assert_no_state_mutation` becomes redundant for top-level
   info fields** — leaf mutation now impossible. The fixture
   itself remains useful for `topic`/`floor`/`roles` etc.

### Recommended PR shape

ONE PR, kernel-only:
- `loom/kernel/room.py` — add `ParticipantInfoView`, modify `view()`.
- `tests/test_kernel_room.py` — new class.
- `tests/property/test_ux_contracts.py` — new test.
- `tests/test_kernel_kernel_boundary.py` — confirm static grep tests
  still green.
- `docs/internal/study/00-orientation.md` — update v0.1.2 limits
  table to mark "deep-frozen `RoomStateView`" as DONE.
- `CHANGELOG.md` — entry under `[Unreleased]` mentioning the fix +
  the breaking semantics.

Sequence:
1. Write `ParticipantInfoView` + tests.
2. Run `pytest tests/test_kernel_room.py` (kernel tier).
3. Run `make test-quick` to catch downstream breakage in policies +
   facade.
4. Run `make bench-quick` to check perf shift (must be < 15% on
   every scenario).
5. Run `make ux-check` to verify the public symbol count didn't
   change unexpectedly.
6. Open PR.

### Why this item first

- **Smallest scope** of the v0.2 list — touches one new dataclass +
  one method.
- **Closes a known soft leak** that's already documented and
  test-fixture-defended (`assert_no_state_mutation` exists because
  of this).
- **No coordinator / actor / streaming / journal changes** — leaves
  the concurrency-critical paths untouched.
- **No public-surface impact** — `ParticipantInfoView` is internal
  to `loom.kernel.room`; consumers see "the field set didn't
  change" but suddenly mutation fails loudly.
- **Sets the pattern** for future deep-freeze work (e.g. if we add
  `RoomConfigView`, `UserTurnPlanView` for more aggressive
  immutability).

---

## Index of invariants

This section lets us look up any invariant by number without
re-reading source.

| Range | Origin | Topic |
|---|---|---|
| **1–11** | 00-orientation | Boundary (kernel/policy import asymmetry, max_responses, dead-letter, watchdog, sender auth, subscriber discipline) |
| **12–19** | 01-primitives | Event construction, ts wall-clock vs monotonic, `_is_int` bool guard, UserTurnPlan __post_init__ checks, RoomState mutation map, `is_known_control` filter, scrubber order |
| **20–30** | 02-bus | `ev.id == position`, inline subscribers, `_post_unchecked` chokepoint, body cap, `post_internal` privileged, re-bind RuntimeError, `stop()` no subscriber notify, DM via prompt boundary, render memo cache key, immutable subscriber tuple |
| **31–48** | 03-prompt-streaming | Charter immutability, fence policy, identifier-only fence names, transcript audience filter, control interleaving, memo on hot path, PASS regex token boundary, buffer threshold, post-stream filters committed-only, `passed` resolves vs `suppressed` doesn't, exactly-one `stream_start/end` contract, lease.room_epoch on chat |
| **49–71** | 04-actor-journal | Daemon thread per participant, single bind, cursor monotonic, pending LRU user-only, lease release in finally, idle-timeout via timeout branch, events.jsonl authoritative, atomic snapshot writes, owner-only perms, bounded snapshot queue, recursion guard, pre-open drop, `is_known_control` replay filter, `replay_into` privileged, `restore_state` defensive coercion |
| **72–101** | 05-coordinator | Single mutator, RLock re-entrant, all `post_internal`, atomic post+open, plan state changes BEFORE open, `policy_error` always emitted, `policy_slow` observability, library default close_turn, debounce returns existing, `wait_for_user` cleared on user post, empty plan auto-close `no_responder`, `cancelled` resolves obligations, `wait_for_user_after` even on cancelled, `advance_turn_pointer` at close+round_robin, distinguished close reasons, cap_reached OR complete, 3 direct-mention bypasses, max_responses race fix, monotonic timestamps, lease.valid+_leases dichotomy, set_default_responder invalidates ALL, set_topic closes turn first, 7-step unregister cascade, first transfer only, mutates frozen plan.allowed_speakers, on_stream_end always records cost, passed no mark_drafted, suppressed leaves intact, expected_holder defensive |
| **102–129** | 06-contracts-policies | `loom.contracts` neutral, `Agent` runtime_checkable, optional getattr defaults, agent_id non-empty, `_FunctionAgent` per-chunk cancel re-check, `agent_from_object` resolution order, `_extract_text` fallback chain, sync/<10ms policy contract, `prior_speaker` removed, `name` ABC default, `BasicPolicy` sorted responders, `_wait_for_user_after` defaults False, `SingleResponder` canonical reference, inactive→ack, `OpenChat` stateless, `RoundRobin` direct subclass, dedupe order, first-vs-subsequent post, `advance_turn_pointer=True/False` on rotation/override, `DefaultPolicy` ANCHOR_SYNTHESIS dup, module-level regexes, `_aliases_for` includes head segment, Case 1 highest priority, Case 5 needs ≥2, fall-through `plan_for_default(None,...)`, role_prompt for anchor+default_responder slots, no `system_prompt` overrides in v0.1.2 |
| **130–165** | 07-public-facade | `LoomRoom` canonical door, `with` required, anchor defaults to first agent, `_warn_on_typoed_agent_attrs` 0.75 cutoff, agent itself as proxy, `post_and_wait` snapshots len before, ack-shaped TurnResult, user_turn_id correlation, closure-reason precedence, `_monotonic` static for patching, dm via plan_for_default, `set_topic` 500-char cap, `LoomSession.add_agent` order, draft handler required, no restart, best-effort snapshot on stop, validate slot ids exist, journal callbacks via `post_internal`, `_VALID_CHANNEL_RE`, `addressees` populated at user post, `handled=False` for non-slash, `/quiet` refuses all-silenced, `/goal` aliases `/topic`, unknown control → None, drop stream events from console, drop agent-to-agent DMs, `Message.from_event` str() coerces body, TurnResult iterable+len+bool+index, projection unknown values pass-through, `LoomError` lazy re-export, `make_test_state` returns view, `make_test_event(id=1)` non-zero default, `FakeProxy` records last_prompt, `RecordReplayProxy` literal-key, `_snapshot_view` covers (active,capable,cost_tier) |
| **166–183** | 08-test-architecture | unittest in kernel tests, tier-specific watchdogs, autouse leak guards, strict-markers requires declaration, system collection-time discipline check, `_lift_room_throttle` test seam, hypothesis profiles, central strategies, perf marker excludes from default, `_bench` GC disabled, mutmut paths exclude runtime+facade, 98% branch gate, `InMemoryFaultJournal` beats chmod, 7 adversarial agent shapes, bench fixture curries+records, fake_clock patches both clocks, forbidden patterns enumerated, `pytest_configure` redundant declaration safety net |
| **184–202** | 09-benchmarks-ci | Baseline relative-only, 15% threshold, p99 reported not gated, ✓ improvement tag, JSON+MD output, host metadata, RSS slope records threads, adversarial asserts log+latency, conftest copies _bench, `bench/adversarial/` opt-in, tracemalloc canonical memory metric, GC disabled in bench, 98% TOTAL branch (not per-module), 90% mutation kill target, run_full_quality fails on new survivors, CI runs fast suite + lint + types on 3.11/3.12, perf extras dev-only, `_make_room` sets anchor+default_responder, bench reply text > 50 chars |

Total: **202 invariants** across the 10 sessions.

---

## Verification — answering ad-hoc questions without re-reading source

### Q1: Where would I add per-participant cost budgets?

Today (Session 0 v0.1.2 limitations): "No per-message rate limiting
or per-participant cost budgets in the public API. The kernel has the
hooks; the room facade doesn't expose them yet."

The kernel hook is **`BudgetConfig`** in
`loom/kernel/coordinator.py` (Session 5). It currently tracks
**cumulative tokens per UserTurn**, not per-participant. To add
per-participant budgets:

1. **`loom/kernel/coordinator.py`**:
   - Extend `BudgetConfig` to track `_per_turn_per_participant:
     dict[tuple[int, str], int]` keyed by `(user_turn_id, holder)`.
   - Add `can_acquire_for(user_turn_id, holder, estimated_cost) ->
     bool` method that checks both the per-turn AND per-participant
     caps.
   - Add a `max_tokens_per_user_turn_per_participant: int` field
     (frozen, default e.g. 50_000).
   - Modify `acquire_lease` step 8: change `_budget.can_acquire(ut.id)`
     to `_budget.can_acquire_for(ut.id, holder)`.
   - Modify `on_stream_end`: change `_budget.record(lease.user_turn_id,
     cost_tokens)` to record per-participant too.

2. **`loom/room.py`**: expose a `room_config` field for the budget
   knob. `RoomConfig` (in `loom/kernel/room.py`) is the natural
   home; add `max_tokens_per_user_turn_per_participant` there.
   `RoomConfig` is frozen — boot-time only.

3. **`loom/kernel/events.py`**: optional new `participant_budget_exceeded`
   control event for observability when a participant hits their cap
   mid-turn (similar to `policy_slow`). Add to `CONTROL_TYPES`
   frozenset; add a factory.

4. **Tests**: `tests/test_kernel_coordinator.py:BudgetTests` extend
   to cover the per-participant axis. `tests/system/test_capacity_and_limits.py`
   add a system-level scenario.

5. **Docs**: update Session 0's v0.1.2 limitations table (mark this
   as done) AND `docs/security-model.md` (the kernel deferred
   hardening list).

**Estimated risk**: Low. `BudgetConfig` is internally scoped (Session
5 — frozen with mutable internal dict, F4.4/P2.2). Changes are
additive — existing callers of `can_acquire(ut.id)` still work via a
default-everyone-passes fallback. Minor perf impact (one extra
dict lookup per `acquire_lease` call).

### Q2: What would async policies require to keep invariant 7 intact?

Invariant 7 (Session 0): **Coordinator is the only mutator of
`RoomState`** and the only writer to bus's authoritative slots.

The current contract (Session 5 invariant 75): `policy.plan_user_turn`
runs **under the coord lock**, atomic with `bus.post_internal(user_event)`
+ `_apply_plan_state_changes_locked` + `open_user_turn`. The lock is
held to prevent the actor-cursor race (actors block on
`coordinator.user_turn` until the open completes).

For **async / off-lock policies**:

1. **Cannot hold the coord lock across the policy call** (the whole
   point — a slow policy currently freezes every actor thread).
2. But **must still ensure actors don't see the user event before
   the turn opens**. Options:
   - **Quarantine the user event**: post the event to a "pending"
     slot the actors don't snapshot from. Actors snapshot the bus,
     so the event has to be visible — rules out trivial quarantine.
   - **Two-phase open**: post the user event; immediately under lock
     create an empty/pending UserTurn (`state="pending"`) so actors
     looking at `coordinator.user_turn` see SOMETHING; then run the
     policy off-lock; finally re-acquire lock to swap the turn from
     `pending` → `open` with the real plan.
   - **Cursor speed-bump**: under lock, post the user event AND
     stamp it with `meta={"awaiting_classification": True}`; actors
     in `decide()` short-circuit on this meta and SKIP without
     advancing the cursor (they remain blocked at this event id);
     when the policy returns, `_apply_plan_state_changes_locked` +
     `open_user_turn` proceed and stamp a follow-up event the actors
     wake on.

The cleanest is probably the **two-phase open** with `state="pending"`
(Session 1 already has `UserTurnState = Literal["open", "closing",
"closed"]` where `"closing"` is unused — repurpose for this).

3. **Session 5 invariant 76** (plan state changes BEFORE open
   check) — must be preserved. With async, the mode-flip happens
   when the policy returns, not synchronously with the user-event
   post.

4. **`policy_error` / `policy_slow` semantics** — the watchdog
   becomes "max time we'll wait before closing the pending turn".
   Currently 100ms threshold (observability); for async, becomes a
   real timeout that closes the pending turn with a new
   `closure_reason="policy_timeout"` (extend the `ClosureReason`
   Literal in Session 1).

5. **Tests**: huge new property test surface — "no actor sees a
   committed reply for a turn that hasn't opened yet"; "policy timeout
   closes pending turn cleanly"; "concurrent user posts mid-async
   classify all serialize through the pending-slot mechanism".

6. **`tests/test_kernel_kernel_boundary.py`** tests still pass —
   they're static greps against the policy modules. The runtime
   contract (boundary invariant 5: policy errors fail closed) needs
   a new test for the async timeout case.

**Estimated risk**: HIGH — this is a foundational concurrency
change. Affects coordinator, actor, possibly bus (new event meta
field). Property tests are essential. Mutation kill rate would need
re-baselining.

This is **the v0.2 work item with the largest blast radius**. The
deep-frozen view (which we'll do first) is a much safer starter.

### Q3: Where would I add a hash chain over the journal?

Session 9 (perf-baseline / mutation): "Hash chain over the journal
(audit P3 / R1). Defense in depth for `events.jsonl` integrity beyond
per-line shape validation."

Today (Session 4 — `restore_state` + `iter_events`): tampered lines
are caught by `Event.from_jsonl` shape validation; corrupt lines
surface as `journal_corruption` events. A disk-write attacker who
stays within the per-line shape can pre-stage state silently.

To add a hash chain:

1. **`loom/kernel/journal.py`**:
   - On `on_event`: compute `line_hash = sha256(prev_line_hash +
     event.to_jsonl()).hexdigest()` and APPEND it as part of the
     line: `f"{event.to_jsonl()}\t{line_hash}\n"`. Or write to a
     companion `.hashes` file (less invasive on the JSONL format).
   - On `iter_events(emit_corruption_events=True)`: re-compute the
     hash for each line; if it doesn't match the recorded value,
     emit a NEW control event `journal_chain_break` (extend
     `CONTROL_TYPES`).
   - Optionally: emit `journal_chain_verified` once at startup
     after replay completes.

2. **`loom/kernel/events.py`**:
   - Add `journal_chain_break` to `CONTROL_TYPES` frozenset.
   - Add a factory function.

3. **`loom/kernel/journal.py:_state_to_dict`** and `restore_state`:
   - The snapshot ALSO needs to record `last_known_hash` so an
     attacker can't truncate the chain and start fresh.
   - Increment `SNAPSHOT_VERSION` to 5; add v5-restore handling.

4. **`bench/adversarial/test_tampered_replay.py`**: add a new test
   that mutates a valid line (preserving shape) and verifies the
   chain-break event surfaces.

5. **Tests**: property test "chain hash for arbitrary event streams
   is stable across `to_jsonl ∘ from_jsonl`".

6. **Docs**: update `docs/security-model.md` (move "hash chain" out
   of "deferred future hardening" into "implemented hardening").
   Update Session 4 invariant list — invariant 67 (defensive
   coercion) gets a sibling about hash verification.

**Performance**: per-line SHA256 hash on 256-byte events ≈ ~2μs each.
At 10k events/min throughput (reasonable upper bound), that's 20ms/min
of hash compute — negligible. The `journal` perf scenario at E=10k
would shift maybe 10% (currently dominated by JSON serialize). Worth
a `benchmark/perf.py` axis.

**Backwards compatibility**: v4 snapshots don't have hashes; loading
them sets `last_known_hash=""` and skips chain verification for the
historical lines (warn loudly via a `journal_chain_unverified`
event).

**Estimated risk**: Medium. Self-contained in the journal module +
new event kind. The replay path is the trickiest — need to handle
the "recovered, started a new chain" case after `journal_chain_break`.

---

## What this curriculum has prepared us for

After 10 sessions, we have:

- **Function-level understanding** of every public symbol and most
  internal helpers across 9,233 LOC.
- **202 cross-referenced invariants** spanning kernel mechanism,
  policy contract, public surface, test architecture, and perf
  gates.
- **End-to-end traces** for 4 canonical scenarios: bus race
  resolution (Session 2), `room.post_and_wait("hi @gpt")` (Session 5),
  agent waking on chat addressed to it (Session 4), the round-robin
  classroom (Session 10).
- **Dry-run modification plan** for the v0.2 deep-freeze item — the
  natural starter.
- **Verbal answers to ad-hoc design questions** without re-reading
  source (Q1: per-participant budgets; Q2: async policies;
  Q3: journal hash chain).

The 11 study artifacts under `docs/internal/study/` are the
authoritative summary going forward. Future sessions can:

- Skim `00-orientation.md` for invariant numbers.
- Read the relevant layer's deliverable (`01–07`) for any specific
  module.
- Use Session 8 to find the right test tier for a new test.
- Use Session 9 to predict perf impact of a change.
- Use Session 10's modification template (the deep-freeze sketch) as
  the shape for any v0.2 work.

The first concrete coding session is the deep-frozen
`RoomStateView` PR (Phase E recommendation). After landing that,
the other v0.2 items become reachable in priority order:

1. Deep-frozen `RoomStateView` ← STARTER
2. Hash chain over the journal (medium scope)
3. Per-participant cost budgets (low scope)
4. Off-thread subscriber dispatch with timeout (medium-high scope —
   touches `MessageBus._post_unchecked`)
5. Auto-restart-recovery wiring from journal (medium scope —
   touches `build_loom_session`)
6. Policy-state snapshot/restore lifecycle hooks (medium scope)
7. Async / off-lock policies (HIGHEST scope — concurrency-foundational)
8. Standalone PyPI package (release-mechanics; not a code change)

The curriculum is complete. The plan worked. Ready to code.

---

## Cross-references

- depends on: every session 00–09. This is the synthesis pass.
- depended on by:
  - The first kernel-modification PR (deep-frozen view).
  - Future curricula iterations — this is the template.

## Open questions / things to revisit

1. **`assert_no_state_mutation` becomes partly obsolete** after the
   deep-freeze ships. Consider renaming or repurposing for the
   `topic` / `floor` / `roles` fields that remain mutable through
   the live state (not view).
2. **The `participants` view dict construction is O(N) per
   `view()` call** (vs O(1) today). For very large rooms (50+
   agents) this may matter. Worth a `view()` cache invalidated by
   `room_epoch` bumps. Defer until bench shows it.
3. **`_aliases_for` in `loom/policy/default.py` reads
   `state.participants` IDs (just keys) — doesn't dereference info
   fields**. So the deep-freeze doesn't affect the vocative path.
   Confirmed.
4. **`tests/property/test_ux_contracts.py`** likely already has a
   "policy cannot mutate state" property test. The new freeze
   tightens that; the existing test should now verify the
   `FrozenInstanceError` raise path.
5. **`docs/loom-ux-spec.md` §4.3** mentions the soft leak. Update
   to "RESOLVED in v0.2" once the freeze ships.
6. **The `mutation-survivors.md` triage may have new entries** after
   the freeze — equivalent mutants that flip a field that used to
   matter (because of the leak) but now can't be reached. Worth a
   re-baseline.
7. **The `weave-repo` next door** consumes Loom. If it captured a
   `ParticipantInfo` and mutated it (against contract), this freeze
   breaks weave. Worth a quick grep over weave's source before
   shipping.
8. **`docs/internal/perf-baseline.md` operation table** (Session 9)
   doesn't list `view()` because it's not measured today. After the
   change, add a microbench case `RoomState.view N=25` to
   `benchmarks/perf.py:run_micro` so we have a baseline.
