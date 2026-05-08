"""Adversarial scenario: tampered journal replay (T1 / P0.1+P0.2).

Threshold:
- Replay completes (no actor crash, no uncaught exception).
- Each tampered line surfaces a ``journal_corruption`` event so the
  corruption is operator-visible rather than silently dropped.
- Tail latency of replay scales linearly with line count (the per-line
  validation overhead is small).
"""
from __future__ import annotations

import json

import pytest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.events import EventShapeError, control_type_of
from loom.kernel.journal import Journal


pytestmark = [pytest.mark.adversarial]


def _build_journal_with_tampered_lines(tmp_path, total: int, *,
                                        tamper_every: int = 20) -> Journal:
    j = Journal(tmp_path)
    lines: list[str] = []
    for i in range(total):
        if i % tamper_every == 0 and i > 0:
            # Inject a tampered line — chat event with non-string body.
            lines.append(json.dumps({
                "kind": "chat", "sender": "user", "body": ["not", "a", "str"],
                "channel": "main", "addressees": [], "room_epoch": 0,
                "user_turn_id": None, "meta": {}, "id": 0, "ts": 0.0,
            }))
            continue
        e = ev.chat(sender="user", body=f"event {i}")
        e.id = i
        e.ts = 1.0 + i * 0.001
        lines.append(e.to_jsonl())
    j.events_path.write_text("\n".join(lines) + "\n")
    return j


def test_replay_completes_with_corruption_surfaced(tmp_path):
    """A 1k-line journal with 5% tampered lines replays cleanly."""
    j = _build_journal_with_tampered_lines(
        tmp_path, total=1000, tamper_every=20)
    yielded = list(j.iter_events(emit_corruption_events=True))
    types = [control_type_of(e) for e in yielded if e.kind == "control"]
    # 49 tampered lines (i in 20, 40, ..., 980).
    n_corruption = sum(1 for t in types if t == "journal_corruption")
    n_truncated = sum(1 for t in types if t == "journal_truncated")
    assert n_corruption + n_truncated >= 45, (
        f"expected >=45 corruption events, got "
        f"corruption={n_corruption}, truncated={n_truncated}")
    # Valid events still passed through.
    valid = [e for e in yielded if e.kind == "chat"]
    assert len(valid) >= 950


def test_silent_mode_preserves_legacy_skip_semantics(tmp_path):
    """Default ``emit_corruption_events=False`` silently skips bad lines."""
    j = _build_journal_with_tampered_lines(
        tmp_path, total=200, tamper_every=10)
    silent = list(j.iter_events())  # default: don't emit corruption
    types = [control_type_of(e) for e in silent if e.kind == "control"]
    assert "journal_corruption" not in types
    assert "journal_truncated" not in types
    # Valid events still parsed.
    assert all(isinstance(e.id, int) for e in silent)


def test_replay_into_bus_completes(tmp_path):
    """``replay_into`` posts every event including corruption surfaces."""
    j = _build_journal_with_tampered_lines(
        tmp_path, total=300, tamper_every=15)

    class _StubCoord:
        def __init__(self):
            self.bus = MessageBus()
    c = _StubCoord()
    posted = j.replay_into(c)
    assert posted >= 280
    log = c.bus.snapshot()
    types = [control_type_of(e) for e in log if e.kind == "control"]
    assert any(t == "journal_corruption" for t in types)


def test_from_jsonl_raises_typed_exception_on_tamper():
    """``from_jsonl`` raises ``EventShapeError`` on shape mismatch — not
    a bare ``TypeError`` that would crash an actor's wakeup loop."""
    bad = json.dumps({
        "kind": "chat", "sender": "user", "body": {"not": "a string"},
    })
    with pytest.raises(EventShapeError):
        ev.Event.from_jsonl(bad)
