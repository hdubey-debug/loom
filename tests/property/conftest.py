"""Shared fixtures + Hypothesis profile registration for tests/property/.

Profiles:
- ``fast``    25 examples, 1s deadline — inner-loop runs.
- ``ci``      100 examples, 2s deadline — default; runs in fast suite.
- ``nightly`` 2000 examples, 10s deadline — release-rhythm runs.

Profile selection via ``HYPOTHESIS_PROFILE`` env var; defaults to ``ci``.
"""

from __future__ import annotations

import os
import signal
import threading

import pytest
from hypothesis import HealthCheck, settings


settings.register_profile("fast", max_examples=25, deadline=1000)
settings.register_profile("ci", max_examples=100, deadline=2000)
settings.register_profile(
    "nightly", max_examples=2000, deadline=10_000, suppress_health_check=[HealthCheck.too_slow]
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "property: Hypothesis property-based test")
    config.addinivalue_line("markers", "watchdog(seconds): override the default watchdog ceiling")


_DEFAULT_WATCHDOG_SECONDS = 60


class _WatchdogFired(Exception):
    pass


def _signal_alarm_available() -> bool:
    return (
        hasattr(signal, "SIGALRM")
        and hasattr(signal, "alarm")
        and threading.current_thread() is threading.main_thread()
    )


@pytest.fixture(autouse=True)
def property_watchdog(request: pytest.FixtureRequest):
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
