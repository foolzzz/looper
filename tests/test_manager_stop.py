"""The Manager's typed stop for the running user turn (``ManagerAgent.stop_turn``).

A stand-in session replaces the resident Claude: its turn streams a text delta, then waits until ``interrupt``
ends it, the way a killed process does. The database is ``agent_db``'s in-memory one.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from looper.agents.bus import MessageBus
from looper.agents.ops import Turn
from looper.agents.claude_cli import ResidentTurn, user_stop_result
from looper.agents.message import MessageEnvelope, MessageType
from looper.agents.run_usage import Usage
from looper.agents.runtimes import StopMethod
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig, StopResult
from tests.test_manager_new_session import _drain, _until, _web


class _Session:
    """A resident session stand-in: a turn waits for ``interrupt`` unless ``hold`` is off."""

    label = "manager"
    pid = None

    def __init__(self, *, hold: bool = True, on_start: Any = None, on_turn: Any = None) -> None:
        self.hold, self.on_start, self.on_turn = hold, on_start, on_turn
        self.started = False
        self.command: list[str] = []
        self.totals = Usage()
        self.turns: list[str] = []
        self.running = asyncio.Event()
        self._stopped = asyncio.Event()

    def stale(self, signature: Any) -> bool:
        return False

    async def start(self, command: list[str], **kwargs: Any) -> None:
        self.started, self.command = True, command
        if self.on_start is not None:
            await self.on_start()

    async def run_turn(self, text: str, on_event: Any, *, tag: Any = None, stop: Any = None) -> ResidentTurn:
        self.turns.append(text)
        delta = {"type": "text_delta", "text": "半截回复"}
        await on_event({"type": "system", "subtype": "init", "session_id": "sess-1"})
        await on_event({"type": "stream_event", "event": {"type": "content_block_delta", "delta": delta}})
        await on_event({"type": "assistant", "message": {"content": [{"type": "text", "text": "半截回复"}]}})
        if self.on_turn is not None:
            await self.on_turn()
        if not self.hold:
            result = {"type": "result", "subtype": "success", "is_error": False, "result": "好的",
                      "session_id": "sess-1"}
            return ResidentTurn(result)
        self.running.set()
        await self._stopped.wait()
        return ResidentTurn(user_stop_result(), stopped=StopMethod.KILL, usage=Usage(cost_usd=0.01))

    async def interrupt(self) -> StopMethod | None:
        if not self.running.is_set() or self._stopped.is_set():
            return None
        self._stopped.set()
        return StopMethod.KILL

    async def stop(self, *, kill: bool = False) -> None:
        self.started = False


def _manager(root: Path, session: _Session) -> tuple[ManagerAgent, MessageBus, asyncio.Queue[MessageEnvelope]]:
    from tests.test_manager_reply import WEB

    bus = MessageBus(runtime_root=root)
    manager = ManagerAgent(config=ManagerConfig(claude_bin="claude", work_dir=root), runtime_root=root)
    manager._bus = bus
    manager._claude = session  # type: ignore[assignment]
    return manager, bus, bus.get_queue(WEB.agent_id)


def _replies(queue: asyncio.Queue[MessageEnvelope]) -> list[dict[str, Any]]:
    return [e.payload for e in _drain(queue) if e.message_type == MessageType.AGENT_REPLY]


def test_stop_while_the_turn_runs(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session()
            manager, bus, queue = _manager(Path(tmp), session)
            envelope = _web("写一篇很长的文章")
            handling = asyncio.create_task(manager._handle(envelope))
            await _until(session.running.is_set)
            assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
            assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.ALREADY_STOPPING
            await handling
            (reply,) = _replies(queue)
            assert reply["text"].endswith("（已停止）") and reply["text"].startswith("半截回复")
            assert "error" not in reply and reply["outcome"] == "stopped"
            (finish,) = agent_db.finish_record.await_args_list
            assert finish.kwargs["status"] == "interrupted"
            assert finish.kwargs["cost_usd"] == 0.01
            (_, notes), = [call.args for call in agent_db.add_metadata.await_args_list if "user_stop" in call.args[1]]
            assert notes["user_stop"]["via"] == "web" and notes["user_stop"]["method"] == "kill"
            assert manager._session_id == "sess-1"
            assert manager._current is None
            await bus.shutdown()

    asyncio.run(run())


def test_stop_of_another_message_or_when_idle(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session()
            manager, bus, queue = _manager(Path(tmp), session)
            assert await manager.stop_turn("nothing", via="web") is StopResult.NOT_RUNNING
            envelope = _web("写一篇很长的文章")
            handling = asyncio.create_task(manager._handle(envelope))
            await _until(session.running.is_set)
            assert await manager.stop_turn("other", via="web") is StopResult.OTHER_MESSAGE
            assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
            await handling
            await bus.shutdown()

    asyncio.run(run())


def test_stop_before_the_process_starts(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = _web("你好")
            holder: dict[str, ManagerAgent] = {}

            async def stop_now() -> None:
                result = await holder["manager"].stop_turn(envelope.message_id, via="web")
                assert result is StopResult.STOPPING

            session = _Session(on_start=stop_now)
            manager, bus, queue = _manager(Path(tmp), session)
            holder["manager"] = manager
            await manager._handle(envelope)
            assert session.turns == []
            (reply,) = _replies(queue)
            assert reply["text"] == "已停止，这次没有回复。" and "error" not in reply
            (finish,) = agent_db.finish_record.await_args_list
            assert finish.kwargs["status"] == "interrupted" and finish.kwargs["cost_usd"] is None
            await bus.shutdown()

    asyncio.run(run())


def test_an_ordinary_turn_after_a_stopped_one(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session()
            manager, bus, queue = _manager(Path(tmp), session)
            first = _web("先来一个", 0)
            handling = asyncio.create_task(manager._handle(first))
            await _until(session.running.is_set)
            await manager.stop_turn(first.message_id, via="web")
            await handling
            _drain(queue)
            session.hold = False
            await manager._handle(_web("再来一个", 1))
            (reply,) = _replies(queue)
            assert reply["text"] == "好的" and "error" not in reply and reply["outcome"] == "answered"
            await bus.shutdown()

    asyncio.run(run())


def _with_ops(manager: ManagerAgent) -> Any:
    """A ``ManagerOps`` over one high-risk stand-in tool that always asks to confirm."""
    from looper.agents.ops import Risk, Tool
    from looper.agents.ops.service import ManagerOps, Platform

    async def run(call: Any) -> dict[str, Any]:
        return {}

    async def escalate(call: Any) -> str | None:
        return "删除 3？"

    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                     tools=(Tool("t_write", "writes", schema, Risk.WRITE, run, escalate=escalate),))
    ops._working_at = ops._clock()  # the MCP entry was probed
    manager.ops = ops
    return ops


def test_an_error_reply_is_failed_and_a_turn_that_asked_to_confirm_is_confirm(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session(hold=False)
            manager, bus, queue = _manager(Path(tmp), session)
            ops = _with_ops(manager)
            message = _web("删掉它", 0)

            async def ask() -> None:
                await ops.call("t_write", {})

            session.on_turn = ask
            await manager._handle(message)
            (reply,) = _replies(queue)
            assert reply["outcome"] == "confirm" and "需要你确认" in reply["text"]

            await manager._send_reply(message, "坏了", error="boom")
            (failed,) = _replies(queue)
            assert failed["outcome"] == "failed" and failed["error"] == "boom"
            await bus.shutdown()

    asyncio.run(run())


def test_a_stopped_turn_that_asked_to_confirm_leaves_no_confirmation(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session()
            manager, bus, queue = _manager(Path(tmp), session)
            ops = _with_ops(manager)

            async def ask() -> None:
                await ops.call("t_write", {})

            session.on_turn = ask
            envelope = _web("删掉它", 0)
            handling = asyncio.create_task(manager._handle(envelope))
            await _until(session.running.is_set)
            assert ops.asks_in(envelope.message_id)
            await manager.stop_turn(envelope.message_id, via="web")
            await handling
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped" and "需要你确认" not in reply["text"]
            assert not ops.asks_in(envelope.message_id)
            assert ops.begin_turn(Turn(channel="web", chat=envelope.payload.get("chat", "web"),
                                       message_id="next")) == ""
            await bus.shutdown()

    asyncio.run(run())


def test_the_turn_after_a_stop_is_told_once_and_new_clears_it(agent_db: Any) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session()
            manager, bus, queue = _manager(Path(tmp), session)
            first = _web("先来一个" + "很长" * 50, 0)
            handling = asyncio.create_task(manager._handle(first))
            await _until(session.running.is_set)
            await manager.stop_turn(first.message_id, via="web")
            await handling
            session.hold = False
            await manager._handle(_web("再来一个", 1))
            await manager._handle(_web("第三个", 2))
            assert session.turns[1].count("[Looper] The user stopped your previous reply") == 1
            assert f'(to: "{" ".join(first.payload["text"].split())[:80]}")' in session.turns[1]
            assert "before it finished. Tool calls it had already made were not undone" in session.turns[1]
            assert "[Looper] The user stopped" not in session.turns[2]

            # /new between the stop and the next turn clears it
            session.hold = True
            session.running.clear()
            session._stopped.clear()
            fourth = _web("第四个", 3)
            handling = asyncio.create_task(manager._handle(fourth))
            await _until(session.running.is_set)
            await manager.stop_turn(fourth.message_id, via="web")
            await handling
            assert manager._stopped_note is not None
            await manager._handle(_web("/new", 4))
            assert manager._stopped_note is None
            session.hold = False
            await manager._handle(_web("第五个", 5))
            assert "[Looper] The user stopped" not in session.turns[-1]
            await bus.shutdown()

    asyncio.run(run())
