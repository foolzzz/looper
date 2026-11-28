"""Canned control responses shaped as Claude Code 2.1.332 answers them before a turn (paths and ids synthetic)."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from looper.agents import operator_mcp, task_manager_readback as rb, task_manager_sandbox as sandbox

HOME = "/home-placeholder"
UID = 501
READ_DENY = (f"{HOME}/.ssh", f"{HOME}/.aws")
MCP_ALLOWED = {"docs": {"type": "stdio", "command": "/usr/bin/docs-server", "args": ["--serve"], "env": {"TOKEN": "x"}},
               "looper": {"type": "stdio", "command": "/usr/bin/python3", "args": ["-I", "/opt/looper/mcp.py"]}}
KNOWN = frozenset({"Agent", "Edit", "Write", "Read", "Glob", "Grep", "Skill", "WebFetch", "WebSearch", "Monitor",
                   "ToolSearch", "NotebookEdit", "EndConversation", "WaitForMcpServers", "Bash"})


def tool_rule(name: str) -> dict[str, Any]:
    return {"behavior": "deny", "source": "toolsNarrowing", "rule": name, "editability": "readonly",
            "description": {"prefix": "Any use of the ", "emphasis": name, "suffix": " tool"}}


def row(identifier: str, entries: list[str]) -> dict[str, Any]:
    return {"id": identifier, "entries": entries, "total": len(entries)}


class Case:
    """One clean readback and the expectation it matches; tests change ``bodies`` and call ``compare``."""

    def __init__(self, tmp_path: Path, *, allowed_domains: tuple[str, ...] = ("api.example.com",)) -> None:
        self.work = tmp_path / "work"
        self.work.mkdir(exist_ok=True)
        self.private = tmp_path / "mcp-cwd"
        injected = sandbox.settings_content(self.work, allowed_domains=allowed_domains, read_deny=READ_DENY)
        pins = sandbox.pins(self.work, allowed_domains=allowed_domains, read_deny=READ_DENY)
        wrapped = {name: operator_mcp.wrap(definition, self.private) for name, definition in MCP_ALLOWED.items()}
        self.expected = rb.Expected(
            private_dir=self.private, pins=pins, fs_allow_write=frozenset(),
            writes_allowed=rb.writes_allowed(HOME, UID), mcp=rb.expected_mcp(wrapped, self.private),
            known_tools=KNOWN)
        self.injected = injected
        self.mode: str | None = "bypassPermissions"
        self.settings: dict[str, Any] = {
            "effective": copy.deepcopy(injected),
            "sources": [{"source": "flagSettings", "settings": copy.deepcopy(injected)},
                        {"source": "policySettings", "settings": {"hooks": {"PreToolUse": [{"matcher": "^Bash$"}]}}}]}
        deny = list(READ_DENY) + [f"{HOME}/.config/gh"]
        self.sandbox: dict[str, Any] = {
            "supported": True, "enabled": True, "unsandboxed_fallback": False, "excluded_commands": [],
            "dependencies": {"errors": [], "warnings": []},
            "restrictions": {"fs_allow_write": [], "fs_deny_write": [], "network_allowed_domains": list(allowed_domains)},
            "lists": {"rows": [row("writes_allowed", sorted(self.expected.writes_allowed)),
                               row("reads_denied", [*deny, f"{HOME}/extra-denied"]),
                               row("writes_denied", [*sandbox.git_deny_write(self.work), "/x"]),
                               *([row("hosts_allowed", list(allowed_domains))] if allowed_domains else [])],
                      "files_confined": True, "all_unix_sockets": False, "other_hosts": "refused"}}
        self.hooks: dict[str, Any] = {"hooks": [
            {"event": "PreToolUse", "matcher": "^Bash$", "source": "policySettings", "type": "command"}]}
        self.mcp: dict[str, Any] = {"mcpServers": [
            {"name": name, "status": "connected", "config": {k: v for k, v in definition.items() if k != "env"},
             "transport": "stdio", "scope": "dynamic", "source": "dynamic",
             "tools": [{"name": "do", "annotations": {}}]} for name, definition in wrapped.items()]}
        self.rules: dict[str, Any] = {"state": {"rules": [
            {"behavior": "deny", "source": "flagSettings", "rule": f"Read(//{HOME}/.ssh/**)"},
            *(tool_rule(name) for name in sorted(KNOWN - {"Bash"}))]}}
        self.context: dict[str, Any] = {"agents": [{"agentType": "uagent", "source": "userSettings", "path": "p"}],
                                         "mcpTools": [], "systemTools": None}

    def readback(self) -> rb.Readback:
        return rb.Readback(self.mode, self.settings, self.sandbox, self.hooks, self.mcp, self.rules, self.context)

    def compare(self) -> list[rb.Mismatch]:
        return rb.compare(self.readback(), self.expected)

    def user_layer(self, key: tuple[str, ...], value: Any) -> None:
        """The user's settings set ``key`` to ``value``: the effective settings and a user layer show it."""
        for tree in (self.settings["effective"], layer := {}):
            node = tree
            for part in key[:-1]:
                node = node.setdefault(part, {})
            node[key[-1]] = copy.deepcopy(value)
        self.settings["sources"].insert(0, {"source": "userSettings", "settings": layer})

    def responses(self) -> dict[str, dict[str, Any]]:
        """What a fake CLI answers, by control subtype."""
        return {"initialize": {"current_permission_mode": self.mode}, "get_settings": self.settings,
                "get_sandbox_dialog": self.sandbox, "get_hooks_listing": self.hooks, "mcp_status": self.mcp,
                "list_permission_rules": self.rules, "get_context_usage": self.context}


class FakeResident:
    """``control`` answers from canned bodies and records every request with its timeout."""

    def __init__(self, bodies: dict[str, Any]) -> None:
        self.bodies = bodies
        self.requests: list[tuple[dict[str, Any], float]] = []

    async def control(self, request: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
        from looper.agents.claude_cli import ControlUnavailable

        self.requests.append((dict(request), timeout))
        body = self.bodies.get(request["subtype"])
        if isinstance(body, Exception):
            raise body
        if body is None:
            raise ControlUnavailable(request["subtype"], "no such request")
        return json.loads(json.dumps(body(request) if callable(body) else body))

