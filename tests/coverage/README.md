# tests/coverage/

Targeted tests that cover defensive code paths (OSError handlers,
recursive callback guards, rare state transitions, slash-command
branches) which the unit / subsystem / system tiers don't reach.

Each test docstring names the **specific uncovered line range** it
targets. The collective effect drives `coverage report --fail-under`
from 89 % (line+branch) up to ≥ 98 %.

Files:

- `test_journal_oserror_paths.py` — OSError on fsync / replace / write,
  recursive-callback guard, blank-line tolerance, restore_state edge
  cases.
- `test_actor_recursive_failure.py` — actor_error post failure,
  pending direct-mention replay, lookup_event linear-scan branch.
- `test_coordinator_rare_states.py` — participant removal mid-turn,
  slot changes mid-turn, lease invalidation cascade, obligation
  reroute edge cases.
- `test_runtime_console_branches.py` — slash-command branches that
  don't get exercised by the four bundled policies' happy paths,
  `run_loom_console` driven by injected `prompt_fn` (the genuine
  `input()` calls are `# pragma: no cover`).
- `test_user_turn_pathologicals.py` — obligation closure boundary
  cases (0, 1, max obligations).
- `test_default_policy_branches.py` — eligibility × role × floor
  combinatorial coverage of `loom/policy/default.py`.

Run alone:

    ./venv/bin/pytest tests/coverage/ -q
