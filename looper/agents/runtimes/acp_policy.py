"""Fail-closed, inherited write boundary for the ACP Reviewer pilot.

The generic ACP wire client knows no vendor configuration. This separate
launch gate recognizes one deliberately narrow local command/configuration
shape; an unrecognized source is a refusal, not an assumption about safety.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import json
import http.client
import os
from pathlib import Path
import socket
import select
import subprocess
import sys
import tempfile
import tomllib
from typing import Any
from urllib.parse import unquote, urlsplit
import ipaddress

from looper.agents import git_safe
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
    PROXY_LOOPBACK_ALLOWED = "proxy_loopback_allowed"
    PROXY_PREFLIGHT_UNAVAILABLE = "proxy_preflight_unavailable"


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


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _ancestor(first: Path, second: Path) -> bool:
    """Filesystem identities handle case aliases, symlinks and hard links."""
    first, second = first.resolve(strict=True), second.resolve(strict=True)
    identity = _identity(first)
    return any(_identity(path) == identity for path in (second, *second.parents))


def _overlaps(first: Path, second: Path) -> bool:
    return _ancestor(first, second) or _ancestor(second, first)


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
                    try:
                        resolved = path.resolve(strict=True)
                    except FileNotFoundError:
                        # A reported deletion/missing file still protects its containing directory.
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
        elif (option in {"--model", "-m", "--reasoning-effort", "--effort"}
              and index + 1 < len(command) - 1 and command[index + 1]
              and not command[index + 1].startswith("-")):
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
            git_root = git_safe.run(current, "rev-parse", "--show-toplevel", capture_output=True,
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
        # Only these independently verified configuration/authentication files.
        for entry in directory.iterdir():
            if entry.name not in {"config.toml", "auth.json"} or not entry.is_file() or entry.is_symlink():
                return ConfigCheck(PolicyReason.UNKNOWN_CLI_CONFIG, f"unknown CLI configuration source: {entry}")
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
            result = git_safe.run(directory, "rev-parse", option, capture_output=True,
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


_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _proxy_ports(environment: Mapping[str, str]) -> tuple[int, ...]:
    ports = set()
    for name in _PROXY_KEYS:
        if not (value := environment.get(name)):
            continue
        proxy = urlsplit(value)
        host = proxy.hostname
        if (proxy.scheme not in {"http", "https"} or not host or proxy.path not in {"", "/"}
                or proxy.query or proxy.fragment or any(ord(c) < 32 for c in value)):
            raise ValueError("only HTTP(S) proxies with a verifiable loopback boundary are supported")
        port = proxy.port or {"http": 80, "https": 443}[proxy.scheme]
        try:
            local = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
        except ValueError:
            local = False
        if local:
            ports.add(port)
    return tuple(sorted(ports))


def _proxy_preflight(environment: Mapping[str, str]) -> None:
    """Require explicit CONNECT and plain HTTP denials, and detect forwarding.

    Probe from the host so sandbox denials cannot hide an unsafe proxy. Only
    Looper-owned listeners are targeted; NO_PROXY never bypasses this check.
    """
    for value in dict.fromkeys(environment[key] for key in _PROXY_KEYS if environment.get(key)):
        proxy = urlsplit(value)
        headers = {}
        if proxy.username is not None:
            credentials = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}".encode()
            headers["Proxy-Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
        for method, host in ((method, host) for method in ("CONNECT", "GET")
                             for host in ("127.0.0.1", "::1", "localhost", "0.0.0.0")):
            try:
                family = socket.AF_INET6 if ":" in host else socket.AF_INET
                with socket.socket(family) as listener:
                    listener.bind(("::1" if family == socket.AF_INET6 else "127.0.0.1", 0))
                    listener.listen(2)
                    port = listener.getsockname()[1]
                    # Establish and drain a positive control before the proxy attempt.
                    with socket.create_connection((host, port), timeout=1):
                        control, _ = listener.accept()
                        control.close()
                    connection_type = http.client.HTTPSConnection if proxy.scheme == "https" else http.client.HTTPConnection
                    connection = connection_type(proxy.hostname, proxy.port, timeout=2)
                    status = None
                    try:
                        target = f"[{host}]:{port}" if family == socket.AF_INET6 else f"{host}:{port}"
                        # Absolute-form GET exercises forwarding without a CONNECT tunnel.
                        connection.request(method, target if method == "CONNECT" else f"http://{target}/", headers=headers)
                        with connection.getresponse() as response:
                            status = response.status
                    except (OSError, http.client.HTTPException):
                        pass  # Connectivity alone is not evidence of a denied request.
                    finally:
                        connection.close()
                    if select.select([listener], [], [], 0)[0] or (status is not None and 200 <= status < 300):
                        raise PolicyError(ConfigCheck(PolicyReason.PROXY_LOOPBACK_ALLOWED,
                                                     f"proxy {method} permits a Looper-owned loopback listener"))
                    if status != 403:
                        raise PolicyError(ConfigCheck(PolicyReason.PROXY_PREFLIGHT_UNAVAILABLE,
                                                     f"proxy {method} did not prove an explicit loopback denial"))
            except (OSError, ValueError, http.client.HTTPException) as exc:
                raise PolicyError(ConfigCheck(PolicyReason.PROXY_PREFLIGHT_UNAVAILABLE,
                                             "proxy loopback preflight unavailable")) from exc


def _profile(scratch: Path, config_dir: Path, proxy_ports: Sequence[int] = ()) -> str:
    writable = tuple(dict.fromkeys((scratch, config_dir)))
    return "\n".join(("(version 1)", "(allow default)", "(deny file-write*)", "(deny job-creation)",
                      '(deny network-outbound (remote unix-socket))',
                      '(allow network-outbound (remote unix-socket (path "/private/var/run/mDNSResponder")))',
                      '(deny network-outbound (remote ip "localhost:*"))',
                      *(f'(allow network-outbound (remote ip "localhost:{port}"))' for port in proxy_ports),
                      '(deny lsopen)', '(deny appleevent-send)',
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
    proxy_environment: dict[str, str]

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
                                                             "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                                                             *_PROXY_KEYS, "NO_PROXY", "no_proxy"}}
        env.update(self.proxy_environment)
        env.update(HOME=str(self.config_dir), GROK_HOME=str(self.config_dir), TMPDIR=str(self.scratch),
                   GROK_DISABLE_AUTOUPDATER="1", GROK_MEMORY="0")
        for vendor in ("CLAUDE", "CURSOR"):
            for surface in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS"):
                env[f"GROK_{vendor}_{surface}_ENABLED"] = "false"
        return env

    def cleanup(self) -> None:
        self._temporary.cleanup()


def _preflight(policy: WritePolicy) -> bool:
    """Prove writable scratch, denied outside writes and denied local IPC.

    All probes belong to Looper; reported paths are never opened for writing.
    Nonblocking, no-follow opens and inode checks prevent a swapped FIFO or
    symlink from blocking or redirecting the fixed probe.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="acp-probe-", dir="/private/tmp" if sys.platform == "darwin" else None) as base:
            root = Path(base)
            probe = root / "write-probe"
            descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(descriptor)
                identity = [info.st_dev, info.st_ino]
            finally:
                os.close(descriptor)
            with socket.socket(socket.AF_UNIX) as unix, socket.socket(socket.AF_INET) as loopback:
                unix_path = str(root / "socket")
                unix.bind(unix_path)
                unix.listen(2)
                loopback.bind(("127.0.0.1", 0))
                loopback.listen(2)
                port = loopback.getsockname()[1]
                # Positive controls establish that both owned listeners accept connections.
                for family, endpoint in ((socket.AF_UNIX, unix_path), (socket.AF_INET, ("127.0.0.1", port))):
                    with socket.socket(family) as client:
                        client.settimeout(1)
                        client.connect(endpoint)
                code = """import errno,json,os,socket,stat,sys
path,identity,unix_path,port,target=json.loads(sys.argv[1])
info=os.stat(path,follow_symlinks=False)
if [info.st_dev,info.st_ino] != identity or not stat.S_ISREG(info.st_mode): sys.exit(2)
try:
 descriptor=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_NONBLOCK|os.O_NOFOLLOW)
except OSError as error:
 if error.errno != errno.EPERM: sys.exit(3)
else:
 os.close(descriptor);sys.exit(4)
for family,endpoint in ((socket.AF_UNIX,unix_path),(socket.AF_INET,("127.0.0.1",port))):
 with socket.socket(family) as client:
  client.settimeout(1)
  try: client.connect(endpoint)
  except OSError as error:
   if error.errno != errno.EPERM: sys.exit(5)
  else: sys.exit(6)
with open(target, 'w') as output: output.write('scratch-ok')
"""
                target = policy.scratch / "preflight"
                arguments = json.dumps([str(probe), identity, unix_path, port, str(target)])
                result = subprocess.run(policy.wrap([sys.executable, "-c", code, arguments]),
                                        env=policy.environment(os.environ), capture_output=True, timeout=5)
                success = result.returncode == 0 and target.read_text() == "scratch-ok"
                target.unlink(missing_ok=True)
                return success
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


async def prepare_policy(command: Sequence[str], *, cwd: str | Path, inspect_roots: Sequence[str | Path] = (),
                         config_dir: str | Path = "", runtime_root: str | Path | None = None,
                         vault: str | Path | None = None) -> WritePolicy:
    """Resolve, check and prove enforcement before the caller can spawn an agent."""
    protected = await asyncio.to_thread(_protected_paths, cwd,
        (*inspect_roots, *(path for path in (runtime_root, vault) if path is not None)))
    temporary = tempfile.TemporaryDirectory(prefix="looper-acp-review-")
    scratch = Path(temporary.name).resolve(strict=True)
    try:
        try:
            config = Path(config_dir).expanduser().resolve(strict=True) if str(config_dir) else scratch
        except (OSError, RuntimeError, ValueError) as exc:
            raise PolicyError(ConfigCheck(PolicyReason.UNREADABLE_CONFIG, f"unreadable CLI config dir: {config_dir}")) from exc
        if any(_ancestor(path, Path.home()) for path in (scratch, config)):
            raise PolicyError(ConfigCheck(PolicyReason.CONFIG_DIR_OVERLAPS, "writable directory contains operator HOME"))
        if any(_overlaps(scratch, path) for path in protected):
            raise PolicyError(ConfigCheck(PolicyReason.CONFIG_DIR_OVERLAPS, "scratch directory overlaps a protected path"))
        check = await asyncio.to_thread(config_check, command, config, protected=protected, cwd=cwd)
        if not check.ok:
            raise PolicyError(check)
        # Never accept Launch.environment(): role/project/task layers are writable
        # by the Manager and API clients, unlike the serving process environment.
        proxy_environment = {key: os.environ[key] for key in (*_PROXY_KEYS, "NO_PROXY", "no_proxy") if key in os.environ}
        try:
            ports = _proxy_ports(proxy_environment)
        except ValueError as exc:
            raise PolicyError(ConfigCheck(PolicyReason.UNKNOWN_CLI_CONFIG, "invalid or unsupported proxy configuration")) from exc
        sandbox = "/usr/bin/sandbox-exec" if sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK) else None
        if not sandbox:
            raise PolicyError(ConfigCheck(PolicyReason.WRITE_POLICY_UNAVAILABLE,
                                         "write policy unavailable: no verified write policy on this host"))
        await asyncio.to_thread(_proxy_preflight, proxy_environment)
        policy = WritePolicy(list(command), scratch, config, protected, _profile(scratch, config, ports), sandbox, temporary,
                             proxy_environment)
        if not await asyncio.to_thread(_preflight, policy):
            raise PolicyError(ConfigCheck(PolicyReason.WRITE_POLICY_UNAVAILABLE,
                                         "write policy unavailable: enforcement preflight failed"))
        return policy
    except BaseException:
        temporary.cleanup()
        raise
