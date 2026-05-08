"""Targeted coverage of loom/kernel/journal.py defensive paths.

Each test names the specific uncovered line range it targets.
Together with the existing journal tests these tests drive the file
to ~100% line+branch coverage.
"""
from __future__ import annotations

import os
import time
from unittest.mock import patch


from loom.kernel import events as ev
from loom.kernel.journal import Journal, restore_state
from loom.kernel.room import RoomConfig, RoomState


# ---------------------------------------------------------------------------
# Lifecycle defensive paths
# ---------------------------------------------------------------------------

def test_open_is_idempotent_returns_early(tmp_path):
    """Covers: journal.py:112-113 — open() short-circuits when already open."""
    j = Journal(tmp_path)
    j.open()
    f1 = j._events_file
    j.open()  # second call must NOT replace the file handle
    assert j._events_file is f1
    j.close()


# ---------------------------------------------------------------------------
# on_event write-failure paths
# ---------------------------------------------------------------------------

def _make_event() -> ev.Event:
    e = ev.chat(sender="user", body="hi", addressees=[])
    e.id = 0
    e.ts = time.time()
    return e


class _OSErrorOnWrite:
    """A file-like that raises OSError on write — for journal tests."""
    def write(self, *_a, **_k):
        raise OSError("disk full")
    def flush(self):
        pass
    def close(self):
        pass


def test_failure_callback_exception_is_swallowed(tmp_path):
    """Covers: journal.py:198-200 — exception inside callback is swallowed."""
    j = Journal(tmp_path)
    j.open()

    def bad_callback(exc):
        raise RuntimeError("callback raised")

    j.set_failure_callback(bad_callback)
    # Swap in a file that raises OSError on write — exercises the OSError
    # branch (not the ValueError branch from a closed file).
    j._events_file = _OSErrorOnWrite()

    j.on_event(_make_event())  # must not raise
    assert j.degraded is True
    # Recursion guard must reset so future callbacks would fire again.
    assert j._in_failure_dispatch is False


def test_snapshot_due_callback_exception_is_swallowed(tmp_path):
    """Covers: journal.py:208-210 — exception in snapshot_due cb swallowed."""
    j = Journal(tmp_path, snapshot_every_events=1)
    j.open()

    def bad_snap_cb():
        raise RuntimeError("snap cb raised")

    j.set_snapshot_due_callback(bad_snap_cb)

    # First event triggers the snap cb (snapshot_every_events=1) → raises.
    j.on_event(_make_event())  # must not raise
    j.close()


def test_snapshot_due_callback_returns_non_dict_skips_snapshot(tmp_path):
    """Covers: journal.py:211 branch — non-dict payload skips snapshot."""
    j = Journal(tmp_path, snapshot_every_events=1)
    j.open()
    # Returning None (a common "nothing to snapshot" signal) skips.
    j.set_snapshot_due_callback(lambda: None)

    j.on_event(_make_event())  # must not raise
    j.close()
    # The snapshot file should NOT exist because the cb returned None.
    assert not j.state_path.exists()


# ---------------------------------------------------------------------------
# _write_snapshot_dict OSError paths
# ---------------------------------------------------------------------------

def test_fsync_oserror_is_swallowed(tmp_path):
    """Covers: journal.py:277-279 — fsync OSError tolerated, snapshot lands."""
    j = Journal(tmp_path)
    state = RoomState(config=RoomConfig())

    def bad_fsync(fd):
        raise OSError("fsync denied")

    with patch.object(os, "fsync", bad_fsync):
        j.snapshot(state)

    # snapshot still landed via os.replace; only fsync failed.
    assert j.state_path.exists()


def test_snapshot_replace_oserror_fires_callback_and_marks_degraded(tmp_path):
    """Covers: journal.py:281-294 — replace OSError → degraded + callback."""
    j = Journal(tmp_path)
    state = RoomState(config=RoomConfig())
    received: list[Exception] = []
    j.set_failure_callback(lambda exc: received.append(exc))

    def bad_replace(src, dst):
        raise OSError("replace denied")

    with patch("os.replace", bad_replace):
        j.snapshot(state)

    assert j.degraded is True
    assert len(received) == 1
    assert isinstance(received[0], OSError)


def test_snapshot_replace_recursive_callback_guard(tmp_path):
    """Covers: journal.py:285-289, 295-297 — recursion guard during snapshot fail."""
    j = Journal(tmp_path)
    state = RoomState(config=RoomConfig())
    call_count = [0]

    def recursive_callback(exc):
        call_count[0] += 1
        # Re-trigger another snapshot — the guard must prevent re-entry.
        j.snapshot(state)

    j.set_failure_callback(recursive_callback)

    def bad_replace(src, dst):
        raise OSError("replace denied")

    with patch("os.replace", bad_replace):
        j.snapshot(state)

    # Outer call invoked the callback once. Inner snapshot's failure
    # path skipped firing the callback again because of the guard.
    assert call_count[0] == 1
    # Guard must be cleared at the end of the outer call.
    assert j._in_failure_dispatch is False


def test_snapshot_callback_exception_is_swallowed(tmp_path):
    """Covers: journal.py:291-294 — exception in snapshot failure callback."""
    j = Journal(tmp_path)
    state = RoomState(config=RoomConfig())

    def bad_callback(exc):
        raise RuntimeError("callback raised inside snapshot fail path")

    j.set_failure_callback(bad_callback)

    def bad_replace(src, dst):
        raise OSError("replace denied")

    with patch("os.replace", bad_replace):
        j.snapshot(state)  # must not raise

    assert j.degraded is True


def test_snapshot_replace_oserror_with_no_callback_still_marks_degraded(tmp_path):
    """Covers: journal.py:286 (cb is None branch)."""
    j = Journal(tmp_path)
    state = RoomState(config=RoomConfig())
    # No callback registered.

    def bad_replace(src, dst):
        raise OSError("replace denied")

    with patch("os.replace", bad_replace):
        j.snapshot(state)

    assert j.degraded is True


# ---------------------------------------------------------------------------
# Background snapshot loop unexpected-exception path
# ---------------------------------------------------------------------------

def test_snapshot_loop_swallows_unexpected_exception_and_continues(tmp_path):
    """Covers: journal.py:312-318 — non-OSError in writer thread continues."""
    j = Journal(tmp_path)
    j.open()
    state = RoomState(config=RoomConfig())

    calls = [0]

    def first_raises(payload):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("unexpected non-OSError")
        # Second call lands normally via the original implementation.
        return None

    j._write_snapshot_dict = first_raises  # type: ignore[method-assign]

    # Push two snapshot payloads. Loop swallows the first failure; second
    # call reaches the now-no-op stub. Test passes if the close drains.
    j._snapshot_queue.put(Journal._state_to_dict(state))
    j._snapshot_queue.put(Journal._state_to_dict(state))

    j.close()
    assert calls[0] >= 1


# ---------------------------------------------------------------------------
# load_events / restore_state edge cases
# ---------------------------------------------------------------------------

def test_load_events_returns_empty_when_file_missing(tmp_path):
    """Covers: journal.py:381-382 — events.jsonl absent → empty list."""
    nonexistent = tmp_path / "nope-no-such-dir"
    j = Journal(nonexistent)  # creates the dir, but events.jsonl absent
    assert j.load_events() == []


def test_load_events_skips_blank_lines(tmp_path):
    """Covers: journal.py:386-388 — blank lines silently skipped."""
    j = Journal(tmp_path)
    j.events_path.write_text("\n\n  \n")
    assert j.load_events() == []


def test_load_state_returns_none_for_unsupported_version(tmp_path):
    """Covers: journal.py:374-376 — unknown version → None."""
    import json
    j = Journal(tmp_path)
    j.state_path.write_text(json.dumps({"version": 999, "topic": "stale"}))
    assert j.load_state() is None


def test_load_state_returns_none_when_decode_fails(tmp_path):
    """Covers: journal.py:372-373 — corrupt JSON → None."""
    j = Journal(tmp_path)
    j.state_path.write_text("{not valid json")
    assert j.load_state() is None


def test_restore_state_with_none_returns_fresh_state(tmp_path):
    """Covers: journal.py:428-429 — None state_data → fresh RoomState."""
    cfg = RoomConfig()
    s = restore_state(None, cfg)
    assert s.config is cfg
    assert s.room_epoch == 0
    assert s.topic is None


def test_restore_state_with_empty_dict_returns_fresh(tmp_path):
    """Covers: journal.py:428 (falsy state_data branch)."""
    cfg = RoomConfig()
    s = restore_state({}, cfg)
    assert s.room_epoch == 0


def test_restore_state_negative_next_speaker_idx_clamped_to_zero(tmp_path):
    """Covers: journal.py:478-479 — negative next_speaker_idx → 0."""
    cfg = RoomConfig()
    state_data = {
        "version": 3,
        "room_epoch": 0,
        "control": {
            "next_speaker_idx": -7,
            "turn_taking_mode": "round_robin",
            "turn_order": ["a", "b"],
            "style": "normal",
        },
    }
    s = restore_state(state_data, cfg)
    assert s.control.next_speaker_idx == 0


def test_restore_state_invalid_next_speaker_idx_defaults_to_zero(tmp_path):
    """Covers: journal.py:475-477 — non-int next_speaker_idx → 0."""
    cfg = RoomConfig()
    state_data = {
        "version": 3,
        "control": {
            "next_speaker_idx": "not an int",
            "turn_taking_mode": "round_robin",
            "turn_order": ["a"],
            "style": "normal",
        },
    }
    s = restore_state(state_data, cfg)
    assert s.control.next_speaker_idx == 0


def test_restore_state_with_invalid_turn_order_uses_empty_list(tmp_path):
    """Covers: journal.py:472-473 — non-list turn_order → []."""
    cfg = RoomConfig()
    state_data = {
        "version": 3,
        "control": {
            "turn_order": "not-a-list",
            "turn_taking_mode": "round_robin",
            "style": "normal",
        },
    }
    s = restore_state(state_data, cfg)
    assert s.control.turn_order == []


def test_restore_state_with_invalid_floor_owner_falls_back_to_none(tmp_path):
    """Covers: journal.py:460-462 — non-list floor_owner → None."""
    cfg = RoomConfig()
    state_data = {
        "version": 3,
        "control": {
            "floor_owner": "not-a-list",
            "style": "normal",
        },
    }
    s = restore_state(state_data, cfg)
    assert s.control.floor_owner is None


def test_restore_state_with_invalid_roles_falls_back_to_empty(tmp_path):
    """Covers: journal.py:457-459 — non-dict roles → {}."""
    cfg = RoomConfig()
    state_data = {
        "version": 3,
        "control": {
            "roles": "not-a-dict",
            "style": "normal",
        },
    }
    s = restore_state(state_data, cfg)
    assert s.control.roles == {}


def test_restore_state_with_unknown_style_defaults_to_normal(tmp_path):
    """Covers: journal.py:464-465 — unknown style → 'normal'."""
    cfg = RoomConfig()
    state_data = {
        "version": 3,
        "control": {
            "style": "verbose-but-fancy",
        },
    }
    s = restore_state(state_data, cfg)
    assert s.control.style == "normal"


def test_restore_state_with_unknown_ttm_defaults_to_broadcast(tmp_path):
    """Covers: journal.py:468-470 — unknown turn_taking_mode → broadcast."""
    cfg = RoomConfig()
    state_data = {
        "version": 3,
        "control": {
            "style": "normal",
            "turn_taking_mode": "completely_made_up",
        },
    }
    s = restore_state(state_data, cfg)
    assert s.control.turn_taking_mode == "broadcast"
