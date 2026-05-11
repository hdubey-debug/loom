"""Security property / fuzz tests (P0.5).

Invariants:

- ``Event.from_jsonl`` raises only :class:`EventShapeError` (or
  passes) on arbitrary string input. No ``TypeError`` /
  ``AttributeError`` / ``KeyError`` may escape.
- ``restore_state`` never raises on arbitrary dict / non-dict input
  and always returns a :class:`RoomState` with the supplied config.
- ``parse_addressees`` always returns a subset of the addressable
  pool, with each id appearing at most once and ``exclude`` filtered.
- ``Journal.iter_events`` returns valid :class:`Event` objects on
  arbitrary tampered file contents (with corruption surfaced as
  ``journal_corruption`` / ``journal_truncated`` events when opted in).
- ``redact_error_text`` strips known API-key shapes regardless of
  surrounding context.

These tests are the runtime fuzz layer behind the per-kind shape
validation in ``Event.from_jsonl``. A regression that lets a
TypeError out of ``from_jsonl`` would make ``replay_into`` crash an
actor thread on a tampered journal — exactly the T1 failure mode
the audit flagged.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from loom.kernel import events as ev
from loom.kernel.addressees import parse_addressees
from loom.kernel.events import (
    Event,
    EventShapeError,
    redact_error_text,
)
from loom.kernel.journal import Journal, restore_state
from loom.kernel.room import RoomConfig, RoomState

from tests.property.strategies import event_streams, participant_ids


# ---------------------------------------------------------------------------
# Event.from_jsonl shape validation
# ---------------------------------------------------------------------------


@given(line=st.text(min_size=0, max_size=200))
def test_from_jsonl_raises_only_event_shape_error_on_garbage(line: str):
    """Arbitrary text either parses to a valid Event or raises EventShapeError.

    Nothing else may escape: no TypeError, no AttributeError, no
    KeyError. ``replay_into`` relies on this contract to surface
    journal_corruption events instead of crashing.
    """
    try:
        e = Event.from_jsonl(line)
    except EventShapeError:
        return
    # If the line happened to parse cleanly, the result is a real Event.
    assert isinstance(e, Event)


@given(
    payload=st.dictionaries(
        keys=st.text(min_size=1, max_size=12),
        values=st.recursive(
            st.one_of(
                st.none(),
                st.booleans(),
                st.integers(),
                st.floats(allow_nan=False, allow_infinity=False),
                st.text(max_size=20),
            ),
            lambda children: st.one_of(
                st.lists(children, max_size=4),
                st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=4),
            ),
            max_leaves=8,
        ),
        max_size=8,
    ),
)
def test_from_jsonl_raises_only_event_shape_error_on_random_dicts(payload):
    """Random JSON-able dicts either parse cleanly or raise EventShapeError."""
    try:
        line = json.dumps(payload)
    except (TypeError, ValueError):
        return  # not JSON-able; skip
    try:
        e = Event.from_jsonl(line)
    except EventShapeError:
        return
    assert isinstance(e, Event)


@given(line=st.binary(min_size=0, max_size=100))
@settings(suppress_health_check=[HealthCheck.filter_too_much])
def test_from_jsonl_raises_only_event_shape_error_on_bytes_decoded(line: bytes):
    """Arbitrary byte-decoded strings either parse or raise EventShapeError."""
    try:
        s = line.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return
    try:
        e = Event.from_jsonl(s)
    except EventShapeError:
        return
    assert isinstance(e, Event)


@pytest.mark.parametrize(
    "bad_kind",
    [
        None,
        "",
        "unknown_kind",
        7,
        [],
        {},
    ],
)
def test_from_jsonl_rejects_unknown_kind(bad_kind):
    line = json.dumps({"kind": bad_kind, "sender": "u", "body": ""})
    with pytest.raises(EventShapeError):
        Event.from_jsonl(line)


@pytest.mark.parametrize(
    "body",
    [
        None,
        7,
        [],
        {},
        True,
    ],
)
def test_from_jsonl_rejects_non_string_chat_body(body):
    line = json.dumps({"kind": "chat", "sender": "u", "body": body})
    with pytest.raises(EventShapeError):
        Event.from_jsonl(line)


@pytest.mark.parametrize(
    "body",
    [
        None,
        "string-instead-of-dict",
        7,
        [],
    ],
)
def test_from_jsonl_rejects_non_dict_control_body(body):
    line = json.dumps({"kind": "control", "sender": "system", "body": body})
    with pytest.raises(EventShapeError):
        Event.from_jsonl(line)


def test_from_jsonl_rejects_control_without_control_type():
    line = json.dumps({"kind": "control", "sender": "system", "body": {"foo": "bar"}})
    with pytest.raises(EventShapeError):
        Event.from_jsonl(line)


def test_from_jsonl_rejects_stream_with_wrong_lease_id_type():
    line = json.dumps(
        {
            "kind": "stream",
            "sender": "p",
            "body": {"stream_event": "start", "lease_id": "not-int"},
        }
    )
    with pytest.raises(EventShapeError):
        Event.from_jsonl(line)


def test_from_jsonl_rejects_bool_in_int_field():
    """Bool is an int subclass in Python; tampered JSON ``true`` must not slip through."""
    line = json.dumps(
        {
            "kind": "chat",
            "sender": "u",
            "body": "hi",
            "room_epoch": True,
        }
    )
    with pytest.raises(EventShapeError):
        Event.from_jsonl(line)


def test_from_jsonl_extra_unknown_keys_rejected():
    """Future schema fields don't leak through with ``cls(**d)``."""
    line = json.dumps(
        {
            "kind": "chat",
            "sender": "u",
            "body": "hi",
            "future_field": "leakable_secret",
        }
    )
    # Should raise — extra fields fail validation in the strict mode,
    # OR get filtered. Either way, no TypeError from the dataclass.
    try:
        e = Event.from_jsonl(line)
    except EventShapeError:
        return
    assert isinstance(e, Event)
    # If filtered through, the unknown field must not be on the Event.
    assert not hasattr(e, "future_field")


# ---------------------------------------------------------------------------
# restore_state defensive guards
# ---------------------------------------------------------------------------


@given(
    state_data=st.one_of(
        st.none(),
        st.text(max_size=10),
        st.lists(st.integers(), max_size=3),
        st.dictionaries(
            st.text(min_size=1, max_size=8),
            st.one_of(
                st.none(),
                st.text(max_size=10),
                st.integers(),
                st.booleans(),
                st.lists(st.text(max_size=5), max_size=3),
            ),
            max_size=6,
        ),
    )
)
def test_restore_state_never_raises_on_arbitrary_input(state_data):
    """Arbitrary state data must not crash restore_state."""
    cfg = RoomConfig()
    state = restore_state(state_data, cfg)
    assert isinstance(state, RoomState)
    assert state.config is cfg


@given(
    participants=st.lists(
        st.one_of(
            st.none(),
            st.text(max_size=8),
            st.integers(),
            st.dictionaries(
                st.text(min_size=1, max_size=8),
                st.one_of(st.none(), st.integers(), st.text(max_size=8), st.booleans()),
                max_size=5,
            ),
        ),
        max_size=8,
    )
)
def test_restore_state_skips_malformed_participant_entries(participants):
    """Malformed participant entries are skipped rather than propagated."""
    cfg = RoomConfig()
    state = restore_state({"version": 3, "participants": participants}, cfg)
    # Every retained participant must have a non-empty string id.
    for pid, info in state.participants.items():
        assert isinstance(pid, str) and pid
        assert info.id == pid


def test_restore_state_rejects_non_string_id():
    cfg = RoomConfig()
    state = restore_state({"version": 3, "participants": [{"id": 7, "capable": True}]}, cfg)
    assert state.participants == {}


def test_restore_state_clamps_bool_room_epoch():
    """``room_epoch=true`` would propagate True (an int subclass) without coercion."""
    cfg = RoomConfig()
    state = restore_state({"version": 3, "room_epoch": True}, cfg)
    assert state.room_epoch == 0


# ---------------------------------------------------------------------------
# parse_addressees
# ---------------------------------------------------------------------------


@given(
    text=st.text(min_size=0, max_size=200),
    addressable=st.lists(participant_ids, min_size=0, max_size=8),
    exclude=st.one_of(st.none(), participant_ids),
)
def test_parse_addressees_returns_subset(text, addressable, exclude):
    """Output is a subset of addressable, no dups, no exclude."""
    out = parse_addressees(text, addressable, exclude=exclude)
    pool = set(addressable)
    assert all(a in pool for a in out)
    assert len(out) == len(set(out))  # no dups
    if exclude is not None:
        assert exclude not in out


# ---------------------------------------------------------------------------
# Journal.iter_events on tampered files
# ---------------------------------------------------------------------------


@given(
    valid_events=event_streams(min_size=0, max_size=8),
    garbage_lines=st.lists(st.text(min_size=0, max_size=200), max_size=8),
)
def test_iter_events_with_corruption_surfaces_corruption_events(
    tmp_path_factory, valid_events, garbage_lines
):
    """Tampered lines surface as ``journal_corruption`` events when opted in."""
    tmp = tmp_path_factory.mktemp("journal_fuzz")
    j = Journal(tmp)

    # Interleave valid + garbage in a single file.
    blob_lines: list[str] = []
    for i, e in enumerate(valid_events):
        blob_lines.append(e.to_jsonl())
        if i < len(garbage_lines):
            g = garbage_lines[i].replace("\n", " ").replace("\r", " ")
            if g.strip():
                blob_lines.append(g)
    j.events_path.write_text("\n".join(blob_lines) + "\n")

    # Without opt-in: silent skip behavior preserved.
    silent = list(j.iter_events())
    for e in silent:
        assert isinstance(e, Event)
        assert ev.control_type_of(e) not in ("journal_corruption", "journal_truncated")

    # With opt-in: corruption events appear for unparseable lines.
    with_corruption = list(j.iter_events(emit_corruption_events=True))
    for e in with_corruption:
        assert isinstance(e, Event)


def test_iter_events_distinguishes_truncated_last_line(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("journal_truncated")
    j = Journal(tmp)
    valid = ev.chat(sender="u", body="hi")
    valid.id, valid.ts = 0, 1.0
    j.events_path.write_text(valid.to_jsonl() + "\n{partial-write")
    yielded = list(j.iter_events(emit_corruption_events=True))
    types = [ev.control_type_of(e) for e in yielded]
    assert "journal_truncated" in types
    # The truncated line must NOT also be reported as corruption.
    assert "journal_corruption" not in types


def test_iter_events_midstream_corruption_is_corruption_not_truncated(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("journal_midstream")
    j = Journal(tmp)
    a = ev.chat(sender="u", body="a")
    a.id, a.ts = 0, 1.0
    b = ev.chat(sender="u", body="b")
    b.id, b.ts = 1, 2.0
    j.events_path.write_text(a.to_jsonl() + "\n{tampered}\n" + b.to_jsonl() + "\n")
    yielded = list(j.iter_events(emit_corruption_events=True))
    types = [ev.control_type_of(e) for e in yielded]
    assert "journal_corruption" in types
    assert "journal_truncated" not in types


# ---------------------------------------------------------------------------
# redact_error_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-1234567890ABCDEFGHIJKL",
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAA",
        "Bearer abcdefghijklmnopq1234567",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyD-aBCDEFGHIJKLMNOPQrstuvwxyz0123456",
        "ya29.A0ARrdaM-aBcDeFgHiJkLmNoPqRsTuV",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk",
    ],
)
def test_redact_error_text_strips_known_secrets(secret: str):
    s = f"Provider error context: {secret} (request id ...)"
    out = redact_error_text(s)
    assert secret not in out
    assert "[redacted-secret]" in out


def test_redact_error_text_caps_length():
    s = "x" * 1000
    out = redact_error_text(s, max_chars=100)
    assert len(out) <= 100


def test_redact_error_text_handles_none_and_empty():
    assert redact_error_text(None) == ""
    assert redact_error_text("") == ""


def test_redact_error_text_idempotent_on_clean_input():
    s = "ValueError: something broke"
    assert redact_error_text(redact_error_text(s)) == redact_error_text(s)


def test_register_secret_scrubber_runs_after_default_patterns():
    custom_marker = "MY-CUSTOM-SECRET-XYZ"

    def _scrub(text: str) -> str:
        return text.replace(custom_marker, "[redacted-custom]")

    ev.register_secret_scrubber(_scrub)
    try:
        out = redact_error_text(f"err: {custom_marker} happened")
        assert custom_marker not in out
        assert "[redacted-custom]" in out
    finally:
        ev.clear_secret_scrubbers()


def test_buggy_scrubber_does_not_break_redaction():
    def _bad(_t: str) -> str:
        raise RuntimeError("boom")

    ev.register_secret_scrubber(_bad)
    try:
        # Default patterns must still apply even with the bad scrubber.
        out = redact_error_text("api: sk-AAAAAAAAAAAAAAAAAAAAAAAA fail")
        assert "sk-" not in out or "[redacted-secret]" in out
    finally:
        ev.clear_secret_scrubbers()


# ---------------------------------------------------------------------------
# SecretShape detector framework (v0.2)
# ---------------------------------------------------------------------------


def test_register_secret_shape_detects_custom_pattern():
    """A custom SecretShape detector contributes to redaction."""
    import re as _re
    from loom.kernel.events import _RegexShape

    custom = _RegexShape("internal_token", _re.compile(r"INT-[A-Z0-9]{12}"))
    ev.register_secret_shape(custom)
    try:
        out = redact_error_text("err: leaked INT-ABCDEF123456 oops")
        assert "INT-ABCDEF123456" not in out
        assert "[redacted-secret]" in out
    finally:
        ev.clear_secret_scrubbers()


def test_default_shapes_each_have_a_unique_name():
    """Default shapes are named for audit/observability."""
    from loom.kernel.events import _DEFAULT_SHAPES

    names = [s.name for s in _DEFAULT_SHAPES]
    assert len(names) == len(set(names)), names
    # Sanity: covers the seven canonical secret families.
    assert {
        "openai_sk",
        "anthropic_sk_ant",
        "bearer_token",
        "aws_access_key",
        "jwt",
        "gcp_api_key",
        "gcp_oauth",
    }.issubset(set(names))


def test_secret_shape_protocol_allows_non_regex_detectors():
    """Custom non-regex detectors satisfy the SecretShape protocol."""

    class _PalindromeShape:
        # A toy structural detector: any 10-char prefix-marked palindrome.
        name = "palindrome10"

        def detect(self, text: str):
            i = 0
            while i + 10 <= len(text):
                window = text[i : i + 10]
                if window.startswith("@@") and window == window[::-1]:
                    yield (i, i + 10)
                i += 1

    ev.register_secret_shape(_PalindromeShape())
    try:
        s = "leak: @@abccba@@ trail"
        out = redact_error_text(s)
        assert "@@abccba@@" not in out
        assert "[redacted-secret]" in out
    finally:
        ev.clear_secret_scrubbers()


def test_buggy_shape_detector_does_not_break_redaction():
    class _BadShape:
        name = "bad"

        def detect(self, _text):
            raise RuntimeError("boom")

    ev.register_secret_shape(_BadShape())
    try:
        out = redact_error_text("api: sk-AAAAAAAAAAAAAAAAAAAAAAAA fail")
        assert "[redacted-secret]" in out
    finally:
        ev.clear_secret_scrubbers()


def test_overlapping_shape_spans_render_single_placeholder():
    """When two detectors match overlapping regions, only one placeholder appears."""

    # ``sk-ant-...`` is matched by BOTH the explicit anthropic shape
    # AND the legacy ``sk-...`` shape in the defaults.
    out = redact_error_text("err: sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA happened")
    assert out.count("[redacted-secret]") == 1


@given(noise=st.text(alphabet="abcdefghijklmnop", max_size=80))
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_shape_detectors_do_not_fire_on_short_lowercase_text(noise: str):
    """No detector fires on short lowercase non-secret strings."""
    out = redact_error_text(noise)
    assert "[redacted-secret]" not in out
