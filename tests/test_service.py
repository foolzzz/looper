"""``loopx service``: the generated launchd and systemd units and how the commands drive the service manager.

The commands run through ``looper.cli.main`` against a stand-in launchd or
systemd that keeps the state a real one would: a ``launchctl bootout`` returns
before the job is gone, and a ``bootstrap`` issued meanwhile fails. Nothing
runs the real ``launchctl`` or ``systemctl``.
"""

from __future__ import annotations

import plistlib
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from looper import cli
from looper.agents import service

UID = 501
LABEL = "com.loopx.serve.test"

# The shell `install` runs in: a Claude Code session's markers and unrelated
# secrets must stay out of the unit.
SHELL_ENV = {
    "PATH": "/opt/tools/bin:/usr/bin:/bin",
    "HOME": "/home/someone",
    "LANG": "en_US.UTF-8",
    "LOOPX_DATABASE_URL": "postgresql://postgres:s3cret@127.0.0.1:54329/loopx",
    "LOOPX_TELEGRAM_BOT_TOKEN": "123:abc",
    "ANTHROPIC_API_KEY": "sk-test",
    "LARK_APP_ID": "cli_test",
    "HTTPS_PROXY": "http://proxy.example:3128",
    "MY_BOT_TOKEN": "custom",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_SESSION_ID": "session",
    "GITHUB_TOKEN": "ghp_unrelated",
}
CAPTURED = {name: SHELL_ENV[name] for name in (
    "ANTHROPIC_API_KEY", "HTTPS_PROXY", "LANG", "LARK_APP_ID", "LOOPX_DATABASE_URL", "LOOPX_TELEGRAM_BOT_TOKEN", "PATH",
)}


class FakeLaunchd:
    """launchd as `launchctl` shows it. A booted-out job stays visible for ``bootout_lag`` prints."""

    def __init__(self, *, loaded: bool = False, bootout_lag: int = 2, bootstrap_failures: int = 0) -> None:
        self.loaded, self.bootout_lag, self.bootstrap_failures = loaded, bootout_lag, bootstrap_failures
        self.unloading = 0
        self.pid = 4000 if loaded else 0
        self.calls: list[list[str]] = []
        self.early_bootstraps = 0

    @property
    def serving(self) -> bool:
        return self.loaded

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        assert argv[0] == "launchctl", argv
        command = argv[1]
        if command == "print":
            assert argv[2] == f"gui/{UID}/{LABEL}"
            if self.unloading:
                self.unloading -= 1
                return self._done(argv, 0, f"{argv[2]} = {{\n\tstate = exited\n}}\n")
            if self.loaded:
                return self._done(argv, 0, f"{argv[2]} = {{\n\tstate = running\n\truns = 1\n\tpid = {self.pid}\n"
                                           "\tlast exit code = (never exited)\n\t\tstate = active\n}\n")
            return self._done(argv, 113, "", f'Could not find service "{LABEL}" in domain for user gui: {UID}\n')
        if command == "bootout":
            assert self.loaded, "bootout of a job that is not loaded"
            self.loaded, self.unloading = False, self.bootout_lag
            return self._done(argv, 0)
        if command == "bootstrap":
            assert argv[2:] == [f"gui/{UID}", argv[3]] and argv[3].endswith(f"/{LABEL}.plist")
            if self.unloading or self.loaded:
                self.early_bootstraps += 1
                return self._done(argv, 5, "", "Bootstrap failed: 5: Input/output error\n")
            if self.bootstrap_failures:
                self.bootstrap_failures -= 1
                return self._done(argv, 5, "", "Bootstrap failed: 5: Input/output error\n")
            self.loaded, self.pid = True, self.pid + 1
            return self._done(argv, 0)
        raise AssertionError(f"unexpected launchctl call {argv}")

    @staticmethod
    def _done(argv: list[str], code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, code, out, err)

    def commands(self) -> list[str]:
        return [argv[1] for argv in self.calls]


class FakeSystemd:
    """A systemd user manager: units are loaded from the unit directory on daemon-reload."""

    def __init__(self) -> None:
        self.active = False
        self.pid = 7000
        self.calls: list[list[str]] = []

    @property
    def serving(self) -> bool:
        return self.active

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        assert argv[:2] == ["systemctl", "--user"], argv
        args = argv[2:]
        if args[0] == "show":
            out = (f"LoadState=loaded\nActiveState={'active' if self.active else 'inactive'}\nSubState=running\n"
                   f"MainPID={self.pid if self.active else 0}\nExecMainCode=0\nExecMainStatus=0\n")
            return subprocess.CompletedProcess(argv, 0, out, "")
        if args[0] == "stop" or args[:2] == ["disable", "--now"]:
            self.active = False
        elif args[:2] == ["enable", "--now"]:
            self.active, self.pid = True, self.pid + 1
        else:
            assert args == ["daemon-reload"], argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    def commands(self) -> list[str]:
        return [" ".join(argv[2:]) for argv in self.calls]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout whose venv has the `loopx` console script next to its interpreter."""
    bin_dir = tmp_path / "check out" / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("python3", "loopx"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    return tmp_path / "check out"


def _host(tmp_path: Path, checkout: Path, fake, *, system: str, port_busy: bool = False,
          status: dict | None = None) -> service.Host:
    clock = Clock()
    answer = status or {"status": "running", "tasks": {"total": 3, "running": 1}, "checks": []}
    return service.Host(
        system=system, home=tmp_path / "home", uid=UID, environ=dict(SHELL_ENV),
        executable=str(checkout / ".venv" / "bin" / "python3"), cwd=checkout, run=fake,
        sleep=clock.sleep, clock=lambda: clock.now,
        get_status=lambda port: answer if fake.serving else None,
        port_in_use=lambda port: port_busy and not fake.serving,
    )


def _loopx(monkeypatch: pytest.MonkeyPatch, host: service.Host, *argv: str) -> int:
    monkeypatch.setattr(service.Host, "current", classmethod(lambda cls: host))
    return cli.main(["service", *argv])


def _install(monkeypatch, host: service.Host, root: Path, *more: str, port: str = "9876") -> int:
    return _loopx(monkeypatch, host, "install", "--label", LABEL, "--port", port, "--runtime-root", str(root), *more)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_launchd_unit_runs_this_loopx_privately_with_the_captured_environment(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeLaunchd()
    host = _host(tmp_path, checkout, fake, system="Darwin")
    root = tmp_path / "runtime"

    assert _install(monkeypatch, host, root, "--env", "MY_BOT_TOKEN") == 0

    plist_path = tmp_path / "home" / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    assert _mode(plist_path) == 0o600
    log = str(root / "logs" / "serve.log")
    assert plistlib.loads(plist_path.read_bytes()) == {
        "Label": LABEL,
        "ProgramArguments": [str(checkout / ".venv" / "bin" / "loopx"), "serve", "--port", "9876",
                             "--runtime-root", str(root)],
        "WorkingDirectory": str(checkout),
        "EnvironmentVariables": {**CAPTURED, "MY_BOT_TOKEN": "custom"},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    assert (root / "logs").is_dir()
    output = capsys.readouterr().out
    assert "0600" in output and "s3cret" not in output


def test_systemd_unit_runs_this_loopx_privately_with_the_captured_environment(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSystemd()
    host = _host(tmp_path, checkout, fake, system="Linux")
    host.environ = {**SHELL_ENV, "LOOPX_ODD": 'a "quoted" 100% $HOME\\path\nline2'}
    root = tmp_path / "run%time $root"

    assert _install(monkeypatch, host, root) == 0

    unit_path = tmp_path / "home" / ".config" / "systemd" / "user" / f"{LABEL}.service"
    assert _mode(unit_path) == 0o600
    lines = unit_path.read_text().splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("Environment="))
    assert service._systemd_words(values["ExecStart"]) == [
        str(checkout / ".venv" / "bin" / "loopx"), "serve", "--port", "9876", "--runtime-root", str(root)]
    assert values["WorkingDirectory"] == str(checkout)
    assert values["Restart"] == "on-failure" and values["WantedBy"] == "default.target"
    log = str(root / "logs" / "serve.log").replace("%", "%%")
    assert values["StandardOutput"] == values["StandardError"] == f"append:{log}"
    environment = [service._systemd_words(line.removeprefix("Environment="))[0]
                   for line in lines if line.startswith("Environment=")]
    assert dict(item.split("=", 1) for item in environment) == {
        **CAPTURED, "LOOPX_ODD": 'a "quoted" 100% $HOME\\path\nline2'}
    assert fake.commands()[:3] == ["daemon-reload", f"stop {LABEL}.service", f"enable --now {LABEL}.service"]


def test_reinstall_updates_the_unit_after_launchd_has_let_go_of_the_old_job(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLaunchd()
    host = _host(tmp_path, checkout, fake, system="Darwin")
    plist_path = tmp_path / "home" / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_text("stale")
    plist_path.chmod(0o644)

    assert _install(monkeypatch, host, tmp_path / "runtime") == 0
    first = plist_path.read_bytes()
    assert fake.commands()[:2] == ["print", "bootstrap"] and _mode(plist_path) == 0o600

    fake.calls.clear()
    assert _install(monkeypatch, host, tmp_path / "runtime") == 0

    assert plist_path.read_bytes() == first and _mode(plist_path) == 0o600
    # bootout returns while launchd still lists the job; bootstrap waits until it is gone.
    assert fake.commands()[:6] == ["print", "bootout", "print", "print", "print", "bootstrap"]
    assert fake.early_bootstraps == 0 and fake.loaded

    assert _install(monkeypatch, host, tmp_path / "runtime", port="9877") == 0
    assert plistlib.loads(plist_path.read_bytes())["ProgramArguments"][3] == "9877"
    assert not list(plist_path.parent.glob(".*.tmp"))


def test_a_failed_bootstrap_is_retried(tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLaunchd(bootstrap_failures=2)
    host = _host(tmp_path, checkout, fake, system="Darwin")

    assert _install(monkeypatch, host, tmp_path / "runtime") == 0
    assert fake.commands().count("bootstrap") == 3 and fake.loaded


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_install_does_not_start_on_a_port_another_process_holds(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], system: str
) -> None:
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system, port_busy=True)

    assert _install(monkeypatch, host, tmp_path / "runtime") == 1

    assert "port 9876 is in use" in capsys.readouterr().err
    assert not fake.serving
    assert not any(command in ("bootstrap", f"enable --now {LABEL}.service") for command in fake.commands())


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_restart_stops_then_starts_and_waits_until_the_port_serves(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], system: str
) -> None:
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    assert _loopx(monkeypatch, host, "restart", "--label", LABEL) == 1
    assert "not installed" in capsys.readouterr().err

    assert _install(monkeypatch, host, tmp_path / "runtime") == 0
    before = fake.pid
    fake.calls.clear()
    capsys.readouterr()

    assert _loopx(monkeypatch, host, "restart", "--label", LABEL) == 0

    assert fake.serving and fake.pid == before + 1
    assert f"pid {before} -> {before + 1}" in capsys.readouterr().out
    if system == "Darwin":
        assert fake.commands()[1:3] == ["print", "bootout"] and fake.commands()[-2] == "bootstrap"
        assert fake.early_bootstraps == 0
    else:
        assert fake.commands()[1:] == ["daemon-reload", f"stop {LABEL}.service", f"enable --now {LABEL}.service",
                                       f"show {LABEL}.service --property=LoadState,ActiveState,SubState,MainPID,"
                                       "ExecMainCode,ExecMainStatus"]


def test_waiting_gives_up_with_the_log_tail(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeLaunchd()
    host = _host(tmp_path, checkout, fake, system="Darwin")
    host.get_status = lambda port: None
    (tmp_path / "runtime" / "logs").mkdir(parents=True)
    (tmp_path / "runtime" / "logs" / "serve.log").write_text("starting\nOSError: address already in use\n")

    assert _install(monkeypatch, host, tmp_path / "runtime", "--timeout", "5") == 1

    err = capsys.readouterr().err
    assert "within 5 s" in err and "OSError: address already in use" in err


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_uninstall_stops_the_service_and_removes_the_unit(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], system: str
) -> None:
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    assert _install(monkeypatch, host, tmp_path / "runtime") == 0
    unit_path = service.manager(host, LABEL).unit_path

    assert _loopx(monkeypatch, host, "uninstall", "--label", LABEL) == 0

    assert not unit_path.exists() and not fake.serving
    if system == "Linux":
        assert fake.commands()[-2:] == [f"disable --now {LABEL}.service", "daemon-reload"]
    assert (tmp_path / "runtime" / "logs").is_dir()
    capsys.readouterr()
    assert _loopx(monkeypatch, host, "uninstall", "--label", LABEL) == 0
    assert "not installed" in capsys.readouterr().out


def test_status_shows_state_pid_last_exit_log_and_the_api(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeLaunchd()
    status = {"status": "running", "tasks": {"total": 3, "running": 1},
              "checks": [{"label": "Claude login", "status": "fail"}]}
    host = _host(tmp_path, checkout, fake, system="Darwin", status=status)
    assert _loopx(monkeypatch, host, "status", "--label", LABEL) == 1
    assert "not installed" in capsys.readouterr().out

    assert _install(monkeypatch, host, tmp_path / "runtime") == 0
    capsys.readouterr()
    assert _loopx(monkeypatch, host, "status", "--label", LABEL) == 0
    out = capsys.readouterr().out
    assert f"running, pid {fake.pid}" in out
    assert str(tmp_path / "runtime" / "logs" / "serve.log") in out
    assert "http://127.0.0.1:9876/api/status: running, 3 task(s), 1 running; check problems: Claude login" in out

    # Killed and not yet started again.
    fake.loaded = False
    original = fake.__call__

    def killed(argv):
        if list(argv)[1] == "print":
            fake.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "x = {\n\tstate = not running\n\truns = 2\n"
                                                        "\tlast terminating signal = Killed: 9\n}\n", "")
        return original(argv)

    host.run = killed
    assert _loopx(monkeypatch, host, "status", "--label", LABEL) == 1
    out = capsys.readouterr().out
    assert "loaded, not running, last exit Killed: 9" in out and "no answer" in out


@pytest.mark.parametrize(
    ("setup", "expected"),
    [("none", service.SKIP), ("running", service.OK), ("stopped", service.WARN), ("windows", service.SKIP)],
)
def test_doctor_reports_the_service_but_never_fails_on_it(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, setup: str, expected: str
) -> None:
    fake = FakeLaunchd()
    host = _host(tmp_path, checkout, fake, system="Windows" if setup == "windows" else "Darwin")
    if setup in ("running", "stopped"):
        assert _install(monkeypatch, host, tmp_path / "runtime") == 0
    if setup == "stopped":
        fake.loaded = False

    check = service.doctor_check(host, LABEL)

    assert (check.id, check.status) == ("service", expected)


@pytest.mark.parametrize("label", ["../escape", "", "-flag", "a/b"])
def test_a_label_that_is_not_a_plain_name_is_rejected(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], label: str
) -> None:
    fake = FakeLaunchd()
    host = _host(tmp_path, checkout, fake, system="Darwin")

    assert _loopx(monkeypatch, host, "install", f"--label={label}", "--runtime-root", str(tmp_path / "r")) == 1

    assert "--label" in capsys.readouterr().err and fake.calls == []
    assert not (tmp_path / "home").exists()


def test_without_a_console_script_the_unit_runs_the_module(tmp_path: Path) -> None:
    python = tmp_path / "bin" / "python3"
    python.parent.mkdir()
    python.write_text("")

    assert service.loopx_command(str(python)) == [str(python), "-m", "looper.cli"]
