"""Task Manager operator launch and the review-hook launch boundary."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from looper import child_env
from looper.agents.agent_settings import Access, AgentSettings, Launch
from looper.agents.outbound_hook import ToolHook
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.runtimes import CLAUDE_CODE


def launch(**fields):
    return Launch(AgentSettings(profile=builtin_resolved_profile()), **fields)


def test_operator_command_golden(tmp_path):
    settings_dir, prompt = tmp_path / "settings", tmp_path / "prompt.md"
    run = launch(tool_hook=ToolHook("review-action"))
    command = CLAUDE_CODE.operator_resident_command(
        "claude", cwd=tmp_path, prompt_file=prompt, launch=run, settings_dir=settings_dir)
    settings_file = command[command.index("--settings") + 1]
    assert command == [
        "claude", "--print", "--verbose", "--output-format", "stream-json",
        "--input-format", "stream-json", "--include-partial-messages", "--replay-user-messages",
        "--strict-mcp-config", "--settings", settings_file, "--append-system-prompt-file", str(prompt),
        "--system-prompt-snapshot", "off", "--dangerously-skip-permissions",
        "--disallowedTools", "ListAgents,SendMessage", "--model", run.model,
        *(["--effort", run.effort] if run.effort else []),
    ]
    assert json.loads(Path(settings_file).read_text()) == {
        "crossSessionInbound": "refuse", "hooks": run.tool_hook.settings()}
    assert Path(settings_file).stat().st_mode & 0o777 == 0o600
    assert settings_dir.stat().st_mode & 0o777 == 0o700
    library = replace(run.library, mcp_config="servers.json")
    with_library = CLAUDE_CODE.operator_resident_command(
        "claude", cwd=tmp_path, prompt_file=prompt, launch=replace(run, library=library), settings_dir=settings_dir)
    assert with_library == [*command, "--mcp-config", "servers.json"]
    assert "--strict-mcp-config" in with_library


def test_read_only_command(tmp_path):
    command = CLAUDE_CODE.resident_command("claude", cwd=tmp_path, prompt_file=tmp_path / "prompt.md",
                                           launch=launch(access=Access.READ_ONLY))
    assert "--setting-sources" in command
    assert "--strict-mcp-config" in command
    assert "Bash,Edit,Write,NotebookEdit,EnterWorktree" in command
    assert "--dangerously-skip-permissions" not in command


def test_environment(monkeypatch):
    from looper.agents import task_manager_safety as safety

    monkeypatch.setenv("SERVER_PRIVATE", "must-not-leak")
    monkeypatch.setenv(child_env.AGENT_RUN_ENV, "manager")
    run = launch()
    run = replace(run, settings=replace(run.settings, env=(
        ("CLAUDE_CODE_SAFE_MODE", "1"), ("CLAUDE_CODE_SIMPLE", "1"), (child_env.AGENT_RUN_ENV, "bad"))))
    env = safety.environment(run, "task-1")
    assert env[child_env.TASK_MANAGER_ENV] == "task-1"
    assert child_env.AGENT_RUN_ENV not in env
    assert "SERVER_PRIVATE" not in env
    assert "CLAUDE_CODE_SAFE_MODE" not in env and "CLAUDE_CODE_SIMPLE" not in env
    assert env == {**{key: value for key, value in run.runtime.environment(run).items()
                     if key not in (*safety.HOOK_ENV, child_env.AGENT_RUN_ENV)},
                   child_env.TASK_MANAGER_ENV: "task-1"}


@pytest.mark.parametrize("source", ["user", "user_local", "project", "project_local", "managed", "launch"])
@pytest.mark.parametrize("phase", ["launch", "resume"])
def test_hook_disabled_sources(tmp_path, monkeypatch, source, phase):
    from looper.agents import task_manager_safety as safety

    home, workspace = tmp_path / "home", tmp_path / "project"
    sources = {"user": home / ".claude/settings.json", "user_local": home / ".claude/settings.local.json",
               "project": workspace / ".claude/settings.json", "project_local": workspace / ".claude/settings.local.json",
               "managed": tmp_path / "managed.json", "launch": tmp_path / "launch.json"}
    path = sources[source]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"disableAllHooks":true}')
    monkeypatch.setattr(safety, "MANAGED_SETTINGS", (sources["managed"],))
    before = sum(safety.refusal_counts.values())
    result = asyncio.run(safety.gate("not-executed", cwd=workspace, environment={"HOME": str(home)},
                                     phase=phase, settings_file=sources["launch"]))
    assert result.reason.value == "review_hook_disabled"
    assert str(path) in result.notice and "disableAllHooks" in result.notice
    assert "remove" in result.notice
    assert sum(safety.refusal_counts.values()) == before + 1


@pytest.mark.parametrize("key,value", [("allowManagedHooksOnly", True), ("strictPluginOnlyCustomization", ["hooks"])])
def test_managed_static_settings(tmp_path, monkeypatch, key, value):
    from looper.agents import task_manager_safety as safety

    path = tmp_path / "managed.json"
    path.write_text(json.dumps({key: value}))
    monkeypatch.setattr(safety, "MANAGED_SETTINGS", (path,))
    result = asyncio.run(safety.gate("not-executed", cwd=tmp_path, environment={"HOME": str(tmp_path)}, phase="launch"))
    assert result.reason.value == "review_hook_disabled" and key in result.notice
    assert any(rule.statically_confirmed and rule.key == (key,) for rule in safety.RULES)


@pytest.mark.parametrize("name", ["CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE"])
@pytest.mark.parametrize("source", ["user", "launch", "process", "environment"])
def test_hook_disabling_environment(tmp_path, monkeypatch, name, source):
    from looper.agents import task_manager_safety as safety

    monkeypatch.setattr(safety, "MANAGED_SETTINGS", ())
    home = tmp_path / "home"
    env = {"HOME": str(home)}
    settings_file = tmp_path / "launch.json"
    if source in ("user", "launch"):
        path = home / ".claude/settings.json" if source == "user" else settings_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"env": {name: "1"}}))
    elif source == "process":
        monkeypatch.setenv(name, "1")
    else:
        env[name] = "1"
    result = asyncio.run(safety.gate("not-executed", cwd=tmp_path, environment=env, phase="launch",
                                     settings_file=settings_file))
    assert result.reason.value == "review_hook_disabled" and name in result.notice


@pytest.mark.parametrize("version,allowed", [("2.1.331", False), ("2.1.332", True), ("2.1.333", False), ("invalid", False)])
def test_version_gate(tmp_path, monkeypatch, version, allowed):
    from fake_claude import fake_claude
    from looper.agents import task_manager_safety as safety

    monkeypatch.setattr(safety, "MANAGED_SETTINGS", ())
    command = fake_claude(tmp_path / "claude", f"print({(version + ' (Claude Code)')!r})\n")
    result = asyncio.run(safety.gate(command, cwd=tmp_path, environment={**child_env.inherited(), "HOME": str(tmp_path)},
                                     phase="launch"))
    assert (result is None) == allowed
    if result:
        assert result.reason.value == "cli_unverified" and version in result.notice


def test_mid_session_settings_caught_on_resume(tmp_path, monkeypatch):
    from fake_claude import fake_claude
    from looper.agents import task_manager_safety as safety

    monkeypatch.setattr(safety, "MANAGED_SETTINGS", ())
    command = fake_claude(tmp_path / "claude", "print('2.1.332 (Claude Code)')\n")
    env = {**child_env.inherited(), "HOME": str(tmp_path)}
    assert asyncio.run(safety.gate(command, cwd=tmp_path, environment=env, phase="launch")) is None
    project_settings = tmp_path / ".claude/settings.local.json"
    project_settings.parent.mkdir()
    project_settings.write_text('{"disableAllHooks":true}')
    result = asyncio.run(safety.gate(command, cwd=tmp_path, environment=env, phase="resume"))
    assert result.reason.value == "review_hook_disabled"


def test_unreadable_settings_fail_closed(tmp_path, monkeypatch):
    from looper.agents import task_manager_safety as safety

    monkeypatch.setattr(safety, "MANAGED_SETTINGS", ())
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir()
    path.write_text("not-json")
    result = asyncio.run(safety.gate("not-executed", cwd=tmp_path, environment={"HOME": str(tmp_path)}, phase="launch"))
    assert result.reason.value == "review_hook_disabled" and str(path) in result.notice


@pytest.mark.installed
def test_real_cli_direct_turn_saves_session(tmp_path, monkeypatch):
    """Run the real task-owned launch against a loopback script, never a paid model."""
    binary = os.environ.get("LOOPER_CLAUDE_PROBE_BIN")
    if not binary:
        pytest.skip("Set LOOPER_CLAUDE_PROBE_BIN for the offline real-CLI task Manager check; a skip is not a pass")
    from fake_anthropic_messages import FakeAnthropicMessages, text, tool_use
    from looper.agents import claude_cli, outbound_hook, task_conversations_settings, task_manager_settings
    from looper.agents.tiers.task_manager import TaskManagerPool
    from looper.db import agent_sessions, projects, tasks
    from tests.db_support import run_db

    binary = str(Path(binary).resolve())
    home, config, workspace = tmp_path / "home", tmp_path / "config", tmp_path / "project"
    for path in (home, config, workspace):
        path.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
    server = FakeAnthropicMessages()
    url, requests = server.start([tool_use("Bash", {"command": "printf probe > result.txt"}), text("done")])
    hook_script = tmp_path / "hook.py"
    hook_script.write_text("import json,pathlib,sys\njson.load(sys.stdin)\npathlib.Path(sys.argv[1]).write_text('fired')\n")

    def hook(name):
        return ToolHook(" ".join(shlex.quote(part) for part in (sys.executable, str(hook_script), str(tmp_path / name))))

    config.joinpath("settings.json").write_text(json.dumps({
        "hooks": hook("user-hook").settings(), "claudeMdExcludes": claude_cli._outside_memory(workspace)}))
    env = {"PATH": os.defpath, "HOME": str(home), "CLAUDE_CONFIG_DIR": str(config),
           "ANTHROPIC_BASE_URL": url, "ANTHROPIC_API_KEY": "sk-probe-not-real",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
           "DISABLE_TELEMETRY": "1", "NO_PROXY": "127.0.0.1,localhost"}
    # Runtime environment construction is covered above; this installed test must never inherit credentials.
    monkeypatch.setattr(CLAUDE_CODE, "environment", lambda launch: dict(env))
    monkeypatch.setattr(outbound_hook, "known_enabled", lambda: True)
    monkeypatch.setattr(task_conversations_settings, "_enabled", True)
    monkeypatch.setattr(task_conversations_settings, "_manager_answers", True)

    async def settings(task):
        return AgentSettings(profile=builtin_resolved_profile())

    monkeypatch.setattr(task_manager_settings, "load_task_manager_settings", settings)

    class Ops:
        def issue(self, task_id):
            return "probe-token"

        async def library_with_server(self, library, runtime_root, token):
            return library

        def tool_hook(self, *args, **kwargs):
            return hook("launch-hook")

        def begin_turn(self, token): pass
        def end_turn(self, token): pass
        def revoke(self, token): pass

    class Sink:
        def __init__(self):
            self.done = asyncio.Event()
            self.outcome = None

        async def delta(self, value): pass
        async def tool(self, record): pass
        async def event(self, kind, payload): pass
        async def final(self, value, outcome):
            self.outcome = outcome
            self.done.set()

    async def check(db):
        project = await projects.create_project(name="Offline probe", work_dir=str(workspace))
        await tasks.create_task(task_id="probe", requirement="Run the offline probe", project_id=project["id"],
                                status="draft", work_dir=str(workspace), workspace_dir=str(workspace),
                                workspace_mode="in_place")
        pool = TaskManagerPool(tmp_path / "runtime", binary, ops=Ops(), max_resident=lambda: 1)
        sink = Sink()
        try:
            submitted = await pool.submit("probe", "Run the offline probe", message_id="probe-message", sink=sink)
            assert submitted.state == "started"
            await asyncio.wait_for(sink.done.wait(), timeout=90)
            session = await agent_sessions.get_session("task-manager-probe")
            assert session is not None and session["session_id"]
            assert workspace.joinpath("result.txt").read_text() == "probe"
            assert tmp_path.joinpath("user-hook").read_text() == "fired"
            assert tmp_path.joinpath("launch-hook").read_text() == "fired"
            assert requests and all(request["api_key"] == "sk-probe-not-real" for request in requests)
        finally:
            await pool.shutdown()

    try:
        run_db(check, monkeypatch)
    finally:
        server.stop()
