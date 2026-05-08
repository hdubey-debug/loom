#!/usr/bin/env bash
# Fast suite + coverage gate. Runs in ~70-90s on a quiescent box.
#
# What it covers:
#   - tests/                 (1328 unit)
#   - tests/subsystem/       (67 component-pair)
#   - tests/system/          (97 whole-kernel)
#   - tests/coverage/        (targeted defensive-path tests)
#   - tests/property/        (Hypothesis property-based, ci profile)
# Then enforces line+branch coverage gate at 98%.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTEST=./venv/bin/pytest
COV=./venv/bin/coverage

echo "=== Running fast suite ==="
# tests/perf/ is opt-in via the ``perf`` marker; the default suite
# excludes it so the coverage gate stays focused on correctness paths.
# To run benchmarks: ``make bench`` / ``make bench-micro``.
COVERAGE_FILE=/tmp/.coverage_loom $PYTEST \
    -p no:cacheprovider -m "not perf" \
    --cov=loom --cov-branch --cov-report=term-missing \
    tests/ tests/subsystem/ tests/system/ \
    tests/coverage/ tests/property/

echo "=== Coverage gate ==="
COVERAGE_FILE=/tmp/.coverage_loom $COV report --fail-under=98

echo "OK — fast suite passed coverage gate."
