"""Subsystem tests — architectural invariants under load.

Mirror of the boundary tests in ``tests/test_kernel_kernel_boundary.py``,
exercised at scale: many actors running, many concurrent prompt builds,
many concurrent classifier calls, repeated bus posts. These tests
verify the static contracts (kernel/policy import asymmetry, charter
always rendered first, read-only view immutability, single mutator)
still hold under runtime pressure — not just at module import time.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.prompt import LOOM_PROTOCOL_INSTRUCTIONS, build_prompt
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
)
from loom.policy.default import DefaultPolicy
from loom.policy.open_chat import OpenChatPolicy


_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOOM_KERNEL_DIR = _REPO_ROOT / "loom" / "kernel"
_LOOM_POLICY_DIR = _REPO_ROOT / "loom" / "policy"


def _python_files(directory: Path) -> list[Path]:
    return [p for p in directory.rglob("*.py")
            if "__pycache__" not in p.parts]


# ---------------------------------------------------------------------------
# Static asymmetry — the kernel never imports policy, even after
# many actors are running.
# ---------------------------------------------------------------------------

class TestImportAsymmetryAtScale:

    def test_kernel_does_not_import_policy_after_active_room(
            self, room_factory, simple_agents):
        # Spin a real room with many agents, then re-grep the kernel
        # source. The contract must still hold (no policy import has
        # been introduced as a quick fix during runtime stress).
        agents = simple_agents(20)
        room = room_factory(agents=agents, policy=OpenChatPolicy())
        # Drive a turn so all actors have actually been initialized.
        room.post_and_wait("ping", timeout=10.0)

        forbidden = re.compile(
            r"^\s*(?:from\s+loom\.policy(?:\.\w+)*\s+import|"
            r"import\s+loom\.policy(?:\.\w+)*)",
            re.MULTILINE,
        )
        offenders: list[str] = []
        for path in _python_files(_LOOM_KERNEL_DIR):
            text = path.read_text()
            if forbidden.search(text):
                offenders.append(str(path.relative_to(_REPO_ROOT)))
        assert offenders == [], offenders


# ---------------------------------------------------------------------------
# Read-only view under concurrent classification.
# ---------------------------------------------------------------------------

class TestReadOnlyViewUnderConcurrency:

    def test_view_top_level_remains_frozen_under_repeated_classification(
            self, thread_harness):
        # Many threads call ``state.view()`` and try to mutate the
        # returned frozen instance. Every attempt must raise.
        from dataclasses import FrozenInstanceError

        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="loom"))

        errors: list[str] = []

        def attacker():
            try:
                for _ in range(200):
                    view = state.view()
                    try:
                        view.room_epoch = 999  # type: ignore[misc]
                    except FrozenInstanceError:
                        continue
                    errors.append("view.room_epoch was assignable")
                    return
            except FrozenInstanceError:
                pass

        for _ in range(8):
            thread_harness.spawn(attacker, name="attacker")
        thread_harness.join_all(timeout=5.0)
        assert errors == []
        assert thread_harness.errors == []

    def test_view_reflects_membership_changes_atomically(self):
        # The participants mapping inside the view is a live
        # MappingProxyType, so post-view membership changes are visible
        # through it. ``room_epoch`` is captured at view() time (frozen
        # dataclass field) — re-call view() to see the new epoch.
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="loom"))
        v1 = state.view()
        e1 = v1.room_epoch
        state.add_participant(ParticipantInfo(id="newcomer"))
        # Live participant mapping reflects the new id.
        assert "newcomer" in v1.participants
        # A fresh view() call captures the bumped epoch.
        v2 = state.view()
        assert v2.room_epoch == e1 + 1


# ---------------------------------------------------------------------------
# Charter renders first under load.
# ---------------------------------------------------------------------------

class TestCharterFirstUnderLoad:

    @pytest.mark.stress
    def test_charter_appears_before_topic_and_persona_under_500_builds(self):
        # Build the prompt 500 times with different persona/topic
        # combinations. The kernel charter must always render before
        # any consumer-supplied text.
        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="loom"))
        coord = RoomCoordinator(bus, state)
        first_charter_line = LOOM_PROTOCOL_INSTRUCTIONS.splitlines()[0]

        for k in range(500):
            persona = f"<<PERSONA_{k}>>"
            topic = f"topic-{k}-x" * 50
            state.set_topic(topic)
            out = build_prompt(
                "loom", trigger_event=None,
                coordinator=coord, policy=DefaultPolicy(),
                persona=persona,
            )
            charter_idx = out.find(first_charter_line)
            persona_idx = out.find(persona)
            topic_idx = out.find(topic)
            assert charter_idx >= 0
            if persona_idx >= 0:
                assert charter_idx < persona_idx, (
                    f"iteration {k}: charter {charter_idx} >= "
                    f"persona {persona_idx}")
            if topic_idx >= 0:
                assert charter_idx < topic_idx, (
                    f"iteration {k}: charter {charter_idx} >= "
                    f"topic {topic_idx}")


# ---------------------------------------------------------------------------
# Single-mutator invariant — only the coordinator writes to RoomState.
# ---------------------------------------------------------------------------

class TestCoordinatorLockSingleMutator:

    def test_no_state_mutation_outside_coordinator_under_load(
            self, thread_harness):
        # Many threads read ``state.view()`` while one thread mutates
        # via the coordinator. The coordinator's lock serializes.
        # We assert: every visible (room_epoch, participants) pair is
        # internally consistent — the participants set never appears
        # to shrink at the same epoch and never grows at the same
        # epoch (each membership change bumps the epoch).
        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="seed"))
        coord = RoomCoordinator(bus, state)

        observed: list[tuple[int, frozenset]] = []
        observed_lock = threading.Lock()

        def reader():
            for _ in range(500):
                view = state.view()
                with observed_lock:
                    observed.append(
                        (view.room_epoch,
                         frozenset(view.participants.keys())))

        def writer():
            for i in range(20):
                coord.register_participant(
                    ParticipantInfo(id=f"churn{i}"))
                coord.unregister_participant(f"churn{i}")

        for _ in range(4):
            thread_harness.spawn(reader, name="reader")
        thread_harness.spawn(writer, name="writer")
        thread_harness.join_all(timeout=10.0)
        assert thread_harness.errors == []

        # Group by epoch and verify each epoch maps to exactly one
        # participant set.
        by_epoch: dict[int, set[frozenset]] = {}
        for epoch, pids in observed:
            by_epoch.setdefault(epoch, set()).add(pids)
        for epoch, sets in by_epoch.items():
            assert len(sets) == 1, (
                f"epoch {epoch} observed with {len(sets)} different "
                f"participant sets — RoomState is not single-writer")


# ---------------------------------------------------------------------------
# Bus event-id monotonicity at scale.
# ---------------------------------------------------------------------------

class TestJournalEventIdMonotonicAtScale:

    @pytest.mark.stress
    def test_2000_events_strict_monotonic_under_concurrent_post(
            self, thread_harness):
        # Identical to the test in test_event_pipeline.py but driven
        # under a heavier load to exercise the bus condvar
        # contention path.
        bus = MessageBus()
        n_posters = 16
        per_poster = 125  # 16 × 125 = 2000

        def poster(pid):
            for j in range(per_poster):
                bus.post(ev.chat(sender=f"a{pid}", body="x",
                                 meta={"pid": pid, "seq": j}))

        for pid in range(n_posters):
            thread_harness.spawn(lambda p=pid: poster(p),
                                  name=f"post{pid}")
        thread_harness.join_all(timeout=10.0)
        assert thread_harness.errors == []

        snap = bus.snapshot(kinds=["chat"])
        assert len(snap) == n_posters * per_poster
        ids = [e.id for e in snap]
        # Strictly monotonic, dense.
        assert ids == sorted(ids)
        assert ids[0] == 0
        assert ids[-1] == len(ids) - 1
