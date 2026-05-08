#!/usr/bin/env bash
# Release-rhythm quality check. Estimated 1.5–2.5h.
#
# Runs:
#   1. Fast suite + coverage gate (run_tests.sh)
#   2. 10x repeated runs of unit + subsystem (flake detection)
#   3. mutmut against loom/kernel/, loom/policy/, loom/adapters.py
#
# Mutation results are written to docs/mutation-baseline.txt.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== [1/3] Fast suite + coverage gate ==="
./scripts/run_tests.sh

echo "=== [2/3] Repeated runs (10x, no stress) ==="
COVERAGE_FILE=/tmp/.coverage_loom ./venv/bin/pytest \
    -p no:cacheprovider \
    --count=10 -m 'not stress and not breakpoint and not perf' \
    tests/ tests/subsystem/ -q

echo "=== [3/3] Mutation testing (estimated 1.5–2h) ==="
mkdir -p docs
MUTMUT_CACHE_DIR=/tmp/.mutmut ./venv/bin/mutmut run
./venv/bin/mutmut results | tee docs/mutation-baseline.txt
echo
echo "Captured mutation baseline → docs/mutation-baseline.txt"
