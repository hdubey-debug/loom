"""v0.2/v0.3 demo — plug a custom :class:`LeaseCheck` into a room.

Run::

    PYTHONPATH=. python examples/custom_lease_check.py

What you'll see:

A custom ``MuteParticipant`` check is wired into the room via
``RoomConfig.lease_checks``. It denies every USER_TURN lease for one
named participant — effectively muting them while keeping them in
the room for context. The other participant can still speak.

Each custom check exposes an ``applies_to`` filter so the kernel
only invokes it for the matching :class:`LeaseKind` (here: only
USER_TURN). Returning ``LeaseCheckResult(False, "<reason>")`` denies
the lease and emits a structured ``lease_denied`` event carrying
the check's ``name`` and ``deny_reason`` for observability.

We post a user message, then print the lease_denied events fired
by our custom check.
"""

from __future__ import annotations

from loom import (
    LoomRoom,
    OpenChatPolicy,
    RoomConfig,
    agent_from_send,
)
from loom.contracts import PASSED, LeaseCheckResult
from loom.kernel.leases import LeaseKind


class MuteParticipant:
    """Block every USER_TURN lease for ``muted_id``."""

    name = "mute_participant"
    applies_to = frozenset({LeaseKind.USER_TURN})

    def __init__(self, muted_id: str) -> None:
        self.muted_id = muted_id

    def check(self, *, holder, trigger_event_id, is_direct_mention, coordinator):
        del trigger_event_id, is_direct_mention, coordinator
        if holder == self.muted_id:
            return LeaseCheckResult(False, f"muted:{self.muted_id}")
        return PASSED


def alice_send(prompt: str) -> str:
    return "alice: thinking aloud about the question."


def bob_send(prompt: str) -> str:
    return "bob: I agree with alice's first point."


def main() -> None:
    config = RoomConfig(
        lease_checks=(MuteParticipant("alice"),),
    )
    alice = agent_from_send("alice", alice_send, persona="planner")
    bob = agent_from_send("bob", bob_send, persona="critic")

    room = LoomRoom(
        agents=[alice, bob],
        policy=OpenChatPolicy(),
        room_config=config,
        topic="design review",
    )
    with room:
        result = room.post_and_wait("what do you think of this plan?", timeout=2.0)
        print(f"replies in turn: {len(result.messages)} (reason={result.closed_reason})")
        for m in result.messages:
            print(f"  - {m.sender}: {m.body[:60]!r}")

        # Inspect the lease_denied events from our custom check.
        denials = []
        for e in room._session.bus.snapshot():
            body = e.body if isinstance(e.body, dict) else {}
            if body.get("control_type") == "lease_denied":
                denials.append((e.id, body))

        print(f"\nlease_denied events: {len(denials)}")
        for eid, body in denials:
            print(
                f"  [{eid:>3}] check={body.get('check_name')!r:<32} "
                f"holder={body.get('holder')!r} reason={body.get('deny_reason')!r}"
            )


if __name__ == "__main__":
    main()
