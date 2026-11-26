"""The operator's MCP servers a task Manager's direct session may load (``tasks.conversations.manager_mcp``).

The direct session keeps the operator's Claude Code configuration, but loads MCP servers only from the
``--mcp-config`` file Looper generates (``--strict-mcp-config``): Looper's own server plus the allowed names.
Their definitions are read from the operator's configuration as Claude Code resolves them for the working directory,
the nearest scope first: the local scope (``projects.<project>.mcpServers`` of the user's ``.claude.json``), a
project ``.mcp.json`` (the directory or one above it), then the user scope (top-level ``mcpServers``). The local
scope's project is the git repository root of the directory (the main repository's, for a git worktree), else the
exact directory, never a parent: verified with Claude Code 2.1.332 (a subdirectory of a repository and a worktree
read the repository's entry; a subdirectory outside any repository reads none). A plugin's
MCP servers are not loadable by name: strict mode excludes them (verified with Claude Code 2.1.332), so a plugin's
server is allowed by configuring it as a user or project server.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any


class UnresolvedServers(Exception):
    """Allowed names with no usable definition in the operator's configuration."""

    def __init__(self, names: Sequence[str], detail: str = "") -> None:
        self.names = tuple(names)
        super().__init__(
            f"MCP server(s) {', '.join(self.names)} allowed for the task Manager (tasks.conversations.manager_mcp) "
            f"cannot be found in the operator's Claude Code configuration{detail}. Configure them (claude mcp add) "
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


def _project_roots(directory: Path) -> list[Path]:
    """The paths Claude Code may key ``directory``'s local scope by: its repository's main root, else itself."""
    try:
        common = subprocess.run(["git", "-C", str(directory), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        root = Path(common).parent if Path(common).name == ".git" else None
    except (OSError, subprocess.SubprocessError):
        root = None
    return list(dict.fromkeys(path for base in (root or directory,) for path in (base, base.resolve())))


def resolve(names: Sequence[str], *, cwd: str | os.PathLike[str],
            environment: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """The definitions of ``names``; ``UnresolvedServers`` for every one that has none (none is skipped)."""
    if not names:
        return {}
    home = Path(environment.get("HOME") or Path.home())
    config_dir = Path(environment["CLAUDE_CONFIG_DIR"]).expanduser() if environment.get("CLAUDE_CONFIG_DIR") else home
    directory = Path(cwd).absolute()
    chain = list(dict.fromkeys([directory, *directory.parents, directory.resolve(), *directory.resolve().parents]))
    layers: list[dict[str, dict[str, Any]]] = []  # nearest scope first
    try:
        user_file = config_dir / ".claude.json"
        projects = _servers(user_file, "projects")
        for candidate in _project_roots(directory):
            if isinstance(entry := projects.get(str(candidate)), dict) and isinstance(entry.get("mcpServers"), dict):
                layers.append(entry["mcpServers"])
        layers += [_servers(candidate / ".mcp.json", "mcpServers") for candidate in chain]
        layers.append(_servers(user_file, "mcpServers"))
    except ValueError as exc:
        raise UnresolvedServers(names, f" ({exc})") from exc
    found: dict[str, dict[str, Any]] = {}
    for name in names:
        definition = next((layer[name] for layer in layers if isinstance(layer.get(name), dict)), None)
        if definition is not None:
            found[name] = definition
    if missing := [name for name in names if name not in found]:
        raise UnresolvedServers(missing)
    return found


async def library_with_allowed(library: Any, runtime_root: Path, names: Sequence[str], *,
                               cwd: str | os.PathLike[str], environment: Mapping[str, str]) -> Any:
    """``library`` (``AgentLibrary``) whose ``--mcp-config`` holds its own servers (Looper's) plus ``names`` from
    the operator's configuration; ``library`` itself for an empty list."""
    from .mcp import write_config
    from .runtimes import library_mcp_servers

    if not names:
        return library
    own = library_mcp_servers(library)
    if clash := [name for name in names if name in own]:
        raise UnresolvedServers(clash, " (the name is Looper's own server)")
    allowed = await asyncio.to_thread(resolve, names, cwd=cwd, environment=environment)
    path = await asyncio.to_thread(write_config, runtime_root, sorted({**own, **allowed}.items()))
    return replace(library, mcp_config=str(path))
