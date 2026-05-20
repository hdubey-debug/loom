"""Render the four Loom architecture diagrams as PNGs (matplotlib).

Re-run from the repo root to regenerate:

    python docs/diagrams/_render.py

This script is the *source of truth* for the diagrams — checked into
the repo so future contributors can adjust coordinates / labels and
re-render rather than reverse-engineering a binary PNG. Visual
language used across all four diagrams:

- Rectangles for modules / state slots (blue fill).
- Rounded rectangles for events (orange fill).
- Diamonds for decision points (gold fill).
- Solid arrows for control flow; dashed arrows for "labelled" reads.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.patches as patches
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Polygon

HERE = Path(__file__).parent

MODULE_FILL = "#dbeafe"     # light blue
MODULE_EDGE = "#1d4ed8"
EVENT_FILL = "#fed7aa"      # light orange
EVENT_EDGE = "#c2410c"
DECISION_FILL = "#fde68a"   # gold
DECISION_EDGE = "#a16207"
USER_FILL = "#bbf7d0"       # green
USER_EDGE = "#15803d"
ARROW_COLOR = "#374151"
LABEL_COLOR = "#111827"


def _module(ax, x, y, w, h, label, fill=MODULE_FILL, edge=MODULE_EDGE, fontsize=10):
    ax.add_patch(
        patches.Rectangle(
            (x, y), w, h, linewidth=1.6, edgecolor=edge, facecolor=fill
        )
    )
    ax.text(
        x + w / 2, y + h / 2, label,
        ha="center", va="center", fontsize=fontsize, color=LABEL_COLOR,
    )


def _event(ax, x, y, w, h, label, fill=EVENT_FILL, edge=EVENT_EDGE, fontsize=9):
    ax.add_patch(
        FancyBboxPatch(
            (x, y), w, h,
            boxstyle="round,pad=0.02,rounding_size=0.18",
            linewidth=1.4, edgecolor=edge, facecolor=fill,
        )
    )
    ax.text(
        x + w / 2, y + h / 2, label,
        ha="center", va="center", fontsize=fontsize, color=LABEL_COLOR,
    )


def _decision(ax, cx, cy, w, h, label, fontsize=9):
    pts = [
        (cx, cy + h / 2),
        (cx + w / 2, cy),
        (cx, cy - h / 2),
        (cx - w / 2, cy),
    ]
    ax.add_patch(
        Polygon(pts, closed=True, linewidth=1.6,
                edgecolor=DECISION_EDGE, facecolor=DECISION_FILL)
    )
    ax.text(cx, cy, label, ha="center", va="center",
            fontsize=fontsize, color=LABEL_COLOR)


def _arrow(ax, x0, y0, x1, y1, label=None, dashed=False, color=ARROW_COLOR,
           label_offset=(0, 0.12), fontsize=8, label_color=LABEL_COLOR):
    ls = (0, (4, 3)) if dashed else "-"
    ax.add_patch(
        FancyArrowPatch(
            (x0, y0), (x1, y1),
            arrowstyle="-|>", mutation_scale=14,
            linewidth=1.4, color=color, linestyle=ls,
        )
    )
    if label:
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        ax.text(mx + label_offset[0], my + label_offset[1], label,
                ha="center", va="center", fontsize=fontsize,
                color=label_color, style="italic")


def _legend(ax, items, loc=(0.02, 0.02)):
    handles = []
    for label, color in items:
        handles.append(Line2D([0], [0], color=color, lw=4, label=label))
    ax.legend(handles=handles, loc="lower left", bbox_to_anchor=loc,
              fontsize=8, frameon=True, facecolor="white")


def _save(fig, name):
    out = HERE / name
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"wrote {out.relative_to(HERE.parent.parent)}")


# ---------------------------------------------------------------------------
# 1. architecture.png — LoomRoom → Session → (Kernel + Policy + Agents)
# ---------------------------------------------------------------------------


def render_architecture():
    fig, ax = plt.subplots(figsize=(14, 9))
    ax.set_xlim(0, 22)
    ax.set_ylim(0, 13)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Loom architecture (v0.3)", fontsize=15, pad=18)

    # Top row — User on the left, slash commands on the right.
    _module(ax, 0.6, 11.4, 4.0, 1.1,
            "User / prompt_fn", fill=USER_FILL, edge=USER_EDGE)
    _module(ax, 8.4, 11.4, 5.2, 1.1,
            "LoomRoom (facade)\npost · post_and_wait · add_agent")
    _module(ax, 17.0, 11.4, 4.6, 1.1,
            "Slash commands\ndispatch_slash_command",
            fill="#fef3c7", edge="#b45309")

    # User → LoomRoom; LoomRoom → Slash commands (top-row only).
    _arrow(ax, 4.6, 11.95, 8.4, 11.95, label="user text")
    _arrow(ax, 17.0, 11.95, 13.6, 11.95,
           label="from user", label_offset=(0, 0.22))

    # Session container.
    ax.add_patch(
        patches.Rectangle(
            (0.6, 0.6), 21.0, 10.0, linewidth=1.6,
            edgecolor="#475569", facecolor="#f8fafc",
            linestyle=(0, (5, 4))
        )
    )
    ax.text(0.85, 10.2, "LoomSession", fontsize=11,
            color="#475569", style="italic", weight="bold")

    # Kernel cluster — left two-thirds.
    ax.add_patch(
        patches.Rectangle(
            (1.1, 4.0), 12.4, 5.6, linewidth=1.4,
            edgecolor=MODULE_EDGE, facecolor="#eff6ff"
        )
    )
    ax.text(7.3, 9.3, "Kernel — the only mutator",
            ha="center", fontsize=11, color=MODULE_EDGE, weight="bold")
    _module(ax, 1.6, 7.4, 3.6, 1.3, "MessageBus\n(append-only)")
    _module(ax, 5.6, 7.4, 3.6, 1.3, "Coordinator\n(leases · checks)")
    _module(ax, 9.6, 7.4, 3.6, 1.3, "EffectRegistry\n(reducers)")
    _module(ax, 1.6, 4.5, 3.6, 1.3, "RoomState\n(participants · topic)")
    _module(ax, 5.6, 4.5, 3.6, 1.3, "KernelState\n(caps · context · floor)")
    _module(ax, 9.6, 4.5, 3.6, 1.3, "Lease registry\n(6 kinds)")

    # Journal — across the bottom.
    _module(ax, 1.6, 1.6, 11.6, 1.2,
            "Journal — events.jsonl + room_state.json (snapshot v7)")

    # Policy + Control actions — right column.
    _module(ax, 14.4, 7.4, 6.8, 1.3,
            "Policy.plan_user_turn",
            fill="#fef3c7", edge="#b45309")
    _module(ax, 14.4, 4.5, 6.8, 1.3,
            "Control actions registry\n(SetTopic · SetAnchor · …)",
            fill="#fef3c7", edge="#b45309")
    _module(ax, 14.4, 1.6, 6.8, 1.2,
            "Agents — 1..N daemon threads (Agent A, B, … N)")

    # Connections — kept few and labelled clearly.
    _arrow(ax, 5.2, 8.05, 5.6, 8.05, label="event", fontsize=8)
    _arrow(ax, 7.4, 7.4, 7.4, 5.8, label="apply", fontsize=8)
    _arrow(ax, 9.2, 5.15, 7.4, 4.8, dashed=True,
           label="state", fontsize=8, label_offset=(0, 0.2))
    _arrow(ax, 9.6, 8.05, 9.2, 8.05, dashed=True,
           label="reduce", fontsize=8, label_offset=(0, 0.18))
    _arrow(ax, 7.4, 4.5, 7.4, 2.8, label="persist", fontsize=8)

    # Policy ↔ Coordinator.
    _arrow(ax, 13.2, 8.3, 14.4, 8.3,
           label="frozen view", dashed=True, fontsize=8)
    _arrow(ax, 14.4, 7.8, 13.2, 7.8,
           label="UserTurnPlan", fontsize=8, label_offset=(0, -0.22))

    # Control actions → Coordinator.
    _arrow(ax, 14.4, 5.4, 13.2, 5.4,
           label="ControlEffect", fontsize=8)
    _arrow(ax, 13.2, 4.9, 14.4, 4.9,
           label="proposal", fontsize=8, label_offset=(0, -0.22))

    # Agents ↔ Coordinator (single edge on the right; routed cleanly).
    _arrow(ax, 17.8, 2.8, 17.8, 4.5,
           label="acquire lease", fontsize=8,
           label_offset=(1.2, 0))
    _arrow(ax, 16.6, 4.5, 16.6, 2.8,
           label="stream chunks", fontsize=8,
           label_offset=(-1.2, 0))

    # LoomRoom → Coordinator (curved arrow to avoid crossing labels).
    ax.add_patch(FancyArrowPatch(
        (11.0, 11.4), (7.4, 8.7),
        arrowstyle="-|>", mutation_scale=14, linewidth=1.4,
        color=ARROW_COLOR,
        connectionstyle="arc3,rad=0.25",
    ))
    ax.text(8.6, 10.3, "builds & posts", fontsize=8,
            color=LABEL_COLOR, style="italic")

    # Slash commands → Control actions (curved arrow on the right).
    ax.add_patch(FancyArrowPatch(
        (19.0, 11.4), (17.8, 5.8),
        arrowstyle="-|>", mutation_scale=14, linewidth=1.4,
        color=ARROW_COLOR,
        connectionstyle="arc3,rad=-0.22",
    ))
    ax.text(20.1, 8.6, "propose", fontsize=8,
            color=LABEL_COLOR, style="italic")

    _legend(ax, [
        ("modules / state", MODULE_EDGE),
        ("hooks (policy / commands)", "#b45309"),
        ("user surface", USER_EDGE),
    ], loc=(0.01, 0.01))
    _save(fig, "architecture.png")


# ---------------------------------------------------------------------------
# 2. lease-lifecycle.png — acquire → checks → grant/deny → release
# ---------------------------------------------------------------------------


def render_lease_lifecycle():
    fig, ax = plt.subplots(figsize=(12, 5.5))
    ax.set_xlim(0, 18)
    ax.set_ylim(0, 8)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Lease lifecycle (v0.3 — six lease kinds)", fontsize=14, pad=14)

    # Holder column.
    _module(ax, 0.4, 3.5, 2.6, 1.2,
            "Holder\n(actor / coordinator)", fill=USER_FILL, edge=USER_EDGE)

    # Acquire.
    _event(ax, 3.4, 3.6, 2.4, 1.0, "acquire_lease(kind, holder, ctx)")

    # Checks chain.
    ax.add_patch(
        patches.Rectangle(
            (6.4, 0.8), 5.6, 6.4, linewidth=1.4,
            edgecolor=MODULE_EDGE, facecolor="#eff6ff"
        )
    )
    ax.text(9.2, 6.85, "Lease-check chain",
            ha="center", fontsize=11, color=MODULE_EDGE, weight="bold")
    checks = [
        "SlotCheck — N concurrent of this kind",
        "CapabilityCheck — proposer holds verb",
        "FloorOverrideCheck — ADD/REPLACE/BLOCK",
        "ThrottleCheck — per-participant cadence",
        "SummarizerSlotCheck — single SUMMARIZATION slot",
        "BudgetCheck — token / turn ceilings",
    ]
    for i, c in enumerate(checks):
        y = 6.0 - i * 0.85
        _module(ax, 6.7, y, 5.0, 0.6, c, fontsize=8,
                fill="white", edge=MODULE_EDGE)

    # Decision diamond.
    _decision(ax, 13.2, 4.1, 2.0, 1.6, "all pass?")

    # Outcomes.
    _module(ax, 14.7, 5.6, 2.8, 1.0, "Lease granted",
            fill="#bbf7d0", edge="#15803d")
    _event(ax, 14.7, 2.5, 2.8, 0.9, "lease_denied(reason)",
           fill="#fecaca", edge="#b91c1c")

    # Release / expire below granted.
    _event(ax, 14.7, 0.9, 2.8, 0.9, "release / expire")

    # Arrows.
    _arrow(ax, 3.0, 4.1, 3.4, 4.1)
    _arrow(ax, 5.8, 4.1, 6.4, 4.1)
    _arrow(ax, 11.7, 4.1, 12.2, 4.1)
    _arrow(ax, 13.7, 4.7, 14.7, 6.0, label="yes")
    _arrow(ax, 13.7, 3.5, 14.7, 2.9, label="no")
    _arrow(ax, 16.1, 5.6, 16.1, 1.8, dashed=True,
           label="effect applied", fontsize=7,
           label_offset=(0.5, 0))

    ax.text(0.4, 1.6, "Each check exposes\napplies_to: frozenset[LeaseKind]\n→ filters per lease kind",
            fontsize=8, color="#475569", style="italic")
    _save(fig, "lease-lifecycle.png")


# ---------------------------------------------------------------------------
# 3. control-action-flow.png — slash command → effect → reducer
# ---------------------------------------------------------------------------


def render_control_action_flow():
    fig, ax = plt.subplots(figsize=(13, 6))
    ax.set_xlim(0, 20)
    ax.set_ylim(0, 9)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Control-action flow (v0.3 — P10/P15)", fontsize=14, pad=14)

    # User vs agent proposers.
    _module(ax, 0.3, 6.8, 3.0, 1.0,
            'User\nproposer_id="user"', fill=USER_FILL, edge=USER_EDGE)
    _module(ax, 0.3, 4.4, 3.0, 1.0,
            "Agent\nproposer_id=<aid>", fill=USER_FILL, edge=USER_EDGE)

    # Slash command parser (user-only path).
    _event(ax, 3.8, 6.8, 3.0, 1.0, "parse_slash_command")

    # propose_control_action.
    _module(ax, 7.4, 5.5, 3.4, 1.4,
            "coordinator.\npropose_control_action()", fontsize=10)

    # CONTROL_ACTION lease.
    _event(ax, 11.4, 5.6, 3.4, 1.2,
           "acquire_lease(kind=CONTROL_ACTION)")

    # Capability check (P10).
    _decision(ax, 13.1, 3.4, 2.5, 1.5,
              "proposer\nholds cap?")

    # P15 bypass annotation.
    ax.annotate(
        "P15 — user-issued commands\nbypass capability gate",
        xy=(13.1, 3.4), xytext=(7.0, 1.4),
        ha="center", fontsize=8, color="#7c2d12", style="italic",
        arrowprops=dict(arrowstyle="-|>", color="#7c2d12",
                        connectionstyle="arc3,rad=-0.2"),
    )

    # Effect.
    _module(ax, 15.4, 6.5, 4.2, 1.0,
            "action.propose_effect(view)\n→ ControlEffect")
    _event(ax, 15.4, 4.6, 4.2, 1.0, "control_action_applied")
    _event(ax, 15.4, 2.9, 4.2, 1.0, "control_action_denied")

    # Reducer + state.
    _module(ax, 15.4, 0.6, 4.2, 1.4,
            "EffectRegistry.apply(state, effect)\n→ KernelState mutation")

    # Arrows.
    _arrow(ax, 3.3, 7.2, 3.8, 7.2)
    _arrow(ax, 6.8, 7.0, 7.4, 6.4, label="ParsedCommand", fontsize=8,
           label_offset=(0, 0.15))
    _arrow(ax, 3.3, 4.9, 7.4, 6.0, label="action_name + params",
           fontsize=8, label_offset=(0.7, 0.15))
    _arrow(ax, 10.8, 6.2, 11.4, 6.2)
    _arrow(ax, 13.1, 5.5, 13.1, 4.2)
    _arrow(ax, 14.4, 3.7, 15.4, 7.0, label="yes", fontsize=8)
    _arrow(ax, 13.1, 2.7, 15.4, 3.4, label="no", fontsize=8)
    _arrow(ax, 17.5, 6.5, 17.5, 5.6, dashed=True)
    _arrow(ax, 17.5, 4.6, 17.5, 2.0, dashed=True, label="reduce", fontsize=8,
           label_offset=(0.5, 0))
    _save(fig, "control-action-flow.png")


# ---------------------------------------------------------------------------
# 4. compaction-flow.png — Path A vs Path B
# ---------------------------------------------------------------------------


def render_compaction_flow():
    fig, ax = plt.subplots(figsize=(13, 6.5))
    ax.set_xlim(0, 20)
    ax.set_ylim(0, 10)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Context compaction (v0.3.x — Path A vs Path B)",
                 fontsize=14, pad=14)

    # Path A header.
    ax.text(3.0, 9.2, "Path A — policy pressure",
            fontsize=11, color="#1d4ed8", weight="bold")
    ax.text(3.0, 1.3, "Path B — /summarize (user/agent)",
            fontsize=11, color="#1d4ed8", weight="bold")

    # Path A modules.
    _event(ax, 0.3, 7.6, 3.2, 1.0, "policy detects\npressure ≥ threshold")
    _module(ax, 4.0, 7.6, 3.4, 1.0, "schedule_summarization\n(scope, range)")

    # Path B modules.
    _event(ax, 0.3, 0.0, 3.2, 1.0, "/summarize\n(or SummarizeAction)")
    _module(ax, 4.0, 0.0, 3.4, 1.0, "request_summarization\n(scope)")

    # Shared convergence.
    _event(ax, 8.2, 4.0, 3.6, 1.4,
           "SUMMARIZATION lease\nholder = default_summarizer",
           fontsize=9)
    _module(ax, 12.4, 6.6, 3.8, 1.1,
            "Off-lock summariser\nemits SummaryRecord")
    _decision(ax, 16.5, 7.1, 2.6, 1.4, "validator\nok?")
    _event(ax, 12.4, 3.6, 3.8, 1.0, "summary_proposed",
           fill="#fed7aa", edge="#c2410c")
    _event(ax, 16.4, 3.6, 3.4, 1.0, "summary_committed",
           fill="#bbf7d0", edge="#15803d")
    _event(ax, 12.4, 2.0, 3.8, 1.0, "summary_failed",
           fill="#fecaca", edge="#b91c1c")
    _module(ax, 16.4, 0.4, 3.4, 1.1,
            "active_summary_by_scope\nlineage edge added")

    # Arrows — Path A → convergence.
    _arrow(ax, 3.5, 8.1, 4.0, 8.1)
    _arrow(ax, 7.4, 8.1, 10.0, 5.4, label="ctx + ttl", fontsize=8,
           label_offset=(0, 0.2))

    # Arrows — Path B → convergence.
    _arrow(ax, 3.5, 0.5, 4.0, 0.5)
    _arrow(ax, 7.4, 0.5, 10.0, 4.0, label="ctx + ttl", fontsize=8,
           label_offset=(0, -0.25))

    # Convergence → off-lock summariser → validator.
    _arrow(ax, 11.8, 4.9, 12.4, 6.9, label="grant", fontsize=8)
    _arrow(ax, 16.2, 7.1, 16.5, 7.4)
    _arrow(ax, 17.0, 6.5, 17.0, 4.1, label="yes", fontsize=8)
    _arrow(ax, 15.5, 7.0, 13.5, 4.6, label="no", fontsize=8,
           label_offset=(0.5, 0.4))
    _arrow(ax, 16.4, 3.6, 12.4, 2.5, dashed=True,
           label="if commit-time conflict",
           fontsize=7, label_offset=(0.5, 0.15))

    _arrow(ax, 18.1, 3.6, 18.1, 1.5, dashed=True, label="apply", fontsize=8)

    ax.text(0.3, 5.4,
            "Per-scope backoff:\nrepeated failures grow the\nretry window for that\n(summarizer, scope) pair.",
            fontsize=8, color="#475569", style="italic")
    _save(fig, "compaction-flow.png")


def main():
    render_architecture()
    render_lease_lifecycle()
    render_control_action_flow()
    render_compaction_flow()


if __name__ == "__main__":
    main()
