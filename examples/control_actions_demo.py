"""v0.3 demo — slash commands, capabilities, and control actions.

Run::

    PYTHONPATH=. python examples/control_actions_demo.py

What you'll see:

1. **User-issued ``/topic``** changes the room topic immediately.
   ``dispatch_slash_command`` runs with ``proposer_id="user"`` which
   bypasses the agent capability gate per doctrine P15.

2. **Agent without a capability** trying to ``SET_TOPIC`` is denied
   (``INSUFFICIENT_CAPABILITY``).

3. **Kernel-side grant** of ``SET_TOPIC`` to ``claude_code`` via the
   direct ``CapabilityGrantedEffect`` path. The slash command
   ``/grant`` parses correctly today but its ``GRANT_CAPABILITY``
   action handler is on the v0.4 list — the underlying machinery is
   already live, so we exercise it via the effect path. The kernel
   API used here is ``loom.kernel.*`` and is therefore "advanced
   surface" — ordinary apps wait for the v0.4 slash-command wiring.

4. **The same agent**, now holding ``SET_TOPIC``, dispatches its own
   ``SET_TOPIC`` control action — granted.

5. We print every ``capability_*`` and ``control_action_*`` event
   that landed on the bus so you can see the full lifecycle.
"""

from __future__ import annotations

from loom import LoomRoom, OpenChatPolicy, agent_from_send
from loom.kernel.capabilities import CapabilityName
from loom.kernel.effects import CapabilityGrantedEffect
from loom.kernel.room import ParticipantInfo
from loom.slash_commands import dispatch_slash_command


def claude_send(prompt: str) -> str:
    return "claude_code: [thinks] noted."


def main() -> None:
    agent = agent_from_send("claude_code", claude_send, persona="orchestrator")
    room = LoomRoom(agents=[agent], policy=OpenChatPolicy(), topic="kick-off")

    with room:
        coord = room._session.coordinator
        bus = room._session.bus
        # Register "user" as a participant so the universal
        # _ParticipantRegisteredCheck passes for proposer_id="user".
        # The v0.4 facade will likely do this automatically.
        coord.register_participant(ParticipantInfo(id="user", capable=False))
        print(f"initial topic: {room.topic!r}")

        # 1. User /topic via slash command — bypasses capability gate (P15).
        r1 = dispatch_slash_command(coord, "/topic v0.3 walkthrough")
        print(
            f"\n[1] /topic by user: granted={r1.granted} reason={r1.reason}  → topic={room.topic!r}"
        )

        # 2. Agent tries the same action without a capability — denied.
        r2 = coord.propose_control_action(
            proposer_id="claude_code",
            action_name="SET_TOPIC",
            params={"topic": "should fail"},
        )
        print(f"\n[2] SET_TOPIC by claude_code (no cap): granted={r2.granted} reason={r2.reason}")

        # 3. Kernel-side grant. `_apply_effect` is internal; the
        # `/grant` slash command will dispatch this in v0.4.
        with coord._lock:
            coord._apply_effect(
                CapabilityGrantedEffect(
                    grant_id="g-claude-set-topic",
                    grantee_id="claude_code",
                    capability=CapabilityName.SET_TOPIC.value,
                    grantor_id="user",
                )
            )
        print("\n[3] kernel-side grant: SET_TOPIC → claude_code")

        # 4. Agent retries — granted.
        r4 = coord.propose_control_action(
            proposer_id="claude_code",
            action_name="SET_TOPIC",
            params={"topic": "recursion lesson"},
        )
        print(
            f"\n[4] SET_TOPIC by claude_code (with cap): "
            f"granted={r4.granted}  → topic={room.topic!r}"
        )

        # 5. Bus inspection.
        print("\n[5] relevant bus events:")
        interesting = {
            "capability_granted",
            "capability_revoked",
            "control_action_proposed",
            "control_action_applied",
            "control_action_denied",
        }
        for ev in bus.snapshot():
            ctype = ev.body.get("control_type", "") if isinstance(ev.body, dict) else ""
            if ctype in interesting:
                proposer = ev.body.get("proposer_id") or ev.body.get("grantee_id") or "-"
                action = ev.body.get("action_name", "")
                detail = action or ev.body.get("capability", "")
                print(f"  [{ev.id:>3}] {ctype:<28} proposer={proposer:<14} {detail}")


if __name__ == "__main__":
    main()
