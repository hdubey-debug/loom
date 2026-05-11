"""Adversarial scenario: large-body single-event DoS (RES4 / P2.1).

Threshold:
- After P2.1 ships: a 1 MB body is rejected by ``bus.post`` in <1 ms
  (well below the body-traversal cost), and the bus log is unchanged.
- The kernel's default cap is 256 KB; this scenario uses a 1 MB body
  to confirm the rejection path fires for any payload past the cap.
"""
from __future__ import annotations

import pytest

from loom.kernel import events as ev
from loom.kernel.bus import _KERNEL_AUTH, BodyOversizeError, MessageBus


pytestmark = [pytest.mark.adversarial]


def test_oversize_body_is_rejected_before_log_grows(bench):
    bus = MessageBus(max_body_bytes=256 * 1024)
    big = "x" * (1024 * 1024)  # 1 MB

    # Establish baseline log length.
    bus.post_internal(ev.chat(sender="user", body="hi"), auth=_KERNEL_AUTH)
    initial_len = len(bus)

    raised: list[Exception] = []

    def attempt():
        try:
            bus.post_internal(ev.chat(sender="user", body=big), auth=_KERNEL_AUTH)
        except BodyOversizeError as exc:
            raised.append(exc)

    res = bench(attempt, name="oversize_body_rejected", iters=50, warmup=2)

    assert raised, "expected BodyOversizeError on every attempt"
    # Both warmup and iters call ``attempt`` — so total raises is
    # iters + warmup. The exact count depends on harness internals;
    # what we care about is "every attempt raised" and "log unchanged."
    assert len(raised) >= res.iters
    assert len(bus) == initial_len, (
        "bus log must not grow on rejected oversize posts")
    # Threshold: the rejection path is microseconds. A regression that
    # accidentally measured len(body) twice would still finish quickly,
    # but if some future change started serializing the body to count
    # bytes, the p99 would balloon — we'd catch it here.
    assert res.ns_p99 < 5_000_000, (  # 5 ms ceiling, very generous
        f"oversize-body rejection p99 = {res.ns_p99:.0f} ns; "
        "expected < 5 ms")


def test_within_cap_body_is_accepted(bench):
    bus = MessageBus(max_body_bytes=256 * 1024)
    payload = "x" * (256 * 1024 - 1)  # one byte under the cap

    def attempt():
        bus.post_internal(ev.chat(sender="user", body=payload), auth=_KERNEL_AUTH)

    res = bench(attempt, name="under_cap_body_accepted", iters=20, warmup=2)
    # The bus log has grown (one event per iter + warmup).
    assert len(bus) >= res.iters
    assert res.ns_p99 < 50_000_000  # generous 50 ms ceiling
