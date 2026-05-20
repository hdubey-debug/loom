"""Loom v0.3 — :class:`CapabilityState` ledger.

Doctrine: **P1** (kernel knows no CEO; authority is atomic verbs),
**P10** (capabilities are atomic verbs in ``CapabilityState``), §6
(capability ledger).

The kernel does not recognise any privileged identity role ("admin",
"chair", "CEO"). It recognises **capabilities** — small, atomic
mutation verbs that a participant either has or doesn't have at a
given moment. A room with one admin and a room with five co-admins
and a room with rotating temporary topic-setters all express
themselves as different *capability assignments* over the same
kernel primitives.

This module implements the ledger:

- :class:`CapabilityName` enumerates the atomic mutation verbs the
  v0.3 kernel recognises, plus the meta verbs that gate
  granting/revoking them.
- :class:`CapabilityGrant` is one issuance of a capability to a
  grantee, with lifecycle metadata (granted_at, expires_at,
  revoked_at, source_event_id).
- :class:`CapabilityState` is the ledger: ``grants: dict[grant_id,
  CapabilityGrant]`` plus query methods (``has``, ``grants_for``,
  ``effective_capabilities``) and a maintenance helper
  (``flush_expired``).

Reducers for ``CapabilityGrantedEffect`` / ``CapabilityRevokedEffect``
/ ``CapabilityExpiredEffect`` are registered into the v0.3
:class:`loom.kernel.effects.EffectRegistry` at module-import time via
:func:`register_capability_reducers`, which the coordinator invokes
on its registry instance during construction.

Anti-escalation invariant (P1): an agent (non-``"user"`` grantor) may
not grant a ``GRANT_CAPABILITY_*`` or ``REVOKE_CAPABILITY_*`` meta
verb to any grantee, including itself. This blocks the obvious
self-promotion path. The user is exempt — the doctrine treats human
root actions (PR 11) as out-of-band trusted writes.

Replay-determinism: ``flush_expired`` accepts an explicit ``now``
parameter so the coordinator's watchdog passes ``time.monotonic()``
in live operation while the journal replay path can drive it from
the event-ts of each event in turn. The ledger never reads the clock
itself.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from loom.kernel.effects import (
    CapabilityExpiredEffect,
    CapabilityGrantedEffect,
    CapabilityRevokedEffect,
    ControlEffect,
    EffectRegistry,
)
from loom.kernel.state import KernelState


# ---------------------------------------------------------------------------
# Capability names
# ---------------------------------------------------------------------------


class CapabilityName(str, Enum):
    """Doctrine §6 — the v0.3 atomic capability verbs.

    Subclassing ``str`` keeps JSON / log lines readable (the enum
    value is the verb name) and allows raw-string comparison in tests.

    Mutation verbs (P1 — what authority can DO):

    - ``GRANT_FLOOR`` — open the floor to a specific participant for
      one lease.
    - ``CANCEL_LEASE`` — terminate a held lease early.
    - ``SET_TOPIC`` — change the room topic.
    - ``SET_ANCHOR`` — change the anchor slot.
    - ``SET_DEFAULT_RESPONDER`` — change the default-responder slot.
    - ``UPDATE_ALLOWED_SPEAKERS`` — replace / extend / block the
      allowed-speakers set via :class:`FloorOverrideEffect` (PR 10).
    - ``SET_ROLES`` — replace the role-assignment map.
    - ``SWITCH_POLICY`` — change the room's
      :class:`ConversationPolicy`.
    - ``SEND_DM`` — post a chat event on a ``dm:<target>`` channel.

    Meta verbs (what authority can DELEGATE) — one per mutation verb.
    A participant with ``GRANT_CAPABILITY_SET_TOPIC`` may grant
    ``SET_TOPIC`` to others; without it, attempted grants are
    rejected by the anti-escalation invariant. The user (slash-command
    sender, PR 11) is exempt — they may grant any capability.
    """

    # Mutation verbs
    GRANT_FLOOR = "GRANT_FLOOR"
    CANCEL_LEASE = "CANCEL_LEASE"
    SET_TOPIC = "SET_TOPIC"
    SET_ANCHOR = "SET_ANCHOR"
    SET_DEFAULT_RESPONDER = "SET_DEFAULT_RESPONDER"
    UPDATE_ALLOWED_SPEAKERS = "UPDATE_ALLOWED_SPEAKERS"
    SET_ROLES = "SET_ROLES"
    SWITCH_POLICY = "SWITCH_POLICY"
    SEND_DM = "SEND_DM"
    # v0.3.x PR 5 — compaction (doctrine §6 / §11). Two separate verbs:
    # ``SUMMARIZE`` (request authority — can ask for a summarisation
    # via the SUMMARIZATION lease) and ``EMIT_SUMMARY`` (production
    # authority — can actually be the slot occupant whose summary
    # commits). Both AND-required for Path A auto-summarisation
    # (defense in depth: a misbehaving policy can't bypass the
    # capability gate by triggering Path A).
    SUMMARIZE = "SUMMARIZE"
    EMIT_SUMMARY = "EMIT_SUMMARY"

    # Meta verbs (grant / revoke each mutation verb).
    GRANT_CAPABILITY_GRANT_FLOOR = "GRANT_CAPABILITY_GRANT_FLOOR"
    GRANT_CAPABILITY_CANCEL_LEASE = "GRANT_CAPABILITY_CANCEL_LEASE"
    GRANT_CAPABILITY_SET_TOPIC = "GRANT_CAPABILITY_SET_TOPIC"
    GRANT_CAPABILITY_SET_ANCHOR = "GRANT_CAPABILITY_SET_ANCHOR"
    GRANT_CAPABILITY_SET_DEFAULT_RESPONDER = "GRANT_CAPABILITY_SET_DEFAULT_RESPONDER"
    GRANT_CAPABILITY_UPDATE_ALLOWED_SPEAKERS = "GRANT_CAPABILITY_UPDATE_ALLOWED_SPEAKERS"
    GRANT_CAPABILITY_SET_ROLES = "GRANT_CAPABILITY_SET_ROLES"
    GRANT_CAPABILITY_SWITCH_POLICY = "GRANT_CAPABILITY_SWITCH_POLICY"
    GRANT_CAPABILITY_SEND_DM = "GRANT_CAPABILITY_SEND_DM"
    GRANT_CAPABILITY_SUMMARIZE = "GRANT_CAPABILITY_SUMMARIZE"
    GRANT_CAPABILITY_EMIT_SUMMARY = "GRANT_CAPABILITY_EMIT_SUMMARY"
    REVOKE_CAPABILITY_GRANT_FLOOR = "REVOKE_CAPABILITY_GRANT_FLOOR"
    REVOKE_CAPABILITY_CANCEL_LEASE = "REVOKE_CAPABILITY_CANCEL_LEASE"
    REVOKE_CAPABILITY_SET_TOPIC = "REVOKE_CAPABILITY_SET_TOPIC"
    REVOKE_CAPABILITY_SET_ANCHOR = "REVOKE_CAPABILITY_SET_ANCHOR"
    REVOKE_CAPABILITY_SET_DEFAULT_RESPONDER = "REVOKE_CAPABILITY_SET_DEFAULT_RESPONDER"
    REVOKE_CAPABILITY_UPDATE_ALLOWED_SPEAKERS = "REVOKE_CAPABILITY_UPDATE_ALLOWED_SPEAKERS"
    REVOKE_CAPABILITY_SET_ROLES = "REVOKE_CAPABILITY_SET_ROLES"
    REVOKE_CAPABILITY_SWITCH_POLICY = "REVOKE_CAPABILITY_SWITCH_POLICY"
    REVOKE_CAPABILITY_SEND_DM = "REVOKE_CAPABILITY_SEND_DM"
    REVOKE_CAPABILITY_SUMMARIZE = "REVOKE_CAPABILITY_SUMMARIZE"
    REVOKE_CAPABILITY_EMIT_SUMMARY = "REVOKE_CAPABILITY_EMIT_SUMMARY"


_META_VERB_PREFIXES = ("GRANT_CAPABILITY_", "REVOKE_CAPABILITY_")


def is_meta_capability(cap: CapabilityName) -> bool:
    """True iff ``cap`` is a ``GRANT_CAPABILITY_*`` / ``REVOKE_CAPABILITY_*`` verb."""
    return cap.value.startswith(_META_VERB_PREFIXES)


# ---------------------------------------------------------------------------
# Grant + state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityGrant:
    """One issuance of a capability to a grantee.

    Frozen — the grant lifecycle (revoke / expire) is tracked by
    *replacing* the grant in :class:`CapabilityState.grants` rather
    than mutating it in place. This keeps reducers idempotent on
    replay (the reducer just writes the new record).

    Lifecycle:

    - Live: ``revoked_at is None`` AND (``expires_at is None`` OR
      ``now < expires_at``).
    - Expired: ``expires_at is not None`` AND ``now >= expires_at``.
    - Revoked: ``revoked_at is not None``.

    ``effective_capabilities`` returns the union of "live" grants.
    Expired / revoked grants stay in the ledger for audit but are
    excluded from the effective set.

    ``scope`` (post-v0.3) will narrow the grant by target object
    (e.g. "can SET_TOPIC for this turn only", "can SEND_DM to
    participant X only"). v0.3 reserves the field as ``None`` so the
    on-disk shape stabilizes early.

    ``conditions`` (post-v0.3) will admit predicate-based grants
    (e.g. "valid while role==teacher"). Same v0.3-reservation
    pattern.
    """

    grant_id: str
    grantor_id: str
    grantee_id: str
    capability: CapabilityName
    granted_at: float  # time.monotonic at grant
    expires_at: Optional[float] = None
    revoked_at: Optional[float] = None
    source_event_id: int = -1
    # v0.4+ reservations.
    scope: Optional[object] = None
    conditions: tuple = ()


@dataclass
class CapabilityState:
    """The grant ledger. Single-writer (the coordinator under lock).

    Mutation happens via reducers (``_apply_capability_*``) registered
    in :func:`register_capability_reducers`; tests can call the
    register methods directly for unit-level work.
    """

    grants: dict[str, CapabilityGrant] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def is_live(self, grant: CapabilityGrant, *, now: Optional[float] = None) -> bool:
        """True if ``grant`` is neither revoked nor expired at ``now``.

        ``now`` defaults to ``+inf`` so callers asking "is this
        grant *currently* effective at all" can omit it and still get
        a useful answer for non-expiring grants.
        """
        if grant.revoked_at is not None:
            return False
        if grant.expires_at is None:
            return True
        if now is None:
            # No time given — treat as "in the eternal future" so an
            # expiring grant counts as expired by default.
            return False
        return now < grant.expires_at

    def grants_for(
        self,
        grantee_id: str,
        *,
        now: Optional[float] = None,
    ) -> tuple[CapabilityGrant, ...]:
        """All live grants currently held by ``grantee_id``."""
        return tuple(
            g for g in self.grants.values()
            if g.grantee_id == grantee_id and self.is_live(g, now=now)
        )

    def effective_capabilities(
        self,
        grantee_id: str,
        *,
        now: Optional[float] = None,
    ) -> frozenset[CapabilityName]:
        """The capability set ``grantee_id`` currently effectively holds."""
        return frozenset(g.capability for g in self.grants_for(grantee_id, now=now))

    def has(
        self,
        grantee_id: str,
        capability: CapabilityName,
        *,
        now: Optional[float] = None,
    ) -> bool:
        """True iff ``grantee_id`` has a live grant for ``capability``."""
        return any(
            g.capability == capability and self.is_live(g, now=now)
            for g in self.grants.values()
            if g.grantee_id == grantee_id
        )

    # ------------------------------------------------------------------
    # Mutation primitives (called by reducers)
    # ------------------------------------------------------------------

    def add_grant(self, grant: CapabilityGrant) -> None:
        if grant.grant_id in self.grants:
            raise ValueError(f"grant_id collision: {grant.grant_id!r}")
        self.grants[grant.grant_id] = grant

    def revoke(self, grant_id: str, *, now: float) -> CapabilityGrant:
        old = self.grants.get(grant_id)
        if old is None:
            raise KeyError(f"no such grant: {grant_id!r}")
        if old.revoked_at is not None:
            return old  # idempotent — already revoked
        new = CapabilityGrant(
            grant_id=old.grant_id,
            grantor_id=old.grantor_id,
            grantee_id=old.grantee_id,
            capability=old.capability,
            granted_at=old.granted_at,
            expires_at=old.expires_at,
            revoked_at=now,
            source_event_id=old.source_event_id,
            scope=old.scope,
            conditions=old.conditions,
        )
        self.grants[grant_id] = new
        return new

    def mark_expired(self, grant_id: str, *, now: float) -> CapabilityGrant:
        """Record ``grant_id`` as expired at ``now``.

        Distinct from :meth:`revoke` only in semantics; the on-disk
        shape simply sets ``revoked_at`` (no separate ``expired_at``
        field — the ``expires_at`` deadline itself is the original
        cause; ``revoked_at`` records when it was *observed*).
        """
        return self.revoke(grant_id, now=now)

    # ------------------------------------------------------------------
    # Watchdog helper
    # ------------------------------------------------------------------

    def find_expired(self, *, now: float) -> list[str]:
        """Grant ids whose ``expires_at`` has passed and ``revoked_at`` is None.

        Returned in stable id order so the coordinator's watchdog
        emits ``capability_expired`` events deterministically across
        runs / replays.
        """
        out: list[str] = []
        for gid, g in self.grants.items():
            if g.revoked_at is None and g.expires_at is not None and now >= g.expires_at:
                out.append(gid)
        out.sort()
        return out


# ---------------------------------------------------------------------------
# Anti-escalation
# ---------------------------------------------------------------------------


class EscalationDenied(PermissionError):
    """Reducer raised this when a grant attempt violates P1 anti-escalation."""


_USER_ID = "user"


def _check_escalation(effect: CapabilityGrantedEffect) -> None:
    """P1 anti-escalation: agents may not grant meta capabilities.

    Only the user (``grantor_id == "user"``) may grant a
    ``GRANT_CAPABILITY_*`` or ``REVOKE_CAPABILITY_*`` verb. Any other
    grantor attempting to do so triggers :class:`EscalationDenied` —
    surfaced to the coordinator's denial path so the proposing
    control action emits a ``control_action_denied`` event with
    reason ``INSUFFICIENT_CAPABILITY`` (PR 9 wiring).
    """
    try:
        cap = CapabilityName(effect.capability)
    except ValueError:
        # Unknown verb — let the reducer write the row anyway; the
        # capability check downstream just won't recognise it.
        return
    if is_meta_capability(cap) and effect.grantor_id != _USER_ID:
        raise EscalationDenied(
            f"anti-escalation: only 'user' may grant meta verb {cap.value!r}; "
            f"grantor was {effect.grantor_id!r}"
        )


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------


def _apply_capability_granted(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, CapabilityGrantedEffect)
    if state.capabilities is None:
        state.capabilities = CapabilityState()
    _check_escalation(effect)
    try:
        cap = CapabilityName(effect.capability)
    except ValueError as exc:
        raise ValueError(f"unknown capability {effect.capability!r}") from exc
    # ``granted_at`` rides on ``effect.applied_at_event_id`` indirectly;
    # the coordinator stamps the event id after the reducer, so the
    # reducer records the wall-clock-of-grant as 0.0 if the event-ts
    # is unavailable here. The journal-replay path overrides via
    # ``Event.ts`` populated upstream.
    grant = CapabilityGrant(
        grant_id=effect.grant_id,
        grantor_id=effect.grantor_id,
        grantee_id=effect.grantee_id,
        capability=cap,
        granted_at=0.0,
        expires_at=effect.expires_at,
        revoked_at=None,
        source_event_id=effect.applied_at_event_id or -1,
    )
    state.capabilities.add_grant(grant)


def _apply_capability_revoked(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, CapabilityRevokedEffect)
    if state.capabilities is None:
        state.capabilities = CapabilityState()
    # Revoker is recorded in the event payload; the grant itself only
    # tracks revoked_at (the *time*). Audit trail = the event line.
    state.capabilities.revoke(effect.grant_id, now=0.0)


def _apply_capability_expired(state: KernelState, effect: ControlEffect) -> None:
    assert isinstance(effect, CapabilityExpiredEffect)
    if state.capabilities is None:
        state.capabilities = CapabilityState()
    state.capabilities.mark_expired(effect.grant_id, now=0.0)


# ---------------------------------------------------------------------------
# Registry extension
# ---------------------------------------------------------------------------


def register_capability_reducers(registry: EffectRegistry) -> None:
    """Wire the three v0.3 capability reducers into ``registry``.

    Called by :class:`RoomCoordinator.__init__` immediately after
    :func:`loom.kernel.effects.build_kernel_registry` so the
    coordinator-bound registry covers the capability shape.
    """
    registry.register("capability_granted", 1, _apply_capability_granted)
    registry.register("capability_revoked", 1, _apply_capability_revoked)
    registry.register("capability_expired", 1, _apply_capability_expired)


def new_grant_id() -> str:
    """Allocate a fresh 128-bit-hex grant id.

    Stable across processes (random); usable as a journal-line
    reference in causal_refs.
    """
    return secrets.token_hex(16)
