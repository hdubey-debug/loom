"""Loom UX contracts — drift guards that fail CI.

These are not unit tests of feature behavior; they're tripwires for
the *naming, layering, and timing* contracts the audit (P3.5) wired
in. A passing change to feature code that breaks one of these means
something has drifted from the design philosophy in
``docs/loom-ux-spec.md``.

Each test is intentionally narrow: one drift class per test, with a
short inline rationale.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Source-tree paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOOM_DIR = _REPO_ROOT / "loom"


def _iter_python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


# ---------------------------------------------------------------------------
# Naming canonicalization (audit F4.1 / §5.2)
# ---------------------------------------------------------------------------


def test_actor_id_appears_only_in_kernel_actor_module():
    """``actor_id`` is reserved for the per-thread runtime in
    ``loom/kernel/actor.py``. User-facing surfaces use ``participant_id``.
    """
    offenders: list[str] = []
    for path in _iter_python_files(_LOOM_DIR):
        rel = path.relative_to(_LOOM_DIR)
        # Allow the threading-runtime module itself.
        if rel.as_posix() == "kernel/actor.py":
            continue
        # The bus's bind_actor seam takes an actor_id parameter — that is
        # a kernel-internal threading binding, not a user-facing API.
        if rel.as_posix() == "kernel/bus.py":
            continue
        # The prompt module's internal helpers use actor_id as the
        # rendering target id; the *public* policy methods (system_prompt
        # / role_prompt) in this module use participant_id (audit P2.4).
        # Skip the file as a whole because the internal helpers are
        # documented as kernel-internal in the module docstring.
        if rel.as_posix() == "kernel/prompt.py":
            continue
        text = path.read_text(encoding="utf-8")
        in_docstring = False
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # Track triple-quoted docstring blocks so legitimate prose
            # references (deprecation notices, history mentions) do
            # not trip the guard.
            triple_count = stripped.count('"""') + stripped.count("'''")
            if triple_count % 2 == 1:
                in_docstring = not in_docstring
                continue
            if in_docstring or stripped.startswith("#"):
                continue
            # Skip lines that contain ``actor_id`` only inside a
            # backtick-quoted reference (prose) — these are common in
            # comments left to flag a name change.
            if re.search(r"\bactor_id\b", line) and not re.search(r"`+actor_id`+", line):
                offenders.append(f"{rel}:{line_no}: {stripped}")
    assert not offenders, (
        "``actor_id`` leaked into a user-facing surface — rename to "
        "``participant_id``:\n  " + "\n  ".join(offenders)
    )


def test_active_goal_field_removed_from_state():
    """P2.3 merged ``RoomControlState.active_goal`` into ``state.topic``;
    the field must not reappear under any name on either struct.
    """
    from loom.kernel.room import RoomControlState, RoomState

    assert not hasattr(RoomControlState(), "active_goal")
    # ``RoomState.topic`` is the canonical merged location.
    state_fields = {f.name for f in RoomState.__dataclass_fields__.values()}
    assert "topic" in state_fields
    assert "active_goal" not in state_fields


# ---------------------------------------------------------------------------
# Public/private boundary (audit Phase 0.3)
# ---------------------------------------------------------------------------


def test_no_loom_kernel_imports_in_public_examples():
    """Bundled examples import only from ``loom.*`` / ``loom.policy.*`` /
    ``loom.adapters.*``.

    ``loom.kernel.*`` is the kernel-private namespace; runnable examples
    that reach into it train downstream authors to do the same. Docs
    (``docs/*.md``) MAY reference kernel internals when explicitly
    walking through advanced surfaces — that case is not in scope here.
    """
    examples_dir = _REPO_ROOT / "examples"
    if not examples_dir.exists():
        pytest.skip("no examples/ directory yet")
    pattern = re.compile(r"\bfrom\s+loom\.kernel|\bimport\s+loom\.kernel")
    offenders: list[str] = []
    for path in examples_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, "examples/ MUST NOT import from loom.kernel.*:\n  " + "\n  ".join(
        offenders
    )


# ---------------------------------------------------------------------------
# Time-handling (audit F4.5 / P2.5)
# ---------------------------------------------------------------------------


def test_no_new_time_time_in_kernel_duration_math():
    """Duration math sites use ``time.monotonic()``.

    ``time.time()`` is allowed only at the bus where ``Event.ts`` is
    documented as a wall-clock for replay / journal display.
    """
    allowed_files = {
        "kernel/bus.py",  # Event.ts wall-clock — replay only.
        "kernel/events.py",  # documents Event.ts wall-clock.
    }
    pattern = re.compile(r"\btime\.time\s*\(\s*\)")
    offenders: list[str] = []
    for path in _iter_python_files(_LOOM_DIR / "kernel"):
        rel = path.relative_to(_LOOM_DIR).as_posix()
        if rel in allowed_files:
            continue
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line) and "monotonic" not in line:
                offenders.append(f"{rel}:{line_no}: {line.strip()}")
    assert not offenders, (
        "kernel duration-math sites must use time.monotonic(); "
        "time.time() is wall-clock and unsafe under clock steps:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Optional[X] vs X | None consistency (audit F4.8)
# ---------------------------------------------------------------------------


def test_optional_typehint_consistency():
    """The codebase uses ``Optional[X]`` exclusively (audit F4.8).

    Mixing ``X | None`` with ``Optional[X]`` is harmless but a churn
    signal; this guard is an early warning that a contributor has
    chosen a different idiom.
    """
    pattern_or_none = re.compile(r":\s*[A-Za-z_][\w\[\], ]*\s*\|\s*None\b")
    offenders: list[str] = []
    for path in _iter_python_files(_LOOM_DIR):
        rel = path.relative_to(_LOOM_DIR).as_posix()
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if pattern_or_none.search(line):
                offenders.append(f"{rel}:{line_no}: {stripped}")
    assert not offenders, (
        "Codebase convention is Optional[X]; do not mix with X | None:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Public API discoverability (audit §3.6)
# ---------------------------------------------------------------------------


def test_loomroom_public_methods_have_docstrings():
    """Every public method on ``LoomRoom`` must have a docstring.

    The audit's discoverability principle (§3.6): ``help(room)`` should
    answer ~80% of "what can I do?" without grepping the kernel.
    """
    from loom.room import LoomRoom

    missing: list[str] = []
    for name in dir(LoomRoom):
        if name.startswith("_"):
            continue
        attr = getattr(LoomRoom, name)
        if not callable(attr):
            continue
        if getattr(attr, "__doc__", None) in (None, ""):
            missing.append(name)
    assert not missing, "LoomRoom public methods missing docstrings (audit §3.6): " + ", ".join(
        missing
    )


# ---------------------------------------------------------------------------
# Agent Protocol structural conformance (audit F3.2)
# ---------------------------------------------------------------------------


def test_bundled_adapter_factories_produce_runtime_checkable_agents():
    """``isinstance(a, Agent)`` passes for every bundled adapter factory.

    The Protocol is decorated ``@runtime_checkable``; any drift in the
    concrete adapter shape (a missing ``id`` / ``stream`` attribute)
    would fail this guard.
    """
    from loom import (
        Agent,
        agent_from_object,
        agent_from_send,
        agent_from_stream,
    )

    a1 = agent_from_send("a", lambda p: "ok")
    a2 = agent_from_stream("b", lambda p: iter(["o", "k"]))

    class _Obj:
        id = "c"

        def stream(self, prompt):
            yield "ok"

    a3 = agent_from_object("c", _Obj())

    for a in (a1, a2, a3):
        assert isinstance(a, Agent), f"adapter {a!r} does not satisfy the Agent Protocol"


# ---------------------------------------------------------------------------
# Routing-case taxonomy (audit P2.6)
# ---------------------------------------------------------------------------


def test_bundled_policies_emit_validated_routing_cases():
    """``UserTurnPlan.routing_case`` rejects unknown values at construction.

    Guards against a policy that introduces a new free-form string
    without first extending the :data:`RoutingCase` Literal +
    ``_VALID_ROUTING_CASES`` set.
    """
    from loom.kernel.obligations import (
        UserTurnPlan,
        _VALID_ROUTING_CASES,
        plan_with_required,
    )

    # Each documented case constructs successfully.
    for case in _VALID_ROUTING_CASES:
        if case == "none":
            UserTurnPlan(
                requires_response=False,
                routing_case=case,  # type: ignore[arg-type]
            )
        else:
            plan_with_required(
                ["alice"],
                routing_case=case,  # type: ignore[arg-type]
                target_event_ids=[1],
                reason=case,
            )

    # An unknown case is rejected at __post_init__ time.
    with pytest.raises(ValueError, match="unknown routing_case"):
        plan_with_required(
            ["alice"],
            routing_case="not_a_real_case",  # type: ignore[arg-type]
            target_event_ids=[1],
            reason="bogus",
        )
