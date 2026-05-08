"""Canonical Loom example — two scripted agents on a console loop.

Run::

    python examples/two_agents.py

This is the smallest end-to-end Loom application that exercises the
v0.1 facade: two agents, an :class:`OpenChatPolicy`, the ``with room:``
lifecycle, and an interactive console. Replace the scripted send
callbacks with real provider calls (e.g. an OpenAI / Anthropic SDK)
to point a real LLM at the room.

Imports come exclusively from the public ``loom.*`` surface — no
``loom.kernel.*`` reach-throughs. The UX contract test
``tests/property/test_ux_contracts.py`` enforces that boundary.
"""
from __future__ import annotations

from loom import LoomRoom, OpenChatPolicy, agent_from_send


def alice_send(prompt: str) -> str:
    return ("Alice: I'd start by clarifying the goal — what does "
            "success look like for this turn?")


def bob_send(prompt: str) -> str:
    return ("Bob: Agreed. I'd add: name the constraint that's binding "
            "us, then enumerate the next two moves.")


def main() -> None:
    alice = agent_from_send("alice", alice_send, persona="planner")
    bob = agent_from_send("bob", bob_send, persona="critic")

    with LoomRoom(agents=[alice, bob], policy=OpenChatPolicy()) as room:
        room.run_console()


if __name__ == "__main__":
    main()
