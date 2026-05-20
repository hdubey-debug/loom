"""Tests for ``loom.kernel.capabilities`` — v0.3 PR 5 capability ledger.

Doctrine: P1 (no CEO), P10 (capabilities are atomic verbs), §6
(capability ledger).

Test classes:

- :class:`CapabilityLedger` exercises the data structures (Grant,
  State, has, grants_for, effective_capabilities, find_expired,
  revoke, mark_expired).
- :class:`AntiEscalation` exercises the P1 invariant (agents may not
  grant meta-verbs; user may).
- :class:`CoordinatorWiresLedger` exercises end-to-end coordinator
  flow: ``_apply_effect(CapabilityGrantedEffect(...))`` updates the
  ledger; events round-trip through the journal envelope.
- :class:`CapabilityEvents` exercises the event constructors +
  validators.
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.capabilities import (
    CapabilityGrant,
    CapabilityName,
    CapabilityState,
    EscalationDenied,
    is_meta_capability,
    new_grant_id,
    register_capability_reducers,
)
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.effects import (
    CapabilityExpiredEffect,
    CapabilityGrantedEffect,
    CapabilityRevokedEffect,
    build_kernel_registry,
)
from loom.kernel.events import EventShapeError, Event
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState
from loom.kernel.state import new_kernel_state


def _grant(
    grantee_id: str = "loom",
    capability: CapabilityName = CapabilityName.SET_TOPIC,
    grantor_id: str = "user",
    expires_at=None,
) -> CapabilityGrant:
    return CapabilityGrant(
        grant_id=new_grant_id(),
        grantor_id=grantor_id,
        grantee_id=grantee_id,
        capability=capability,
        granted_at=100.0,
        expires_at=expires_at,
    )


class CapabilityLedger(unittest.TestCase):
    def test_capability_name_enum_member_counts(self):
        # v0.3.x PR 5: 11 mutation verbs (added SUMMARIZE, EMIT_SUMMARY)
        # + 11 GRANT_CAPABILITY_* + 11 REVOKE_CAPABILITY_* = 33.
        names = list(CapabilityName)
        self.assertEqual(len(names), 33)
        mutations = [n for n in names if not is_meta_capability(n)]
        self.assertEqual(len(mutations), 11)

    def test_add_grant_then_has(self):
        st = CapabilityState()
        g = _grant()
        st.add_grant(g)
        self.assertTrue(st.has("loom", CapabilityName.SET_TOPIC))

    def test_has_false_for_other_grantee(self):
        st = CapabilityState()
        st.add_grant(_grant(grantee_id="loom"))
        self.assertFalse(st.has("claude_code", CapabilityName.SET_TOPIC))

    def test_grants_for_returns_only_live(self):
        st = CapabilityState()
        active = _grant()
        st.add_grant(active)
        expiring = _grant(capability=CapabilityName.SET_ANCHOR, expires_at=200.0)
        st.add_grant(expiring)
        # At now=150 both are live.
        live = st.grants_for("loom", now=150.0)
        self.assertEqual(len(live), 2)
        # At now=300 the second has expired.
        live = st.grants_for("loom", now=300.0)
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].grant_id, active.grant_id)

    def test_effective_capabilities_aggregates(self):
        st = CapabilityState()
        st.add_grant(_grant(capability=CapabilityName.SET_TOPIC))
        st.add_grant(_grant(capability=CapabilityName.GRANT_FLOOR))
        eff = st.effective_capabilities("loom", now=150.0)
        self.assertEqual(
            eff,
            frozenset({CapabilityName.SET_TOPIC, CapabilityName.GRANT_FLOOR}),
        )

    def test_revoke_marks_grant_inactive(self):
        st = CapabilityState()
        g = _grant()
        st.add_grant(g)
        st.revoke(g.grant_id, now=200.0)
        self.assertFalse(st.has("loom", CapabilityName.SET_TOPIC, now=150.0))

    def test_revoke_idempotent(self):
        st = CapabilityState()
        g = _grant()
        st.add_grant(g)
        st.revoke(g.grant_id, now=200.0)
        # Second revoke must not raise.
        st.revoke(g.grant_id, now=300.0)

    def test_revoke_unknown_raises(self):
        st = CapabilityState()
        with self.assertRaises(KeyError):
            st.revoke("nope", now=100.0)

    def test_find_expired_returns_only_currently_expired(self):
        st = CapabilityState()
        st.add_grant(_grant(grantee_id="a", expires_at=100.0))
        st.add_grant(_grant(grantee_id="b", expires_at=200.0))
        st.add_grant(_grant(grantee_id="c"))  # no expiry
        expired = st.find_expired(now=150.0)
        # 'a' expired (100 < 150); 'b' and 'c' not.
        self.assertEqual(len(expired), 1)

    def test_find_expired_excludes_revoked(self):
        st = CapabilityState()
        g = _grant(expires_at=100.0)
        st.add_grant(g)
        st.revoke(g.grant_id, now=50.0)
        # Already revoked; should not show as expired.
        self.assertEqual(st.find_expired(now=200.0), [])

    def test_grant_id_collision_raises(self):
        st = CapabilityState()
        g = _grant()
        st.add_grant(g)
        with self.assertRaises(ValueError):
            st.add_grant(g)

    def test_is_live_treats_no_now_as_expired_for_expiring_grants(self):
        st = CapabilityState()
        g = _grant(expires_at=100.0)
        self.assertFalse(st.is_live(g, now=None))
        g2 = _grant()  # no expiry
        self.assertTrue(st.is_live(g2, now=None))


class AntiEscalation(unittest.TestCase):
    def _registry(self):
        reg = build_kernel_registry()
        register_capability_reducers(reg)
        return reg

    def test_user_may_grant_meta_capability(self):
        reg = self._registry()
        state = new_kernel_state(RoomState(config=RoomConfig()))
        effect = CapabilityGrantedEffect(
            grant_id="g1",
            grantee_id="loom",
            capability=CapabilityName.GRANT_CAPABILITY_SET_TOPIC.value,
            grantor_id="user",
        )
        # No exception expected.
        reg.apply(state, effect)
        self.assertTrue(state.capabilities is not None)
        self.assertEqual(len(state.capabilities.grants), 1)

    def test_agent_may_grant_mutation_capability(self):
        reg = self._registry()
        state = new_kernel_state(RoomState(config=RoomConfig()))
        effect = CapabilityGrantedEffect(
            grant_id="g2",
            grantee_id="claude_code",
            capability=CapabilityName.SET_TOPIC.value,
            grantor_id="loom",  # not 'user', but capability is non-meta.
        )
        reg.apply(state, effect)
        self.assertEqual(len(state.capabilities.grants), 1)

    def test_agent_cannot_grant_meta_capability(self):
        reg = self._registry()
        state = new_kernel_state(RoomState(config=RoomConfig()))
        effect = CapabilityGrantedEffect(
            grant_id="g3",
            grantee_id="loom",
            capability=CapabilityName.GRANT_CAPABILITY_SET_TOPIC.value,
            grantor_id="claude_code",  # NOT 'user'
        )
        with self.assertRaises(EscalationDenied):
            reg.apply(state, effect)
        # State unchanged.
        self.assertTrue(
            state.capabilities is None or len(state.capabilities.grants) == 0
        )

    def test_agent_cannot_self_promote_via_revoke_meta(self):
        reg = self._registry()
        state = new_kernel_state(RoomState(config=RoomConfig()))
        effect = CapabilityGrantedEffect(
            grant_id="g4",
            grantee_id="loom",
            capability=CapabilityName.REVOKE_CAPABILITY_SET_TOPIC.value,
            grantor_id="loom",
        )
        with self.assertRaises(EscalationDenied):
            reg.apply(state, effect)


class CoordinatorWiresLedger(unittest.TestCase):
    def _coord(self) -> RoomCoordinator:
        bus = MessageBus()
        state = RoomState(config=RoomConfig())
        state.add_participant(ParticipantInfo(id="loom"))
        return RoomCoordinator(bus, state)

    def test_coordinator_init_populates_capability_state(self):
        coord = self._coord()
        self.assertIsInstance(coord.kernel_state.capabilities, CapabilityState)
        self.assertEqual(len(coord.kernel_state.capabilities.grants), 0)

    def test_apply_capability_granted_effect_updates_ledger(self):
        coord = self._coord()
        effect = CapabilityGrantedEffect(
            grant_id="g1",
            grantee_id="loom",
            capability=CapabilityName.SET_TOPIC.value,
            grantor_id="user",
        )
        with coord._lock:
            coord._apply_effect(effect)
        self.assertTrue(
            coord.kernel_state.capabilities.has("loom", CapabilityName.SET_TOPIC)
        )

    def test_apply_capability_revoked_effect_removes_capability(self):
        coord = self._coord()
        granted = CapabilityGrantedEffect(
            grant_id="g1",
            grantee_id="loom",
            capability=CapabilityName.SET_TOPIC.value,
            grantor_id="user",
        )
        with coord._lock:
            coord._apply_effect(granted)
            coord._apply_effect(
                CapabilityRevokedEffect(grant_id="g1", revoker_id="user")
            )
        self.assertFalse(
            coord.kernel_state.capabilities.has(
                "loom", CapabilityName.SET_TOPIC, now=150.0
            )
        )

    def test_apply_capability_expired_marks_inactive(self):
        coord = self._coord()
        granted = CapabilityGrantedEffect(
            grant_id="g1",
            grantee_id="loom",
            capability=CapabilityName.SET_TOPIC.value,
            grantor_id="user",
            expires_at=100.0,
        )
        with coord._lock:
            coord._apply_effect(granted)
            coord._apply_effect(CapabilityExpiredEffect(grant_id="g1"))
        # The grant is now marked revoked-at-expiry; not in effective set.
        self.assertFalse(
            coord.kernel_state.capabilities.has(
                "loom", CapabilityName.SET_TOPIC, now=200.0
            )
        )


class CapabilityEvents(unittest.TestCase):
    def test_capability_granted_constructor(self):
        e = ev.capability_granted(
            grant_id="g1",
            grantor_id="user",
            grantee_id="loom",
            capability=CapabilityName.SET_TOPIC.value,
            source_event_id=42,
        )
        self.assertEqual(ev.control_type_of(e), "capability_granted")
        self.assertEqual(e.body["grant_id"], "g1")
        self.assertEqual(e.body["grantor_id"], "user")
        self.assertEqual(e.body["capability"], "SET_TOPIC")
        self.assertEqual(e.body["source_event_id"], 42)

    def test_capability_granted_round_trip(self):
        e = ev.capability_granted(
            grant_id="g1",
            grantor_id="user",
            grantee_id="loom",
            capability=CapabilityName.SET_TOPIC.value,
            expires_at=200.0,
            source_event_id=42,
        )
        e.id, e.ts = 1, 1.0
        loaded = Event.from_jsonl(e.to_jsonl())
        self.assertEqual(loaded.body, e.body)

    def test_capability_revoked_constructor(self):
        e = ev.capability_revoked(grant_id="g1", revoker_id="user", reason="manual")
        self.assertEqual(ev.control_type_of(e), "capability_revoked")
        self.assertEqual(e.body["reason"], "manual")

    def test_capability_expired_constructor(self):
        e = ev.capability_expired(grant_id="g1")
        self.assertEqual(ev.control_type_of(e), "capability_expired")
        self.assertEqual(e.body["grant_id"], "g1")

    def test_validator_rejects_missing_grant_id(self):
        # The shape validator runs at from_jsonl time. Build a
        # synthetic JSON line that omits grant_id.
        import json
        line = json.dumps(
            {
                "kind": "control",
                "sender": "system",
                "body": {"control_type": "capability_revoked", "revoker_id": "user"},
                "channel": "main",
                "addressees": [],
                "room_epoch": 0,
                "user_turn_id": None,
                "meta": {},
                "id": 0,
                "ts": 1.0,
            }
        )
        with self.assertRaises(EventShapeError):
            Event.from_jsonl(line)

    def test_validator_accepts_well_formed_capability_granted(self):
        e = ev.capability_granted(
            grant_id="g1",
            grantor_id="user",
            grantee_id="loom",
            capability="SET_TOPIC",
            source_event_id=1,
        )
        e.id, e.ts = 0, 0.0
        # round-trip exercises the validator.
        Event.from_jsonl(e.to_jsonl())


class ReplayDeterminism(unittest.TestCase):
    """Replay of capability_* events reconstructs the same CapabilityState."""

    def test_grant_revoke_replay_matches_live(self):
        reg = build_kernel_registry()
        register_capability_reducers(reg)
        live = new_kernel_state(RoomState(config=RoomConfig()))
        replayed = new_kernel_state(RoomState(config=RoomConfig()))
        seq = [
            CapabilityGrantedEffect(
                grant_id="g1",
                grantee_id="loom",
                capability="SET_TOPIC",
                grantor_id="user",
            ),
            CapabilityGrantedEffect(
                grant_id="g2",
                grantee_id="claude_code",
                capability="GRANT_FLOOR",
                grantor_id="user",
            ),
            CapabilityRevokedEffect(grant_id="g1", revoker_id="user"),
        ]
        for effect in seq:
            reg.apply(live, effect)
        for effect in seq:
            reg.apply(replayed, effect)
        # The grant ledgers should be value-equal.
        self.assertEqual(live.capabilities.grants, replayed.capabilities.grants)


if __name__ == "__main__":
    unittest.main()
