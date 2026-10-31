"""``looper service``: the generated launchd and systemd units and how the commands drive the service manager.

The commands run through ``looper.cli.main`` against a stand-in launchd or
systemd that keeps the state a real one would: a ``launchctl bootout`` returns
before the job is gone, and a ``bootstrap`` issued meanwhile fails. Nothing
runs the real ``launchctl`` or ``systemctl``.
"""

from __future__ import annotations

import io
import json
import plistlib
import stat
import subprocess
import urllib.error
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper import cli, ops_config
from looper.agents import operator_auth, service

UID = 501
LABEL = "com.looper.serve.test"

# The shell `install` runs in: a Claude Code session's markers and unrelated
# secrets must stay out of the unit.
SHELL_ENV = {
    "PATH": "/opt/tools/bin:/usr/bin:/bin",
    "HOME": "/home/someone",
    "LANG": "en_US.UTF-8",
    "TZ": "UTC",
    "CODEX_HOME": "/test/codex-home",
    "LOOPER_DATABASE_URL": "postgresql://postgres:s3cret@127.0.0.1:54329/loopx",
    "LOOPX_DATABASE_URL": "postgresql://postgres:old-s3cret@127.0.0.1:54329/loopx",
    "LOOPER_PUBLIC_URL": "https://looper.example.com",
    "LOOPER_TELEGRAM_BOT_TOKEN": "123:abc",
    "LOOPX_TELEGRAM_BOT_TOKEN": "456:retired",
    "ANTHROPIC_API_KEY": "sk-test",
    "LARK_APP_ID": "cli_test",
    "ANTHROPIC_CUSTOM_HEADERS": "X-Secret: header-secret",
    "HTTPS_PROXY": "http://proxy.example:3128",
    "MY_BOT_TOKEN": "custom",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_SESSION_ID": "session",
    "GITHUB_TOKEN": "ghp_unrelated",
}
CAPTURED = {name: SHELL_ENV[name] for name in (
    "ANTHROPIC_API_KEY", "CODEX_HOME", "HTTPS_PROXY", "LANG", "LOOPER_TELEGRAM_BOT_TOKEN",
    "LOOPX_TELEGRAM_BOT_TOKEN", "PATH", "TZ",
)}


class FakeLaunchd:
    """launchd as `launchctl` shows it. A booted-out job stays visible for ``bootout_lag`` prints."""

    def __init__(self, *, loaded: bool = False, bootout_lag: int = 2, bootstrap_failures: int = 0,
                 label: str = LABEL) -> None:
        self.loaded, self.bootout_lag, self.bootstrap_failures = loaded, bootout_lag, bootstrap_failures
        self.label = label
        self.unloading = 0
        self.pid = 4000 if loaded else 0
        self.calls: list[list[str]] = []
        self.early_bootstraps = 0
        self.probe = None

    @property
    def serving(self) -> bool:
        return self.loaded

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        assert argv[0] == "launchctl", argv
        if argv[-1].endswith((f"/{self.label}.probe", f"/{self.label}.probe.plist")):
            if self.probe is None:
                self.probe = FakeLaunchd(label=f"{self.label}.probe")
            return self.probe(argv)
        command = argv[1]
        if command == "print":
            assert argv[2] == f"gui/{UID}/{self.label}"
            if self.unloading:
                self.unloading -= 1
                return self._done(argv, 0, f"{argv[2]} = {{\n\tstate = exited\n}}\n")
            if self.loaded:
                return self._done(argv, 0, f"{argv[2]} = {{\n\tstate = running\n\truns = 1\n\tpid = {self.pid}\n"
                                           "\tlast exit code = (never exited)\n\t\tstate = active\n}\n")
            return self._done(argv, 113, "", f'Could not find service "{self.label}" in domain for user gui: {UID}\n')
        if command == "bootout":
            assert self.loaded, "bootout of a job that is not loaded"
            self.loaded, self.unloading = False, self.bootout_lag
            return self._done(argv, 0)
        if command == "bootstrap":
            assert argv[2:] == [f"gui/{UID}", argv[3]] and argv[3].endswith(f"/{self.label}.plist")
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
        self.probe_active = False

    @property
    def serving(self) -> bool:
        return self.active

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        assert argv[:2] == ["systemctl", "--user"], argv
        args = argv[2:]
        if args[-1] == f"{LABEL}-probe.timer":
            assert args[:2] in (["enable", "--now"], ["disable", "--now"])
            self.probe_active = args[0] == "enable"
            return subprocess.CompletedProcess(argv, 0, "", "")
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
    """A checkout whose venv has the `looper` console script next to its interpreter."""
    bin_dir = tmp_path / "check out" / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("python3", "looper"):
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
        get_status=lambda port, token: answer if fake.serving else None,
        port_in_use=lambda port: port_busy and not fake.serving,
    )


def _looper(monkeypatch: pytest.MonkeyPatch, host: service.Host, *argv: str) -> int:
    with monkeypatch.context() as patch:
        patch.setattr(service.Host, "current", classmethod(lambda cls: host))
        patch.setenv("HOME", str(host.home))
        return cli.main(["service", *argv])


def _write_ops(root: Path, *, port: int | None = None, database: bool = True) -> Path:
    path = root / ops_config.FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    text = '[database]\nurl = "postgresql://postgres:s3cret@127.0.0.1:54329/loopx"\n' if database else ""
    text += '[server]\npublic_url = "https://looper.example.com"\n'
    if port is not None:
        text += f"port = {port}\n"
    path.write_text(text)
    path.chmod(0o600)
    return path


def _install(monkeypatch, host: service.Host, root: Path, *more: str, port: str | None = "9876") -> int:
    if not (root / ops_config.FILE).exists():
        _write_ops(root)
    flags = ("--port", port) if port is not None else ()
    return _looper(monkeypatch, host, "install", "--label", LABEL, *flags, "--runtime-root", str(root), *more)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _probe_paths(host):
    if host.system == "Darwin":
        return (service.Launchd(host, f"{LABEL}.probe").unit_path,)
    directory = service.Systemd(host, LABEL).unit_path.parent
    return tuple(directory / f"{LABEL}-probe.{suffix}" for suffix in ("service", "timer"))


def _configure_probe(root):
    path = _write_ops(root)
    with path.open("a") as stream:
        stream.write('[probe]\nnotify_channel = "lark"\nnotify_chat = "oc_probechat123"\n')


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_probe_units_run_once_per_minute_without_service_secrets(tmp_path, checkout, system):
    host = _host(tmp_path, checkout, FakeLaunchd(), system=system)
    host.environ["PATH"] = '/tools/100%/$bin:"quoted"\\path\nnext'
    root = tmp_path / "runtime 100%"
    config = root / ops_config.FILE
    spec = service.build_probe_spec(host, label=LABEL, runtime_root=root, config=config)
    expected = [str(checkout / ".venv/bin/looper"), "service", "probe", "--config", str(config),
                "--runtime-root", str(root)]
    assert list(spec.argv) == expected
    environment = {name: host.environ[name] for name in ("PATH", "TZ", "LANG", "HTTPS_PROXY")}
    assert dict(spec.env) == environment
    assert not set(dict(spec.env)) & {"ANTHROPIC_API_KEY", "LOOPER_TELEGRAM_BOT_TOKEN"}
    if system == "Darwin":
        unit = plistlib.loads(service.render_probe_plist(spec))
        assert unit == {"Label": f"{LABEL}.probe", "ProgramArguments": expected,
                        "WorkingDirectory": str(checkout), "EnvironmentVariables": environment,
                        "StartInterval": 60, "RunAtLoad": True,
                        "StandardOutPath": str(root / "logs/probe.log"),
                        "StandardErrorPath": str(root / "logs/probe.log")}
    else:
        unit, timer = service.render_probe_systemd(spec)
        values = dict(line.split("=", 1) for line in unit.splitlines()
                      if "=" in line and not line.startswith("Environment="))
        assert values["Type"] == "oneshot"
        assert service._systemd_words(values["ExecStart"]) == expected
        assert values["WorkingDirectory"] == str(checkout)
        assert values["StandardOutput"] == values["StandardError"] == f"append:{root}/logs/probe.log".replace("%", "%%")
        assignments = [service._systemd_words(line.removeprefix("Environment="))[0]
                       for line in unit.splitlines() if line.startswith("Environment=")]
        assert dict(item.split("=", 1) for item in assignments) == environment
        assert "Restart=" not in unit
        assert "[Timer]\nOnBootSec=60\nOnUnitActiveSec=60\n" in timer
        assert f"Unit={LABEL}-probe.service" in timer and "WantedBy=timers.target" in timer


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_install_reloads_probe_and_removes_it_when_unconfigured(tmp_path, checkout, monkeypatch, capsys, system):
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    root = tmp_path / "runtime"
    _configure_probe(root)
    for _ in range(2):
        assert _install(monkeypatch, host, root, "--env", "MY_BOT_TOKEN") == 0
        assert all(path.is_file() and _mode(path) == 0o600 for path in _probe_paths(host))
        assert all(b"sk-test" not in path.read_bytes() and b"MY_BOT_TOKEN" not in path.read_bytes()
                   for path in _probe_paths(host))
        out = capsys.readouterr().out
        assert f"probe      {_probe_paths(host)[-1]} every 60 s → lark oc_pro…" in out
        assert "oc_probechat123" not in out
        if system == "Darwin":
            assert fake.probe.loaded and fake.probe.early_bootstraps == 0
            assert fake.commands().index("bootstrap") < next(
                i for i, argv in enumerate(fake.calls) if argv[-1].endswith(f"{LABEL}.probe.plist"))
        else:
            assert fake.probe_active
            assert fake.commands()[-2:] == ["daemon-reload", f"enable --now {LABEL}-probe.timer"]
    _write_ops(root)
    assert _install(monkeypatch, host, root) == 0
    assert not any(path.exists() for path in _probe_paths(host))
    assert "probe      not configured: add [probe] to" in capsys.readouterr().out
    assert fake.serving
    if system == "Darwin":
        assert not fake.probe.loaded
    else:
        assert not fake.probe_active


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_probe_load_failure_keeps_service_successful_and_cleans_units(tmp_path, checkout, monkeypatch, capsys, system):
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    def run(argv):
        if (argv[1] == "bootstrap" and argv[-1].endswith(".probe.plist")
                or list(argv[2:4]) == ["enable", "--now"] and argv[-1].endswith("-probe.timer")):
            return subprocess.CompletedProcess(argv, 1, "", "probe rejected")
        return fake(argv)
    host.run = run
    root = tmp_path / "runtime"
    _configure_probe(root)
    assert _install(monkeypatch, host, root) == 0
    out = capsys.readouterr().out
    assert "probe      failed to load:" in out and "probe rejected" in out
    assert fake.serving and not any(path.exists() for path in _probe_paths(host))
    assert _looper(monkeypatch, host, "status", "--label", LABEL) == 0
    assert "probe      not installed" in capsys.readouterr().out


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("service_missing", [False, True])
def test_uninstall_removes_probe_before_the_service_even_if_service_is_missing(
    tmp_path, checkout, monkeypatch, system, service_missing,
):
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    root = tmp_path / "runtime"
    _configure_probe(root)
    assert _install(monkeypatch, host, root) == 0
    unit = service.manager(host, LABEL).unit_path
    if service_missing:
        unit.unlink()
        if system == "Darwin":
            fake.loaded = False
        else:
            fake.active = False
    fake.calls.clear()
    assert _looper(monkeypatch, host, "uninstall", "--label", LABEL) == 0
    assert not unit.exists() and not any(path.exists() for path in _probe_paths(host))
    assert not fake.serving
    if system == "Darwin":
        assert not fake.probe.loaded
        stops = [argv[-1] for argv in fake.calls if argv[1] == "bootout"]
        assert stops == [f"gui/{UID}/{LABEL}.probe", *([] if service_missing else [f"gui/{UID}/{LABEL}"])]
    else:
        assert not fake.probe_active
        stops = [argv[-1] for argv in fake.calls if argv[2:4] == ["disable", "--now"]]
        assert stops == [f"{LABEL}-probe.timer", *([] if service_missing else [f"{LABEL}.service"])]


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_restart_leaves_probe_running(tmp_path, checkout, monkeypatch, system):
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    root = tmp_path / "runtime"
    _configure_probe(root)
    assert _install(monkeypatch, host, root) == 0
    units = {path: path.read_bytes() for path in _probe_paths(host)}
    fake.calls.clear()
    assert _looper(monkeypatch, host, "restart", "--label", LABEL) == 0
    assert all(path.read_bytes() == data for path, data in units.items())
    assert not any(argv[-1].endswith((f"/{LABEL}.probe", f"/{LABEL}.probe.plist", f"{LABEL}-probe.timer"))
                   for argv in fake.calls)
    assert fake.probe.loaded if system == "Darwin" else fake.probe_active


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("state", [None, {"last_sample_at": "2026-10-30T12:00:00+00:00", "last_verdict": "no_answer",
                                        "alert_started_at": "2026-10-30T11:59:00+00:00"},
                                   {"last_sample_at": "2026-10-30T12:00:00+00:00", "last_verdict": "tick_stale",
                                    "last_send_error": "lark-cli missing"}, "corrupt"])
def test_status_reports_probe_check_alert_and_send_error(tmp_path, checkout, monkeypatch, capsys, system, state):
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    root = tmp_path / "runtime"
    _configure_probe(root)
    assert _install(monkeypatch, host, root) == 0
    capsys.readouterr()
    if state is not None:
        path = root / ".local/probe/state.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken" if state == "corrupt" else json.dumps(state))
    assert _looper(monkeypatch, host, "status", "--label", LABEL) == 0
    out = capsys.readouterr().out
    assert "probe      installed" in out
    if state in (None, "corrupt"):
        assert "last check no check yet" in out
    else:
        assert f"last check {state['last_sample_at']} {state['last_verdict']}" in out
        if state.get("alert_started_at"):
            assert f"alert since {state['alert_started_at']}" in out
        if state.get("last_send_error"):
            assert state["last_send_error"] in out


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_probe_follows_a_custom_service_label(tmp_path, checkout, system):
    label = "looper.other"
    fake = FakeLaunchd(label=label) if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    if system == "Linux":
        def run(argv):
            if argv[-1] == f"{label}-probe.timer":
                fake.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, "", "")
            return fake(argv)
        host.run = run
    root = tmp_path / "runtime"
    _configure_probe(root)
    ops = ops_config.load(ops_config.Flags(runtime_root=root), environ={})
    service.install(host, ops, label=label)
    path = (service.Launchd(host, f"{label}.probe").unit_path if system == "Darwin"
            else service.Systemd(host, f"{label}-probe").unit_path)
    assert path.is_file()
    assert not any(path.exists() for path in _probe_paths(host))
    service.uninstall(host, label=label)
    assert not path.exists()


def test_systemd_probe_cleanup_ignores_disable_failure(tmp_path, checkout):
    fake = FakeSystemd()
    host = _host(tmp_path, checkout, fake, system="Linux")
    for path in _probe_paths(host):
        service.write_private(path, b"stale")
    def run(argv):
        if list(argv[2:4]) == ["disable", "--now"]:
            fake.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, "", "timer already unloaded")
        return fake(argv)
    host.run = run
    service.remove_probe_units(host, LABEL)
    assert not any(path.exists() for path in _probe_paths(host))
    assert fake.commands() == [f"disable --now {LABEL}-probe.timer", "daemon-reload"]


@pytest.mark.parametrize("token", [None, "test-operator-token"])
@pytest.mark.parametrize("code", [200, 401, 503])
def test_status_request_sends_the_token_and_distinguishes_a_refusal(
    monkeypatch: pytest.MonkeyPatch, token: str | None, code: int,
) -> None:
    def open_request(request, *, timeout):
        assert request.full_url == "http://127.0.0.1:18834/api/v1/system/status"
        assert request.get_method() == "POST" and request.data == b"{}"
        assert request.get_header("Content-type") == "application/json"
        assert request.get_header("Authorization") == (f"Bearer {token}" if token else None)
        assert timeout == 3
        if code != 200:
            raise urllib.error.HTTPError(request.full_url, code, "refused", {}, None)
        return io.BytesIO(b'{"status": "ok", "result": {"status": "running"}}')

    monkeypatch.setattr(service.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    assert service._get_status(18834, token) == (
        {"status": "running"} if code == 200 else {"status": "unauthorized"} if code == 401 else None)


def test_start_wait_reads_the_token_again_when_the_new_server_creates_it(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLaunchd()
    host = _host(tmp_path, checkout, fake, system="Darwin")
    root = tmp_path / "runtime"
    received = []

    def get_status(port: int, token: str | None) -> dict | None:
        received.append((port, token))
        if token is None:
            operator_auth.token_path(root).write_text("test-operator-token\n")
            return None
        return {"status": "running"}

    host.get_status = get_status
    assert _install(monkeypatch, host, root) == 0
    assert received == [(9876, None), (9876, "test-operator-token")]


def test_launchd_unit_runs_this_looper_privately_with_the_captured_environment(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeLaunchd()
    host = _host(tmp_path, checkout, fake, system="Darwin")
    root = tmp_path / "runtime"

    assert _install(monkeypatch, host, root, "--env", "MY_BOT_TOKEN") == 0

    plist_path = tmp_path / "home" / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    assert _mode(plist_path) == 0o600
    log = str(root / "logs" / "serve.log")
    assert plist_path.read_bytes() == plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(checkout / ".venv" / "bin" / "looper"), "serve", "--config",
                             str(root / ops_config.FILE), "--runtime-root", str(root), "--port", "9876"],
        "WorkingDirectory": str(checkout),
        "EnvironmentVariables": {**CAPTURED, "MY_BOT_TOKEN": "custom"},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })
    assert (root / "logs").is_dir()
    output = capsys.readouterr().out
    assert "0600" in output and "s3cret" not in output
    assert "not copied" in output and "LARK_APP_ID" in output and "cli_test" not in output
    assert "ANTHROPIC_CUSTOM_HEADERS" in output and "header-secret" not in output


def test_systemd_unit_runs_this_looper_privately_with_the_captured_environment(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeSystemd()
    host = _host(tmp_path, checkout, fake, system="Linux")
    host.environ = {**SHELL_ENV, "LOOPER_ODD": 'a "quoted" 100% $HOME\\path\nline2'}
    root = tmp_path / "run%time $root"

    assert _install(monkeypatch, host, root, "--env", "LOOPER_ODD") == 0

    unit_path = tmp_path / "home" / ".config" / "systemd" / "user" / f"{LABEL}.service"
    assert _mode(unit_path) == 0o600
    lines = unit_path.read_text().splitlines()
    assert lines.count("KillMode=process") == 1
    assert lines.index("[Service]") < lines.index("KillMode=process") < lines.index("[Install]")
    assert lines[lines.index("RestartSec=5") + 1] == "KillMode=process"
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("Environment="))
    assert service._systemd_words(values["ExecStart"]) == [
        str(checkout / ".venv" / "bin" / "looper"), "serve", "--config", str(root / ops_config.FILE),
        "--runtime-root", str(root), "--port", "9876"]
    assert values["WorkingDirectory"] == str(checkout)
    assert values["Restart"] == "on-failure" and values["WantedBy"] == "default.target"
    log = str(root / "logs" / "serve.log").replace("%", "%%")
    assert values["StandardOutput"] == values["StandardError"] == f"append:{log}"
    environment = [service._systemd_words(line.removeprefix("Environment="))[0]
                   for line in lines if line.startswith("Environment=")]
    assert dict(item.split("=", 1) for item in environment) == {
        **CAPTURED, "LOOPER_ODD": 'a "quoted" 100% $HOME\\path\nline2'}
    assert fake.commands()[:3] == ["daemon-reload", f"stop {LABEL}.service", f"enable --now {LABEL}.service"]
    output = capsys.readouterr()
    assert "not copied" in output.out and "LARK_APP_ID" in output.out
    assert "cli_test" not in output.out + output.err and "s3cret" not in output.out + output.err


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
    assert service._given_port(plistlib.loads(plist_path.read_bytes())["ProgramArguments"]) == 9877
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
    assert _looper(monkeypatch, host, "restart", "--label", LABEL) == 1
    assert "not installed" in capsys.readouterr().err

    assert _install(monkeypatch, host, tmp_path / "runtime") == 0
    before = fake.pid
    fake.calls.clear()
    capsys.readouterr()

    assert _looper(monkeypatch, host, "restart", "--label", LABEL) == 0

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
    host.get_status = lambda port, token: None
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

    assert _looper(monkeypatch, host, "uninstall", "--label", LABEL) == 0

    assert not unit_path.exists() and not fake.serving
    if system == "Linux":
        assert fake.commands()[-2:] == [f"disable --now {LABEL}.service", "daemon-reload"]
    assert (tmp_path / "runtime" / "logs").is_dir()
    capsys.readouterr()
    assert _looper(monkeypatch, host, "uninstall", "--label", LABEL) == 0
    assert "not installed" in capsys.readouterr().out


def test_status_shows_state_pid_last_exit_log_and_the_api(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeLaunchd()
    status = {"status": "running", "tasks": {"total": 3, "running": 1},
              "checks": [{"label": "Claude login", "status": "fail"}]}
    host = _host(tmp_path, checkout, fake, system="Darwin", status=status)
    assert _looper(monkeypatch, host, "status", "--label", LABEL) == 1
    assert "not installed" in capsys.readouterr().out

    assert _install(monkeypatch, host, tmp_path / "runtime") == 0
    capsys.readouterr()
    assert _looper(monkeypatch, host, "status", "--label", LABEL) == 0
    out = capsys.readouterr().out
    assert f"running, pid {fake.pid}" in out
    assert str(tmp_path / "runtime" / "logs" / "serve.log") in out
    assert "http://127.0.0.1:9876/api/v1/system/status: running, 3 task(s), 1 running; check problems: Claude login" in out

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
    assert _looper(monkeypatch, host, "status", "--label", LABEL) == 1
    out = capsys.readouterr().out
    assert "loaded, not running, last exit Killed: 9" in out and "no answer" in out


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_status_reads_the_operator_token_from_the_units_runtime_root(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, system: str,
) -> None:
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    root = tmp_path / "runtime"
    assert _install(monkeypatch, host, root) == 0
    operator_auth.token_path(root).write_text("test-operator-token\n")
    received = []
    host.get_status = lambda port, token: received.append((port, token)) or {"status": "running"}

    assert _looper(monkeypatch, host, "status", "--label", LABEL) == 0
    assert received == [(9876, "test-operator-token")]


@pytest.mark.parametrize("label", [service.DEFAULT_LABEL, LABEL])
@pytest.mark.parametrize("kill_mode", ["", "KillMode=control-group\n", "#KillMode=process\n", "KillMode=process\n"])
def test_systemd_status_and_doctor_warn_when_the_unit_cannot_preserve_command_schedules(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    label: str, kill_mode: str,
) -> None:
    fake = FakeSystemd()
    host = _host(tmp_path, checkout, fake, system="Linux")
    root = tmp_path / "runtime"
    config = _write_ops(root)
    mgr = service.Systemd(host, label)
    spec = service.build_spec(host, label=label, port=9876, runtime_root=root, config=config)
    unit = service.render_systemd_unit(spec).replace("KillMode=process\n", "")
    service.write_private(mgr.unit_path, unit.replace("RestartSec=5\n", f"RestartSec=5\n{kill_mode}").encode())
    fake.active = True
    warning = ("the unit predates KillMode=process: restarting it ends running command schedules; "
               "run `looper service install` again")
    compatible = kill_mode == "KillMode=process\n"

    assert _looper(monkeypatch, host, "status", "--label", label) == 0
    assert (f"warning    {warning}" in capsys.readouterr().out) is not compatible
    check = service.doctor_check(host, label)
    assert check.status == (service.OK if compatible else service.WARN)
    if not compatible:
        assert warning in check.detail
        assert check.fix == "looper service install"

    fake.active = False
    assert _looper(monkeypatch, host, "status", "--label", label) == 1
    assert (f"warning    {warning}" in capsys.readouterr().out) is not compatible
    check = service.doctor_check(host, label)
    assert check.status == service.WARN
    if not compatible:
        assert check.fix == "looper service install"


def test_systemd_missing_unit_does_not_need_a_kill_mode_upgrade(tmp_path: Path, checkout: Path) -> None:
    host = _host(tmp_path, checkout, FakeSystemd(), system="Linux")

    assert service.Systemd(host, LABEL).kill_mode_ok()
    code, output = service.status(host, label=LABEL)
    assert code == 1 and "not installed" in output and "warning" not in output
    assert service.doctor_check(host, LABEL).status == service.SKIP


def test_status_reports_a_refused_operator_token_as_answering(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeLaunchd()
    host = _host(tmp_path, checkout, fake, system="Darwin", status={"status": "unauthorized"})
    root = tmp_path / "runtime"
    assert _install(monkeypatch, host, root) == 0
    capsys.readouterr()

    assert _looper(monkeypatch, host, "status", "--label", LABEL) == 0
    assert (f"api        http://127.0.0.1:9876/api/v1/system/status: unauthorized "
            f"(the operator token in {operator_auth.token_path(root)} was refused)") in capsys.readouterr().out


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

    _write_ops(tmp_path / "r")
    assert _looper(monkeypatch, host, "install", f"--label={label}", "--runtime-root", str(tmp_path / "r")) == 1

    assert "--label" in capsys.readouterr().err and fake.calls == []
    assert not (tmp_path / "home").exists()


def test_without_a_console_script_the_unit_runs_the_module(tmp_path: Path) -> None:
    python = tmp_path / "bin" / "python3"
    python.parent.mkdir()
    python.write_text("")

    assert service.looper_command(str(python)) == [str(python), "-m", "looper.cli"]


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_default_service_never_queries_or_changes_the_retired_unit_or_directory(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, system: str,
) -> None:
    fake = FakeLaunchd(label=service.DEFAULT_LABEL) if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    old_root = host.home / ".loopx"
    old_root.mkdir(parents=True)
    marker = old_root / "keep"
    marker.write_bytes(b"unchanged")
    old_unit = service.manager(host, "com.loopx.serve").unit_path
    old_unit.parent.mkdir(parents=True)
    old_unit.write_bytes(b"unchanged unit")
    assert service.doctor_check(host).status == service.SKIP
    assert _looper(monkeypatch, host, "status") == 1
    config = _write_ops(checkout)

    assert _looper(monkeypatch, host, "install", "--port", "9876", "--config", str(config)) == 0

    assert old_unit.read_bytes() == b"unchanged unit" and marker.read_bytes() == b"unchanged"
    assert not old_root.is_symlink()
    assert (host.home / ".looper" / "logs").is_dir()
    assert all("com.loopx.serve" not in " ".join(call) for call in fake.calls)
    assert service.doctor_check(host).status == service.OK


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_install_without_a_port_uses_the_ops_file_for_status_and_restart(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], system: str,
) -> None:
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    root = tmp_path / "runtime"
    _write_ops(root, port=9002)
    received = []
    host.get_status = lambda port, token: received.append(port) or {"status": "running"}

    assert _install(monkeypatch, host, root, port=None) == 0
    mgr = service.manager(host, LABEL)
    assert b"--port" not in mgr.unit_path.read_bytes()
    assert mgr.unit_settings() == (9002, root / "logs" / "serve.log", root)
    assert received == [9002]

    _write_ops(root, port=9003)
    received.clear()
    assert _looper(monkeypatch, host, "status", "--label", LABEL) == 0
    assert received == [9003]
    received.clear()
    assert _looper(monkeypatch, host, "restart", "--label", LABEL) == 0
    assert received == [9003]
    assert "http://127.0.0.1:9003" in capsys.readouterr().out


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_reinstall_does_not_pin_a_port_read_from_the_ops_file(tmp_path, checkout, monkeypatch, system):
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    root = tmp_path / "runtime"
    _write_ops(root, port=9002)
    assert _install(monkeypatch, host, root, port=None) == 0
    assert _install(monkeypatch, host, root, port=None) == 0
    mgr = service.manager(host, LABEL)
    assert b"--port" not in mgr.unit_path.read_bytes()
    _write_ops(root, port=9003)
    assert mgr.unit_settings()[0] == 9003


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_install_refuses_to_copy_a_database_setting_with_env(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], system: str,
) -> None:
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)

    assert _install(monkeypatch, host, tmp_path / "runtime", "--env", "LOOPER_DATABASE_URL") == 1

    err = capsys.readouterr().err
    assert "--env LOOPER_DATABASE_URL" in err and "ops setting" in err and "s3cret" not in err
    assert fake.calls == [] and not service.manager(host, LABEL).unit_path.exists()


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("variable,value", [
    ("LOOPER_DATABASE_URL", SHELL_ENV["LOOPER_DATABASE_URL"]),
    ("LOOPX_DATABASE_URL", SHELL_ENV["LOOPX_DATABASE_URL"]),
    ("LOOPER_PUBLIC_URL", "https://env.example.com"),
])
def test_reinstall_refuses_environment_settings_before_changing_the_running_service(
    tmp_path, checkout, monkeypatch, capsys, system, variable, value,
):
    for name in ops_config.VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ops_config, "DOTENV", tmp_path / ".env")
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    root = tmp_path / "runtime"
    assert _install(monkeypatch, host, root) == 0
    mgr = service.manager(host, LABEL)
    before = mgr.unit_path.read_bytes()
    config = root / ops_config.FILE
    config.write_text('[server]\n' if variable.endswith("DATABASE_URL") else
                      '[database]\nurl = "postgresql://localhost/loopx"\n')
    config_before = config.read_bytes()
    host.environ = {**host.environ, variable: value}
    monkeypatch.setenv(variable, value)
    fake.calls.clear()
    capsys.readouterr()

    assert _install(monkeypatch, host, root) == 1

    output = capsys.readouterr()
    assert variable in output.err and str(config) in output.err
    assert "s3cret" not in output.out + output.err
    assert mgr.unit_path.read_bytes() == before and config.read_bytes() == config_before
    assert fake.serving and fake.calls == []
    # Persisting the effective settings makes the same installation safe, even with the variable still set.
    ops = ops_config.load(ops_config.Flags(runtime_root=root))
    ops_config.write_private(config, ops_config.render(ops))
    assert _install(monkeypatch, host, root) == 0
    assert fake.serving and b"DATABASE_URL" not in mgr.unit_path.read_bytes()


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_install_requires_an_ops_file(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], system: str,
) -> None:
    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)

    assert _looper(monkeypatch, host, "install", "--label", LABEL, "--runtime-root", str(tmp_path / "runtime")) == 1

    output = capsys.readouterr()
    assert "looper config init-ops" in output.err and "server.toml" in output.err
    assert "s3cret" not in output.err + output.out
    assert fake.calls == [] and not service.manager(host, LABEL).unit_path.exists()


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize("override", ["none", "flag", "file"])
@pytest.mark.parametrize("old_config", [False, True], ids=["legacy-unit", "ops-unit"])
def test_reinstall_preserves_installed_port_unless_explicitly_overridden(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    system: str, override: str, old_config: bool,
) -> None:
    fake = FakeLaunchd(loaded=True) if system == "Darwin" else FakeSystemd()
    if system == "Linux":
        fake.active = True
    host = _host(tmp_path, checkout, fake, system=system)
    root = tmp_path / "runtime"
    _write_ops(root)
    mgr = service.manager(host, LABEL)
    old = service.ServiceSpec(
        label=LABEL, argv=(str(checkout / ".venv" / "bin" / "looper"), "serve", "--port", "9001",
                          "--runtime-root", str(root),
                          *(("--config", str(root / ops_config.FILE)) if old_config else ())), working_dir=checkout,
        env=(("LOOPER_DATABASE_URL", SHELL_ENV["LOOPER_DATABASE_URL"]),),
        log_path=root / "logs" / "serve.log",
    )
    service.write_private(mgr.unit_path, mgr.render(old))
    assert b"LOOPER_DATABASE_URL" in mgr.unit_path.read_bytes()

    expected = {"none": 9001, "flag": 9003, "file": 8767}[override]
    if override != "none":
        _write_ops(root, port=8767)
    host.port_in_use = lambda port: port == 8767 and override != "file"
    received = []
    host.get_status = lambda port, token: received.append(port) or {"status": "running"}
    assert _install(monkeypatch, host, root, port="9003" if override == "flag" else None) == 0

    data = mgr.unit_path.read_bytes()
    assert b"DATABASE_URL" not in data and b"postgresql://" not in data and b"s3cret" not in data
    assert (b"--port" in data) is (override != "file")
    assert mgr.unit_settings() == (expected, root / "logs" / "serve.log", root)
    assert received == [expected] and fake.serving
    assert _looper(monkeypatch, host, "restart", "--label", LABEL) == 0
    assert received == [expected, expected]
    output = capsys.readouterr()
    assert str(expected) in output.out and "s3cret" not in output.out + output.err


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_install_preserves_the_legacy_telegram_token_for_the_builtin_channel(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool,
    system: str,
) -> None:
    from looper.agents.channels import bots

    fake = FakeLaunchd() if system == "Darwin" else FakeSystemd()
    host = _host(tmp_path, checkout, fake, system=system)
    del host.environ["LOOPER_TELEGRAM_BOT_TOKEN"]
    flags = ("--env", "LOOPX_TELEGRAM_BOT_TOKEN") if explicit else ()
    assert _install(monkeypatch, host, tmp_path / ".loopx", *flags) == 0
    data = service.manager(host, LABEL).unit_path.read_bytes()
    if system == "Darwin":
        unit = plistlib.loads(data)
        environment, argv = unit["EnvironmentVariables"], unit["ProgramArguments"]
    else:
        lines = data.decode().splitlines()
        environment = dict(service._systemd_words(line.removeprefix("Environment="))[0].split("=", 1)
                           for line in lines if line.startswith("Environment="))
        argv = service._systemd_words(next(line.removeprefix("ExecStart=") for line in lines
                                          if line.startswith("ExecStart=")))
    assert environment["LOOPX_TELEGRAM_BOT_TOKEN"] == SHELL_ENV["LOOPX_TELEGRAM_BOT_TOKEN"]
    assert argv[-4:-2] == ["--runtime-root", str(tmp_path / ".loopx")]
    monkeypatch.setattr(service.os, "environ", environment)
    assert bots.builtin_env_token({}) == SHELL_ENV["LOOPX_TELEGRAM_BOT_TOKEN"]
