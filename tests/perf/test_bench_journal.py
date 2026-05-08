"""Microbench — journal replay throughput.

Phase 2.4 will switch ``Journal.load_events`` from ``[Event.from_jsonl(line)
for line in file]`` to a streaming generator that does not materialize the
whole list — replay-time RSS goes from O(E) to O(1).

For this baseline we measure the current cost of loading and replaying
N events as a list (which is what the kernel does today).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from loom.kernel import events as ev
from loom.kernel.journal import Journal


pytestmark = pytest.mark.perf


def _write_events(path: Path, n: int) -> None:
    with path.open("w") as f:
        for i in range(n):
            e = ev.chat(sender="alice" if i % 2 == 0 else "bob",
                        body=f"msg {i}")
            e.id = i
            e.ts = 1_700_000_000.0 + i * 0.001
            f.write(e.to_jsonl() + "\n")


@pytest.mark.parametrize("size", [100, 1_000, 10_000])
def test_journal_load_events(bench, tmp_path: Path, size):
    """End-to-end load — disk read + per-line JSON parse + Event reconstruct."""
    j = Journal(session_dir=tmp_path)
    _write_events(j.events_path, size)
    bench(lambda: j.load_events(),
          name=f"Journal.load_events E={size}", iters=20, inner=1)


@pytest.mark.parametrize("size", [100, 1_000])
def test_journal_load_events_peak_bytes(bench, tmp_path: Path, size):
    """Wall-clock proxy for peak-memory regressions; the actual RSS gate is
    enforced in ``benchmarks/perf.py`` via ``tracemalloc``.
    """
    j = Journal(session_dir=tmp_path)
    _write_events(j.events_path, size)

    def load_and_drop():
        out = j.load_events()
        del out

    bench(load_and_drop,
          name=f"Journal.load_events drop E={size}", iters=20, inner=1)
