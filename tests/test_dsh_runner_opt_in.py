"""DSH's opt-in binds all roles to private, policy-checked runner sessions."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper import ops_config as ops
from looper.agents import serve
from looper.agents.agent_settings import AgentSettings, AgentType, Launch
from looper.agents.knowledge import AgentKnowledge
from looper.agents.library import AgentLibrary, ItemKind, LoadedItem
from looper.agents.profile_registry import CatalogModel, LegacyProviderSpec, RuntimeSpec
from looper.agents.runtimes import ADAPTERS, ApiProtocol, AuthKind, ReadOnly, RuntimeUnsupported
from looper.agents.runtimes import dsh, dsh_runner, pi_runner
from looper.agents.runtimes.api.adapter import KEY_ENV
from looper.agents.runtimes.channel import Fork, RunStartError
from looper.agents.runtimes.dsh_runner import DshRunnerRuntime
from looper.agents.runtimes.runner.channel import RunnerChannel
from looper.agents.runtimes.runner.protocol import ErrorCode, ProtocolError
from looper.agents.runtimes.runner.session import Descriptor, RunnerSession
from looper.agents.runtimes.runner.supervisor import RunnerUnavailable
from tests.runtime_model_helpers import resolved_profile, with_model_name

KEY = "test-dsh-runner-key"
MODEL = "deepseek-flash[1m]"


def launch(role=AgentType.MANAGER, **provider_fields):
    provider = LegacyProviderSpec(**{"id": "dsh-account", "name": "DSH account", "auth": AuthKind.API_KEY,
        "protocols": frozenset({ApiProtocol.ANTHROPIC_MESSAGES}), "api_key": KEY,
        "base_url": "https://gateway.example.invalid/anthropic/v1/",
        "catalog": (CatalogModel(MODEL, context_window=1048576),), **provider_fields})
    profile = resolved_profile("dsh-profile", "DSH", RuntimeSpec("rt-dsh", "DSH", "dsh"),
                               provider, MODEL, "high")
    return Launch(AgentSettings(profile=profile), role=role)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    executable = tmp_path / "dsh"
    executable.touch(mode=0o700)
    adapter = DshRunnerRuntime(SimpleNamespace(), dsh=executable.resolve())
    monkeypatch.setitem(ADAPTERS, "dsh", adapter)
    return adapter, str(executable)


def prompt_file(tmp_path):
    prompt = tmp_path / ".local" / "prompts" / "input.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("LOOPER_PROMPT")
    return prompt


@pytest.mark.parametrize("value", ["cli", "runner"])
def test_ops_dsh_runtime_round_trip(tmp_path, value):
    config = ops.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops.FILE, dsh_runtime=value)
    text = ops.render(config)
    assert '[runtimes]\n# pi = "runner"\n' in text
    assert ('# dsh = "runner"' if value == "runner" else 'dsh = "cli"') in text
    ops.write_private(config.path, text)
    assert ops.load(ops.Flags(runtime_root=tmp_path), environ={}, dotenv=tmp_path / "absent").dsh_runtime == value


@pytest.mark.parametrize("value", ['"private-invalid-value"', "true", "42", "[]", "{}"])
def test_invalid_ops_dsh_runtime_names_only_key(tmp_path, value):
    ops.write_private(tmp_path / ops.FILE, f"[runtimes]\ndsh = {value}\n")
    with pytest.raises(ops.OpsConfigError, match=r"runtimes\.dsh") as caught:
        ops.load(ops.Flags(runtime_root=tmp_path), require_database=False, environ={}, dotenv=tmp_path / "absent")
    assert "private-invalid-value" not in str(caught.value)


def test_ops_default_has_no_environment_opt_in(tmp_path):
    config = ops.load(ops.Flags(runtime_root=tmp_path), require_database=False,
        environ={"LOOPER_DSH_RUNTIME": "runner", "LOOPX_DSH_RUNTIME": "runner"}, dotenv=tmp_path / "absent")
    assert config.dsh_runtime == "runner"
    assert type(ADAPTERS["dsh"]) is dsh.DshRuntime
    assert not ADAPTERS["dsh"].hosts_runs
    assert isinstance(ADAPTERS["dsh"].resident_session("test"), dsh.ResidentDsh)


@pytest.mark.parametrize("pi_choice,dsh_choice", [("cli", "cli"), ("runner", "cli"),
                                                  ("cli", "runner"), ("runner", "runner")])
def test_serve_enables_each_opted_in_runner(tmp_path, monkeypatch, pi_choice, dsh_choice):
    calls, enabled = [], []

    async def stopped(name):
        calls.append(name)

    def enable(kind, root):
        assert root == tmp_path
        calls.append(f"enable {kind}")
        supervisor = SimpleNamespace(stop=lambda: stopped(f"stop {kind}"))
        enabled.append(supervisor)
        return supervisor

    async def owned(**kwargs):
        assert kwargs.get("runner_supervisors", ()) == tuple(enabled)
        if enabled:
            kwargs["ownership"].push_async_callback(stopped, "agents")
        raise serve.StartupCheckFailed("fixture startup failure")

    monkeypatch.setattr(pi_runner, "enable", lambda root: enable("pi", root))
    monkeypatch.setattr(dsh_runner, "enable", lambda root: enable("dsh", root))
    monkeypatch.setattr(serve, "_run_agent_system_owned", owned)
    monkeypatch.setattr("looper.db.close_pool", lambda: stopped("database"))
    config = ops.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops.FILE,
                           pi_runtime=pi_choice, dsh_runtime=dsh_choice)
    with pytest.raises(serve.StartupCheckFailed):
        asyncio.run(serve._run_agent_system(ops=config, runtime_root=tmp_path, work_dir=tmp_path))
    kinds = [kind for kind, choice in (("pi", pi_choice), ("dsh", dsh_choice)) if choice == "runner"]
    assert calls == ([*(f"enable {kind}" for kind in kinds), "agents",
                      *(f"stop {kind}" for kind in reversed(kinds)), "database"] if kinds else [])


@pytest.mark.parametrize("failure", ["unsafe_state", "permission", "missing_entry"])
def test_serve_continues_with_unavailable_dsh_and_owns_pi(tmp_path, monkeypatch, failure):
    monkeypatch.setitem(ADAPTERS, "dsh", dsh.DshRuntime())
    monkeypatch.setattr(dsh_runner.shutil, "which", lambda name: f"/{name}")
    monkeypatch.setattr(dsh_runner.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="v22.19.0"))
    stopped = []

    async def stop(name):
        stopped.append(name)

    pi = SimpleNamespace(stop=lambda: stop("pi"))
    monkeypatch.setattr(pi_runner, "enable", lambda root: pi)
    monkeypatch.setattr("looper.db.close_pool", lambda: stop("database"))
    if failure == "unsafe_state":
        (tmp_path / "runners").mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "runners/state").symlink_to(outside, target_is_directory=True)
    else:
        def failed(*args):
            if failure == "permission":
                raise PermissionError("fixture state directory denied")
            raise RuntimeUnsupported("fixture runner entry missing")

        monkeypatch.setattr(dsh_runner.dsh_command, "state_root" if failure == "permission" else "entry", failed)

    async def owned(*, runner_supervisors, ownership, **kwargs):
        assert runner_supervisors == (pi,)
        adapter = ADAPTERS["dsh"]
        assert isinstance(adapter, DshRunnerRuntime) and adapter.unavailable
        assert adapter.supervisor is None and adapter.dsh is None
        for role in AgentType.MANAGER, AgentType.PLANNER, AgentType.WORKER, AgentType.REVIEWER:
            with pytest.raises(RuntimeUnsupported, match="DSH runner"):
                if role in (AgentType.MANAGER, AgentType.PLANNER):
                    adapter.resident_command("dsh", cwd=tmp_path, prompt_file=tmp_path / "absent", launch=launch(role))
                else:
                    adapter.hosted_run("dsh", cwd=tmp_path, prompt_file=tmp_path / "absent", launch=launch(role),
                        max_turns=0, timeout_s=0, session_dir=None, tools=None, limits=AsyncMock())
        ownership.push_async_callback(stop, "agents")
        raise serve.StartupCheckFailed("fixture later startup failure")

    monkeypatch.setattr(serve, "_run_agent_system_owned", owned)
    config = ops.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops.FILE, pi_runtime="runner", dsh_runtime="runner")
    with pytest.raises(serve.StartupCheckFailed, match="later startup failure"):
        asyncio.run(serve._run_agent_system(ops=config, runtime_root=tmp_path, work_dir=tmp_path))
    assert stopped == ["agents", "pi", "database"]
    if failure == "unsafe_state":
        assert list(outside.iterdir()) == []


@pytest.mark.parametrize("linked_root", [False, True])
def test_enable_accepts_existing_directory_symlinks_above_state(tmp_path, monkeypatch, linked_root):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    root = linked if linked_root else linked / "new-runtime"
    monkeypatch.setitem(ADAPTERS, "dsh", dsh.DshRuntime())
    monkeypatch.setattr(dsh_runner.shutil, "which", lambda name: f"/{name}")
    monkeypatch.setattr(dsh_runner.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="v22.19.0"))
    supervisor = dsh_runner.enable(root)
    adapter = ADAPTERS["dsh"]
    assert supervisor is not None and adapter.supervisor is supervisor and adapter.unavailable is None
    state = dsh_runner.dsh_command.state_root(root)
    assert state.resolve() == (actual if linked_root else actual / "new-runtime") / "runners/state/dsh-native"
    for directory in (state.parent.parent, state.parent, state):
        assert directory.stat().st_mode & 0o777 == 0o700
    marker = state / "persisted-session.jsonl"
    marker.write_text("persisted")
    assert dsh_runner.dsh_command.state_root(root) == state and marker.read_text() == "persisted"


@pytest.mark.parametrize("role", [AgentType.MANAGER, AgentType.PLANNER])
def test_resident_descriptor_prompt_and_environment(tmp_path, monkeypatch, runtime, role):
    adapter, executable = runtime
    selected = replace(launch(role), knowledge=AgentKnowledge(add_dirs=(str(tmp_path / "knowledge"),)),
                       read_dirs=(str(tmp_path / "readable"),))
    selected = replace(selected, settings=replace(selected.settings, env=(("LOOPER_PRIVATE", "secret"),
                       ("LOOPX_PRIVATE", "secret"), ("HOME", "private-home"), ("DSH_HOME", "private-dsh"),
                       ("DEEPSEEK_API_KEY", "ambient-key"), ("ANTHROPIC_BASE_URL", "private-url"),
                       ("XDG_STATE_HOME", "private-state"))))
    monkeypatch.setenv("LOOPER_OPERATOR_TOKEN", "operator-secret")
    (tmp_path / "AGENTS.md").write_text("PROJECT_SENTINEL")
    prompt = prompt_file(tmp_path)
    argv = adapter.resident_command(executable, cwd=tmp_path, prompt_file=prompt, launch=selected)
    descriptor = Descriptor.parse(argv)
    resumed = Descriptor.parse(argv + adapter.resume_args("saved-session"))
    assert resumed.session == "saved-session"
    assert (descriptor.kind, descriptor.role, descriptor.provider_id) == ("dsh", role.value, "dsh-account")
    assert descriptor.protocol is ApiProtocol.ANTHROPIC_MESSAGES
    assert (descriptor.base_url, descriptor.model, descriptor.effort) == (
        "https://gateway.example.invalid/anthropic/v1/", MODEL, "high")
    assert descriptor.context_window == 1048576 and descriptor.reasoning
    assert descriptor.system_prompt_mode == "append"
    assert descriptor.builtin == (("read", "glob", "grep") if role is AgentType.PLANNER
                                  else ("read", "glob", "grep", "write", "edit", "bash"))
    assert descriptor.read_only is (role is AgentType.PLANNER)
    assert descriptor.add_dirs == (*selected.knowledge.add_dirs, *selected.read_dirs)
    written = Path(descriptor.system_prompt_file)
    text = written.read_text()
    assert written == prompt.parent.parent / "dsh-runner" / "prompts" / f"{hashlib.sha256(text.encode()).hexdigest()}.md"
    assert written.stat().st_mode & 0o777 == 0o600
    assert written.parent.stat().st_mode & 0o777 == 0o700
    assert all(sentinel in text for sentinel in ("LOOPER_PROMPT", "PROJECT_SENTINEL", "platform tools are named looper_*"))
    blocks = adapter.prompt_blocks(selected, "LOOPER_PROMPT", cwd=tmp_path)
    assert [block.name for block in blocks] == ["Default prompt", "Looper instructions"]
    assert blocks[0].text is None and blocks[-1].text == text
    environment = adapter.environment(selected)
    assert environment[KEY_ENV] == KEY and "HOME" not in environment
    assert not any(key.startswith(("DSH_", "DEEPSEEK_", "ANTHROPIC_", "XDG_", "LOOPX_")) or
                   key.startswith("LOOPER_") and key != KEY_ENV for key in environment)
    assert KEY not in " ".join(argv) and KEY not in text
    session = adapter.resident_session("test")
    assert isinstance(session, RunnerSession) and session.kind == "dsh"
    assert adapter.hosts_runs and adapter.platform_tools_in_process and adapter.can_interrupt
    assert adapter.read_only is ReadOnly.PERMISSIONS and not adapter.can_replace_system_prompt
    assert adapter.isolation_id == "dsh-runner/v1"


@pytest.mark.parametrize("failure", ["provider_env", "web_search", "model", "key", "unavailable", "executable"])
def test_launch_refusals_before_writes(tmp_path, runtime, failure):
    adapter, executable = runtime
    selected = launch()
    if failure == "provider_env":
        selected, expected = launch(env=(("HTTPS_PROXY", "private-provider-value"),)), "provider's environment"
    elif failure == "web_search":
        selected, expected = replace(selected, settings=replace(selected.settings, env=(("DSH_ALLOW_WEB_SEARCH", "true"),))), "no web search"
    elif failure == "model":
        selected, expected = replace(selected, settings=replace(selected.settings,
            profile=with_model_name(selected.profile, "unsupported-model"))), "DSH supports only"
    elif failure == "key":
        selected, expected = launch(api_key=None), "no API key"
    elif failure == "unavailable":
        adapter.unavailable = expected = "fixture unavailable runner"
    else:
        other = tmp_path / "other-dsh"
        other.touch(mode=0o700)
        executable, expected = str(other), "launch executable"
    with pytest.raises(RuntimeUnsupported, match=expected) as caught:
        adapter.resident_command(executable, cwd=tmp_path, prompt_file=tmp_path / "absent", launch=selected)
    assert "private-provider-value" not in str(caught.value)
    assert not (tmp_path / "dsh-runner").exists() and not (tmp_path / ".local").exists()


@pytest.mark.parametrize("role", [AgentType.WORKER, AgentType.REVIEWER])
@pytest.mark.parametrize("max_turns,timeout_s,requests,tool_timeout", [(7, 360, 7, 360), (0, 0, 1000, 86400)])
def test_hosted_run_and_consult_fork(tmp_path, runtime, role, max_turns, timeout_s, requests, tool_timeout):
    adapter, executable = runtime
    selected = launch(role)
    kwargs = dict(cwd=tmp_path, prompt_file=prompt_file(tmp_path), launch=selected,
        session_dir=tmp_path / "sessions", max_turns=max_turns, timeout_s=timeout_s, tools=None, limits=AsyncMock())
    handle = adapter.hosted_run(executable, **kwargs)
    descriptor = Descriptor.parse(handle.command)
    assert descriptor.role == role.value and descriptor.builtin == ("read", "glob", "grep", "write", "edit", "bash")
    assert (descriptor.max_requests, descriptor.tool_timeout_s) == (requests, tool_timeout)
    assert descriptor.fork_from is None and not descriptor.read_only
    assert handle.session.kind == "dsh" and not handle.session._owned
    assert handle.session._state_dir == kwargs["session_dir"] and KEY not in str(handle.command)
    source = tmp_path / "saved"
    source.mkdir()
    (source / "history").write_text("original history")
    fork = Fork("source-id", source, tmp_path / "forks" / "consult")
    consult = adapter.hosted_run(executable, **{**kwargs, "launch": replace(selected, consult=True), "fork": fork})
    descriptor = Descriptor.parse(consult.command)
    assert descriptor.read_only and descriptor.builtin == ("read", "glob", "grep")
    assert descriptor.fork_from == "source-id" and descriptor.system_prompt_mode == "append"
    assert consult.session._state_dir == fork.fork_dir and fork.fork_dir.stat().st_mode & 0o777 == 0o700
    assert list(fork.fork_dir.iterdir()) == [] and (source / "history").read_text() == "original history"
    with pytest.raises(RuntimeUnsupported, match="Worker"):
        adapter.hosted_run(executable, **{**kwargs, "launch": launch()})
    for method in ("run_command", "steer_command", "consult_command"):
        with pytest.raises(RuntimeUnsupported, match="hosts this run; there is no DSH CLI command"):
            getattr(adapter, method)(executable, **kwargs)


def test_hosted_fork_failure_removes_created_directory(tmp_path, monkeypatch, runtime):
    adapter, executable = runtime
    fork = Fork("source-id", tmp_path / "source", tmp_path / "forks" / "consult")

    def failed(*args, **kwargs):
        assert fork.fork_dir.is_dir()
        raise ValueError("fixture descriptor failure")

    monkeypatch.setattr(adapter, "_descriptor", failed)
    with pytest.raises(ValueError, match="fixture descriptor failure"):
        adapter.hosted_run(executable, cwd=tmp_path, prompt_file=prompt_file(tmp_path),
            launch=replace(launch(AgentType.REVIEWER), consult=True), session_dir=None,
            max_turns=0, timeout_s=0, tools=None, limits=AsyncMock(), fork=fork)
    assert not fork.fork_dir.exists()


@pytest.mark.parametrize("availability", ["no_node", "no_dsh", "old_node", "timeout", "available"])
def test_enable_records_unavailability_and_installs_supervisor(tmp_path, monkeypatch, availability):
    monkeypatch.setitem(ADAPTERS, "dsh", dsh.DshRuntime())
    node, executable = "/node", str(tmp_path / "installed-dsh")
    monkeypatch.setattr(dsh_runner.shutil, "which", lambda name: None if
        availability == "no_" + name else node if name == "node" else executable)

    def version(command, **kwargs):
        assert kwargs["timeout"] == 2
        if availability == "timeout":
            raise subprocess.TimeoutExpired(command, 2)
        return SimpleNamespace(stdout="v22.18.0" if availability == "old_node" else "v22.19.0")

    monkeypatch.setattr(dsh_runner.subprocess, "run", version)
    seen = {}

    def supervisor(root, commands, **kwargs):
        seen.update(root=root, commands=commands, **kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(dsh_runner, "RunnerSupervisor", supervisor)
    result = dsh_runner.enable(tmp_path)
    installed = ADAPTERS["dsh"]
    assert isinstance(installed, DshRunnerRuntime)
    if availability != "available":
        assert result is None and installed.unavailable and installed.dsh is None
        assert "node 22.19+" in installed.unavailable and "dsh" in installed.unavailable
        assert not seen
    else:
        assert installed.supervisor is result and installed.dsh == Path(executable).resolve()
        assert seen["root"] == tmp_path and seen["session_cap"] == 32
        state = tmp_path / "runners" / "state" / "dsh-native"
        assert list(seen["commands"]["dsh"][-4:]) == ["--dsh", str(Path(executable).resolve()), "--state-root", str(state)]
        assert seen["connect"] is dsh_runner.dsh_command.connect and state.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("failure,expected", [
    (ProtocolError(ErrorCode.CAPACITY), "the dsh runner has no free session (CAPACITY); a session frees when a Manager, Planner or run ends"),
    (RunnerUnavailable("fixture unavailable"), "the dsh runner is unavailable: fixture unavailable"),
])
def test_runner_channel_reasons_name_the_kind(tmp_path, failure, expected):
    session = RunnerSession("worker-run", SimpleNamespace(), "dsh", owned=False, state_dir=tmp_path)
    session.start = AsyncMock(side_effect=failure)
    descriptor = Descriptor("dsh", "worker", "account", ApiProtocol.ANTHROPIC_MESSAGES,
                            "https://example.invalid", MODEL, "high", 1048576, "prompt.md")
    handle = RunnerChannel(session, descriptor, cwd=str(tmp_path), env={}, tools=None,
                           provider_id="account", model=MODEL, price=None, limits=AsyncMock())
    with pytest.raises(RunStartError) as caught:
        asyncio.run(handle.launch())
    assert str(caught.value) == expected


def test_launch_record_skips_catalog_without_cli_materialization(monkeypatch, runtime):
    adapter, _ = runtime

    def forbidden(*args, **kwargs):
        raise AssertionError("runner launch must not materialize the CLI catalog or MCP bridge")

    monkeypatch.setattr(dsh, "materialize", forbidden)
    monkeypatch.setattr(dsh, "mcp_config", forbidden)
    selected = launch()
    record = adapter.launch_record(selected)
    assert record == {"model": MODEL, "isolation": "dsh-runner/v1", "system_prompt_mode": "append",
                      "web_search": False, "runtime_limits": {"max_requests": "checked before every model request"}}
    library = AgentLibrary(items=(LoadedItem(ItemKind.SKILL, "example", "hash"),
        LoadedItem(ItemKind.PLUGIN, "example-plugin", "hash"), LoadedItem(ItemKind.MCP, "example-mcp", "hash"),
        LoadedItem(ItemKind.MCP, "looper", "hash")))
    record = adapter.launch_record(replace(selected, library=library))
    assert record["runtime_catalog"] == {"runtime": "dsh", "skipped": [
        f"{kind} {name}: the DSH runner loads no catalog skills, plugins or MCP servers"
        for kind, name in (("skill", "example"), ("plugin", "example-plugin"), ("mcp", "example-mcp"))]}


@pytest.mark.parametrize("errors,expected", [([], None), (["lost", "rejected"], "lost; rejected"), (["x" * 600], "x" * 500)])
def test_failure_detail_is_bounded(runtime, errors, expected):
    adapter, _ = runtime
    assert adapter.failure_detail({"type": "result", "errors": errors}, launch=launch(), command=[]) == expected
