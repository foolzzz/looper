"""Pi and DSH profiles use their SDK runners unless the ops file sets cli."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess

import pytest

from looper import ops_config as ops
from looper.agents.runtimes import ADAPTERS, ApiProtocol
from looper.agents.runtimes import dsh as dsh_runtime
from looper.agents.runtimes import dsh_runner, pi_runner
from looper.agents.runtimes.dsh import DshRuntime
from looper.agents.runtimes.dsh_runner import DshRunnerRuntime
from looper.agents.runtimes.pi import PiRuntime
from looper.agents.runtimes.pi_runner import PiRunnerRuntime
from looper.agents.runtimes import RuntimeUnsupported
from looper.agents.runtimes.api.adapter import KEY_ENV
from looper.agents.runtimes.channel import RunStartError
from looper.agents.runtimes.runner.bridge import RpcFailure, RunnerBridge
from looper.agents.runtimes.runner.channel import RunnerChannel
from looper.agents.runtimes.runner.protocol import ErrorCode, ProtocolError
from looper.agents.runtimes.runner.session import Descriptor, RunnerSession
from looper.agents.runtimes.runner.supervisor import RunnerUnavailable


def _load(tmp_path, **environ):
    return ops.load(ops.Flags(runtime_root=tmp_path), require_database=False,
                    environ=environ, dotenv=tmp_path / "absent")


def test_defaults_resolve_to_runner_and_ignore_environment(tmp_path):
    config = _load(tmp_path, LOOPER_PI_RUNTIME="cli", LOOPX_PI_RUNTIME="cli",
                   LOOPER_DSH_RUNTIME="cli", LOOPX_DSH_RUNTIME="cli")
    assert config.pi_runtime == "runner" and config.dsh_runtime == "runner"
    assert "pi_runtime" not in config.sources and "dsh_runtime" not in config.sources
    rendered = ops.render(config)
    assert "The setting is read once at startup" in rendered
    assert 'Set "cli" to opt out' in rendered
    assert '# pi = "runner"' in rendered and '# dsh = "runner"' in rendered
    assert 'pi = "cli"' not in rendered and 'dsh = "cli"' not in rendered
    ops.write_private(config.path, rendered)
    loaded = _load(tmp_path)
    assert loaded.pi_runtime == "runner" and loaded.dsh_runtime == "runner"
    assert type(ADAPTERS["pi"]) is PiRuntime
    assert type(ADAPTERS["dsh"]) is DshRuntime


def test_explicit_cli_keeps_the_cli_adapters(tmp_path):
    config = ops.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops.FILE,
                           pi_runtime="cli", dsh_runtime="cli")
    rendered = ops.render(config)
    assert 'pi = "cli"' in rendered and 'dsh = "cli"' in rendered
    assert "The setting is read once at startup" in rendered
    ops.write_private(config.path, rendered)
    loaded = _load(tmp_path)
    assert loaded.pi_runtime == "cli" and loaded.dsh_runtime == "cli"
    assert type(ADAPTERS["pi"]) is PiRuntime and not ADAPTERS["pi"].hosts_runs
    assert type(ADAPTERS["dsh"]) is DshRuntime and not ADAPTERS["dsh"].hosts_runs


def test_claude_code_is_untouched_by_either_setting(tmp_path, monkeypatch):
    claude = ADAPTERS["claude-code"]
    monkeypatch.setitem(ADAPTERS, "pi", ADAPTERS["pi"])
    monkeypatch.setitem(ADAPTERS, "dsh", ADAPTERS["dsh"])
    assert _load(tmp_path).pi_runtime == "runner"
    monkeypatch.setattr(pi_runner.shutil, "which", lambda name: None)
    monkeypatch.setattr(dsh_runner.shutil, "which", lambda name: None)
    assert pi_runner.enable(tmp_path) is None
    assert dsh_runner.enable(tmp_path) is None
    assert ADAPTERS["claude-code"] is claude
    assert type(ADAPTERS["pi"]) is PiRunnerRuntime
    assert type(ADAPTERS["dsh"]) is DshRunnerRuntime


def _package(tmp_path, name, version):
    root = tmp_path / "pkg"
    (root / "bin").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": name, "version": version}))
    executable = root / "bin" / "tool"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    return str(executable)


def _refuse(monkeypatch, module, *, node, executable, version="v22.19.0"):
    monkeypatch.setattr(module.shutil, "which", lambda name: node if name == "node" else executable)

    def run(command, **kwargs):
        assert kwargs["timeout"] == 2
        if version == "timeout":
            raise subprocess.TimeoutExpired(command, 2)
        return subprocess.CompletedProcess(command, 0, stdout=version)

    monkeypatch.setattr(module.subprocess, "run", run)


@pytest.mark.parametrize("node,executable,version,cause", [
    (None, "/pi", None, "not both on PATH"),
    ("/node", "/pi", "v22.18.9", "v22.18.9 is older than 22.19"),
    ("/node", "/pi", "timeout", "did not finish within 2 seconds"),
])
def test_pi_start_failure_names_the_cause_and_the_opt_out(tmp_path, monkeypatch, node, executable, version, cause):
    monkeypatch.setitem(ADAPTERS, "pi", PiRuntime())
    _refuse(monkeypatch, pi_runner, node=node, executable=executable, version=version)
    assert pi_runner.enable(tmp_path) is None
    installed = ADAPTERS["pi"]
    assert type(installed) is PiRunnerRuntime and installed.pi is None
    with pytest.raises(RuntimeUnsupported, match="Pi runner") as caught:
        installed.resident_command("pi", cwd=tmp_path, prompt_file=tmp_path / "absent", launch=None)
    message = str(caught.value)
    assert cause in message
    assert '[runtimes] pi = "cli" in the ops file' in message
    assert "install the supported version" in message


def test_pi_sdk_outside_1_3_names_the_version_and_the_opt_out(tmp_path, monkeypatch):
    monkeypatch.setitem(ADAPTERS, "pi", PiRuntime())
    executable = _package(tmp_path, "@earendil-works/pi-coding-agent", "1.4.0")
    _refuse(monkeypatch, pi_runner, node="/node", executable=executable)
    assert pi_runner.enable(tmp_path) is None
    message = ADAPTERS["pi"].unavailable
    assert type(ADAPTERS["pi"]) is PiRunnerRuntime
    assert "1.3.x" in message and "1.4.0" in message
    assert '[runtimes] pi = "cli" in the ops file' in message
    with pytest.raises(RuntimeUnsupported, match="1.4.0"):
        ADAPTERS["pi"].resident_command("pi", cwd=tmp_path, prompt_file=tmp_path / "absent", launch=None)


def test_pi_executable_outside_its_package_names_the_gate(tmp_path, monkeypatch):
    monkeypatch.setitem(ADAPTERS, "pi", PiRuntime())
    executable = tmp_path / "pi"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    _refuse(monkeypatch, pi_runner, node="/node", executable=str(executable))
    assert pi_runner.enable(tmp_path) is None
    message = ADAPTERS["pi"].unavailable
    assert "@earendil-works/pi-coding-agent" in message and "1.3.x" in message
    assert "not inside" in message
    assert '[runtimes] pi = "cli" in the ops file' in message


@pytest.mark.parametrize("node,executable,version,cause", [
    (None, "/dsh", None, "not both on PATH"),
    ("/node", "/dsh", "v22.18.9", "v22.18.9"),
    ("/node", "/dsh", "timeout", "did not finish within 2 seconds"),
])
def test_dsh_start_failure_names_the_cause_and_the_opt_out(tmp_path, monkeypatch, node, executable, version, cause):
    monkeypatch.setitem(ADAPTERS, "dsh", DshRuntime())
    _refuse(monkeypatch, dsh_runner, node=node, executable=executable, version=version)
    assert dsh_runner.enable(tmp_path) is None
    installed = ADAPTERS["dsh"]
    assert type(installed) is DshRunnerRuntime and installed.dsh is None
    message = installed.unavailable
    assert "node 22.19+" in message and "dsh" in message and cause in message
    assert '[runtimes] dsh = "cli" in the ops file' in message
    assert "install the supported version" in message
    with pytest.raises(RuntimeUnsupported, match="DSH runner"):
        installed.resident_command("dsh", cwd=tmp_path, prompt_file=tmp_path / "absent", launch=None)


def test_dsh_sdk_outside_the_pin_names_the_version_and_the_opt_out(tmp_path, monkeypatch):
    monkeypatch.setitem(ADAPTERS, "dsh", DshRuntime())
    executable = _package(tmp_path, "@deepseek-ai/dsh", "0.2.0")
    _refuse(monkeypatch, dsh_runner, node="/node", executable=executable)
    assert dsh_runner.enable(tmp_path) is None
    message = ADAPTERS["dsh"].unavailable
    assert type(ADAPTERS["dsh"]) is DshRunnerRuntime
    assert "0.2.1-rc.2" in message and "0.2.0" in message and "node 22.19+" in message
    assert '[runtimes] dsh = "cli" in the ops file' in message
    with pytest.raises(RuntimeUnsupported, match="0.2.0"):
        ADAPTERS["dsh"].hosted_run("dsh", cwd=tmp_path, prompt_file=tmp_path / "absent", launch=None,
                                   max_turns=0, timeout_s=0, session_dir=None, tools=None, limits=None)


def test_supported_sdk_versions_still_install_the_runner(tmp_path, monkeypatch):
    monkeypatch.setitem(ADAPTERS, "pi", PiRuntime())
    monkeypatch.setitem(ADAPTERS, "dsh", dsh_runtime.DshRuntime())
    pi = _package(tmp_path / "pi-sdk", "@earendil-works/pi-coding-agent", "1.3.2")
    dsh = _package(tmp_path / "dsh-sdk", "@deepseek-ai/dsh", "0.2.1-rc.2")
    seen = {}

    def supervisor(root, commands, **kwargs):
        seen.setdefault("caps", []).append(kwargs["session_cap"])
        return object()

    monkeypatch.setattr(pi_runner.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="v22.19.0"))
    monkeypatch.setattr(dsh_runner.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="v24.20.0"))
    monkeypatch.setattr(pi_runner.shutil, "which", lambda name: "/node" if name == "node" else pi)
    monkeypatch.setattr(pi_runner, "RunnerSupervisor", supervisor)
    assert pi_runner.enable(tmp_path) is not None
    assert ADAPTERS["pi"].unavailable is None and seen["caps"] == [pi_runner.SESSION_CAP]
    monkeypatch.setattr(dsh_runner.shutil, "which", lambda name: "/node" if name == "node" else dsh)
    monkeypatch.setattr(dsh_runner, "RunnerSupervisor", supervisor)
    assert dsh_runner.enable(tmp_path) is not None
    assert ADAPTERS["dsh"].unavailable is None and seen["caps"] == [32, dsh_runner.SESSION_CAP]
    assert pi_runner.SESSION_CAP == dsh_runner.SESSION_CAP == 32


@pytest.mark.installed
@pytest.mark.parametrize("kind", ["pi", "dsh"])
def test_default_runner_admits_32_sdk_sessions(tmp_path, monkeypatch, kind):
    from dsh_runner_support import guard_env, require_dsh, session_params as dsh_params
    from pi_runner_support import require_pi, session_params as pi_params

    node, executable = require_pi() if kind == "pi" else require_dsh()
    module = pi_runner if kind == "pi" else dsh_runner
    monkeypatch.setitem(ADAPTERS, kind, ADAPTERS[kind])
    which = module.shutil.which
    monkeypatch.setattr(module.shutil, "which", lambda name:
        node if name == "node" else executable if name == kind else which(name))
    if kind == "pi":
        async def connect(process, kind):
            # Match the installed-test cold-import bound; this test checks admission, not latency.
            bridge = RunnerBridge(process.stdout, process.stdin, kind, control_timeout=60)
            try:
                await bridge.initialize(permitted=module.pi_command.PERMITTED)
            except BaseException:
                await bridge.close()
                raise
            bridge.control_timeout = 10
            return bridge
        monkeypatch.setattr(module.pi_command, "connect", connect)
    supervisor = module.enable(tmp_path / "runtime")
    assert supervisor is not None
    supervisor._env.setdefault(kind, {}).update(guard_env(tmp_path))

    def params(lease):
        values = (pi_params(lease, base_url="http://127.0.0.1:1", key="synthetic-pi-runner-key")
                  if kind == "pi" else dsh_params(lease))
        values["limits"]["tool_output_bytes"] = 102400
        values["provider"]["reasoning"] = kind == "dsh"
        return values

    async def exercise():
        leases = []
        try:
            # Retain every SDK session: no model turns, provider traffic or load generation.
            for _ in range(32):
                lease = await supervisor.acquire(kind)
                leases.append(lease)
                await lease.open_session(params(lease))
            bridge = leases[0].bridge
            assert bridge.capabilities["max_sessions"] == 32
            assert (await bridge.health())["sessions"] == 32
            with pytest.raises(ProtocolError) as host_full:
                await supervisor.acquire(kind)
            assert host_full.value.code == ErrorCode.CAPACITY
            # Bypass host admission to exercise the SDK runner's independent hard cap.
            (leases[0].directory.parent / "overflow" / "agent").mkdir(parents=True)
            with pytest.raises(RpcFailure) as runner_full:
                await bridge.open_session({**params(leases[0]), "session_key": "overflow"})
            assert runner_full.value.code == ErrorCode.CAPACITY
            await leases.pop().release()
            replacement = await supervisor.acquire(kind)
            leases.append(replacement)
            await replacement.open_session(params(replacement))
            assert (await bridge.health())["sessions"] == 32
            for lease in leases:
                await lease.release()
            assert (await bridge.health())["sessions"] == 0
        finally:
            await supervisor.stop()

    asyncio.run(asyncio.wait_for(exercise(), 120))


@pytest.mark.parametrize("kind", ["pi", "dsh"])
@pytest.mark.parametrize("role", ["manager", "worker"])
def test_lazy_spawn_failure_retains_cause_and_opt_out(tmp_path, monkeypatch, kind, role):
    module = pi_runner if kind == "pi" else dsh_runner
    package, version = (("@earendil-works/pi-coding-agent", "1.3.2") if kind == "pi"
                        else ("@deepseek-ai/dsh", "0.2.1-rc.2"))
    executable = _package(tmp_path, package, version)
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nprintf 'v24.20.0\\n'\n")
    node.chmod(0o755)
    monkeypatch.setitem(ADAPTERS, kind, ADAPTERS[kind])
    which = module.shutil.which
    monkeypatch.setattr(module.shutil, "which", lambda name:
        str(node) if name == "node" else executable if name == kind else which(name))
    supervisor = module.enable(tmp_path / "runtime")
    assert supervisor is not None and ADAPTERS[kind].unavailable is None
    node.unlink()  # Startup checks succeeded; the captured Node disappears before first use.
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Disposable instructions.")
    descriptor = Descriptor(kind=kind, role=role, provider_id="test",
        protocol=ApiProtocol.ANTHROPIC_MESSAGES if kind == "dsh" else ApiProtocol.OPENAI_RESPONSES,
        base_url="http://127.0.0.1:1", model="test-model", effort=None,
        context_window=None, system_prompt_file=str(prompt))

    async def exercise():
        try:
            session = (ADAPTERS[kind].resident_session("test") if role == "manager" else
                       RunnerSession("worker-run", supervisor, kind, owned=False))
            channel = RunnerChannel(session, descriptor, cwd=str(tmp_path), env={KEY_ENV: "synthetic-key"},
                tools=None, provider_id="test", model="test-model", price=None, limits=None)
            for _ in range(2):  # The terminal refusal must retain the diagnostic on later attempts too.
                with pytest.raises(RunnerUnavailable if role == "manager" else RunStartError) as caught:
                    if role == "manager":
                        await session.start(descriptor.to_argv(), cwd=str(tmp_path), signature=None,
                                            env={KEY_ENV: "synthetic-key"})
                    else:
                        await channel.launch()
                message = str(caught.value)
                assert "No such file or directory" in message
                assert str(node) in message
                assert f'[runtimes] {kind} = "cli" in the ops file' in message
            assert supervisor._kinds[kind].restart is None
        finally:
            await supervisor.stop()

    asyncio.run(asyncio.wait_for(exercise(), 10))


@pytest.mark.parametrize("command", ["run", "dialogs"])
@pytest.mark.parametrize("flags,pi,dsh", [
    ([], "runner", "runner"),
    (["--pi-runner", "--dsh-runner"], "runner", "runner"),
    (["--pi-cli"], "cli", "runner"),
    (["--dsh-cli"], "runner", "cli"),
    (["--pi-cli", "--dsh-cli"], "cli", "cli"),
])
def test_eval_flags_select_the_runner_unless_cli_is_named(monkeypatch, command, flags, pi, dsh):
    from evals.prompt_regression import __main__ as cli
    from evals.prompt_regression import dialogs
    from evals.prompt_regression import runner as eval_runner

    def run(**kwargs):
        assert kwargs["pi_runtime"] == pi and kwargs["dsh_runtime"] == dsh
        return {}, Path("unused.json")

    monkeypatch.setattr(eval_runner, "run", run)
    monkeypatch.setattr(dialogs, "run", run)
    monkeypatch.setattr(cli, "_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(dialogs, "markdown", lambda *_: "")
    assert cli.main([command, "--pg-url", "unused", *flags]) == 0


@pytest.mark.parametrize("command", ["run", "dialogs"])
@pytest.mark.parametrize("flags", [["--pi-cli", "--pi-runner"], ["--dsh-cli", "--dsh-runner"]])
def test_eval_rejects_cli_and_runner_flags_together(monkeypatch, command, flags):
    from evals.prompt_regression import __main__ as cli
    from evals.prompt_regression import runner as eval_runner

    def run(**kwargs):
        raise AssertionError("both flags must not start a run")

    monkeypatch.setattr(eval_runner, "run", run)
    assert cli.main([command, "--pg-url", "unused", *flags]) == 2


def test_dsh_validation_uses_runner_evidence_unless_dsh_cli(tmp_path, monkeypatch):
    from examples.dsh import validate as launcher

    argv = launcher.eval_argv("task-basic", "postgresql://postgres:postgres@127.0.0.1:54329/unused", tmp_path)
    assert "--dsh-runner" in argv and "--dsh-cli" not in argv
    cli_argv = launcher.eval_argv("manager", "postgresql://postgres:postgres@127.0.0.1:54329/unused", tmp_path,
                                  dsh_runner=False)
    assert "--dsh-cli" in cli_argv and "--dsh-runner" not in cli_argv
    runner_row = {"id": "single_step", "rep": 1, "outcome": "pass", "contract_violations": {"total": 0},
                  "planner_modified_files": 0, "dependency_order": {"edges": 0, "verified": 0, "violations": 0,
                                                                    "unknown": 0},
                  "runtime_evidence": [{"role": role, "runtime": "dsh", "executable": "looper-runner",
                                        "runner_kind": "dsh", "system_prompt_mode": "append",
                                        "session_present": True} for role in ("planner", "worker")]}
    assert launcher.basic_evidence_gaps([runner_row]) == []
    assert launcher.basic_evidence_gaps([runner_row], dsh_runner=False)
    cli_row = {"id": "single_step", "rep": 1, "outcome": "pass", "contract_violations": {"total": 0},
               "planner_modified_files": 0, "runtime_evidence": [
                   {"role": role, "runtime": "dsh", "executable": "dsh", "system_prompt_mode": "append",
                    "session_present": True} for role in ("planner", "worker")]}
    assert launcher.basic_evidence_gaps([cli_row])
    assert launcher.basic_evidence_gaps([cli_row], dsh_runner=False) == []

    seen = {}

    def execute(command, pg_url, key, baseline, *, dsh_runner):
        seen["dsh_runner"] = dsh_runner
        return {"status": "pass"}, 0

    monkeypatch.setattr(launcher, "preflight", lambda url: {"status": "pass"})
    monkeypatch.setattr(launcher, "read_key", lambda: "synthetic")
    monkeypatch.setattr(launcher, "execute", execute)
    pg = "--disposable-pg-url"
    url = "postgresql://postgres:postgres@127.0.0.1:54329/unused"
    assert launcher.main(["task-basic", "--real", pg, url]) == 0
    assert seen["dsh_runner"] is True
    assert launcher.main(["task-basic", "--real", "--dsh-runner", pg, url]) == 0
    assert seen["dsh_runner"] is True
    assert launcher.main(["task-basic", "--real", "--dsh-cli", pg, url]) == 0
    assert seen["dsh_runner"] is False
    assert launcher.main(["smoke", "--real", "--dsh-cli", "--dsh-runner", pg, url]) == 2
