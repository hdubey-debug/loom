"""Property: kernel capability invariants under random pressure (P4.2).

Invariants this file locks in:

- **Sender forgery is rejected.** A thread bound to actor ``X`` cannot
  post an event with any sender other than ``X`` (audit C1, P1).
  ``post_internal`` is the only privileged escape.
- **DM privacy holds at the visibility gate.** ``bus.snapshot(audience=Y)``
  for ``Y`` not in ``{target, "user", "system"}`` never returns events
  on ``dm:target`` (audit D1).
- **Render-memo is keyed by ``(ev.id, scope)``.** A chat event rendered
  under ``scope="main"`` and ``scope="dm"`` produces distinct cached
  strings, both of which contain the body verbatim — confirms the
  D2 invariant about audience-scoped rendering.
"""
from __future__ import annotations

import threading

import pytest
from hypothesis import given, strategies as st

from loom.kernel import events as ev
from loom.kernel.bus import (
    MessageBus,
    SenderMismatchError,
    visible_to,
)
from tests.property.strategies import participant_ids


# ---------------------------------------------------------------------------
# Sender authentication (Option B)
# ---------------------------------------------------------------------------

@given(actor_id=participant_ids,
       forged_sender=st.one_of(
           st.just("user"),
           st.just("system"),
           participant_ids,
       ),
       body=st.text(max_size=40))
def test_bound_thread_cannot_forge_sender(actor_id, forged_sender, body):
    """Any sender that is not the bound actor id is rejected."""
    if forged_sender == actor_id:
        return  # vacuously fine (it's not a forgery)
    bus = MessageBus()
    unbind = bus.bind_actor(actor_id)
    try:
        with pytest.raises(SenderMismatchError):
            bus.post(ev.chat(sender=forged_sender, body=body))
    finally:
        unbind()


@given(actor_id=participant_ids, body=st.text(max_size=40))
def test_bound_thread_can_post_own_sender(actor_id, body):
    """Posting with sender == bound id always works."""
    bus = MessageBus()
    unbind = bus.bind_actor(actor_id)
    try:
        rid = bus.post(ev.chat(sender=actor_id, body=body))
        assert rid == 0
    finally:
        unbind()


@given(actor_id=participant_ids,
       sender=st.one_of(st.just("user"), st.just("system"),
                         participant_ids))
def test_post_internal_bypasses_binding(actor_id, sender):
    """``post_internal`` posts any sender even on a bound thread."""
    bus = MessageBus()
    unbind = bus.bind_actor(actor_id)
    try:
        rid = bus.post_internal(ev.chat(sender=sender, body="x"))
        assert rid == 0
    finally:
        unbind()


def test_unbind_removes_check():
    """Unbinding restores unrestricted posting on the thread."""
    bus = MessageBus()
    unbind = bus.bind_actor("alice")
    try:
        with pytest.raises(SenderMismatchError):
            bus.post(ev.chat(sender="bob", body="x"))
    finally:
        unbind()
    # After unbind, sender is unconstrained.
    rid = bus.post(ev.chat(sender="bob", body="x"))
    assert rid == 0


def test_rebinding_to_different_actor_raises():
    bus = MessageBus()
    bus.bind_actor("alice")
    try:
        with pytest.raises(RuntimeError):
            bus.bind_actor("bob")
    finally:
        bus.unbind_actor()


def test_concurrent_actor_threads_each_see_own_binding():
    """Two threads bound to different actors don't see each other's binding."""
    bus = MessageBus()
    bound_a, bound_b = [], []

    def actor_a():
        unbind = bus.bind_actor("alice")
        try:
            bound_a.append(bus.bound_actor_for())
            # alice should be able to post as alice but not as bob
            try:
                bus.post(ev.chat(sender="bob", body="forged"))
                bound_a.append("FAIL")
            except SenderMismatchError:
                bound_a.append("ok-rejected")
            bus.post(ev.chat(sender="alice", body="legit"))
        finally:
            unbind()

    def actor_b():
        unbind = bus.bind_actor("bob")
        try:
            bound_b.append(bus.bound_actor_for())
            bus.post(ev.chat(sender="bob", body="legit-b"))
        finally:
            unbind()

    ta = threading.Thread(target=actor_a)
    tb = threading.Thread(target=actor_b)
    ta.start()
    tb.start()
    ta.join(timeout=2.0)
    tb.join(timeout=2.0)

    assert bound_a[0] == "alice"
    assert "ok-rejected" in bound_a
    assert bound_b[0] == "bob"


# ---------------------------------------------------------------------------
# DM privacy
# ---------------------------------------------------------------------------

@given(target=participant_ids,
       other=participant_ids,
       body=st.text(max_size=40))
def test_dm_never_visible_to_non_target(target, other, body):
    if other == target:
        return  # not a privacy violation
    bus = MessageBus()
    bus.post_internal(ev.chat(
        sender="user", body=body, channel=f"dm:{target}"))
    snap = bus.snapshot(audience=other)
    for e in snap:
        assert e.channel != f"dm:{target}"


@given(target=participant_ids, body=st.text(max_size=40))
def test_dm_visible_to_target_user_system(target, body):
    bus = MessageBus()
    bus.post_internal(ev.chat(
        sender="user", body=body, channel=f"dm:{target}"))
    for audience in (target, "user", "system"):
        snap = bus.snapshot(audience=audience)
        assert any(e.channel == f"dm:{target}" for e in snap)


@given(channel=st.text(min_size=1, max_size=20).filter(
    lambda s: not s.startswith("dm:") and s != "main"))
def test_unknown_channel_visible_to_nobody(channel):
    """A non-main / non-dm channel is treated as private to nobody."""
    e = ev.chat(sender="user", body="x", channel=channel)
    assert not visible_to(e, "user")
    assert not visible_to(e, "system")
    assert not visible_to(e, "anyone")


# ---------------------------------------------------------------------------
# Render memo D2 contract
# ---------------------------------------------------------------------------

@given(body=st.text(min_size=1, max_size=40))
def test_render_memo_distinct_per_scope(body):
    """``main`` and ``dm`` scopes produce distinct, stable cached renders."""
    import json as _json
    bus = MessageBus()
    e = ev.chat(sender="alice", body=body)
    bus.post_internal(e)
    main_a = bus.render_chat_line(e, scope="main")
    main_b = bus.render_chat_line(e, scope="main")
    dm_a = bus.render_chat_line(e, scope="dm")
    dm_b = bus.render_chat_line(e, scope="dm")
    # Idempotent within a scope.
    assert main_a is main_b
    assert dm_a is dm_b
    # Distinct across scopes (the ``scope`` field embeds the value).
    if body.strip():
        assert main_a != dm_a
    # Both renders parse back to a JSON object with the body intact —
    # guards against any JSON-escape mismatch (control chars get
    # ``\\uXXXX`` encoded so a substring check on the raw string would
    # fail; round-tripping through ``json.loads`` recovers the body).
    assert _json.loads(main_a)["body"] == body
    assert _json.loads(dm_a)["body"] == body
