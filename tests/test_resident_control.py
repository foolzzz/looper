"""ResidentClaude.control: one control request between turns, answered by the output reader."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents.claude_cli import ControlUnavailable, ResidentClaude, _Output


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
            await self.after_write()


def _idle() -> tuple[ResidentClaude, _Writer, _Output]:
    resident = ResidentClaude(label="synthetic-control")
    writer = _Writer()
    resident._process = SimpleNamespace(stdin=writer, returncode=None)
    output = resident._output = _Output(resident, asyncio.StreamReader(), None)
    return resident, writer, output


def _answer(output: _Output, writer: _Writer, response: dict[str, Any]):
    async def after() -> None:
        request = json.loads(writer.lines[-1])
        await output._dispatch({"type": "control_response", "response": {"request_id": request["request_id"], **response}})
    return after


def test_success_returns_the_body_and_removes_the_future() -> None:
    async def run() -> None:
        resident, writer, output = _idle()
        writer.after_write = _answer(output, writer, {"subtype": "success", "response": {"a": 1}})
        assert await resident.control({"subtype": "get_status"}) == {"a": 1}
        assert json.loads(writer.lines[0])["request"] == {"subtype": "get_status"}
        assert resident._controls == {}

    asyncio.run(run())


def test_error_response_raises_and_removes_the_future() -> None:
    async def run() -> None:
        resident, writer, output = _idle()
        writer.after_write = _answer(output, writer, {"subtype": "error", "error": "Unsupported"})
        with pytest.raises(ControlUnavailable) as caught:
            await resident.control({"subtype": "mcp_status"})
        assert caught.value.subtype == "mcp_status" and "Unsupported" in caught.value.detail
        assert resident._controls == {}

    asyncio.run(run())


def test_timeout_raises_and_removes_the_future() -> None:
    async def run() -> None:
        resident, writer, _ = _idle()
        with pytest.raises(ControlUnavailable) as caught:
            await resident.control({"subtype": "get_settings"}, timeout=0.05)
        assert caught.value.subtype == "get_settings"
        assert resident._controls == {}

    asyncio.run(run())


@pytest.mark.parametrize("state", ["no_process", "exited", "closing", "broken_pipe"])
def test_no_usable_process_raises(state: str) -> None:
    async def run() -> None:
        resident, writer, _ = _idle()
        if state == "no_process":
            resident._process = None
        elif state == "exited":
            resident._process.returncode = 1
        elif state == "closing":
            writer.closing = True
        else:
            writer.error = BrokenPipeError("gone")
        with pytest.raises(ControlUnavailable):
            await resident.control({"subtype": "get_status"})
        assert resident._controls == {}

    asyncio.run(run())


def test_refuses_while_a_user_turn_runs() -> None:
    async def run() -> None:
        resident, _, _ = _idle()
        resident._user_turn = object()  # type: ignore[assignment]
        with pytest.raises(AssertionError):
            await resident.control({"subtype": "get_status"})

    asyncio.run(run())
