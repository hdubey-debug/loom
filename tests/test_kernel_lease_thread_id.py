"""Tests for v0.3.x PR 1 — LeaseContext.thread_id + emit helpers.

Doctrine: P21. Each of the 5 v0.3 LeaseContext subclasses carries a
``thread_id: str = "main"`` field; the coordinator's emit helpers
inherit it onto events posted under the lease.
"""

from __future__ import annotations

import unittest

from loom.kernel import events as ev
from loom.kernel.bus import MessageBus
from loom.kernel.coordinator import RoomCoordinator
from loom.kernel.events import Event
from loom.kernel.leases import (
    ControlActionContext,
    Lease,
    LeaseKind,
    ReactiveContext,
    ToolInvocationContext,
    UserTurnContext,
    WorkflowStepContext,
)
from loom.kernel.room import ParticipantInfo, RoomConfig, RoomState


def _coord() -> RoomCoordinator:
    bus = MessageBus()
    state = RoomState(config=RoomConfig())
    state.add_participant(ParticipantInfo(id="loom"))
    return RoomCoordinator(bus, state)


class ContextFieldsHaveThreadId(unittest.TestCase):
    def test_user_turn_context_default_thread_main(self):
        c = UserTurnContext(user_turn_id=1, trigger_event_id=0, room_epoch=0)
        self.assertEqual(c.thread_id, "main")

    def test_user_turn_context_explicit_thread(self):
        c = UserTurnContext(user_turn_id=1, trigger_event_id=0, room_epoch=0, thread_id="t9")
        self.assertEqual(c.thread_id, "t9")

    def test_control_action_context_default_thread(self):
        self.assertEqual(ControlActionContext(action_name="SET_TOPIC").thread_id, "main")

    def test_tool_invocation_context_default_thread(self):
        self.assertEqual(ToolInvocationContext(tool_name="x").thread_id, "main")

    def test_workflow_step_context_default_thread(self):
        self.assertEqual(WorkflowStepContext(workflow_id="w", step_id="s").thread_id, "main")

    def test_reactive_context_default_thread(self):
        self.assertEqual(ReactiveContext(reason="r").thread_id, "main")


class EmitHelpers(unittest.TestCase):
    def test_emit_system_default_main(self):
        coord = _coord()
        e = ev.system("note")
        coord._emit_system(e)
        self.assertEqual(e.thread_id, "main")

    def test_emit_system_explicit_thread(self):
        coord = _coord()
        e = ev.system("note")
        coord._emit_system(e, thread_id="t9")
        self.assertEqual(e.thread_id, "t9")

    def test_emit_under_lease_inherits_context_thread(self):
        coord = _coord()
        lease = Lease(
            id=1,
            kind=LeaseKind.REACTIVE,
            holder="loom",
            context=ReactiveContext(reason="x", thread_id="debate-2"),
            acquired_at=0.0,
            expires_at=0.0,
        )
        e = ev.system("note")
        coord._emit_under_lease(lease, e)
        self.assertEqual(e.thread_id, "debate-2")

    def test_emit_under_lease_main_context_leaves_default(self):
        coord = _coord()
        lease = Lease(
            id=1,
            kind=LeaseKind.REACTIVE,
            holder="loom",
            context=ReactiveContext(reason="x"),
            acquired_at=0.0,
            expires_at=0.0,
        )
        e = ev.system("note")
        coord._emit_under_lease(lease, e)
        self.assertEqual(e.thread_id, "main")

    def test_emit_under_lease_does_not_overwrite_explicit_event_thread(self):
        # If the caller pre-set a non-default thread_id, the helper
        # leaves it alone.
        coord = _coord()
        lease = Lease(
            id=1,
            kind=LeaseKind.REACTIVE,
            holder="loom",
            context=ReactiveContext(reason="x", thread_id="from-lease"),
            acquired_at=0.0,
            expires_at=0.0,
        )
        e = Event(kind="system", sender="system", body="x", thread_id="from-event")
        coord._emit_under_lease(lease, e)
        self.assertEqual(e.thread_id, "from-event")


if __name__ == "__main__":
    unittest.main()
