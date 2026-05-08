# tests/property/

Hypothesis property-based tests for the Loom kernel. Each file targets
one invariant: a small set of `@given` tests that fuzz over generated
inputs to verify the property holds.

## Properties

| File | Invariant |
|---|---|
| `test_event_roundtrip.py` | `from_jsonl(to_jsonl(e)) == e` for every event kind. |
| `test_journal_replay.py` | Replay of `events.jsonl` is deterministic; tail truncation never crashes. |
| `test_bus_concurrent.py` | Concurrent posters produce strictly monotonic ids; subscribers see same order as the log. |
| `test_round_robin.py` | Round-robin pointer wraps cleanly under arbitrary participant add/remove sequences. |
| `test_lease_invariants.py` | At most one valid lease per participant; expired leases never validate; obligation counts balance. |
| `test_policy_plans.py` | Each policy returns plans where required ⊆ allowed_speakers, optional ⊆ allowed_speakers. |
| `test_throttle_fairness.py` | Throttle window pruning is correct at the 60s boundary; per-participant quota fair. |

## Running

Default `ci` profile (100 examples, 2s deadline) — built into the fast suite:

    ./venv/bin/pytest tests/property/ -q

Faster `fast` profile for the inner loop:

    HYPOTHESIS_PROFILE=fast ./venv/bin/pytest tests/property/ -q

Heavier `nightly` profile (2000 examples, 10s deadline):

    HYPOTHESIS_PROFILE=nightly ./venv/bin/pytest tests/property/ -q
