"""Pure upgrade decisions; deployment and drain are separate operations."""
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from collections.abc import Sequence

from .components import Manifest


class UpgradeKind(StrEnum):
    NOOP = "NOOP"
    WEB_ONLY = "WEB_ONLY"
    CONTROL_PLANE = "CONTROL_PLANE"
    FULL = "FULL"


@dataclass(frozen=True)
class Keeper:
    protocol: str


@dataclass(frozen=True)
class UpgradePlan:
    kind: UpgradeKind
    reasons: tuple[str, ...]
    changed_components: tuple[str, ...]


def scan_keepers(runtime_root: Path | None) -> tuple[Keeper, ...]:
    # Live keeper discovery is enabled when upgrades can preserve running agents.
    return ()


def decide(target: Manifest, running: Manifest, keepers: Sequence[Keeper], keeper_mode: bool) -> UpgradePlan:
    changed = tuple(sorted(c for c in target.components.keys() | running.components.keys()
                           if target.components.get(c) != running.components.get(c)))
    same_adoption = target.adoption == running.adoption
    if not changed and same_adoption:
        return UpgradePlan(UpgradeKind.NOOP, ("No component or adoption contract changed.",), changed)
    if set(changed) <= {"web"} and changed and same_adoption:
        return UpgradePlan(UpgradeKind.WEB_ONLY, ("Only web changed; the control plane can keep running.",), changed)
    if not keeper_mode:
        return UpgradePlan(UpgradeKind.FULL, ("Keeper mode is off; control-plane changes require a full drain.",), changed)
    unsupported = sorted({k.protocol for k in keepers} - set(target.adoption["keeper_protocols"]))
    if unsupported:
        return UpgradePlan(UpgradeKind.FULL, ("Target does not support live keeper protocols: " + ", ".join(unsupported),),
                           changed)
    if not same_adoption:
        return UpgradePlan(UpgradeKind.FULL, ("The adoption contract changed; live runs cannot be adopted.",), changed)
    return UpgradePlan(UpgradeKind.CONTROL_PLANE, ("Adoption contracts and live keeper protocols are compatible.",), changed)
