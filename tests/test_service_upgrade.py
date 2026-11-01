"""Upgrade previews use the running manifest and never mutate a service."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper import cli, components
from looper.agents import operator_auth, service
from looper.components import Manifest


def _manifest(changed: str | None = None) -> Manifest:
    return Manifest(
        version="1.0.0", loaded_at="2026-10-30T00:00:00+00:00",
        components={name: ("b" if name == changed else "a") * 64
                    for name in ("web", "control_plane", "keeper", "runners")},
        adoption={"run_manifest_version": 0, "work_contract": "c" * 64,
                  "review_contract": "d" * 64,
                  "seat_tools": {"worker": "e" * 64, "reviewer": "f" * 64},
                  "keeper_protocols": []},
    )


@pytest.fixture
def upgrade_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "runtime"
    token = operator_auth.ensure_token(root)
    unit = tmp_path / "looper.service"
    unit.write_text('Environment="UNIT_MARKER=service"\n')
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    requests = []

    def forbidden(*args, **kwargs):
        pytest.fail("an upgrade preview must not install, restart, sleep or run the service manager")

    def get_components(port, credential):
        requests.append((port, credential))
        assert (port, credential) == (18834, token)
        return _manifest().to_dict()

    def run(argv, *, env=None):
        assert argv[:3] == [host.executable, "-I", "-c"], "ownership must be checked before git"
        if "sysconfig" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0,
                json.dumps([str(checkout / ".venv"), str(checkout), str(checkout)]), "")
        if "sys.prefix" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, str(checkout / ".venv"), "")
        assert env["UNIT_MARKER"] == "service"
        assert "PYTHONPATH" not in env
        return subprocess.CompletedProcess(argv, 0, str(checkout), "")

    host = service.Host(
        system="Linux", home=tmp_path / "home", uid=501, environ={},
        executable=str(tmp_path / ".venv/bin/python"), cwd=checkout, package_root=checkout, run=run,
        sleep=forbidden, get_status=lambda *args: {"status": "ok"}, port_in_use=forbidden, get_components=get_components,
        get_whoami=forbidden,
    )
    state = service.State(installed=True, loaded=True, running=True, pid=1234, port=18834,
                          log_path=root / "logs/serve.log", runtime_root=root)
    manager = SimpleNamespace(
        host=host,
        unit_argv=lambda: service.looper_command(host.executable) + ["serve"],
        unit_path=unit, state=lambda: state, unit_settings=lambda **kwargs: (18834, state.log_path, root),
        start=forbidden, stop=forbidden, remove=forbidden,
    )
    monkeypatch.setattr(service, "manager", lambda host, label: manager)
    monkeypatch.setattr(service.Host, "current", classmethod(lambda cls: host))
    from looper.agents import http_server
    monkeypatch.setattr(http_server, "default_static_dir", lambda: checkout / "looper/web/dashboard")
    monkeypatch.setenv("HOME", str(host.home))
    return host, requests


@pytest.fixture
def stopping_upgrade(upgrade_host):
    host, _ = upgrade_host
    mgr = service.manager(host, "test")
    events, elapsed = [], [0.0]
    state = mgr.state()

    def stop():
        events.append("stop")
        state.running, state.pid, state.loaded = False, None, False

    def start():
        events.append("start")
        state.running, state.pid, state.loaded = True, 5678, True

    host.clock = lambda: elapsed[0]
    host.sleep = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    host.pid_alive = lambda pid: state.pid == pid
    host.port_in_use = lambda *args: state.running
    host.get_status = lambda *args: {"status": "ok", "pid": state.pid} if state.running else None
    host.get_whoami = lambda *args: {"looper_version": "1.0.0"}
    host.system_operation = lambda port, token, name, args: events.append(name) or {"phase": "drained"}
    mgr.stop, mgr.start = stop, start

    def execute():
        from looper.upgrade import UpgradeKind, UpgradePlan

        return service._execute_plan(host, mgr, port=18834, root=state.runtime_root,
            log_path=state.log_path, plan=UpgradePlan(UpgradeKind.FULL, (), ()),
            running=_manifest(), target=_manifest(), installer=lambda: events.append("install"),
            timeout=1, drain_timeout=1, no_force=False)

    return SimpleNamespace(host=host, mgr=mgr, state=state, events=events, elapsed=elapsed, execute=execute)


def test_upgrade_renews_drain_during_a_blocking_stop(stopping_upgrade, monkeypatch):
    from concurrent.futures import wait

    case = stopping_upgrade
    entered, release = threading.Event(), threading.Event()
    renewals = []
    stop, operation = case.mgr.stop, case.host.system_operation

    def blocking_stop():
        entered.set()
        assert release.wait(1), "blocking stop was not accompanied by lease renewal"
        stop()

    def fake_wait(futures, *, timeout):
        assert entered.wait(1)
        assert timeout <= 5
        case.elapsed[0] += timeout
        if case.elapsed[0] >= 35:
            release.set()
            return wait(futures, timeout=1)
        return wait(futures, timeout=0)

    def operation_with_lease(port, token, name, args):
        if name == "drain_status":
            renewals.append(case.elapsed[0])
        return operation(port, token, name, args)

    case.mgr.stop = blocking_stop
    case.host.system_operation = operation_with_lease
    monkeypatch.setattr(service, "wait", fake_wait, raising=False)
    result = json.loads(case.execute().splitlines()[-1])
    assert result["status"] == "ok"
    assert renewals and max(b - a for a, b in zip([0, *renewals], [*renewals, 35])) <= 5
    assert case.events[-3:] == ["stop", "install", "start"]


@pytest.mark.parametrize("other_pid", [None, 9876])
def test_upgrade_recovery_unloads_an_exited_launchd_label_before_starting(stopping_upgrade, other_pid):
    case = stopping_upgrade
    case.host.system = "Darwin"
    launchd = service.Launchd(case.host, "com.looper.upgrade.test")
    launchd.unit_path.parent.mkdir(parents=True)
    launchd.unit_path.write_bytes(launchd.render(service.ServiceSpec(
        launchd.label, ("python", "-m", "looper.cli", "serve", "--port", "18834",
                        "--runtime-root", str(case.state.runtime_root)),
        case.host.cwd, (), case.state.log_path)))
    loaded, bootouts, unloading = [True], [0], [0]

    def run(argv):
        command = argv[1]
        case.events.append(command)
        if command == "print":
            if not loaded[0]:
                return subprocess.CompletedProcess(argv, 113, "", "")
            if unloading[0]:
                unloading[0] -= 1
                if not unloading[0]:
                    loaded[0] = False
            pid = (f"\tstate = running\n\tpid = {case.state.pid}\n" if case.state.pid
                   else "\tstate = exited\n\tlast exit code = 0\n")
            return subprocess.CompletedProcess(argv, 0, pid, "")
        if command == "bootout":
            bootouts[0] += 1
            case.state.running, case.state.pid = False, None
            if bootouts[0] == 1:
                case.elapsed[0] += service._STOP_WAIT_SECONDS
                raise service.ServiceError("stop timed out after the old process exited")
            unloading[0] = 2
        elif command == "bootstrap":
            assert not loaded[0], "bootstrap must follow a completed bootout"
            loaded[0], case.state.running, case.state.pid = True, True, 5678
        else:
            pytest.fail(f"unexpected command: {argv}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    case.host.run = run
    case.mgr.stop, case.mgr.start, case.mgr.state = launchd.stop, launchd.start, launchd.state
    case.host.port_in_use = lambda *args: other_pid is not None or case.state.running
    case.host.get_status = lambda *args: ({"status": "ok"}
                                         if other_pid or case.state.running else None)
    operation = case.host.system_operation
    case.host.system_operation = lambda port, token, name, args: (
        {"pid": other_pid} if name == "health" else operation(port, token, name, args))
    with pytest.raises(service.ServiceError) as caught:
        case.execute()
    result = json.loads(str(caught.value).splitlines()[-1])
    assert case.events.count("bootstrap") == (0 if other_pid else 1)
    assert result["serving"] is True
    if other_pid:
        assert str(other_pid) in result["recovery_error"] and "18834" in result["recovery_error"]
    else:
        assert bootouts[0] == 2
        assert "recovery_error" not in result


@pytest.mark.parametrize("dirty", [False, True])
def test_upgrade_rejects_foreign_pythonpath_checkout_before_git(upgrade_host, tmp_path, monkeypatch, dirty):
    host, _ = upgrade_host
    foreign = tmp_path / "foreign-checkout"
    foreign.mkdir()
    host.cwd = host.package_root = foreign
    host.environ = {"PYTHONPATH": str(foreign)}
    monkeypatch.setenv("PYTHONPATH", str(foreign))
    probe, git_calls = host.run, []

    def run(argv, **kwargs):
        if argv[0] == "git":
            git_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, " M looper/cli.py" if dirty else "", "")
        return probe(argv, **kwargs)

    host.run = run
    monkeypatch.setattr(components, "manifest_from_git", lambda *args: _manifest())
    with pytest.raises(service.ServiceError) as caught:
        service.upgrade(host)
    assert "PYTHONPATH" in str(caught.value)
    assert git_calls == []


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_upgrade_queries_the_unit_interpreter_in_an_isolated_environment(
        upgrade_host, tmp_path, monkeypatch, system):
    host, _ = upgrade_host
    own_root = Path(service.__file__).resolve().parents[2]
    foreign = tmp_path / "foreign"
    (foreign / "looper").mkdir(parents=True)
    (foreign / "looper/__init__.py").write_text("raise AssertionError('foreign package imported')\n")
    host.cwd = host.package_root = foreign
    host.executable, host.system = sys.executable, system
    host.environ = {"SHELL_ONLY": "shell"}
    monkeypatch.setenv("PYTHONPATH", str(foreign))
    monkeypatch.setenv("SHELL_ONLY", "shell")
    mgr = (service.Launchd if system == "Darwin" else service.Systemd)(host, "com.looper.upgrade.test")
    mgr.unit_path.parent.mkdir(parents=True)
    mgr.unit_path.write_bytes(mgr.render(service.ServiceSpec(mgr.label,
        (*service.looper_command(sys.executable), "serve"), own_root,
        (("UNIT_MARKER", "service $$ value"),), tmp_path / "serve.log")))
    # Keep settings/status fake; read the real unit's argv and environment and run its Python.
    fake = service.manager(host, "test")
    fake.unit_path, fake.unit_argv = mgr.unit_path, mgr.unit_argv
    probes = []

    def run(argv, *, env):
        assert argv[:3] == [sys.executable, "-I", "-c"], "no git command may precede the ownership check"
        assert env["UNIT_MARKER"] == "service $$ value"
        assert "PYTHONPATH" not in env and "SHELL_ONLY" not in env
        assert "UNIT_MARKER=service $$ value" not in argv
        result = service._run(argv, env=env)
        assert result.returncode == 0, result.stderr
        probes.append(Path(result.stdout.strip()))
        return result

    host.run = run
    with pytest.raises(service.ServiceError, match="not the service's checkout") as caught:
        service.upgrade(host)
    assert probes == [own_root]
    assert str(own_root) in str(caught.value) and str(foreign) in str(caught.value)


@pytest.mark.parametrize("change", ["web", None], ids=["web_only", "noop"])
def test_wheel_upgrade_refuses_a_foreign_dashboard_before_installing(upgrade_host, tmp_path, monkeypatch, change):
    from looper.agents import http_server

    host, _ = upgrade_host
    own_root = host.cwd
    foreign = tmp_path / "foreign/looper/web/dashboard"
    monkeypatch.setattr(http_server, "default_static_dir", lambda: foreign)
    monkeypatch.setattr(components, "manifest_from_wheel", lambda *args: _manifest(change))
    monkeypatch.setattr(service, "_install_target", lambda *args, **kwargs: pytest.fail("must refuse before install"))
    with pytest.raises(service.ServiceError, match="not the service's dashboard") as caught:
        service.upgrade(host, to="candidate.whl")
    assert str(foreign) in str(caught.value) and str(own_root) in str(caught.value)


@pytest.mark.parametrize("change, changed_component, expected", [
    ("docs", None, "NOOP"),
    ("web/src", "web", "WEB_ONLY"),
    ("looper/agents/tiers/planner.py", "control_plane", "FULL"),
])
def test_cli_upgrade_dry_run_rfc_examples(upgrade_host, monkeypatch, capsys, change, changed_component, expected):
    host, requests = upgrade_host
    targets = []

    def from_git(repo, ref):
        targets.append((repo, ref))
        return _manifest(changed_component)

    monkeypatch.setattr(components, "manifest_from_git", from_git)

    assert cli.main(["service", "upgrade", "--dry-run", "--to", "candidate"]) == 0
    output = capsys.readouterr().out
    reasons = {"NOOP": "No component or adoption contract changed.",
               "WEB_ONLY": "Only web changed; the control plane can keep running.",
               "FULL": "Keeper mode is off; control-plane changes require a full drain."}
    assert output == (f"plan       {expected}\ntarget     1.0.0\n"
                      "running    1.0.0 (loaded 2026-10-30T00:00:00+00:00)\n"
                      f"changed    {changed_component or 'none'}\nreason     {reasons[expected]}\n")
    assert targets == [(host.cwd, "candidate")]
    assert len(requests) == 1


@pytest.mark.parametrize("system,preserves,keeper_on,expected", [
    ("Darwin", True, False, "FULL"),
    ("Darwin", True, True, "CONTROL_PLANE"),
    ("Linux", False, True, "FULL"),
    ("Linux", True, True, "CONTROL_PLANE"),
])
def test_upgrade_preview_uses_running_keeper_mode_and_unit(
        upgrade_host, monkeypatch, system, preserves, keeper_on, expected):
    host, _ = upgrade_host
    host.system = system
    monkeypatch.setattr(service, "_unit_package_root", lambda *args: host.package_root)
    host.get_status = lambda *args: {"keepers": {"mode": "on"}} if keeper_on else {"status": "ok"}
    if system == "Linux":
        original = service.manager(host, "test")
        mgr = service.Systemd(host, "upgrade-test")
        monkeypatch.setattr(mgr, "unit_argv", original.unit_argv)
        monkeypatch.setattr(mgr, "unit_settings", original.unit_settings)
        monkeypatch.setattr(mgr, "kill_mode_ok", lambda: preserves)
        monkeypatch.setattr(service, "manager", lambda *args: mgr)
        monkeypatch.setattr(service, "_unit_package_root", lambda *args: host.package_root)
    target = _manifest("control_plane")
    monkeypatch.setattr(components, "manifest_from_git", lambda *args: target)
    output = service.upgrade(host, dry_run=True)
    assert f"plan       {expected}" in output
    if keeper_on:
        assert "keepers    on (0 live)" in output
        assert ("reinstall the service" in output) is not preserves
    else:
        assert output == "\n".join((
            "plan       FULL", "target     1.0.0", "running    1.0.0 (loaded 2026-10-30T00:00:00+00:00)",
            "changed    control_plane",
            "reason     Keeper mode is off; control-plane changes require a full drain."))


def test_upgrade_preview_refuses_unsupported_live_keeper(upgrade_host, monkeypatch):
    from looper import upgrade
    host, _ = upgrade_host
    host.get_status = lambda *args: {"keepers": {"mode": "on"}}
    target = _manifest("control_plane")
    target.adoption["keeper_protocols"] = ["1.0"]
    running = _manifest()
    running.adoption["keeper_protocols"] = ["1.0"]
    host.get_components = lambda *args: running.to_dict()
    monkeypatch.setattr(components, "manifest_from_git", lambda *args: target)
    monkeypatch.setattr(upgrade, "scan_keepers", lambda root: (upgrade.Keeper(protocols=("2.0",)),))
    output = service.upgrade(host, dry_run=True)
    assert "plan       FULL" in output and "2.0" in output
    assert "keepers    on (1 live)" in output


def test_upgrade_refuses_when_keeper_liveness_is_unknown(upgrade_host, monkeypatch, capsys):
    from looper import upgrade
    host, _ = upgrade_host
    monkeypatch.setattr(components, "manifest_from_git", lambda *args: _manifest("control_plane"))
    def unknown(root):
        raise ValueError("cannot tell which keepers are running")
    monkeypatch.setattr(upgrade, "scan_keepers", unknown)
    assert cli.main(["service", "upgrade", "--dry-run"]) == 1
    assert "cannot tell which keepers are running" in capsys.readouterr().err


def test_upgrade_preview_rejects_a_git_work_contract_change(upgrade_host):
    host, _ = upgrade_host
    root = Path(components.__file__).resolve().parent.parent
    for name in ("looper/components.toml", "looper/__init__.py", "looper/agents/system_prompt.py",
                 "pyproject.toml", *components.SEAT_SOURCES):
        destination = host.cwd / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((root / name).read_bytes())
    def git(*args):
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                        "-C", str(host.cwd), *args], check=True, capture_output=True, text=True, timeout=10)
    git("init", "-q")
    git("config", "user.name", "Upgrade fixture")
    git("config", "user.email", "upgrade@example.invalid")
    git("add", "looper", "pyproject.toml")
    git("commit", "-qm", "initial source")
    running = components.manifest_from_git(host.cwd, "HEAD")
    host.get_components = lambda *args: running.to_dict()
    host.get_status = lambda *args: {"keepers": {"mode": "on"}}
    contract = host.cwd / "looper/agents/system_prompt.py"
    contract.write_text(contract.read_text().replace('WORK_CONTRACT = """\\\n',
        'WORK_CONTRACT = """\\\nUpdated work contract.\n', 1))
    git("add", "looper/agents/system_prompt.py")
    git("commit", "-qm", "work contract update")
    output = service.upgrade(host, to="HEAD", dry_run=True)
    assert "plan       FULL" in output
    assert "adoption contract changed" in output.lower()


def test_cli_upgrade_defaults_to_checkout_origin_main(upgrade_host, monkeypatch, capsys):
    host, _ = upgrade_host
    targets = []

    def from_git(repo, ref):
        targets.append((repo, ref))
        return _manifest()

    monkeypatch.setattr(components, "manifest_from_git", from_git)
    assert cli.main(["service", "upgrade", "--dry-run"]) == 0
    assert "NOOP" in capsys.readouterr().out
    assert targets == [(host.cwd, "origin/main")]


def test_cli_upgrade_can_inspect_a_wheel_without_installing(upgrade_host, monkeypatch, tmp_path, capsys):
    wheel = tmp_path / "looper-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"reader is replaced by a manifest stand-in")
    targets = []

    def from_wheel(path):
        targets.append(path)
        return _manifest("web")

    monkeypatch.setattr(components, "manifest_from_wheel", from_wheel)
    monkeypatch.setattr(components, "manifest_from_git", lambda *args: pytest.fail("wheel must not read git"))
    assert cli.main(["service", "upgrade", "--dry-run", "--to", str(wheel)]) == 0
    assert "WEB_ONLY" in capsys.readouterr().out
    assert targets == [wheel]


@pytest.mark.parametrize("target_kind", ["git", "wheel"])
def test_cli_upgrade_preview_with_the_released_seat_tool_probe(
    upgrade_host, monkeypatch, tmp_path, capsys, target_kind,
):
    host, _ = upgrade_host
    root = Path(components.__file__).resolve().parent.parent
    sources = {name: (root / name).read_bytes() for name in (
        "looper/components.toml", "looper/__init__.py", "looper/agents/system_prompt.py",
        "pyproject.toml", *components.SEAT_SOURCES,
    )}
    if target_kind == "wheel":
        wheel = tmp_path / "candidate.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, source in sources.items():
                archive.writestr(name, source)
        arguments = ["--to", str(wheel)]
    else:
        for name, source in sources.items():
            destination = host.cwd / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source)
        for args in (("init", "-q"), ("add", "looper", "pyproject.toml"),
                     ("commit", "-qm", "upgrade candidate"), ("update-ref", "refs/remotes/origin/main", "HEAD")):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                            "-c", "user.name=Upgrade fixture", "-c", "user.email=upgrade@example.invalid",
                            "-C", str(host.cwd), *args], check=True, capture_output=True, timeout=10)
        arguments = []  # The default target is origin/main.

    # Frozen from v2.14.69, blob c668178e663718836a2821c39a6a7aa79c006577.
    # Planning uses the installed reader before any candidate code is installed.
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "_component_probe.py").write_bytes(
        (root / "tests/fixtures/component_probe_v2_14_69.py").read_bytes())
    monkeypatch.setattr(components, "__file__", str(installed / "components.py"))
    monkeypatch.setattr(components, "_seat_tools", components._seat_tools.__wrapped__)

    assert cli.main(["service", "upgrade", "--dry-run", *arguments]) == 0
    assert "plan       FULL" in capsys.readouterr().out


@pytest.mark.parametrize("path, needle, replacement", [
    ("looper/agents/ops/__init__.py", '    READ = "read"',
     '    READ = (MARK, "read")[1]'),
    ("looper/agents/ops/__init__.py", "    name: str",
     "    MARK\n    name: str"),
    ("looper/agents/ops/__init__.py", "@dataclass(frozen=True)\nclass Tool:",
     "@(MARK or dataclass(frozen=True))\nclass Tool:"),
    ("looper/agents/ops/__init__.py", '        return {"name": self.name,',
     '        MARK\n        return {"name": self.name,'),
    ("looper/agents/ops/room.py", '"maximum": 120',
     '"maximum": (MARK, 120)[1]'),
    ("looper/agents/ops/room.py", "    if kind is MemberKind.WORK:",
     "    MARK\n    if kind is MemberKind.WORK:"),
    ("looper/agents/ops/vault.py", "async def _never(call: Any)",
     "async def _never(call: Any = MARK)"),
])
def test_cli_upgrade_rejects_executable_wheel_declarations_without_side_effects(
    upgrade_host, tmp_path, capsys, path, needle, replacement,
):
    root = Path(components.__file__).resolve().parent.parent
    sources = {name: (root / name).read_text() for name in (
        "looper/components.toml", "looper/__init__.py", "looper/agents/system_prompt.py",
        *components.SEAT_SOURCES,
    )}
    wheel = tmp_path / "candidate.whl"

    def write_wheel():
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, source in sources.items():
                archive.writestr(name, source)

    # A real, supported wheel must work through the same CLI and manifest reader.
    write_wheel()
    assert cli.main(["service", "upgrade", "--dry-run", "--to", str(wheel)]) == 0
    capsys.readouterr()
    marker = tmp_path / "executed"
    payload = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')"
    assert needle in sources[path]
    sources[path] = sources[path].replace(needle, replacement.replace("MARK", payload), 1)
    write_wheel()

    result = cli.main(["service", "upgrade", "--dry-run", "--to", str(wheel)])

    assert not marker.exists(), "dry-run executed code from the candidate wheel"
    assert result == 1
    captured = capsys.readouterr()
    assert "cannot read target Worker/Reviewer seat tool contracts" in captured.err + captured.out


@pytest.mark.parametrize("answer", [None, {"status": "unauthorized"}, {"unexpected": "response"}])
def test_upgrade_fails_closed_when_the_running_manifest_is_unavailable(upgrade_host, monkeypatch, answer):
    host, _ = upgrade_host
    host.get_components = lambda port, token: answer
    monkeypatch.setattr(components, "manifest_from_git", lambda *args: _manifest("control_plane"))
    with pytest.raises(service.ServiceError):
        service.upgrade(host, dry_run=True)


@pytest.mark.parametrize("code", [200, 401, 503])
def test_components_request_uses_operator_auth_and_post(monkeypatch, code):
    manifest = _manifest().to_dict()

    def open_request(request, *, timeout):
        assert request.full_url == "http://127.0.0.1:18834/api/v1/system/components"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer preview-token"
        assert timeout == 3
        if code != 200:
            raise urllib.error.HTTPError(request.full_url, code, "refused", {}, None)
        return io.BytesIO(json.dumps({"status": "ok", "result": manifest}).encode())

    monkeypatch.setattr(service.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    if code == 200:
        assert service._get_components(18834, "preview-token") == manifest
    else:
        with pytest.raises(service.ServiceError, match=str(code)):
            service._get_components(18834, "preview-token")


@pytest.mark.parametrize("field,value", [("keeper_protocols", [{}]), ("seat_tools", {}),
                                          ("run_manifest_version", "0")])
def test_upgrade_rejects_malformed_adoption_data(upgrade_host, monkeypatch, field, value):
    host, _ = upgrade_host
    answer = _manifest().to_dict()
    answer["adoption"][field] = value
    host.get_components = lambda port, token: answer
    monkeypatch.setattr(components, "manifest_from_git", lambda *args: _manifest())
    with pytest.raises(service.ServiceError, match="adoption"):
        service.upgrade(host, dry_run=True)
