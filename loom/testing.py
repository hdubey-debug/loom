"""Loom test helpers — public fixtures for policy and adapter authors.

Authors of :class:`~loom.contracts.ConversationPolicy` subclasses and
:class:`~loom.contracts.Agent` adapters can import from this module to
build minimal test scaffolding without reaching into ``loom.kernel.*``.

Provided helpers:

- :func:`make_test_state` — construct a :class:`RoomStateView` from a
  compact spec; the read-only view is what
  :meth:`ConversationPolicy.plan_user_turn` actually receives.
- :func:`make_test_event` — wrap :func:`loom.kernel.events.chat` with
  test-friendly defaults.
- :class:`FakeProxy` — minimal :class:`~loom.kernel.streaming.StreamingProxy`
  that yields a fixed list of chunks and supports raise / cancel.
- :func:`assert_no_state_mutation` — context manager that snapshots a
  :class:`RoomStateView` and re-checks key fields on exit. Catches
  policies that mutate state through the (deliberately partial) freeze.
- :class:`RecordReplayProxy` — record real provider output once,
  replay deterministically thereafter. Use to pin adapter tests
  without burning API quota on every CI run.

These helpers are intentionally minimal. Tests that exercise the
journal, coordinator, or full session lifecycle should construct
those objects directly — the kernel internals are still imported by
test code in those cases.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Any,
    Iterator,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from loom.kernel import events as _ev
from loom.kernel.events import Event
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomState,
    RoomStateView,
)


# ---------------------------------------------------------------------------
# State / event builders
# ---------------------------------------------------------------------------

ParticipantSpec = Union[
    str,  # "pid"  -> active=True, capable=True
    ParticipantInfo,  # passthrough
    Mapping[str, Any],  # {"id": "pid", "active": False, ...}
    Sequence,  # ("pid", active, capable[, cost_tier])
]


def _build_participant(spec: ParticipantSpec) -> ParticipantInfo:
    if isinstance(spec, ParticipantInfo):
        return spec
    if isinstance(spec, str):
        return ParticipantInfo(id=spec)
    if isinstance(spec, Mapping):
        return ParticipantInfo(**spec)
    seq = list(spec)
    pid = seq[0]
    fields = ("active", "capable", "cost_tier")
    kwargs: dict[str, Any] = {}
    for name, value in zip(fields, seq[1:]):
        kwargs[name] = value
    return ParticipantInfo(id=pid, **kwargs)


def make_test_state(
    *participants: ParticipantSpec,
    config: Optional[RoomConfig] = None,
    topic: Optional[str] = None,
    default_responder: Optional[str] = None,
    anchor: Optional[str] = None,
) -> RoomStateView:
    """Build a :class:`RoomStateView` from compact participant specs.

    Each spec is one of:

    - ``"pid"``                — all defaults (active, capable, tier 0).
    - :class:`ParticipantInfo` — used as-is.
    - ``{"id": "pid", ...}``   — kwargs forwarded to ``ParticipantInfo``.
    - ``("pid", True, True)``  — positional (active, capable, cost_tier).

    Returns a :class:`RoomStateView` (the read-only contract a policy
    actually receives), not the live :class:`RoomState`. Use
    :meth:`RoomState.view` directly if you need to construct state
    incrementally.
    """
    state = RoomState(config=config or RoomConfig())
    for spec in participants:
        state.add_participant(_build_participant(spec))
    if topic is not None:
        state.topic = topic
    if default_responder is not None:
        state.set_default_responder(default_responder)
    if anchor is not None:
        state.anchor_id = anchor
    return state.view()


def make_test_event(
    body: str = "hi",
    *,
    sender: str = "user",
    id: Optional[int] = 1,
    channel: str = "main",
    addressees: Optional[list[str]] = None,
    user_turn_id: Optional[int] = None,
    room_epoch: int = 0,
) -> Event:
    """Build a chat :class:`Event` with test-friendly defaults.

    The bus normally assigns ``id`` on post; here it is set explicitly
    (default ``1``) so policies that branch on ``user_event.id`` work
    out of the box. Pass ``id=None`` to leave it unassigned, or
    ``id=0`` to exercise the first-post-on-bus boundary.
    """
    e = _ev.chat(
        sender=sender,
        body=body,
        channel=channel,
        addressees=addressees,
        user_turn_id=user_turn_id,
        room_epoch=room_epoch,
    )
    if id is not None:
        e.id = id
    return e


# ---------------------------------------------------------------------------
# Streaming proxies
# ---------------------------------------------------------------------------


class FakeProxy:
    """Minimal :class:`~loom.kernel.streaming.StreamingProxy` for tests.

    Yields a fixed list of chunks. Optionally raises at a chosen
    chunk index, or raises immediately on ``stream()``.
    Records whether ``cancel()`` was called via the ``cancelled`` flag.

    Example::

        proxy = FakeProxy(["Hello", " world"])
        list(proxy.stream("anything"))  # ["Hello", " world"]

        proxy = FakeProxy(["partial"], raises_at=0)
        for chunk in proxy.stream("x"): ...   # raises RuntimeError

        proxy = FakeProxy([], raises=ValueError("boom"))
        next(proxy.stream("x"))  # raises ValueError("boom")
    """

    def __init__(
        self,
        chunks: Sequence[str] = (),
        *,
        raises: Optional[BaseException] = None,
        raises_at: Optional[int] = None,
    ) -> None:
        self.chunks: list[str] = list(chunks)
        self.raises = raises
        self.raises_at = raises_at
        self.cancelled: bool = False
        self.last_prompt: Optional[str] = None

    def stream(self, prompt: str) -> Iterator[str]:
        self.last_prompt = prompt
        if self.raises is not None and self.raises_at is None:
            raise self.raises
        for i, chunk in enumerate(self.chunks):
            if self.cancelled:
                return
            if self.raises_at is not None and i == self.raises_at:
                raise self.raises or RuntimeError(f"FakeProxy: raise at chunk {i}")
            yield chunk

    def cancel(self) -> None:
        self.cancelled = True


# ---------------------------------------------------------------------------
# State-mutation guard
# ---------------------------------------------------------------------------


def _snapshot_view(view: RoomStateView) -> dict[str, Any]:
    """Capture mutation-relevant fields of a :class:`RoomStateView`.

    The view itself is frozen, but ``ParticipantInfo`` values are
    still mutable (documented soft leak). This helper records the
    fields a buggy policy would most plausibly write to so that
    :func:`assert_no_state_mutation` can detect post-hoc mutation.
    """
    pmap: dict[str, tuple] = {}
    for pid, info in view.participants.items():
        pmap[pid] = (info.active, info.capable, info.cost_tier)
    ctrl = view.control
    return {
        "topic": view.topic,
        "anchor_id": view.anchor_id,
        "chair_id": view.chair_id,
        "default_responder_id": view.default_responder_id,
        "participants": pmap,
        "control_wait_for_user": ctrl.wait_for_user,
        "control_style": ctrl.style,
        "control_turn_order": ctrl.turn_order,
        "control_next_speaker_idx": ctrl.next_speaker_idx,
        "control_roles": dict(ctrl.roles),
    }


@contextmanager
def assert_no_state_mutation(view: RoomStateView) -> Iterator[None]:
    """Assert that a code block did not mutate ``view``.

    Snapshots key fields on entry and compares on exit. Raises
    :class:`AssertionError` if any tracked field changed. Use to
    pin a policy against accidental writes through the (intentionally
    partial) view freeze::

        view = make_test_state("a", "b")
        with assert_no_state_mutation(view):
            plan = MyPolicy().plan_user_turn(make_test_event(), view)
    """
    before = _snapshot_view(view)
    yield
    after = _snapshot_view(view)
    if before != after:
        diff = [f"{k}: {before[k]!r} -> {after[k]!r}" for k in before if before[k] != after[k]]
        raise AssertionError(
            "RoomStateView mutated under assert_no_state_mutation:\n  " + "\n  ".join(diff)
        )


# ---------------------------------------------------------------------------
# Record / replay proxy
# ---------------------------------------------------------------------------


class RecordReplayProxy:
    """Adapter test harness — record real provider chunks once, replay forever.

    On first run (no recording file at ``path``), wraps an inner
    :class:`~loom.kernel.streaming.StreamingProxy` and records each
    ``stream(prompt)`` call's chunks into a JSONL file keyed by
    ``prompt``. On subsequent runs (file exists), replays the recorded
    chunks deterministically — no ``inner`` is required.

    Mode selection:

    - ``mode="auto"`` (default) — replay if ``path`` exists, else
      record.
    - ``mode="record"`` — always re-record (overwrites).
    - ``mode="replay"`` — replay only; raise if file missing or
      prompt unrecorded.

    The recording is keyed on the literal prompt string. If your
    prompt embeds a timestamp or a random nonce, normalize before
    recording or your replays will miss.

    Example::

        # First CI run: wrap a real proxy, record.
        proxy = RecordReplayProxy(
            "tests/fixtures/openai_smoke.jsonl",
            inner=OpenAIProxy(api_key=...),
        )
        # Subsequent runs: replays from disk; no inner needed.
        proxy = RecordReplayProxy("tests/fixtures/openai_smoke.jsonl")
    """

    def __init__(
        self,
        path: Union[str, os.PathLike],
        *,
        inner: Any = None,
        mode: str = "auto",
    ) -> None:
        if mode not in ("auto", "record", "replay"):
            raise ValueError(f"RecordReplayProxy: mode must be auto|record|replay, got {mode!r}")
        self.path = Path(path)
        self.inner = inner
        if mode == "auto":
            mode = "replay" if self.path.exists() else "record"
        self.mode = mode
        self._cache: dict[str, list[str]] = {}
        if mode == "replay":
            if not self.path.exists():
                raise FileNotFoundError(
                    f"RecordReplayProxy: replay mode but no recording at {self.path}"
                )
            self._cache = self._load(self.path)
        elif mode == "record":
            if inner is None:
                raise ValueError("RecordReplayProxy: record mode requires an inner proxy")
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _load(path: Path) -> dict[str, list[str]]:
        cache: dict[str, list[str]] = {}
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                cache[rec["prompt"]] = list(rec["chunks"])
        return cache

    def _append(self, prompt: str, chunks: list[str]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"prompt": prompt, "chunks": chunks},
                    ensure_ascii=False,
                )
            )
            fh.write("\n")

    def stream(self, prompt: str) -> Iterator[str]:
        if self.mode == "replay":
            chunks = self._cache.get(prompt)
            if chunks is None:
                raise KeyError(
                    f"RecordReplayProxy: no recorded reply for prompt "
                    f"(len={len(prompt)}). Re-record or check key drift."
                )
            for c in chunks:
                yield c
            return
        # record mode
        captured: list[str] = []
        for c in self.inner.stream(prompt):
            captured.append(c)
            yield c
        self._cache[prompt] = captured
        self._append(prompt, captured)

    def cancel(self) -> None:
        if self.mode == "record" and self.inner is not None:
            cancel = getattr(self.inner, "cancel", None)
            if cancel is not None:
                cancel()


__all__ = [
    "make_test_state",
    "make_test_event",
    "FakeProxy",
    "assert_no_state_mutation",
    "RecordReplayProxy",
    "ParticipantSpec",
]
