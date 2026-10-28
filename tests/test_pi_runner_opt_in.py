"""Pi's opt-in leaves CLI launches intact and binds resident runner descriptors safely."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from looper import ops_config as ops
from looper.agents import serve
from looper.agents.agent_settings import AgentType
from looper.agents.knowledge import AgentKnowledge
from looper.agents.profile_registry import CatalogModel
from looper.agents.runtimes import ADAPTERS, ApiProtocol, RuntimeUnsupported
from looper.agents.runtimes.pi import KEY_ENV, PiRuntime, ResidentPi
from looper.agents.runtimes import pi_runner
from looper.agents.runtimes.pi_runner import PiRunnerRuntime
from looper.agents.runtimes.runner.session import Descriptor, RunnerSession
from looper.agents.system_prompt_mode import SystemPromptMode
from tests.pi_runtime_support import KEY, launch, provider


@pytest.mark.parametrize("value", ["cli", "runner"])
def test_ops_runtime_round_trip(tmp_path, value):
    config = ops.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops.FILE, pi_runtime=value)
    rendered = ops.render(config)
    assert '[runtimes]\n' + ('pi = "runner"' if value == "runner" else '# pi = "cli"') in rendered
    ops.write_private(config.path, rendered)
    loaded = ops.load(ops.Flags(runtime_root=tmp_path), environ={}, dotenv=tmp_path / "absent")
    assert loaded.pi_runtime == value


def test_ops_default_has_no_environment_variable(tmp_path):
    config = ops.load(ops.Flags(runtime_root=tmp_path), require_database=False,
                      environ={"LOOPER_PI_RUNTIME": "runner", "LOOPX_PI_RUNTIME": "runner"},
                      dotenv=tmp_path / "absent")
    assert config.pi_runtime == "cli"
    assert type(ADAPTERS["pi"]) is PiRuntime
    assert isinstance(ADAPTERS["pi"].resident_session("test"), ResidentPi)


@pytest.mark.parametrize("value", ['"private-invalid-value"', "true", "42", "[]", "{}"])
def test_invalid_ops_runtime_names_only_the_key(tmp_path, value):
    ops.write_private(tmp_path / ops.FILE, f"[runtimes]\npi = {value}\n")
    with pytest.raises(ops.OpsConfigError, match=r"runtimes\.pi") as caught:
        ops.load(ops.Flags(runtime_root=tmp_path), require_database=False, environ={}, dotenv=tmp_path / "absent")
    assert "private-invalid-value" not in str(caught.value)


@pytest.mark.parametrize("value", ["cli", "runner"])
def test_serve_reads_opt_in_before_ownership_and_cleans_partial_start(tmp_path, monkeypatch, value):
    calls = []

    async def stopped(name):
        calls.append(name)

    supervisor = SimpleNamespace(stop=lambda: stopped("runner stopped"))

    def enable(root):
        assert root == tmp_path
        calls.append("enabled")
        return supervisor

    async def owned(**kwargs):
        if value == "runner":
            assert kwargs["runner_supervisor"] is supervisor
            kwargs["ownership"].push_async_callback(stopped, "agents stopped")
        else:
            assert "runner_supervisor" not in kwargs
        raise serve.StartupCheckFailed("fixture startup failure")

    monkeypatch.setattr(pi_runner, "enable", enable)
    monkeypatch.setattr(serve, "_run_agent_system_owned", owned)
    monkeypatch.setattr("looper.db.close_pool", lambda: stopped("database stopped"))
    config = ops.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops.FILE, pi_runtime=value)
    with pytest.raises(serve.StartupCheckFailed):
        asyncio.run(serve._run_agent_system(ops=config, runtime_root=tmp_path, work_dir=tmp_path))
    assert calls == (["enabled", "agents stopped", "runner stopped", "database stopped"] if value == "runner" else [])


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    executable = tmp_path / "pi"
    executable.touch(mode=0o700)
    result = PiRunnerRuntime(SimpleNamespace(), pi=executable.resolve())
    monkeypatch.setitem(ADAPTERS, "pi", result)
    return result, str(executable)


@pytest.mark.parametrize("role,mode,efforts", [
    (AgentType.MANAGER, SystemPromptMode.REPLACE, None),
    (AgentType.PLANNER, SystemPromptMode.APPEND, ()),
])
def test_resident_descriptor_prompt_environment_and_session(tmp_path, monkeypatch, runtime, role, mode, efforts):
    adapter, executable = runtime
    selected = launch(provider(catalog=(CatalogModel("selected", context_window=123456, efforts=efforts),)))
    selected = replace(selected, role=role, settings=replace(selected.settings, system_prompt_mode=mode),
                       knowledge=AgentKnowledge(add_dirs=(str(tmp_path / "knowledge"),)),
                       read_dirs=(str(tmp_path / "readable"),))
    monkeypatch.setenv("LOOPER_OPERATOR_TOKEN", "operator-private-value")
    monkeypatch.setenv("PI_CONFIG_DIR", "operator-config")
    prompt = tmp_path / ".local" / "prompts" / "original.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("LOOPER_PROMPT")
    (tmp_path / "AGENTS.md").write_text("PROJECT_SENTINEL")
    argv = adapter.resident_command(executable, cwd=tmp_path, prompt_file=prompt, launch=selected)
    descriptor = Descriptor.parse(argv)
    assert descriptor.kind == "pi" and descriptor.role == role.value
    assert descriptor.protocol is ApiProtocol.OPENAI_RESPONSES
    assert (descriptor.provider_id, descriptor.model, descriptor.effort) == ("prov-test", "selected", "high")
    assert descriptor.context_window == 123456
    assert descriptor.builtin == (("read", "grep", "find", "ls") if role is AgentType.PLANNER
                                  else ("read", "bash", "edit", "write"))
    assert descriptor.read_only is (role is AgentType.PLANNER)
    assert descriptor.system_prompt_mode == mode.value
    assert descriptor.reasoning is (efforts != ())
    assert descriptor.add_dirs == (*selected.knowledge.add_dirs, *selected.read_dirs)
    written = Path(descriptor.system_prompt_file)
    text = written.read_text()
    assert written == prompt.parent.parent / "pi-runner" / "prompts" / f"{hashlib.sha256(text.encode()).hexdigest()}.md"
    assert written.stat().st_mode & 0o777 == 0o600
    assert "PROJECT_SENTINEL" in text and "LOOPER_PROMPT" in text
    assert "platform tools are named looper_*" in text and "mcp__looper__" not in text
    assert adapter.prompt_blocks(selected, "LOOPER_PROMPT", cwd=tmp_path)[-1].text == text
    environment = adapter.environment(selected)
    assert environment[KEY_ENV] == KEY
    assert "LOOPER_OPERATOR_TOKEN" not in environment and "PI_CONFIG_DIR" not in environment
    assert KEY not in " ".join(argv) and KEY not in text
    assert isinstance(adapter.resident_session("test"), RunnerSession)
    assert adapter.resume_args("saved") == ["--session", "saved"]
    assert adapter.platform_tools_in_process and adapter.can_interrupt and adapter.isolation_id == "pi-runner/v1"


def test_resident_launch_rejects_mismatch_provider_env_and_unavailable(tmp_path, runtime):
    adapter, executable = runtime
    selected = replace(launch(provider()), role=AgentType.MANAGER)
    kwargs = dict(cwd=tmp_path, prompt_file=tmp_path / "absent", launch=selected)
    with pytest.raises(RuntimeUnsupported, match="launch executable") as mismatch:
        adapter.resident_command("missing-other-pi", **kwargs)
    assert executable in str(mismatch.value) and "missing-other-pi" in str(mismatch.value)
    selected = replace(launch(provider(env=(("HTTPS_PROXY", "private-provider-value"),))), role=AgentType.MANAGER)
    with pytest.raises(RuntimeUnsupported, match="provider's environment") as caught:
        adapter.resident_command(executable, **{**kwargs, "launch": selected})
    assert "private-provider-value" not in str(caught.value)
    unavailable = PiRunnerRuntime(None, pi=None, unavailable="fixture missing runner")
    with pytest.raises(RuntimeUnsupported, match="fixture missing runner"):
        unavailable.resident_command(executable, **kwargs)
    with pytest.raises(RuntimeUnsupported, match="Manager and Planner"):
        adapter.resident_command(executable, **{**kwargs, "launch": replace(selected, role=AgentType.WORKER)})


@pytest.mark.parametrize("role,consult", [(AgentType.WORKER, False), (AgentType.REVIEWER, False),
                                         (AgentType.WORKER, True)])
def test_other_roles_keep_cli_commands_environment_and_notes(tmp_path, runtime, role, consult):
    adapter, executable = runtime
    selected = replace(launch(provider()), role=role, consult=consult)
    prompt = tmp_path / ".local" / "prompts" / "input.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("Looper prompt")
    kwargs = dict(cwd=tmp_path, prompt_file=prompt, launch=selected, session_dir=tmp_path / "sessions")
    plain = PiRuntime()
    assert adapter.run_command(executable, **kwargs) == plain.run_command(executable, **kwargs)
    assert adapter.environment(selected) == plain.environment(selected)
    assert adapter.prompt_blocks(selected, "prompt", cwd=tmp_path) == plain.prompt_blocks(selected, "prompt", cwd=tmp_path)
    for name in ("run_command", "steer_command", "run_channel", "consult_command"):
        assert getattr(PiRunnerRuntime, name) is getattr(PiRuntime, name)


@pytest.mark.parametrize("node,pi,version", [(None, "/pi", None), ("/node", None, None),
    ("/node", "/pi", "v22.18.9"), ("/node", "/pi", "unreadable"), ("/node", "/pi", "timeout")])
def test_enable_records_unavailability_without_fallback(tmp_path, monkeypatch, node, pi, version):
    monkeypatch.setitem(ADAPTERS, "pi", PiRuntime())
    monkeypatch.setattr(pi_runner.shutil, "which", lambda name: node if name == "node" else pi)

    def run(command, **kwargs):
        assert kwargs["timeout"] == 2
        if version == "timeout":
            raise subprocess.TimeoutExpired(command, 2)
        return subprocess.CompletedProcess(command, 0, stdout=version)

    monkeypatch.setattr(pi_runner.subprocess, "run", run)
    assert pi_runner.enable(tmp_path) is None
    installed = ADAPTERS["pi"]
    assert isinstance(installed, PiRunnerRuntime) and installed.pi is None
    with pytest.raises(RuntimeUnsupported, match="Pi runner"):
        installed.resident_command("pi", cwd=tmp_path, prompt_file=tmp_path / "absent",
                                   launch=replace(launch(provider()), role=AgentType.MANAGER))


@pytest.mark.parametrize("version", ["v22.19.0", "v24.20.0"])
def test_enable_installs_one_ten_session_supervisor(tmp_path, monkeypatch, version):
    monkeypatch.setitem(ADAPTERS, "pi", PiRuntime())
    monkeypatch.setattr(pi_runner.shutil, "which", lambda name: "/node" if name == "node" else "/pi")
    monkeypatch.setattr(pi_runner.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=version))
    seen = {}

    def supervisor(root, commands, **kwargs):
        seen.update(root=root, commands=commands, **kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(pi_runner, "RunnerSupervisor", supervisor)
    result = pi_runner.enable(tmp_path)
    assert ADAPTERS["pi"].supervisor is result
    assert ADAPTERS["pi"].pi == Path("/pi")
    assert seen["root"] == tmp_path and seen["session_cap"] == 10
    assert seen["commands"]["pi"][0] == "/node" and seen["commands"]["pi"][-2:] == ["--pi", "/pi"]
    assert seen["env"] == {"pi": pi_runner.pi_command.ENV}
    assert seen["connect"] is pi_runner.pi_command.connect
