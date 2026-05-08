# Mutation survivors — triage notes

This file documents every surviving mutant from the latest full
mutation run, classified into one of three buckets:

- **test gap** → a new test should kill this mutant.
- **equivalent** → the mutant produces semantically identical behavior;
  marked `# pragma: no mutate` upstream or skipped via mutmut's filter.
- **intentional** → the original behavior is permissive on purpose;
  documented here with the rationale.

When a new full baseline is captured, this file should be updated.
`scripts/run_full_quality.sh` fails on any *new* survivor not listed
below.

---

## Pilot run — `loom/policy/open_chat.py` (2026-05-08)

22 surviving mutants out of 39 generated — 43.6% kill rate. The full
list is in `mutmut show <id>`. Representative survivors:

- **mutmut_7, _8, _9** — boolean-flip mutations in the routing
  branches. Likely killable by adding tests that assert specific
  obligation levels for each branch.
- **mutmut_16–_21** — string-literal and integer-constant mutations.
  Probably equivalents (same observable output).

Triage status: **DEFERRED** to first full baseline run. The pilot
exists to validate the harness; the assertion-strengthening pass is
expected to run alongside the first full baseline.

## Full baseline — pending

(To be filled in once `make test-full` runs the full kernel + policy
baseline.)
