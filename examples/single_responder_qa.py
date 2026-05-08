"""Single-responder Q&A — the minimal Loom skeleton.

Run::

    python examples/single_responder_qa.py

A ``SingleResponderPolicy`` always routes to one configured agent.
This is the right shape for "talk to one assistant" — chatbot, tool
use, single-model copilot.

The agent here is scripted; swap ``expert_send`` for an OpenAI /
Anthropic / Gemini call to point a real LLM at the room.
"""
from __future__ import annotations

from loom import LoomRoom, SingleResponderPolicy, agent_from_send


def expert_send(prompt: str) -> str:
    return (
        "Expert: I'd answer in three parts — restate the question, "
        "give the shortest correct answer, then list one caveat."
    )


def main() -> None:
    expert = agent_from_send("expert", expert_send, persona="domain expert")

    room = LoomRoom(
        agents=[expert],
        policy=SingleResponderPolicy("expert"),
        topic="Q&A",
    )

    with room:
        room.run_console()


if __name__ == "__main__":
    main()
