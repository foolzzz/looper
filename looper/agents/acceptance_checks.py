"""Acceptance check definitions and protocol validation. No commands are executed here."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from .project_execution import AcceptanceChecksMode

MAX_CHECKS = 12
MAX_COMMAND_CHARS = 2000
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 1800
MAX_ARTIFACTS = 3
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class CheckType(str, Enum):
    COMMAND = "command"
    ARTIFACT = "artifact"


@dataclass(frozen=True)
class CommandCheck:
    id: str
    criterion: int
    command: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    required: bool = True
    protect: tuple[str, ...] = ()
    network: bool = False

    @property
    def type(self) -> CheckType:
        return CheckType.COMMAND

    def row(self) -> dict[str, Any]:
        return {"id": self.id, "criterion": self.criterion, "type": self.type.value, "command": self.command,
                "timeout_seconds": self.timeout_seconds, "required": self.required,
                "protect": list(self.protect), "network": self.network}


@dataclass(frozen=True)
class ArtifactCheck(CommandCheck):
    artifact: tuple[str, ...] = ()

    @property
    def type(self) -> CheckType:
        return CheckType.ARTIFACT

    def row(self) -> dict[str, Any]:
        return {**super().row(), "artifact": list(self.artifact)}


Check = CommandCheck | ArtifactCheck


def _paths(raw: Any, where: str, *, artifacts: bool = False) -> tuple[str, ...]:
    if not isinstance(raw, list) or (artifacts and not 1 <= len(raw) <= MAX_ARTIFACTS):
        raise ValueError(f"{where} must be a list of relative paths" + (" (1 to 3)" if artifacts else ""))
    for path in raw:
        if (not isinstance(path, str) or not path.strip() or "\x00" in path
                or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts):
            raise ValueError(f"{where} paths must be nonempty, relative to the step working directory, without '..'")
    return tuple(raw)


def parse_checks(raw: Any, criteria: int, mode: AcceptanceChecksMode) -> tuple[Check, ...]:
    """Validate the optional ``checks`` key; errors are surfaced by the Planner's repair turn."""
    if mode is AcceptanceChecksMode.OFF:
        raise ValueError("acceptance_checks is off; enable it in the project's Settings → Execution, or omit checks")
    if not isinstance(raw, list) or len(raw) > MAX_CHECKS:
        raise ValueError("checks must be a list of at most 12 checks")
    parsed: list[Check] = []
    ids: set[str] = set()
    required_keys = {"id", "criterion", "type", "command"}
    allowed = required_keys | {"timeout_seconds", "required", "protect", "network", "artifact"}
    for i, item in enumerate(raw):
        where = f"checks[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where} must be an object with id, criterion, type and command")
        if missing := sorted(required_keys - item.keys()):
            raise ValueError(f"{where} is missing {', '.join(missing)}")
        if unknown := sorted(item.keys() - allowed):
            raise ValueError(f"{where} has unknown keys: {', '.join(unknown)}")
        key = item["id"]
        if not isinstance(key, str) or not _ID.fullmatch(key) or key in ids:
            raise ValueError(f"{where}.id must be a unique short label (letters, digits, '.', '_' or '-')")
        ids.add(key)
        criterion = item["criterion"]
        if type(criterion) is not int or not 1 <= criterion <= criteria:
            raise ValueError(f"{where}.criterion must be an integer from 1 to {criteria}")
        try:
            kind = CheckType(item["type"])
        except (ValueError, TypeError):
            raise ValueError(f'{where}.type must be "command" or "artifact"') from None
        command = item["command"]
        if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS:
            raise ValueError(f"{where}.command must be a nonempty string of at most 2000 characters")
        timeout = item.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"{where}.timeout_seconds must be an integer from 1 to 1800 (default 300)")
        for field, default in (("required", True), ("network", False)):
            if type(item.get(field, default)) is not bool:
                raise ValueError(f"{where}.{field} must be true or false")
        if item.get("network", False) and mode is not AcceptanceChecksMode.NETWORK:
            raise ValueError(f"{where}.network: true requires the project's acceptance_checks setting to be network")
        values = dict(id=key, criterion=criterion, command=command, timeout_seconds=timeout,
                      required=item.get("required", True), protect=_paths(item.get("protect", []), f"{where}.protect"),
                      network=item.get("network", False))
        if kind is CheckType.ARTIFACT:
            parsed.append(ArtifactCheck(**values, artifact=_paths(item.get("artifact"), f"{where}.artifact", artifacts=True)))
        else:
            if "artifact" in item:
                raise ValueError(f"{where}.artifact is only allowed for type artifact")
            parsed.append(CommandCheck(**values))
    return tuple(parsed)


# The example and paragraph in the executable acceptance RFC, section 6, verbatim.
PROTOCOL_SECTION = '''

```json
{"steps": [{
  "id": "s1",
  "instruction": "...",
  "acceptance_criteria": ["the report endpoint returns 200 for a valid id", "the empty state shows a message"],
  "depends_on": [],
  "review": true,
  "checks": [
    {"id": "c1", "criterion": 1, "type": "command",
     "command": "uv run pytest tests/test_report.py -q", "timeout_seconds": 300,
     "required": true, "protect": ["tests/test_report.py"]},
    {"id": "c2", "criterion": 2, "type": "artifact",
     "command": "python scripts/shot.py empty-state out/empty.png", "artifact": ["out/empty.png"],
     "required": false}
  ]
}]}
```

一个标准能用命令量的，写成检查，绑到它的序号上；量不出来的留给 Reviewer。检查要真的测到标准，不要写永远通过的命令。受保护路径写你的检查依赖的测试文件。检查由 Looper 在 Worker 交付后运行，证据在你的决策回合里给出。`final.verification` 引用检查编号，不要自己宣称通过。
'''
