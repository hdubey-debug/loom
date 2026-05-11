# Changelog

All notable changes to **Loom** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed
- **`RoomControlState.floor_owner`**, `RoomState.set_floor_owner()`,
  `RoomCoordinator.set_floor_owner()`, and the `LoomRoom.set_floor()`
  facade method. The field was a soft signal with no kernel-enforced
  semantics — `DefaultPolicy` interpreted it to narrow
  `allowed_speakers` across turns. v0.2 removes the kernel-level
  carrier; equivalent UX patterns:
  - `@<id>` an agent directly each turn for direct narrowing.
  - For persistent narrowing, subclass `DefaultPolicy` and keep the
    narrowed set as policy-internal state.
  - The `floor_updated` control event survives (under its legacy name
    for journal back-compat) and now carries only `wait_for_user`;
    `RoomCoordinator.set_wait_for_user_flag()` replaces the
    floor-aware `set_floor_owner()` call.
- **Console commands `/floor`, `/release`, `/quiet`** were removed
  along with the field they wrote to. The runtime now returns a
  removed-feature notice rather than mutating state. `/who` no longer
  renders a `floor:` line; `/control` no longer renders one either.
- **Snapshot schema dropped `control.floor_owner`** in v5. Older v1-v4
  snapshots carrying the field still load cleanly; the field is
  silently discarded at restore time.
- **`RoomControlState.turn_taking_mode`** and
  **`UserTurnPlan.set_turn_taking_mode`**. Round-robin is now
  signalled by ``state.control.turn_order`` being non-empty; entering
  the mode means setting ``turn_order`` to a non-empty list, and
  exiting means setting it to ``[]``. The `TurnTakingMode` Literal
  type alias and `RoomState.set_turn_taking_mode()` method are also
  gone. Snapshot schema bumped to v5; v3/v4 snapshots carrying a
  ``turn_taking_mode`` field are still loadable — the value is
  discarded at restore time.
  - Migration for downstream policies: replace
    ``plan.set_turn_taking_mode = "round_robin"`` with
    ``plan.set_turn_order = […]``; replace
    ``set_turn_taking_mode = "broadcast"`` with
    ``set_turn_order = []``. To convey "round-robin is active" to
    agents in the system prompt, override
    ``ConversationPolicy.charter_text(state)`` — the default
    implementation already emits a rotation advisory whenever
    ``state.control.turn_order`` is non-empty.

### Added
- **`TriggerPriorityFn` Protocol +
  `RoomConfig.trigger_priority` override**. The actor's trigger
  classification (direct mention → dead-letter/reroute →
  required-obligation user post) is now a hook, with the v0.1.2
  classifier exposed as
  ``loom.kernel.actor.DEFAULT_TRIGGER_PRIORITY``. ``RoomConfig``
  gains a ``trigger_priority`` field (``None`` means "use the
  default"); :func:`loom.kernel.actor.decide` and
  :func:`pick_priority_trigger` both accept the hook via a
  keyword-only ``priority_fn=`` parameter for direct testability.
- **Dedicated coordinator watchdog thread**. ``RoomCoordinator`` now
  spawns a daemon thread (``loom-coord-watchdog``) that fires
  ``check_idle_timeout`` every ``RoomConfig.watchdog_interval_s``
  (default 5.0s). ``LoomSession.start()`` / ``stop()`` wire the
  thread's lifecycle automatically. The existing piggybacked call on
  ``ParticipantActor._loop`` is retained as defense-in-depth — both
  paths are idempotent. Exceptions in the watchdog loop are swallowed
  so a single bad tick cannot crash the thread.
- **`ConversationPolicy.should_post_response(body, state, participant_id)
  -> bool`** veto hook. Called by
  :func:`loom.kernel.streaming.run_streaming_call` AFTER the kernel's
  built-in filters (empty / idle-phrase / IoU loop-guard). Returning
  ``False`` suppresses the commit; returning ``True`` lets it
  proceed. Default ``True`` — the kernel filters are unchanged.
  Useful for policy-specific veto rules (semantic similarity,
  off-topic detection, custom rate limits). Buggy hooks that raise
  are treated as ``True`` so a bad policy cannot drop a legitimate
  response.
- **`PromptSection` dataclass + `ConversationPolicy.prompt_sections()`
  hook**. Policies can inject named sections into the system preamble
  immediately after the kernel charter, persona, topic, participant
  id, capabilities, and the legacy `system_prompt` / `role_prompt`
  blocks. Each section renders with an uppercase ``<<<NAME>>>``
  header so prompt diffs are attributable. Default returns ``[]`` —
  bundled policies emit no extra sections. Empty-text sections are
  silently skipped; a buggy hook that raises is caught and skipped so
  ``build_prompt`` never breaks on user error.
- **`LeaseCheck` Protocol + `LeaseCheckResult` NamedTuple** in
  `loom.contracts`. `RoomConfig.lease_checks: tuple[LeaseCheck, ...]`
  defaults to the empty tuple ("use the kernel's built-in 8-step
  chain"); passing a non-empty tuple lets advanced consumers prepend,
  append, or replace gates. The eight default checks
  (`open_turn` → `participant_registered` →
  `participant_active` → `allowed_speaker` →
  `per_participant_cap` → `max_responses` →
  `throttle` → `budget`) ship as `DEFAULT_LEASE_CHECKS` in
  `loom.kernel.coordinator`. Behavior is identical to v0.1.2 —
  rejections were previously silent ``return None``.
- **`lease_denied` control event**. Every rejected
  `acquire_lease` call now emits this event with
  `holder`, `check_name`, `deny_reason`, and `trigger_event_id`.
  Default ``deny_reason`` strings: `"no_open_user_turn"`,
  `"unknown_participant"`, `"participant_inactive"`,
  `"not_in_allowed_speakers"`, `"no_obligation"`,
  `"speaker_cap_reached"`, `"max_responses_reached"`,
  `"throttle_exceeded"`, `"budget_exceeded"`. Buggy custom checks
  that raise emit `"check_raised:<ExceptionClass>"`.
- **`ConversationPolicy.dead_letter_target(state, removed_participant) ->
  pid | None`** hook. Called when a participant is removed mid-turn
  and outstanding @-mentions need a fallback. The default
  implementation preserves v0.1.2 kernel behavior
  (configured ``default_responder_id`` → cheapest active capable).
  Returning ``None`` emits the ``dead_letter`` event with
  ``reroute_to=None``, dropping the mention. Buggy hook
  implementations that raise fall back to the kernel default. To
  wire a custom policy onto this code path, construct
  ``RoomCoordinator(..., policy=my_policy)`` — the runtime layer
  does this automatically.
- **`ConversationPolicy.charter_text(state_view) -> str`** hook
  rendered in the system preamble immediately after the kernel charter
  (`LOOM_PROTOCOL_INSTRUCTIONS`) and BEFORE persona / participant id /
  topic. Default emits a one-line round-robin advisory when
  ``state.control.turn_order`` is non-empty. Use to describe
  policy-specific behavioral rules that should sit alongside the
  protocol-level rules. The kernel charter is unconditional — policy
  text cannot precede or replace it (preserving invariant 5).
- **`SecretShape` Protocol + `register_secret_shape()` API** in
  `loom.kernel.events`. Each of the seven default secret detectors
  (OpenAI/Anthropic `sk-` and `sk-ant-`, Bearer, AWS access key,
  JWT, GCP `AIza`, GCP `ya29` OAuth) is now a named
  `SecretShape` object (`_RegexShape`) with a `.detect(text) ->
  Iterable[(start, end)]` method. Adapters can register new
  shape-based detectors without monkey-patching the kernel; the
  legacy `register_secret_scrubber` callable API continues to work
  unchanged and runs AFTER all `SecretShape` detectors.

### Security
- **`MessageBus.post_internal` now requires a `_KERNEL_AUTH` token**
  (`auth=` keyword-only). The token is a module-private sentinel
  defined in `loom.kernel.bus` and is never re-exported from
  `loom`. Identity is checked at the call boundary so a separately
  constructed `_KernelAuth()` does not unlock the method. This
  promotes the previously convention-based "kernel-internal callers
  only" rule into a structural guarantee — the kernel/policy import
  boundary already prevents policy code from reaching the token. A
  new boundary test asserts that no `loom.policy.*` module
  references `_KERNEL_AUTH` or `_KernelAuth`.

### Changed
- **Bus subscriber fan-out runs OUTSIDE the bus lock**. The append +
  `notify_all` are still protected (preserves `ev.id == position`),
  but each subscriber callback runs after the lock is released so a
  slow subscriber cannot freeze readers (`snapshot`, `get`, `len`)
  or other writers waiting on the lock. Subscribers see a snapshot
  of the subscribers tuple captured at lock-release time, so
  concurrent subscribe/unsubscribe is safe. Across-subscriber
  ordering is relaxed: a subscriber may observe event N before
  another subscriber observes event N-1, but each subscriber still
  sees events in append order.
- **`RoomStateView.participants`** now yields `ParticipantInfoView`
  instances (`@dataclass(frozen=True)` mirroring `ParticipantInfo`'s
  five fields, with `role_hints` wrapped in `MappingProxyType`). The
  previously documented soft leak — a policy capturing a participant
  entry and writing `info.active = False` — now raises
  `FrozenInstanceError`.
- **`RoomState.view()` participants are snapshotted at call time**.
  Prior versions exposed a live `MappingProxyType` over the
  underlying dict; now each entry is materialized as a frozen
  `ParticipantInfoView` at the moment `view()` is called. Adding or
  removing a participant after `view()` is no longer visible through
  that view — callers must invoke `view()` again to see new
  membership. Top-level scalar fields (`room_epoch`, slot ids) were
  already snapshot fields on the frozen `RoomStateView`.

## [0.1.2] — 2026-05-08

First public release.

### Added
- **`LoomRoom`** facade: `post`, `post_and_wait`, `add_agent`,
  `remove_agent`, `run_console`, context-manager start/stop.
- **Bundled policies**: `DefaultPolicy` (floor-aware classifier with
  vocative + game-start detection), `OpenChatPolicy`,
  `SingleResponderPolicy`, `RoundRobinPolicy`.
- **Adapters**: `agent_from_send`, `agent_from_stream`,
  `agent_from_object` for wrapping ordinary callables and clients into
  the `Agent` protocol.
- **Streaming kernel**: bus, coordinator, leases, obligations,
  watchdog, prompt sandbox, throttle, budget primitives.
- **Journal**: append-only `events.jsonl` + advisory
  `room_state.json` snapshot for audit + tooling-grade replay.
- **`max_responses` race fix**: enforced at lease-grant time
  (counts committed drafts plus outstanding valid leases).
- **Dead-letter rerouting**: transfers required obligations to a
  fallback agent (default responder, else cheapest active capable).
- **Tests**: 1170+ tests covering kernel, policies, adapters, race
  conditions, threading, journal, and the policy-purity boundary.

### Known limitations
- No async / off-lock policies.
- No policy state persistence across restart.
- No automatic restart-recovery wiring from the journal.
- `RoomStateView` is shallow at the leaf level. *(Closed in [Unreleased] via `ParticipantInfoView`.)*
- No standalone PyPI package — install from source.

[0.1.2]: https://github.com/hdubey-debug/loom/releases/tag/v0.1.2
