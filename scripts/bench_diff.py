#!/usr/bin/env python3
"""Loom performance regression diff.

Reads two ``benchmarks/perf.py`` JSON outputs and prints a delta
table for every case present in both. Exits non-zero if any p50 metric
regresses by more than the threshold (default 15%).

Usage::

    scripts/bench_diff.py docs/perf-baseline.json /tmp/perf-after.json
    scripts/bench_diff.py --threshold 0.10 baseline.json after.json
    scripts/bench_diff.py --json baseline.json after.json   # delta-only JSON

The threshold is a ratio: 0.15 = 15%. Compares ``p50_ns`` only —
``p99_ns`` swings are noisier and not gated. Cases present in only one
file are reported as "missing" (informational, does not fail).

Special-cased extras:
- ``per_op_ns``       gated like p50 (15% regression threshold)
- ``peak_bytes``      gated like p50 (memory must not balloon)
- ``bytes_per_event`` gated like p50

The CI gating policy is documented in ``docs/perf-baseline.md``: we
use *relative* deltas vs the committed baseline, never absolute
thresholds, because the bench host varies.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional


_GATED_EXTRAS = ("per_op_ns", "peak_bytes", "bytes_per_event",
                 "rss_delta_per_turn")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _fmt_ns(ns: float) -> str:
    if ns >= 1_000_000_000:
        return f"{ns / 1_000_000_000:.2f}s"
    if ns >= 1_000_000:
        return f"{ns / 1_000_000:.2f}ms"
    if ns >= 1_000:
        return f"{ns / 1_000:.2f}us"
    return f"{ns:.0f}ns"


def _fmt_bytes(b: float) -> str:
    if b >= 1024 * 1024:
        return f"{b / 1024 / 1024:.2f}MB"
    if b >= 1024:
        return f"{b / 1024:.2f}KB"
    return f"{b:.0f}B"


def _delta(old: float, new: float) -> tuple[float, str]:
    if old == 0:
        return (0.0, "n/a") if new == 0 else (float("inf"), "+inf")
    ratio = (new - old) / old
    sign = "+" if ratio >= 0 else ""
    return ratio, f"{sign}{ratio * 100:.1f}%"


def _compare_case(scen: str, name: str,
                   old: dict[str, Any], new: dict[str, Any],
                   threshold: float) -> dict[str, Any]:
    rec: dict[str, Any] = {"scenario": scen, "case": name}

    # p50 gate
    if old["p50_ns"] > 0 or new["p50_ns"] > 0:
        ratio, label = _delta(old["p50_ns"], new["p50_ns"])
        rec["p50"] = {
            "old_ns": old["p50_ns"], "new_ns": new["p50_ns"],
            "ratio": ratio, "label": label,
            "regression": ratio > threshold,
            "improvement": ratio < -threshold,
        }
    if old.get("p99_ns") and new.get("p99_ns"):
        ratio, label = _delta(old["p99_ns"], new["p99_ns"])
        rec["p99"] = {
            "old_ns": old["p99_ns"], "new_ns": new["p99_ns"],
            "ratio": ratio, "label": label,
        }

    # gated extras
    old_x = old.get("extras", {})
    new_x = new.get("extras", {})
    for key in _GATED_EXTRAS:
        if key in old_x and key in new_x:
            o = old_x[key]
            n = new_x[key]
            ratio, label = _delta(float(o), float(n))
            rec[key] = {
                "old": o, "new": n,
                "ratio": ratio, "label": label,
                "regression": ratio > threshold,
                "improvement": ratio < -threshold,
            }

    return rec


def _print_table(diffs: list[dict[str, Any]], threshold: float) -> None:
    print(f"{'scenario':<14} {'case':<48} {'metric':<14} "
          f"{'old':>14} {'new':>14} {'delta':>10}")
    print("-" * 116)
    last_scen = None
    for d in diffs:
        if d["scenario"] != last_scen:
            last_scen = d["scenario"]
        for metric in ("p50", "p99", "per_op_ns", "peak_bytes",
                        "bytes_per_event", "rss_delta_per_turn"):
            if metric not in d:
                continue
            m = d[metric]
            old_v = m.get("old_ns") if "old_ns" in m else m.get("old")
            new_v = m.get("new_ns") if "new_ns" in m else m.get("new")
            if metric in ("p50", "p99"):
                old_s = _fmt_ns(float(old_v))
                new_s = _fmt_ns(float(new_v))
            elif metric in ("peak_bytes",):
                old_s = _fmt_bytes(float(old_v))
                new_s = _fmt_bytes(float(new_v))
            elif metric == "per_op_ns":
                old_s = _fmt_ns(float(old_v))
                new_s = _fmt_ns(float(new_v))
            else:
                old_s = f"{float(old_v):.1f}"
                new_s = f"{float(new_v):.1f}"
            tag = ""
            if m.get("regression"):
                tag = " ⚠"
            elif m.get("improvement"):
                tag = " ✓"
            print(f"{d['scenario']:<14} {d['case']:<48} {metric:<14} "
                  f"{old_s:>14} {new_s:>14} {m['label']:>10}{tag}")


def _gather_regressions(diffs: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for d in diffs:
        for metric in ("p50",) + _GATED_EXTRAS:
            m = d.get(metric)
            if m and m.get("regression"):
                out.append(f"{d['scenario']}/{d['case']} {metric}: {m['label']}")
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("baseline", help="committed baseline JSON")
    p.add_argument("after", help="post-change JSON to compare")
    p.add_argument("--threshold", type=float, default=0.15,
                   help="regression threshold ratio (default 0.15 = 15%%)")
    p.add_argument("--json", action="store_true",
                   help="emit the delta as JSON instead of a table")
    p.add_argument("--quiet-pass", action="store_true",
                   help="suppress output when no regressions; only print failures")
    args = p.parse_args(argv)

    base = _load(Path(args.baseline))
    after = _load(Path(args.after))
    threshold = float(args.threshold)

    diffs: list[dict[str, Any]] = []
    base_scen = base.get("scenarios", {})
    after_scen = after.get("scenarios", {})
    only_in_base: list[str] = []
    only_in_after: list[str] = []
    for scen, base_cases in base_scen.items():
        after_cases = after_scen.get(scen, {})
        for name, base_case in base_cases.items():
            after_case = after_cases.get(name)
            if after_case is None:
                only_in_base.append(f"{scen}/{name}")
                continue
            diffs.append(_compare_case(scen, name, base_case, after_case,
                                        threshold))
        for name in after_cases:
            if name not in base_cases:
                only_in_after.append(f"{scen}/{name}")
    for scen in after_scen:
        if scen not in base_scen:
            for name in after_scen[scen]:
                only_in_after.append(f"{scen}/{name}")

    regressions = _gather_regressions(diffs)

    if args.json:
        print(json.dumps({
            "threshold": threshold,
            "baseline_ts": base.get("ts"),
            "baseline_git": base.get("git_rev"),
            "after_ts": after.get("ts"),
            "after_git": after.get("git_rev"),
            "diffs": diffs,
            "only_in_baseline": only_in_base,
            "only_in_after": only_in_after,
            "regressions": regressions,
        }, indent=2))
    else:
        if not args.quiet_pass or regressions:
            _print_table(diffs, threshold)
            if only_in_base:
                print(f"\nOnly in baseline ({len(only_in_base)}): "
                      f"{', '.join(only_in_base[:5])}"
                      f"{'...' if len(only_in_base) > 5 else ''}")
            if only_in_after:
                print(f"Only in after    ({len(only_in_after)}): "
                      f"{', '.join(only_in_after[:5])}"
                      f"{'...' if len(only_in_after) > 5 else ''}")
            if regressions:
                print(f"\nFAIL: {len(regressions)} regressions "
                      f"(threshold {threshold * 100:.0f}%):")
                for r in regressions:
                    print(f"  - {r}")
            else:
                print(f"\nOK: no regressions over {threshold * 100:.0f}% "
                      f"({len(diffs)} cases compared)")

    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
