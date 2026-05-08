PYTEST = ./venv/bin/pytest -p no:cacheprovider
PYTHON = ./venv/bin/python
BASELINE_JSON = docs/perf-baseline.json

.PHONY: help test test-quick test-property test-full lint mutation-show coverage-html bench bench-quick bench-micro bench-diff bench-soak bench-baseline security-test security-bench ux-check

help:                ## list targets
	@grep -hE '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?##"}; {printf "  %-20s %s\n", $$1, $$2}'

test:                ## fast suite + coverage gate
	./scripts/run_tests.sh

test-quick:          ## unit only (no perf), for inner loop
	$(PYTEST) -x -q -m "not perf" tests/

test-property:       ## just the Hypothesis tier
	$(PYTEST) -q tests/property/

test-coverage:       ## just the targeted coverage tier
	$(PYTEST) -q tests/coverage/

test-full:           ## fast + repeated + mutation (~2h)
	./scripts/run_full_quality.sh

lint:                ## ruff + mypy
	./venv/bin/ruff check loom/ tests/
	./venv/bin/mypy --cache-dir=/tmp/mypy_cache loom/

coverage-html:       ## render HTML coverage report under /tmp/coverage_html
	COVERAGE_FILE=/tmp/.coverage_loom ./venv/bin/coverage html -d /tmp/coverage_html
	@echo "Open /tmp/coverage_html/index.html"

mutation-show:       ## show last mutmut run results
	./venv/bin/mutmut results

bench:               ## run microbench tier + full scenario suite (~3-5 min)
	$(PYTEST) -q -m perf tests/perf/
	$(PYTHON) -m benchmarks.perf --output /tmp/perf-current.json
	@echo "Wrote /tmp/perf-current.json — diff against committed baseline:"
	@echo "  make bench-diff"

bench-quick:         ## quick scenario suite (~30 s, smaller axes)
	$(PYTHON) -m benchmarks.perf --quick --output /tmp/perf-current-quick.json

bench-micro:         ## just the pytest microbench tier
	$(PYTEST) -q -m perf tests/perf/

bench-diff:          ## diff /tmp/perf-current.json against committed baseline (CI gate)
	$(PYTHON) scripts/bench_diff.py $(BASELINE_JSON) /tmp/perf-current.json

bench-baseline:      ## capture a fresh baseline at docs/perf-baseline.{json,md} (commit the result)
	$(PYTHON) -m benchmarks.perf --output $(BASELINE_JSON)
	@echo "Captured baseline → $(BASELINE_JSON) (and .md)"
	@echo "Review and commit:  git add $(BASELINE_JSON) docs/perf-baseline.md"

bench-soak:          ## reliability / long-run workloads (~1h)
	$(PYTEST) -q -m perf tests/perf/ -k soak

security-test:       ## run the security property + fuzz suite (~30 s)
	$(PYTEST) -q tests/property/test_security_fuzz.py \
	             tests/property/test_prompt_fence_fuzz.py \
	             tests/property/test_capability_invariants.py \
	             tests/property/test_event_meta_no_render.py

security-bench:      ## opt-in adversarial scenarios (~1 min)
	$(PYTEST) -q bench/adversarial/ -v

ux-check:            ## UX contract tests + public-symbol count
	$(PYTEST) -q tests/property/test_ux_contracts.py
	@echo "--- public-symbol count (loom.__all__) ---"
	@$(PYTHON) -c "import loom; print(f'  primary surface: {len(loom.__all__)} symbols (target: <= 20)')"
