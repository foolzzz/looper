"""Native Coco launches and recorded events; all executables and logins are stand-ins."""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents.agent_settings import AgentSettings, AgentType, Launch
from looper.agents.claude_cli import ClaudeRunRecorder
from looper.agents.library import AgentLibrary
from looper.agents.mcp import write_config
from looper.agents.run_usage import Usage
from looper.agents.profile_registry import (
    Endpoint, LegacyAgentProfile, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel, RuntimeSpec,
)
from looper.agents.runtimes import ApiProtocol, AuthKind, Capability, Launcher, RuntimeUnsupported, VendorType, adapter
from looper.agents.runtimes import coco_login
from looper.agents.runtimes.coco import CocoRuntime
from looper.agents.runtimes.codex import _codex_usage
from looper.agents.system_prompt import SystemPrompt

FIXTURE = Path(__file__).parent / "fixtures" / "coco_exec" / "sample.jsonl"
COCO = CocoRuntime()


def _launch(*, model="GPT-5.5", executable="coco", library=None, env=()):
    account = ProviderSpec("prov-coco-login", "coco-login", VendorType.COCO_LOGIN, AuthKind.OAUTH,
                           "coco")
    catalog_model = ModelSpec("model-coco", account.id, model, ApiProtocol.OPENAI_RESPONSES)
    resolved = ResolvedModel(catalog_model, account, Endpoint(account.id, catalog_model.protocol, ""))
    profile = ResolvedAgentProfile("profile-coco", "Coco", RuntimeSpec("rt-coco", "coco", "coco", executable,
                                   COCO.capabilities), catalog_model.id, resolved_model=resolved)
    return Launch(AgentSettings(profile=profile, env=env), library or AgentLibrary())


@pytest.fixture(autouse=True)
def isolated_login(tmp_path, monkeypatch):
    monkeypatch.setattr(coco_login, "login_file", lambda: tmp_path / "operator-login.json")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude(bin_dir / "coco", (
        "import sys\nassert sys.argv[1:] == ['models', '--json']\n"
        "print('[{\"name\": \"GPT-5.5\", \"config_name\": \"gpt-5.5\"}]')\n"))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    coco_login.forget_models()
    yield
    coco_login.forget_models()


def test_recorded_coco_events_keep_tools_warnings_background_completion_and_tokens():
    async def parse():
        reader = asyncio.StreamReader()
        reader.feed_data(FIXTURE.read_bytes())
        reader.feed_eof()
        return [event async for event in adapter("coco").parse(reader, model="GPT-5.5")]

    events = asyncio.run(parse())
    assert [e["runtime"] for e in events if e.get("subtype") == "init"] == ["coco", "coco"]
    calls = [b for e in events if e["type"] == "assistant" for b in e["message"]["content"]
             if b["type"] == "tool_use"]
    results = [b for e in events if e["type"] == "user" for b in e["message"]["content"]]
    assert [(c["name"], c["id"]) for c in calls] == [
        ("shell", "item_0"), ("mcp__probe__probe_echo", "item_1"), ("shell", "item_1"), ("shell", "item_3")]
    assert calls[1]["input"] == {"text": "x"}
    assert [(r["content"], r["is_error"]) for r in results] == [
        ("HOME=/home/user\n", False), ("probe-echo:x", False), ("", False), ("", False)]
    assert results[-1]["tool_use_id"] == "item_3"
    assert len([e for e in events if e.get("subtype") == "codex_warning"]) == 1
    completed = [e for e in events if e["type"] == "result"]
    assert [e["result"] for e in completed] == ["HOME=/home/user probe-echo:x LOOPER-OK.", "done"]
    assert all(e["subtype"] == "success" and not e["is_error"] for e in completed)
    assert completed[0]["modelUsage"]["GPT-5.5"]["inputTokens"] == 65419 - 40960


def test_coco_cache_creation_is_counted_separately_from_uncached_input():
    assert _codex_usage({"input_tokens": 10, "cached_input_tokens": 2,
                         "cache_creation_input_tokens": 3, "output_tokens": 1}) == {
        "inputTokens": 5, "cacheReadInputTokens": 2, "cacheCreationInputTokens": 3, "outputTokens": 1}


def test_coco_tokens_have_no_builtin_price_even_when_the_model_name_matches():
    launch = _launch()
    assert launch.price("GPT-5.5", Usage(input_tokens=100, output_tokens=1)) is None


@pytest.mark.parametrize("read_only", [False, True])
def test_run_uses_its_own_home_config_login_link_and_permissions(tmp_path, read_only):
    library = AgentLibrary(mcp_config=str(write_config(tmp_path, [
        ("probe", {"type": "stdio", "command": "probe-mcp", "args": ["--safe"]})])))
    launch = replace(_launch(library=library), consult=read_only)
    prompt_file = SystemPrompt("COCO INSTRUCTIONS").write_file(tmp_path)
    command = COCO.run_command("coco", cwd=tmp_path, prompt_file=prompt_file, launch=launch)
    home = Path(command[1].removeprefix("TRAE_HOME="))
    assert home.parent == tmp_path / ".local" / "coco" / "runs"
    assert re.fullmatch("[0-9a-f]{64}", home.name)
    permissions = ["-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"'] if read_only else [
        "--dangerously-bypass-approvals-and-sandbox"]
    assert command == ["env", f"TRAE_HOME={home}", "coco", "exec", "--json", "--skip-git-repo-check",
                       *permissions, "-m", "GPT-5.5", "--ephemeral", "-"]
    text = (home / "traecli.toml").read_text()
    config = tomllib.loads(text)
    assert coco_login.CONFIG_MARKER in text.splitlines()
    assert config["check_for_update_on_startup"] is False
    assert config["skills"] == {"include_instructions": False}
    assert config["features"] == {"plugins": False, "apps": False, "memories": False}
    assert config["shell_environment_policy"]["set"] == {
        "TRAE_HOME": str(tmp_path / ".local" / "coco" / "shell-home")}
    assert config["mcp_servers"]["probe"]["command"] == "probe-mcp"
    assert "commit_attribution" not in config
    notes = (home / "AGENTS.md").read_text()
    assert notes.startswith("# Runtime notes (Coco (TRAE CLI))\n")
    assert "Looper runs you through Coco (TRAE CLI)." in notes
    assert "COCO INSTRUCTIONS" in notes
    assert (home / "cli" / "auth.json").is_symlink()
    assert os.readlink(home / "cli" / "auth.json") == str(coco_login.login_file())
    assert launch.environment().get("HOME") == os.environ.get("HOME")


@pytest.mark.parametrize("case,message", [
    ("codex-login", "coco runs with the coco login only"),
    ("api-key", "coco runs with the coco login only"),
    ("effort", "coco takes no effort level"),
    ("protocol", "exact protocol"),
    ("catalog", "one resolved model"),
    ("coco-login", None),
])
def test_provider_binding_refuses_other_accounts_effort_and_unresolved_models(case, message):
    provider = _launch().profile.provider
    if case == "codex-login":
        provider = replace(provider, oauth_runtime_type="codex")
    elif case == "api-key":
        provider = replace(provider, auth=AuthKind.API_KEY, oauth_runtime_type=None, api_key="stand-in-key")
    elif case == "protocol":
        provider = replace(provider, protocols=frozenset({ApiProtocol.OPENAI_CHAT}))
    elif case == "catalog":
        provider = replace(provider, catalog=())
    if message:
        with pytest.raises(RuntimeUnsupported, match=message):
            COCO.bind_provider(provider, "GPT-5.5", "high" if case == "effort" else "")
    else:
        binding = COCO.bind_provider(provider, "GPT-5.5", "")
        assert binding.args == ("-m", "GPT-5.5")
        assert binding.unset == coco_login.ENV_UNSET


@pytest.mark.parametrize("name,source", [("TRAE_HOME", "agent"), ("TRAECLI_HOME", "provider")])
def test_explicit_environment_cannot_replace_loopers_coco_home(name, source):
    setting = ((name, "/untrusted-home"),)
    launch = _launch(env=setting if source == "agent" else ())
    if source == "provider":
        # A legacy projection must be refused too, even without registry input validation.
        profile = launch.profile
        legacy = LegacyAgentProfile(profile.id, profile.name, profile.runtime,
                                    replace(profile.provider, env=setting), profile.model)
        launch = Launch(AgentSettings(profile=legacy))
    with pytest.raises(RuntimeUnsupported, match=name):
        launch.environment()


def test_probe_checks_only_version_in_a_disposable_home(tmp_path):
    recorded = tmp_path / "probed-home"
    executable = fake_claude(tmp_path / "coco", (
        "import os, sys\nfrom pathlib import Path\n"
        "assert sys.argv[1:] == ['--version'], sys.argv\n"
        "home = Path(os.environ['TRAE_HOME'])\nassert list(home.iterdir()) == []\n"
        f"Path({str(recorded)!r}).write_text(str(home))\n"
        "print('traecli 0.214.1')\n"
    ))
    probe = COCO.probe_for(executable, Launcher.NATIVE)
    assert probe.error is None and probe.version == "traecli 0.214.1"
    assert probe.capabilities == {Capability.RUN, Capability.MCP, Capability.ADD_DIRS,
                                  Capability.RESIDENT_SESSION, Capability.RESUME}
    assert not Path(recorded.read_text()).exists()


def test_pairing_and_launch_check_the_selected_executable_catalog_without_case_sensitivity(tmp_path, monkeypatch):
    executable = fake_claude(tmp_path / "coco", (
        "import sys\nassert sys.argv[1:] == ['models', '--json']\n"
        "print('[{\"name\": \"GPT-5.5\", \"config_name\": \"gpt-5.5\"}]')\n"
    ))
    monkeypatch.setattr(coco_login, "launch_problem", AsyncMock(return_value=None))
    for name in ("GPT-5.5", "gpt-5.5"):
        profile = _launch(model=name, executable=executable).profile
        profile.resolved_model.check_runtime(profile.runtime)
        assert asyncio.run(COCO.login_problem(profile.provider, executable=executable)) is None
    profile = _launch(model="GPT-9", executable=executable).profile
    provider = profile.resolved_model.binding_provider()
    with pytest.raises(RuntimeUnsupported, match="GPT-9.*GPT-5.5"):
        COCO.check_pairing(provider, profile.model, executable=executable)
    assert "GPT-9" in asyncio.run(COCO.login_problem(provider, executable=executable))


def test_cold_pairing_never_starts_the_slow_catalog_probe(tmp_path):
    ran = tmp_path / "models-ran"
    executable = fake_claude(tmp_path / "slow-coco", (
        "import sys, time\nfrom pathlib import Path\n"
        "assert sys.argv[1:] == ['models', '--json']\n"
        f"Path({str(ran)!r}).touch()\n"
        "time.sleep(5)\n"
        "print('[{\"name\": \"GPT-5.5\"}]')\n"
    ))
    provider = _launch(model="GPT-9", executable=executable).profile.resolved_model.binding_provider()
    started = time.monotonic()
    COCO.check_pairing(provider, "GPT-9", executable=executable)
    assert time.monotonic() - started < 0.5
    assert not ran.exists()
    assert coco_login.available_models(executable) == {"gpt-5.5": "GPT-5.5"}
    assert ran.exists()
    with pytest.raises(RuntimeUnsupported, match="GPT-9.*GPT-5.5"):
        COCO.check_pairing(provider, "GPT-9", executable=executable)


def test_launch_checks_a_cold_catalog_off_the_event_loop(tmp_path, monkeypatch):
    executable = fake_claude(tmp_path / "coco", "print('[{\"name\": \"GPT-5.5\"}]')\n")
    available = coco_login.available_models
    loop_thread = threading.get_ident()

    def catalog(executable):
        assert threading.get_ident() != loop_thread
        return available(executable)

    monkeypatch.setattr(coco_login, "available_models", catalog)
    monkeypatch.setattr(coco_login, "launch_problem", AsyncMock(return_value=None))
    provider = _launch(model="GPT-9", executable=executable).profile.resolved_model.binding_provider()
    assert "GPT-9" in asyncio.run(COCO.login_problem(provider, executable=executable))


def test_unavailable_model_catalog_does_not_block_pairing(monkeypatch):
    monkeypatch.setattr(coco_login, "available_models", lambda executable: None)
    COCO.check_pairing(_launch(model="GPT-9").profile.provider, "GPT-9")


@pytest.mark.parametrize("method", ["consult_command", "steer_command"])
def test_unsupported_session_operations_refuse_before_creating_a_home(tmp_path, method):
    kwargs = {"cwd": tmp_path, "prompt_file": tmp_path / "unused", "launch": _launch()}
    if method == "consult_command":
        kwargs.update(session_id="session", session_dir=None, fork_dir=tmp_path / "fork")
    with pytest.raises(RuntimeUnsupported):
        getattr(COCO, method)("coco", **kwargs)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bin"]


@pytest.fixture
def resident_coco(tmp_path):
    return fake_claude(tmp_path / "resident-coco", r"""
import json, os, sys
from pathlib import Path
home = Path(os.environ['TRAE_HOME'])
args = sys.argv[1:]
assert args[:3] == ['exec', '--json', '--skip-git-repo-check'], args
assert args[-1] == '-' and '--ephemeral' not in args, args
log = home / 'argv.jsonl'
resumed = 'resume' in args
assert args[-3:] == ['resume', 'T', '-'] if resumed else not log.exists()
with log.open('a') as out:
    out.write(json.dumps(args) + '\n')
message = sys.stdin.read()
if message == 'fork':
    link = home / 'cli' / 'auth.json'
    link.unlink()
    link.write_text('synthetic fork')
for event in [
    {'type': 'thread.started', 'thread_id': 'T'},
    {'type': 'turn.started'},
    {'type': 'item.completed', 'item': {'id': 'answer', 'type': 'agent_message', 'text': message}},
    {'type': 'turn.completed', 'usage': {'input_tokens': 37731 if resumed else 14923,
                                        'output_tokens': 10 if resumed else 5}},
]:
    print(json.dumps(event), flush=True)
""")


def test_resident_turns_resume_in_a_private_home_and_count_only_their_own_tokens(tmp_path, resident_coco):
    launch = _launch(executable=resident_coco)
    command = COCO.resident_command(resident_coco, cwd=tmp_path,
                                   prompt_file=SystemPrompt("Resident instructions").write_file(tmp_path), launch=launch)
    run_home = Path(command[1].removeprefix("TRAE_HOME="))
    home = tmp_path / ".local" / "coco" / "sessions" / "manager"

    async def run():
        session = COCO.resident_session("manager")
        assert session.runtime_label == "Coco (TRAE CLI)" and not session.can_interrupt
        events = []

        async def on_event(event):
            events.append(event)

        await session.start(command, cwd=str(tmp_path), signature="first", env=launch.environment())
        try:
            first = await session.run_turn("first", on_event)
            second = await session.run_turn("second", on_event)
            # Restore the saved thread and totals, as after a prompt change or service restart.
            await session.stop()
            await session.start(command + COCO.resume_args("T"), cwd=str(tmp_path), signature="second",
                                env=launch.environment(), totals=session.totals)
            restored = await session.run_turn("restored", on_event)
            assert restored.session_started and restored.result_event["result"] == "restored"
            return first, second, events
        finally:
            await session.stop()

    first, second, events = asyncio.run(run())
    assert all(turn.result_event["subtype"] == "success" for turn in (first, second))
    assert [first.result_event["result"], second.result_event["result"]] == ["first", "second"]
    assert first.usage == Usage(input_tokens=14923, output_tokens=5, cache_read_tokens=0, cache_creation_tokens=0)
    assert second.usage == Usage(input_tokens=22808, output_tokens=5, cache_read_tokens=0, cache_creation_tokens=0)
    assert [event["runtime"] for event in events if event.get("subtype") == "init"] == ["coco"] * 3
    argv = [json.loads(line) for line in (home / "argv.jsonl").read_text().splitlines()]
    assert argv == [command[3:] + ["-"], *[command[3:] + ["resume", "T", "-"]] * 2]
    for name in ("traecli.toml", "AGENTS.md"):
        assert (home / name).read_text() == (run_home / name).read_text()
    assert (home / "cli" / "auth.json").is_symlink()
    assert os.readlink(home / "cli" / "auth.json") == str(coco_login.login_file())


def test_planner_coco_launch_approves_its_mcp_server(tmp_path):
    library = AgentLibrary(mcp_config=str(write_config(tmp_path, [
        ("looper", {"type": "stdio", "command": "planner-mcp"})])))
    launch = replace(_launch(library=library), role=AgentType.PLANNER)
    command = COCO.resident_command("coco", cwd=tmp_path,
                                   prompt_file=SystemPrompt("Planner").write_file(tmp_path), launch=launch)
    home = Path(command[1].removeprefix("TRAE_HOME="))
    assert tomllib.loads((home / "traecli.toml").read_text())["mcp_servers"]["looper"][
        "default_tools_approval_mode"] == "approve"


def test_finishing_each_resident_turn_repairs_and_reports_a_forked_login(tmp_path, monkeypatch, resident_coco):
    record = AsyncMock(return_value="login link repaired")
    monkeypatch.setattr(coco_login, "record_problems", record)
    launch = _launch(executable=resident_coco)
    command = COCO.resident_command(resident_coco, cwd=tmp_path,
                                   prompt_file=SystemPrompt("Manager").write_file(tmp_path), launch=launch)
    home = tmp_path / ".local" / "coco" / "sessions" / "manager"

    async def run():
        session = COCO.resident_session("manager")
        await session.start(command, cwd=str(tmp_path), signature="s", env=launch.environment())
        try:
            for _ in range(2):
                recorder = ClaudeRunRecorder(agent_id="manager", task_id=None, command=command,
                                            cwd=str(tmp_path), input_text="fork", launch=launch)
                turn = await session.run_turn("fork", recorder.record)
                assert turn.result_event["subtype"] == "success"
                assert not (home / "cli" / "auth.json").is_symlink()
                await recorder.finish(output_text="fork", result_event=turn.result_event, usage=turn.usage)
                assert (home / "cli" / "auth.json").is_symlink()
                assert os.readlink(home / "cli" / "auth.json") == str(coco_login.login_file())
        finally:
            await session.stop()

    asyncio.run(run())
    assert record.await_count == 2
