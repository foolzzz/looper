"""``looper service``: run ``looper serve`` as a managed background service.

macOS gets a launchd LaunchAgent (``~/Library/LaunchAgents/<label>.plist``),
Linux a systemd user unit (``~/.config/systemd/user/<label>.service``). Either
way the service starts at login, is started again when it exits with a failure
(a crash or a kill), and writes its output to ``<runtime_root>/logs/serve.log``.

The unit runs the ``looper`` of the interpreter that ran ``install`` (a
checkout's ``.venv/bin/looper``) in the directory ``install`` ran in, as
``looper serve --config <ops file> --runtime-root <root>``, both absolute, and
``--port`` only when ``install`` was given one: the ops file
(``looper.ops_config``) holds the database URL and the other settings, so the
unit holds none of them, and ``install`` refuses while one still comes from a
variable it would not carry. launchd and systemd start services with an almost
empty environment, so ``install`` copies the variables the service and its
agents need (``CAPTURED_NAMES``, and each ``--env NAME``) from its own
environment into the unit, which is why the unit file is written with mode
0600: they can hold API keys.

launchd: a job is reloaded with ``launchctl bootout`` then ``bootstrap``, never
``kickstart -k``, which can reuse the code-signing state of the binary it
started before. ``bootout`` returns before the job is gone, and a ``bootstrap``
issued meanwhile fails, so ``stop`` waits until ``launchctl print`` no longer
finds the job and ``bootstrap`` is retried a few times.

Before the rename the service was ``com.loopx.serve``. ``install`` with the
default label first stops and removes that unit, so the two never compete
for the port, and moves ``~/.loopx`` to ``~/.looper`` when the new unit uses
the default runtime root (``looper.paths.settle_runtime_root``).
"""

from __future__ import annotations

import http.client
import json
import os
import platform
import plistlib
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import child_env, ops_config
from ..ops_config import OpsConfig
from ..paths import settle_runtime_root
from . import operator_auth
from .self_check import OK, SKIP, WARN, Check

DEFAULT_LABEL = "com.looper.serve"
LEGACY_LABEL = "com.loopx.serve"  # before the rename; `install` replaces it
DEFAULT_PORT = 8767
DEFAULT_TIMEOUT_SECONDS = 120.0
LOG_RELATIVE_PATH = Path("logs") / "serve.log"

# Copied from the environment of `install` when set: what the server and the agents it starts read
# (``child_env``) but launchd and systemd do not set, and the Telegram channel's token variable (``LOOPX_*`` for
# one release). Not the session's own (HOME, SSH_AUTH_SOCK, TMPDIR: the service manager sets them), not CLAUDECODE
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


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=_COMMAND_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise ServiceError(f"`{argv[0]}` is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ServiceError(f"`{' '.join(argv)}` did not finish in {_COMMAND_TIMEOUT_SECONDS:.0f} s") from exc


def _get_status(port: int, token: str | None) -> dict | None:
    """``GET /api/status`` on 127.0.0.1, bypassing any proxy; None when nothing answers with JSON."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/status",
                                    headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with opener.open(request, timeout=3) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"status": "unauthorized"} if exc.code == 401 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return payload if isinstance(payload, dict) else None


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


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

    def unit_settings(self) -> tuple[int | None, Path | None, Path | None]:
        """The port, log path and runtime root the installed unit names."""
        raise NotImplementedError

    def unit_argv(self) -> list[str]:
        """The installed unit's command; [] when there is none to read."""
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

    def unit_settings(self) -> tuple[int | None, Path | None, Path | None]:
        try:
            unit = plistlib.loads(self.unit_path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None, None, None
        log = unit.get("StandardOutPath")
        argv = unit.get("ProgramArguments") or []
        return _port_of(argv), Path(log) if log else None, _runtime_root_of(argv)

    def unit_argv(self) -> list[str]:
        try:
            return list(plistlib.loads(self.unit_path.read_bytes()).get("ProgramArguments") or [])
        except (OSError, plistlib.InvalidFileException, ValueError):
            return []

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

    def unit_settings(self) -> tuple[int | None, Path | None, Path | None]:
        try:
            lines = self.unit_path.read_text().splitlines()
        except OSError:
            return None, None, None
        values = dict(line.split("=", 1) for line in lines if "=" in line)
        log = values.get("StandardOutput", "")
        argv = _systemd_words(values.get("ExecStart", ""))
        return (_port_of(argv),
                Path(log.removeprefix("append:").replace("%%", "%")) if log.startswith("append:") else None,
                _runtime_root_of(argv))

    def unit_argv(self) -> list[str]:
        try:
            lines = self.unit_path.read_text().splitlines()
        except OSError:
            return []
        return _systemd_words(dict(line.split("=", 1) for line in lines if "=" in line).get("ExecStart", ""))

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
    """Start the stopped service on a free ``port`` and wait until it answers ``GET /api/status``."""
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
    """Write the unit (again) for the ops settings ``ops`` (``ops_config.load`` with this command's flags), reload
    the service and wait until it serves; the report to print. ``port``: the ``--port`` given, written into the unit;
    without one, the ops file's port, or the port the unit installed before names when the file names none.

    It refuses before anything is written when there is no ops file, or when a setting still comes from a variable
    of this environment, which the unit no longer carries. With the default label, the unit from before the rename
    (``LEGACY_LABEL``) is stopped and removed first, then the runtime root is settled (``~/.loopx`` moved to
    ``~/.looper``) before anything is written under it.
    """
    mgr = manager(host, label)
    if not os.path.lexists(ops.path):
        raise ServiceError(f"no ops file at {ops.path}: run `looper config init-ops` first, which writes it from the "
                           "settings in effect")
    if from_variables := sorted(source.label for source in ops.sources.values()
                                if source.origin is ops_config.Origin.ENVIRONMENT):
        raise ServiceError(f"{', '.join(from_variables)} still sets what the ops file should: the unit does not "
                           f"carry it. Set it in {ops.path} (or remove the file and run `looper config init-ops`), "
                           "remove the variable, and install again")
    if port is None and "port" not in ops.sources:
        port = _given_port(mgr.unit_argv())  # keep a port the installed unit was given (every one before the ops file)
    runtime_root = ops.runtime_root
    spec = build_spec(host, label=label, port=port, runtime_root=runtime_root, config=ops.path, extra_env=extra_env)
    retired = _retire_legacy_unit(host) if label == DEFAULT_LABEL else ""
    settled = settle_runtime_root(_absolute_root(host, runtime_root), home=host.home)
    if settled.root != _absolute_root(host, runtime_root):
        spec = build_spec(host, label=label, port=port, runtime_root=settled.root, config=ops.path,
                          extra_env=extra_env)
    settled.root.mkdir(mode=0o700, parents=True, exist_ok=True)  # private before logs/ under it (operator_auth)
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    write_private(mgr.unit_path, mgr.render(spec))
    mgr.stop()
    serving_port = port if port is not None else ops.port
    serving = _start_and_wait(mgr, serving_port, spec.log_path, timeout, settled.root)
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
    lines += [f"  replaced   {retired}"] if retired else []
    lines += [f"  {'warning' if settled.warning else 'runtime':<10} {settled.note}"] if settled.note else []
    lines += [f"  warning    {warning}" for warning in ops.warnings]
    if host.system == "Linux":
        lines.append("  note       to run it without a login session too: loginctl enable-linger $USER")
    return "\n".join(lines)


def _retire_legacy_unit(host: Host) -> str:
    """Stop and remove the ``LEGACY_LABEL`` unit when it is there; what was removed, or ""."""
    legacy = manager(host, LEGACY_LABEL)
    state = legacy.state()
    if not state.installed and not state.loaded:
        return ""
    legacy.remove()
    return f"{LEGACY_LABEL}, the unit from before the rename: stopped it and removed {legacy.unit_path}"


def _legacy_hint(host: Host, label: str) -> str:
    """For the default label: a note when the unit from before the rename is still there, else ""."""
    if label != DEFAULT_LABEL:
        return ""
    state = manager(host, LEGACY_LABEL).state()
    if not state.installed and not state.loaded:
        return ""
    return f"{LEGACY_LABEL} from before the rename is installed; `looper service install` replaces it"


def restart(host: Host, *, label: str = DEFAULT_LABEL, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
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
        hint = _legacy_hint(host, label)
        return 1, (f"{label}: not installed (`looper service install` runs looper serve in the background)"
                   + (f"\n{hint}" if hint else ""))
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
        lines.append(f"api        http://127.0.0.1:{state.port}/api/status: no answer")
    elif answer.get("status") == "unauthorized":
        path = operator_auth.token_path(state.runtime_root) if state.runtime_root is not None else "unknown"
        lines.append(f"api        http://127.0.0.1:{state.port}/api/status: unauthorized "
                     f"(the operator token in {path} was refused)")
    else:
        tasks = answer.get("tasks") or {}
        failing = [check.get("label", "?") for check in answer.get("checks") or []
                   if isinstance(check, dict) and check.get("status") in ("fail", "warn")]
        lines.append(f"api        http://127.0.0.1:{state.port}/api/status: {answer.get('status', '?')}, "
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
        if hint := _legacy_hint(host, label):
            return Check("service", "Service", WARN, f"{label} not installed; {hint}", "run `looper service install`")
        return Check("service", "Service", SKIP, f"{label} not installed (optional: `looper service install`)")
    if state.running:
        return Check("service", "Service", OK, f"{label} running (pid {state.pid})")
    return Check("service", "Service", WARN, f"{label} installed but not running ({_describe(state)})",
                 f"run `looper service restart`; the log is {state.log_path or 'under the runtime root'}")
