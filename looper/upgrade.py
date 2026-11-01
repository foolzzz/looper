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
    protocols: tuple[str, ...]


@dataclass(frozen=True)
class UpgradePlan:
    kind: UpgradeKind
    reasons: tuple[str, ...]
    changed_components: tuple[str, ...]


def scan_keepers(runtime_root: Path | None) -> tuple[Keeper, ...]:
    from .agents import kept_dirs
    from .agents.keeper import RUNS_DIR, STATE, KeeperState, read_json

    if runtime_root is None or not (runs := runtime_root / RUNS_DIR).exists():
        return ()
    records = {path: record for path in sorted(runs.iterdir()) if path.is_dir()
               if (record := kept_dirs.KeeperRecord.read(path / STATE)) is not None
               and record.state != KeeperState.EXITED}
    try:
        alive = kept_dirs.live({path.name: record for path, record in records.items()}) if records else {}
    except OSError as exc:
        raise ValueError("cannot tell which keepers are running") from exc
    keepers = []
    for path in records:
        if alive[path.name]:
            protocols = (read_json(path / STATE) or {}).get("protocol_versions")
            valid = isinstance(protocols, list) and all(isinstance(p, str) and p for p in protocols)
            keepers.append(Keeper(tuple(protocols) if valid else ()))
    return tuple(keepers)


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
    supported = set(target.adoption["keeper_protocols"])
    unsupported = sorted({k.protocols for k in keepers if not supported.intersection(k.protocols)})
    if unsupported:
        return UpgradePlan(UpgradeKind.FULL, ("Target does not support live keeper protocols: "
                           + "; ".join(", ".join(protocols) or "unknown" for protocols in unsupported),),
                           changed)
    if not same_adoption:
        return UpgradePlan(UpgradeKind.FULL, ("The adoption contract changed; live runs cannot be adopted.",), changed)
    return UpgradePlan(UpgradeKind.CONTROL_PLANE, ("Adoption contracts and live keeper protocols are compatible.",), changed)
