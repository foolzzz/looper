"""Planned deployments exercise CLI ordering, failure recovery and their reports."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from looper import cli, components
from looper.agents import operator_auth, service
from looper.upgrade import UpgradeKind, UpgradePlan

_REAL_INSTALLER = service._install_target


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
    state = SimpleNamespace(running=True, pid=123456789)
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

    def run(argv):
        assert argv[:3] == ["git", "-C", str(tmp_path)]
        return subprocess.CompletedProcess(argv, 0, "" if "status" in argv else "1" * 40, "")

    host = service.Host(system="Linux", home=tmp_path / "home", uid=1, environ={},
        executable="python", cwd=tmp_path, run=run, clock=lambda: elapsed[0],
        sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        get_components=lambda port, token: (current if scenario.verify_error else scenario.current).to_dict(),
        system_operation=operation, get_status=lambda *args: {"status": "ok"},
        get_whoami=lambda *args: {"looper_version": scenario.current.version},
        port_in_use=lambda port: False, pid_alive=lambda pid: scenario.process_lingers)
    manager = SimpleNamespace(host=host, state=lambda: state, unit_path=unit,
        unit_settings=lambda **kwargs: (18841, root / "logs/serve.log", root), start=start, stop=stop)
    monkeypatch.setattr(service, "manager", lambda *args: manager)
    monkeypatch.setattr(service.Host, "current", classmethod(lambda cls: host))
    monkeypatch.setattr(components, "manifest_from_git", lambda *args: scenario.target)
    monkeypatch.setattr(service, "_install_target", install)
    return SimpleNamespace(host=host, events=events, statuses=statuses, state=state, scenario=scenario,
                           root=root, elapsed=elapsed)


def report(deployment):
    return json.loads((deployment.root / "logs/serve.log").read_text().splitlines()[-1])


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
@pytest.mark.parametrize("dependencies", [True, False])
def test_wheel_installer_targets_current_interpreter(deployment, pip_available, dependencies):
    commands = []

    def run(argv):
        commands.append(list(argv))
        return subprocess.CompletedProcess(argv, 0 if pip_available or argv[-1] != "--version" else 1, "", "")

    deployment.host.run = run
    # The production installer is replaced by the ordering fixture, so restore
    # the original from a saved reference below for this command-boundary test.
    _REAL_INSTALLER(deployment.host, "/candidate.whl", wheel=True, build_web=False, dependencies=dependencies)
    expected = (["python", "-m", "pip", "install", "--force-reinstall", "/candidate.whl"] if pip_available else
                ["uv", "pip", "install", "--python", "python", "--reinstall", "/candidate.whl"])
    if not dependencies:
        expected.insert(-1, "--no-deps")
    assert commands == [["python", "-m", "pip", "--version"], expected]


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
