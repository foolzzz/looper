"""Read back what Claude Code itself reports it will run with, before the direct session's turn (RFC 25.6).

``read`` asks a resident Claude Code (through ``ResidentClaude.control``) for its effective settings, sandbox,
hooks, MCP servers, permission rules and agents; ``compare`` checks them against what the launch injected and refuses
(typed ``Mismatch``) when anything is wider, when the hook or MCP sets differ, or when the answer is not shaped as the
verified CLI version answers. The control subtypes are an undocumented protocol of Claude Code: they are covered by
tests/test_task_manager_readback_probe.py, which must be rerun before ``VERIFIED_CLI_VERSION`` changes.

What is read, and when (verified with 2.1.332):

* before the first user turn of a launch or a resume: ``initialize`` (only for ``current_permission_mode``; it is
  side-effect free the first time and is not repeated), then the subtypes below;
* before every later turn: the subtypes below without ``initialize``.

The positive tool list (``system/init.tools``) only exists after the first turn, so tools are checked from the
negative list the CLI reports before it: every known built-in outside the allowlist is denied by a ``toolsNarrowing``
rule, and no allowed name is. The upgrade probe asserts the positive list for ``VERIFIED_CLI_VERSION``.

``mcp_status.config`` carries ``type``, ``command``, ``args`` (and ``url``) but no ``env`` or ``cwd``: those are
covered by the resolver-side fingerprint only.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import operator_mcp, task_manager_sandbox
from .claude_cli import CONTROL_STARTUP_SEC, CONTROL_TIMEOUT_SEC, ControlUnavailable, ResidentClaude
from .task_manager_safety import Reason
from .task_manager_sandbox import Check, Pin

# Control requests of every readback, in order. ``initialize`` comes first at a launch or resume only.
INITIALIZE = {"subtype": "initialize"}
REQUESTS: tuple[dict[str, Any], ...] = (
    {"subtype": "get_settings"},
    {"subtype": "get_sandbox_dialog", "lists": True},  # ``lists`` also gives the effective write set
    {"subtype": "get_hooks_listing"},
    {"subtype": "mcp_status"},
    {"subtype": "list_permission_rules"},
    {"subtype": "get_context_usage", "detail": "summary"},  # "full" would call the model API
)
# ``mcp_status`` answers ``pending`` while a server connects; wait for it, within this bound.
MCP_PENDING_WAIT_SEC = 10.0
MCP_PENDING_POLL_SEC = 0.25

# The built-in tools Claude Code ships, by CLI version: under ``--tools`` each one outside the allowlist is denied by a
# ``toolsNarrowing`` rule, which ``compare`` requires before the first turn. The list holds the tools every install
# has (file, shell, network, agent, scheduling, messaging and task tools); tools that exist only for some accounts or
# connectors are left out, because a rule for a tool an install does not have never appears. Whether another tool
# slipped in is asserted by the upgrade probe (tests/test_task_manager_readback_probe.py), which must be rerun, and
# this constant updated, before ``VERIFIED_CLI_VERSION`` changes.
KNOWN_BUILTIN_TOOLS: dict[str, frozenset[str]] = {
    "2.1.332": frozenset({
        "Agent", "AskUserQuestion", "Bash", "CronCreate", "CronDelete", "CronList", "Edit", "EndConversation",
        "EnterPlanMode", "EnterWorktree", "ExitPlanMode", "ExitWorktree", "Glob", "Grep", "ListMcpResourcesTool",
        "LSP", "Monitor", "NotebookEdit", "Read", "ReadMcpResourceTool", "RemoteTrigger", "ScheduleWakeup",
        "SendMessage", "Skill", "TaskCreate", "TaskGet", "TaskList", "TaskStop", "TaskUpdate", "TodoWrite",
        "WaitForMcpServers", "WebFetch", "WebSearch", "Write"}),
}

_WRITES_FIXED = ("/dev/stdout", "/dev/stderr", "/dev/null", "/dev/tty", "/dev/dtracehelper", "/dev/autofs_nowait",
                 "/tmp/claude", "/private/tmp/claude", ".")


def writes_allowed(home: str, uid: int, claude_tmpdir: str | None = None) -> frozenset[str]:
    """The sandbox's effective write set for a cwd with no ``allowWrite`` (2.1.332, macOS): Claude Code's own
    entries only. The per-user temporary root is ``<realpath(CLAUDE_CODE_TMPDIR or /tmp)>/claude-<uid>/`` and does
    not follow the process's ``TMPDIR``; ``.`` is the working directory."""
    root = os.path.realpath(claude_tmpdir or "/tmp")
    return frozenset((*_WRITES_FIXED, f"{home}/.npm/_logs", f"{home}/.claude/debug", f"{root}/claude-{uid}/"))


class ReadbackUnavailable(Exception):
    """The CLI did not answer a readback request, or answered with a shape the verified protocol does not have."""

    def __init__(self, item: str, detail: str = "") -> None:
        super().__init__(f"{item}: {detail}" if detail else item)
        self.item = item
        self.detail = detail


class Source(StrEnum):
    USER = "user"
    FLAG = "flag"
    POLICY = "policy"
    PROJECT = "project"
    LOCAL = "local"
    UNKNOWN = "unknown"


_SOURCES = {"userSettings": Source.USER, "flagSettings": Source.FLAG, "policySettings": Source.POLICY,
            "projectSettings": Source.PROJECT, "localSettings": Source.LOCAL}
_TRUSTED = frozenset({Source.USER, Source.FLAG, Source.POLICY})


def source_of(name: Any) -> Source:
    return _SOURCES.get(name, Source.UNKNOWN) if isinstance(name, str) else Source.UNKNOWN


@dataclass(frozen=True)
class Readback:
    permission_mode: str | None  # ``initialize``; None before a later turn (it is read at a launch or resume only)
    settings: Mapping[str, Any]  # get_settings
    sandbox: Mapping[str, Any]  # get_sandbox_dialog (lists)
    hooks: Mapping[str, Any]  # get_hooks_listing
    mcp: Mapping[str, Any]  # mcp_status
    rules: Mapping[str, Any]  # list_permission_rules
    context: Mapping[str, Any]  # get_context_usage (summary)


@dataclass(frozen=True)
class Expected:
    private_dir: Path  # the launch's MCP private directory (the wrapper's cwd)
    pins: Sequence[Pin]
    fs_allow_write: frozenset[str]  # ``restrictions.fs_allow_write``: empty, no ``allowWrite`` is injected
    writes_allowed: frozenset[str]  # the effective write set (``writes_allowed``)
    mcp: Mapping[str, str]  # server name -> ``shape`` of its definition before wrapping
    known_tools: frozenset[str]  # the built-ins this CLI version ships (``KNOWN_BUILTIN_TOOLS``)


@dataclass(frozen=True)
class Mismatch:
    reason: Reason
    item: str
    expected: Any
    actual: Any
    source: Source
    key: str = ""  # the dotted settings key, when the item is a settings pin


def shape(definition: Mapping[str, Any]) -> str:
    """The fingerprint of what both sides of the comparison have: transport, command, args and URL of an MCP server
    definition (``mcp_status`` returns no ``env`` or ``cwd``, which the resolver-side fingerprint covers)."""
    full = operator_mcp.fingerprint_input(definition)
    return hashlib.sha256(json.dumps({key: full[key] for key in ("transport", "command", "args", "url")},
                                     sort_keys=True).encode("utf-8")).hexdigest()


def expected_mcp(servers: Mapping[str, Mapping[str, Any]], private_dir: Path) -> dict[str, str]:
    """``shape`` of each server of the generated ``--mcp-config`` as the user scope defined it (stdio ones unwrapped)."""
    return {name: shape(operator_mcp.unwrap(definition, private_dir) if "command" in definition else definition)
            for name, definition in servers.items()}


def _need(body: Mapping[str, Any], subtype: str, *path: str, kind: type | tuple[type, ...]) -> Any:
    value: Any = body
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            raise ReadbackUnavailable(f"{subtype}.{'.'.join(path)}", "missing")
        value = value[key]
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ReadbackUnavailable(f"{subtype}.{'.'.join(path)}", f"is {type(value).__name__}")
    return value


def _rows(body: Mapping[str, Any], subtype: str, *path: str, fields: Mapping[str, type]) -> list[Mapping[str, Any]]:
    rows = _need(body, subtype, *path, kind=list)
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReadbackUnavailable(f"{subtype}.{'.'.join(path)}", "an entry is not an object")
        for key, kind in fields.items():
            if not isinstance(row.get(key), kind):
                raise ReadbackUnavailable(f"{subtype}.{'.'.join(path)}.{key}", "missing or mistyped")
    return rows


def _validate(readback: Readback) -> None:
    """Every field ``compare`` reads exists with its type; else ``ReadbackUnavailable`` names it."""
    if readback.permission_mode is not None and not isinstance(readback.permission_mode, str):
        raise ReadbackUnavailable("initialize.current_permission_mode", "mistyped")
    _need(readback.settings, "get_settings", "effective", kind=dict)
    _rows(readback.settings, "get_settings", "sources", fields={"source": str, "settings": dict})
    sandbox = readback.sandbox
    _need(sandbox, "get_sandbox_dialog", "supported", kind=bool)
    _need(sandbox, "get_sandbox_dialog", "dependencies", "errors", kind=list)
    _need(sandbox, "get_sandbox_dialog", "dependencies", "warnings", kind=list)
    _need(sandbox, "get_sandbox_dialog", "restrictions", "fs_allow_write", kind=list)
    rows = _rows(sandbox, "get_sandbox_dialog", "lists", "rows", fields={"id": str})
    if not any(row["id"] == "writes_allowed" and isinstance(row.get("entries"), list) for row in rows):
        raise ReadbackUnavailable("get_sandbox_dialog.lists.rows.writes_allowed", "missing")
    _rows(readback.hooks, "get_hooks_listing", "hooks", fields={"source": str})
    _rows(readback.mcp, "mcp_status", "mcpServers", fields={"name": str, "status": str, "config": dict})
    _rows(readback.rules, "list_permission_rules", "state", "rules", fields={"behavior": str, "source": str, "rule": str})
    _rows(readback.context, "get_context_usage", "agents", fields={"agentType": str, "source": str})


async def read(resident: ResidentClaude, phase: str) -> Readback:
    """Ask the running Claude Code for everything ``compare`` needs. ``phase`` is ``launch``, ``resume`` or ``turn``;
    ``initialize`` and the longer startup wait belong to the first two. ``ReadbackUnavailable`` for any request that
    fails and any field that is missing or mistyped."""
    startup = phase != "turn"
    first = True

    async def ask(request: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal first
        timeout = CONTROL_STARTUP_SEC if first and startup else CONTROL_TIMEOUT_SEC
        first = False
        try:
            return await resident.control(dict(request), timeout=timeout)
        except ControlUnavailable as exc:
            raise ReadbackUnavailable(exc.subtype, exc.detail) from exc

    mode: str | None = None
    if startup:
        mode = _need(await ask(INITIALIZE), "initialize", "current_permission_mode", kind=str)
    bodies = {request["subtype"]: await ask(request) for request in REQUESTS if request["subtype"] != "mcp_status"}
    deadline = asyncio.get_running_loop().time() + MCP_PENDING_WAIT_SEC
    while True:
        mcp = await ask({"subtype": "mcp_status"})
        servers = _need(mcp, "mcp_status", "mcpServers", kind=list)
        if not any(isinstance(server, Mapping) and server.get("status") == "pending" for server in servers):
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise ReadbackUnavailable("mcp_status.status", "a server is still pending")
        await asyncio.sleep(MCP_PENDING_POLL_SEC)
    readback = Readback(mode, bodies["get_settings"], bodies["get_sandbox_dialog"], bodies["get_hooks_listing"], mcp,
                        bodies["list_permission_rules"], bodies["get_context_usage"])
    _validate(readback)
    return readback


_MISSING = object()


def _get(tree: Any, key: Sequence[str]) -> Any:
    for part in key:
        if not isinstance(tree, Mapping) or part not in tree:
            return _MISSING
        tree = tree[part]
    return tree


def _same(left: Any, right: Any) -> bool:
    """JSON equality without ``True == 1``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[k], right[k]) for k in left)
    return type(left) is type(right) and left == right


def _members(items: Sequence[Any]) -> set[str]:
    return {json.dumps(item, sort_keys=True) for item in items}


def _satisfied(pin: Pin, value: Any) -> bool:
    if pin.check is Check.NOT_ASSERTED:
        return True
    if pin.check is Check.ABSENT:
        return value is _MISSING
    if value is _MISSING:
        return False
    if pin.check is Check.EQUAL:
        return _same(value, pin.value)
    if not isinstance(value, list):
        return False
    if pin.check is Check.ARRAY_EQUAL:
        return _members(value) == _members(pin.value)
    return _members(pin.value) <= _members(value)  # CONTAINS


def _blame(pin: Pin, readback: Readback) -> Source:
    """The first non-flag settings layer whose own value for the key does not satisfy the pin."""
    for layer in readback.settings["sources"]:
        source = source_of(layer["source"])
        if source is Source.FLAG:
            continue
        if _get(layer["settings"], pin.key) is not _MISSING and not _satisfied(pin, _get(layer["settings"], pin.key)):
            return source
    return Source.UNKNOWN


def _blame_env(readback: Readback, names: Sequence[str]) -> Source:
    for layer in readback.settings["sources"]:
        env = layer["settings"].get("env")
        if isinstance(env, Mapping) and any(name in env for name in names):
            return source_of(layer["source"])
    return Source.UNKNOWN


def _unknown_sandbox_keys(effective: Mapping[str, Any]) -> list[str]:
    known = {tuple(key) if key and key[0] == "sandbox" else ("sandbox", *key)
             for key in task_manager_sandbox.KNOWN_SANDBOX_KEYS}
    prefixes = {key[:index] for key in known for index in range(1, len(key))}
    found: list[str] = []

    def walk(tree: Any, path: tuple[str, ...]) -> None:
        for name, value in tree.items():
            here = (*path, str(name))
            if here in known:
                continue
            if here in prefixes and isinstance(value, Mapping):
                walk(value, here)
            else:
                found.append(".".join(here))

    sandbox = effective.get("sandbox")
    if isinstance(sandbox, Mapping):
        walk(sandbox, ("sandbox",))
    return found


def compare(readback: Readback, expected: Expected) -> list[Mismatch]:
    """Every difference between what the CLI reports and what the launch injected, in a fixed order."""
    found: list[Mismatch] = []
    wider = Reason.LAUNCH_SETTING_MISMATCH
    effective = readback.settings["effective"]
    sandbox = readback.sandbox

    # 1. the sandbox runs at all
    if sandbox["supported"] is not True:
        found.append(Mismatch(Reason.SANDBOX_UNAVAILABLE, "supported", True, sandbox["supported"], Source.UNKNOWN))
    if sandbox["dependencies"]["errors"]:
        found.append(Mismatch(Reason.SANDBOX_UNAVAILABLE, "dependencies", [],
                              [str(error)[:100] for error in sandbox["dependencies"]["errors"]], Source.UNKNOWN))
    # 2. only the layers Looper trusts
    layers = [source_of(layer["source"]) for layer in readback.settings["sources"]]
    if bad := sorted({layer.value for layer in layers if layer not in _TRUSTED}):
        found.append(Mismatch(wider, "sources", sorted(s.value for s in _TRUSTED), bad, Source.UNKNOWN))
    # 3. every pin
    for pin in expected.pins:
        value = _get(effective, pin.key)
        if not _satisfied(pin, value):
            found.append(Mismatch(wider, pin.item, pin.value, None if value is _MISSING else value,
                                  _blame(pin, readback), key=".".join(pin.key)))
    # 4. a sandbox key the verified readback does not know
    for path in _unknown_sandbox_keys(effective):
        found.append(Mismatch(wider, "unknown_key", None, path, Source.UNKNOWN, key=path))
    # 5. F3: no extra writable path (the effective set is Claude Code's own entries)
    write_pin = next((pin for pin in expected.pins if pin.item == "F3"), None)
    blamed = _blame(write_pin, readback) if write_pin is not None else Source.UNKNOWN
    if set(sandbox["restrictions"]["fs_allow_write"]) != expected.fs_allow_write and not any(m.item == "F3" for m in found):
        found.append(Mismatch(wider, "F3", sorted(expected.fs_allow_write),
                              sorted(sandbox["restrictions"]["fs_allow_write"]), blamed))
    entries = next(row["entries"] for row in sandbox["lists"]["rows"] if row["id"] == "writes_allowed")
    if set(entries) != expected.writes_allowed:
        found.append(Mismatch(wider, "F3.effective", sorted(expected.writes_allowed), sorted(map(str, entries)), blamed))
    # 6. no forbidden variable (names only, never values)
    env = effective.get("env")
    if isinstance(env, Mapping) and (names := sorted(name for name in task_manager_sandbox.FORBIDDEN_ENV if name in env)):
        found.append(Mismatch(wider, "env", [], names, _blame_env(readback, names), key="env"))
    # 7. hooks come from the user or the policy only
    hook_sources = sorted({source_of(hook["source"]).value for hook in readback.hooks["hooks"]
                           if source_of(hook["source"]) not in (Source.USER, Source.POLICY)})
    if hook_sources:
        found.append(Mismatch(wider, "hooks", ["user", "policy"], hook_sources, Source.UNKNOWN))
    # 8. tools: the built-ins outside the allowlist are denied, no allowed one is, MCP tools belong to allowed servers
    allowed = {*task_manager_sandbox.BUILTIN_TOOLS, *task_manager_sandbox.UNREMOVABLE_TOOLS}
    denied = {rule["rule"] for rule in readback.rules["state"]["rules"]
              if rule["source"] == "toolsNarrowing" and rule["behavior"] == "deny"}
    open_tools = sorted(expected.known_tools - allowed - denied)
    denied_allowed = sorted(set(task_manager_sandbox.BUILTIN_TOOLS) & denied)
    foreign = sorted(f"mcp__{server['name']}__{tool['name']}" for server in readback.mcp["mcpServers"]
                     for tool in server.get("tools") or () if isinstance(tool, Mapping)
                     and not task_manager_sandbox.tool_allowed(f"mcp__{server['name']}__{tool['name']}", expected.mcp))
    if open_tools or denied_allowed or foreign:
        found.append(Mismatch(wider, "tools", sorted(allowed), [*open_tools, *denied_allowed, *foreign], Source.UNKNOWN))
    # 9. the permission mode (read at a launch or resume only)
    if readback.permission_mode is not None and readback.permission_mode != "bypassPermissions":
        found.append(Mismatch(wider, "permissions.defaultMode", "bypassPermissions", readback.permission_mode,
                              Source.UNKNOWN))
    # 10. no agent from the task directory
    if agents := sorted(agent["agentType"] for agent in readback.context["agents"]
                        if source_of(agent["source"]) in (Source.PROJECT, Source.LOCAL)):
        found.append(Mismatch(wider, "agents", [], agents, Source.PROJECT))
    # 11. the MCP set, its wrapped launch shape and its definitions
    found.extend(_compare_mcp(readback, expected))
    return found


def _compare_mcp(readback: Readback, expected: Expected) -> list[Mismatch]:
    found: list[Mismatch] = []
    servers = {server["name"]: server["config"] for server in readback.mcp["mcpServers"]}
    if servers.keys() != expected.mcp.keys():
        found.append(Mismatch(Reason.MCP_SET_MISMATCH, "set", sorted(expected.mcp), sorted(servers), Source.UNKNOWN))
    for name in sorted(servers.keys() & expected.mcp.keys()):
        config = servers[name]
        try:
            definition = operator_mcp.unwrap(config, expected.private_dir) if "command" in config else config
        except ValueError:
            found.append(Mismatch(Reason.MCP_SET_MISMATCH, f"wrapper:{name}", None, None, Source.UNKNOWN))
            continue
        if shape(definition) != expected.mcp[name]:
            found.append(Mismatch(Reason.MCP_SET_MISMATCH, f"fingerprint:{name}", None, None, Source.UNKNOWN))
    return found


def _short(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)[:200]


def notice(mismatches: Sequence[Mismatch], version: str) -> str:
    """The refusal text, one line per mismatch: item, expected, actual, source and what to do. It never carries a
    value of the environment, a policy's content or an MCP server's command line or variables."""
    unknown = (f"Claude Code {version} reports something the verified readback does not know; run the CLI upgrade "
               "probe before changing VERIFIED_CLI_VERSION.")
    lines = []
    for mismatch in mismatches:
        if mismatch.reason is Reason.MCP_SET_MISMATCH:
            name = mismatch.item.partition(":")[2]
            if mismatch.item == "set":
                lines.append(f"mcp set: expected servers {_short(mismatch.expected)}, running {_short(mismatch.actual)}; "
                             "the launch configuration differs from what Claude Code loaded (layer launch).")
            else:
                lines.append(f"mcp server {name}: {mismatch.item.partition(':')[0]} differs from the launch "
                             "configuration (layer launch); the task Manager will not start with it.")
            continue
        if mismatch.reason is Reason.READBACK_UNAVAILABLE or mismatch.item == "unknown_key":
            lines.append(f"{mismatch.item}: {unknown}")
            continue
        head = f"{mismatch.item}: expected {_short(mismatch.expected)}, found {_short(mismatch.actual)} ({mismatch.source.value})."
        if mismatch.source is Source.POLICY:
            lines.append(f"{mismatch.item} ({mismatch.source.value}): an organization-managed policy loosens it; the task "
                         "Manager will not start on this machine until an administrator changes it.")
        elif mismatch.source is Source.USER:
            lines.append(f"{head} Narrow or remove `{mismatch.key or mismatch.item}` in your Claude Code user settings.")
        elif mismatch.source is Source.UNKNOWN:
            lines.append(f"{head} {unknown}")
        else:
            lines.append(head)
    return "\n".join(lines)
