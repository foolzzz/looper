"""The operator's MCP servers a task Manager's direct session may load (``tasks.conversations.manager_mcp``).

The direct session keeps the operator's Claude Code settings, but loads MCP servers only from the
``--mcp-config`` file Looper generates (``--strict-mcp-config``): Looper's own server plus the allowed names.
MCP servers run outside the sandbox with the operator's rights, and a task's directory is not trusted: its project
``.mcp.json`` and the local scope tied to its path are never read. A definition comes from the user scope only
(top-level ``mcpServers`` of the user's ``.claude.json``; ``$CLAUDE_CONFIG_DIR/.claude.json`` when that is set), so a
name defined only in a project or the local scope is unresolved. A plugin's MCP servers are not loadable by name:
strict mode excludes them (verified with Claude Code 2.1.332), so a plugin's server is allowed by configuring it as
a user server. Each resolved definition has a ``fingerprint``, to compare with what the CLI launched.

Where a server starts is not trusted either: Claude Code starts a stdio server in the session's directory (the task
directory, which the model can write), outside the sandbox. Looper therefore starts every stdio server, its own
included, through ``wrap``: a ``/bin/sh`` that changes into a private directory outside any task directory and then
executes the absolute command. ``launcher_problem`` additionally refuses launchers that resolve code from the
working directory (``npx``, ``python -m`` without ``-I``, a relative script...), as defense in depth.
Fingerprints are of the definition before wrapping: ``unwrap`` turns a wrapped definition back into the user-scope
one, so a comparison with what the CLI reports unwraps first (and checks ``is_wrapped``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
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


class UnsafeLauncher(Exception):
    """An allowed server whose launch resolves code from the working directory (or a relative path)."""

    def __init__(self, name: str, launcher: str) -> None:
        self.name, self.launcher = name, launcher
        super().__init__(
            f"MCP server {name} allowed for the task Manager (tasks.conversations.manager_mcp) is started by "
            f"{launcher}, which can run code from the task directory. Configure the server with an absolute path to "
            "an installed executable instead.")


# One row per launcher that resolves a package, script or module from the working directory (or a relative path):
# (basename of the command, the argument that makes it unsafe or None for always, the reason it is reported as).
UNSAFE_LAUNCHERS: tuple[tuple[str, str | None, str], ...] = (
    ("npx", None, "npx"),  # runs ./node_modules/.bin/<pkg> of the working directory first
    ("uvx", None, "uvx"),
    ("bunx", None, "bunx"),
    ("uv", "run", "uv run"),  # runs the project of the working directory
    ("pnpm", "dlx", "pnpm dlx"),
    ("yarn", "dlx", "yarn dlx"),
    ("deno", "run", "deno run"),  # reads deno.json of the working directory
    ("bundle", "exec", "bundle exec"),  # reads the Gemfile of the working directory
    ("go", "run", "go run"),
    ("cargo", "run", "cargo run"),
)
_INTERPRETERS = ("node", "bun", "deno", "ruby", "perl", "sh", "bash", "zsh")
# Environment variables that point module resolution at a directory; a relative one is the working directory.
_PATH_ENV = ("PYTHONPATH", "NODE_PATH")


def launcher_problem(definition: Mapping[str, Any]) -> str | None:
    """How a stdio ``definition`` could run code from the working directory, as the launcher's name; ``None`` when it
    cannot. A server with a ``url`` has no launcher."""
    command = definition.get("command")
    if not command and definition.get("url"):
        return None
    cwd = definition.get("cwd")
    if cwd is not None:  # the wrapper decides where a server starts; a definition does not
        return "relative_cwd" if isinstance(cwd, str) and not posixpath.isabs(cwd) else "cwd"
    if not isinstance(command, str) or not command:
        return "relative_command"
    args = [str(arg) for arg in definition.get("args") or []]
    base = posixpath.basename(command)
    for name, argument, reason in UNSAFE_LAUNCHERS:  # named first: "npx" is more telling than "relative_command"
        if base == name and (argument is None or argument in args[:1]):
            return reason
    python = base.startswith("python")
    if python and "-m" in args and "-I" not in args[:args.index("-m")]:
        return "python -m"
    if not posixpath.isabs(command):
        return "relative_command"
    if (python or base in _INTERPRETERS) and not (python and ("-m" in args or "-c" in args)):
        script = next((arg for arg in args if not arg.startswith("-")), None)
        if script is not None and not posixpath.isabs(script):
            return "relative_script"
    env = definition.get("env")
    if isinstance(env, Mapping):
        for name in _PATH_ENV:
            value = env.get(name)
            if isinstance(value, str) and any(part and not posixpath.isabs(part) for part in value.split(os.pathsep)):
                return f"relative {name}"
    return None


# The wrapper: change into the private directory, then replace the shell with the absolute command.
_WRAPPER_SCRIPT = 'cd "$1" && shift && exec "$@"'
_WRAPPER_HEAD = ["-c", _WRAPPER_SCRIPT, "sh"]
_SHELL = "/bin/sh"


def private_cwd(path: Path) -> Path:
    """Create (0700) and return ``path``, the directory wrapped servers start in: Looper's own, outside every
    task directory (``<runtime_root>/task-managers/<task_id>/mcp-cwd``)."""
    path = path.absolute()
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def is_wrapped(definition: Mapping[str, Any]) -> bool:
    args = definition.get("args")
    return (definition.get("command") == _SHELL and isinstance(args, list) and len(args) >= 5
            and args[:3] == _WRAPPER_HEAD)


def wrap(definition: Mapping[str, Any], cwd: Path) -> dict[str, Any]:
    """``definition`` started by the wrapper in ``cwd``: the command, arguments, ``env`` and the rest are the
    original's. A server with a ``url`` is returned unchanged; a stdio ``command`` must be absolute."""
    if "command" not in definition or is_wrapped(definition):
        return dict(definition)
    command = definition["command"]
    if not isinstance(command, str) or not posixpath.isabs(command):
        raise ValueError("an MCP server's command must be an absolute path")
    return {**definition, "command": _SHELL,
            "args": [*_WRAPPER_HEAD, str(cwd), command, *(str(arg) for arg in definition.get("args") or [])]}


def unwrap(definition: Mapping[str, Any]) -> dict[str, Any]:
    """The definition ``wrap`` was given (a definition that is not wrapped is returned as it is)."""
    if not is_wrapped(definition):
        return dict(definition)
    args = definition["args"]
    return {**definition, "command": args[4], "args": args[5:]}


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
    """The fingerprints of ``names`` as the library's generated ``--mcp-config`` holds them, before wrapping (the
    fingerprint of the user-scope definition)."""
    from .runtimes import library_mcp_servers

    servers = library_mcp_servers(library)
    return {name: fingerprint(unwrap(servers[name])) for name in names if name in servers}


async def library_with_allowed(library: Any, runtime_root: Path, names: Sequence[str], *,
                               environment: Mapping[str, str], cwd: Path) -> Any:
    """``library`` (``AgentLibrary``) whose ``--mcp-config`` holds its own servers (Looper's) plus ``names`` from
    the operator's user-scope configuration, every stdio server started through ``wrap`` in ``cwd`` (created).
    ``UnsafeLauncher`` for the first allowed server whose launcher is unsafe, before anything is written. A library
    without servers and an empty list is returned as it is."""
    from .mcp import write_config
    from .runtimes import library_mcp_servers

    own = library_mcp_servers(library)
    if not own and not names:
        return library
    if clash := [name for name in names if name in own]:
        raise UnresolvedServers(clash, " (the name is Looper's own server)")
    allowed = await asyncio.to_thread(resolve, names, environment=environment)
    for name, definition in allowed.items():
        if (problem := launcher_problem(definition)) is not None:
            raise UnsafeLauncher(name, problem)
    cwd = await asyncio.to_thread(private_cwd, cwd)
    servers = {name: wrap(definition, cwd) for name, definition in {**own, **allowed}.items()}
    path = await asyncio.to_thread(write_config, runtime_root, sorted(servers.items()))
    return replace(library, mcp_config=str(path))
