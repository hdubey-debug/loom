"""Shared fixtures for tests/coverage/.

Minimal: just a watchdog. The targeted tests don't need the heavy
adversarial / multi-room fixtures from subsystem and system tiers —
they exercise specific code paths via monkeypatch.
"""

from __future__ import annotations

import signal
import threading

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "watchdog(seconds): override the default 30s watchdog ceiling"
    )


_DEFAULT_WATCHDOG_SECONDS = 30


class _WatchdogFired(Exception):
    pass


def _signal_alarm_available() -> bool:
    return (
        hasattr(signal, "SIGALRM")
        and hasattr(signal, "alarm")
        and threading.current_thread() is threading.main_thread()
    )


@pytest.fixture(autouse=True)
def coverage_watchdog(request: pytest.FixtureRequest):
    seconds = _DEFAULT_WATCHDOG_SECONDS
    marker = request.node.get_closest_marker("watchdog")
    if marker:
        if marker.kwargs.get("seconds") is not None:
            seconds = int(marker.kwargs["seconds"])
        elif marker.args:
            seconds = int(marker.args[0])

    if _signal_alarm_available():
        prev = signal.getsignal(signal.SIGALRM)

        def _handler(signum, frame):  # noqa: ARG001
            raise _WatchdogFired(f"watchdog fired after {seconds}s in {request.node.nodeid}")

        signal.signal(signal.SIGALRM, _handler)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, prev)
    else:
        yield
