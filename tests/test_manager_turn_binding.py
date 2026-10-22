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
    # Stoppable, though Looper's per-turn runtimes are not (they have no interrupt): what is tested is the binding of
    # a call that comes late, which a turn bridged per turn has whatever ended it.
    can_interrupt = True

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


class _ResidentSession(_PerTurnSession):
    """A resident runtime (one bridge shared by the turns): its turn streams tool_use events, then calls."""

    shares_bridge = True

    def __init__(self, ops: ManagerOps, turn_file: Path, script: Any) -> None:
        super().__init__(ops, turn_file)
        self.script = script

    async def run_turn(self, text: str, on_event: Any, *, tag: Any = None, stop: Any = None,
                       cancel: Any = None) -> ResidentTurn:
        binding = json.loads(self.turn_file.read_text())["turn"]
        await on_event({"type": "system", "subtype": "init", "session_id": "sess-1"})
        await self.script(self, binding, on_event)
        return ResidentTurn({"type": "result", "subtype": "success", "is_error": False, "result": "好的",
                             "session_id": "sess-1"})


def _tool_use(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "parent_tool_use_id": None, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": call_id, "name": name, "input": arguments}]}}


def _resident(script: Any, *, per_turn: bool = False, swapped_from: Any = None,
              ) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
    """Run one turn of a runtime driven by ``script``; returns (script's answers, writes run, reads run).
    ``swapped_from``: the Manager's session until then is a per-turn one of that runtime, swapped for this one
    when the turn starts."""
    answers: list[Any] = []
    writes: list[dict[str, Any]] = []
    reads: list[str] = []

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            turn_file = root / ".local" / "manager-turn.json"

            async def write(call: Call) -> dict[str, Any]:
                writes.append(dict(call.arguments))
                return {"ok": True}

            async def read(call: Call) -> dict[str, Any]:
                reads.append("read")
                return {"ok": True}

            schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
            ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                             tools=(Tool("t_write", "writes", schema, Risk.WRITE, write),
                                    Tool("t_read", "reads", schema, Risk.READ, read)), turn_file=turn_file)
            ops._working_at = ops._clock()
            session = _ResidentSession(ops, turn_file, script)
            session.shares_bridge = not per_turn
            session.answers = answers  # type: ignore[attr-defined]
            if swapped_from is None:
                manager, bus, _ = _manager(root, session)  # type: ignore[arg-type]
            else:
                from looper.agents.runtimes import CLAUDE_CODE

                before = _PerTurnSession(ops, turn_file)
                manager, bus, _ = _manager(root, before)  # type: ignore[arg-type]
                manager._claude_runtime = swapped_from
                assert CLAUDE_CODE is not swapped_from
                CLAUDE_CODE.resident_session = lambda label: session  # type: ignore[method-assign]
            manager.ops = ops
            try:
                await manager._handle(_web("改一下"))
            finally:
                if swapped_from is not None:
                    del CLAUDE_CODE.resident_session  # type: ignore[attr-defined]
                await bus.shutdown()

    asyncio.run(run())
    return answers, writes, reads


def test_resident_write_matching_the_streamed_tool_use_runs(agent_db: Any) -> None:
    async def script(session: Any, binding: str, on_event: Any) -> None:
        await on_event(_tool_use("tu1", "mcp__looper__t_write", {"n": 1}))
        await on_event(_tool_use("tu2", "t_write", {"n": 2}))
        session.answers.append(await session.ops.call("t_write", {"n": 1}, binding=binding, call_id="tu1"))
        session.answers.append(await session.ops.call("t_write", {"n": 2}, binding=binding))

    answers, writes, _ = _resident(script)
    assert [a["status"] for a in answers] == ["ok", "ok"] and writes == [{"n": 1}, {"n": 2}]


def test_resident_write_without_a_matching_tool_use_is_refused_after_the_wait(
        agent_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.ops import service

    monkeypatch.setattr(service, "MATCH_WAIT_SEC", 0.2)

    async def script(session: Any, binding: str, on_event: Any) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        session.answers.append(await session.ops.call("t_write", {"n": 1}, binding=binding))
        session.answers.append(loop.time() - started)
        session.answers.append(await session.ops.call("t_write", {"n": 1}, binding=binding))  # same call, other turn
        await on_event(_tool_use("tu1", "mcp__looper__t_write", {"n": 9}))  # a different call's tool_use
        session.answers.append(await session.ops.call("t_write", {"n": 1}, binding=binding))

    answers, writes, _ = _resident(script)
    assert answers[0]["error"]["code"] == "not_this_turn" and 0.15 <= answers[1] < 1.5
    assert answers[2]["error"]["code"] == "not_this_turn" and answers[3]["error"]["code"] == "not_this_turn"
    assert writes == []


def test_resident_read_runs_without_a_match_and_per_turn_never_waits(
        agent_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.ops import service

    monkeypatch.setattr(service, "MATCH_WAIT_SEC", 0.2)  # the resident write below is refused after the wait

    async def script(session: Any, binding: str, on_event: Any) -> None:
        session.answers.append(await session.ops.call("t_read", {}, binding=binding))
        session.answers.append(await session.ops.call("t_write", {"n": 1}, binding=binding))

    answers, writes, reads = _resident(script)
    assert answers[0]["status"] == "ok" and answers[1]["error"]["code"] == "not_this_turn" and reads == ["read"]

    answers, writes, _ = _resident(script, per_turn=True)
    assert answers[1]["status"] == "ok" and writes == [{"n": 1}]


def test_resident_tool_use_trailing_the_call_still_matches(agent_db: Any) -> None:
    async def script(session: Any, binding: str, on_event: Any) -> None:
        later = asyncio.create_task(_later(on_event))
        session.answers.append(await session.ops.call("t_write", {"n": 1}, binding=binding, call_id="tu1"))
        await later

    async def _later(on_event: Any) -> None:
        await asyncio.sleep(0.2)
        await on_event(_tool_use("tu1", "mcp__looper__t_write", {"n": 1}))

    answers, writes, _ = _resident(script)
    assert answers[0]["status"] == "ok" and writes == [{"n": 1}]


def test_resident_expectation_is_consumed_once(agent_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.ops import service

    monkeypatch.setattr(service, "MATCH_WAIT_SEC", 0.1)

    async def script(session: Any, binding: str, on_event: Any) -> None:
        await on_event(_tool_use("tu1", "mcp__looper__t_write", {"n": 1}))
        for _ in range(2):
            session.answers.append(await session.ops.call("t_write", {"n": 1}, binding=binding, call_id="tu1"))

    answers, writes, _ = _resident(script)
    assert answers[0]["status"] == "ok" and answers[1]["error"]["code"] == "not_this_turn" and writes == [{"n": 1}]


def test_resident_write_of_a_subagent_matches_its_streamed_tool_use(
        agent_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # A subagent's tool_use carries its parent's id: it is no reply text, and still a call of this turn.
    from looper.agents.ops import service

    monkeypatch.setattr(service, "MATCH_WAIT_SEC", 0.2)

    async def script(session: Any, binding: str, on_event: Any) -> None:
        nested = _tool_use("tu-sub", "mcp__looper__t_write", {"n": 3})
        nested["parent_tool_use_id"] = "tu-task"
        nested["message"]["content"].insert(0, {"type": "text", "text": "subagent's own words"})
        await on_event(nested)
        session.answers.append(await session.ops.call("t_write", {"n": 3}, binding=binding, call_id="tu-sub"))

    answers, writes, _ = _resident(script)
    assert answers[0]["status"] == "ok", answers
    assert writes == [{"n": 3}]


@pytest.mark.parametrize("tool_input", [
    {"tool_name": "looper__t_write", "tool_input": {"n": 4}},
    {"tool_name": "looper__t_write", "tool_input": json.dumps({"n": 4})},
    {"tool_name": "mcp__looper__t_write", "tool_input": {"n": 4}},
])
def test_resident_grok_use_tool_matches_the_inner_tool_and_arguments(
        agent_db: Any, monkeypatch: pytest.MonkeyPatch, tool_input: dict[str, Any]) -> None:
    # Grok ACP calls Looper's tools through its use_tool meta-tool; its MCP call carries no call id.
    from looper.agents.ops import service

    monkeypatch.setattr(service, "MATCH_WAIT_SEC", 0.2)

    async def script(session: Any, binding: str, on_event: Any) -> None:
        await on_event(_tool_use("acp-1", "use_tool", tool_input))
        session.answers.append(await session.ops.call("t_write", {"n": 4}, binding=binding))

    answers, writes, _ = _resident(script)
    assert answers[0]["status"] == "ok", answers
    assert writes == [{"n": 4}]


def _text(words: str) -> dict[str, Any]:
    return {"type": "assistant", "parent_tool_use_id": None, "message": {"role": "assistant", "content": [
        {"type": "text", "text": words}]}}


@pytest.mark.parametrize("slow_event", ["earlier", "same"])
def test_resident_write_matches_though_recording_the_events_is_slow(
        agent_db: Any, monkeypatch: pytest.MonkeyPatch, slow_event: str) -> None:
    # The runtime hands its events on in order, one at a time; recording one (a database write) takes longer than
    # the match waits. The bridge's call comes as the tool_use is read: it still matches, as soon as it is read.
    from looper.db import llm_records
    from looper.agents.ops import service

    monkeypatch.setattr(service, "MATCH_WAIT_SEC", 0.3)
    real_append = llm_records.append_events

    async def slow_append(record_id: Any, events: Any, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.8)
        return await real_append(record_id, events, *args, **kwargs)

    monkeypatch.setattr(llm_records, "append_events", slow_append)

    async def script(session: Any, binding: str, on_event: Any) -> None:
        async def stream() -> None:  # the runtime's read loop: the next event only once the last is handled
            if slow_event == "earlier":
                await on_event(_text("我来改一下"))
            await on_event(_tool_use("tu1", "mcp__looper__t_write", {"n": 1}))

        reading = asyncio.create_task(stream())
        await asyncio.sleep(0)
        session.answers.append(await session.ops.call("t_write", {"n": 1}, binding=binding, call_id="tu1"))
        await reading

    answers, writes, _ = _resident(script)
    assert answers[0]["status"] == "ok", answers
    assert writes == [{"n": 1}]


def test_resident_write_is_matched_in_the_turn_that_swaps_a_per_turn_runtime_for_it(
        agent_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The Manager's session was a per-turn runtime's (no matching); this turn's settings name Claude Code, so the
    # turn starts its resident session: the turn's calls are matched from its first turn on.
    from looper.agents.ops import service
    from looper.agents.runtimes import ADAPTERS

    monkeypatch.setattr(service, "MATCH_WAIT_SEC", 0.2)

    async def script(session: Any, binding: str, on_event: Any) -> None:
        session.answers.append(await session.ops.call("t_write", {"n": 1}, binding=binding))  # no tool_use of it
        await on_event(_tool_use("tu1", "mcp__looper__t_write", {"n": 2}))
        session.answers.append(await session.ops.call("t_write", {"n": 2}, binding=binding, call_id="tu1"))

    answers, writes, _ = _resident(script, swapped_from=ADAPTERS["omp"])
    assert answers[0]["error"]["code"] == "not_this_turn", answers
    assert answers[1]["status"] == "ok" and writes == [{"n": 2}]
