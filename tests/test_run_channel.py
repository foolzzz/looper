"""A Worker's run channel, including the default stream path and steering dispatch."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest

from looper.agents import proc_tree
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.profile_registry import CatalogModel
from looper.agents.runtimes import ADAPTERS, CLAUDE_CODE, Capability, RuntimeUnsupported, StreamChannel, adapter
from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
from looper.agents.runtimes.pi import PiRuntime
from looper.agents.runtimes.pi_steer import PiChannel
from looper.agents.tiers import worker
from looper.agents.tiers.worker import WorkerConfig, WorkerPool
from tests.runtime_model_helpers import builtin_launch
from tests.test_agent_planner_worker import _LISTENING_CLAUDE, _fake_claude, _work_item
from tests.test_pi_runtime import _launch as _pi_launch, _provider as _pi_provider

CODEX = adapter("codex")


def _stdin() -> Mock:
    stdin = Mock(drain=AsyncMock(), is_closing=Mock(return_value=False))
    stdin.close.side_effect = lambda: setattr(stdin.is_closing, "return_value", True)
    return stdin


def test_stream_channel_writes_turns_through_the_runtime() -> None:
    async def run() -> None:
        stdin = _stdin()
        runtime = Mock(write_turn=AsyncMock())
        channel = StreamChannel(runtime, SimpleNamespace(stdin=stdin), model=None)
        await channel.start("first prompt")
        await channel.send("another turn", uuid="reminder-1")
        assert runtime.write_turn.await_args_list == [
            call(stdin, "first prompt"), call(stdin, "another turn", uuid="reminder-1")]

    asyncio.run(run())


def test_stream_channel_cannot_send_after_codex_closes_input() -> None:
    async def run() -> None:
        stdin = _stdin()
        channel = StreamChannel(CODEX, SimpleNamespace(stdin=stdin), model=None)
        await channel.start("first prompt")
        with pytest.raises(RuntimeError):
            await channel.send("too late")
        stdin.write.assert_called_once_with(b"first prompt")
        channel.end_input()
        stdin.close.assert_called_once_with()

    asyncio.run(run())


def test_stream_channel_ends_input_once() -> None:
    stdin = _stdin()
    channel = StreamChannel(CLAUDE_CODE, SimpleNamespace(stdin=stdin), model=None)
    channel.end_input()
    channel.end_input()
    stdin.close.assert_called_once_with()


def test_stream_channel_yields_the_runtimes_events() -> None:
    async def run() -> None:
        event = {"type": "system", "subtype": "init", "session_id": "s"}
        stdout = asyncio.StreamReader()
        stdout.feed_data((json.dumps(event) + "\n").encode())
        stdout.feed_eof()
        channel = StreamChannel(CLAUDE_CODE, SimpleNamespace(stdout=stdout), model="opus")
        assert [item async for item in channel.events()] == [event]

    asyncio.run(run())


@pytest.mark.parametrize("steer", [False, True])
def test_launch_steers_uses_the_registered_runtimes_capabilities(steer: bool) -> None:
    launch = builtin_launch()
    assert launch.steers is False
    assert Capability.STEER not in CLAUDE_CODE.capabilities
    capabilities = launch.profile.runtime.capabilities
    if steer:
        capabilities |= {Capability.STEER}
    runtime = replace(launch.profile.runtime, capabilities=capabilities)
    profile = replace(launch.profile, runtime=runtime)
    launch = replace(launch, settings=replace(launch.settings, profile=profile))
    assert launch.steers is steer


def test_a_runtime_without_steering_rejects_a_steered_command(tmp_path: Path) -> None:
    with pytest.raises(RuntimeUnsupported, match="a Claude Code run cannot be steered"):
        CLAUDE_CODE.steer_command("claude", cwd=tmp_path, prompt_file=tmp_path / "prompt", launch=builtin_launch())


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError, RuntimeError])
def test_a_rejected_turn_stops_accepting_input(tmp_path: Path, error: type[Exception]) -> None:
    async def run() -> None:
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        channel = SimpleNamespace(send_timeout=4.0, wait_input_ready=AsyncMock(),
                                  send=AsyncMock(side_effect=error))
        pool._inputs["w1"] = channel
        assert pool.accepts_input("w1")
        assert not await pool.send_input("w1", "  ")
        channel.send.assert_not_awaited()
        assert not await pool.send_input("w1", "hello")
        assert not pool.accepts_input("w1")
        assert not await pool.send_input("w1", "again")
        channel.send.assert_awaited_once_with("hello")

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["started", "failed", "killed"])
def test_input_sent_while_a_worker_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any, outcome: str,
) -> None:
    async def run() -> None:
        starting, release = asyncio.Event(), asyncio.Event()
        spawn = proc_tree.spawn

        async def blocked_spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            starting.set()
            await release.wait()
            if outcome == "failed":
                raise OSError("cannot start worker")
            return await spawn(*args, **kwargs)

        monkeypatch.setattr(proc_tree, "spawn", blocked_spawn)
        bus = MessageBus(runtime_root=tmp_path)
        pool = WorkerPool(config=WorkerConfig(claude_bin=_fake_claude(tmp_path, _LISTENING_CLAUDE)),
                          runtime_root=tmp_path)
        await pool.start(bus)
        delivery = None
        try:
            await pool._dispatch(_work_item(tmp_path))
            await asyncio.wait_for(starting.wait(), 5)
            delivery = asyncio.create_task(pool.send_input("w1", "hello"))
            await asyncio.sleep(0)
            assert not delivery.done()  # no acknowledgement before the input channel opens
            if outcome == "killed":
                assert await pool.kill("w1")
            else:
                release.set()
            assert await asyncio.wait_for(delivery, 5) is (outcome == "started")
            if outcome == "started":
                queue = bus.get_queue("planner-t1")
                result = await asyncio.wait_for(queue.get(), 20)
                while result.message_type != MessageType.WORK_RESULT:
                    result = await asyncio.wait_for(queue.get(), 20)
                assert result.payload["summary"] == "got: hello"
            else:
                assert not pool.accepts_input("w1")
                assert not await pool.send_input("w1", "too late")
        finally:
            if delivery is not None:
                delivery.cancel()
                await asyncio.gather(delivery, return_exceptions=True)
            await pool.stop()
            await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("steer", [False, True])
def test_worker_uses_its_run_channel_over_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any, steer: bool,
) -> None:
    executable = _fake_claude(tmp_path, _LISTENING_CLAUDE)
    ready = asyncio.Event()
    sent: list[tuple[str, str | None]] = []
    commands: list[str] = []
    ended: list[bool] = []

    class RecordingChannel(StreamChannel):
        async def start(self, prompt: str) -> None:
            await super().start(prompt)
            ready.set()

        async def send(self, text: str, *, uuid: str | None = None) -> None:
            sent.append((text, uuid))
            await super().send(text, uuid=uuid)

        def end_input(self) -> None:
            ended.append(True)
            super().end_input()

    class Runtime(ClaudeCodeRuntime):
        capabilities = ClaudeCodeRuntime.capabilities | {Capability.STEER}

        def run_command(self, executable: str, **kwargs: Any) -> list[str]:
            commands.append("run")
            return [executable]

        def steer_command(self, executable: str, **kwargs: Any) -> list[str]:
            commands.append("steer")
            return [executable]

        def run_channel(self, process: asyncio.subprocess.Process, *, launch: Launch, **kwargs: Any) -> RecordingChannel:
            return RecordingChannel(self, process, model=launch.model)

    runtime = Runtime()
    monkeypatch.setitem(ADAPTERS, "claude-code", runtime)
    launch = builtin_launch()
    registered = replace(launch.profile.runtime, executable=executable,
                         capabilities=runtime.capabilities if steer else CLAUDE_CODE.capabilities)
    settings = AgentSettings(profile=replace(launch.profile, runtime=registered))
    monkeypatch.setattr(worker, "load_agent_settings", AsyncMock(return_value=settings))

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        await pool.start(bus)
        try:
            await asyncio.sleep(0)
            await bus.publish(_work_item(tmp_path))
            await asyncio.wait_for(ready.wait(), 5)
            assert pool.accepts_input("w1")
            reply = await bus.request(MessageEnvelope.create(
                source=planner_address("t1"), target=worker_address("w1"),
                message_type=MessageType.WORKER_INPUT, payload={"text": "hello"},
            ), timeout=5)
            assert reply.payload["delivered"] is True
            queue = bus.get_queue("planner-t1")
            result = await asyncio.wait_for(queue.get(), 20)
            while result.message_type != MessageType.WORK_RESULT:
                result = await asyncio.wait_for(queue.get(), 20)
            assert result.payload["status"] == "completed"
            assert result.payload["summary"] == "got: hello"
            assert commands == ["steer" if steer else "run"]
            assert sent == [("hello", None)] and ended == [True]
            assert not pool.accepts_input("w1")
            assert not await pool.send_input("w1", "too late")
        finally:
            await pool.stop()
            await bus.shutdown()

    asyncio.run(run())


_WORKER_PI = r'''
import select, signal, subprocess
from pathlib import Path

scenario = sys.argv[1]
root = Path.cwd()
sys.stdin = open(0, 'rb', buffering=0, closefd=False)

def record(value):
    with open(root / 'pi-requests.jsonl', 'a') as out:
        out.write(json.dumps(value) + '\n')

def emit(value):
    print(json.dumps(value), flush=True)

def ack(request, **data):
    emit(dict(type='response', id=request['id'], command=request['type'], success=True, data=data))

def finish():
    answer = json.dumps(dict(status='completed', changed_files={}, summary='got: second instruction'))
    emit(dict(type='message_end', message=dict(role='assistant', model='selected', stopReason='stop',
        content=[dict(type='text', text=answer)])))
    emit(dict(type='agent_settled', aborted=False))

while True:
    if not select.select([sys.stdin], [], [], .005)[0]:
        continue
    line = sys.stdin.readline()
    if not line:
        record(dict(stdin_closed=True))
        if scenario != 'normal':
            while True:
                time.sleep(1)
        break
    request = json.loads(line)
    record(request)
    kind = request['type']
    if kind == 'get_state':
        ack(request, sessionId='worker-pi-session')
    elif kind == 'prompt':
        ack(request, disposition='started')
        if scenario != 'normal':
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            child = subprocess.Popen(['/bin/sh', '-c',
                'trap "" TERM; sleep 60 & echo $! > pi-grandchild.pid; wait'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pid_file = root / 'pi-grandchild.pid'
            while not pid_file.exists() or not pid_file.read_text().strip():
                time.sleep(.005)
            record(dict(tree=[os.getpid(), child.pid, int(pid_file.read_text())]))
        record(dict(ready=True))
    elif kind == 'steer':
        record(dict(steer_received=True))
        gate = root / ('pi-ack' if scenario == 'normal' else 'pi-cap')
        while not gate.exists():
            time.sleep(.005)
        if scenario == 'normal':
            ack(request, disposition='queued')
            while not (root / 'pi-finish').exists():
                time.sleep(.005)
            finish()
        else:
            emit(dict(type='message_end', message=dict(role='assistant', model='selected', stopReason='toolUse',
                content=[dict(type='text', text='over the cap')], usage=dict(input=100, output=10))))
    else:
        ack(request)
'''


def _pi_worker_records(root: Path) -> list[dict[str, Any]]:
    path = root / 'pi-requests.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def _pi_worker_record(root: Path, key: str) -> dict[str, Any]:
    async with asyncio.timeout(20):
        while True:
            if matches := [row for row in _pi_worker_records(root) if key in row]:
                return matches[-1]
            await asyncio.sleep(.005)


def _pi_worker_running(pids: list[int]) -> list[int]:
    running = []
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        running.append(pid)
    return running


async def _pi_worker_tree_ended(pids: list[int]) -> None:
    async with asyncio.timeout(20):
        while _pi_worker_running(pids):
            await asyncio.sleep(.01)


def _pi_worker_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    executable = _fake_claude(tmp_path, _WORKER_PI)

    class Runtime(PiRuntime):
        def run_command(self, executable: str, **kwargs: Any) -> list[str]:
            raise AssertionError('a steering-capable Worker must use the RPC command')

        def steer_command(self, executable: str, **kwargs: Any) -> list[str]:
            return [executable, scenario]

    monkeypatch.setitem(ADAPTERS, 'pi', Runtime())
    provider = _pi_provider(catalog=(CatalogModel('selected', input_usd_per_mtok=1,
                                                 output_usd_per_mtok=1),))
    launch = _pi_launch(provider)
    runtime = replace(launch.profile.runtime, executable=executable,
                      capabilities=frozenset({Capability.RUN, Capability.STEER}))
    settings = replace(launch.settings, profile=replace(launch.profile, runtime=runtime),
                       max_budget_usd=.00001 if scenario == 'cap' else None)
    monkeypatch.setattr(worker, 'load_agent_settings', AsyncMock(return_value=settings))
    monkeypatch.setattr(worker, '_load_settings', AsyncMock(return_value=worker.WorkerSettings(
        6 if scenario == 'timeout' else 0, 0, 'work')))


async def _pi_worker_result(bus: MessageBus) -> MessageEnvelope:
    async with asyncio.timeout(20):
        queue = bus.get_queue('planner-t1')
        while True:
            result = await queue.get()
            if result.message_type == MessageType.WORK_RESULT:
                return result


def test_pi_worker_input_waits_for_queued_ack_then_settled_closes_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    _pi_worker_settings(tmp_path, monkeypatch, 'normal')

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        await pool.start(bus)
        reply: asyncio.Task[MessageEnvelope] | None = None
        try:
            await asyncio.sleep(0)
            await bus.publish(_work_item(tmp_path))
            await _pi_worker_record(tmp_path, 'ready')
            channel = pool._inputs['w1']
            assert isinstance(channel, PiChannel)
            reply = asyncio.create_task(bus.request(MessageEnvelope.create(
                source=planner_address('t1'), target=worker_address('w1'),
                message_type=MessageType.WORKER_INPUT, payload={'text': 'second instruction'},
            ), timeout=5))
            await _pi_worker_record(tmp_path, 'steer_received')
            assert not reply.done()
            assert channel._steers
            (tmp_path / 'pi-ack').touch()
            response = await asyncio.wait_for(reply, 5)
            assert response.payload['delivered'] is True
            assert pool.accepts_input('w1')
            assert not channel._steers
            assert not any(row.get('stdin_closed') for row in _pi_worker_records(tmp_path))
            (tmp_path / 'pi-finish').touch()
            result = await _pi_worker_result(bus)
            assert result.payload['status'] == 'completed'
            assert result.payload['summary'] == 'got: second instruction'
            assert (await _pi_worker_record(tmp_path, 'stdin_closed'))['stdin_closed'] is True
            assert not pool.accepts_input('w1')
            assert not await pool.send_input('w1', 'too late')
            assert channel._pending == {} and channel._steers == {}
            sent = [row for row in _pi_worker_records(tmp_path) if row.get('type') == 'steer']
            assert len(sent) == 1 and sent[0]['message'] == 'second instruction'
        finally:
            if reply is not None and not reply.done():
                reply.cancel()
                await asyncio.gather(reply, return_exceptions=True)
            await pool.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 45))


@pytest.mark.parametrize('reason', ['cancel', 'timeout', 'cap'])
def test_pi_worker_stops_its_process_tree_and_clears_unacknowledged_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any, reason: str,
) -> None:
    _pi_worker_settings(tmp_path, monkeypatch, reason)
    monkeypatch.setattr(proc_tree, 'TERM_GRACE_SEC', .1)
    tree: list[int] = []

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        await pool.start(bus)
        reply: asyncio.Task[MessageEnvelope] | None = None
        try:
            await asyncio.sleep(0)
            await bus.publish(_work_item(tmp_path))
            tree.extend((await _pi_worker_record(tmp_path, 'tree'))['tree'])
            assert _pi_worker_running(tree) == tree
            assert all(os.getpgid(pid) == tree[0] for pid in tree)
            await _pi_worker_record(tmp_path, 'ready')
            channel = pool._inputs['w1']
            reply = asyncio.create_task(bus.request(MessageEnvelope.create(
                source=planner_address('t1'), target=worker_address('w1'),
                message_type=MessageType.WORKER_INPUT, payload={'text': 'never acknowledged'},
            ), timeout=5))
            await _pi_worker_record(tmp_path, 'steer_received')
            assert channel._steers and not reply.done()
            if reason == 'cancel':
                assert await pool.kill('w1')
            else:
                if reason == 'cap':
                    (tmp_path / 'pi-cap').touch()
                result = await _pi_worker_result(bus)
                assert result.payload['status'] == 'failed'
                if reason == 'timeout':
                    assert 'timeout_seconds' in result.payload['error']
                else:
                    assert 'per-run spend cap' in result.payload['error']
            response = await asyncio.wait_for(reply, 5)
            assert response.payload['delivered'] is False
            assert not pool.accepts_input('w1')
            assert channel._pending == {} and channel._steers == {}
            await _pi_worker_tree_ended(tree)
            assert _pi_worker_running(tree) == []
        finally:
            if reply is not None and not reply.done():
                reply.cancel()
                await asyncio.gather(reply, return_exceptions=True)
            await pool.stop()
            await bus.shutdown()

    try:
        asyncio.run(asyncio.wait_for(run(), 45))
    finally:
        # Also clean up descendants when an assertion fails before Worker shutdown.
        for pid in _pi_worker_running(tree):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
