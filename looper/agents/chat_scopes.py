"""Typed chat capabilities, resolution and launch authority: pure code with no I/O."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar

FULL, LEGACY, CHAT_ONLY = "full", "legacy", "chat_only"
SHELL_SETS = frozenset({FULL, LEGACY})


class CapabilityKind(StrEnum):
    CHAT = "chat"
    TASKS = "tasks"
    SCHEDULES = "schedules"
    FILES_READ = "files.read"
    VAULT_READ = "vault.read"
    SKILLS = "skills"
    MCP = "mcp"
    SHELL = "shell"


class TaskAction(StrEnum):
    READ = "read"
    CREATE = "create"
    INSTRUCT = "instruct"
    START = "start"
    STOP = "stop"
    RESTART = "restart"


class TaskVisibility(StrEnum):
    OWN = "own"
    CHAT = "chat"
    PROJECT = "project"


class ScheduleAction(StrEnum):
    READ = "read"
    MANAGE = "manage"


class LaunchMode(StrEnum):
    PLATFORM_TOOLS = "platform_tools"
    RESTRICTED_READ = "restricted_read"
    FULL = "full"


class ScopeAudience(StrEnum):
    OPERATOR = "operator"
    SHARED = "shared"


class PrincipalKind(StrEnum):
    PERSON = "person"
    BOT = "bot"


@dataclass(frozen=True)
class Chat:
    kind: ClassVar[CapabilityKind] = CapabilityKind.CHAT


@dataclass(frozen=True)
class Tasks:
    projects: str | tuple[str, ...]
    actions: frozenset[TaskAction]
    visibility: TaskVisibility
    templates_only: bool
    kind: ClassVar[CapabilityKind] = CapabilityKind.TASKS

    def __post_init__(self) -> None:
        if self.projects != "manager":
            object.__setattr__(self, "projects", tuple(sorted(set(self.projects))))
        object.__setattr__(self, "actions", frozenset(self.actions))


@dataclass(frozen=True)
class Schedules:
    actions: frozenset[ScheduleAction]
    kind: ClassVar[CapabilityKind] = CapabilityKind.SCHEDULES

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", frozenset(self.actions))


@dataclass(frozen=True)
class FilesRead:
    roots: tuple[str, ...]
    deny: tuple[str, ...]
    required: bool
    kind: ClassVar[CapabilityKind] = CapabilityKind.FILES_READ

    def __post_init__(self) -> None:
        object.__setattr__(self, "roots", tuple(sorted(set(self.roots))))
        object.__setattr__(self, "deny", tuple(sorted(set(self.deny))))


@dataclass(frozen=True)
class VaultRead:
    paths: tuple[str, ...]
    kind: ClassVar[CapabilityKind] = CapabilityKind.VAULT_READ

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", tuple(sorted(set(self.paths))))


@dataclass(frozen=True)
class Skills:
    ids: tuple[str, ...]
    kind: ClassVar[CapabilityKind] = CapabilityKind.SKILLS

    def __post_init__(self) -> None:
        object.__setattr__(self, "ids", tuple(sorted(set(self.ids))))


@dataclass(frozen=True)
class Mcp:
    servers: tuple[str, ...]
    kind: ClassVar[CapabilityKind] = CapabilityKind.MCP

    def __post_init__(self) -> None:
        object.__setattr__(self, "servers", tuple(sorted(set(self.servers))))


@dataclass(frozen=True)
class Shell:
    kind: ClassVar[CapabilityKind] = CapabilityKind.SHELL


Item = Chat | Tasks | Schedules | FilesRead | VaultRead | Skills | Mcp | Shell
_ITEM_TYPES = {item.kind: item for item in (Chat, Tasks, Schedules, FilesRead, VaultRead, Skills, Mcp, Shell)}


class ScopeUnavailable(Exception):
    """The entire stored set is unusable; never skip an unknown capability."""

    def __init__(self, scope: str, reason: str) -> None:
        self.scope, self.reason = scope, reason
        super().__init__(f"{scope}: {reason}" if scope else reason)


class ScopeInvariantViolation(Exception):
    """Refuse a launch that would break an authority invariant."""


def check_launch(scope: SessionScope | None, launch: Any, *, command: list[str]) -> None:
    """Check the assembled authority before it can reach a resident process."""
    from .agent_settings import Access
    from .library import ItemKind

    if scope is None or scope.mode is LaunchMode.FULL:
        return
    if any("--dangerously-skip-permissions" in arg or "bypassPermissions" in arg for arg in command):
        raise ScopeInvariantViolation("restricted launch bypasses permissions")
    if scope.mode is LaunchMode.PLATFORM_TOOLS:
        if launch.access_level is not Access.PLATFORM_TOOLS:
            raise ScopeInvariantViolation("platform-tools launch has wider access")
        if launch.library.plugin_dirs or any(
            item.kind is not ItemKind.MCP or item.name != "looper" or item.scope != "builtin"
            for item in launch.library.items
        ):
            raise ScopeInvariantViolation("platform-tools launch carries local capabilities")


def capability_of(tool: str, arguments: Mapping[str, Any]) -> str:
    """A refusal's capability label; unknown or future tools never inherit a grant."""
    from .ops.levels import TOOL_LEVELS

    if tool in {"looper_overview", "looper_tasks", "looper_gates", "looper_projects"}:
        return "tasks.read"
    if tool == "looper_task_create":
        return "tasks.create"
    if tool in {"looper_task_instruct", "looper_task_gate_reply", "looper_task_update"}:
        return "tasks.instruct"
    if tool == "looper_task_control":
        action = arguments.get("action")
        return f"tasks.{action}" if action in ("start", "stop", "restart") else "full"
    if tool == "looper_schedules":
        return "schedules.read" if arguments.get("action") in ("list", "get") else "schedules.manage"
    return "full" if tool in TOOL_LEVELS else "unknown"


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
        raise ValueError("expected a list of strings")
    return tuple(sorted(set(value)))


def parse_items(raw: Any, *, scope: str) -> tuple[Item, ...]:
    try:
        if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
            raise ValueError("items must be a list of objects")
        items: dict[CapabilityKind, Item] = {}
        for row in raw:
            kind = CapabilityKind(row.get("kind"))
            cls = _ITEM_TYPES[kind]
            if set(row) != {"kind", *(field.name for field in fields(cls))}:
                raise ValueError(f"{kind}: unknown or missing field")
            if kind in items:
                raise ValueError(f"duplicate kind: {kind}")
            values = {key: value for key, value in row.items() if key != "kind"}
            if kind is CapabilityKind.TASKS:
                if values["projects"] != "manager":
                    values["projects"] = _strings(values["projects"])
                values["actions"] = frozenset(TaskAction(action) for action in _strings(values["actions"]))
                values["visibility"] = TaskVisibility(values["visibility"])
                if type(values["templates_only"]) is not bool:
                    raise ValueError("templates_only must be a boolean")
            elif kind is CapabilityKind.SCHEDULES:
                values["actions"] = frozenset(ScheduleAction(action) for action in _strings(values["actions"]))
            elif kind is CapabilityKind.FILES_READ:
                if values["roots"] != ["chat_attachments"]:
                    raise ValueError("files.read.roots must be exactly [chat_attachments]")
                values["roots"], values["deny"] = _strings(values["roots"]), _strings(values["deny"])
                if type(values["required"]) is not bool:
                    raise ValueError("required must be a boolean")
            elif kind is CapabilityKind.VAULT_READ:
                if values["paths"] != []:
                    raise ValueError("vault.read.paths must be empty")
                values["paths"] = _strings(values["paths"])
            elif kind is CapabilityKind.SKILLS:
                values["ids"] = _strings(values["ids"])
            elif kind is CapabilityKind.MCP:
                values["servers"] = _strings(values["servers"])
            items[kind] = cls(**values)
        if CapabilityKind.CHAT not in items:
            raise ValueError("every set must include chat")
        return tuple(items[kind] for kind in sorted(items))
    except (ValueError, TypeError) as exc:
        raise ScopeUnavailable(scope, str(exc)) from exc


def to_json(items: tuple[Item, ...]) -> list[dict]:
    result = []
    for item in sorted(items, key=lambda item: item.kind):
        values = {"kind": item.kind.value, **asdict(item)}
        result.append({key: sorted(value) if isinstance(value, (tuple, frozenset)) else value
                       for key, value in sorted(values.items())})
    return result


_FULL_ITEMS = (Chat(), Schedules(frozenset(ScheduleAction)), Shell(),
               Tasks("manager", frozenset(TaskAction), TaskVisibility.PROJECT, False))
BUILTIN_VERSIONS: Mapping[str, tuple[int, tuple[Item, ...]]] = MappingProxyType({
    CHAT_ONLY: (1, (Chat(),)), FULL: (1, _FULL_ITEMS), LEGACY: (1, _FULL_ITEMS),
})


@dataclass(frozen=True)
class Ceiling:
    tools: str
    task_scope: str
    project_ids: tuple[str, ...]
    task_policy: str

    @classmethod
    def of(cls, definition: Any) -> Ceiling:
        def value(name: str) -> str:
            field = getattr(definition, name)
            return getattr(field, "value", field)

        return cls(value("tools"), value("task_scope"), tuple(sorted(set(definition.scope_project_ids))),
                   value("task_policy"))


@dataclass(frozen=True)
class Effective:
    items: tuple[Item, ...]
    ceiling: Ceiling


def effective(items: tuple[Item, ...], definition: Any) -> Effective:
    ceiling = Ceiling.of(definition)
    result = []
    for item in items:
        if isinstance(item, Tasks):
            projects = item.projects
            if projects != "manager" and ceiling.task_scope == "projects":
                projects = tuple(sorted(set(projects) & set(ceiling.project_ids)))
                if not projects:
                    continue
            item = replace(item, projects=projects,
                           templates_only=item.templates_only or ceiling.task_policy == "templates_only")
        result.append(item)
    return Effective(tuple(sorted(result, key=lambda item: item.kind)), ceiling)


def launch_mode(eff: Effective, supported: frozenset[LaunchMode]) -> tuple[LaunchMode, Effective]:
    if any(isinstance(item, Shell) for item in eff.items):
        return LaunchMode.FULL, eff
    if any(isinstance(item, (Skills, Mcp)) for item in eff.items):
        raise ScopeUnavailable("", "skills and mcp need step 4")
    files = tuple(item for item in eff.items if isinstance(item, FilesRead))
    vault = any(isinstance(item, VaultRead) for item in eff.items)
    if files or vault:
        if LaunchMode.RESTRICTED_READ not in supported and not vault and all(not item.required for item in files):
            return LaunchMode.PLATFORM_TOOLS, replace(eff, items=tuple(item for item in eff.items
                                                                      if not isinstance(item, FilesRead)))
        return LaunchMode.RESTRICTED_READ, eff
    return LaunchMode.PLATFORM_TOOLS, eff


def looper_tools(eff: Effective) -> frozenset[str] | None:
    if any(isinstance(item, Shell) for item in eff.items):
        return None
    if any(isinstance(item, (Tasks, Schedules)) for item in eff.items):
        raise ScopeUnavailable("", "task and schedule items without shell arrive with task_entry (step 2)")
    return frozenset()


def fingerprint(*, scope_id: str, version: int, eff: Effective, audience: ScopeAudience,
                inputs_version: int, mode: LaunchMode) -> str:
    payload = {"set": [scope_id, version], "items": to_json(eff.items), "ceiling": asdict(eff.ceiling),
               "audience": audience.value, "inputs": inputs_version, "mode": mode.value}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Override:
    principal_id: str
    kind: PrincipalKind
    scope_id: str


@dataclass(frozen=True)
class ScopeConfig:
    bound: bool
    default_scope: str | None
    legacy_allowed: bool
    overrides: Mapping[str, Override]


@dataclass(frozen=True)
class Resolved:
    scope_id: str
    audience: ScopeAudience
    principal_kind: PrincipalKind
    legacy_allowed: bool

    def __post_init__(self) -> None:
        if self.audience is ScopeAudience.SHARED and self.scope_id == FULL:
            raise ScopeInvariantViolation("shared audience cannot have full scope")
        if self.audience is ScopeAudience.OPERATOR and self.scope_id != FULL:
            raise ScopeInvariantViolation("operator audience requires full scope")
        if self.scope_id == LEGACY and not self.legacy_allowed:
            raise ScopeInvariantViolation("legacy scope requires legacy_allowed")


def resolve(*, sender: str | None, sender_kind: PrincipalKind | None, operator_id: str | None,
            config: ScopeConfig) -> Resolved:
    kind = sender_kind if sender_kind is not None else PrincipalKind.BOT
    if kind is PrincipalKind.PERSON and operator_id and sender == operator_id:
        return Resolved(FULL, ScopeAudience.OPERATOR, kind, config.legacy_allowed)
    if sender in config.overrides:
        scope = config.overrides[sender].scope_id
        if scope in SHELL_SETS:
            raise ScopeInvariantViolation("an override cannot name full or legacy")
    elif kind is PrincipalKind.BOT and config.default_scope != LEGACY:
        scope = CHAT_ONLY
    else:
        scope = config.default_scope if config.default_scope is not None else CHAT_ONLY
    return Resolved(scope, ScopeAudience.SHARED, kind, config.legacy_allowed)


@dataclass(frozen=True)
class SessionScope:
    resolved: Resolved
    version: int
    effective: Effective
    mode: LaunchMode
    fingerprint: str

    def __post_init__(self) -> None:
        shell = any(isinstance(item, Shell) for item in self.effective.items)
        if shell and self.resolved.scope_id not in SHELL_SETS:
            raise ScopeInvariantViolation("shell is forbidden in this scope")
        if self.mode is LaunchMode.FULL and not (
            (self.resolved.audience is ScopeAudience.OPERATOR and self.resolved.scope_id == FULL)
            or (self.resolved.scope_id == LEGACY and self.resolved.legacy_allowed)
        ):
            raise ScopeInvariantViolation("full launch requires operator full or permitted legacy scope")
        if self.mode is not LaunchMode.FULL and shell:
            raise ScopeInvariantViolation("shell requires full mode")


def session_scope(resolved: Resolved, version: int, raw_items: Any, definition: Any, *,
                  supported: frozenset[LaunchMode], inputs_version: int) -> SessionScope:
    items = parse_items(raw_items, scope=resolved.scope_id)
    try:
        mode, eff = launch_mode(effective(items, definition), supported)
    except ScopeUnavailable as exc:
        raise ScopeUnavailable(resolved.scope_id, exc.reason) from exc
    digest = fingerprint(scope_id=resolved.scope_id, version=version, eff=eff, audience=resolved.audience,
                         inputs_version=inputs_version, mode=mode)
    return SessionScope(resolved, version, eff, mode, digest)


@dataclass(frozen=True)
class Principal:
    audience: ScopeAudience
    kind: PrincipalKind
    scope_id: str
    scope_version: int
