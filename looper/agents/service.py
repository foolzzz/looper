"""``looper service``: run ``looper serve`` as a managed background service.

macOS gets a launchd LaunchAgent (``~/Library/LaunchAgents/<label>.plist``),
Linux a systemd user unit (``~/.config/systemd/user/<label>.service``). Either
way the service starts at login, is started again when it exits with a failure
(a crash or a kill), and writes its output to ``<runtime_root>/logs/serve.log``.

The unit runs the ``looper`` of the interpreter that ran ``install`` (a
checkout's ``.venv/bin/looper``) in the directory ``install`` ran in, as
``looper serve --config <ops file> --runtime-root <root>``, both absolute, and
``--port`` when explicitly selected or retained from an installed unit: the ops file
(``looper.ops_config``) holds the database URL and the other settings, so the
unit holds none of them. launchd and systemd start services with an almost
empty environment, so ``install`` copies the variables the service and its
agents need (``CAPTURED_NAMES``, and each ``--env NAME``) from its own
environment into the unit, which is why the unit file is written with mode
0600: they can hold API keys.

launchd: a job is reloaded with ``launchctl bootout`` then ``bootstrap``, never
``kickstart -k``, which can reuse the code-signing state of the binary it
started before. ``bootout`` returns before the job is gone, and a ``bootstrap``
issued meanwhile fails, so ``stop`` waits until ``launchctl print`` no longer
finds the job and ``bootstrap`` is retried a few times.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import platform
import plistlib
import re
import shlex
import socket
import shutil
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import child_env, ops_config
from ..ops_config import OpsConfig
from . import operator_auth
from .self_check import OK, SKIP, WARN, Check

DEFAULT_LABEL = "com.looper.serve"
DEFAULT_PORT = 8767
DEFAULT_TIMEOUT_SECONDS = 120.0
LOG_RELATIVE_PATH = Path("logs") / "serve.log"

# Copied from the environment of `install` when set: what the server and the agents it starts read
# (``child_env``) but launchd and systemd do not set, and the Telegram channel's token variable.
# Not the session's own (HOME, SSH_AUTH_SOCK, TMPDIR: the service manager sets them), not CLAUDECODE
# or a Claude Code session's other markers, and no ops setting: the ops file holds those.
CAPTURED_NAMES = (
    "PATH", "TZ", *(name for name in child_env.SYSTEM if name.startswith(("LANG", "LC_"))),
    *child_env.NETWORK, *child_env.RUNTIMES, "LOOPER_TELEGRAM_BOT_TOKEN", "LOOPX_TELEGRAM_BOT_TOKEN",
)
# What `install` copied before the ops file, by prefix: a variable of these it no longer copies is named in its report.
FORMER_PREFIXES = ("LOOPER_", "LOOPX_", "ANTHROPIC_", "LARK_")

_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_STOP_WAIT_SECONDS = 30.0  # launchd sends SIGKILL 20 s after SIGTERM by default
_POLL_SECONDS = 0.5
_BOOTSTRAP_ATTEMPTS = 10
_COMMAND_TIMEOUT_SECONDS = 30.0


class ServiceError(RuntimeError):
    """A service command could not do its job; the message says why and what to do."""


def _run(argv: Sequence[str], *, timeout: float = _COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=timeout)
    except FileNotFoundError as exc:
        raise ServiceError(f"`{argv[0]}` is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ServiceError(f"`{' '.join(argv)}` did not finish in {timeout:.0f} s") from exc


def _get_status(port: int, token: str | None) -> dict | None:
    """``POST /api/v1/system/status`` on 127.0.0.1, bypassing any proxy; None when nothing answers with JSON."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/system/status",
                                    data=b"{}", headers={"Content-Type": "application/json",
                                                           **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with opener.open(request, timeout=3) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"status": "unauthorized"} if exc.code == 401 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return (payload["result"] if isinstance(payload, dict) and payload.get("status") == "ok"
            and isinstance(payload.get("result"), dict) else None)


def _get_components(port: int, token: str | None) -> dict | None:
    """Read the startup snapshot through the operator-authenticated v1 API."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/system/components",
                                    headers={"Authorization": f"Bearer {token}"} if token else {}, method="POST")
    try:
        with opener.open(request, timeout=3) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise ServiceError(f"components endpoint returned HTTP {exc.code}; check the running version and operator token") from exc
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ServiceError("cannot read running component fingerprints") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ok" or not isinstance(payload.get("result"), dict):
        raise ServiceError("invalid response from components endpoint")
    return payload["result"]


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _system_operation(port: int, token: str, name: str, arguments: dict) -> dict:
    return _request_json(port, token, f"/api/v1/system/{name}", arguments)


def _request_json(port: int, token: str, path: str, arguments: dict | None = None) -> dict:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
        data=json.dumps(arguments).encode() if arguments is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=3) as response:
            payload = json.loads(response.read())
        if isinstance(payload, dict) and payload.get("status") == "ok" and isinstance(payload.get("result"), dict):
            return payload["result"]
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ServiceError(f"{path} failed: {exc}") from exc
    raise ServiceError(f"invalid response from {path}")


def _get_whoami(port: int, token: str) -> dict:
    return _request_json(port, token, "/api/v1/whoami")


def _get_index(port: int, token: str) -> bytes:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(f"http://127.0.0.1:{port}/index.html",
                                         headers={"Cookie": f"{operator_auth.cookie_name(port)}={token}"})
        with opener.open(request, timeout=3) as response:
            return response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise ServiceError("the installed dashboard index.html is not served") from exc


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@dataclass
class Host:
    """What the service commands read and run, replaceable in tests."""

    system: str  # platform.system()
    home: Path
    uid: int
    environ: Mapping[str, str]
    executable: str  # the interpreter running looper
    cwd: Path
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    get_status: Callable[[int, str | None], dict | None] = _get_status
    port_in_use: Callable[[int], bool] = _port_in_use
    get_components: Callable[[int, str | None], dict | None] = _get_components
    system_operation: Callable[[int, str, str, dict], dict] = _system_operation
    get_whoami: Callable[[int, str], dict] = _get_whoami
    get_index: Callable[[int, str], bytes] = _get_index
    pid_alive: Callable[[int], bool] = _pid_alive

    @classmethod
    def current(cls) -> Host:
        return cls(system=platform.system(), home=Path.home(), uid=os.getuid() if hasattr(os, "getuid") else 0,
                   environ=dict(os.environ), executable=sys.executable, cwd=Path.cwd(), run=_run)


# --- What the unit runs ---------------------------------------------------------


@dataclass(frozen=True)
class ServiceSpec:
    label: str
    argv: tuple[str, ...]
    working_dir: Path
    env: tuple[tuple[str, str], ...]
    log_path: Path


def looper_command(executable: str) -> list[str]:
    """The current ``looper``: the console script next to the interpreter, else ``python -m looper.cli``."""
    script = Path(os.path.abspath(executable)).parent / "looper"
    if script.is_file() and os.access(script, os.X_OK):
        return [str(script)]
    return [os.path.abspath(executable), "-m", "looper.cli"]


def captured_env(environ: Mapping[str, str], extra: Sequence[str] = ()) -> dict[str, str]:
    """The variables of ``environ`` the unit carries: the fixed names and ``extra``."""
    wanted = set(CAPTURED_NAMES) | set(extra)
    return {name: environ[name] for name in sorted(environ) if name in wanted}


def build_spec(host: Host, *, label: str, port: int | None, runtime_root: Path, config: Path,
               extra_env: Sequence[str] = ()) -> ServiceSpec:
    """The unit: ``looper serve`` with the ops file ``config`` and ``runtime_root``, and ``--port`` when given."""
    check_label(label)
    if port is not None and not 0 < port < 65536:
        raise ServiceError(f"--port {port} is not a TCP port")
    missing = [name for name in extra_env if name not in host.environ]
    if missing:
        raise ServiceError(f"--env {', '.join(missing)}: not set in this environment")
    if settings := sorted(set(extra_env) & ops_config.VARIABLES):
        raise ServiceError(f"--env {', '.join(settings)}: an ops setting, which the unit does not carry; set it in "
                           f"{config} instead")
    root = _absolute_root(host, runtime_root)
    argv = (*looper_command(host.executable), "serve", "--config", str(config), "--runtime-root", str(root),
            *(("--port", str(port)) if port is not None else ()))
    return ServiceSpec(label=label, argv=argv, working_dir=host.cwd,
                       env=tuple(captured_env(host.environ, extra_env).items()), log_path=root / LOG_RELATIVE_PATH)


def _absolute_root(host: Host, runtime_root: Path) -> Path:
    return Path(os.path.abspath(os.path.join(host.cwd, os.path.expanduser(str(runtime_root)))))


def check_label(label: str) -> None:
    if not _LABEL.fullmatch(label):
        raise ServiceError(f"--label {label!r}: use letters, digits, '.', '_' and '-', starting with a letter or digit")


def render_plist(spec: ServiceSpec) -> bytes:
    return plistlib.dumps({
        "Label": spec.label,
        "ProgramArguments": list(spec.argv),
        "WorkingDirectory": str(spec.working_dir),
        "EnvironmentVariables": dict(spec.env),
        "RunAtLoad": True,
        # Started again after a failure (a non-zero exit or a signal), as
        # systemd's Restart=on-failure; a clean exit after SIGTERM stays down.
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(spec.log_path),
        "StandardErrorPath": str(spec.log_path),
    })


def _systemd_quote(word: str) -> str:
    """One ExecStart word or Environment assignment: quoted and C-escaped, with ``%`` kept literal."""
    escaped = word.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("%", "%%")
    return f'"{escaped}"'


def render_systemd_unit(spec: ServiceSpec) -> str:
    exec_start = " ".join(_systemd_quote(word.replace("$", "$$")) for word in spec.argv)
    log = str(spec.log_path).replace("%", "%%")
    lines = [
        "[Unit]",
        f"Description=Looper agent system (looper serve, {spec.label})",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        f"WorkingDirectory={str(spec.working_dir).replace('%', '%%')}",
        *(f"Environment={_systemd_quote(f'{name}={value}')}" for name, value in spec.env),
        "Restart=on-failure",
        "RestartSec=5",
        f"StandardOutput=append:{log}",
        f"StandardError=append:{log}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _systemd_words(value: str) -> list[str]:
    """The words of an ExecStart value as render_systemd_unit wrote them."""
    words, current, quoted, chars = [], [], False, iter(value)
    for char in chars:
        if char == "\\":
            escaped = next(chars, "")
            current.append({"n": "\n"}.get(escaped, escaped))
        elif char == '"':
            quoted = not quoted
        elif char.isspace() and not quoted:
            if current:
                words.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        words.append("".join(current))
    return [word.replace("%%", "%").replace("$$", "$") for word in words]


def write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` with mode 0600, replacing the file in one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# --- The service managers -----------------------------------------------------------


@dataclass
class State:
    """What the service manager knows about the service right now."""

    installed: bool  # the unit file exists
    loaded: bool
    running: bool
    pid: int | None = None
    last_exit: str = ""
    port: int | None = None
    log_path: Path | None = None
    runtime_root: Path | None = None


class _Manager:
    kind = ""

    def __init__(self, host: Host, label: str) -> None:
        check_label(label)
        self.host, self.label = host, label

    @property
    def unit_path(self) -> Path:
        raise NotImplementedError

    def render(self, spec: ServiceSpec) -> bytes:
        raise NotImplementedError

    def unit_settings(self, *, explicit_port: bool = False) -> tuple[int | None, Path | None, Path | None]:
        """The unit's port, log path and root; ``explicit_port`` ignores a port read from its ops file."""
        raise NotImplementedError

    def state(self) -> State:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def remove(self) -> None:
        raise NotImplementedError

    def _call(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return self.host.run(list(argv))


class Launchd(_Manager):
    kind = "launchd LaunchAgent"

    @property
    def unit_path(self) -> Path:
        return self.host.home / "Library" / "LaunchAgents" / f"{self.label}.plist"

    @property
    def target(self) -> str:
        return f"gui/{self.host.uid}/{self.label}"

    def render(self, spec: ServiceSpec) -> bytes:
        return render_plist(spec)

    def unit_settings(self, *, explicit_port: bool = False) -> tuple[int | None, Path | None, Path | None]:
        try:
            unit = plistlib.loads(self.unit_path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None, None, None
        log = unit.get("StandardOutPath")
        argv = unit.get("ProgramArguments") or []
        port = _given_port(argv) if explicit_port else _port_of(argv)
        return port, Path(log) if log else None, _runtime_root_of(argv)

    def _print(self) -> subprocess.CompletedProcess[str]:
        return self._call("launchctl", "print", self.target)

    def state(self) -> State:
        printed = self._print()
        # Top-level fields only: one tab deep; nested blocks are indented further.
        found = re.findall(r"^\t([a-z][a-z ]*?) = (.*)$", printed.stdout, re.M) if printed.returncode == 0 else []
        fields = dict(found)
        pid = int(fields["pid"]) if fields.get("pid", "").isdigit() else None
        last_exit = fields.get("last terminating signal") or fields.get("last exit code", "")
        port, log, runtime_root = self.unit_settings()
        return State(installed=self.unit_path.is_file(), loaded=printed.returncode == 0,
                     running=fields.get("state") == "running" and pid is not None, pid=pid,
                     last_exit=last_exit, port=port, log_path=log, runtime_root=runtime_root)

    def stop(self) -> None:
        if self._print().returncode != 0:
            return
        self._call("launchctl", "bootout", self.target)
        deadline = self.host.clock() + _STOP_WAIT_SECONDS
        while self._print().returncode == 0:
            if self.host.clock() >= deadline:
                raise ServiceError(f"launchd still lists {self.target} {_STOP_WAIT_SECONDS:.0f} s after "
                                   "`launchctl bootout`")
            self.host.sleep(_POLL_SECONDS)

    def start(self) -> None:
        for attempt in range(1, _BOOTSTRAP_ATTEMPTS + 1):
            result = self._call("launchctl", "bootstrap", f"gui/{self.host.uid}", str(self.unit_path))
            if result.returncode == 0:
                return
            if attempt < _BOOTSTRAP_ATTEMPTS:
                self.host.sleep(1.0)
        reason = (result.stderr or result.stdout).strip()
        raise ServiceError(f"`launchctl bootstrap gui/{self.host.uid} {self.unit_path}` failed "
                           f"{_BOOTSTRAP_ATTEMPTS} times: {reason}")

    def remove(self) -> None:
        self.stop()
        self.unit_path.unlink(missing_ok=True)


class Systemd(_Manager):
    kind = "systemd user unit"

    @property
    def unit(self) -> str:
        return f"{self.label}.service"

    @property
    def unit_path(self) -> Path:
        config = self.host.environ.get("XDG_CONFIG_HOME") or str(self.host.home / ".config")
        return Path(config) / "systemd" / "user" / self.unit

    def render(self, spec: ServiceSpec) -> bytes:
        return render_systemd_unit(spec).encode()

    def unit_settings(self, *, explicit_port: bool = False) -> tuple[int | None, Path | None, Path | None]:
        try:
            lines = self.unit_path.read_text().splitlines()
        except OSError:
            return None, None, None
        values = dict(line.split("=", 1) for line in lines if "=" in line)
        log = values.get("StandardOutput", "")
        argv = _systemd_words(values.get("ExecStart", ""))
        port = _given_port(argv) if explicit_port else _port_of(argv)
        return (port,
                Path(log.removeprefix("append:").replace("%%", "%")) if log.startswith("append:") else None,
                _runtime_root_of(argv))

    def _systemctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = self._call("systemctl", "--user", *args)
        if check and result.returncode != 0:
            reason = (result.stderr or result.stdout).strip()
            raise ServiceError(f"`systemctl --user {' '.join(args)}` failed: {reason}")
        return result

    def state(self) -> State:
        installed = self.unit_path.is_file()
        port, log, runtime_root = self.unit_settings()
        if not installed:
            return State(installed=False, loaded=False, running=False, port=port, log_path=log, runtime_root=runtime_root)
        shown = self._systemctl("show", self.unit, "--property=LoadState,ActiveState,SubState,MainPID,"
                                "ExecMainCode,ExecMainStatus", check=False)
        fields = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
        pid = int(fields["MainPID"]) if fields.get("MainPID", "0").isdigit() and fields.get("MainPID") != "0" else None
        last_exit = f"status {fields['ExecMainStatus']}" if fields.get("ExecMainCode", "0") != "0" else ""
        return State(installed=True, loaded=fields.get("LoadState") == "loaded",
                     running=fields.get("ActiveState") == "active" and pid is not None, pid=pid,
                     last_exit=last_exit, port=port, log_path=log, runtime_root=runtime_root)

    def stop(self) -> None:
        if self.unit_path.is_file():
            self._systemctl("daemon-reload")
            self._systemctl("stop", self.unit)

    def start(self) -> None:
        self._systemctl("enable", "--now", self.unit)

    def remove(self) -> None:
        if self.unit_path.is_file():
            self._systemctl("disable", "--now", self.unit)
            self.unit_path.unlink()
            self._systemctl("daemon-reload")


def _given_port(argv: Sequence[str]) -> int | None:
    """A unit's ``--port``; None without one."""
    argv = list(argv)
    if "--port" in argv[:-1] and argv[argv.index("--port") + 1].isdigit():
        return int(argv[argv.index("--port") + 1])
    return None


def _port_of(argv: Sequence[str]) -> int | None:
    """The port a unit's ``looper serve`` listens on: its ``--port``, else its ops file's (``--config``); None when
    neither can be read."""
    argv = list(argv)
    if (port := _given_port(argv)) is not None:
        return port
    if "--config" in argv[:-1]:
        try:
            return ops_config.read_file(Path(argv[argv.index("--config") + 1])).get("port", ops_config.DEFAULT_PORT)
        except ops_config.OpsConfigError:
            return None
    return None


def _runtime_root_of(argv: Sequence[str]) -> Path | None:
    argv = list(argv)
    if "--runtime-root" in argv[:-1]:
        return Path(argv[argv.index("--runtime-root") + 1])
    return None


def manager(host: Host, label: str) -> _Manager:
    if host.system == "Darwin":
        return Launchd(host, label)
    if host.system == "Linux":
        return Systemd(host, label)
    raise ServiceError(f"`looper service` supports macOS (launchd) and Linux (systemd), "
                       f"not {host.system or 'this system'}")


# --- The commands ------------------------------------------------------------------


@dataclass
class Serving:
    pid: int | None
    status: dict


def _start_and_wait(mgr: _Manager, port: int, log_path: Path | None, timeout: float,
                    runtime_root: Path | None) -> Serving:
    """Start the stopped service on a free ``port`` and wait until it answers ``POST /api/v1/system/status``."""
    host = mgr.host
    if host.port_in_use(port):
        raise ServiceError(f"port {port} is in use by another process, so the service is installed but not "
                           "started; stop that process (or install again with another --port), then run "
                           "`looper service restart`")
    mgr.start()
    deadline = host.clock() + timeout
    while True:
        token = operator_auth.read_token(runtime_root) if runtime_root is not None else None
        status = host.get_status(port, token)
        if status is not None:
            return Serving(pid=mgr.state().pid, status=status)
        if host.clock() >= deadline:
            state = mgr.state()
            raise ServiceError(f"the service did not answer on port {port} within {timeout:.0f} s "
                               f"({_describe(state)}); see {log_path}{_log_tail(log_path)}")
        host.sleep(1.0)


def _log_tail(log_path: Path | None, lines: int = 15) -> str:
    try:
        tail = log_path.read_text(errors="replace").splitlines()[-lines:] if log_path else []
    except OSError:
        return ""
    return ("\n  " + "\n  ".join(tail)) if tail else ""


def _describe(state: State) -> str:
    if state.running:
        text = f"running, pid {state.pid}"
    elif state.loaded:
        text = "loaded, not running"
    else:
        text = "not loaded"
    return f"{text}, last exit {state.last_exit}" if state.last_exit and state.last_exit != "(never exited)" else text


def install(host: Host, ops: OpsConfig, *, label: str = DEFAULT_LABEL, port: int | None = None,
            extra_env: Sequence[str] = (), timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Write the unit for ``ops``, reload it and wait until it serves. Keep an installed unit's explicit port
    unless a new flag or the ops file selects one; otherwise the service reads its port from the ops file.
    """
    mgr = manager(host, label)
    if not os.path.lexists(ops.path):
        raise ServiceError(f"no ops file at {ops.path}: run `looper config init-ops` first, which writes it from the "
                           "settings in effect")
    if transient := [str(source) for source in ops.sources.values()
                     if source.origin is ops_config.Origin.ENVIRONMENT]:
        raise ServiceError(f"{', '.join(transient)} still comes from the environment, which the unit does not carry; "
                           f"set these settings in {ops.path} before installing (the existing service is unchanged)")
    if port is None and "port" not in ops.sources:
        port, _, _ = mgr.unit_settings(explicit_port=True)
    runtime_root = _absolute_root(host, ops.runtime_root)
    spec = build_spec(host, label=label, port=port, runtime_root=runtime_root, config=ops.path, extra_env=extra_env)
    runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)  # private before logs/ under it (operator_auth)
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    write_private(mgr.unit_path, mgr.render(spec))
    mgr.stop()
    serving_port = port if port is not None else ops.port
    serving = _start_and_wait(mgr, serving_port, spec.log_path, timeout, runtime_root)
    names = [name for name, _ in spec.env]
    dropped = sorted(name for name in host.environ if name.startswith(FORMER_PREFIXES) and name not in names
                     and name not in ops_config.VARIABLES and name not in ("LOOPER_RUNTIME_ROOT", "LOOPX_RUNTIME_ROOT"))
    lines = [
        f"installed {label} ({mgr.kind}); it starts at login and again after a crash",
        f"  unit       {mgr.unit_path}",
        "             mode 0600: it holds the captured environment, which can hold API keys",
        f"  command    {shlex.join(spec.argv)}",
        f"  directory  {spec.working_dir}",
        f"  ops file   {ops.path} (the database URL and the other settings; no database URL is in the unit)",
        f"  env        {', '.join(names) if names else '(none)'}",
        f"  log        {spec.log_path}",
        f"  serving    http://127.0.0.1:{serving_port} (pid {serving.pid or 'unknown'})",
    ]
    lines += [f"  not copied {', '.join(dropped)}: install no longer copies every LOOPER_*, ANTHROPIC_* and LARK_* "
              "variable; add --env NAME for one the service needs"] if dropped else []
    lines += [f"  warning    {warning}" for warning in ops.warnings]
    if host.system == "Linux":
        lines.append("  note       to run it without a login session too: loginctl enable-linger $USER")
    return "\n".join(lines)


def restart(host: Host, *, label: str = DEFAULT_LABEL, timeout: float = DEFAULT_TIMEOUT_SECONDS,
            drain: bool = False, drain_timeout: float = 1200, no_force: bool = False) -> str:
    if drain:
        return _planned_restart(host, label=label, timeout=timeout, drain_timeout=drain_timeout,
                                no_force=no_force)
    mgr = manager(host, label)
    if not mgr.unit_path.is_file():
        raise ServiceError(f"{label} is not installed; run `looper service install`")
    port, log_path, runtime_root = mgr.unit_settings()
    if port is None:
        raise ServiceError(f"{mgr.unit_path} names no --port; run `looper service install` again")
    before = mgr.state().pid
    mgr.stop()
    serving = _start_and_wait(mgr, port, log_path, timeout, runtime_root)
    return (f"restarted {label}: pid {before or 'none'} -> {serving.pid or 'unknown'}, "
            f"serving http://127.0.0.1:{port}")


def uninstall(host: Host, *, label: str = DEFAULT_LABEL) -> str:
    mgr = manager(host, label)
    state = mgr.state()
    if not state.installed and not state.loaded:
        return f"{label} is not installed"
    mgr.remove()
    kept = f"; the log stays at {state.log_path}" if state.log_path else ""
    return f"uninstalled {label}: stopped it and removed {mgr.unit_path}{kept}"


def status(host: Host, *, label: str = DEFAULT_LABEL) -> tuple[int, str]:
    """The status report, and 0 when the service runs and answers on its port, else 1."""
    mgr = manager(host, label)
    state = mgr.state()
    if not state.installed and not state.loaded:
        return 1, f"{label}: not installed (`looper service install` runs looper serve in the background)"
    lines = [
        f"service    {label} ({mgr.kind})",
        f"unit       {mgr.unit_path}" + ("" if state.installed else " (missing; the service is still loaded)"),
        f"state      {_describe(state)}",
        f"log        {state.log_path or 'unknown'}",
    ]
    token = operator_auth.read_token(state.runtime_root) if state.runtime_root is not None else None
    answer = host.get_status(state.port, token) if state.port else None
    if state.port is None:
        lines.append("api        unknown port")
    elif answer is None:
        lines.append(f"api        http://127.0.0.1:{state.port}/api/v1/system/status: no answer")
    elif answer.get("status") == "unauthorized":
        path = operator_auth.token_path(state.runtime_root) if state.runtime_root is not None else "unknown"
        lines.append(f"api        http://127.0.0.1:{state.port}/api/v1/system/status: unauthorized "
                     f"(the operator token in {path} was refused)")
    else:
        tasks = answer.get("tasks") or {}
        failing = [check.get("label", "?") for check in answer.get("checks") or []
                   if isinstance(check, dict) and check.get("status") in ("fail", "warn")]
        lines.append(f"api        http://127.0.0.1:{state.port}/api/v1/system/status: {answer.get('status', '?')}, "
                     f"{tasks.get('total', '?')} task(s), {tasks.get('running', '?')} running"
                     + (f"; check problems: {', '.join(failing)}" if failing else ""))
    return (0 if state.running and answer is not None else 1), "\n".join(lines)


def doctor_check(host: Host | None = None, label: str = DEFAULT_LABEL) -> Check:
    """The ``looper doctor`` line: is the background service installed and running? Never a failure."""
    host = host or Host.current()
    try:
        state = manager(host, label).state()
    except ServiceError as exc:
        return Check("service", "Service", SKIP, f"not checked: {exc}")
    if not state.installed and not state.loaded:
        return Check("service", "Service", SKIP, f"{label} not installed (optional: `looper service install`)")
    if state.running:
        return Check("service", "Service", OK, f"{label} running (pid {state.pid})")
    return Check("service", "Service", WARN, f"{label} installed but not running ({_describe(state)})",
                 f"run `looper service restart`; the log is {state.log_path or 'under the runtime root'}")


def _checked(host: Host, argv: Sequence[str], *, installing: bool = False) -> str:
    result = (_run(argv, timeout=600) if installing and host.run is _run else host.run(argv))
    if result.returncode:
        raise ServiceError(f"`{shlex.join(argv)}` failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def _swap_directory(staged: Path, destination: Path) -> None:
    """Publish a complete build with one rename, including an existing nonempty dist."""
    if not destination.exists():
        os.replace(staged, destination)
        return
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(staged), os.fsencode(destination), 2)  # RENAME_SWAP
    elif sys.platform == "linux":
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(staged), -100, os.fsencode(destination), 2)  # RENAME_EXCHANGE
    else:
        raise ServiceError("atomic dashboard replacement requires macOS or Linux")
    if result != 0:
        error = ctypes.get_errno()
        raise ServiceError(f"cannot atomically publish the dashboard: {os.strerror(error)}")


def _install_target(host: Host, source: str, *, wheel: bool, build_web: bool,
                    dependencies: bool = True) -> tuple[bytes, str] | None:
    from ..components import record_web_source, web_fingerprints

    if wheel:
        # uv tool environments omit pip; target that same interpreter with uv's pip interface.
        has_pip = host.run([host.executable, "-m", "pip", "--version"]).returncode == 0
        command = ([host.executable, "-m", "pip", "install", "--force-reinstall", source] if has_pip else
                   ["uv", "pip", "install", "--python", host.executable, "--reinstall", source])
        # NOOP/WEB_ONLY retain the interpreter. Keep its unchanged dependencies
        # in place so delayed imports use the same resolution.
        if not dependencies:
            command.insert(-1, "--no-deps")
        _checked(host, command, installing=True)
        return None
    _checked(host, ["git", "-C", str(host.cwd), "merge", "--ff-only", source], installing=True)
    _checked(host, ["uv", "sync", "--project", str(host.cwd)], installing=True)
    if not build_web:
        return None
    web = host.cwd / "web"
    staged = Path(tempfile.mkdtemp(prefix=".dist-upgrade-", dir=web))
    try:
        _checked(host, ["npm", "ci", "--prefix", str(web)], installing=True)
        _checked(host, ["npm", "run", "build", "--prefix", str(web), "--", "--outDir", str(staged)], installing=True)
        if not (staged / "index.html").is_file():
            raise ServiceError("the dashboard build did not produce index.html")
        record_web_source(host.cwd, staged)
        expected = ((staged / "index.html").read_bytes(), web_fingerprints(staged)[1])
        _swap_directory(staged, web / "dist")
        return expected
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _deploy_report(report: dict, log_path: Path | None) -> str:
    line = json.dumps({"event": "service_upgrade", **report}, sort_keys=True)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as output:
            output.write(line + "\n")
    return line


def _stop_for_install(mgr: _Manager, before: int | None) -> None:
    mgr.stop()
    deadline = mgr.host.clock() + _STOP_WAIT_SECONDS
    while mgr.state().running or (before is not None and mgr.host.pid_alive(before)):
        if mgr.host.clock() >= deadline:
            raise ServiceError("old service process is still running; refusing to install")
        mgr.host.sleep(_POLL_SECONDS)


def _execute_plan(host: Host, mgr: _Manager, *, port: int, root: Path, log_path: Path | None,
                  plan, running, target, installer, timeout: float, drain_timeout: float,
                  no_force: bool) -> str:
    from ..components import Manifest
    from ..upgrade import UpgradeKind

    if not all(math.isfinite(value) and value > 0 for value in (timeout, drain_timeout)):
        raise ServiceError("--timeout and --drain-timeout must be positive finite numbers")
    token = operator_auth.read_token(root)
    if not token:
        raise ServiceError("the service operator token is missing")
    report = {"plan": plan.kind.value, "reasons": list(plan.reasons), "drain_seconds": 0.0,
              "gap_seconds": 0.0, "interrupted": [], "kept_before": 0, "adopted": 0,
              "running_code_fingerprint": running.components["control_plane"],
              "installed_package_version": running.version, "status": "failed"}
    draining, stopped, stop_attempted, restart_started = False, False, False, False
    state = mgr.state()
    before = state.pid
    if plan.kind in (UpgradeKind.FULL, UpgradeKind.CONTROL_PLANE) and (not state.running or before is None):
        raise ServiceError("the service manager has no running PID; cannot confirm exit before installing or restarting")
    try:
        expected = None
        if plan.kind in (UpgradeKind.FULL, UpgradeKind.CONTROL_PLANE):
            started = host.clock()
            # Mark the attempt first: a lost response may still have begun draining.
            draining = True
            status = host.system_operation(port, token, "drain", {
                "scope": "turns" if plan.kind == UpgradeKind.CONTROL_PLANE else "runs",
                "timeout_seconds": drain_timeout})
            while True:
                phase = status.get("phase")
                if phase not in ("draining", "drained"):
                    raise ServiceError("drain was cancelled; deployment stopped")
                passed = status.get("deadline_passed") or host.clock() - started >= drain_timeout
                report["drain_seconds"] = round(host.clock() - started, 3)
                if passed and phase != "drained":
                    if no_force:
                        raise ServiceError("drain deadline passed; --no-force cancelled deployment")
                    report["interrupted"] = [
                        {"worker_id": row["worker_id"], "role": row["role"], "reason": "deadline"}
                        for row in status.get("waiting", {}).get("runs", [])]
                if phase == "drained" or passed:
                    break
                host.sleep(_POLL_SECONDS)
                status = host.system_operation(port, token, "drain_status", {})
            gap_started = host.clock()
            stop_attempted = True
            _stop_for_install(mgr, before)
            stopped = True
            if installer:
                expected = installer()
                report["installed_package_version"] = target.version
            restart_started = True
            _start_and_wait(mgr, port, log_path, timeout, root)
            report["gap_seconds"] = round(host.clock() - gap_started, 3)
        elif installer:
            expected = installer()
            report["installed_package_version"] = target.version

        actual = Manifest.from_dict(host.get_components(port, token))
        if target is not None:
            if actual.components != target.components or actual.adoption != target.adoption:
                raise ServiceError("running system.components does not match the target")
            if plan.kind == UpgradeKind.WEB_ONLY:
                index = host.get_index(port, token)
                if not index or (expected and (index != expected[0] or actual.verification.get("web") != expected[1])):
                    raise ServiceError("the served dashboard does not match the installed build")
                if expected is None and actual.verification.get("web") != target.verification.get("web"):
                    raise ServiceError("the served dashboard does not match the target wheel")
        whoami = host.get_whoami(port, token)
        report.update(status="ok", running_code_fingerprint=actual.components["control_plane"],
                      running_version=whoami["looper_version"], web_fingerprint=actual.components["web"])
    except BaseException as exc:
        report["error"] = str(exc)
        if draining and not restart_started:
            try:
                # bootout/stop can take effect and then report an error. Check
                # exit evidence before deciding that only drain_cancel is needed.
                if stop_attempted and not stopped and before is not None and not host.pid_alive(before):
                    try:
                        stopped = not mgr.state().running
                    except ServiceError:
                        stopped = not host.port_in_use(port)
                # If installation failed after stop, restore availability before cancelling.
                if stopped:
                    _start_and_wait(mgr, port, log_path, timeout, root)
                host.system_operation(port, token, "drain_cancel", {})
            except Exception as cancel_error:
                report["recovery_error"] = str(cancel_error)
        line = _deploy_report(report, log_path)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise ServiceError(f"{exc}\n{line}") from exc
    return (f"plan       {plan.kind.value}\n"
            + "\n".join("reason     " + reason for reason in plan.reasons)
            + f"\nrunning code fingerprint {report['running_code_fingerprint']}, "
              f"installed package version {report['installed_package_version']}\n"
            + _deploy_report(report, log_path))


def _planned_restart(host: Host, *, label: str, timeout: float, drain_timeout: float, no_force: bool) -> str:
    from ..components import Manifest
    from ..upgrade import UpgradeKind, UpgradePlan
    mgr = manager(host, label)
    port, log_path, root = mgr.unit_settings()
    if port is None or root is None or not mgr.state().running:
        raise ServiceError("cannot locate a running installed service to drain")
    token = operator_auth.read_token(root)
    try:
        running = Manifest.from_dict(host.get_components(port, token))
    except (ValueError, KeyError) as exc:
        raise ServiceError(str(exc)) from exc
    plan = UpgradePlan(UpgradeKind.FULL, ("Planned restart requested; no package installation.",), ())
    return _execute_plan(host, mgr, port=port, root=root, log_path=log_path, plan=plan,
        running=running, target=None, installer=None, timeout=timeout, drain_timeout=drain_timeout, no_force=no_force)


def upgrade(host: Host, *, label: str = DEFAULT_LABEL, to: str | None = None, dry_run: bool = False,
            timeout: float = DEFAULT_TIMEOUT_SECONDS, drain_timeout: float = 1200, no_force: bool = False) -> str:
    """Compute a plan before mutating files; stop the old interpreter before a full install."""
    from .. import components
    from ..upgrade import UpgradeKind, decide, scan_keepers

    try:
        wheel = bool(to and to.endswith(".whl"))
        source = str(Path(to).resolve()) if wheel else to or "origin/main"
        if not dry_run and not wheel:
            if _checked(host, ["git", "-C", str(host.cwd), "status", "--porcelain", "--untracked-files=no"]):
                raise ServiceError("the installed checkout has tracked changes; refusing to upgrade")
            source = _checked(host, ["git", "-C", str(host.cwd), "rev-parse", "--verify",
                                     "--end-of-options", source + "^{commit}"])
        target = (components.manifest_from_wheel(Path(source)) if wheel
                  else components.manifest_from_git(host.cwd, source))
        mgr = manager(host, label)
        port, log_path, root = mgr.unit_settings()
        if port is None or root is None:
            raise ServiceError("cannot locate the service port and runtime root; install the service first")
        token = operator_auth.read_token(root)
        if not token:
            raise ServiceError("the service operator token is missing")
        running = components.Manifest.from_dict(host.get_components(port, token))
        plan = decide(target, running, scan_keepers(root), keeper_mode=False)
        if dry_run:
            return "\n".join((f"plan       {plan.kind.value}", f"target     {target.version}",
                f"running    {running.version} (loaded {running.loaded_at})",
                "changed    " + (", ".join(plan.changed_components) or "none"),
                *("reason     " + reason for reason in plan.reasons)))
        return _execute_plan(host, mgr, port=port, root=root, log_path=log_path, plan=plan,
            running=running, target=target,
            installer=lambda: _install_target(host, source, wheel=wheel, build_web="web" in plan.changed_components,
                dependencies=plan.kind in (UpgradeKind.FULL, UpgradeKind.CONTROL_PLANE)),
            timeout=timeout, drain_timeout=drain_timeout, no_force=no_force)
    except (ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise ServiceError(str(exc)) from exc
