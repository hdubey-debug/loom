"""Loom error types — single import surface for user-catchable exceptions.

The base class :class:`LoomError` lets callers write a catch-all
without naming each kernel-defined error individually:

.. code-block:: python

    from loom.errors import LoomError

    try:
        room.post(text)
    except LoomError as exc:
        ...

The kernel-defined exceptions also continue to inherit from
:class:`ValueError` so existing ``except ValueError`` code keeps
working (back-compat with v0.0). New code should prefer
:class:`LoomError`.

This module is the canonical import path for error types. The
exceptions themselves live next to the code that raises them
(``loom.kernel.events``, ``loom.kernel.bus``); this module is a thin
re-export so users don't need to know the kernel layout.

Design note: the typed exceptions are exposed via lazy ``__getattr__``
so this module stays a true leaf — kernel modules can ``from
loom.errors import LoomError`` without a circular import. Users see the
same surface either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


class LoomError(Exception):
    """Base class for all Loom-defined exceptions.

    Library authors who want a single ``except`` clause covering every
    kernel-raised error import this and catch it. The concrete kernel
    exception types (:class:`EventShapeError`, :class:`BodyOversizeError`,
    :class:`SenderMismatchError`) inherit from this AND from
    :class:`ValueError`, so legacy callers using ``except ValueError``
    continue to catch them.

    User code SHOULD NOT raise :class:`LoomError` directly — it exists
    as a tag for the typed subclasses below.
    """


if TYPE_CHECKING:  # pragma: no cover - for static checkers only
    from loom.kernel.bus import (  # noqa: F401
        BodyOversizeError,
        SenderMismatchError,
    )
    from loom.kernel.events import EventShapeError  # noqa: F401


_LAZY_RE_EXPORTS = {
    "EventShapeError": ("loom.kernel.events", "EventShapeError"),
    "BodyOversizeError": ("loom.kernel.bus", "BodyOversizeError"),
    "SenderMismatchError": ("loom.kernel.bus", "SenderMismatchError"),
}


def __getattr__(name: str):
    """Lazy re-export of typed kernel exceptions.

    Avoids a circular import: kernel modules import :class:`LoomError`
    from this module at module-load time; this module only resolves
    the kernel-side classes when a caller asks for them.
    """
    target = _LAZY_RE_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'loom.errors' has no attribute {name!r}")
    module_name, attr = target
    import importlib

    return getattr(importlib.import_module(module_name), attr)


__all__ = [
    "LoomError",
    "EventShapeError",
    "BodyOversizeError",
    "SenderMismatchError",
]
