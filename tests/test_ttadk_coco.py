"""Coco through TTADK, with synthetic executables, settings and login paths."""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents import agent_settings, profile_registry as registry
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.llmrec import control
from looper.agents.profile_registry import Endpoint, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, Launcher, VendorType, adapter, coco_login, ttadk
from looper.agents.system_prompt import SystemPrompt
from looper.agents.tiers import worker
from test_agent_planner_worker import _step_envelope, _worker_row

COCO = adapter("coco")
FIXTURE = Path(__file__).parent / "fixtures" / "coco_exec" / "sample.jsonl"


def _launch(executable="ttadk"):
    provider = ProviderSpec("prov-coco-login", "Coco login", VendorType.COCO_LOGIN, AuthKind.OAUTH, "coco")
    model = ModelSpec("model-coco", provider.id, "GPT-5.5", ApiProtocol.OPENAI_RESPONSES)
    resolved = ResolvedModel(model, provider, Endpoint(provider.id, model.protocol, ""))
    runtime = RuntimeSpec("rt-ttadk-coco", "ttadk-coco", "coco", executable, COCO.capabilities,
                          launcher=Launcher.TTADK, version="ttadk 1.0.37 / traecli 0.214.1")
    profile = ResolvedAgentProfile("profile-coco", "Coco", runtime, model.id, resolved_model=resolved)
    return Launch(AgentSettings(profile=profile))


@pytest.fixture
def stand_ins(tmp_path, monkeypatch):
    operator = tmp_path / "operator"
    operator.mkdir()
    monkeypatch.setenv("HOME", str(operator))
    monkeypatch.setenv("TRAE_HOME", str(tmp_path / "login-home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: operator))
    monkeypatch.setattr(coco_login, "recorded_problem", AsyncMock(return_value=None))
    calls = tmp_path / "calls.jsonl"
    events = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    reply = json.dumps({"status": "completed", "summary": "finished the step", "changed_files": {}})
    next(e["item"] for e in reversed(events) if e.get("item", {}).get("type") == "agent_message")["text"] = reply
    stream = "\n".join(json.dumps(e) for e in events) + "\n"
    native = f'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open({str(calls)!r}, 'a') as log: log.write(json.dumps([Path(sys.argv[0]).name, *args]) + '\\n')
if args == ['--version']: print('traecli 0.214.1')
elif args == ['login', 'status']: print('Logged in')
elif args == ['models', '--json']: print('[{{"name":"GPT-5.5","config_name":"gpt-5.5"}}]')
else:
    assert args[0] == 'exec' and args[-1] == '-', args
    assert Path(os.environ['TRAE_HOME'], 'cli', 'auth.json').is_symlink()
    assert sys.stdin.read()
    print({stream!r}, end='')
'''
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("coco", "traex", "custom-coco"):
        fake_claude(bin_dir / name, native)
    executable = fake_claude(bin_dir / "ttadk", f'''
import json, os, sys
args = sys.argv[1:]
with open({str(calls)!r}, 'a') as log: log.write(json.dumps(['ttadk', *args]) + '\\n')
if args == ['--version']: print('1.0.37')
elif args == ['models', '-t', 'trae-cli', '-f', 'json']: print('{{"models":[]}}')
else:
    assert args[:6] == ['code', '-t', 'trae-cli', '--skip-check', '--', 'exec'], args
    print('Login successful.', flush=True)
    print('', flush=True)
    print('Launching CLI...', flush=True)
    os.execv({str(bin_dir / 'traex')!r}, ['traex', *args[5:]])
''')
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.defpath)
    ttadk.forget_probe()
    coco_login.forget_probe()
    coco_login.forget_models()
    yield executable, calls, operator
    ttadk.forget_probe()
    coco_login.forget_probe()
    coco_login.forget_models()


def test_coco_wrapper_command_keeps_model_after_the_separator(tmp_path, stand_ins):
    executable, _, _ = stand_ins
    assert ttadk.prefix("ttadk", "trae-cli") == ["ttadk", "code", "-t", "trae-cli", "--skip-check", "--"]
    launch = _launch(executable)
    command = COCO.run_command(executable, cwd=tmp_path,
        prompt_file=SystemPrompt("Instructions").write_file(tmp_path / "runtime"), launch=launch)
    assert command[0] == "env" and command[1].startswith("TRAE_HOME=")
    assert command[2:9] == [executable, "code", "-t", "trae-cli", "--skip-check", "--", "exec"]
    assert command[9:11] == ["--json", "--skip-git-repo-check"]
    assert command[command.index("-m"):][:2] == ["-m", "GPT-5.5"]
    assert "-m" not in command[:command.index("--")] and command[-1] == "-"
    launch = replace(launch, settings=replace(launch.settings, env=(("CI", "1"), ("DEBUG", "1"))))
    env = launch.environment()
    assert "CI" not in env and "DEBUG" not in env
    assert env["TTADK_DISABLE_COMPLETION_AUTO_INSTALL"] == "1"
    assert COCO.launch_record(launch)["launcher"] == {
        "kind": "ttadk", "version": launch.profile.runtime.version, "tool": "trae-cli"}


@pytest.mark.parametrize("command", [None, "custom-coco", "custom-coco --flag", "'", "missing-coco"])
def test_coco_wrapper_probe_uses_configured_native_command(stand_ins, command):
    executable, calls, operator = stand_ins
    if command is not None:
        (operator / ".ttadk").mkdir()
        (operator / ".ttadk" / "setting.json").write_text(json.dumps({"ai_tool_commands": {"trae-cli": command}}))
    found = COCO.probe_for(executable, Launcher.TTADK)
    assert found.capabilities == COCO.capabilities
    if command == "missing-coco":
        assert found.version == "ttadk 1.0.37 / unknown" and "not found" in found.error
    else:
        assert found.version == "ttadk 1.0.37 / traecli 0.214.1"
        if command in ("custom-coco --flag", "'"):
            assert "not a single word" in found.error
        else:
            assert found.error is None
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert invocations[0] == ["ttadk", "--version"]
    if command != "missing-coco":
        assert invocations[1] == ["custom-coco" if command == "custom-coco" else "traex", "--version"]


def test_coco_wrapper_preflight_uses_ttadk_then_native_login_and_model(stand_ins):
    executable, calls, _ = stand_ins
    problem = asyncio.run(agent_settings._login_problem(_launch(executable).profile))
    assert problem is None
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert invocations == [["ttadk", "models", "-t", "trae-cli", "-f", "json"],
                           ["traex", "login", "status"], ["traex", "models", "--json"]]


@pytest.mark.parametrize("catalog", [None, {"gpt-5.5": "GPT-5.5"}, {"other": "Other"}])
def test_coco_view_uses_only_the_cached_model_catalog(stand_ins, monkeypatch, catalog):
    executable, calls, _ = stand_ins
    now = time.monotonic()
    monkeypatch.setitem(ttadk._probed, (executable, "trae-cli"), (now, None))
    monkeypatch.setitem(coco_login._probed, "traex", (
        now, coco_login.LoginStatus(coco_login.LoginState.LOGGED_IN, "logged in")))
    if catalog is not None:
        monkeypatch.setitem(coco_login._models, "traex", (now, catalog))
    problem = asyncio.run(agent_settings._login_problem(_launch(executable).profile, wait=False))
    assert bool(problem) is (catalog is not None and "gpt-5.5" not in catalog)
    assert not calls.exists(), "a view started a subprocess"


def test_coco_wrapper_pairing_refuses_api_key_without_gateway_claim(stand_ins):
    launch = _launch()
    provider = registry.LegacyProviderSpec("key", "Key account", AuthKind.API_KEY,
                                          frozenset({ApiProtocol.OPENAI_RESPONSES}))
    with pytest.raises(registry.RegistryError) as failure:
        registry.check_pairing(launch.profile.runtime, provider, "GPT-5.5")
    assert "coco-login" in str(failure.value) and "gateway" not in str(failure.value)


def test_worker_completes_after_coco_wrapper_banners(tmp_path, monkeypatch, stand_ins, agent_db):
    executable, calls, _ = stand_ins
    launch = _launch(executable)
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 10, "max_turns": 0})
    monkeypatch.setattr(control, "_control", control.ProxyControl(tmp_path / "runtime", "", enabled=True))

    async def load(role, *args, **kwargs):
        return await agent_settings._check_profile_settings(launch.settings, role)

    monkeypatch.setattr(worker, "load_agent_settings", load)
    from looper.db import profile_registry
    monkeypatch.setattr(profile_registry, "list_models", AsyncMock(return_value=[]))
    (tmp_path / "work").mkdir()

    async def run():
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        return await pool._execute(_step_envelope(tmp_path / "work", verify=False), is_verify=False)

    result = asyncio.run(asyncio.wait_for(run(), 20))
    assert result["status"] == "completed" and result["summary"] == "finished the step"
    metadata = agent_db.start_record.await_args.kwargs["metadata"]
    assert metadata["profile"]["runtime"]["id"] == "rt-ttadk-coco"
    assert metadata["launcher"]["tool"] == "trae-cli" and metadata["commit_attribution"] == "trae"
    assert metadata["llmrec"]["reason"] == "not_covered"
    assert "TTADK" in metadata["llmrec"]["detail"]
    assert any(json.loads(line)[:2] == ["ttadk", "code"] for line in calls.read_text().splitlines())


def test_coco_wrapper_resident_session_resumes_after_banners(tmp_path, stand_ins):
    executable, calls, _ = stand_ins
    launch = _launch(executable)
    command = COCO.resident_command(executable, cwd=tmp_path,
        prompt_file=SystemPrompt("Instructions").write_file(tmp_path / "runtime"), launch=launch)

    async def run():
        session = COCO.resident_session("planner")

        async def on_event(event):
            pass

        try:
            await session.start(command, cwd=str(tmp_path), signature="same", env=launch.environment())
            first = await session.run_turn("first", on_event)
            second = await session.run_turn("second", on_event)
            assert first.result_event["subtype"] == second.result_event["subtype"] == "success"
        finally:
            await session.stop()

    asyncio.run(asyncio.wait_for(run(), 15))
    commands = [args for line in calls.read_text().splitlines() if (args := json.loads(line))[:2] == ["ttadk", "code"]]
    assert len(commands) == 2 and "resume" not in commands[0]
    thread = next(json.loads(line)["thread_id"] for line in reversed(FIXTURE.read_text().splitlines())
                  if json.loads(line)["type"] == "thread.started")
    assert commands[1][-3:] == ["resume", thread, "-"]


def test_coco_wrapper_failed_sign_in_blocks_native_checks(tmp_path, stand_ins):
    _, calls, _ = stand_ins
    executable = fake_claude(tmp_path / "signed-out-ttadk", "import sys\n"
        "assert sys.argv[1:] == ['models', '-t', 'trae-cli', '-f', 'json']\nraise SystemExit(1)\n")
    problem = asyncio.run(agent_settings._login_problem(_launch(executable).profile))
    assert "sign-in" in problem and not calls.exists()
