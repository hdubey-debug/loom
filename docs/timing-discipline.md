# Timing discipline in the Loom kernel

**Audience**: kernel contributors and reviewers.
**Status**: enforced by `tests/test_kernel_kernel_boundary.py::ClockDisciplineBoundary` (v0.2.1 PR 5; audit findings B1, B2).

## The rule

The kernel uses two clocks with non-overlapping responsibilities:

1. **`time.monotonic()`** is the only admissible clock for
   **duration / TTL / debounce / window** math. Lease TTLs, idle
   timeouts, debounce intervals, throttle windows, policy-watchdog
   elapsed-ms measurements — all use `time.monotonic()`.

2. **`time.time()`** (wall-clock, epoch seconds) is reserved for
   exactly one site: the `Event.ts` assignment in `MessageBus.post`
   at `loom/kernel/bus.py:281`. That field is for journal correlation
   and human-readable rendering; it is never compared against a
   duration.

## Why

NTP can step the wall clock — forward or backward, by seconds or
minutes — without warning. Any TTL or duration computed via
`time.time()` is then either prematurely expired (wall clock jumped
forward) or eternally valid (jumped back). `time.monotonic()` is by
contract immune to such jumps; it can only advance.

The replay path (`Journal.replay_into`, `loom/kernel/journal.py:606-633`)
re-emits events with their original wall-clock timestamps from
`events.jsonl`. No real-time clock call appears in that path. This is
what makes replay deterministic: state derives only from event
content + ordering, not from "when replay happened to run".

## How to extend

- Adding a new TTL / debounce / window in the kernel: use
  `time.monotonic()`. The boundary test will reject any
  `time.time()` call you sneak in.
- Adding a new event metadata field that needs wall-clock semantics:
  derive it from `Event.ts` (which `MessageBus.post` already
  populates), not from a fresh `time.time()` call. If you must
  introduce a second wall-clock site, extend the whitelist in
  `tests/test_kernel_kernel_boundary.py::ClockDisciplineBoundary._WHITELISTED_TIME_TIME_FILE`
  and document the new site here.
- Adding any time call inside `loom/kernel/journal.py`: don't. The
  replay path's clock-agnostic property is load-bearing for the v0.3
  doctrine's P6 (event-sourced replay). If you have a legitimate
  reason — design it as an event content field instead, populated
  upstream of the journal.

## Related

- v0.2.1 hardening audit: `docs/internal/study/12-v02-hardening-audit.md` §5 (Area B).
- v0.3 doctrine: `docs/internal/study/11-orchestration-os-doctrine.md` §timing-discipline + P6 (event-sourced replay).
- Existing inline comments anchoring the rule: `loom/kernel/coordinator.py:101-107`, `loom/kernel/coordinator.py:1222-1223`, `loom/kernel/bus.py:281` (event-ts comment in the dataclass at `loom/kernel/events.py:302-308`).
