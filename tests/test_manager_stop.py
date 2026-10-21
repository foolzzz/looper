"""The Manager's typed stop for the running user turn (``ManagerAgent.stop_turn``).

A stand-in session replaces the resident Claude: its turn streams a text delta, then waits until ``interrupt``
ends it, the way a killed process does. The database is ``agent_db``'s in-memory one.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_settings
from looper.agents.bus import MessageBus
from looper.agents.ops import Turn
from looper.agents.claude_cli import ResidentTurn, user_stop_result
from looper.agents.message import MessageEnvelope, MessageType
from looper.agents.run_usage import Usage
from looper.agents.runtimes import StopMethod
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig, StopResult
from tests.test_manager_new_session import _drain, _until, _web


@pytest.fixture(autouse=True)
def _stop_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping a reply is off by default (``chat_settings``); these tests are about it working."""
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)


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


def test_the_web_page_stops_its_message_by_the_id_it_sent(agent_db: Any) -> None:
    """The page knows its message by the id it chose for the chat, not the envelope's: its Stop button names that."""
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session()
            manager, bus, queue = _manager(Path(tmp), session)
            envelope = _web("写一篇很长的文章")
            envelope.payload["channel_context"]["client_message_id"] = "m-page-1"
            handling = asyncio.create_task(manager._handle(envelope))
            await _until(session.running.is_set)
            assert await manager.stop_turn("m-page-2", via="web") is StopResult.OTHER_MESSAGE
            assert await manager.stop_turn("m-page-1", via="web") is StopResult.STOPPING
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


def test_stop_while_the_turn_is_prepared_starts_no_model(agent_db: Any) -> None:
    """A Stop during the persisting, the status and the tools probe (before the process) is kept, not ``not_running``."""
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = _web("你好")
            session = _Session()
            manager, bus, queue = _manager(Path(tmp), session)
            results: list[StopResult] = []
            persist = manager._persist_chat

            async def slow_persist(*args: Any, **kwargs: Any) -> Any:
                results.append(await manager.stop_turn(envelope.message_id, via="web"))
                return await persist(*args, **kwargs)

            manager._persist_chat = slow_persist  # type: ignore[method-assign]
            await asyncio.wait_for(manager._handle(envelope), 20)
            assert results == [StopResult.STOPPING]
            assert session.turns == [] and not session.started
            (reply,) = _replies(queue)
            assert reply["text"] == "已停止，这次没有回复。" and reply["outcome"] == "stopped"
            assert manager._current is None
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


def _held_ops(manager: ManagerAgent) -> tuple[Any, asyncio.Event, list[str]]:
    """A ``ManagerOps`` with ``looper_confirm`` over one high-risk stand-in tool whose ``escalate`` waits for the
    returned event (a database read under way); the list collects the calls it ran."""
    from looper.agents.ops import Risk, Tool
    from looper.agents.ops.service import CONFIRM, ManagerOps, Platform

    release, ran = asyncio.Event(), []

    async def run(call: Any) -> dict[str, Any]:
        ran.append(call.turn.message_id)
        return {}

    async def escalate(call: Any) -> str | None:
        await release.wait()
        return "删除 3？"

    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                     tools=(Tool("t_write", "writes", schema, Risk.WRITE, run, escalate=escalate), CONFIRM))
    ops._working_at = ops._clock()  # the MCP entry was probed
    manager.ops = ops
    return ops, release, ran


def test_a_confirmation_asked_after_the_stop_is_refused_and_never_runs(agent_db: Any) -> None:
    """A call still in ``escalate`` when the user stops the turn does not register its confirmation once it
    returns: the next turn is not told about it, and ``looper_confirm`` cannot run what the user never saw."""
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session()
            manager, bus, queue = _manager(Path(tmp), session)
            ops, release, ran = _held_ops(manager)
            calls: list[asyncio.Task[Any]] = []

            async def ask() -> None:
                calls.append(asyncio.create_task(ops.call("t_write", {})))
                await asyncio.sleep(0)  # the call took the turn and waits in escalate

            session.on_turn = ask
            envelope = _web("删掉它", 0)
            handling = asyncio.create_task(manager._handle(envelope))
            await _until(session.running.is_set)
            await manager.stop_turn(envelope.message_id, via="web")
            await handling
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped" and "需要你确认" not in reply["text"]

            release.set()
            answer = await calls[0]
            assert answer["status"] == "error" and answer["error"]["code"] == "stopped"
            assert "stopped" in answer["error"]["message"]
            assert not ops.asks_in(envelope.message_id) and not ops._pending

            # The next turn is told of no confirmation, and none can be confirmed in it.
            nxt = Turn(channel="web", chat="session-a", message_id="next")
            assert ops.begin_turn(nxt) == ""
            ops.end_turn(nxt)
            assert ran == []
            await bus.shutdown()

    asyncio.run(run())


def test_calls_that_come_after_the_stop_are_refused_as_stopped(agent_db: Any) -> None:
    """Once the user stopped the turn, a call it still makes (a write, a looper_confirm of the confirmation its
    message answered) is refused and told the turn was stopped; nothing runs."""
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session(hold=False)
            manager, bus, queue = _manager(Path(tmp), session)
            ops, release, ran = _held_ops(manager)
            release.set()

            async def ask() -> None:
                await ops.call("t_write", {})

            session.on_turn = ask
            await manager._handle(_web("删掉它", 0))
            (pending,) = ops._pending.values()
            _drain(queue)

            answers: list[dict[str, Any]] = []

            async def late() -> None:
                await manager.stop_turn(confirm.message_id, via="web")
                answers.append(await ops.call("looper_confirm", {"confirmation_id": pending.id}))
                answers.append(await ops.call("t_write", {}))

            session.hold, session.running, session._stopped = True, asyncio.Event(), asyncio.Event()
            session.on_turn = late
            confirm = _web("确认", 1)
            handling = asyncio.create_task(manager._handle(confirm))
            await _until(session.running.is_set)
            await manager._interrupt_claude(manager.current_turn)
            await handling
            assert [a["error"]["code"] for a in answers] == ["stopped", "stopped"]
            assert ran == [] and not ops._pending
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped" and "需要你确认" not in reply["text"]
            await bus.shutdown()

    asyncio.run(run())


def test_a_confirmation_asked_after_its_turn_ended_is_refused(agent_db: Any) -> None:
    """A call still in ``escalate`` when its turn ended some other way (Claude failed) registers nothing either:
    its reply was sent without it."""
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[asyncio.Task[Any]] = []

            async def ask() -> None:
                calls.append(asyncio.create_task(ops.call("t_write", {})))
                await asyncio.sleep(0)
                raise RuntimeError("Claude went away")

            session = _Session(hold=False, on_turn=ask)
            manager, bus, queue = _manager(Path(tmp), session)
            ops, release, ran = _held_ops(manager)
            envelope = _web("删掉它", 0)
            await manager._handle(envelope)
            (reply,) = _replies(queue)
            assert reply["outcome"] == "failed" and "需要你确认" not in reply["text"]
            release.set()
            answer = await calls[0]
            assert answer["status"] == "error" and answer["error"]["code"] == "expired"
            assert not ops._pending and ran == []
            await bus.shutdown()

    asyncio.run(run())


def test_a_stop_after_the_runtime_answered_ends_the_turn_stopped(agent_db: Any) -> None:
    """The stop lands after the runtime has the turn's result, before the Manager handled it (the runtime has no
    turn left to interrupt): the turn still ends stopped, never as an answer, and its confirmation is dropped."""
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session(hold=False)
            manager, bus, queue = _manager(Path(tmp), session)
            ops = _with_ops(manager)
            envelope = _web("删掉它", 0)

            async def ask_then_stop() -> None:
                await ops.call("t_write", {})
                assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING

            session.on_turn = ask_then_stop
            await manager._handle(envelope)
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped" and "error" not in reply and "需要你确认" not in reply["text"]
            assert not ops.asks_in(envelope.message_id)
            (finish,) = agent_db.finish_record.await_args_list
            assert finish.kwargs["status"] == "interrupted"
            assert manager._stopped_note is not None and manager._session_id == "sess-1"
            await bus.shutdown()

    asyncio.run(run())


def test_a_stop_once_the_turn_is_recorded_is_not_running_and_changes_nothing(agent_db: Any) -> None:
    """Once the turn's end is recorded, a stop is too late: it is refused (``NOT_RUNNING``, as for a turn that has
    ended), so the reply, its outcome and its confirmation are the turn's own."""
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session(hold=False)
            manager, bus, queue = _manager(Path(tmp), session)
            ops = _with_ops(manager)
            envelope = _web("删掉它", 0)
            results: list[StopResult] = []

            async def ask() -> None:
                await ops.call("t_write", {})

            session.on_turn = ask
            save = manager._save_session_state

            async def late_stop() -> None:
                if agent_db.finish_record.await_args_list and not results:
                    results.append(await manager.stop_turn(envelope.message_id, via="web"))
                await save()

            manager._save_session_state = late_stop  # type: ignore[method-assign]
            await manager._handle(envelope)
            assert results == [StopResult.NOT_RUNNING]
            (reply,) = _replies(queue)
            assert reply["outcome"] == "confirm" and "需要你确认" in reply["text"]
            assert ops.asks_in(envelope.message_id)
            (finish,) = agent_db.finish_record.await_args_list
            assert finish.kwargs["status"] != "interrupted"
            await bus.shutdown()

    asyncio.run(run())


def test_a_stop_whose_status_is_slow_never_interrupts_the_next_turn(agent_db: Any) -> None:
    """The stop's "Stopping…" status is slow to send while the stopped turn ends by itself (the ``stop=`` hook)
    and the next one starts: the stopped turn's interrupt never reaches the next turn."""
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class _HookSession(_Session):
                """The first turn ends at the ``stop=`` hook, as a runtime does at its next event."""

                interrupts = 0

                async def run_turn(self, text: str, on_event: Any, *, tag: Any = None, stop: Any = None) -> Any:
                    if self.turns:
                        return await super().run_turn(text, on_event, tag=tag, stop=stop)
                    self.turns.append(text)
                    await on_event({"type": "system", "subtype": "init", "session_id": "sess-1"})
                    while (capped := stop()) is None:
                        await asyncio.sleep(0.01)
                    return ResidentTurn(capped.result_event, usage=capped.usage)

                async def interrupt(self) -> StopMethod | None:
                    self.interrupts += 1
                    return await super().interrupt()

            session = _HookSession()
            manager, bus, queue = _manager(Path(tmp), session)
            status, slow = manager._send_status, asyncio.Event()

            async def slow_status(envelope: Any, label: str, **kwargs: Any) -> None:
                if label == "Stopping…":
                    await slow.wait()
                await status(envelope, label, **kwargs)

            manager._send_status = slow_status  # type: ignore[method-assign]
            first, second = _web("先来一个", 0), _web("再来一个", 1)
            handling = asyncio.create_task(manager._handle(first))
            await _until(lambda: bool(session.turns))
            stopping = asyncio.create_task(manager.stop_turn(first.message_id, via="web"))
            await handling
            nxt = asyncio.create_task(manager._handle(second))
            await _until(session.running.is_set)
            slow.set()
            assert await stopping is StopResult.STOPPING
            await asyncio.sleep(0.1)
            assert not nxt.done() and not session._stopped.is_set()  # the next turn runs on
            assert await manager.stop_turn(second.message_id, via="web") is StopResult.STOPPING
            await nxt
            assert [r["outcome"] for r in _replies(queue)] == ["stopped", "stopped"]
            await bus.shutdown()

    asyncio.run(run())


def test_a_stop_while_the_process_fails_to_start_ends_the_turn_stopped(agent_db: Any) -> None:
    """The stop lands while the runtime is started and the start fails: the turn ends stopped, not with the start's
    error reply (which the page would take for an answer)."""
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = _web("你好")
            holder: dict[str, ManagerAgent] = {}

            async def stop_then_fail() -> None:
                assert await holder["manager"].stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
                raise OSError("no such executable")

            session = _Session(on_start=stop_then_fail)
            manager, bus, queue = _manager(Path(tmp), session)
            holder["manager"] = manager
            await manager._handle(envelope)
            assert session.turns == []
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped" and reply["text"] == "已停止，这次没有回复。"
            (finish,) = agent_db.finish_record.await_args_list
            assert finish.kwargs["status"] == "interrupted"
            await bus.shutdown()

    asyncio.run(run())


def test_a_failed_process_start_replies_failed(agent_db: Any) -> None:
    """The runtime cannot start (nobody asked to stop): the reply is a failure, not an answer, so the page does not
    send the queue staged behind it."""
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async def fail() -> None:
                raise OSError("no such executable")

            session = _Session(on_start=fail)
            manager, bus, queue = _manager(Path(tmp), session)
            await manager._handle(_web("你好"))
            assert session.turns == []
            (reply,) = _replies(queue)
            assert reply["outcome"] == "failed" and "无法启动" in reply["text"]
            # As written, naming the runtime: not wrapped as the Manager's own error.
            assert reply["text"] == "无法启动 Claude Code，请检查安装。"
            assert reply["error"] == "Cannot start Claude Code: no such executable"
            await bus.shutdown()

    asyncio.run(run())
