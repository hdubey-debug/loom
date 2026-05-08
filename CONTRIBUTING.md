# Contributing to Loom

Thanks for your interest in Loom. This guide gets you set up and
points at the parts of the repo that newcomers usually need.

## Setup

```bash
git clone https://github.com/hdubey-debug/loom.git
cd loom
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
```

Python `>=3.11` is required.

## Running the test suite

```bash
pytest -q                  # full suite (1100+ tests, runs in ~30s)
pytest tests/policy        # one subtree
pytest -k race             # name filter
```

The default suite excludes `perf` (microbench) tests; run them with
`pytest -m perf` if you're chasing a regression.

## Lint + type-check

```bash
ruff check loom tests
ruff format --check loom tests
mypy loom
```

CI runs these on push and PR.

## Performance + mutation baselines

Heavy benchmarks live in `bench/` and `benchmarks/`:

```bash
make bench         # perf harness
make mutation      # mutmut suite (long-running)
```

Baselines are tracked in `docs/internal/`.

## Where to start in the code

- **`loom/__init__.py`** — public API surface.
- **`loom/room.py`** — `LoomRoom` facade.
- **`loom/kernel/`** — bus, coordinator, state, obligations.
- **`loom/policy/`** — bundled policies. `single_responder.py` is the
  canonical reference for new policy authors.

## Writing a new policy

Read [`docs/writing-a-policy.md`](docs/writing-a-policy.md). The
contract:

- `plan_user_turn` must be **synchronous, deterministic, fast**
  (<10ms typical). The kernel holds its lock across this call.
- Policies must not mutate `RoomState` and must not post to the bus —
  both are the kernel's responsibility. Declarative requests go on the
  returned `UserTurnPlan`. A boundary test enforces this with a
  static grep.

## Writing a new adapter

Read [`docs/writing-an-adapter.md`](docs/writing-an-adapter.md). The
`Agent` protocol is structural: any object with `id: str` and
`stream(prompt) -> Iterator[str]` qualifies.

## Pull requests

- Keep PRs focused. One feature or one fix per PR.
- Add tests. The bar is "if your change broke and the tests still
  passed, file a bug on yourself." 
- Run `pytest -q` and `ruff check loom tests` locally before pushing.
- Update `CHANGELOG.md` under an `## [Unreleased]` heading.

## Code of conduct

This project follows the
[Contributor Covenant v2.1](CODE_OF_CONDUCT.md).
