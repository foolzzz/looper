"""The operator's MCP servers a task Manager's direct session may load (``tasks.conversations.manager_mcp``).

The direct session keeps the operator's Claude Code settings, but loads MCP servers only from the
``--mcp-config`` file Looper generates (``--strict-mcp-config``): Looper's own server plus the allowed names.
MCP servers run outside the sandbox with the operator's rights, and a task's directory is not trusted: its project
``.mcp.json`` and the local scope tied to its path are never read. A definition comes from the user scope only
(top-level ``mcpServers`` of the user's ``.claude.json``; ``$CLAUDE_CONFIG_DIR/.claude.json`` when that is set), so a
name defined only in a project or the local scope is unresolved. A plugin's MCP servers are not loadable by name:
strict mode excludes them (verified with Claude Code 2.1.332), so a plugin's server is allowed by configuring it as
a user server. Each resolved definition has a ``fingerprint``, to compare with what the CLI launched.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any


class UnresolvedServers(Exception):
    """Allowed names with no usable definition in the operator's user-scope configuration."""

    def __init__(self, names: Sequence[str], detail: str = "") -> None:
        self.names = tuple(names)
        super().__init__(
            f"MCP server(s) {', '.join(self.names)} allowed for the task Manager (tasks.conversations.manager_mcp) "
            f"cannot be found in the operator's Claude Code user settings{detail}. Configure them (claude mcp add) "
            "or fix the list with task_conversations.update (manager_mcp).")


def _servers(path: Path, *keys: str) -> dict[str, dict[str, Any]]:
    """The server map at ``keys`` of a JSON file; empty when the file or the key is absent, ``ValueError`` when it
    is not usable."""
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path.name} cannot be read") from exc
    for key in keys:
        if not isinstance(value, dict):
            raise ValueError(f"{path.name} is not a JSON object")
        value = value.get(key)
        if value is None:
            return {}
    if not isinstance(value, dict) or not all(isinstance(entry, dict) for entry in value.values()):
        raise ValueError(f"{path.name} has malformed MCP servers")
    return value


def resolve(names: Sequence[str], *, environment: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """The user-scope definitions of ``names``; ``UnresolvedServers`` for every one that has none (none is skipped)."""
    if not names:
        return {}
    home = Path(environment.get("HOME") or Path.home())
    config_dir = Path(environment["CLAUDE_CONFIG_DIR"]).expanduser() if environment.get("CLAUDE_CONFIG_DIR") else home
    try:
        user = _servers(config_dir / ".claude.json", "mcpServers")
    except ValueError as exc:
        raise UnresolvedServers(names, f" ({exc})") from exc
    if missing := [name for name in names if name not in user]:
        raise UnresolvedServers(missing, " as a user-scope server")
    return {name: user[name] for name in names}


def fingerprint_input(definition: Mapping[str, Any]) -> dict[str, Any]:
    """What identifies a definition without its secrets: transport, command, args, the names of its variables and
    headers, and a URL without userinfo and query."""
    from urllib.parse import urlsplit

    url = definition.get("url")
    parts = urlsplit(url) if isinstance(url, str) else None
    return {"transport": definition.get("type") or ("stdio" if "command" in definition else "http"),
            "command": definition.get("command"), "args": list(definition.get("args") or []),
            "env_keys": sorted(definition.get("env") or {}), "header_keys": sorted(definition.get("headers") or {}),
            "url": f"{parts.scheme}://{parts.hostname}{parts.path}" if parts and parts.hostname else None}


def fingerprint(definition: Mapping[str, Any]) -> str:
    """The SHA-256 of ``fingerprint_input``."""
    return hashlib.sha256(json.dumps(fingerprint_input(definition), sort_keys=True).encode("utf-8")).hexdigest()


def fingerprints(library: Any, names: Sequence[str]) -> dict[str, str]:
    """The fingerprints of ``names`` as the library's generated ``--mcp-config`` holds them."""
    from .runtimes import library_mcp_servers

    servers = library_mcp_servers(library)
    return {name: fingerprint(servers[name]) for name in names if name in servers}


async def library_with_allowed(library: Any, runtime_root: Path, names: Sequence[str], *,
                               environment: Mapping[str, str]) -> Any:
    """``library`` (``AgentLibrary``) whose ``--mcp-config`` holds its own servers (Looper's) plus ``names`` from
    the operator's user-scope configuration; ``library`` itself for an empty list."""
    from .mcp import write_config
    from .runtimes import library_mcp_servers

    if not names:
        return library
    own = library_mcp_servers(library)
    if clash := [name for name in names if name in own]:
        raise UnresolvedServers(clash, " (the name is Looper's own server)")
    allowed = await asyncio.to_thread(resolve, names, environment=environment)
    path = await asyncio.to_thread(write_config, runtime_root, sorted({**own, **allowed}.items()))
    return replace(library, mcp_config=str(path))
