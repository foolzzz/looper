"""Fail-closed, inherited write boundary for the ACP Reviewer pilot.

The generic ACP wire client knows no vendor configuration. This separate
launch gate recognizes one deliberately narrow local command/configuration
shape; an unrecognized source is a refusal, not an assumption about safety.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Any

from .channel import RunStartError


class PolicyReason(str, Enum):
    OK = "ok"
    CONFIG_DIR_OVERLAPS = "config_dir_overlaps"
    EXTERNAL_MCP_SERVER = "external_mcp_server"
    EXTERNAL_WRITER = "external_writer"
    UNKNOWN_CLI_CONFIG = "unknown_cli_config"
    UNREADABLE_CONFIG = "unreadable_config"
    UNRESOLVABLE_PROTECTED_PATH = "unresolvable_protected_path"
    WRITE_POLICY_UNAVAILABLE = "write_policy_unavailable"


@dataclass(frozen=True)
class ConfigCheck:
    reason: PolicyReason
    detail: str

    @property
    def ok(self) -> bool:
        return self.reason is PolicyReason.OK

    @property
    def record(self) -> dict[str, str]:
        return {"reason": self.reason.value, "detail": self.detail}


class PolicyError(RunStartError):
    def __init__(self, check: ConfigCheck) -> None:
        super().__init__(check.detail)
        self.reason = check.reason
        self.record = check.record


def _overlaps(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def report_inspect_roots(work_result: Any, cwd: str | Path) -> tuple[str, ...]:
    """The external filesystem locations the verify prompt presents as data.

    Preserve unresolved directories so the launch gate can name and reject
    them. File symlinks also protect the target's parent, even when the link
    itself is inside the review worktree. No report text is executed.
    """
    from .. import reported_files

    if not isinstance(work_result, Mapping):
        return ()
    current = Path(cwd).resolve()
    roots: dict[str, str] = {}

    def directory(path: Path) -> None:
        try:
            resolved = path.resolve()
            if resolved.is_relative_to(current):
                return
        except (OSError, RuntimeError, ValueError):
            resolved = path  # Strict resolution in prepare_policy will refuse this item.
        roots.setdefault(str(resolved), str(path))

    for item in reported_files.locations(work_result.get("found_elsewhere")):
        if item["where"] == "worktree":
            directory(Path(item["worktree"]))
    for field in ("changed_files", "deleted_files"):
        paths = work_result.get(field, ())
        if field == "changed_files" and isinstance(paths, dict):
            paths = paths.keys()
        elif not isinstance(paths, (list, tuple)):
            continue
        for value in paths:
            if not isinstance(value, str) or not value:
                continue
            path = Path(value)
            if not path.is_absolute():
                path = current / path
            directory(path.parent)
            if field == "changed_files":
                try:
                    resolved = path.resolve()
                except (OSError, RuntimeError, ValueError):
                    # A loop/unresolvable changed path must not disappear from
                    # the protected set merely because its parent exists.
                    roots.setdefault(str(path), str(path))
                else:
                    directory(resolved.parent)
    return tuple(roots.values())


def _command_check(command: Sequence[str]) -> ConfigCheck:
    if not command or Path(command[0]).name != "grok" or len(command) < 3 or command[1] != "agent":
        return ConfigCheck(PolicyReason.UNKNOWN_CLI_CONFIG, "unknown CLI configuration: only grok agent stdio is verified")
    # Only options whose configuration discovery is understood are accepted.
    # In particular, profile/plugin/leader/config overrides are not guessed at.
    index = 2
    while index < len(command) - 1:
        option = command[index]
        if option == "--no-leader":
            index += 1
        elif option in {"--model", "-m", "--reasoning-effort", "--effort"} and index + 1 < len(command) - 1:
            index += 2
        else:
            return ConfigCheck(PolicyReason.UNKNOWN_CLI_CONFIG,
                               f"unknown CLI configuration: unsupported argument at argv[{index}]")
    if command[-1] != "stdio":
        return ConfigCheck(PolicyReason.UNKNOWN_CLI_CONFIG, "unknown CLI configuration: expected agent stdio")
    return ConfigCheck(PolicyReason.OK, "known Grok stdio command")


def config_check(command: Sequence[str], config_dir: str | Path, *, protected: Sequence[Path] = (),
                 cwd: str | Path | None = None) -> ConfigCheck:
    """Inspect the known command's configuration without starting any CLI.

    A missing config is intentionally not synthesized: v0 cannot prove what an
    unconfigured CLI will discover. Settings can show this check before a run;
    launch repeats it with all resolved protected paths and the actual cwd.
    """
    if not (check := _command_check(command)).ok:
        return check
    try:
        directory = Path(config_dir).expanduser().resolve(strict=True)
        if not directory.is_dir():
            raise NotADirectoryError(str(directory))
        for path in protected:
            if _overlaps(directory, Path(path).resolve(strict=True)):
                return ConfigCheck(PolicyReason.CONFIG_DIR_OVERLAPS, f"config dir overlaps protected path: {path}")
        if cwd is not None:
            current = Path(cwd).resolve(strict=True)
            git_root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=current, capture_output=True,
                                      text=True, timeout=5, check=True).stdout.strip()
            root = Path(git_root).resolve(strict=True)
            # Grok 1.0.89 inspect confirms both sources are discovered even
            # with the Claude/Cursor compatibility MCP switches disabled.
            for ancestor in (current, *current.parents):
                for name in (".mcp.json", ".grok"):
                    source = ancestor / name
                    try:
                        source.lstat()
                    except FileNotFoundError:
                        continue
                    return ConfigCheck(PolicyReason.UNKNOWN_CLI_CONFIG,
                                       f"unknown CLI configuration source outside config dir: {source}")
                if ancestor == root:
                    break
        # These can introduce additional MCP/plugin/profile configuration.
        for name in (".mcp.json", "plugins", "agents", "agent.md", "settings.json"):
            if (directory / name).exists():
                return ConfigCheck(PolicyReason.UNKNOWN_CLI_CONFIG, f"unknown CLI configuration source: {directory / name}")
        config = tomllib.loads((directory / "config.toml").read_text(encoding="utf-8"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return ConfigCheck(PolicyReason.UNREADABLE_CONFIG, f"unreadable CLI config in {config_dir}: {type(exc).__name__}")
    # Supporting further config keys requires checking their discovery and
    # process behavior first. The pilot accepts a minimal separate config.
    if config.keys() - {"cli", "mcp_servers"}:
        return ConfigCheck(PolicyReason.UNKNOWN_CLI_CONFIG, "unknown CLI configuration sections; use a separate minimal config dir")
    cli = config.get("cli", {})
    if not isinstance(cli, dict) or cli.keys() - {"use_leader", "auto_update"}:
        return ConfigCheck(PolicyReason.UNKNOWN_CLI_CONFIG, "unknown CLI configuration in [cli]")
    if cli.get("use_leader") is not False and "--no-leader" not in command:
        return ConfigCheck(PolicyReason.EXTERNAL_WRITER, "shared leader behavior is not disabled; set cli.use_leader=false")
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return ConfigCheck(PolicyReason.UNKNOWN_CLI_CONFIG, "unknown CLI configuration: mcp_servers must be a table")
    for name, server in servers.items():
        if not isinstance(server, dict):
            return ConfigCheck(PolicyReason.UNKNOWN_CLI_CONFIG, f"unknown MCP configuration: {name}")
        transport = server.get("type", "stdio")
        if "url" in server or transport in ("http", "sse", "streamable-http"):
            return ConfigCheck(PolicyReason.EXTERNAL_MCP_SERVER, f"external MCP server refused: {name}; only stdio child processes are allowed")
        args, env = server.get("args", []), server.get("env", {})
        if (transport != "stdio" or not isinstance(server.get("command"), str)
                or not server["command"].strip() or not isinstance(args, list)
                or not all(isinstance(item, str) for item in args) or not isinstance(env, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
                or server.keys() - {"command", "args", "env", "type", "enabled"}):
            return ConfigCheck(PolicyReason.UNKNOWN_CLI_CONFIG, f"unknown MCP configuration: {name}")
    return ConfigCheck(PolicyReason.OK, "known Grok configuration; MCP servers are stdio children and shared leader is disabled")


def _protected_paths(cwd: str | Path, inspect_roots: Sequence[str | Path]) -> tuple[Path, ...]:
    try:
        directory = Path(cwd).resolve(strict=True)
        paths = []
        for option in ("--show-toplevel", "--git-dir", "--git-common-dir"):
            result = subprocess.run(["git", "rev-parse", option], cwd=directory, capture_output=True,
                                    text=True, timeout=5, check=True)
            path = Path(result.stdout.strip())
            paths.append((path if path.is_absolute() else directory / path).resolve(strict=True))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise PolicyError(ConfigCheck(PolicyReason.UNRESOLVABLE_PROTECTED_PATH,
                                     f"cannot resolve protected git path in {cwd}: {type(exc).__name__}")) from exc
    for value in inspect_roots:
        try:
            path = Path(value).expanduser()
            paths.append((path if path.is_absolute() else directory / path).resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise PolicyError(ConfigCheck(PolicyReason.UNRESOLVABLE_PROTECTED_PATH,
                                         f"cannot resolve protected inspect root: {value}")) from exc
    return tuple(dict.fromkeys(paths))


def _profile(scratch: Path, config_dir: Path) -> str:
    writable = tuple(dict.fromkeys((scratch, config_dir)))
    return "\n".join(("(version 1)", "(allow default)", "(deny file-write*)", "(deny job-creation)",
                      *(f"(allow file-write* (subpath {json.dumps(str(path))}))" for path in writable),
                      '(allow file-write* (literal "/dev/null"))'))


@dataclass
class WritePolicy:
    command: list[str]
    scratch: Path
    config_dir: Path
    protected: tuple[Path, ...]
    profile: str
    sandbox_executable: str
    _temporary: tempfile.TemporaryDirectory[str]

    @property
    def record(self) -> dict[str, Any]:
        return {"reason": PolicyReason.OK.value, "mechanism": "macos-seatbelt", "looper_mcp_injected": False,
                "config_dir_is_scratch": self.config_dir == self.scratch}

    def wrap(self, command: Sequence[str] | None = None) -> list[str]:
        return [self.sandbox_executable, "-p", self.profile, *(self.command if command is None else command)]

    def environment(self, inherited: Mapping[str, str]) -> dict[str, str]:
        # Never let an inherited selector bypass the directory we checked.
        env = {key: value for key, value in inherited.items()
               if not key.startswith("GROK_") and key not in {"CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME",
                                                             "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"}}
        env.update(HOME=str(self.config_dir), GROK_HOME=str(self.config_dir), TMPDIR=str(self.scratch),
                   GROK_DISABLE_AUTOUPDATER="1", GROK_MEMORY="0")
        for vendor in ("CLAUDE", "CURSOR"):
            for surface in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS"):
                env[f"GROK_{vendor}_{surface}_ENABLED"] = "false"
        return env

    def cleanup(self) -> None:
        self._temporary.cleanup()


def _preflight(policy: WritePolicy) -> bool:
    """Positive writable control, inherited EPERM controls, and a scratch write.

    Protected probes only open for append, writing zero bytes. Even if the
    host stops enforcing the sandbox, existing content cannot be changed.
    Looper creates and removes an empty canary in each protected directory.
    """
    created: list[Path] = []
    probes: list[str] = []
    try:
        for protected in policy.protected:
            if protected.is_dir():
                descriptor, name = tempfile.mkstemp(prefix=".looper-acp-policy-", dir=protected)
                os.close(descriptor)
                path = Path(name)
                created.append(path)
            else:
                path = protected
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
            os.close(descriptor)
            probes.append(str(path))
        code = """import errno,json,os,sys
for path in json.loads(sys.argv[1]):
 try:
  descriptor=os.open(path,os.O_WRONLY|os.O_APPEND)
 except OSError as error:
  if error.errno != errno.EPERM: sys.exit(2)
 else:
  os.close(descriptor);sys.exit(3)
with open(sys.argv[2], 'w') as output: output.write('scratch-ok')
"""
        target = policy.scratch / "preflight"
        result = subprocess.run(policy.wrap([sys.executable, "-c", code, json.dumps(probes), str(target)]),
                                env=policy.environment(os.environ), capture_output=True, timeout=5)
        success = result.returncode == 0 and target.read_text() == "scratch-ok"
        target.unlink(missing_ok=True)
        return success
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    finally:
        for path in created:
            path.unlink(missing_ok=True)


async def prepare_policy(command: Sequence[str], *, cwd: str | Path, inspect_roots: Sequence[str | Path] = (),
                         config_dir: str | Path = "") -> WritePolicy:
    """Resolve, check and prove enforcement before the caller can spawn an agent."""
    protected = await asyncio.to_thread(_protected_paths, cwd, inspect_roots)
    temporary = tempfile.TemporaryDirectory(prefix="looper-acp-review-")
    scratch = Path(temporary.name).resolve(strict=True)
    try:
        try:
            config = Path(config_dir).expanduser().resolve(strict=True) if str(config_dir) else scratch
        except (OSError, RuntimeError, ValueError) as exc:
            raise PolicyError(ConfigCheck(PolicyReason.UNREADABLE_CONFIG, f"unreadable CLI config dir: {config_dir}")) from exc
        if any(_overlaps(scratch, path) for path in protected):
            raise PolicyError(ConfigCheck(PolicyReason.CONFIG_DIR_OVERLAPS, "scratch directory overlaps a protected path"))
        check = await asyncio.to_thread(config_check, command, config, protected=protected, cwd=cwd)
        if not check.ok:
            raise PolicyError(check)
        sandbox = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
        if not sandbox:
            raise PolicyError(ConfigCheck(PolicyReason.WRITE_POLICY_UNAVAILABLE,
                                         "write policy unavailable: no verified write policy on this host"))
        policy = WritePolicy(list(command), scratch, config, protected, _profile(scratch, config), sandbox, temporary)
        if not await asyncio.to_thread(_preflight, policy):
            raise PolicyError(ConfigCheck(PolicyReason.WRITE_POLICY_UNAVAILABLE,
                                         "write policy unavailable: enforcement preflight failed"))
        return policy
    except BaseException:
        temporary.cleanup()
        raise
