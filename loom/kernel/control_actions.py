"""Loom v0.3 — control action dispatch (PR 9).

Doctrine: **P3** (extended — every state mutation goes through the
lease + capability path), **P13** (pure policy with frozen
``KernelStateView`` — extended to control-action authorization),
**P14** (custom actions return typed built-in effects only),
§7 (control action spec).

A *control action* is a named, parameterised request to mutate
kernel state — "set the topic to X", "assign role Y to participant Z",
"grant the floor to W". Every action runs through one path:

1. Caller invokes :meth:`RoomCoordinator.propose_control_action` with
   the action name, params, and proposer id.
2. The coordinator emits ``control_action_proposed`` (P2 control plane
   event).
3. The :class:`ControlActionRegistry` resolves the name to a
   :class:`ControlAction` instance and asks it to ``validate_params``.
4. The coordinator acquires a ``LeaseKind.CONTROL_ACTION`` lease
   (PR 7) — its ``_CapabilityCheck`` confirms the proposer has the
   ``ControlAction.required_capability``.
5. On grant, the action's ``propose_effect`` runs against a frozen
   :class:`KernelStateView` and returns a tuple of
   :class:`ControlEffect` instances; the coordinator applies each via
   ``_apply_effect`` under the lock and emits
   ``control_action_applied`` carrying the effect summary.
6. On denial, the coordinator emits ``control_action_denied`` with a
   :class:`DenialReason`.

Three registration layers (doctrine §7):

- **Kernel layer**: the 9 doctrine-required actions (SetTopic /
  SetAnchor / etc.) registered at coordinator construction.
- **Room layer**: :attr:`RoomConfig.custom_control_actions` —
  user-supplied :class:`ControlAction` instances. **P14**: their
  ``propose_effect`` MUST return built-in effect subclasses only;
  ``_apply_effect`` rejects unregistered effect types.
- **Policy layer** (PR 9 stub): the
  :meth:`ConversationPolicy.control_actions_for_participant` hook
  lets a policy narrow which actions a given participant may even
  *attempt* (a pre-capability filter). The kernel does not depend on
  the hook — its absence is silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from loom.kernel.capabilities import CapabilityName
from loom.kernel.causality import CausalRelation
from loom.kernel.effects import (
    AnchorAssignedEffect,
    ControlEffect,
    DefaultResponderSetEffect,
    RolesAssignedEffect,
    StyleChangedEffect,
    TopicChangedEffect,
)


# ---------------------------------------------------------------------------
# Denial taxonomy
# ---------------------------------------------------------------------------


class DenialReason(str, Enum):
    """Doctrine §7 — the kernel-recognised reasons for refusing a control action.

    Mirrors the string used in ``control_action_denied.reason`` so the
    journal carries the structured discriminator directly.
    """

    INSUFFICIENT_CAPABILITY = "INSUFFICIENT_CAPABILITY"
    INVALID_PARAMS = "INVALID_PARAMS"
    PARTICIPANT_NOT_ACTIVE = "PARTICIPANT_NOT_ACTIVE"
    ROOM_FROZEN = "ROOM_FROZEN"
    VETOED_BY_POLICY = "VETOED_BY_POLICY"
    RATE_LIMITED = "RATE_LIMITED"
    CHECK_RAISED = "CHECK_RAISED"
    LEASE_DENIED = "LEASE_DENIED"
    UNKNOWN_ACTION = "UNKNOWN_ACTION"


# ---------------------------------------------------------------------------
# Control action protocol
# ---------------------------------------------------------------------------


ParamValidator = Callable[[dict], tuple[bool, Optional[str]]]


@runtime_checkable
class ControlAction(Protocol):
    """The shape every control action implements."""

    name: str
    required_capability: CapabilityName

    def validate_params(self, params: dict) -> tuple[bool, Optional[str]]:
        """Return ``(True, None)`` if the params are well-formed for this action.

        ``(False, reason)`` triggers a ``DenialReason.INVALID_PARAMS``
        denial without acquiring the lease.
        """
        ...

    def propose_effect(
        self,
        params: dict,
        state_view: Any,
    ) -> tuple[ControlEffect, ...]:
        """Produce the effects to apply.

        Pure — must not mutate ``state_view`` (which is a frozen
        ``KernelStateView``). May raise :class:`ValueError` to signal
        an invariant violation that surfaced only after the
        capability + lease checks; the coordinator surfaces this as
        ``DenialReason.CHECK_RAISED``.
        """
        ...


# ---------------------------------------------------------------------------
# Kernel-defined actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BaseAction:
    """Mixin for kernel actions that just lift one param into one effect."""

    name: str
    required_capability: CapabilityName


class SetTopicAction:
    name = "SET_TOPIC"
    required_capability = CapabilityName.SET_TOPIC

    def validate_params(self, params: dict) -> tuple[bool, Optional[str]]:
        if "topic" not in params:
            return False, "missing 'topic'"
        if params["topic"] is not None and not isinstance(params["topic"], str):
            return False, "'topic' must be str or null"
        return True, None

    def propose_effect(self, params: dict, state_view: Any) -> tuple[ControlEffect, ...]:
        return (TopicChangedEffect(topic=params["topic"]),)


class SetAnchorAction:
    name = "SET_ANCHOR"
    required_capability = CapabilityName.SET_ANCHOR

    def validate_params(self, params: dict) -> tuple[bool, Optional[str]]:
        pid = params.get("participant_id")
        if pid is not None and not isinstance(pid, str):
            return False, "'participant_id' must be str or null"
        return True, None

    def propose_effect(self, params: dict, state_view: Any) -> tuple[ControlEffect, ...]:
        return (AnchorAssignedEffect(anchor_id=params.get("participant_id")),)


class SetDefaultResponderAction:
    name = "SET_DEFAULT_RESPONDER"
    required_capability = CapabilityName.SET_DEFAULT_RESPONDER

    def validate_params(self, params: dict) -> tuple[bool, Optional[str]]:
        pid = params.get("participant_id")
        if pid is not None and not isinstance(pid, str):
            return False, "'participant_id' must be str or null"
        return True, None

    def propose_effect(self, params: dict, state_view: Any) -> tuple[ControlEffect, ...]:
        return (DefaultResponderSetEffect(participant_id=params.get("participant_id")),)


class SetRolesAction:
    name = "SET_ROLES"
    required_capability = CapabilityName.SET_ROLES

    def validate_params(self, params: dict) -> tuple[bool, Optional[str]]:
        roles = params.get("roles")
        if not isinstance(roles, dict):
            return False, "'roles' must be a dict"
        for k, v in roles.items():
            if not isinstance(k, str) or not isinstance(v, str):
                return False, "roles dict must be str→str"
        return True, None

    def propose_effect(self, params: dict, state_view: Any) -> tuple[ControlEffect, ...]:
        return (RolesAssignedEffect(roles=dict(params["roles"])),)


# Note: SetStyleAction maps to a capability that's not in the
# CapabilityName enum (style is a UI hint, not a doctrine verb). For
# PR 9 we ship a placeholder action requiring SET_TOPIC (the closest
# proximate). v0.4+ can elevate "style" to a first-class verb if the
# product surface demands it.
class SetStyleAction:
    name = "SET_STYLE"
    required_capability = CapabilityName.SET_TOPIC  # proximate for v0.3.

    def validate_params(self, params: dict) -> tuple[bool, Optional[str]]:
        if params.get("style") not in ("brief", "normal", "detailed"):
            return False, "'style' must be brief|normal|detailed"
        return True, None

    def propose_effect(self, params: dict, state_view: Any) -> tuple[ControlEffect, ...]:
        return (StyleChangedEffect(style=params["style"]),)


# Kernel-built-in action set the registry hydrates at init.
KERNEL_BUILTIN_ACTIONS: tuple[ControlAction, ...] = (
    SetTopicAction(),
    SetAnchorAction(),
    SetDefaultResponderAction(),
    SetRolesAction(),
    SetStyleAction(),
    # Remaining doctrine actions (UpdateAllowedSpeakers, SwitchPolicy,
    # SendDM, GrantFloor, CancelLease) join when their reducer support
    # lands: PR 10 (FloorOverrideEffect reducer), PR 12 (SwitchPolicy
    # reducer + lease cancel), or v0.4 (SendDM via DM channel).
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class ControlActionRegistry:
    """Three-layer registry: kernel built-ins + room customs + policy filter.

    The room layer is filled at coordinator construction from
    ``RoomConfig.custom_control_actions``. The policy layer is
    consulted at proposal time via
    :meth:`ConversationPolicy.control_actions_for_participant`.
    """

    by_name: dict[str, ControlAction] = field(default_factory=dict)

    def register(self, action: ControlAction) -> None:
        if action.name in self.by_name:
            raise ValueError(f"control action already registered: {action.name!r}")
        self.by_name[action.name] = action

    def get(self, name: str) -> Optional[ControlAction]:
        return self.by_name.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.by_name.keys()))


def build_kernel_action_registry(
    customs: tuple[ControlAction, ...] = (),
) -> ControlActionRegistry:
    """Construct the canonical v0.3 control-action registry.

    ``customs`` are appended after the kernel built-ins; name
    collisions raise :class:`ValueError`.
    """
    reg = ControlActionRegistry()
    for action in KERNEL_BUILTIN_ACTIONS:
        reg.register(action)
    for action in customs:
        reg.register(action)
    return reg


# ---------------------------------------------------------------------------
# ControlInterest (P9 / §7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlInterest:
    """Per-participant subscription filter for control-plane events.

    Wraps the four dimensions a policy can declare via
    :meth:`ConversationPolicy.control_interest_for_participant`:

    - ``event_types`` — control_type values the participant cares about.
    - ``relations`` — :class:`CausalRelation` predicates they're
      interested in (e.g. ``RESPONDS_TO`` for chat-only consumers).
    - ``channels`` — channel restriction (``"main"``, ``"dm:<id>"``).
    - ``capabilities_required`` — only deliver if the participant
      currently holds these capabilities.

    PR 9 ships the dataclass; the wiring of "filter before delivery"
    is intentionally light — v0.4+ may push the filter into the bus
    transport. For v0.3 the kernel just exposes the type so policies
    have a stable shape to populate.
    """

    event_types: frozenset[str] = frozenset()
    relations: frozenset[CausalRelation] = frozenset()
    channels: frozenset[str] = frozenset()
    direct_mentions: bool = False
    capabilities_required: frozenset[CapabilityName] = frozenset()


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlActionResult:
    """Outcome of :meth:`RoomCoordinator.propose_control_action`.

    On success: ``granted=True``, ``effects`` holds the tuple of
    applied :class:`ControlEffect` instances (each stamped with
    ``applied_at_event_id``).

    On denial: ``granted=False``, ``reason`` is a
    :class:`DenialReason`, ``message`` carries a short human-readable
    detail (e.g. the failing check name or the validator's complaint).
    """

    granted: bool
    effects: tuple[ControlEffect, ...] = ()
    reason: Optional[DenialReason] = None
    message: Optional[str] = None
