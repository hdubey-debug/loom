"""Property: ``Event.meta`` is never rendered into prompts (audit T5).

The ``meta`` field on :class:`Event` is free-form, journaled, and has
no schema. Today no code path renders it into prompts (verified via
``grep`` of ``prompt.py`` for ``\\.meta``). This test locks that in:
arbitrary meta values must NOT appear in any prompt rendering.

If a future change starts pulling ``meta`` into prompts, this test
will fail loudly and force the author to either:

1. Apply ``_render_system_field``-style fencing to the meta surface
   (defending PI-style injection through meta).
2. Filter to a known-safe key whitelist before rendering.

Either way, the change is forced through review rather than slipping
in silently.
"""
from __future__ import annotations

from hypothesis import given, strategies as st

from loom.kernel import events as ev
from loom.kernel.bus import _KERNEL_AUTH, MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.prompt import build_prompt
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState


_MARKER_KEYS = ["secret_key", "internal_flag", "audit_only_marker"]
_MARKER_VALUES = [
    "META_SHOULD_NOT_APPEAR_IN_PROMPT",
    "secret-meta-payload-XYZ",
    "audit-leak-canary-12345",
]


def _build_session():
    bus = MessageBus()
    cfg = RoomConfig()
    state = RoomState(config=cfg)
    coord = RoomCoordinator(bus, state)
    coord.register_participant(ParticipantInfo(id="alice"))
    coord.register_participant(ParticipantInfo(id="bob"))
    return bus, coord


@given(meta_key=st.sampled_from(_MARKER_KEYS),
       meta_value=st.sampled_from(_MARKER_VALUES),
       sender=st.sampled_from(["alice", "bob", "user"]),
       body=st.text(min_size=1, max_size=40))
def test_chat_event_meta_does_not_leak_into_prompt(
        meta_key, meta_value, sender, body):
    """Meta keys and values do not appear in the rendered prompt body."""
    bus, coord = _build_session()
    chat = ev.chat(sender=sender, body=body,
                   meta={meta_key: meta_value})
    bus.post_internal(chat, auth=_KERNEL_AUTH)
    prompt = build_prompt("alice", chat, coord)
    # The body itself is rendered (that's the transcript) — that's fine.
    # But meta keys/values must NOT be present anywhere.
    assert meta_key not in prompt, (
        f"meta key {meta_key!r} leaked into prompt; review whether "
        "you want to apply _render_system_field fencing or a key "
        "whitelist.")
    assert meta_value not in prompt, (
        f"meta value {meta_value!r} leaked into prompt; same review "
        "as for the key.")


def test_summary_event_meta_does_not_leak_into_prompt():
    bus, coord = _build_session()
    summary = ev.summary(
        body="prior summary content",
        meta={"compaction_marker": "META_LEAK_CANARY_SUM"},
    )
    bus.post_internal(summary, auth=_KERNEL_AUTH)
    prompt = build_prompt("alice", None, coord)
    assert "compaction_marker" not in prompt
    assert "META_LEAK_CANARY_SUM" not in prompt
