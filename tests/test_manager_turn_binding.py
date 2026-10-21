"""A tool call belongs to the turn whose binding it carries, not to the turn running when it arrives.

The Manager publishes each turn's binding in a file; a stand-in per-turn runtime reads it the way the MCP bridge
does, and its "tool" calls ``ManagerOps`` with what it read (``tests/test_mcp_server.py`` covers the bridge itself).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_settings
from looper.agents.claude_cli import ResidentTurn, user_stop_result
from looper.agents.ops import Call, Risk, Tool
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.run_usage import Usage
from looper.agents.runtimes import StopMethod
from looper.agents.tiers.manager import StopResult
from tests.test_manager_new_session import _until, _web
from tests.test_manager_stop import _manager


@pytest.fixture(autouse=True)
def _stop_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)


class _PerTurnSession:
    """A runtime with a process per turn: it reads the binding file as its bridge does, then (first turn) waits for
    the stop and calls its tool late, with the binding it read."""

    label = "manager"
    pid = None
    shares_bridge = False

    def __init__(self, ops: ManagerOps, turn_file: Path) -> None:
        self.ops, self.turn_file = ops, turn_file
        self.started = False
        self.command: list[str] = []
        self.totals = Usage()
        self.running = asyncio.Event()
        self.hold_first = True
        self.late: list[dict[str, Any]] = []
        self.files_at_exit: list[Any] = []

    def stale(self, signature: Any) -> bool:
        return False

    async def start(self, command: list[str], **kwargs: Any) -> None:
        self.started, self.command = True, command

    async def stop(self, *, kill: bool = False) -> None:
        self.started = False

    async def run_turn(self, text: str, on_event: Any, *, tag: Any = None, stop: Any = None,
                       cancel: Any = None) -> ResidentTurn:
        binding = json.loads(self.turn_file.read_text())["turn"]  # what the bridge reads at spawn
        gone = asyncio.Event()
        if cancel is not None:
            cancel.on_request(gone.set)
        await on_event({"type": "system", "subtype": "init", "session_id": "sess-1"})
        if self.hold_first:
            self.hold_first = False
            self.running.set()
            await gone.wait()
            # The tool call was in flight when the user stopped; it arrives after.
            self.late.append(await self.ops.call("t_write", {"n": 1}, binding=binding))
            self.files_at_exit.append(json.loads(self.turn_file.read_text())["turn"])
            return ResidentTurn(user_stop_result(), stopped=StopMethod.KILL, usage=Usage(), prompt_sent=True)
        self.late.append(await self.ops.call("t_write", {"n": 2}, binding=binding))
        return ResidentTurn({"type": "result", "subtype": "success", "is_error": False, "result": "好的",
                             "session_id": "sess-1"})


def test_a_call_that_comes_after_the_stop_is_refused_and_the_next_turns_runs(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            turn_file = root / ".local" / "manager-turn.json"
            ran: list[dict[str, Any]] = []

            async def write(call: Call) -> dict[str, Any]:
                ran.append(dict(call.arguments))
                return {"ok": True}

            schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
            ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                             tools=(Tool("t_write", "writes", schema, Risk.WRITE, write),), turn_file=turn_file)

            ops._working_at = ops._clock()  # the MCP entry was probed
            session = _PerTurnSession(ops, turn_file)
            manager, bus, _ = _manager(root, session)  # type: ignore[arg-type]
            manager.ops = ops

            first = _web("写一篇很长的文章")
            handling = asyncio.create_task(manager._handle(first))
            await _until(session.running.is_set)
            assert json.loads(turn_file.read_text())["turn"] is not None
            assert await manager.stop_turn(first.message_id, via="web") is StopResult.STOPPING
            await handling
            assert session.late[0]["error"]["code"] == "stopped" and ran == []
            assert session.files_at_exit[0] is not None  # still the stopped turn's until the runtime returned
            assert json.loads(turn_file.read_text())["turn"] is None

            await manager._handle(_web("再来一次"))
            assert session.late[1]["status"] == "ok" and ran == [{"n": 2}]
            assert json.loads(turn_file.read_text())["turn"] is None
            await bus.shutdown()

    asyncio.run(run())
