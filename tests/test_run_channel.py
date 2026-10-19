"""A Worker's run channel, including the default stream path and steering dispatch."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest

from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.runtimes import ADAPTERS, CLAUDE_CODE, Capability, RuntimeUnsupported, StreamChannel, adapter
from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
from looper.agents.tiers import worker
from looper.agents.tiers.worker import WorkerConfig, WorkerPool
from tests.test_agent_planner_worker import _LISTENING_CLAUDE, _fake_claude, _work_item

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
    launch = Launch()
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
        CLAUDE_CODE.steer_command("claude", cwd=tmp_path, prompt_file=tmp_path / "prompt", launch=Launch())


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError, RuntimeError])
def test_a_rejected_turn_stops_accepting_input(tmp_path: Path, error: type[Exception]) -> None:
    async def run() -> None:
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        channel = SimpleNamespace(send=AsyncMock(side_effect=error))
        pool._inputs["w1"] = channel
        assert pool.accepts_input("w1")
        assert not await pool.send_input("w1", "  ")
        channel.send.assert_not_awaited()
        assert not await pool.send_input("w1", "hello")
        assert not pool.accepts_input("w1")
        assert not await pool.send_input("w1", "again")
        channel.send.assert_awaited_once_with("hello")

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
    launch = Launch()
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
