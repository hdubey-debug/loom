"""Three scripted agents on a ``RoundRobinPolicy``.

Run::

    python examples/round_robin_classroom.py

Each user post advances the rotation by exactly one speaker. With the
order ``[alice, bob, carol]``, posting three times cycles through all
three — useful for a structured classroom roundtable, a debate, or any
strict-rotation scenario.

No API key needed; the agents are scripted.
"""
from __future__ import annotations

from loom import LoomRoom, RoundRobinPolicy, agent_from_send


def alice_send(prompt: str) -> str:
    return "Alice: My take — start from the assumptions and work outward."


def bob_send(prompt: str) -> str:
    return "Bob: Counterpoint — the assumptions usually leak. Test them first."


def carol_send(prompt: str) -> str:
    return "Carol: Synthesis — name the strongest version of each side, then choose."


def main() -> None:
    agents = [
        agent_from_send("alice", alice_send, persona="optimist"),
        agent_from_send("bob", bob_send, persona="skeptic"),
        agent_from_send("carol", carol_send, persona="synthesiser"),
    ]
    policy = RoundRobinPolicy(["alice", "bob", "carol"])

    with LoomRoom(agents=agents, policy=policy, topic="design review") as room:
        room.run_console()


if __name__ == "__main__":
    main()
