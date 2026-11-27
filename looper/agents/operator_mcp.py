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
    ("npx-cli.js", None, "npx"),  # npx's own script, run by node
    ("npm", "exec", "npm exec"),
    ("npm", "x", "npm x"),
    ("pnpm", "dlx", "pnpm dlx"),
    ("pnpm", "exec", "pnpm exec"),
    ("yarn", "exec", "yarn exec"),
    ("yarn", "dlx", "yarn dlx"),
    ("deno", "run", "deno run"),  # reads deno.json of the working directory
    ("bundle", "exec", "bundle exec"),  # reads the Gemfile of the working directory
    ("go", "run", "go run"),
    ("cargo", "run", "cargo run"),
)
_INTERPRETERS = ("node", "bun", "deno", "ruby", "perl")
# Programs that run another program, or a script string, so what they start cannot be told from the definition: never
# a launcher of an allowed server.
_INDIRECT = ("env", "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "xargs", "nohup", "exec", "command",
             "time", "nice", "sudo", "doas", "arch", "caffeinate")
# Variables that make an interpreter load code from a path or a string of the definition's choosing, or change which
# project it reads. Not allowed in an allowed server's ``env`` at all, whatever the value (fail closed).
_CODE_ENV = ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONUSERBASE", "NODE_PATH", "NODE_OPTIONS", "RUBYOPT",
             "RUBYLIB", "PERL5OPT", "PERL5LIB", "BUNDLE_GEMFILE", "GOFLAGS", "LD_PRELOAD", "LD_LIBRARY_PATH",
             "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH")


def _names(command: str) -> set[str]:
    """The case-normalized basenames the command goes by: as written and after resolving symbolic links."""
    found = {posixpath.basename(command).casefold()}
    if posixpath.isabs(command):
        found.add(posixpath.basename(os.path.realpath(command)).casefold())
    return found


def _module_flag(args: Sequence[str]) -> int | None:
    """The index of the ``-m`` option of a Python command line, glued (``-mpkg``) or in a cluster included."""
    for index, arg in enumerate(args):
        if not arg.startswith("-"):
            return None  # the script: what follows is its own arguments
        if arg.startswith("--"):
            continue
        if "m" in arg[1:] and not any(c in arg[1:arg[1:].index("m") + 1] for c in "cW"):
            return index
    return None


def launcher_problem(definition: Mapping[str, Any]) -> str | None:
    """How a stdio ``definition`` could run code from the working directory or something the definition cannot show, as
    the launcher's name; ``None`` when it cannot. A server with a ``url`` has no launcher. The wrapper is the
    mechanism (a private start directory); this is defense in depth."""
    command = definition.get("command")
    if not command and definition.get("url"):
        return None
    cwd = definition.get("cwd")
    if cwd is not None:  # the wrapper decides where a server starts; a definition does not
        return "relative_cwd" if isinstance(cwd, str) and not posixpath.isabs(cwd) else "cwd"
    if not isinstance(command, str) or not command:
        return "relative_command"
    args = [str(arg) for arg in definition.get("args") or []]
    names = _names(command)
    if names & set(_INTERPRETERS) and (script := next((arg for arg in args if not arg.startswith("-")), None)):
        names |= _names(script)  # `node .../npx-cli.js` is npx
    for name, argument, reason in UNSAFE_LAUNCHERS:  # named first: "npx" is more telling than "relative_command"
        if name in names and (argument is None or argument in args):
            return reason
    if names & set(_INDIRECT):
        return f"{sorted(names & set(_INDIRECT))[0]} (starts another program)"
    python = any(name.startswith("python") for name in names)
    if python and (at := _module_flag(args)) is not None:
        before = [*args[:at], args[at].split("m", 1)[0]]  # the options up to the module flag, its own cluster included
        isolated = any(arg.startswith("-") and not arg.startswith("--") and "I" in arg[1:] for arg in before)
        if not isolated:
            return "python -m"
    if "/" in command and not posixpath.isabs(command):  # a bare name resolves through PATH
        return "relative_command"
    if (python or names & set(_INTERPRETERS)) and not (python and (_module_flag(args) is not None or "-c" in args)):
        script = next((arg for arg in args if not arg.startswith("-")), None)
        if script is not None and not posixpath.isabs(script):
            return "relative_script"
    env = definition.get("env")
    if isinstance(env, Mapping):
        for name in sorted(env):
            if str(name).upper() in _CODE_ENV:
                return f"env {name}"
    return None


# The wrapper: change into the private directory, then replace the shell with the absolute command.
_WRAPPER_SCRIPT = 'cd "$1" && shift && exec "$@"'
_WRAPPER_HEAD = ["-c", _WRAPPER_SCRIPT, "sh"]
_SHELL = "/bin/sh"


def private_cwd(path: Path) -> Path:
    """Create (0700) and return the real path of ``path``, the directory wrapped servers start in: Looper's own,
    outside every task directory (``<runtime_root>/task-managers/<task_id>/mcp-cwd``)."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path.resolve()


def reset_cwd(path: Path) -> None:
    """Empty the start directory before a new process, so nothing a previous server left is in it."""
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def is_wrapped(definition: Mapping[str, Any]) -> bool:
    args = definition.get("args")
    return (definition.get("command") == _SHELL and isinstance(args, list) and len(args) >= 5
            and args[:3] == _WRAPPER_HEAD)


def wrap(definition: Mapping[str, Any], cwd: Path) -> dict[str, Any]:
    """``definition`` started by the wrapper in ``cwd``: the command, arguments, ``env`` and the rest are the
    original's. A server with a ``url`` is returned unchanged; a stdio ``command`` is absolute or a bare name. A
    definition that already has the wrapper's shape is wrapped like any other: its directory is never trusted."""
    if "command" not in definition:
        return dict(definition)
    command = definition["command"]
    if not isinstance(command, str) or not command or ("/" in command and not posixpath.isabs(command)):
        raise ValueError("an MCP server's command must be an absolute path or a bare name resolved through PATH")
    return {**definition, "command": _SHELL,
            "args": [*_WRAPPER_HEAD, str(cwd), command, *(str(arg) for arg in definition.get("args") or [])]}


def unwrap(definition: Mapping[str, Any], private_dir: Path | None = None) -> dict[str, Any]:
    """The definition ``wrap`` was given. With ``private_dir`` (the directory the launch expects), a definition that is
    not wrapped into exactly that directory is a ``ValueError``: a changed directory, shell, script or missing part
    is refused. Without it a definition that is not wrapped is returned as it is."""
    if private_dir is not None:
        if not is_wrapped(definition) or definition["args"][3] != str(private_dir):
            raise ValueError("the server is not started by Looper's wrapper in the expected directory")
    elif not is_wrapped(definition):
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
    # A command with a path separator that is not absolute is resolved by Claude Code against the task directory
    # (verified with 2.1.332), which the model can write. A bare name resolves through PATH.
    if relative := [name for name in names if _relative_command(user[name])]:
        raise UnresolvedServers(relative, " (a command with a path separator must be an absolute path; a relative "
                                          "one would be resolved in the task directory)")
    return {name: user[name] for name in names}


def _relative_command(definition: Mapping[str, Any]) -> bool:
    command = definition.get("command")
    return isinstance(command, str) and "/" in command and not posixpath.isabs(command)


_KNOWN_FIELDS = frozenset({"type", "command", "args", "env", "headers", "url", "cwd"})


def fingerprint_input(definition: Mapping[str, Any]) -> dict[str, Any]:
    """What identifies a definition without its secrets: transport, command, args, ``cwd``, the names of its
    variables and headers, a URL without userinfo and query values (its port and the sorted names of its query
    parameters stay), and each unknown field as a hash of its value."""
    from urllib.parse import parse_qsl, urlsplit

    url = definition.get("url")
    parts = urlsplit(url) if isinstance(url, str) else None
    try:
        port = parts.port if parts else None
    except ValueError:
        port = -1
    shown = None
    if parts and parts.hostname:
        shown = f"{parts.scheme}://{parts.hostname}{f':{port}' if port else ''}{parts.path}"
    return {"transport": definition.get("type") or ("stdio" if "command" in definition else "http"),
            "command": definition.get("command"), "args": list(definition.get("args") or []),
            "cwd": definition.get("cwd"),
            "env_keys": sorted(definition.get("env") or {}), "header_keys": sorted(definition.get("headers") or {}),
            "url": shown, "query_names": sorted(name for name, _ in parse_qsl(parts.query, keep_blank_values=True))
            if parts else [],
            "extra": {name: hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()
                      for name, value in sorted(definition.items()) if name not in _KNOWN_FIELDS}}


def fingerprint(definition: Mapping[str, Any]) -> str:
    """The SHA-256 of ``fingerprint_input``."""
    return hashlib.sha256(json.dumps(fingerprint_input(definition), sort_keys=True).encode("utf-8")).hexdigest()


def fingerprints(library: Any, names: Sequence[str], private_dir: Path | None = None) -> dict[str, str]:
    """The fingerprints of ``names`` as the library's generated ``--mcp-config`` holds them, before wrapping (the
    fingerprint of the user-scope definition)."""
    from .runtimes import library_mcp_servers

    servers = library_mcp_servers(library)
    if missing := [name for name in names if name not in servers]:
        raise UnresolvedServers(missing, " (not in the generated --mcp-config)")
    return {name: fingerprint(unwrap(servers[name], private_dir)) for name in names}


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
    # Beside the task Manager's own directory, not in the shared ``.local/mcp``: ``sweep_orphans`` removes these
    # after a crash, and no other agent's configuration is in the way.
    path = await asyncio.to_thread(lambda: write_config(runtime_root, sorted(servers.items()),
                                                        directory=_config_directory(cwd)))
    return replace(library, mcp_config=str(path))


def _config_directory(cwd: Path) -> Path:
    return cwd.absolute().parent / "mcp"


def sweep_orphans(runtime_root: Path) -> int:
    """Remove the generated configs (they can hold secrets) that a crashed server left under
    ``<runtime_root>/task-managers/*/mcp``; run at service start, when no task Manager is resident. Returns how many."""
    removed = 0
    for path in (runtime_root.absolute() / "task-managers").glob("*/mcp/*.json"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
