"""Native resident input admission, without calling a model or another runtime."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.claude_cli import ResidentClaude, _Output, _UserTurn
from looper.agents.runtimes.cancel import TurnCancel


class _Writer:
    def __init__(self) -> None:
        self.lines: list[bytes] = []
        self.closing = False
        self.error: Exception | None = None
        self.after_write: Any = None

    def is_closing(self) -> bool:
        return self.closing

    def write(self, line: bytes) -> None:
        if self.error is not None:
            raise self.error
        self.lines.append(line)

    async def drain(self) -> None:
        if self.after_write is not None:
            self.after_write()


def _running() -> tuple[ResidentClaude, _Writer, _UserTurn]:
    resident = ResidentClaude(label="synthetic-mid-turn")
    writer = _Writer()
    resident._process = SimpleNamespace(stdin=writer, returncode=None)
    turn = _UserTurn("original", "first", "original-tag", None, asyncio.get_running_loop().create_future(),
                     started=True)
    resident._user_turn = turn
    resident._cancel = TurnCancel.never()
    resident._cancel.sent()
    resident._output = SimpleNamespace(turn=turn, ended=False, discard=False)
    return resident, writer, turn


@pytest.mark.parametrize("state", [
    "idle", "not_sent", "not_started", "answered", "stopping", "cancelled", "exited", "closing",
    "output_ended", "discarded", "different_turn",
])
def test_input_is_refused_unless_the_original_turn_can_still_take_it(state: str) -> None:
    async def run() -> None:
        resident, writer, turn = _running()
        if state == "idle":
            resident._user_turn = None
        elif state == "not_sent":
            resident._cancel.prompt_sent = False
        elif state == "not_started":
            turn.started = False
        elif state == "answered":
            turn.done.set_result({"type": "result"})
        elif state == "stopping":
            turn.stopping = True
        elif state == "cancelled":
            resident._cancel.request()
        elif state == "exited":
            resident._process.returncode = 0
        elif state == "closing":
            writer.closing = True
        elif state == "output_ended":
            resident._output.ended = True
        elif state == "discarded":
            resident._output.discard = True
        elif state == "different_turn":
            resident._output.turn = None
        assert await resident.write_mid_turn("second") is False
        assert writer.lines == []

    asyncio.run(run())


def test_a_result_during_drain_retires_the_pipe_before_queue_fallback(monkeypatch) -> None:
    async def run() -> None:
        resident, writer, turn = _running()
        retired = []
        async def retire(**kwargs):
            retired.append(kwargs)
            resident._process = None
        monkeypatch.setattr(resident, "stop", retire)
        writer.after_write = lambda: turn.done.set_result({"type": "result"})
        assert await resident.write_mid_turn("补充\ntext") is False
        assert json.loads(writer.lines[0]) == {
            "type": "user", "message": {"role": "user", "content": "补充\ntext"},
        }
        assert resident._user_turn is turn and turn.tag == "original-tag"
        assert retired == [{"kill": True}]
        assert await resident.write_mid_turn("too late") is False
        assert len(writer.lines) == 1

    asyncio.run(run())


def test_buffered_result_is_checked_again_immediately_before_writing() -> None:
    async def run() -> None:
        resident, writer, turn = _running()
        asyncio.get_running_loop().call_soon(setattr, turn, "result_received", True)
        assert await resident.write_mid_turn("too late") is False
        assert writer.lines == []

    asyncio.run(run())


@pytest.mark.parametrize("result_at", ["before_write", "after_write"])
def test_kernel_buffered_result_with_immediate_drain_refuses_input(monkeypatch, result_at) -> None:
    """Poll must wake the transport and then the blocked reader before admission decides."""
    async def run() -> None:
        resident, _, turn = _running()
        client, peer = socket.socketpair()
        reader, writer = await asyncio.open_connection(sock=client)
        result = b'{"type":"result","subtype":"success","result":"original only"}\n'
        writes, retired = [], []

        async def observe(event):
            pass

        turn.on_event = observe
        output = resident._output = _Output(resident, reader, None)
        output.turn = turn
        process = resident._process

        class Writer:
            def is_closing(self):
                return writer.is_closing()

            def write(self, line):
                writes.append(line)
                writer.write(line)
                if result_at == "after_write":
                    peer.sendall(result)

            async def drain(self):
                # Assert this real short-line drain does not suspend.
                resumed = []
                asyncio.get_running_loop().call_soon(resumed.append, True)
                await writer.drain()
                assert not resumed

        async def retire(**kwargs):
            retired.append(kwargs)
            process.returncode = -9
            resident._process = None
            writer.close()
            await writer.wait_closed()

        process.stdin = Writer()
        monkeypatch.setattr(resident, "stop", retire)
        output.read()
        try:
            await asyncio.sleep(0)  # the real read loop blocks waiting for OS data
            assert reader._waiter is not None and not reader._buffer
            if result_at == "before_write":
                peer.sendall(result)  # kernel bytes, no feed_data/call_soon wake-up
            assert await resident.write_mid_turn("B") is False
            assert turn.result_received
            assert len(writes) == int(result_at == "after_write")
            assert retired == ([{"kill": True}] if writes else [])
            if writes:
                assert process.returncode == -9 and writer.is_closing()
        finally:
            peer.close()
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(output._task, 5)

    asyncio.run(run())


def test_a_broken_input_pipe_is_reported_to_the_owner() -> None:
    async def run() -> None:
        resident, writer, _ = _running()
        writer.error = BrokenPipeError()
        with pytest.raises(BrokenPipeError):
            await resident.write_mid_turn("second")
        assert writer.lines == []

    asyncio.run(run())


def test_native_input_joins_one_resident_turn_and_keeps_its_result(tmp_path: Path) -> None:
    """The subprocess reads two stdin inputs and emits exactly one result for run_turn."""
    from looper.agents.runtimes import MidTurnInput

    async def run() -> None:
        resident = ResidentClaude(label="synthetic-mid-turn")
        script = (
            "import json, sys\n"
            "first = json.loads(sys.stdin.readline())\n"
            "print(json.dumps(dict(first, isReplay=True)), flush=True)\n"
            "second = json.loads(sys.stdin.readline())\n"
            "print(json.dumps(dict(second, isReplay=True)), flush=True)\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': "
            "first['message']['content'] + ' + ' + second['message']['content']}), flush=True)\n"
            "sys.stdin.read()\n"
        )
        command = [sys.executable, "-u", "-c", script]
        await resident.start(command, cwd=str(tmp_path), signature=("synthetic", 1))
        started = asyncio.Event()
        seen: list[dict[str, Any]] = []

        async def observe(event: dict[str, Any]) -> None:
            seen.append(event)
            if event.get("isReplay"):
                started.set()

        try:
            assert isinstance(resident, MidTurnInput)
            task = asyncio.create_task(resident.run_turn("first", observe))
            await asyncio.wait_for(started.wait(), 5)
            assert await resident.write_mid_turn("second") is True
            result = await asyncio.wait_for(task, 5)
            assert result.result_event["result"] == "first + second"
            assert len([event for event in seen if event["type"] == "result"]) == 1
            assert resident.command == command and resident._signature == ("synthetic", 1)
            assert await resident.write_mid_turn("third") is False
        finally:
            await resident.stop(kill=True)

    asyncio.run(run())


def test_a_result_closes_input_before_its_event_callback_finishes(tmp_path: Path) -> None:
    async def run() -> None:
        resident = ResidentClaude(label="synthetic-mid-turn-result")
        script = (
            "import json, sys\n"
            "message = json.loads(sys.stdin.readline())\n"
            "print(json.dumps(dict(message, isReplay=True)), flush=True)\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'done'}), flush=True)\n"
            "sys.stdin.read()\n"
        )
        await resident.start([sys.executable, "-u", "-c", script], cwd=str(tmp_path), signature=1)
        at_result, release = asyncio.Event(), asyncio.Event()

        async def observe(event: dict[str, Any]) -> None:
            if event["type"] == "result":
                at_result.set()
                await release.wait()

        task = asyncio.create_task(resident.run_turn("first", observe))
        try:
            await asyncio.wait_for(at_result.wait(), 5)
            assert await resident.write_mid_turn("too late") is False
        finally:
            release.set()
            await asyncio.wait_for(task, 5)
            await resident.stop(kill=True)

    asyncio.run(run())
