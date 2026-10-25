"""Codex app-server steering, against a protocol-speaking stand-in executable."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents.runtimes.channel import DeliveryResult

from fake_claude import fake_claude
from looper import child_env
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.profile_registry import LegacyProviderSpec as ProviderSpec, RuntimeSpec
from tests.runtime_model_helpers import resolved_profile
from tests.test_agent_settings import _row
from looper.agents.runtimes import ApiProtocol, AuthKind, Capability
from looper.agents.runtimes.codex import CodexRuntime, CodexStream
from looper.agents.runtimes.codex_steer import CodexChannel, exec_events, exec_item
from looper.agents.tiers.worker import WorkerConfig, WorkerPool

CODEX = CodexRuntime()
TOTALS = {"inputTokens": 100, "cachedInputTokens": 30, "cacheWriteInputTokens": 10,
          "outputTokens": 20, "reasoningOutputTokens": 5}

_APP_SERVER = r'''
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    home = Path(os.environ["CODEX_HOME"])
    assert home.is_dir() and not any(home.iterdir()), "version probe needs an empty CODEX_HOME"
    print("codex-cli " + os.environ.get("FAKE_CODEX_VERSION", "0.162.0"))
    sys.exit(int(os.environ.get("FAKE_CODEX_VERSION_EXIT", "0")))
if "generate-json-schema" in args:
    home = Path(os.environ["CODEX_HOME"])
    assert home.is_dir() and not any(home.iterdir()), "schema probe needs an empty CODEX_HOME"
    with Path(os.environ["FAKE_CODEX_LOG"]).open("a") as stream:
        stream.write(json.dumps({"probe": "schema", "home": str(home), "empty": True}) + "\n")
    if os.environ.get("FAKE_CODEX_SCHEMA_FAIL"):
        sys.exit(1)
    out = Path(args[args.index("--out") + 1])
    out.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("FAKE_CODEX_SCHEMA_MISSING"):
        (out / "ClientRequest.json").write_text(json.dumps({
            "method": "turn/start" if os.environ.get("FAKE_CODEX_NO_STEER") else "turn/steer"}))
    sys.exit(0)

mode = os.environ.get("FAKE_CODEX_MODE", "normal")
log = Path(os.environ["FAKE_CODEX_LOG"])

def record(value):
    with log.open("a") as stream:
        stream.write(json.dumps(value) + "\n")

def emit(value):
    print(json.dumps(value), flush=True)

def notify(method, **params):
    emit({"method": method, "params": {"threadId": "thread-1", "turnId": "turn-1", **params}})

def response(request, result):
    emit({"id": request["id"], "result": result})

def finish(text):
    notify("item/completed", item={"id": "command-1", "type": "commandExecution", "command": "sleep 1",
                                  "aggregatedOutput": "done\n", "exitCode": 0, "status": "completed"})
    notify("item/completed", item={"id": "answer-1", "type": "agentMessage", "text": text,
                                  "phase": "final_answer"})
    notify("thread/tokenUsage/updated", tokenUsage={"total": {
        "inputTokens": 100, "cachedInputTokens": 30, "cacheWriteInputTokens": 10,
        "outputTokens": 20, "reasoningOutputTokens": 5}})
    notify("turn/completed", turn={"id": "turn-1", "status": "completed", "error": None})

record({"argv": args})
if "exec" in args:
    record({"prompt": sys.stdin.read(), "instructions": (Path(os.environ["CODEX_HOME"]) / "AGENTS.md").read_text()})
    time.sleep(0.15)
    emit({"type": "thread.started", "thread_id": "thread-exec"})
    emit({"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": json.dumps({
        "status": "completed", "summary": "exec prompt received"})}})
    emit({"type": "turn.completed", "usage": {}})
    sys.exit(0)

for line in sys.stdin:
    request = json.loads(line)
    record(request)
    method = request.get("method")
    if method == "initialize":
        if mode == "stall_initialize":
            continue
        if mode == "early":
            time.sleep(0.1)
        if mode == "eof_initialize":
            sys.exit(1)
        if mode == "initialize_error":
            emit({"id": request["id"], "error": {"code": -32600, "message": "initialize rejected"}})
        else:
            response(request, {})
    elif method == "thread/start":
        if mode == "thread_error":
            emit({"id": request["id"], "error": {"code": -32600, "message": "thread rejected"}})
        else:
            response(request, {"thread": {"id": "thread-1"}})
    elif method == "turn/start":
        if mode == "turn_error":
            emit({"id": request["id"], "error": {"code": -32600, "message": "turn rejected"}})
            continue
        response(request, {"turn": {"id": "turn-1"}})
        notify("turn/started", turn={"id": "turn-1"})
        if mode == "noise":
            print("not json", flush=True)
            print("[]", flush=True)
            print("null", flush=True)
            notify("item/agentMessage/delta", delta="not a final answer")
            notify("future/notification", data="ignored")
        notify("item/started", item={"id": "command-1", "type": "commandExecution", "command": "sleep 1",
                                    "aggregatedOutput": "", "exitCode": None, "status": "inProgress"})
        if mode == "crash":
            sys.exit(1)
        if mode == "server_request":
            emit({"id": 900, "method": "item/tool/requestUserInput", "params": {"question": "continue?"}})
        elif mode not in ("hold", "early", "steer_error", "ack_gate", "worker", "worker_steer_error", "steer_crash", "stall_steer"):
            finish("normal answer")
    elif method == "turn/steer":
        if mode == "steer_crash":
            sys.exit(1)
        if mode == "stall_steer":
            continue
        if request["params"]["expectedTurnId"] != "turn-1":
            emit({"id": request["id"], "error": {"code": -32600, "message": "wrong active turn"}})
            continue
        if mode == "ack_gate":
            gate = Path(os.environ["FAKE_CODEX_ACK_GATE"])
            deadline = time.monotonic() + 3
            while not gate.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
        if mode in ("steer_error", "worker_steer_error"):
            emit({"id": request["id"], "error": {"code": -32600, "message": "steer refused"}})
            if mode == "worker_steer_error":
                gate = Path(os.environ["FAKE_CODEX_FINISH_GATE"])
                deadline = time.monotonic() + 3
                while not gate.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                finish(json.dumps({"status": "completed", "summary": "steer was refused"}))
            else:
                finish("steer was refused")
        else:
            text = request["params"]["input"][0]["text"]
            response(request, {"turnId": "turn-1"})
            notify("item/started", item={"id": "steer-user", "type": "userMessage", "content": [text]})
            finish(json.dumps({"status": "completed", "summary": "got: " + text}) if mode == "worker"
                   else "got: " + text)
    elif request.get("id") == 900:
        finish("server request answered")
'''


@pytest.fixture
def app_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    # The stand-in has explicit controls; they are not part of the production allowlist.
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {
        "FAKE_CODEX_LOG", "FAKE_CODEX_MODE", "FAKE_CODEX_VERSION", "FAKE_CODEX_VERSION_EXIT",
        "FAKE_CODEX_SCHEMA_FAIL", "FAKE_CODEX_SCHEMA_MISSING", "FAKE_CODEX_NO_STEER",
        "FAKE_CODEX_ACK_GATE", "FAKE_CODEX_FINISH_GATE",
    })
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    return fake_claude(tmp_path / "codex", _APP_SERVER), log


def _requests(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _launch(executable: str, *, steer: bool = True) -> Launch:
    provider = ProviderSpec("provider", "fake", AuthKind.API_KEY, frozenset({ApiProtocol.OPENAI_RESPONSES}),
                            api_key="fake-key")
    capabilities = CODEX.capabilities | {Capability.STEER} if steer else CODEX.capabilities
    profile = resolved_profile("profile", "codex/test", RuntimeSpec("runtime", "codex", "codex", executable,
                                                                frozenset(capabilities)), provider, "gpt-5.5", "low")
    return Launch(AgentSettings(profile=profile))


@asynccontextmanager
async def _channel(executable: str, cwd: Path, *, keep_session: bool = False, effort: str = "low"):
    home = cwd / "channel-home"
    home.mkdir(exist_ok=True)
    process = await asyncio.create_subprocess_exec(executable, "app-server", cwd=cwd,
                                                   env={**os.environ, "CODEX_HOME": str(home)},
                                                   stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE)
    channel = CodexChannel(process, model="gpt-5.5", effort=effort, cwd=str(cwd), keep_session=keep_session)
    try:
        yield channel, process
    finally:
        channel.end_input()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def _collect(channel: CodexChannel) -> list[dict[str, Any]]:
    events = []
    async for event in channel.events():
        events.append(event)
        if event["type"] == "result":
            channel.end_input()
    return events


async def _wait_request(log: Path, method: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            for request in _requests(log):
                if request.get("method") == method:
                    return request
            await asyncio.sleep(0.01)


def _blocks(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [block for event in events if event["type"] in ("assistant", "user")
            for block in event["message"]["content"] if block["type"] == kind]


@pytest.mark.parametrize(("item", "expected"), [
    ({"type": "agentMessage", "text": "answer", "phase": "final_answer"},
     {"type": "agent_message", "text": "answer"}),
    ({"type": "reasoning", "summary": [{"text": "one"}], "content": [{"text": "two"}]},
     {"type": "reasoning", "text": "one\ntwo"}),
    ({"type": "reasoning", "summary": ["one"], "content": ["two"]},
     {"type": "reasoning", "text": "one\ntwo"}),
    ({"type": "commandExecution", "command": "pwd", "aggregatedOutput": "/tmp", "exitCode": 0,
      "status": "completed"}, {"type": "command_execution", "command": "pwd", "aggregated_output": "/tmp",
                                  "exit_code": 0, "status": "completed"}),
    ({"type": "commandExecution", "command": "pwd", "aggregatedOutput": "", "exitCode": None,
      "status": "inProgress"}, {"type": "command_execution", "command": "pwd", "aggregated_output": "",
                                   "exit_code": None, "status": "in_progress"}),
    ({"type": "commandExecution", "command": "pwd", "aggregatedOutput": "denied", "exitCode": None,
      "status": "declined"}, {"type": "command_execution", "command": "pwd", "aggregated_output": "denied",
                                 "exit_code": None, "status": "failed"}),
    ({"type": "fileChange", "changes": [{"path": "a.py", "kind": {"type": "add"}},
                                            {"path": "b.py", "kind": {"type": "delete"}},
                                            {"path": "c.py", "kind": {"type": "update", "move_path": None}}],
      "status": "completed"}, {"type": "file_change", "changes": [{"path": "a.py", "kind": "add"},
                                                                     {"path": "b.py", "kind": "delete"},
                                                                     {"path": "c.py", "kind": "update"}],
                                 "status": "completed"}),
    ({"type": "mcpToolCall", "server": "docs", "tool": "read", "arguments": {"path": "a"},
      "result": {"content": [{"type": "text", "text": "ok"}]}, "error": None, "status": "completed"},
     {"type": "mcp_tool_call", "server": "docs", "tool": "read", "arguments": {"path": "a"},
      "result": {"content": [{"type": "text", "text": "ok"}]}, "error": None, "status": "completed"}),
    ({"type": "webSearch", "query": "a query"}, {"type": "web_search", "query": "a query"}),
    ({"type": "plan", "text": "do this"}, {"type": "todo_list", "items": [{"text": "do this", "completed": False}]}),
    ({"type": "futureItem", "data": {"x": 1}}, {"type": "future_item", "data": {"x": 1}}),
    ({"type": "userMessage", "content": [{"type": "text", "text": "private steer"}]}, None),
])
def test_exec_item_conversion(item: dict[str, Any], expected: dict[str, Any] | None) -> None:
    source = {"id": "item-1", **item}
    assert exec_item(source) == ({"id": "item-1", **expected} if expected is not None else None)


@pytest.mark.parametrize(("method", "params", "expected"), [
    ("turn/started", {"turn": {"id": "turn-1"}}, [{"type": "turn.started"}]),
    ("item/started", {"item": {"id": "x", "type": "webSearch", "query": "q"}},
     [{"type": "item.started", "item": {"id": "x", "type": "web_search", "query": "q"}}]),
    ("item/completed", {"item": {"id": "x", "type": "agentMessage", "text": "ok"}},
     [{"type": "item.completed", "item": {"id": "x", "type": "agent_message", "text": "ok"}}]),
    ("item/completed", {"item": {"id": "x", "type": "userMessage", "content": []}}, []),
    ("turn/completed", {"turn": {"status": "failed", "error": {"message": "failure"}}},
     [{"type": "turn.failed", "error": {"message": "failure"}}]),
    ("turn/completed", {"turn": {"status": "interrupted", "error": None}},
     [{"type": "turn.failed", "error": {"message": "the turn was interrupted"}}]),
    ("error", {"error": {"message": "retry"}, "willRetry": True}, [{"type": "error", "message": "retry"}]),
    ("item/agentMessage/delta", {"delta": "discard"}, []),
    ("account/rateLimits/updated", {}, []),
    ("future/notification", {}, []),
])
def test_exec_events_conversion(method: str, params: dict[str, Any], expected: list[dict[str, Any]]) -> None:
    assert exec_events(method, {"threadId": "thread-1", "turnId": "turn-1", **params}, TOTALS) == expected


def test_app_server_usage_is_identical_to_exec_running_totals() -> None:
    converted = exec_events("turn/completed", {"turn": {"status": "completed", "error": None}}, TOTALS)
    raw = {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 30,
                                                "cache_write_input_tokens": 10, "output_tokens": 20}}
    assert converted == [raw]
    assert CodexStream(model="gpt-5.5").translate(converted[0]) == CodexStream(model="gpt-5.5").translate(raw)
    usage, result = CodexStream(model="gpt-5.5").translate(converted[0])
    assert usage["usage"]["input_tokens"] == 60
    assert result["modelUsage"]["gpt-5.5"]["outputTokens"] == 20


@pytest.mark.parametrize("mode", ["normal", "noise"])
def test_channel_normal_run_and_ignored_noise(app_server: tuple[str, Path], tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    executable, log = app_server
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)

    async def run() -> list[dict[str, Any]]:
        async with _channel(executable, tmp_path) as (channel, process):
            await channel.start("the prompt")
            assert not log.exists() or not any(r.get("method") == "thread/start" for r in _requests(log))
            events = await asyncio.wait_for(_collect(channel), 3)
            channel.end_input()
            assert await process.wait() == 0
            return events

    events = asyncio.run(run())
    assert events[0] == {"type": "system", "subtype": "init", "session_id": "thread-1", "runtime": "codex"}
    assert [b["id"] for b in _blocks(events, "tool_use")] == ["command-1"]
    assert [b["content"] for b in _blocks(events, "tool_result")] == ["done\n"]
    assert [b["text"] for b in _blocks(events, "text")] == ["normal answer"]
    assert [e["type"] for e in events[-2:]] == ["usage", "result"]
    assert events[-1]["result"] == "normal answer" and not events[-1]["is_error"]
    requests = _requests(log)
    methods = [r.get("method") for r in requests if "method" in r]
    assert methods == ["initialize", "initialized", "thread/start", "turn/start"]
    initialize = next(r for r in requests if r.get("method") == "initialize")
    assert initialize["params"] == {"clientInfo": {"name": "looper", "title": None, "version": "0"}}
    thread = next(r for r in requests if r.get("method") == "thread/start")
    assert thread["params"] == {"cwd": str(tmp_path), "model": "gpt-5.5", "approvalPolicy": "never",
                                "sandbox": "danger-full-access", "ephemeral": True}
    turn = next(r for r in requests if r.get("method") == "turn/start")
    assert turn["params"] == {"threadId": "thread-1", "input": [{"type": "text", "text": "the prompt"}],
                              "effort": "low"}


@pytest.mark.parametrize("early", [False, True])
def test_steer_waits_for_active_turn_and_uses_its_id(app_server: tuple[str, Path], tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch, early: bool) -> None:
    executable, log = app_server
    monkeypatch.setenv("FAKE_CODEX_MODE", "early" if early else "hold")

    async def run() -> list[dict[str, Any]]:
        async with _channel(executable, tmp_path, keep_session=True) as (channel, _):
            await channel.start("prompt")
            reading = asyncio.create_task(_collect(channel))
            if not early:
                await _wait_request(log, "turn/start")
            await asyncio.wait_for(channel.send("include HERON", uuid="ignored-uuid"), 3)
            return await asyncio.wait_for(reading, 3)

    events = asyncio.run(run())
    assert events[-1]["result"] == "got: include HERON"
    steer = next(r for r in _requests(log) if r.get("method") == "turn/steer")
    assert steer["params"] == {"threadId": "thread-1", "expectedTurnId": "turn-1",
                               "input": [{"type": "text", "text": "include HERON"}]}
    assert next(r for r in _requests(log) if r.get("method") == "thread/start")["params"]["ephemeral"] is False
    assert not any(e.get("subtype") == "codex_item" for e in events)


def test_send_returns_only_after_server_accepts(app_server: tuple[str, Path], tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    executable, log = app_server
    gate = tmp_path / "accept-steer"
    monkeypatch.setenv("FAKE_CODEX_MODE", "ack_gate")
    monkeypatch.setenv("FAKE_CODEX_ACK_GATE", str(gate))

    async def run() -> None:
        async with _channel(executable, tmp_path) as (channel, _):
            await channel.start("prompt")
            reading = asyncio.create_task(_collect(channel))
            sending = asyncio.create_task(channel.send("wait for acceptance"))
            await _wait_request(log, "turn/steer")
            assert not sending.done()
            gate.touch()
            await asyncio.wait_for(sending, 3)
            assert (await asyncio.wait_for(reading, 3))[-1]["result"] == "got: wait for acceptance"

    asyncio.run(run())


def test_send_after_turn_completed_is_refused_without_writing(app_server: tuple[str, Path], tmp_path: Path) -> None:
    executable, log = app_server

    async def run() -> None:
        async with _channel(executable, tmp_path) as (channel, _):
            await channel.start("prompt")
            events = channel.events()
            async for event in events:
                if event["type"] == "result":
                    with pytest.raises(RuntimeError, match="no active turn"):
                        await channel.send("too late")
                    channel.end_input()
            with pytest.raises(RuntimeError, match="the Codex run has ended"):
                await channel.send("after EOF")

    asyncio.run(run())
    assert not any(r.get("method") == "turn/steer" for r in _requests(log))


def test_steer_server_error_is_returned_to_sender(app_server: tuple[str, Path], tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    executable, _ = app_server
    monkeypatch.setenv("FAKE_CODEX_MODE", "steer_error")

    async def run() -> None:
        async with _channel(executable, tmp_path) as (channel, _):
            await channel.start("prompt")
            reading = asyncio.create_task(_collect(channel))
            with pytest.raises(RuntimeError, match="steer refused"):
                await channel.send("rejected")
            await asyncio.wait_for(reading, 3)

    asyncio.run(run())


def test_crash_mid_turn_has_no_result_and_late_send_fails(app_server: tuple[str, Path], tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    executable, _ = app_server
    monkeypatch.setenv("FAKE_CODEX_MODE", "crash")

    async def run() -> None:
        async with _channel(executable, tmp_path) as (channel, process):
            await channel.start("prompt")
            events = await asyncio.wait_for(_collect(channel), 3)
            assert not any(event["type"] == "result" for event in events)
            assert _blocks(events, "tool_use")
            assert await process.wait() == 1
            with pytest.raises((RuntimeError, BrokenPipeError)):
                await channel.send("after crash")

    asyncio.run(run())


def test_server_request_gets_error_response_and_system_event(app_server: tuple[str, Path], tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    executable, log = app_server
    monkeypatch.setenv("FAKE_CODEX_MODE", "server_request")

    async def run() -> list[dict[str, Any]]:
        async with _channel(executable, tmp_path) as (channel, _):
            await channel.start("prompt")
            return await asyncio.wait_for(_collect(channel), 3)

    events = asyncio.run(run())
    assert {"type": "system", "subtype": "codex_server_request", "method": "item/tool/requestUserInput"} in events
    assert next(r for r in _requests(log) if r.get("id") == 900) == {
        "id": 900, "error": {"code": -32601, "message": "Looper runs Codex without interactive requests"}}
    assert events[-1]["result"] == "server request answered"


@pytest.mark.parametrize(("mode", "message", "methods"), [
    ("initialize_error", "initialize rejected", ["initialize"]),
    ("thread_error", "thread rejected", ["initialize", "initialized", "thread/start"]),
    ("turn_error", "turn rejected", ["initialize", "initialized", "thread/start", "turn/start"]),
    ("eof_initialize", "", ["initialize"]),
])
def test_handshake_failure_emits_one_error_and_never_retries(app_server: tuple[str, Path], tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch, mode: str,
                                                           message: str, methods: list[str]) -> None:
    executable, log = app_server
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)

    async def run() -> list[dict[str, Any]]:
        async with _channel(executable, tmp_path) as (channel, _):
            await channel.start("prompt")
            return await asyncio.wait_for(_collect(channel), 3)

    events = asyncio.run(run())
    results = [event for event in events if event["type"] == "result"]
    assert len(results) == 1
    assert results[0]["subtype"] == "error_during_execution" and results[0]["is_error"]
    assert results[0]["result"] == ""
    assert len(results[0]["errors"]) == 1 and results[0]["errors"][0].startswith("Codex app-server: ")
    assert message in results[0]["errors"][0]
    assert [r["method"] for r in _requests(log) if "method" in r] == methods


@pytest.mark.parametrize("session", [False, True])
def test_steer_command_reuses_run_home_and_only_effort_args(tmp_path: Path, session: bool) -> None:
    launch = _launch("fake-codex")
    prompt_file = tmp_path / ".local" / "prompts" / "worker.md"
    prompt_file.parent.mkdir(parents=True)
    prompt_file.write_text("Worker system instructions")
    session_dir = tmp_path / "session" if session else None
    run = CODEX.run_command("fake-codex", cwd=tmp_path, prompt_file=prompt_file, launch=launch,
                           session_dir=session_dir)
    steer = CODEX.steer_command("fake-codex", cwd=tmp_path, prompt_file=prompt_file, launch=launch,
                               session_dir=session_dir)
    assert steer == ["env", run[1], "fake-codex", "app-server", "-c", 'model_reasoning_effort="low"']
    assert not any(arg in steer for arg in ("exec", "--json", "-m", "--add-dir", "--ephemeral"))
    home = Path(steer[1].removeprefix("CODEX_HOME="))
    if session:
        assert home == session_dir
    assert (home / "config.toml").is_file()
    assert "Worker system instructions" in (home / "AGENTS.md").read_text()


@pytest.mark.parametrize(("variable", "value", "steer"), [
    ("FAKE_CODEX_VERSION", "0.162.0", True),
    ("FAKE_CODEX_VERSION", "0.161.9", False),
    ("FAKE_CODEX_SCHEMA_FAIL", "1", False),
    ("FAKE_CODEX_NO_STEER", "1", False),
    ("FAKE_CODEX_SCHEMA_MISSING", "1", False),
    ("FAKE_CODEX_VERSION_EXIT", "1", False),
])
def test_probe_requires_version_and_schema(app_server: tuple[str, Path], monkeypatch: pytest.MonkeyPatch,
                                           variable: str, value: str, steer: bool) -> None:
    monkeypatch.setenv(variable, value)
    probe = CODEX.probe(app_server[0])
    assert (Capability.STEER in probe.capabilities) is steer
    assert Capability.STEER not in CODEX.capabilities
    schemas = [r for r in _requests(app_server[1]) if r.get("probe") == "schema"]
    if (variable == "FAKE_CODEX_VERSION" and value == "0.161.9") or variable == "FAKE_CODEX_VERSION_EXIT":
        assert not schemas
    else:
        assert len(schemas) == 1 and schemas[0]["empty"]
        assert not Path(schemas[0]["home"]).exists()


def test_missing_executable_has_no_steer(tmp_path: Path) -> None:
    assert Capability.STEER not in CODEX.probe(str(tmp_path / "not-installed")).capabilities


@pytest.mark.parametrize(("steer", "reject"), [
    pytest.param(True, False, id="accepted-steer"),
    pytest.param(False, False, id="exec-fallback"),
    pytest.param(True, True, id="rejected-steer-finishes"),
])
def test_worker_bus_input_uses_detected_steering_capability(agent_db: Any, app_server: tuple[str, Path],
                                                          tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                          steer: bool, reject: bool) -> None:
    executable, log = app_server
    finish_gate = tmp_path / "finish-after-rejection"
    monkeypatch.setenv("FAKE_CODEX_MODE", "worker_steer_error" if reject else "worker")
    monkeypatch.setenv("FAKE_CODEX_FINISH_GATE", str(finish_gate))
    launch = _launch(executable, steer=steer)
    profile = _row(launch.profile)
    from looper.db import profile_registry

    monkeypatch.setattr(profile_registry, "get_api_key", AsyncMock(return_value="fake-key"))
    model = launch.profile.resolved_model.model
    monkeypatch.setattr(profile_registry, "list_models", AsyncMock(return_value=[
        {**asdict(model), "protocol": model.protocol.value}]))
    agent_db.list_layers.return_value = [{"scope": "system", "profile": profile}]

    payload = {"task_id": "s1", "instruction": "add the parser", "context": {"work_dir": str(tmp_path)}}

    async def run() -> tuple[bool, str, str]:
        bus = MessageBus(runtime_root=tmp_path)
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        await pool.start(bus)
        try:
            await asyncio.sleep(0)
            await bus.publish(MessageEnvelope.create(
                source=planner_address("t1"), target=worker_address("w1"), message_type=MessageType.WORK_ITEM,
                payload=payload,
            ))
            async with asyncio.timeout(5):
                while not pool.accepts_input("w1"):
                    await asyncio.sleep(0.01)
            ack = await bus.request(MessageEnvelope.create(
                source=planner_address("t1"), target=worker_address("w1"), message_type=MessageType.WORKER_INPUT,
                payload={"text": "include HERON"},
            ), timeout=5)
            if reject:
                assert ack.payload["delivered"] is False
                finish_gate.touch()
            queue = bus.get_queue("planner-t1")
            async with asyncio.timeout(10):
                while True:
                    result = await queue.get()
                    if result.message_type == MessageType.WORK_RESULT:
                        return ack.payload["delivered"], result.payload["summary"], pool._build_work_prompt(payload)
        finally:
            await pool.stop()
            await bus.shutdown()

    delivered, summary, expected_prompt = asyncio.run(run())
    assert delivered is (steer and not reject)
    requests = _requests(log)
    argv = next(r["argv"] for r in requests if "argv" in r)
    if steer:
        assert "app-server" in argv and "exec" not in argv
        assert summary == ("steer was refused" if reject else "got: include HERON")
        assert next(r for r in requests if r.get("method") == "turn/steer")["params"]["expectedTurnId"] == "turn-1"
    else:
        assert "exec" in argv and "--json" in argv
        assert summary == "exec prompt received"
        first_turn = next(r for r in requests if "prompt" in r)
        assert first_turn["prompt"] == expected_prompt
        assert "add the parser" in first_turn["instructions"]
        assert not any(r.get("method") == "turn/steer" for r in requests)


def test_pending_steer_fails_when_stdout_closes(app_server: tuple[str, Path], tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    executable, _ = app_server
    monkeypatch.setenv("FAKE_CODEX_MODE", "steer_crash")

    async def run() -> None:
        async with _channel(executable, tmp_path) as (channel, _):
            await channel.start("prompt")
            reading = asyncio.create_task(_collect(channel))
            assert await channel.send("crash before acknowledgement") is DeliveryResult.UNKNOWN
            assert not any(e["type"] == "result" for e in await asyncio.wait_for(reading, 3))

    asyncio.run(run())


@pytest.mark.parametrize(("mode", "message"), [
    ("stall_initialize", "did not accept the steer"),
    ("stall_steer", "did not accept the steer"),
])
def test_send_has_bounded_handshake_and_ack_waits(app_server: tuple[str, Path], tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch, mode: str, message: str) -> None:
    from looper.agents.runtimes import codex_steer

    executable, log = app_server
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    monkeypatch.setattr(codex_steer, "_REQUEST_TIMEOUT_SEC", 0.05)

    async def run() -> None:
        async with _channel(executable, tmp_path) as (channel, _):
            await channel.start("prompt")
            reading = asyncio.create_task(_collect(channel))
            if mode == "stall_steer":
                await _wait_request(log, "turn/start")
                await asyncio.sleep(0.01)
            expected = DeliveryResult.REJECTED if mode == "stall_initialize" else DeliveryResult.UNKNOWN
            assert await channel.send("do not hang") is expected
            channel.end_input()
            await asyncio.wait_for(reading, 3)

    asyncio.run(run())


def test_empty_effort_is_omitted_from_turn_start(app_server: tuple[str, Path], tmp_path: Path) -> None:
    executable, log = app_server

    async def run() -> None:
        async with _channel(executable, tmp_path, effort="") as (channel, _):
            await channel.start("prompt")
            await asyncio.wait_for(_collect(channel), 3)

    asyncio.run(run())
    turn = next(r for r in _requests(log) if r.get("method") == "turn/start")
    assert "effort" not in turn["params"]


def test_handshake_write_failure_is_one_failed_result(app_server: tuple[str, Path], tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    executable, log = app_server

    async def run() -> list[dict[str, Any]]:
        async with _channel(executable, tmp_path) as (channel, process):
            await channel.start("prompt")
            monkeypatch.setattr(process.stdin, "drain", AsyncMock(side_effect=BrokenPipeError("closed at handshake")))
            return await asyncio.wait_for(_collect(channel), 3)

    events = asyncio.run(run())
    results = [e for e in events if e["type"] == "result"]
    assert len(results) == 1 and results[0]["is_error"]
    assert results[0]["errors"] == ["Codex app-server: closed at handshake"]
    assert not any(r.get("method") == "turn/start" for r in _requests(log))


def test_schema_probe_timeout_is_not_an_error(app_server: tuple[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.runtimes import codex

    original = codex.subprocess.run
    schema_home = []

    def run(command: list[str], **kwargs: Any):
        if "generate-json-schema" in command:
            home = Path(kwargs["env"]["CODEX_HOME"])
            assert home.is_dir() and not any(home.iterdir())
            assert kwargs["timeout"] == codex._PROBE_TIMEOUT_SEC
            schema_home.append(home)
            raise codex.subprocess.TimeoutExpired(command, kwargs["timeout"])
        return original(command, **kwargs)

    monkeypatch.setattr(codex.subprocess, "run", run)
    probe = CODEX.probe(app_server[0])
    assert probe.error is None and Capability.STEER not in probe.capabilities
    assert schema_home and not schema_home[0].exists()


def test_unreadable_version_does_not_probe_schema(app_server: tuple[str, Path],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_VERSION", "unknown")
    probe = CODEX.probe(app_server[0])
    assert Capability.STEER not in probe.capabilities
    assert not any(r.get("probe") == "schema" for r in _requests(app_server[1]))


class _StalledStdin:
    """A pipe under backpressure: writes buffer, ``drain`` never returns."""

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        await asyncio.Event().wait()

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        pass


class _Process:
    def __init__(self, stdin: Any, lines: list[bytes] | None = None) -> None:
        self.stdin = stdin
        self.stdout = asyncio.StreamReader()
        for line in lines or []:
            self.stdout.feed_data(line)


def test_a_stalled_writer_cannot_outlast_the_one_steer_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.runtimes import codex_steer

    monkeypatch.setattr(codex_steer, "_REQUEST_TIMEOUT_SEC", 0.2)

    async def run() -> None:
        channel = CodexChannel(_Process(_StalledStdin()), model="m", effort="", cwd="/", keep_session=False)  # type: ignore[arg-type]
        channel._turn_ready.set()
        channel._turn_id, channel._thread_id = "t", "th"
        started = asyncio.get_running_loop().time()
        assert await channel.send("x") is DeliveryResult.UNKNOWN
        assert asyncio.get_running_loop().time() - started < 0.6
        assert channel._pending == {}

    asyncio.run(run())


@pytest.mark.parametrize("envelope", [
    {"id": [], "result": {}}, {"id": True, "result": {}}, {"id": {"a": 1}, "method": "item/x", "params": {}},
    {"method": "turn/started", "params": [1]}, {"id": 1, "result": [1]}, {"method": 5, "params": {}},
    {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": []}},
])
def test_the_reader_survives_malformed_envelopes(envelope: dict[str, Any]) -> None:
    async def run() -> None:
        good = {"method": "turn/started", "params": {}}
        process = _Process(object(), [json.dumps(envelope).encode() + b"\n", json.dumps(good).encode() + b"\n"])
        channel = CodexChannel(process, model="m", effort="", cwd="/", keep_session=False)  # type: ignore[arg-type]
        channel._turn_ready.set()
        process.stdout.feed_eof()
        events = [e async for e in channel.events()]
        assert not any(e.get("is_error") for e in events)

    asyncio.run(run())


@pytest.mark.parametrize("envelope", [
    {"method": "item/started", "params": {"item": [1]}},
    {"method": "item/completed", "params": {"item": {"type": "fileChange", "changes": [1]}}},
    {"method": "turn/completed", "params": {"turn": [1]}},
    {"method": "turn/completed", "params": {"turn": {"status": "failed", "error": [1]}}},
    {"method": "error", "params": {"error": [1]}},
])
def test_the_reader_survives_nested_non_objects(envelope: dict[str, Any]) -> None:
    async def run() -> None:
        process = _Process(object(), [json.dumps(envelope).encode() + b"\n"])
        channel = CodexChannel(process, model="m", effort="", cwd="/", keep_session=False)  # type: ignore[arg-type]
        channel._turn_ready.set()
        process.stdout.feed_eof()
        [e async for e in channel.events()]

    asyncio.run(run())


@pytest.mark.parametrize(("method", "result"), [("thread/start", {"thread": [1]}), ("turn/start", {"turn": [1]})])
def test_a_handshake_result_with_a_non_object_fails_the_launch_cleanly(method: str, result: dict[str, Any]) -> None:
    class Stdin(_StalledStdin):
        async def drain(self) -> None:
            return None

    async def run() -> None:
        process = _Process(Stdin())
        channel = CodexChannel(process, model="m", effort="", cwd="/", keep_session=False)  # type: ignore[arg-type]
        await channel.start("p")
        order = ["initialize", "thread/start", "turn/start"]
        for name in order[: order.index(method) + 1]:
            process.stdout.feed_data(json.dumps({"id": order.index(name) + 1, "result": result if name == method
                                                 else ({"thread": {"id": "t"}} if name == "thread/start" else {})}).encode() + b"\n")
        process.stdout.feed_eof()
        events = [e async for e in channel.events()]
        assert [e["is_error"] for e in events if e["type"] == "result"] == [True]

    asyncio.run(run())


def test_a_secret_used_as_a_key_does_not_reach_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        line = json.dumps({"SECRET-KEY-xyz": 1, "params": [1]}).encode() + b"\n"
        process = _Process(object(), [line, b"not json SECRET-KEY-xyz\n"])
        channel = CodexChannel(process, model="m", effort="", cwd="/", keep_session=False)  # type: ignore[arg-type]
        channel._turn_ready.set()
        process.stdout.feed_eof()
        [e async for e in channel.events()]

    caplog.set_level("DEBUG")
    asyncio.run(run())
    assert "ignoring" in caplog.text and "SECRET" not in caplog.text
