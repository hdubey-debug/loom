# Changelog

All notable changes to **Loom** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- `RoomStateView` is shallow at the leaf level.
- No standalone PyPI package — install from source.

[0.1.2]: https://github.com/hdubey-debug/loom/releases/tag/v0.1.2
