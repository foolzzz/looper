"""OMP rpc steering, against a stand-in process with the same command acknowledgements."""

from __future__ import annotations

import asyncio
import json
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.runtimes import Capability, StreamChannel
from looper.agents.runtimes import omp as omp_module, omp_steer
from looper.agents.runtimes.omp_steer import OmpChannel
from looper.agents.tiers import worker
from looper.agents.tiers.worker import WorkerConfig, WorkerPool
from tests.test_agent_planner_worker import _work_item
from tests.test_omp_runtime import OMP, _blocks, _launch


_RPC = """
import json, os, select, sys, time

def emit(event):
    print(json.dumps(event), flush=True)

if '--version' in sys.argv:
    assert os.listdir(os.environ['HOME']) == []
    print(os.environ.get('FAKE_VERSION', 'omp/18.4.3'))
    sys.exit(0)
if '--help' in sys.argv:
    assert os.listdir(os.environ['HOME']) == []
    print(os.environ.get('FAKE_HELP', '--mode rpc --no-ui'))
    sys.exit(int(os.environ.get('FAKE_HELP_EXIT', '0')))

def record(value):
    with open(os.environ['FAKE_RECORD'], 'a') as out:
        out.write(json.dumps(value) + '\\n')

record({'argv': sys.argv[1:]})
scenario = os.environ.get('FAKE_SCENARIO', 'normal')
finished = False
def finish(text):
    global finished
    emit({'type': 'message_end', 'message': {'role': 'toolResult', 'toolCallId': 'tool-1',
          'content': [{'type': 'text', 'text': 'tool finished'}]}})
    answer = json.dumps({'status': 'completed', 'changed_files': {}, 'summary': text})
    emit({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop',
          'content': [{'type': 'text', 'text': answer}], 'usage': {'input': 7, 'output': 3}}})
    emit({'type': 'agent_end', 'isTerminal': True})
    emit({'type': 'prompt_result'})
    emit({'type': 'session_settled'})
    finished = True

if '-p' in sys.argv:
    sys.stdin.read()
    emit({'type': 'session', 'id': 'print-session'})
    finish('no input')
    sys.exit(0)

emit({'type': 'ready'})
sys.stdin = open(0, 'rb', buffering=0, closefd=False)
deadline = None
while True:
    if deadline is not None and time.monotonic() >= deadline:
        finish('no input')
        deadline = None
    readable, _, _ = select.select([sys.stdin], [], [], 0.02)
    if not readable:
        continue
    line = sys.stdin.readline()
    if not line:
        record({'stdin_closed': True, 'finished': finished})
        break
    request = json.loads(line)
    record(request)
    command = request['type']
    response = {'type': 'response', 'id': request['id'], 'command': command, 'success': True}
    if scenario == 'fail_' + command:
        response.update(success=False, error=command + ' rejected')
        emit(response)
        continue
    if command == 'get_state':
        response['data'] = {'sessionId': 'rpc-session'}
    if command == 'prompt' and scenario == 'eof_before_ack':
        sys.exit(1)
    if command == 'steer':
        emit({'type': 'notice', 'text': 'steer received'})
        if scenario == 'crash_steer':
            sys.exit(1)
        if scenario == 'no_answer':
            continue
        time.sleep(0.1)
    emit(response)
    if command == 'prompt':
        if scenario == 'noise':
            print('not json SECRET-xyz', flush=True)
            print('[]', flush=True)
            emit({'type': 'response', 'command': 'parse', 'success': False, 'error': 'bad line'})
            emit({'type': 'response', 'id': 'unknown', 'success': False})
            emit({'type': 'future_event'})
            for kind in ('available_commands_update', 'extension_ui_request'):
                emit({'type': kind})
        emit({'type': 'agent_start'})
        emit({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'toolUse',
              'content': [{'type': 'toolCall', 'id': 'tool-1', 'name': 'bash',
                           'arguments': {'command': 'long tool'}}], 'usage': {'input': 5, 'output': 2}}})
        emit({'type': 'tool_execution_start', 'toolCallId': 'tool-1', 'toolName': 'bash'})
        emit({'type': 'agent_end', 'isTerminal': False})
        if scenario == 'crash':
            sys.exit(1)
        if scenario in ('normal', 'noise'):
            deadline = time.monotonic() + 0.15
    elif command == 'steer':
        finish('got: ' + request['message'])
"""


def _fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str = "normal") -> str:
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    monkeypatch.setenv("FAKE_RECORD", str(tmp_path / "requests.jsonl"))
    return fake_claude(tmp_path / "omp", _RPC)


def _requests(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]


async def _spawn(executable: str, cwd: Path) -> tuple[asyncio.subprocess.Process, OmpChannel]:
    process = await asyncio.create_subprocess_exec(
        executable, "--mode", "rpc", "--no-ui", cwd=str(cwd), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    return process, OmpChannel(process, model="test-model", cwd=str(cwd))


async def _close(process: asyncio.subprocess.Process, channel: OmpChannel) -> None:
    channel.end_input()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _collect(channel: OmpChannel, events: list[dict[str, Any]], *, tool: asyncio.Event | None = None,
                   received: asyncio.Event | None = None) -> None:
    async for event in channel.events():
        events.append(event)
        if tool is not None and _blocks([event], "tool_use"):
            tool.set()
        if received is not None and event.get("event", {}).get("text") == "steer received":
            received.set()
        if event["type"] == "result":
            channel.end_input()


def test_rpc_run_keeps_input_open_until_end_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = _fake(tmp_path, monkeypatch)

    async def run() -> None:
        process, channel = await _spawn(executable, tmp_path)
        try:
            await asyncio.wait_for(channel.start("first prompt"), 2)
            events = []
            iterator = channel.events()
            async for event in iterator:
                events.append(event)
                if event["type"] == "result":
                    break
            assert events[0] == {"type": "system", "subtype": "init", "session_id": "rpc-session",
                                 "runtime": "omp", "model": "test-model", "cwd": str(tmp_path)}
            assert _blocks(events, "tool_use")[0]["name"] == "bash"
            assert _blocks(events, "tool_result")[0]["content"] == "tool finished"
            assert _blocks(events, "text")[-1]["text"] == events[-1]["result"]
            assert sum(e["type"] == "usage" for e in events) == 2
            assert [e["subtype"] for e in events if e["type"] == "result"] == ["success"]
            assert events[-1]["modelUsage"]["test-model"]["inputTokens"] == 12
            assert process.stdin is not None and not process.stdin.is_closing()
            assert process.returncode is None
            with pytest.raises(RuntimeError, match="the OMP run has ended"):
                await channel.send("too late")
            channel.end_input()
            channel.end_input()
            assert [e async for e in iterator] == []
            await asyncio.wait_for(process.wait(), 2)
            requests = _requests(tmp_path)
            commands = [r for r in requests if "id" in r]
            assert [r["type"] for r in commands] == ["set_interrupt_mode", "get_state", "prompt"]
            assert commands[0]["mode"] == "wait" and commands[2]["message"] == "first prompt"
            assert len({r["id"] for r in commands}) == 3
            assert all(isinstance(r["id"], str) for r in commands)
            assert requests[-1] == {"stdin_closed": True, "finished": True}
        finally:
            await _close(process, channel)

    asyncio.run(run())


def test_steer_ack_waits_for_server_acceptance_and_reaches_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _fake(tmp_path, monkeypatch, "wait_steer")

    async def run() -> None:
        process, channel = await _spawn(executable, tmp_path)
        events: list[dict[str, Any]] = []
        tool, received = asyncio.Event(), asyncio.Event()
        await channel.start("first prompt")
        reader = asyncio.create_task(_collect(channel, events, tool=tool, received=received))
        try:
            await asyncio.wait_for(tool.wait(), 2)
            send = asyncio.create_task(channel.send("HELLO", uuid="caller-id"))
            await asyncio.wait_for(received.wait(), 2)
            assert not send.done()
            await asyncio.wait_for(send, 2)
            await asyncio.wait_for(reader, 2)
            assert "HELLO" in events[-1]["result"]
            assert sum(e["type"] == "result" for e in events) == 1
            with pytest.raises(RuntimeError, match="the OMP run has ended"):
                await channel.send("late")
        finally:
            await _close(process, channel)
            await reader

    asyncio.run(run())


@pytest.mark.parametrize("scenario,problem", [("fail_steer", "steer rejected"),
                                             ("no_answer", "OMP did not accept the steer in time")])
def test_steer_failure_and_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                   scenario: str, problem: str) -> None:
    executable = _fake(tmp_path, monkeypatch, scenario)
    monkeypatch.setattr(omp_steer, "_STEER_TIMEOUT_SEC", 0.2)

    async def run() -> None:
        process, channel = await _spawn(executable, tmp_path)
        tool = asyncio.Event()
        await channel.start("prompt")
        reader = asyncio.create_task(_collect(channel, [], tool=tool))
        try:
            await asyncio.wait_for(tool.wait(), 2)
            with pytest.raises(RuntimeError, match=problem):
                await channel.send("hello")
            assert channel._pending == {}
        finally:
            await _close(process, channel)
            await reader

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["fail_set_interrupt_mode", "fail_get_state", "fail_prompt", "eof_before_ack"])
def test_handshake_failure_yields_one_error_without_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        scenario: str) -> None:
    executable = _fake(tmp_path, monkeypatch, scenario)

    async def run() -> None:
        process, channel = await _spawn(executable, tmp_path)
        try:
            await channel.start("prompt")
            events = [e async for e in channel.events()]
            results = [e for e in events if e["type"] == "result"]
            assert len(results) == 1
            assert results[0]["is_error"] and results[0]["subtype"] == "error_during_execution"
            assert results[0]["result"] == "" and results[0]["errors"][0].startswith("OMP rpc: ")
            with pytest.raises(RuntimeError, match="the OMP run has ended"):
                await channel.send("late")
        finally:
            await _close(process, channel)
        commands = [r["type"] for r in _requests(tmp_path) if "id" in r]
        # The prompt follows only after both setup requests succeeded.
        assert commands.count("prompt") == (0 if scenario in ("fail_set_interrupt_mode", "fail_get_state") else 1)

    asyncio.run(run())


def test_crash_ends_events_without_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = _fake(tmp_path, monkeypatch, "crash")

    async def run() -> None:
        process, channel = await _spawn(executable, tmp_path)
        try:
            await channel.start("prompt")
            events = [e async for e in channel.events()]
            assert _blocks(events, "tool_use") and not any(e["type"] == "result" for e in events)
            await process.wait()
            assert process.returncode == 1
            with pytest.raises((RuntimeError, BrokenPipeError)):
                await channel.send("late")
        finally:
            await _close(process, channel)

    asyncio.run(run())


def test_eof_fails_a_pending_steer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = _fake(tmp_path, monkeypatch, "crash_steer")

    async def run() -> None:
        process, channel = await _spawn(executable, tmp_path)
        events: list[dict[str, Any]] = []
        tool = asyncio.Event()
        await channel.start("prompt")
        reader = asyncio.create_task(_collect(channel, events, tool=tool))
        try:
            await asyncio.wait_for(tool.wait(), 2)
            with pytest.raises(ConnectionResetError):
                await channel.send("hello")
            await asyncio.wait_for(reader, 2)
            assert not any(e["type"] == "result" for e in events)
        finally:
            await _close(process, channel)
            await reader

    asyncio.run(run())


def test_send_refuses_closed_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = _fake(tmp_path, monkeypatch, "wait_steer")

    async def run() -> None:
        process, channel = await _spawn(executable, tmp_path)
        try:
            await channel.start("prompt")
            channel.end_input()
            with pytest.raises(RuntimeError, match="the OMP run has ended"):
                await channel.send("late")
        finally:
            await _close(process, channel)

    asyncio.run(run())


def test_protocol_noise_does_not_change_the_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    executable = _fake(tmp_path, monkeypatch, "noise")
    caplog.set_level("DEBUG", logger=omp_steer.__name__)

    async def run() -> None:
        process, channel = await _spawn(executable, tmp_path)
        events: list[dict[str, Any]] = []
        try:
            await channel.start("prompt")
            await asyncio.wait_for(_collect(channel, events), 3)
            assert [e["subtype"] for e in events if e["type"] == "result"] == ["success"]
            assert [e["event"]["type"] for e in events if e.get("subtype") == "omp_event"] == ["future_event"]
            assert "non-JSON line" in caplog.text and "SECRET" not in caplog.text and "bad line" not in caplog.text
        finally:
            await _close(process, channel)

    asyncio.run(run())


@pytest.mark.parametrize("keep_session", [False, True])
def test_rpc_command_preserves_launch_isolation_and_session(tmp_path: Path, keep_session: bool) -> None:
    launch = _launch()
    prompt_file = tmp_path / ".local" / "prompts" / "prompt.md"
    prompt_file.parent.mkdir(parents=True)
    prompt_file.write_text("system instructions")
    session_dir = tmp_path / "session" if keep_session else None
    command = OMP.steer_command("omp", cwd=tmp_path, prompt_file=prompt_file, launch=launch, session_dir=session_dir)
    ordinary = OMP.run_command("omp", cwd=tmp_path, prompt_file=prompt_file, launch=launch, session_dir=session_dir)
    assert command == [*ordinary[:3], "--mode", "rpc", "--no-ui", *ordinary[6:]]
    assert command[0] == "env" and command[1].startswith("HOME=")
    assert "--config" in command and "--append-system-prompt" in command and "-p" not in command
    assert OMP.launch_args(launch)[:3] == ["-p", "--mode", "json"]
    if session_dir is not None:
        assert command[-2:] == ["--session-dir", str(session_dir)]
        assert stat.S_IMODE(session_dir.stat().st_mode) == 0o700
    else:
        assert command[-1] == "--no-session"


@pytest.mark.parametrize("version,help_text,exit_code,steer,read_tools", [
    ("omp/18.4.3", "--mode rpc --no-ui", "0", True, True),
    ("omp/18.4.2", "--mode rpc --no-ui", "0", False, False),
    ("omp/18.4.3", "--mode json --no-ui", "0", False, True),
    ("omp/18.4.3", "--mode rpc", "0", False, True),
    ("omp/18.4.3", "--mode rpc --no-ui", "1", False, True),
    ("unreadable", "--mode rpc --no-ui", "0", False, False),
])
def test_steer_probe_requires_version_and_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              version: str, help_text: str, exit_code: str,
                                              steer: bool, read_tools: bool) -> None:
    executable = _fake(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_VERSION", version)
    monkeypatch.setenv("FAKE_HELP", help_text)
    monkeypatch.setenv("FAKE_HELP_EXIT", exit_code)
    probe = OMP.probe(executable)
    assert probe.error is None
    assert (Capability.STEER in probe.capabilities) is steer
    assert (Capability.READ_TOOLS in probe.capabilities) is read_tools
    assert Capability.STEER not in OMP.capabilities_for(version)
    assert Capability.STEER not in OMP.capabilities
    assert Capability.STEER not in OMP.probe(str(tmp_path / "missing")).capabilities


@pytest.mark.parametrize("failure", [OSError("failed"), subprocess.TimeoutExpired("omp --help", 10),
                                    UnicodeError("invalid help output")])
def test_help_probe_failure_keeps_read_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           failure: Exception) -> None:
    executable = _fake(tmp_path, monkeypatch)
    real_run = subprocess.run

    def run(command: list[str], **kwargs: Any) -> Any:
        if command[-1] == "--help":
            raise failure
        return real_run(command, **kwargs)

    monkeypatch.setattr(omp_module.subprocess, "run", run)
    probe = OMP.probe(executable)
    assert probe.version == "omp/18.4.3" and probe.error is None
    assert Capability.STEER not in probe.capabilities
    assert Capability.READ_TOOLS in probe.capabilities


@pytest.mark.parametrize("steer", [False, True])
def test_worker_omp_input_over_the_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                     agent_db: Any, steer: bool) -> None:
    executable = _fake(tmp_path, monkeypatch, "wait_steer")
    launch = _launch(executable=executable)
    registered = replace(launch.profile.runtime, capabilities=OMP.capabilities | ({Capability.STEER} if steer else set()))
    launch = replace(launch, settings=replace(launch.settings, profile=replace(launch.profile, runtime=registered)))
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=launch.settings))
    ready = asyncio.Event()
    channel_factory = OMP.run_channel

    def run_channel(*args: Any, **kwargs: Any) -> Any:
        channel = channel_factory(*args, **kwargs)
        assert isinstance(channel, OmpChannel if steer else StreamChannel)
        start = channel.start

        async def started(prompt: str) -> None:
            await start(prompt)
            ready.set()

        channel.start = started
        return channel

    monkeypatch.setattr(OMP, "run_channel", run_channel)

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        await pool.start(bus)
        try:
            await asyncio.sleep(0)
            await bus.publish(_work_item(tmp_path))
            await asyncio.wait_for(ready.wait(), 5)
            reply = await bus.request(MessageEnvelope.create(
                source=planner_address("t1"), target=worker_address("w1"),
                message_type=MessageType.WORKER_INPUT, payload={"text": "HELLO"},
            ), timeout=5)
            assert reply.payload["delivered"] is steer
            queue = bus.get_queue("planner-t1")
            while True:
                result = await asyncio.wait_for(queue.get(), 10)
                if result.message_type == MessageType.WORK_RESULT:
                    break
            assert result.payload["status"] == "completed"
            assert result.payload["summary"] == ("got: HELLO" if steer else "no input")
            requests = _requests(tmp_path)
            argv = requests[0]["argv"]
            assert argv[argv.index("--mode") + 1] == ("rpc" if steer else "json")
            assert ("-p" in argv) is (not steer)
            assert not await pool.send_input("w1", "late")
        finally:
            await pool.stop()
            await bus.shutdown()

    asyncio.run(run())


def test_a_stalled_writer_cannot_outlast_the_one_steer_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(omp_steer, "_STEER_TIMEOUT_SEC", 0.2)

    class Stalled:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            await asyncio.Event().wait()

        def is_closing(self) -> bool:
            return False

    async def run() -> None:
        channel = OmpChannel(SimpleNamespace(stdin=Stalled(), stdout=None), model=None, cwd="/")  # type: ignore[arg-type]
        started = asyncio.get_running_loop().time()
        with pytest.raises(RuntimeError, match="in time"):
            await channel.send("x")
        assert asyncio.get_running_loop().time() - started < 0.6
        assert channel._pending == {}

    asyncio.run(run())


def test_the_prompt_waits_for_both_setup_acks_in_any_order() -> None:
    import json as _json

    class Stdin:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        def write(self, data: bytes) -> None:
            self.sent.append(_json.loads(data))

        async def drain(self) -> None:
            return None

        def is_closing(self) -> bool:
            return False

    async def run() -> None:
        from types import SimpleNamespace

        stdin = Stdin()
        process = SimpleNamespace(stdin=stdin, stdout=asyncio.StreamReader())
        channel = OmpChannel(process, model=None, cwd="/")  # type: ignore[arg-type]
        await channel.start("the prompt")
        ids = {r["type"]: r["id"] for r in stdin.sent}
        assert [r["type"] for r in stdin.sent] == ["set_interrupt_mode", "get_state"]

        def ack(command: str, **extra: Any) -> None:
            process.stdout.feed_data(_json.dumps({"type": "response", "id": ids[command], "command": command,
                                                  "success": True, **extra}).encode() + b"\n")

        events = channel.events()
        ack("get_state", data={"sessionId": "s"})  # out of order: the interrupt mode is not acknowledged yet
        first = await asyncio.wait_for(events.__anext__(), 2)
        assert first["type"] == "system" and "prompt" not in [r["type"] for r in stdin.sent]
        ack("set_interrupt_mode")
        process.stdout.feed_eof()
        [e async for e in events]
        assert [r["type"] for r in stdin.sent] == ["set_interrupt_mode", "get_state", "prompt"]

    asyncio.run(run())
