"""Cursor's native Worker runtime, using recorded events and stand-in executables only."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fake_claude import fake_claude
from looper import child_env
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.knowledge import AgentKnowledge
from looper.agents.library import AgentLibrary
from looper.agents.profile_registry import (
    Endpoint, LegacyAgentProfile, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel, RuntimeSpec,
)
from looper.agents.run_usage import Usage, run_usage
from looper.agents.runtimes import ApiProtocol, AuthKind, CostSource, RuntimeUnsupported, VendorType, adapter, cursor_agent, cursor_login
from looper.agents.runtimes.cursor_agent import (
    HOME_LAYOUT_ID, INSTRUCTION_NAMES, PERSONAL_CONFIG, PLUGIN_DIR, RULES_FILE,
    CursorAgentRuntime, CursorStream, mcp_servers, runtime_notes, write_run_dir,
)
from looper.agents.system_prompt import SystemPrompt
from looper.agents.tiers import worker as worker_module
from looper.agents.tiers.worker import WorkerConfig, WorkerPool
from test_agent_planner_worker import _step_envelope, _worker_row


CURSOR: CursorAgentRuntime = adapter("cursor-agent")  # type: ignore[assignment]
MODEL = "gpt-5.3-codex"
SESSION = "00000000-0000-4000-8000-000000000001"
FIXTURE = Path(__file__).parent / "fixtures" / "cursor-stream-sample.jsonl"


def _launch(*, executable: str = "cursor-agent", library: AgentLibrary | None = None,
            env: tuple[tuple[str, str], ...] = (), provider_env: tuple[tuple[str, str], ...] = (),
            effort: str = "") -> Launch:
    provider = ProviderSpec("prov-cursor-login", "Cursor login", VendorType.CURSOR_LOGIN,
                            AuthKind.OAUTH, "cursor-agent")
    model = ModelSpec("model-cursor", provider.id, MODEL, ApiProtocol.OPENAI_RESPONSES)
    resolved = ResolvedModel(model, provider, Endpoint(provider.id, model.protocol, ""))
    profile = ResolvedAgentProfile("agent-cursor", f"cursor-agent/{MODEL}",
                                   RuntimeSpec("rt-cursor", "Cursor Agent", "cursor-agent", executable,
                                               CURSOR.capabilities), model.id, effort or None,
                                   resolved_model=resolved)
    if provider_env:
        # Adapter inputs can predate the registry's credential validation; its launch gate must still refuse them.
        profile = LegacyAgentProfile(profile.id, profile.name, profile.runtime,
                                     dataclasses.replace(resolved.binding_provider(), env=provider_env), MODEL)
    return Launch(AgentSettings(profile=profile, env=env), library or AgentLibrary(), AgentKnowledge())


def _prompt(root: Path, text: str = "LOOPER PROMPT") -> Path:
    return SystemPrompt(text).write_file(root)


def _run_dir(command: list[str]) -> Path:
    return Path(command[1].removeprefix("CURSOR_CONFIG_DIR=")).parent


def _blocks(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [block for event in events if event["type"] in ("assistant", "user")
            for block in event["message"]["content"] if block["type"] == kind]


def _lines(*events: dict[str, Any]) -> bytes:
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode()


@pytest.mark.parametrize("stream", ["recorded", "routed", "mcp"])
def test_cursor_stream_preserves_events_and_uses_only_the_final_attempt(stream: str) -> None:
    if stream == "recorded":
        data = FIXTURE.read_bytes()
    elif stream == "routed":
        routed = "> Auto routed to Grok 4.7\n\npong"
        data = _lines({"type": "system", "subtype": "init", "session_id": SESSION},
                      {"type": "assistant", "message": {"content": [{"type": "text", "text": routed}]}},
                      {"type": "result", "result": routed})
    else:
        call = {"mcpToolCall": {"args": {"providerIdentifier": "looper", "toolName": "looper_tasks",
                                         "args": {"action": "list"}},
                                "result": {"success": {"content": [{"type": "text", "text": "tasks"}]}}}}
        data = _lines({"type": "tool_call", "subtype": "started", "call_id": "mcp-1", "tool_call": call},
                      {"type": "tool_call", "subtype": "completed", "call_id": "mcp-1", "tool_call": call})

    async def parse() -> list[dict[str, Any]]:
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return [event async for event in CURSOR.parse(reader, model=MODEL)]

    events = asyncio.run(parse())
    if stream == "mcp":
        (tool,) = _blocks(events, "tool_use")
        assert (tool["name"], tool["input"]) == ("mcp__looper__looper_tasks", {"action": "list"})
        (result,) = _blocks(events, "tool_result")
        assert result["content"] == "tasks" and result["is_error"] is False
        return
    result = events[-1]
    assert result["type"] == "result" and result["routed_model"] == "Grok 4.7"
    assert result["result"] == ("pong HOME=/home/user" if stream == "recorded" else "pong")
    assert "usage" not in result
    assert events[-2]["type"] == "usage"
    if stream == "routed":
        return
    init = [event for event in events if event.get("subtype") == "init"]
    assert len(init) == 1 and init[0]["runtime"] == "cursor-agent" and init[0]["session_id"] == SESSION
    assert all(event.get("session_id") == SESSION for event in events)
    assert len(_blocks(events, "thinking")) == 3
    (tool,) = _blocks(events, "tool_use")
    assert tool["name"] == "shell" and tool["input"]["command"] == "echo HOME=$HOME"
    (tool_result,) = _blocks(events, "tool_result")
    assert (tool_result["tool_use_id"], tool_result["content"], tool_result["is_error"]) == (
        tool["id"], "HOME=/home/user\n", False)
    assert len([event for event in events if event["type"] == "user"]) == 1
    for subtype, phases in (("cursor_connection", ["reconnecting", "reconnected"]),
                            ("cursor_retry", ["starting", "resuming"])):
        for phase in phases:
            assert len([event for event in events if event.get("subtype") == subtype
                        and event["phase"] == phase]) == 2
    assert "endpoint_url" not in json.dumps(events)
    (usage,) = [Usage.from_dict(event["usage"]) for event in events if event["type"] == "usage"]
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_creation_tokens) == (
        95, 8, 19584, 0)
    assert run_usage(result) == usage


def test_command_materializes_private_plugin_and_reuses_only_the_same_prompt(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"looper": {"command": "python", "args": ["-m", "looper"]}}}))
    library = AgentLibrary(plugin_dirs=("/plugins/one", "/plugins/two"), mcp_config=str(config))
    launch = dataclasses.replace(_launch(library=library), knowledge=AgentKnowledge(add_dirs=("/knowledge", "/knowledge")))
    prompt = _prompt(tmp_path / "runtime")
    command = CURSOR.run_command("cursor-agent", cwd=tmp_path, prompt_file=prompt, launch=launch,
                                 session_dir=tmp_path / "unused-session")
    run = _run_dir(command)
    assert command == ["env", f"CURSOR_CONFIG_DIR={run}/config", f"CURSOR_DATA_DIR={run}/data", "cursor-agent",
                       "-p", "--output-format", "stream-json", "--force", "--trust", "--sandbox", "disabled",
                       "--disable-auto-update", "--model", MODEL, "--plugin-dir", f"{run}/looper-plugin",
                       "--plugin-dir", "/plugins/one", "--plugin-dir", "/plugins/two", "--add-dir", "/knowledge"]
    assert not any(arg.startswith("HOME=") for arg in command)
    assert not (tmp_path / "unused-session").exists()
    plugin = run / PLUGIN_DIR
    files = {str(path.relative_to(plugin)): path.read_text() for path in plugin.rglob("*") if path.is_file()}
    assert json.loads(files[".cursor-plugin/plugin.json"]) == {
        "name": "looper", "version": "1", "description": "Looper instructions and platform tools"}
    assert files[RULES_FILE].startswith("---\ndescription: Looper instructions\nalwaysApply: true\n---\n\n")
    assert runtime_notes(["looper"]) in files[RULES_FILE] and files[RULES_FILE].endswith("LOOPER PROMPT")
    assert json.loads(files[".mcp.json"])["mcpServers"]["looper"]["command"] == "python"
    key = hashlib.sha256((HOME_LAYOUT_ID + "\n" + "\0".join(
        f"{path}\0{text}" for path, text in sorted(files.items()))).encode()).hexdigest()
    assert run.name == key and run.parent == (tmp_path / "runtime/.local/cursor-agent/runs").absolute()
    for path in (run.parent.parent, run / "config", run / "data"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert all(stat.S_IMODE((plugin / name).stat().st_mode) == 0o600 for name in files)
    # Cached runs restore generated instructions, including files the CLI removed.
    (plugin / RULES_FILE).write_text("changed")
    (plugin / ".mcp.json").unlink()
    (run / "config").rmdir()
    again = CURSOR.run_command("cursor-agent", cwd=tmp_path, prompt_file=prompt, launch=launch)
    assert _run_dir(again) == run and (run / "config").is_dir()
    assert {name: (plugin / name).read_text() for name in files} == files
    other = CURSOR.run_command("cursor-agent", cwd=tmp_path, prompt_file=_prompt(tmp_path / "runtime", "OTHER"), launch=launch)
    assert _run_dir(other) != run


def test_run_directory_preserves_cli_state_when_restoring_generated_files(tmp_path: Path) -> None:
    files = {RULES_FILE: "instructions", ".cursor-plugin/plugin.json": "{}"}
    run = write_run_dir(tmp_path, files)
    state = run / "data" / "chat.json"
    state.write_text("a finished chat")
    (run / PLUGIN_DIR / RULES_FILE).unlink()
    assert write_run_dir(tmp_path, dict(reversed(list(files.items())))) == run
    assert state.read_text() == "a finished chat" and (run / PLUGIN_DIR / RULES_FILE).read_text() == "instructions"


def test_stream_recovers_missing_tool_starts_and_clears_retried_reports() -> None:
    stream = CursorStream()

    def assistant(text: str) -> dict[str, Any]:
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}

    stream.translate(assistant("> Auto routed to first\n\nstale"))
    completed = {"type": "tool_call", "subtype": "completed", "call_id": "orphan",
                 "tool_call": {"shellToolCall": {"args": {"command": "false"},
                                                "result": {"success": {"stdout": "out", "stderr": "err", "exitCode": 1}}}}}
    events = stream.translate(completed)
    (tool,) = _blocks(events, "tool_use")
    (result,) = _blocks(events, "tool_result")
    assert tool["name"] == "shell" and result["is_error"] is True and result["content"] == "out\nerr"
    for call in ({}, {"readToolCall": {"args": {"path": "a"}}, "editToolCall": {"args": {"path": "b"}}}):
        (event,) = stream.translate({"type": "tool_call", "subtype": "started", "call_id": "unknown", "tool_call": call})
        block = event["message"]["content"][0]
        assert (block["name"], block["input"]) == ("tool", {})
    stream.translate(assistant("discard this attempt"))
    stream.translate({"type": "retry", "subtype": "resuming", "attempt": 1})
    stream.translate(assistant("> Auto routed to second\n\nfinal"))
    usage, result = stream.translate({"type": "result", "usage": {"inputTokens": -1, "outputTokens": 2}})
    assert result["result"] == "final" and result["routed_model"] == "first"
    assert result["modelUsage"] == {"cursor-agent": {"inputTokens": 0, "outputTokens": 2,
                                                   "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}
    assert usage["type"] == "usage"
    (unknown,) = stream.translate({"type": "future", "details": "kept"})
    assert unknown["subtype"] == "cursor_event" and unknown["event"]["details"] == "kept"


def test_mcp_transports_are_converted_without_injecting_home(tmp_path: Path) -> None:
    source = {"stdio": {"command": "python", "args": ["server.py"], "env": {"VALUE": "1"}},
              "http": {"type": "http", "url": "https://mcp.invalid", "headers": {"X-Test": "1"}},
              "sse": {"type": "sse", "url": "https://sse.invalid", "headers": {}},
              "other": {"type": "websocket"}, "unknown": {}}
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": source}))
    servers, skipped = mcp_servers(AgentLibrary(mcp_config=str(config)))
    assert servers == {"stdio": source["stdio"],
                       "http": {"url": "https://mcp.invalid", "headers": {"X-Test": "1"}}, "sse": source["sse"]}
    assert skipped == ["MCP server other: Cursor Agent has no websocket transport",
                       "MCP server unknown: Cursor Agent has no unknown transport"]
    assert mcp_servers(AgentLibrary()) == ({}, [])
    record = CURSOR.launch_record(_launch(library=AgentLibrary(mcp_config=str(config))))
    assert record == {"personal_config": list(PERSONAL_CONFIG),
                      "runtime_catalog": {"runtime": "cursor-agent", "skipped": skipped}}
    assert "MCP server `looper`" not in runtime_notes([])


@pytest.mark.parametrize("source", ["agent", "provider", "inherited"])
def test_environment_keeps_operator_home_and_rejects_login_overrides(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    inherited = {"HOME": str(tmp_path), **{name: "poison" for name in (*cursor_login.ENV_UNSET, *cursor_login.DIRECTORY_VARS)}}
    monkeypatch.setattr(child_env, "inherited", lambda: inherited)
    name = "CURSOR_AUTH_TOKEN" if source == "agent" else "AGENT_CLI_CREDENTIAL_STORE"
    launch = _launch(env=((name, "poison"),) if source == "agent" else (),
                     provider_env=((name, "poison"),) if source == "provider" else ())
    if source != "inherited":
        with pytest.raises(RuntimeUnsupported, match=name):
            CURSOR.environment(launch)
    else:
        assert CURSOR.environment(launch) == {"HOME": os.environ["HOME"], "NO_OPEN_BROWSER": "1"}
    assert CURSOR.provider_env_owned == frozenset()


@pytest.mark.parametrize("invalid", ["auth", "runtime", "protocols", "catalog", "empty", "model", "effort"])
def test_binding_refuses_incompatible_providers_before_writing(invalid: str, tmp_path: Path) -> None:
    provider = _launch().profile.provider
    if invalid == "auth":
        provider = dataclasses.replace(provider, auth=AuthKind.API_KEY)
    elif invalid == "runtime":
        provider = dataclasses.replace(provider, oauth_runtime_type="grok")
    elif invalid == "protocols":
        provider = dataclasses.replace(provider, protocols=frozenset({ApiProtocol.OPENAI_CHAT}))
    elif invalid in ("catalog", "empty"):
        provider = dataclasses.replace(provider, catalog=provider.catalog * 2 if invalid == "catalog" else ())
    with pytest.raises(RuntimeUnsupported):
        CURSOR.bind_provider(provider, "other" if invalid == "model" else MODEL, "high" if invalid == "effort" else "")
    prompt = _prompt(tmp_path / "runtime")
    bad = SimpleNamespace(profile=SimpleNamespace(provider=provider), model=MODEL, effort="high")
    with pytest.raises(RuntimeUnsupported):
        CURSOR.run_command("cursor-agent", cwd=tmp_path, prompt_file=prompt, launch=bad)
    assert not (tmp_path / "runtime/.local/cursor-agent").exists()


def test_turn_writes_utf8_and_closes_stdin_before_rejecting_another_turn(tmp_path: Path) -> None:
    capture = tmp_path / "stdin.txt"
    executable = fake_claude(tmp_path / "cursor-agent", f"import sys, pathlib\npathlib.Path({str(capture)!r}).write_text(sys.stdin.read())")

    async def run() -> None:
        process = await asyncio.create_subprocess_exec(executable, stdin=asyncio.subprocess.PIPE)
        try:
            assert process.stdin is not None
            await CURSOR.write_turn(process.stdin, "一次 prompt")
            with pytest.raises(RuntimeError, match="Cursor Agent reads one prompt per run: it takes no user turn while it runs"):
                await CURSOR.write_turn(process.stdin, "second")
            assert await process.wait() == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(asyncio.wait_for(run(), 10))
    assert capture.read_text() == "一次 prompt"


@pytest.mark.parametrize("mode", ["version", "empty", "error", "missing", "timeout"])
def test_probe_uses_throwaway_directories_and_preserves_operator_home(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "operator"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    capture = tmp_path / "probe.json"
    executable = fake_claude(tmp_path / "cursor-agent", f"""
import json, os, pathlib, sys
names = ('HOME', 'CURSOR_CONFIG_DIR', 'CURSOR_DATA_DIR', 'NO_OPEN_BROWSER')
env = {{name: os.environ[name] for name in names}}
assert all(pathlib.Path(env[name]).is_dir() for name in ('CURSOR_CONFIG_DIR', 'CURSOR_DATA_DIR'))
pathlib.Path({str(capture)!r}).write_text(json.dumps({{'argv': sys.argv[1:], 'env': env}}))
if {mode!r} == 'version': print('2026.10.22')
if {mode!r} == 'error': print('boom', file=sys.stderr); raise SystemExit(1)
if {mode!r} == 'timeout':
    import time
    time.sleep(2)
""")
    assert cursor_agent._PROBE_TIMEOUT_SEC == 10.0
    if mode == "timeout":
        monkeypatch.setattr(cursor_agent, "_PROBE_TIMEOUT_SEC", 0.2)
    if mode == "missing":
        Path(executable).unlink()
    result = CURSOR.probe(executable)
    assert result.capabilities == CURSOR.capabilities
    if mode == "missing":
        assert result.error and "was not found" in result.error
        return
    record = json.loads(capture.read_text())
    assert record["argv"] == ["--version"] and record["env"]["HOME"] == str(home)
    assert record["env"]["NO_OPEN_BROWSER"] == "1" and list(home.iterdir()) == []
    assert all(not Path(record["env"][name]).exists() for name in cursor_login.DIRECTORY_VARS)
    if mode == "version":
        assert result.version == "2026.10.22" and result.error is None
    elif mode == "timeout":
        assert result.version is None and result.error and "TimeoutExpired" in result.error
    else:
        assert result.version is None and result.error and "failed" in result.error


@pytest.mark.parametrize(("name", "arguments", "summary"), [
    ("shell", {"command": "x" * 100}, "Running: " + "x" * 80),
    ("read", {"path": "a.py"}, "Reading: a.py"),
    ("edit", {"path": "a.py"}, "Editing: a.py"),
    ("write", {"path": "a.py"}, "Editing: a.py"),
    ("grep", {"pattern": "x" * 100}, "grep: " + "x" * 60),
    ("glob", {"pattern": "*.py"}, "glob: *.py"),
    ("mcp__looper__looper_tasks", {}, "Tool: mcp__looper__looper_tasks"),
])
def test_tool_activity_summaries(name: str, arguments: dict[str, Any], summary: str) -> None:
    assert CURSOR.tool_summary(name, arguments) == summary


@pytest.mark.parametrize("mode", ["recorded-report", "worker-json", "reviewer-json", "stderr-without-result"])
def test_worker_runs_native_cursor_with_stdin_and_records_configuration_and_routing(
    mode: str, agent_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "operator"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    capture = tmp_path / "run.json"
    verify = mode == "reviewer-json"
    stream = FIXTURE.read_text()
    final = "pong HOME=/home/user"
    if mode in ("worker-json", "reviewer-json"):
        report = {"passed": True, "findings": [], "summary": final} if verify else {
            "status": "completed", "changed_files": {}, "summary": final}
        final = json.dumps(report)
        events = [json.loads(line) for line in stream.splitlines()]
        assistant = next(event for event in reversed(events) if event["type"] == "assistant")
        assistant["message"]["content"] = [{"type": "text", "text": final}]
        stream = "\n".join(json.dumps(event) for event in events) + "\n"
    body = f"""
import json, os, pathlib, sys
if sys.argv[1:] == ['status', '--format', 'json']:
    print('{{"status":"authenticated","isAuthenticated":true}}')
    raise SystemExit(0)
args = iter(sys.argv[1:])
for arg in args:
    if arg in ('--output-format', '--sandbox', '--model', '--plugin-dir', '--add-dir'): next(args)
    elif arg not in ('-p', '--force', '--trust', '--disable-auto-update'): raise SystemExit(3)
prompt = sys.stdin.read()
pathlib.Path(os.environ['CURSOR_CONFIG_DIR'], 'cli-config.json').write_text('{{"stand_in":true}}')
pathlib.Path({str(capture)!r}).write_text(json.dumps({{'HOME': os.environ['HOME'], 'prompt': prompt,
    'config': os.environ['CURSOR_CONFIG_DIR'], 'argv': sys.argv[1:]}}))
if {mode != 'stderr-without-result'!r}: print({stream!r}, end='')
else: print('boom', file=sys.stderr); raise SystemExit(1)
"""
    executable = fake_claude(tmp_path / "cursor-agent", body)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    cursor_login.forget_probe()
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 10, "max_turns": 0})
    settings = _launch(executable=executable).settings

    async def load(*args: Any, **kwargs: Any) -> Any:
        return settings

    monkeypatch.setattr(worker_module, "load_agent_settings", load)
    work = tmp_path / "work"
    work.mkdir()

    async def run() -> dict[str, Any]:
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path / "runtime")
        return await pool._execute(_step_envelope(work, verify=verify), is_verify=verify)

    try:
        result = asyncio.run(asyncio.wait_for(run(), 20))
    finally:
        cursor_login.forget_probe()
    seen = json.loads(capture.read_text())
    assert seen["HOME"] == str(home) and seen["prompt"]
    config = Path(seen["config"])
    assert config.is_relative_to(tmp_path / "runtime/.local/cursor-agent/runs")
    assert json.loads((config / "cli-config.json").read_text()) == {"stand_in": True}
    assert not (home / ".cursor").exists() and list(home.iterdir()) == []
    assert agent_db.start_record.await_args.kwargs["metadata"]["personal_config"] == list(PERSONAL_CONFIG)
    if mode != "stderr-without-result":
        finish = agent_db.finish_record.await_args.kwargs
        assert finish["output_text"] == final and finish["metadata"]["routed_model"] == "Grok 4.7"
        if mode == "recorded-report":
            # The transport keeps the recorded reply; the Worker's established JSON report contract still applies.
            assert result["status"] == "failed" and result["final_message"] == final
            assert "JSON status report" in result["summary"]
        else:
            assert result["passed"] is True if verify else result["status"] == "completed"
            assert result["summary"] == "pong HOME=/home/user" and finish["error"] is None
    else:
        assert result["status"] == "failed" and result["stderr"] == "boom\n"
        assert "exited with code 1" in result["error"]


def test_runtime_metadata_prompt_inputs_and_unavailable_sessions(tmp_path: Path) -> None:
    assert CURSOR.loads_operator_config and not CURSOR.can_replace_system_prompt and not CURSOR.runtime_cost_capped
    assert CURSOR.login_models == cursor_login.SUGGESTED_MODELS and CURSOR.effort_levels == ()
    assert CURSOR.isolation_id == "cursor-operator-home/v1" and CURSOR.isolation_args(tmp_path) == []
    binding = CURSOR.bind_provider(_launch().profile.provider, MODEL, "")
    assert binding.args == ("--model", MODEL) and binding.unset == (*cursor_login.ENV_UNSET, *cursor_login.DIRECTORY_VARS)
    usage = Usage(input_tokens=1, output_tokens=2)
    assert CURSOR.cost(usage, _launch().profile.provider).source is CostSource.PROVIDER_PRICE
    assert CURSOR.cost(usage, _launch().profile.provider).usage == usage and CURSOR.budget_error(None) is None
    for name in INSTRUCTION_NAMES:
        (tmp_path / name).write_text(name)
    assert [item.path.name for item in CURSOR.instruction_files(tmp_path)] == list(INSTRUCTION_NAMES)
    blocks = CURSOR.prompt_blocks(_launch(), "LOOPER")
    assert [(block.name, block.owner) for block in blocks] == [
        ("Default prompt", "runtime"), ("Operator configuration", "runtime"), ("Looper instructions", "looper")]
    assert blocks[-1].text == runtime_notes([]) + "\n\n---\n\nLOOPER" and blocks[-1].note == RULES_FILE
    for call in (lambda: CURSOR.resident_command("cursor-agent", cwd=tmp_path, prompt_file=_prompt(tmp_path), launch=_launch()),
                 lambda: CURSOR.resume_args("session"), lambda: CURSOR.resident_session("manager")):
        with pytest.raises(RuntimeUnsupported, match="cannot run the Manager or a Planner yet"):
            call()
    with pytest.raises(RuntimeUnsupported):
        CURSOR.consult_command("cursor-agent", cwd=tmp_path, prompt_file=_prompt(tmp_path), launch=_launch(),
                               session_id="session", session_dir=None, fork_dir=tmp_path / "fork")
