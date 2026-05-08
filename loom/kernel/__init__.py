"""Loom kernel — mechanism only.

Modules here implement the protocol mechanism: events, bus, room state,
obligations, user turns, journal, addressees, streaming, coordinator,
actor, prompt assembly. They MUST NOT import from :mod:`loom.policy`.

Kernel modules MAY import from :mod:`loom.contracts` (the
:class:`ConversationPolicy` ABC) — that module is neutral with respect
to the kernel/policy boundary by design.
"""
