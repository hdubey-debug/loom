"""Two OpenAI-backed agents on an ``OpenChatPolicy`` room.

Run::

    export OPENAI_API_KEY=sk-...
    python examples/openai_two_agents.py

Wraps the official ``openai`` SDK as Loom agents using
:func:`agent_from_send`. The agents speak in turn through Loom's open
chat policy — both reply to every user post.

This example is also a smoke test of the public surface against a real
provider. Replace ``base_url`` to talk to any OpenAI-compatible
endpoint (Anthropic via OAI-compat, Gemini via OAI-compat, Together,
Groq, vLLM, Ollama, ...).
"""

from __future__ import annotations

import os
import sys

from loom import LoomRoom, OpenChatPolicy, agent_from_send


def _make_send(model: str, system: str):
    """Build a send-callback that hits the OpenAI Chat Completions API."""
    try:
        from openai import OpenAI
    except ImportError:  # pragma: no cover
        sys.exit("openai>=1.0 is required: pip install openai")

    client = OpenAI()  # honours OPENAI_API_KEY / OPENAI_BASE_URL env vars

    def send(prompt: str) -> str:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        return resp.choices[0].message.content or ""

    return send


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY to run this example.")

    gpt = agent_from_send(
        "gpt",
        _make_send("gpt-4o-mini", "You are a concise planner."),
        persona="planner",
    )
    critic = agent_from_send(
        "critic",
        _make_send("gpt-4o-mini", "You are a concise critic. One paragraph."),
        persona="critic",
    )

    with LoomRoom(agents=[gpt, critic], policy=OpenChatPolicy()) as room:
        room.run_console()


if __name__ == "__main__":
    main()
