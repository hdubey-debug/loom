# Loom kernel — security model

This document is the in-repo reference for the kernel's security
posture. The full audit, threat-model brainstorm, and remediation
roadmap live in
`~/.claude/plans/virtual-cuddling-lantern.md`; this file is the short,
authoritative summary that ships with the code.

The audit was conducted post-perf-pass on 2026-05-08; the file
references in this document target the kernel state at that time.

## What the kernel defends against

The kernel takes responsibility for the following adversaries:

- **Untrusted LLM-generated content.** Chat bodies and addressee
  strings are walled at the transcript layer (kernel charter +
  whitelist parser at `loom/kernel/addressees.py`).
- **Tampered or corrupt journal lines.** `Event.from_jsonl` validates
  per-kind body shape and raises `EventShapeError` on mismatch; the
  replay path surfaces `journal_corruption` / `journal_truncated`
  control events instead of silently dropping the lines (T1, P0.1 /
  P0.2 / P0.4).
- **Tampered snapshot files.** `restore_state` defensively coerces
  scalar fields (`_coerce_int`, `_coerce_str_or_none`) and skips
  malformed participant entries (T2, P0.3).
- **Filesystem-level confidentiality leaks.** `events.jsonl` and
  `room_state.json` are created with mode `0o600`; `session_dir` is
  created with mode `0o700` (T6, P0.6). Existing files with looser
  perms produce a `warnings.warn` but are not chmod'd (operator
  intent is preserved).
- **Secret leakage via error events.** `redact_error_text` is applied
  at the kernel boundary in `stream_end`, `actor_error`, and
  `journal_error` so provider exception strings (`sk-…`, `Bearer …`,
  `AKIA…`, `eyJ…JWT`, `AIza…`, `ya29.…`) are scrubbed before they
  reach the journal or any subscriber (OBS1, P0.7). Adapters can
  install provider-specific scrubbers via `register_secret_scrubber`.
- **Prompt-injection on non-transcript surfaces.** `topic`, `persona`,
  `capability_block`, and `active_goal` go through
  `prompt._render_system_field`, which fences each value in
  `<name>...</name>` and neutralizes the closing tag and our protocol
  section markers `<<<...>>>` (PI1, PI2, P0.8). The kernel charter
  tells the LLM to treat tag-fenced fields as data, not instructions.

## What the kernel does NOT defend against (deployment owns these)

- **API key acquisition, storage, use, rotation, and encryption at
  rest.** Adapter and deployment concern. The kernel owns the audit
  trail (where keys could leak) but never holds the keys themselves.
- **Network attacks.** The kernel has no network surface. Adapters
  (`loom/adapters/*`) are responsible for their own TLS, auth, and
  request/response handling.
- **Encryption at rest.** `events.jsonl` is plaintext on disk.
  Deploy LUKS / dm-crypt / application-level field encryption if
  the threat model requires it.
- **Multi-tenant isolation.** v0 is single-room-per-process.
- **Compromised LLM weights.** Upstream / model-supplier concern.
- **Python-runtime exploits / sandbox escape.** An attacker with
  arbitrary Python in-process defeats any in-process defense.
- **Hardware / firmware threats.**
- **Supply-chain compromise of dependencies or Python interpreter.**

## API-key layering

| Layer | Owns | Does NOT own |
|-------|------|--------------|
| **Adapter** | Key acquisition, in-process storage, use against provider, provider-specific exception scrubbing before raising | The audit trail of leaks that get past it |
| **Kernel** | Error-body length cap + redaction (`redact_error_text`); journal file permissions; body-size caps; "tag-fenced fields are data" framing | Raw key material; key rotation; encryption-at-rest |
| **Deployment** | Disk encryption; secret-store integration; key rotation; access control on the session dir | The in-process audit trail |

Each layer has non-overlapping responsibility. A hole in any one
leaks. The kernel-owned holes that this audit closed are: error-text
passthrough (OBS1 / P0.7), journal file permissions (T6 / P0.6), and
the prompt-construction non-transcript surfaces (PI1 / P0.8).

## Trust contract for callers

These are the rules the kernel relies on. A caller that violates them
breaks security invariants the kernel cannot enforce on its own.

- **`MessageBus.post` authenticates `sender` against thread-local
  actor binding (P1, Option B).** When a thread calls
  `bus.bind_actor(actor_id)` (which `ParticipantActor._loop` does
  automatically), any `bus.post(ev)` from that thread requires
  `ev.sender == actor_id`; mismatches raise `SenderMismatchError`.
  Privileged kernel callers (coordinator, runtime, journal replay,
  the actor's own `actor_error` emission) bypass via
  `bus.post_internal`. Unbound threads (test code, the runtime
  thread) skip the check entirely.
- **Subscribers run synchronously, inline, under the bus lock.** A
  subscriber that blocks for `N` ms blocks every actor in the room
  for `N` ms. Subscribers MUST complete in milliseconds; long-running
  work belongs on a background thread (the journal's snapshot writer
  is the canonical example). A misbehaving subscriber's exception is
  swallowed; a misbehaving subscriber's *latency* is not.
- **Replay re-injects events without re-authentication.** `sender`,
  `channel`, `body` shape (after the P0.1 validation, which catches
  type confusion) are not re-checked at replay time. A disk-write
  attacker who can edit `events.jsonl` between sessions can pre-stage
  any room state (R1). This collapses to "treat journal disk as
  sensitive" — kernel responsibilities here are file permissions
  (P0.6) and parse safety (P0.1 / P0.2). Disk integrity (hash chain)
  is a v0.1 hardening item.
- **System-prompt non-transcript surfaces are kernel-fenced, not
  caller-sanitized.** `prompt._render_system_field` fences values; it
  does NOT validate them. Slash-command handlers in `loom/runtime.py`
  may add an upstream length-cap / forbidden-char-set sanitization
  layer, but the kernel boundary alone is enough to prevent
  break-out from the system-prompt surface into LLM-instruction
  injection.

## Implemented hardening (P0 + P1 + P2 + P3 + P4)

The audit's P0–P4 items are now in code:

- **P0 (crash safety + secret leak + perms).** Per-kind shape
  validation in `Event.from_jsonl`; corruption / truncation events
  in `Journal.iter_events`; type-coerced `restore_state`; journal
  file mode `0o600`, dir mode `0o700`; secret-redaction helper
  (`events.redact_error_text`) wired at `stream_end`,
  `actor_error`, `journal_error`; tag-fenced
  `prompt._render_system_field`; this doc + module docstrings.
- **P1 (sender authentication).** Thread-local actor binding;
  `bus.post` rejects forgery; `bus.post_internal` is the
  documented privileged bypass for coordinator / runtime / replay /
  failure callbacks.
- **P2 (resource limits).** `MessageBus(max_body_bytes=256 KB)`
  cap; bounded snapshot queue with `snapshot_dropped` event on
  overflow.
- **P3 (defense in depth).** `runtime.post_user_text` channel
  regex; `expected_holder` param on `_resolve_obligation_locked`;
  lease bookkeeping migrated to `time.monotonic()`.
- **P4 (property + adversarial tests).**
  `tests/property/test_security_fuzz.py`,
  `test_capability_invariants.py`,
  `test_event_meta_no_render.py`,
  `test_prompt_fence_fuzz.py`; `bench/adversarial/test_large_body.py`,
  `test_tampered_replay.py`. Run via `make security-test` and
  `make security-bench`.

## Deferred / future hardening

- **Off-thread subscriber dispatch with timeout** (audit CON1 /
  P2.5 partial). The contract is documented; the actual
  off-thread implementation is a v0.1 work item.
- **Hash chain over the journal** (audit P3 / R1). Defense in depth
  for journal-disk integrity beyond per-line shape validation.
- **Stream-delta batching** (audit RES6 / P2.4). Perf-plan item;
  reframed here as DoS resistance.
- **Encryption at rest.** Deployment concern (LUKS / dm-crypt /
  application-level field encryption).
- **`max_participants` cap** (audit RES5). Soft warn/refuse at 64.
- **HMAC-signed events** (audit Option C from §6). Heavyweight;
  only relevant when Loom runs across processes / hosts.

## Phase 0 audit findings (post-kernel-pass full-surface review)

The kernel-only audit was followed by a Phase 0 full-surface sweep
(`loom/runtime.py`, slash-command handlers, adapters, policy /
contracts). Everything actionable has been remediated; what remains
is documentation of what's verified safe vs. what's deferred:

**Resolved in code:**

- **HIGH — `plan.instruction` was unfenced in the TURN CARD**
  (`prompt.py`). Fixed by routing through `_render_system_field("instruction", ...)`
  and adding `<instruction>` to the kernel charter's fenced-field
  list. Custom policies that derive `instruction` from user text
  cannot inject system-level directives.
- **MED — `/topic` and `/goal` slash commands had no length cap**
  (`runtime.py`). Each now rejects arguments exceeding 500 chars at
  the entry, before any kernel call. Defense is layered: the kernel
  fence prevents injection; the cap prevents prompt-bloat / memory
  DoS from a multi-MB paste.
- **MED — `build_loom_session(default_responder_id=, anchor_id=)`
  silently accepted unknown participant ids** (`runtime.py`). Now
  raises `ValueError` with the known-ids list when the id is not
  registered.

**Verified safe (no remediation needed):**

- Adapter exception path is properly redacted: provider exceptions
  flow through `streaming.py:run_streaming_call`'s `except Exception`,
  feed into `stream_end(error=...)`, which the factory pushes through
  `redact_error_text` at the kernel boundary. The default redaction
  patterns cover all bundled-adapter key shapes.
- Persona and capability_block cannot be mutated at runtime — only
  set at wiring time (`runtime.py`).
- Channel argument validation (`_VALID_CHANNEL_RE`) covers
  `post_user_text`. `/dm` constructs `dm:{target}` only after
  verifying `target` is a registered participant.
- `/anchor` and `/responder` slash handlers verify the argument is
  a known participant before calling the coordinator.
- `/roles` filters unknown participant ids before applying.
- `RoomStateView` is genuinely read-only via `MappingProxyType`
  + `frozen=True` dataclass. Soft leak through mutable `ParticipantInfo`
  values is documented and acceptable for v0.

**Deferred to v0.1 (documented, not yet remediated):**

- **D3 — public `bus` / `coordinator` / `journal` attributes on
  `LoomSession`.** Today: yes (acknowledged in this doc). If
  in-process isolation is needed, rename to `_bus` / `_coordinator`
  / `_journal` and add facade methods. v0 keeps them public to
  unblock external integrations (Loom, examples, tests).
- Adapter-specific scrubber registration. The kernel default
  patterns cover known shapes; provider-specific scrubbers via
  `register_secret_scrubber` are an opt-in defense in depth that
  no adapter has installed yet.

## Cross-references

- `loom/kernel/events.py` — `EventShapeError`, `redact_error_text`,
  `register_secret_scrubber`, factory functions for `journal_error` /
  `actor_error` / `journal_corruption` / `journal_truncated`.
- `loom/kernel/journal.py` — file-permission code at `__init__`,
  `open`, `_write_snapshot_dict`; corruption surfacing in
  `iter_events` / `replay_into`; defensive guards in `restore_state`.
- `loom/kernel/prompt.py` — `_render_system_field`,
  `_escape_system_value`, kernel charter
  (`LOOM_PROTOCOL_INSTRUCTIONS`).
- `tests/property/test_security_fuzz.py` — Hypothesis fuzz coverage
  of `Event.from_jsonl`, `restore_state`, `parse_addressees`,
  `iter_events`, `redact_error_text`.
- `tests/property/test_prompt_fence_fuzz.py` — Hypothesis fuzz
  coverage of `_render_system_field`'s fence integrity.
