"""Planned deployments exercise CLI ordering, failure recovery and their reports."""
from __future__ import annotations

import json
import asyncio
import multiprocessing
import os
import plistlib
import signal
import subprocess
import venv
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper import cli, components
from looper.agents import operator_auth, service
from looper.upgrade import UpgradeKind, UpgradePlan

_REAL_INSTALLER = service._install_target
_REAL_UNIT_PACKAGE_ROOT = service._unit_package_root


def shell_environment():
    return {name: value for name, value in os.environ.items()
            if name not in (*service._PYTHON_IMPORT_OVERRIDES, *service._GIT_REPOSITORY_OVERRIDES)}


def wheel_paths(host):
    return json.dumps([str(host.cwd / ".venv"), str(host.package_root), str(host.package_root)])


def manifest(change=None, version="1.0.0"):
    return components.Manifest(version, "startup", {
        name: ("b" if name == change else "a") * 64 for name in ("web", "control_plane", "keeper", "runners")},
        {"run_manifest_version": 0, "work_contract": "c" * 64, "review_contract": "d" * 64,
         "seat_tools": {"worker": "e" * 64, "reviewer": "f" * 64}, "keeper_protocols": []},
        {"web": "f" * 64})


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    operator_auth.ensure_token(root)
    unit = tmp_path / "unit"
    unit.write_text("isolated manager")
    events = []
    current = manifest()
    target = manifest("control_plane", "1.0.1")
    state = service.State(installed=True, loaded=True, running=True, pid=123456789)
    elapsed = [0.0]
    statuses = [{"phase": "draining", "waiting": {"runs": []}}, {"phase": "drained"}]
    scenario = SimpleNamespace(target=target, current=current, install_error=False, stop_error=False, stop_then_error=False,
                               verify_error=False, process_lingers=False)

    def operation(port, token, name, args):
        events.append(name)
        if name == "drain_cancel":
            return {"phase": "serving"}
        return statuses.pop(0) if len(statuses) > 1 else statuses[0]

    def stop():
        events.append("stop")
        if scenario.stop_error:
            raise service.ServiceError("stop failed")
        state.running = False
        state.pid = None
        if scenario.stop_then_error:
            scenario.stop_then_error = False
            raise service.ServiceError("manager stopped the service but lost its response")

    def start():
        events.append("start")
        state.running = True
        state.pid = 123456790

    def install(*args, **kwargs):
        assert not state.running, "install must wait for the old control plane to exit"
        assert "stop" in events
        assert "drained" in [status.get("phase") for status in statuses] or statuses[0].get("deadline_passed")
        events.append("install")
        if scenario.install_error:
            raise service.ServiceError("install failed")
        scenario.current = scenario.target

    def run(argv, **kwargs):
        if argv[:3] == [host.executable, "-I", "-c"]:
            output = wheel_paths(host) if "sysconfig" in argv[-1] else str(tmp_path / ".venv")
            return subprocess.CompletedProcess(argv, 0, output, "")
        assert argv[:3] == ["git", "-C", str(tmp_path)]
        return subprocess.CompletedProcess(argv, 0, "1" * 40 if "rev-parse" in argv else "", "")

    host = service.Host(system="Linux", home=tmp_path / "home", uid=1, environ={},
        executable="python", cwd=tmp_path, package_root=tmp_path, run=run, clock=lambda: elapsed[0],
        sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        get_components=lambda port, token: (current if scenario.verify_error else scenario.current).to_dict(),
        system_operation=operation, get_status=lambda *args: {"status": "ok"},
        get_whoami=lambda *args: {"looper_version": scenario.current.version},
        port_in_use=lambda port, address="127.0.0.1": False, pid_alive=lambda pid: scenario.process_lingers)
    manager = SimpleNamespace(host=host, state=lambda: state, unit_path=unit,
        unit_argv=lambda: service.looper_command(host.executable) + ["serve"],
        unit_settings=lambda **kwargs: (18841, root / "logs/serve.log", root), start=start, stop=stop)
    monkeypatch.setattr(service, "manager", lambda *args: manager)
    monkeypatch.setattr(service.Host, "current", classmethod(lambda cls: host))
    monkeypatch.setattr(components, "manifest_from_git", lambda *args: scenario.target)
    monkeypatch.setattr(service, "_install_target", install)
    monkeypatch.setattr(service, "_unit_package_root", lambda host, mgr: host.package_root.resolve())
    monkeypatch.setattr(service, "__file__", str(tmp_path / "looper/agents/service.py"))
    return SimpleNamespace(host=host, events=events, statuses=statuses, state=state, scenario=scenario,
                           root=root, elapsed=elapsed)


def report(deployment):
    return json.loads((deployment.root / "logs/serve.log").read_text().splitlines()[-1])


@pytest.mark.parametrize("name,value", [
    ("PYTHONPATH", "other"), ("GIT_DIR", "other"),
    *((name, "") for name in service._GIT_REPOSITORY_OVERRIDES),
])
@pytest.mark.parametrize("dry_run", [False, True])
def test_upgrade_refuses_shell_import_or_repository_overrides_before_contacting_service(
        deployment, monkeypatch, name, value, dry_run, capsys):
    deployment.host.environ = {name: value}
    deployment.host.run = lambda *args, **kwargs: pytest.fail("must refuse before running git or Python")
    deployment.host.get_components = lambda *args: pytest.fail("must refuse first")
    monkeypatch.setattr(service, "manager", lambda *args: pytest.fail("must refuse before reading the unit"))
    assert cli.main(["service", "upgrade", *(["--dry-run"] if dry_run else [])]) == 1
    assert deployment.events == []
    assert name in capsys.readouterr().err


@pytest.mark.parametrize("name", service._PYTHON_IMPORT_OVERRIDES)
@pytest.mark.parametrize("dry_run", [False, True])
def test_upgrade_allows_empty_shell_python_import_overrides(deployment, name, dry_run, capsys):
    deployment.host.environ = {name: ""}
    assert cli.main(["service", "upgrade", *(["--dry-run"] if dry_run else [])]) == 0
    if dry_run:
        assert "FULL" in capsys.readouterr().out
        assert deployment.events == []
    else:
        assert report(deployment)["status"] == "ok"
        assert deployment.events[-3:] == ["stop", "install", "start"]


@pytest.mark.parametrize("wheel", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_upgrade_refuses_code_from_another_package_root(
        deployment, tmp_path, monkeypatch, wheel, dry_run, capsys):
    deployment.host.package_root = tmp_path / "other-checkout"
    monkeypatch.setattr(service, "_unit_package_root", lambda *args: tmp_path.resolve())
    flags = (["--to", "target.whl"] if wheel else []) + (["--dry-run"] if dry_run else [])
    assert cli.main(["service", "upgrade", *flags]) == 1
    assert deployment.events == []
    error = capsys.readouterr().err
    assert str(deployment.host.package_root) in error and str(tmp_path.resolve()) in error


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("argv", [["/other/bin/looper", "serve"], []])
def test_upgrade_refuses_another_interpreter_before_contacting_service(deployment, dry_run, argv, capsys):
    service.manager(deployment.host, "test").unit_argv = lambda: argv
    deployment.host.get_components = lambda *args: pytest.fail("must refuse before contacting the service")
    assert cli.main(["service", "upgrade", *(["--dry-run"] if dry_run else [])]) == 1
    assert deployment.events == []
    assert "run looper service upgrade with that looper" in capsys.readouterr().err


@pytest.mark.parametrize("dry_run", [False, True])
def test_upgrade_refuses_unreadable_unit(deployment, dry_run, capsys):
    def unreadable():
        raise OSError("cannot read unit")
    service.manager(deployment.host, "test").unit_argv = unreadable
    assert cli.main(["service", "upgrade", *(["--dry-run"] if dry_run else [])]) == 1
    assert deployment.events == []
    assert "cannot read the service unit" in capsys.readouterr().err


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_checkout_upgrade_refuses_unit_pythonpath_before_drain(
        deployment, tmp_path, monkeypatch, system, dry_run, capsys):
    environment = tmp_path / ".venv"
    venv.EnvBuilder().create(environment)
    python = str(environment / "bin/python")
    foreign = tmp_path / "foreign"
    for checkout in (tmp_path, foreign):
        (checkout / "looper").mkdir(parents=True, exist_ok=True)
        (checkout / "looper/__init__.py").write_text("")

    def execute(argv, *, env=None):
        return subprocess.run(argv, cwd=environment / "bin", env=env,
                              capture_output=True, text=True, timeout=10)

    site = Path(execute([python, "-I", "-c",
        "import sysconfig; print(sysconfig.get_path('purelib'))"]).stdout.strip())
    (site / "checkout.pth").write_text(str(tmp_path) + "\n")
    entry = environment / "bin/looper"
    entry.write_text(f"#!{python}\nimport looper; print(looper.__file__)\n")
    entry.chmod(0o755)
    unit_env = {"PYTHONPATH": str(foreign)}
    # The unit's own executable really imports the foreign checkout, while -I sees this one.
    loaded = execute([str(entry)], env=unit_env)
    assert loaded.returncode == 0, loaded.stderr
    assert Path(loaded.stdout.strip()) == foreign / "looper/__init__.py"
    isolated = execute([python, "-I", "-c", "import looper; print(looper.__file__)"])
    assert Path(isolated.stdout.strip()) == tmp_path / "looper/__init__.py"
    deployment.host.system, deployment.host.executable = system, python
    mgr = service.manager(deployment.host, "test")
    if system == "Darwin":
        mgr.unit_path.write_bytes(plistlib.dumps({"EnvironmentVariables": unit_env}))
    else:
        mgr.unit_path.write_text(f'Environment="PYTHONPATH={foreign}"\n')
    monkeypatch.setattr(service, "_unit_package_root", _REAL_UNIT_PACKAGE_ROOT)
    run = deployment.host.run
    deployment.host.run = lambda argv, **kwargs: execute(argv, **kwargs) if argv[0] == python else run(argv)
    assert cli.main(["service", "upgrade", *(["--dry-run"] if dry_run else [])]) == 1
    assert deployment.events == []
    assert deployment.state.running
    assert "PYTHONPATH" in capsys.readouterr().err


@pytest.mark.parametrize("dry_run", [False, True])
def test_wheel_upgrade_refuses_package_outside_install_scheme_before_drain(
        deployment, tmp_path, monkeypatch, dry_run, capsys):
    environment = tmp_path / ".venv"
    venv.EnvBuilder().create(environment)
    deployment.host.executable = str(environment / "bin/python")
    deployment.host.run = lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10)
    monkeypatch.setattr(components, "manifest_from_wheel", lambda *args: deployment.scenario.target)
    assert cli.main(["service", "upgrade", "--to", str(tmp_path / "update.whl"),
                     *(["--dry-run"] if dry_run else [])]) == 1
    assert deployment.events == []
    assert deployment.state.running
    assert "outside the interpreter's installation paths" in capsys.readouterr().err


@pytest.mark.parametrize("dry_run", [False, True])
def test_checkout_upgrade_refuses_foreign_sync_environment_before_drain(
        deployment, tmp_path, monkeypatch, dry_run, capsys):
    environment = tmp_path / ".venv"
    venv.EnvBuilder().create(environment)
    deployment.host.executable = str(environment / "bin/python")
    foreign = tmp_path / "other-venv"
    deployment.host.environ = {**shell_environment(), "UV_PROJECT_ENVIRONMENT": str(foreign)}
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(foreign))
    run = deployment.host.run

    def execute(argv):
        if argv[0] == deployment.host.executable:
            return subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return run(argv)

    deployment.host.run = execute
    assert cli.main(["service", "upgrade", *(["--dry-run"] if dry_run else [])]) == 1
    assert deployment.events == []
    assert deployment.state.running
    assert not foreign.exists()
    error = capsys.readouterr().err
    assert "UV_PROJECT_ENVIRONMENT" in error and str(environment) in error


def test_checkout_upgrade_pins_real_sync_to_preflight_interpreter(deployment, tmp_path, monkeypatch):
    environment, foreign = tmp_path / "service venv", tmp_path / "other-venv"
    for path in (environment, foreign):
        venv.EnvBuilder().create(path)
    python = str(environment / "bin/python")
    wheel = tmp_path / "sync_probe-1.0.0-py3-none-any.whl"
    metadata = "sync_probe-1.0.0.dist-info"
    files = {
        "sync_probe.py": "VALUE = 'installed'\n",
        f"{metadata}/METADATA": "Metadata-Version: 2.1\nName: sync-probe\nVersion: 1.0.0\n",
        f"{metadata}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    files[f"{metadata}/RECORD"] = "".join(f"{name},,\n" for name in (*files, f"{metadata}/RECORD"))
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "sync-fixture"\nversion = "1.0.0"\nrequires-python = ">=3.11"\n'
        f'[project.optional-dependencies]\ntest = ["sync-probe @ {wheel.as_uri()}"]\n')
    deployment.host.executable = python
    deployment.host.environ = {**shell_environment(), "HOME": str(deployment.host.home),
        "UV_PROJECT_ENVIRONMENT": environment.name, "UV_OFFLINE": "1", "UV_NO_CONFIG": "1",
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"), "UV_PYTHON_DOWNLOADS": "never"}
    monkeypatch.setattr(service, "_install_target", _REAL_INSTALLER)
    operation, git = deployment.host.system_operation, deployment.host.run

    def drain(port, token, name, args):
        if name == "drain":
            monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(foreign))
            deployment.host.environ["UV_PROJECT_ENVIRONMENT"] = str(foreign)
        return operation(port, token, name, args)

    def run(argv, *, env=None):
        if argv[0] == "git":
            return git(argv)
        result = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        if argv[:2] == ["uv", "sync"]:
            assert not deployment.state.running
            assert result.returncode == 0, result.stderr
            deployment.scenario.current = deployment.scenario.target
        return result

    deployment.host.system_operation, deployment.host.run = drain, run
    assert cli.main(["service", "upgrade"]) == 0
    assert report(deployment)["status"] == "ok"
    probe = ["-I", "-c", "import sync_probe; print(sync_probe.VALUE)"]
    installed = subprocess.run([python, *probe], capture_output=True, text=True, timeout=10)
    assert installed.returncode == 0 and installed.stdout.strip() == "installed"
    untouched = subprocess.run([str(foreign / "bin/python"), *probe], capture_output=True, text=True, timeout=10)
    assert untouched.returncode != 0 and "ModuleNotFoundError" in untouched.stderr


@pytest.mark.parametrize("wheel", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_checkout_must_be_its_package_root_but_wheel_allows_any_cwd(
        deployment, monkeypatch, wheel, dry_run, capsys):
    deployment.host.package_root = deployment.host.cwd / "other-checkout"
    monkeypatch.setattr(components, "manifest_from_wheel", lambda *args: deployment.scenario.target)
    flags = (["--to", "target.whl"] if wheel else []) + (["--dry-run"] if dry_run else [])
    assert cli.main(["service", "upgrade", *flags]) == (0 if wheel else 1)
    if not wheel:
        assert deployment.events == []
        assert str(deployment.host.package_root) in capsys.readouterr().err


@pytest.mark.parametrize("late_exit", [True, False])
@pytest.mark.parametrize("manager_readable", [True, False])
def test_stop_timeout_recovers_a_late_exit_and_cancels_drain(
        deployment, late_exit, manager_readable, capsys):
    mgr = service.manager(deployment.host, "test")
    stop_deadline = []
    original_stop = mgr.stop

    def stop():
        if stop_deadline:
            original_stop()
            return
        deployment.events.append("stop")
        deployment.elapsed[0] += service._STOP_WAIT_SECONDS
        stop_deadline.append(deployment.elapsed[0])
        raise service.ServiceError("stop deadline passed")

    def state():
        if not manager_readable and "stop" in deployment.events and "start" not in deployment.events:
            raise service.ServiceError("manager unavailable")
        return deployment.state

    def alive(pid):
        living = not late_exit or deployment.elapsed[0] <= stop_deadline[0]
        if not living:
            deployment.state.running = False
            deployment.state.pid = None
        return living

    mgr.stop, mgr.state = stop, state
    deployment.host.pid_alive = alive
    assert cli.main(["service", "upgrade"]) == 1
    assert deployment.events.count("start") == int(late_exit)
    assert "install" not in deployment.events
    assert deployment.events[-1] == "drain_cancel"
    data = report(deployment)
    assert data["status"] == "failed"
    assert ("recovery_error" in data) is not late_exit
    if not late_exit:
        assert "old service process is still running" in data["recovery_error"]
    assert "serving: yes" in capsys.readouterr().err


@pytest.mark.parametrize("renew_error", [None, service.ServiceError, OSError])
def test_stop_wait_renews_drain_best_effort(deployment, renew_error):
    deployment.statuses[:] = [{"phase": "drained"}]
    deployment.host.pid_alive = lambda pid: deployment.elapsed[0] < 2 * service._POLL_SECONDS
    operation = deployment.host.system_operation

    def renew(port, token, name, args):
        result = operation(port, token, name, args)
        if name == "drain_status" and renew_error is not None:
            raise renew_error("old service no longer answers")
        return result

    deployment.host.system_operation = renew
    assert cli.main(["service", "upgrade"]) == 0
    assert deployment.events.count("drain_status") == 2


@pytest.mark.parametrize("live_pid", [False, True])
def test_failed_start_stops_the_unit_before_retry_even_without_a_live_pid(deployment, live_pid):
    mgr = service.manager(deployment.host, "test")
    start = mgr.start

    def fail_once():
        if deployment.events.count("start") == 0:
            deployment.events.append("start")
            deployment.state.running = live_pid
            deployment.state.pid = 123456790 if live_pid else None
            raise service.ServiceError("first start failed")
        start()

    mgr.start = fail_once
    deployment.host.pid_alive = lambda pid: deployment.state.pid == pid
    assert cli.main(["service", "upgrade"]) == 1
    assert deployment.events.count("start") == 2
    assert deployment.events.count("stop") == 2
    assert report(deployment)["serving"] is True


@pytest.mark.parametrize("deadline", [False, True])
def test_full_install_only_after_drain_and_old_process_exit(deployment, deadline, capsys):
    if deadline:
        deployment.statuses[:] = [{"phase": "draining", "deadline_passed": True, "waiting": {"runs": [
            {"worker_id": "worker-at-deadline", "role": "reviewer"}]}}]
    assert cli.main(["service", "upgrade", "--to", "candidate"]) == 0
    assert deployment.events[-3:] == ["stop", "install", "start"]
    data = report(deployment)
    assert data["status"] == "ok"
    assert data["plan"] == "FULL"
    assert data["installed_package_version"] == "1.0.1"
    assert data["interrupted"] == ([{"worker_id": "worker-at-deadline", "role": "reviewer", "reason": "deadline"}]
                                     if deadline else [])
    assert json.dumps(data, sort_keys=True) in capsys.readouterr().out


def test_no_force_cancels_deadline_without_stopping_or_installing(deployment, capsys):
    deployment.statuses[:] = [{"phase": "draining", "deadline_passed": True}]
    assert cli.main(["service", "upgrade", "--no-force"]) == 1
    assert deployment.events == ["drain", "drain_cancel"]
    assert deployment.state.running
    assert report(deployment)["status"] == "failed"
    assert "--no-force" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["stop_error", "stop_then_error", "install_error", "process_lingers"])
def test_failure_before_restart_cancels_drain_and_recovers_serving(deployment, failure):
    setattr(deployment.scenario, failure, True)
    assert cli.main(["service", "upgrade"]) == 1
    assert deployment.events[-1] == "drain_cancel"
    assert "install" not in deployment.events or failure == "install_error"
    assert deployment.state.running or failure == "process_lingers"
    assert report(deployment)["status"] == "failed"


def test_lost_drain_response_also_attempts_cancel(deployment):
    def operation(port, token, name, args):
        deployment.events.append(name)
        if name == "drain":
            raise service.ServiceError("lost response")
        return {"phase": "serving"}
    deployment.host.system_operation = operation
    assert cli.main(["service", "upgrade"]) == 1
    assert deployment.events == ["drain", "drain_cancel"]


def test_restart_drain_has_no_install_step(deployment):
    assert cli.main(["service", "restart", "--drain", "--drain-timeout", "5"]) == 0
    assert deployment.events == ["drain", "drain_status", "stop", "start"]
    assert report(deployment)["installed_package_version"] == "1.0.0"


def test_control_plane_retains_turns_drain_branch(deployment, monkeypatch):
    def operation(port, token, name, args):
        assert name == "drain"
        assert args == {"scope": "turns", "timeout_seconds": 180}
        return {"phase": "drained"}
    deployment.host.system_operation = operation
    plan = UpgradePlan(UpgradeKind.CONTROL_PLANE, ("compatible keepers",), ("control_plane",))
    mgr = service.manager(deployment.host, "test")
    result = service._execute_plan(deployment.host, mgr, port=18841, root=deployment.root,
        log_path=deployment.root / "logs/serve.log", plan=plan, running=manifest(), target=None,
        installer=None, timeout=120, drain_timeout=180, no_force=False)
    assert "CONTROL_PLANE" in result
    assert deployment.events == ["stop", "start"]


def test_full_verifies_components_after_restart(deployment):
    deployment.scenario.verify_error = True
    assert cli.main(["service", "upgrade"]) == 1
    assert "does not match the target" in report(deployment)["error"]


def test_atomic_dashboard_publication_replaces_nonempty_directory(tmp_path):
    old, staged = tmp_path / "dist", tmp_path / "staged"
    old.mkdir()
    staged.mkdir()
    (old / "index.html").write_text("old")
    (old / "old.js").write_text("old asset")
    (staged / "index.html").write_text("new")
    service._swap_directory(staged, old)
    assert (old / "index.html").read_text() == "new"
    assert not (old / "old.js").exists()
    assert (staged / "index.html").read_text() == "old"


@pytest.mark.parametrize("pip_available", [True, False])
def test_wheel_installer_targets_current_interpreter(deployment, pip_available):
    commands = []

    def run(argv, **kwargs):
        if "-c" in argv:
            return subprocess.CompletedProcess(argv, 0, wheel_paths(deployment.host), "")
        commands.append(list(argv))
        return subprocess.CompletedProcess(argv, 0 if pip_available or argv[-1] != "--version" else 1, "", "")

    deployment.host.run = run
    # The production installer is replaced by the ordering fixture, so restore
    # the original from a saved reference below for this command-boundary test.
    _REAL_INSTALLER(deployment.host, "/candidate.whl", wheel=True, build_web=False, dependencies=True)
    prefix = str(deployment.host.cwd / ".venv")
    expected = (["python", "-I", "-m", "pip", "install", "--force-reinstall"] if pip_available else
                ["uv", "pip", "install", "--python", "python", "--reinstall"])
    assert commands == [["python", "-I", "-m", "pip", "--version"],
                        [*expected, "--prefix", prefix, "/candidate.whl"]]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_drain_deadline_refused_without_any_mutation(deployment, value):
    assert cli.main(["service", "upgrade", "--drain-timeout", value]) == 1
    assert deployment.events == []


def test_web_verifies_served_index_and_asset_fingerprint(deployment):
    target = manifest("web", "1.0.1")
    deployment.scenario.current = target
    deployment.host.get_index = lambda port, token: b"installed page"
    plan = UpgradePlan(UpgradeKind.WEB_ONLY, ("web changed",), ("web",))
    with pytest.raises(service.ServiceError, match="does not match the installed build"):
        service._execute_plan(deployment.host, service.manager(deployment.host, "test"),
            port=18841, root=deployment.root, log_path=deployment.root / "logs/serve.log", plan=plan,
            running=manifest(), target=target, installer=lambda: (b"installed page", "0" * 64),
            timeout=120, drain_timeout=1200, no_force=False)
    assert deployment.events == []


def test_full_refuses_an_http_service_not_owned_by_the_service_manager(deployment):
    # A manual serve with the same runtime root can answer HTTP while the
    # installed service unit is stopped. Its PID cannot be confirmed exited.
    deployment.state.running = False
    deployment.state.pid = None
    assert cli.main(["service", "upgrade"]) == 1
    assert deployment.events == []


@pytest.mark.parametrize("command", [["upgrade"], ["restart", "--drain"]])
@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGTERM])
def test_terminated_cli_cancels_drain_and_restores_signal_handlers(deployment, tmp_path, command, sig):
    outcome = tmp_path / "termination.json"

    def run():
        previous = {s: signal.getsignal(s) for s in (signal.SIGHUP, signal.SIGTERM)}
        deployment.host.sleep = lambda seconds: os.kill(os.getpid(), sig)
        try:
            cli.main(["service", *command])
        finally:
            outcome.write_text(json.dumps({"events": deployment.events,
                "restored": all(signal.getsignal(s) == handler for s, handler in previous.items())}))

    process = multiprocessing.get_context("fork").Process(target=run)
    process.start()
    try:
        process.join(5)
        assert not process.is_alive()
        assert process.exitcode == 128 + sig
        data = json.loads(outcome.read_text())
        assert data == {"events": ["drain", "drain_cancel"], "restored": True}
        assert report(deployment)["status"] == "failed"
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)
        process.close()


@pytest.mark.parametrize("build_web", [True, False], ids=["web_only", "noop"])
def test_wheel_dashboard_is_staged_and_published_atomically_while_http_serves(
        deployment, tmp_path, monkeypatch, build_web):
    from tests.test_api_v1_auth import TOKEN, _send_raw, _serving

    wheel = tmp_path / "dashboard-update.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("looper/web/dashboard/index.html", "new dashboard")
        archive.writestr("looper/web/dashboard/assets/app.js", "new script")
        archive.writestr("looper/__init__.py", "target Python code")
    static = tmp_path / "dashboard"
    static.mkdir()
    (static / "assets").mkdir()
    (static / "assets/app.js").write_text("old script")
    monkeypatch.setenv("PYTEST_LOOPER_STATIC_DIR", str(static))
    installs = []

    def install(argv, **kwargs):
        if "-c" in argv:
            return subprocess.CompletedProcess(argv, 0, wheel_paths(deployment.host), "")
        if "install" in argv:
            assert "--no-deps" in argv
            with zipfile.ZipFile(argv[-1]) as archive:
                assert archive.read("looper/__init__.py") == b"target Python code"
                assert not any(name.startswith("looper/web/dashboard/") for name in archive.namelist())
            installs.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    deployment.host.run = install
    swap = service._swap_directory
    publications = []

    async def run():
        async with _serving(static, listen=True) as server:
            loop = asyncio.get_running_loop()

            def read(path):
                return asyncio.run_coroutine_threadsafe(_send_raw(server, "GET " + path,
                    {"Cookie": f"looper_operator_{server._port}={TOKEN}"}), loop).result(2)

            def publish(staged, destination):
                assert destination == static
                assert read("/")[2] == "<html>dashboard</html>"
                assert read("/assets/app.js")[2] == "old script"
                assert (staged / "index.html").read_text() == "new dashboard"
                assert (staged / "assets/app.js").read_text() == "new script"
                swap(staged, destination)
                assert read("/")[2] == "new dashboard"
                assert read("/assets/app.js")[2] == "new script"
                publications.append(destination)

            monkeypatch.setattr(service, "_swap_directory", publish)
            expected = await asyncio.to_thread(_REAL_INSTALLER, deployment.host, str(wheel),
                wheel=True, build_web=build_web, dependencies=False)
            assert expected == (b"new dashboard", components.web_fingerprints(static)[1])

    asyncio.run(asyncio.wait_for(run(), 10))
    assert publications == [static]
    assert len(installs) == 1
    assert not list(tmp_path.glob(".dist-upgrade-*"))


def test_cli_repeated_wheel_upgrade_preserves_dashboard_without_dependency_reinstall(deployment, tmp_path, monkeypatch):
    static = tmp_path / "looper/web/dashboard"
    static.mkdir(parents=True)
    files = {"index.html": b"dashboard", "assets/app.js": b"script"}
    wheel = tmp_path / "same-version.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            path = static / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            archive.writestr("looper/web/dashboard/" + name, data)
    monkeypatch.setenv("PYTEST_LOOPER_STATIC_DIR", str(static))
    monkeypatch.setattr(components, "manifest_from_wheel", lambda path: deployment.scenario.current)
    monkeypatch.setattr(service, "_install_target", _REAL_INSTALLER)
    installs = []

    def install(argv, **kwargs):
        if "-c" in argv:
            return subprocess.CompletedProcess(argv, 0, wheel_paths(deployment.host), "")
        if "install" in argv:
            assert "--no-deps" in argv
            with zipfile.ZipFile(argv[-1]) as archive:
                assert not any(name.startswith("looper/web/dashboard/") for name in archive.namelist())
            installs.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    deployment.host.run = install

    for _ in range(2):
        assert cli.main(["service", "upgrade", "--to", str(wheel)]) == 0
        assert report(deployment)["plan"] == "NOOP"
        assert report(deployment)["status"] == "ok"
        assert {p.relative_to(static).as_posix(): p.read_bytes()
                for p in static.rglob("*") if p.is_file()} == files
    assert deployment.events == []
    assert len(installs) == 2
    assert not list(tmp_path.glob(".dist-upgrade-*"))


@pytest.mark.parametrize("pip_available", [True, False], ids=["pip", "uv"])
@pytest.mark.parametrize("change", [None, "web", "control_plane"], ids=["noop", "web_only", "full"])
def test_wheel_upgrade_installs_cli_code_and_metadata_in_service_environment(
        deployment, tmp_path, monkeypatch, pip_available, change):
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = str(environment / "bin/python")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))

    def execute(argv, *, env=None):
        return subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=30,
                              env=env if env is not None else
                              {k: v for k, v in os.environ.items() if k != "PYTHONPATH"})

    wheels = []
    for version, marker in (("1.0.0", "old"), ("1.0.1", "fixed")):
        wheel = tmp_path / f"looper-{version}-py3-none-any.whl"
        metadata = f"looper-{version}.dist-info"
        files = {
            "looper/__init__.py": f"__version__ = {version!r}\n",
            "looper/cli.py": ("from . import __version__\nfrom .agents.service import MARKER\n"
                              f"def main(): print(__version__, {marker!r}, MARKER)\n"),
            "looper/agents/__init__.py": "",
            "looper/agents/service.py": f"MARKER = {marker!r}\n",
            "looper/web/dashboard/index.html": "complete dashboard",
            "looper/web/dashboard/assets/app.js": "complete script",
            f"{metadata}/METADATA": f"Metadata-Version: 2.1\nName: looper\nVersion: {version}\n",
            f"{metadata}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"{metadata}/entry_points.txt": "[console_scripts]\nlooper = looper.cli:main\n",
        }
        files[f"{metadata}/RECORD"] = "".join(f"{name},,\n" for name in (*files, f"{metadata}/RECORD"))
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        wheels.append(wheel)
    installed = execute([python, "-m", "pip", "install", "--no-deps", str(wheels[0])])
    assert installed.returncode == 0, installed.stderr
    site = Path(execute([python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).stdout.strip())
    static = site / "looper/web/dashboard"
    original = {p.relative_to(static).as_posix(): p.read_bytes() for p in static.rglob("*") if p.is_file()}
    monkeypatch.setenv("PYTEST_LOOPER_STATIC_DIR", str(static))
    monkeypatch.setattr(service, "__file__", str(site / "looper/agents/service.py"))
    monkeypatch.setattr(service, "_install_target", _REAL_INSTALLER)
    deployment.host.executable = python
    deployment.host.package_root = site
    deployment.scenario.target = manifest(change, version="1.0.1")
    monkeypatch.setattr(components, "manifest_from_wheel", lambda path: deployment.scenario.target)
    installations = []
    restarts = change == "control_plane"

    def run(argv, *, env=None):
        assert {p.relative_to(static).as_posix(): p.read_bytes()
                for p in static.rglob("*") if p.is_file()} == original
        if argv[-1] == "--version" and not pip_available:
            return subprocess.CompletedProcess(argv, 1, "", "pip not installed")
        if "install" in argv:
            with zipfile.ZipFile(argv[-1]) as archive:
                assert "looper/cli.py" in archive.namelist()
                assert "looper/agents/service.py" in archive.namelist()
                if "--no-deps" in argv:
                    assert not any(name.startswith("looper/web/dashboard/") for name in archive.namelist())
            if "--no-deps" in argv:
                assert all("looper/web/dashboard/" not in p.read_text()
                           for p in site.glob("looper-*.dist-info/RECORD"))
            else:
                assert not deployment.state.running
            installations.append(list(argv))
        result = execute(argv, env=env)
        assert {p.relative_to(static).as_posix(): p.read_bytes()
                for p in static.rglob("*") if p.is_file()} == original
        return result

    deployment.host.run = run
    before = execute([str(environment / "bin/looper")])
    assert before.stdout.strip() == "1.0.0 old old"
    redirected = tmp_path / "redirected"
    pip_config = tmp_path / "pip.conf"
    pip_config.write_text(f"[install]\ntarget = {redirected / 'config'}\n")
    if change is None:
        monkeypatch.setenv("PIP_TARGET", str(redirected / "pip-target"))
    monkeypatch.setenv("PIP_CONFIG_FILE", str(pip_config))
    # A venv-local config must also be ignored, independently of PIP_CONFIG_FILE.
    (environment / "pip.conf").write_text(f"[install]\ntarget = {redirected / 'site-config'}\n")
    deployment.host.environ = shell_environment()
    operation = deployment.host.system_operation

    def drain(port, token, name, args):
        if name == "drain":
            # The installation must use the environment validated before draining.
            deployment.host.environ.update(PIP_TARGET=str(redirected / "after-drain"),
                                           PIP_PREFIX=str(redirected / "after-drain-prefix"))
            monkeypatch.setenv("PIP_TARGET", str(redirected / "after-drain"))
        return operation(port, token, name, args)

    deployment.host.system_operation = drain
    deployment.host.get_index = lambda *args: (static / "index.html").read_bytes()
    deployment.host.get_whoami = lambda *args: {
        "looper_version": "1.0.1" if restarts and installations else "1.0.0"}
    deployment.host.get_components = lambda *args: components.Manifest(
        "1.0.1" if restarts and installations else "1.0.0", "startup",
        (deployment.scenario.target if installations else deployment.scenario.current).components,
        deployment.scenario.current.adoption, {"web": components.web_fingerprints(static)[1]}).to_dict()
    for attempt in range(2):
        assert cli.main(["service", "upgrade", "--to", str(wheels[1])]) == 0
        data = report(deployment)
        assert data["plan"] == (("FULL" if restarts else "WEB_ONLY") if change and attempt == 0 else "NOOP")
        assert data["status"] == "ok"
        assert data["installed_package_version"] == "1.0.1"
        assert data["running_version"] == ("1.0.1" if restarts else "1.0.0")
        assert execute([str(environment / "bin/looper")]).stdout.strip() == "1.0.1 fixed fixed"
        metadata = execute([python, "-c", "from importlib.metadata import version; print(version('looper'))"])
        assert metadata.stdout.strip() == "1.0.1"
        assert not (site / "looper-1.0.0.dist-info").exists()
        record = (site / "looper-1.0.1.dist-info/RECORD").read_text()
        assert all("looper/web/dashboard/" + name + "," in record for name in original)
    assert len(installations) == 2
    assert ["--no-deps" in argv for argv in installations] == [not restarts, True]
    assert deployment.events == (["drain", "drain_status", "stop", "start"] if restarts else [])
    assert not redirected.exists()
    assert not list(static.parent.glob(".*-upgrade-*"))


@pytest.mark.parametrize("failure", ["missing_index", "unsafe_path", "install", "swap"])
@pytest.mark.parametrize("build_web", [True, False], ids=["web_only", "noop"])
def test_failed_wheel_dashboard_publication_preserves_all_current_files(
        deployment, tmp_path, monkeypatch, failure, build_web):
    static = tmp_path / "dashboard"
    static.mkdir()
    original = {"index.html": b"old dashboard", "app.js": b"old script"}
    for name, data in original.items():
        (static / name).write_bytes(data)
    monkeypatch.setenv("PYTEST_LOOPER_STATIC_DIR", str(static))
    old_record = tmp_path / "looper-1.0.0.dist-info/RECORD"
    if failure == "install":
        old_record.parent.mkdir()
        old_record.write_text("looper/web/dashboard/index.html,,\nlooper/cli.py,,\n")
        original_record = old_record.read_bytes()
    wheel = tmp_path / "dashboard-update.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("looper/web/dashboard/app.js", "new script")
        if failure != "missing_index":
            archive.writestr("looper/web/dashboard/index.html", "new dashboard")
        if failure == "unsafe_path":
            archive.writestr("looper/web/dashboard/../escaped.js", "unsafe")
    if failure == "swap":
        def fail(*args):
            raise service.ServiceError("atomic exchange refused")
        monkeypatch.setattr(service, "_swap_directory", fail)
    def install(argv, **kwargs):
        if "-c" in argv:
            return subprocess.CompletedProcess(argv, 0, wheel_paths(deployment.host), "")
        assert failure in ("install", "swap"), "invalid dashboard must fail before package installation"
        assert {p.name: p.read_bytes() for p in static.iterdir()} == original
        if "install" in argv:
            assert "--no-deps" in argv
            if failure == "install":
                assert "dashboard" not in old_record.read_text()
                return subprocess.CompletedProcess(argv, 1, "", "installation failed")
        return subprocess.CompletedProcess(argv, 0, "", "")

    deployment.host.run = install
    with pytest.raises(service.ServiceError):
        _REAL_INSTALLER(deployment.host, str(wheel), wheel=True, build_web=build_web, dependencies=False)
    assert {p.name: p.read_bytes() for p in static.iterdir()} == original
    if failure == "install":
        assert old_record.read_bytes() == original_record
    assert not (tmp_path / "escaped.js").exists()
    assert not list(tmp_path.glob(".dist-upgrade-*"))
    assert not list(tmp_path.glob(".wheel-upgrade-*"))


@pytest.fixture
def checkout_install(deployment, tmp_path):
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "src/app.ts").write_text("dashboard source")
    config = tmp_path / "looper/components.toml"
    config.parent.mkdir()
    config.write_bytes(components.CONFIG.read_bytes())
    commands = []

    def run(argv, **kwargs):
        commands.append(list(argv))
        if argv[:3] == [deployment.host.executable, "-I", "-c"]:
            return subprocess.CompletedProcess(argv, 0, str(tmp_path / ".venv"), "")
        if "--outDir" in argv:
            staged = Path(argv[-1])
            (staged / "assets").mkdir()
            (staged / "index.html").write_text("new dashboard")
            (staged / "assets/app.js").write_text("script")
        return subprocess.CompletedProcess(argv, 0, "", "")

    deployment.host.run = run
    return SimpleNamespace(host=deployment.host, web=web, commands=commands)


@pytest.mark.parametrize("build_web", [False, True], ids=["noop", "web_only"])
def test_checkout_live_upgrade_does_not_sync_dependencies(checkout_install, build_web):
    _REAL_INSTALLER(checkout_install.host, "candidate", wheel=False, build_web=build_web, dependencies=False)
    assert checkout_install.commands[0][:4] == ["git", "-C", str(checkout_install.host.cwd), "merge"]
    assert not any("sync" in argv for argv in checkout_install.commands)
    assert (checkout_install.web / "dist/index.html").exists() == build_web


def test_full_checkout_sync_retains_test_extra_and_other_installed_packages(checkout_install):
    _REAL_INSTALLER(checkout_install.host, "candidate", wheel=False, build_web=False, dependencies=True)
    sync = next(argv for argv in checkout_install.commands if argv[:2] == ["uv", "sync"])
    assert sync[sync.index("--extra") + 1] == "test"
    assert "--inexact" in sync


def test_checkout_dashboard_build_uses_service_python(checkout_install):
    checkout_install.host.executable = "/service venv/bin/python"
    _REAL_INSTALLER(checkout_install.host, "candidate", wheel=False, build_web=True, dependencies=False)
    build = next(argv for argv in checkout_install.commands if "--outDir" in argv)
    assert build[:3] == ["env", "PYTHON=/service venv/bin/python", "npm"]


@pytest.mark.parametrize("path", ["checkout", "wheel"])
def test_upgrade_tools_run_without_python_or_virtualenv_overrides(
        deployment, checkout_install, tmp_path, path):
    host = deployment.host
    host.environ = {"PATH": "/usr/bin", "HOME": str(tmp_path), "PYTHONSAFEPATH": "1", "PYTHONSTARTUP": "x",
                    "VIRTUAL_ENV": str(tmp_path / "other-venv"), "HTTPS_PROXY": "http://proxy.example:3128",
                    "UV_INDEX_URL": "https://mirror.example/simple", "npm_config_registry": "https://mirror.example/npm"}
    if path == "checkout":
        environments = []
        run = host.run

        def record(argv, **kwargs):
            if argv[0] != "git":
                environments.append(kwargs.get("env"))
            return run(argv, **kwargs)

        host.run = record
        _REAL_INSTALLER(host, "candidate", wheel=False, build_web=True, dependencies=True,
                        sync_environment=tmp_path / ".venv")
        assert len(environments) == 3  # uv sync, npm ci and npm run build
        assert environments[0]["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / ".venv")
    else:
        host.environ["PIP_TARGET"] = "/x"
        host.run = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, wheel_paths(host), "")
        environment = service._wheel_install_environment(host, host.package_root)
        assert "PIP_TARGET" not in environment
        assert environment["PIP_CONFIG_FILE"] == os.devnull
        assert environment["PIP_PREFIX"] == str(tmp_path / ".venv")
        assert environment["UV_NO_CONFIG"] == "true"
        environments = [environment]
    for environment in environments:
        assert environment is not None
        assert not any(name.startswith("PYTHON") for name in environment)
        assert "VIRTUAL_ENV" not in environment
        for name in ("PATH", "HOME", "HTTPS_PROXY", "UV_INDEX_URL", "npm_config_registry"):
            assert environment[name] == host.environ[name]


@pytest.mark.parametrize("existing", [False, True])
def test_dashboard_publication_sets_readable_directory_and_file_modes(tmp_path, existing):
    import stat
    staged, destination = tmp_path / "staged", tmp_path / "dist"
    staged.mkdir(mode=0o700)
    (staged / "assets").mkdir(mode=0o700)
    (staged / "assets/app.js").write_text("script")
    (staged / "assets/app.js").chmod(0o600)
    if existing:
        destination.mkdir(mode=0o755)
    service._swap_directory(staged, destination)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755
    assert stat.S_IMODE((destination / "assets").stat().st_mode) == 0o755
    assert stat.S_IMODE((destination / "assets/app.js").stat().st_mode) == 0o644


@pytest.mark.parametrize("recovery_serves", [True, False])
def test_failed_start_retries_new_install_and_reports_serving(deployment, recovery_serves, capsys):
    log = deployment.root / "logs/serve.log"
    log.parent.mkdir(parents=True)
    log.write_text("startup output\n")
    deployment.host.get_status = lambda *args: (
        {"status": "ok"} if recovery_serves and deployment.events.count("start") == 2 else None)
    assert cli.main(["service", "upgrade", "--timeout", "1"]) == 1
    assert deployment.events.count("start") == 2
    assert deployment.events.count("install") == 1
    assert deployment.scenario.current is deployment.scenario.target
    data = report(deployment)
    assert data["status"] == "failed"
    assert data["serving"] is recovery_serves
    assert ("recovery_error" in data) is not recovery_serves
    error_line = capsys.readouterr().err.splitlines()[0]
    assert f"serving: {'yes' if recovery_serves else 'no'}" in error_line
    if recovery_serves:
        assert data["running_code_fingerprint"] == deployment.scenario.target.components["control_plane"]
    else:
        assert data["running_code_fingerprint"] is None
        assert data["running_code_fingerprint_error"]
        assert data["restart_hint"] == "looper service restart"
        assert data["restart_hint"] in error_line


def test_serving_failure_reports_unknown_fingerprint_when_readback_fails(deployment):
    reads = []

    def components_readback(*args):
        reads.append(True)
        if len(reads) == 1:
            return deployment.scenario.current.to_dict()
        raise service.ServiceError("components unavailable")

    deployment.scenario.install_error = True
    deployment.host.get_components = components_readback
    assert cli.main(["service", "upgrade"]) == 1
    data = report(deployment)
    assert data["serving"] is True
    assert data["running_code_fingerprint"] is None
    assert data["running_code_fingerprint_error"] == "components unavailable"


@pytest.mark.parametrize("kind", [UpgradeKind.CONTROL_PLANE, UpgradeKind.FULL])
@pytest.mark.parametrize("turns_timeout", [None, "7"])
def test_upgrade_uses_scope_specific_drain_deadline(deployment, monkeypatch, kind, turns_timeout):
    import looper.upgrade
    monkeypatch.setattr(looper.upgrade, "decide", lambda *args, **kwargs: UpgradePlan(kind, (), ("control_plane",)))
    requests = []
    operation = deployment.host.system_operation

    def record_operation(port, token, name, args):
        if name == "drain":
            requests.append(args)
        return operation(port, token, name, args)

    deployment.host.system_operation = record_operation
    flags = [] if turns_timeout is None else ["--turns-timeout", turns_timeout]
    assert cli.main(["service", "upgrade", "--drain-timeout", "11", *flags]) == 0
    assert requests == [{"scope": "turns" if kind == UpgradeKind.CONTROL_PLANE else "runs",
                         "timeout_seconds": (float(turns_timeout or 180) if kind == UpgradeKind.CONTROL_PLANE else 11)}]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_turns_deadline_refused_before_mutation(deployment, value):
    assert cli.main(["service", "upgrade", "--turns-timeout", value]) == 1
    assert deployment.events == []


@pytest.mark.parametrize("path,blocked,symlink", [
    ("looper/extra module.py", True, False), ("web/src/extra.ts", True, False), ("runners/extra.py", True, False),
    ("web/dist/extra.js", True, False), ("docs/notes.md", False, False), ("looper/ignored.py", False, False),
    ("uv.lock", False, False), ("looper/__pycache__/cache.pyc", False, False), ("looper/extra.pyc", False, False),
    ("web/src/node_modules/extra.ts", False, False), ("web/src/extra.pyc", True, False),
    ("looper/extra.pyc", True, True),
])
def test_checkout_untracked_fingerprinted_files_refused_before_drain(
        deployment, tmp_path, path, blocked, symlink, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=10)
    (tmp_path / ".gitignore").write_text("looper/ignored.py\n")
    source = tmp_path / path
    source.parent.mkdir(parents=True, exist_ok=True)
    if symlink:
        source.symlink_to("target.py")
    else:
        source.write_text("untracked source")

    probe = deployment.host.run

    def run(argv):
        if argv[0] == deployment.host.executable:
            return probe(argv)
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, "1" * 40, "")
        return subprocess.run(argv, capture_output=True, text=True, timeout=10)

    deployment.host.run = run
    assert cli.main(["service", "upgrade"]) == (1 if blocked else 0)
    if blocked:
        assert deployment.events == []
        assert path in capsys.readouterr().err
    else:
        assert "install" in deployment.events
