"""``loopx service``: run ``loopx serve`` as a managed background service.

macOS gets a launchd LaunchAgent (``~/Library/LaunchAgents/<label>.plist``),
Linux a systemd user unit (``~/.config/systemd/user/<label>.service``). Either
way the service starts at login, is started again when it exits with a failure
(a crash or a kill), and writes its output to ``<runtime_root>/logs/serve.log``.

The unit runs the ``loopx`` of the interpreter that ran ``install`` (a
checkout's ``.venv/bin/loopx``) in the directory ``install`` ran in, so a
checkout's ``.env`` keeps working. launchd and systemd start services with an
almost empty environment, so ``install`` copies the variables the service
needs from its own environment into the unit, which is why the unit file is
written with mode 0600: ``LOOPX_DATABASE_URL`` can hold a password.

launchd: a job is reloaded with ``launchctl bootout`` then ``bootstrap``, never
``kickstart -k``, which can reuse the code-signing state of the binary it
started before. ``bootout`` returns before the job is gone, and a ``bootstrap``
issued meanwhile fails, so ``stop`` waits until ``launchctl print`` no longer
finds the job and ``bootstrap`` is retried a few times.
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
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .self_check import OK, SKIP, WARN, Check

DEFAULT_LABEL = "com.loopx.serve"
DEFAULT_PORT = 8767
DEFAULT_TIMEOUT_SECONDS = 120.0
LOG_RELATIVE_PATH = Path("logs") / "serve.log"

# Copied from the environment of `install` when set. Not CLAUDECODE or
# CLAUDE_CODE_*: an install run from inside a Claude Code session would pass
# that session's markers on to every agent the service starts.
CAPTURED_NAMES = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "CLAUDE_CONFIG_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)
CAPTURED_PREFIXES = ("LOOPX_", "ANTHROPIC_", "LARK_")

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


def _get_status(port: int) -> dict | None:
    """``GET /api/status`` on 127.0.0.1, bypassing any proxy; None when nothing answers with JSON."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=3) as response:
            payload = json.loads(response.read())
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
    executable: str  # the interpreter running loopx
    cwd: Path
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    get_status: Callable[[int], dict | None] = _get_status
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


def loopx_command(executable: str) -> list[str]:
    """The current ``loopx``: the console script next to the interpreter, else ``python -m loopx.cli``."""
    script = Path(os.path.abspath(executable)).parent / "loopx"
    if script.is_file() and os.access(script, os.X_OK):
        return [str(script)]
    return [os.path.abspath(executable), "-m", "loopx.cli"]


def captured_env(environ: Mapping[str, str], extra: Sequence[str] = ()) -> dict[str, str]:
    """The variables of ``environ`` the unit carries: the fixed names, the prefixes and ``extra``."""
    wanted = set(CAPTURED_NAMES) | set(extra)
    return {name: environ[name] for name in sorted(environ)
            if name in wanted or name.startswith(CAPTURED_PREFIXES)}


def build_spec(host: Host, *, label: str, port: int, runtime_root: Path, extra_env: Sequence[str] = ()) -> ServiceSpec:
    check_label(label)
    if not 0 < port < 65536:
        raise ServiceError(f"--port {port} is not a TCP port")
    missing = [name for name in extra_env if name not in host.environ]
    if missing:
        raise ServiceError(f"--env {', '.join(missing)}: not set in this environment")
    root = Path(os.path.abspath(os.path.join(host.cwd, os.path.expanduser(str(runtime_root)))))
    argv = (*loopx_command(host.executable), "serve", "--port", str(port), "--runtime-root", str(root))
    return ServiceSpec(label=label, argv=argv, working_dir=host.cwd,
                       env=tuple(captured_env(host.environ, extra_env).items()), log_path=root / LOG_RELATIVE_PATH)


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
        f"Description=LoopX agent system (loopx serve, {spec.label})",
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

    def unit_settings(self) -> tuple[int | None, Path | None]:
        """The port and log path the installed unit names."""
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

    def unit_settings(self) -> tuple[int | None, Path | None]:
        try:
            unit = plistlib.loads(self.unit_path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None, None
        log = unit.get("StandardOutPath")
        return _port_of(unit.get("ProgramArguments") or []), Path(log) if log else None

    def _print(self) -> subprocess.CompletedProcess[str]:
        return self._call("launchctl", "print", self.target)

    def state(self) -> State:
        printed = self._print()
        # Top-level fields only: one tab deep; nested blocks are indented further.
        found = re.findall(r"^\t([a-z][a-z ]*?) = (.*)$", printed.stdout, re.M) if printed.returncode == 0 else []
        fields = dict(found)
        pid = int(fields["pid"]) if fields.get("pid", "").isdigit() else None
        last_exit = fields.get("last terminating signal") or fields.get("last exit code", "")
        port, log = self.unit_settings()
        return State(installed=self.unit_path.is_file(), loaded=printed.returncode == 0,
                     running=fields.get("state") == "running" and pid is not None, pid=pid,
                     last_exit=last_exit, port=port, log_path=log)

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

    def unit_settings(self) -> tuple[int | None, Path | None]:
        try:
            lines = self.unit_path.read_text().splitlines()
        except OSError:
            return None, None
        values = dict(line.split("=", 1) for line in lines if "=" in line)
        log = values.get("StandardOutput", "")
        return (_port_of(_systemd_words(values.get("ExecStart", ""))),
                Path(log.removeprefix("append:").replace("%%", "%")) if log.startswith("append:") else None)

    def _systemctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = self._call("systemctl", "--user", *args)
        if check and result.returncode != 0:
            reason = (result.stderr or result.stdout).strip()
            raise ServiceError(f"`systemctl --user {' '.join(args)}` failed: {reason}")
        return result

    def state(self) -> State:
        installed = self.unit_path.is_file()
        port, log = self.unit_settings()
        if not installed:
            return State(installed=False, loaded=False, running=False, port=port, log_path=log)
        shown = self._systemctl("show", self.unit, "--property=LoadState,ActiveState,SubState,MainPID,"
                                "ExecMainCode,ExecMainStatus", check=False)
        fields = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
        pid = int(fields["MainPID"]) if fields.get("MainPID", "0").isdigit() and fields.get("MainPID") != "0" else None
        last_exit = f"status {fields['ExecMainStatus']}" if fields.get("ExecMainCode", "0") != "0" else ""
        return State(installed=True, loaded=fields.get("LoadState") == "loaded",
                     running=fields.get("ActiveState") == "active" and pid is not None, pid=pid,
                     last_exit=last_exit, port=port, log_path=log)

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


def _port_of(argv: Sequence[str]) -> int | None:
    argv = list(argv)
    if "--port" in argv[:-1] and argv[argv.index("--port") + 1].isdigit():
        return int(argv[argv.index("--port") + 1])
    return None


def manager(host: Host, label: str) -> _Manager:
    if host.system == "Darwin":
        return Launchd(host, label)
    if host.system == "Linux":
        return Systemd(host, label)
    raise ServiceError(f"`loopx service` supports macOS (launchd) and Linux (systemd), "
                       f"not {host.system or 'this system'}")


# --- The commands ------------------------------------------------------------------


@dataclass
class Serving:
    pid: int | None
    status: dict


def _start_and_wait(mgr: _Manager, port: int, log_path: Path | None, timeout: float) -> Serving:
    """Start the stopped service on a free ``port`` and wait until it answers ``GET /api/status``."""
    host = mgr.host
    if host.port_in_use(port):
        raise ServiceError(f"port {port} is in use by another process, so the service is installed but not "
                           "started; stop that process (or install again with another --port), then run "
                           "`loopx service restart`")
    mgr.start()
    deadline = host.clock() + timeout
    while True:
        status = host.get_status(port)
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


def install(host: Host, *, label: str = DEFAULT_LABEL, port: int = DEFAULT_PORT, runtime_root: Path,
            extra_env: Sequence[str] = (), timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Write the unit (again), reload the service and wait until it serves; the report to print."""
    mgr = manager(host, label)
    spec = build_spec(host, label=label, port=port, runtime_root=runtime_root, extra_env=extra_env)
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    write_private(mgr.unit_path, mgr.render(spec))
    mgr.stop()
    serving = _start_and_wait(mgr, port, spec.log_path, timeout)
    names = [name for name, _ in spec.env]
    lines = [
        f"installed {label} ({mgr.kind}); it starts at login and again after a crash",
        f"  unit       {mgr.unit_path}",
        "             mode 0600: it holds the captured environment, and LOOPX_DATABASE_URL can hold a password",
        f"  command    {shlex.join(spec.argv)}",
        f"  directory  {spec.working_dir}",
        f"  env        {', '.join(names) if names else '(none)'}",
        f"  log        {spec.log_path}",
        f"  serving    http://127.0.0.1:{port} (pid {serving.pid or 'unknown'})",
    ]
    if "LOOPX_DATABASE_URL" not in names:
        lines.append("  note       LOOPX_DATABASE_URL is not set here; the service reads it from the checkout's .env, "
                     "else uses the default")
    if host.system == "Linux":
        lines.append("  note       to run it without a login session too: loginctl enable-linger $USER")
    return "\n".join(lines)


def restart(host: Host, *, label: str = DEFAULT_LABEL, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    mgr = manager(host, label)
    if not mgr.unit_path.is_file():
        raise ServiceError(f"{label} is not installed; run `loopx service install`")
    port, log_path = mgr.unit_settings()
    if port is None:
        raise ServiceError(f"{mgr.unit_path} names no --port; run `loopx service install` again")
    before = mgr.state().pid
    mgr.stop()
    serving = _start_and_wait(mgr, port, log_path, timeout)
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
        return 1, f"{label}: not installed (`loopx service install` runs loopx serve in the background)"
    lines = [
        f"service    {label} ({mgr.kind})",
        f"unit       {mgr.unit_path}" + ("" if state.installed else " (missing; the service is still loaded)"),
        f"state      {_describe(state)}",
        f"log        {state.log_path or 'unknown'}",
    ]
    answer = host.get_status(state.port) if state.port else None
    if state.port is None:
        lines.append("api        unknown port")
    elif answer is None:
        lines.append(f"api        http://127.0.0.1:{state.port}/api/status: no answer")
    else:
        tasks = answer.get("tasks") or {}
        failing = [check.get("label", "?") for check in answer.get("checks") or []
                   if isinstance(check, dict) and check.get("status") in ("fail", "warn")]
        lines.append(f"api        http://127.0.0.1:{state.port}/api/status: {answer.get('status', '?')}, "
                     f"{tasks.get('total', '?')} task(s), {tasks.get('running', '?')} running"
                     + (f"; check problems: {', '.join(failing)}" if failing else ""))
    return (0 if state.running and answer is not None else 1), "\n".join(lines)


def doctor_check(host: Host | None = None, label: str = DEFAULT_LABEL) -> Check:
    """The ``loopx doctor`` line: is the background service installed and running? Never a failure."""
    host = host or Host.current()
    try:
        state = manager(host, label).state()
    except ServiceError as exc:
        return Check("service", "Service", SKIP, f"not checked: {exc}")
    if not state.installed and not state.loaded:
        return Check("service", "Service", SKIP, f"{label} not installed (optional: `loopx service install`)")
    if state.running:
        return Check("service", "Service", OK, f"{label} running (pid {state.pid})")
    return Check("service", "Service", WARN, f"{label} installed but not running ({_describe(state)})",
                 f"run `loopx service restart`; the log is {state.log_path or 'under the runtime root'}")
