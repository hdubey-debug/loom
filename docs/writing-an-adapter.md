# Writing a custom Loom adapter

An Loom "adapter" is anything that turns an LLM provider into an
object the room can use as a participant. The room consumes the
`Agent` Protocol — `id` plus `stream(prompt) -> Iterator[str]` —
which is intentionally minimal.

This tutorial walks through the canonical path (`agent_from_send`)
first, then shows how to write a custom proxy class when that's not
enough.

## 1. The canonical path — `agent_from_send`

If your LLM client exposes a `send(prompt) -> str` (or returns
something with a `.text` / `.body` / `.content` attribute), wrap it:

```python
from loom import LoomRoom, agent_from_send

def openai_send(prompt: str) -> str:
    response = openai_client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content

room = LoomRoom(
    agents=[
        agent_from_send(
            "gpt",
            openai_send,
            persona="You are a helpful assistant named gpt.",
            cost_tier=2,
        ),
    ],
    policy=OpenChatPolicy(),
)
```

`agent_from_send` returns an object satisfying `Agent` directly. No
subclassing, no boilerplate.

The two siblings handle other shapes:

- `agent_from_stream(id, stream_fn)` — when your client already
  yields delta strings.
- `agent_from_object(id, client_obj)` — when you have a client
  object and want to auto-detect `.stream` vs `.send`.

For most adapters, this is the entire integration. Stop reading
unless you need streaming or per-turn cancellation.

## 2. The `Agent` Protocol

The full contract:

```python
@runtime_checkable
class Agent(Protocol):
    id: str

    def stream(self, prompt: str) -> Iterator[str]: ...
```

Optional attributes (read by the room via `getattr`; absent →
default):

| Attribute | Default | Effect |
|---|---|---|
| `persona: str` | `""` | Self-description rendered into the prompt. |
| `capability_block: str` | `""` | Short feature/limit summary rendered into the prompt. |
| `cost_tier: int` | `1` | Cheaper agents are preferred for slot fallback. |
| `capable: bool` | `True` | Gate for slot fallback eligibility. |
| `cancel() -> None` | (no-op) | Best-effort hard cancel of an in-flight stream. |

Typo-protection: `_agent_to_wiring` warns when an Agent has an
attribute name close to a known optional but mistyped (e.g.,
`personality` for `persona`). Watch the warnings on the first run
of a fresh adapter.

## 3. Streaming contract

Your `stream(prompt)` is a generator. The room consumes it like
this:

1. Calls `stream(prompt)` on the actor's thread.
2. Reads chunks from the iterator.
3. Buffers the first 16 chars (configurable via
   `RoomConfig.pass_buffer_chars`) to detect the `[PASS]` prefix.
4. If the buffer matches `^\s*\[PASS\](\s|$)`, the stream is
   suppressed — no UI deltas, no canonical chat event, no
   rendering. The participant declined the floor.
5. Otherwise the buffer flushes and subsequent chunks render as
   deltas.
6. On terminal end, posts one canonical `chat` event + one
   `stream_end` control event with status `committed` / `passed` /
   `suppressed` / `cancelled` / `error` / `lease_expired`.

Empty iterables count as "no draft" — same effect as `[PASS]`.

**Implications for your adapter.**

- Yield strings. Other types are coerced via `str(...)`.
- Yield as many or as few chunks as you want. Single-shot
  `agent_from_send`-style adapters yield one full string.
- Don't pre-pend whitespace before a possible `[PASS]` — the regex
  allows leading whitespace, but consistency reads better in logs.
- Streamed errors: raise from inside `stream()`. The kernel catches
  the exception, scrubs secrets via `redact_error_text`, and posts
  `stream_end(status="error", error=<scrubbed>)`. See
  `docs/security-model.md` for the redaction patterns.
- `cancel()`: implement if your provider supports best-effort
  cancel. The kernel calls it when the lease is invalidated
  (mode/membership change, timeout). No-op if your provider can't
  cancel.

## 4. A custom streaming proxy class

When `agent_from_send` isn't enough — say you want true streaming
plus a `cancel()` that aborts the underlying HTTP request — write
the class directly:

```python
import threading
from typing import Iterator
from anthropic import Anthropic

class ClaudeProxy:
    """Minimal Claude adapter with streaming + cancel."""

    id: str
    persona: str
    capability_block: str
    cost_tier: int
    capable: bool

    def __init__(
        self,
        agent_id: str,
        api_key: str,
        *,
        model: str = "claude-3-5-sonnet-latest",
        persona: str = "",
        capability_block: str = "",
        cost_tier: int = 3,
    ) -> None:
        self.id = agent_id
        self.persona = persona
        self.capability_block = capability_block
        self.cost_tier = cost_tier
        self.capable = True
        self._client = Anthropic(api_key=api_key)
        self._model = model
        self._cancel = threading.Event()

    def stream(self, prompt: str) -> Iterator[str]:
        self._cancel.clear()
        with self._client.messages.stream(
            model=self._model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ) as response:
            for text in response.text_stream:
                if self._cancel.is_set():
                    return
                yield text

    def cancel(self) -> None:
        self._cancel.set()
```

Then plug it in:

```python
room = LoomRoom(
    agents=[ClaudeProxy("claude", api_key=os.environ["ANTHROPIC_API_KEY"])],
    policy=OpenChatPolicy(),
)
```

The room's `_agent_to_wiring` reads the optional attrs (`persona`,
`capability_block`, `cost_tier`, `capable`) via `getattr` and uses
your `stream()` as the proxy directly.

## 5. Configuration patterns

The canonical pattern: **constructor kwargs for static config + an
env-var fallback for secrets.**

```python
class CohereProxy:
    def __init__(
        self,
        agent_id: str,
        *,
        api_key: Optional[str] = None,
        model: str = "command-r",
        ...
    ) -> None:
        self._api_key = api_key or os.environ.get("LCP_COHERE_API_KEY")
        if not self._api_key:
            raise ValueError(
                "CohereProxy needs api_key= or LCP_COHERE_API_KEY env var")
        ...
```

Why:

- Constructor kwargs make the wiring explicit and testable.
- An env-var fallback for secrets keeps real keys out of source.
- A clear error message on missing config saves a debugging session.

Don't:

- Hardcode keys (obviously).
- Swallow `None` silently — fail loudly with a useful message.
- Read config at module import time (makes tests painful and
  startup error timing fragile).

## 6. Errors and secret leakage

Your provider exception bubbles out of `stream()`. The kernel posts
`stream_end(error=<text>)` and the journal records it.

The kernel scrubs known secret shapes via `redact_error_text` —
OpenAI keys, Bearer tokens, AWS keys, JWTs, Google OAuth tokens. If
your provider has a distinct key shape, register a custom scrubber
at startup:

```python
from loom.kernel.events import register_secret_scrubber
import re

register_secret_scrubber(
    re.compile(r"co_[A-Za-z0-9]{40}"),
    "[redacted-cohere-key]",
)
```

This runs at the kernel boundary; you don't need to scrub in your
adapter.

## 7. Testing

The simplest mock:

```python
class FakeProxy:
    id = "fake"
    def __init__(self, chunks=("hello",), raises=None):
        self._chunks = chunks
        self._raises = raises
    def stream(self, prompt):
        if self._raises:
            raise self._raises
        yield from self._chunks
```

`loom.testing.FakeProxy` (v0.1) ships this with extra knobs:
configurable PASS prefix, configurable cancel behavior, recording
support for round-trip tests.

For tests against a real provider that you don't want to burn API
calls on, `loom.testing.RecordReplayProxy` (v0.1) records on first
run and replays on subsequent. Until v0.1, `unittest.mock.patch` on
the underlying client works fine.

## 8. Cross-references

- `loom/adapters.py` — `agent_from_send` (canonical reference).
- `loom/contracts.py` — `Agent` Protocol.
- `loom/kernel/streaming.py` — streaming contract + PASS protocol.
- `loom/kernel/events.py` — `redact_error_text`,
  `register_secret_scrubber`.
- `docs/security-model.md` — secret-leakage posture (provider keys).
- `docs/loom-ux-spec.md` — kernel-wide UX contract.
