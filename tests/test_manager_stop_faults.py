"""A stop at each await of the Manager's turn preparation and start (``ManagerAgent._prepare_turn`` and
``_start_turn``): every await there is a ``token.step`` or ``token.keep``, so the turn ends stopped at once."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import dataclasses
import inspect
import json
import os
import signal
import tempfile
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents import proc_tree
from looper.agents.claude_cli import ResidentClaude, ResidentTurn, user_stop_result
from looper.agents.ops import Call, Risk, Tool
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.runtimes import StopMethod
from looper.agents.runtimes import cancel as cancel_module
from looper.agents.runtimes.cancel import TurnCancelled
from looper.agents.tiers import manager as manager_module
from looper.agents.tiers.manager import PREPARATION_STEPS, ManagerAgent, StopResult
from looper.agents.tiers.turn_token import TurnPhase
from tests.db_support import until
from tests.stop_faults import hold_at
from tests.stop_matrix_support import _alive
from tests.test_manager_new_session import _web
from tests.test_manager_stop import _manager, _replies, _Session, _stop_on  # noqa: F401


def _functions() -> list[ast.AsyncFunctionDef]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(manager_module)))
    (cls,) = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == ManagerAgent.__name__]
    return [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in ("_prepare_turn", "_start_turn")]


def test_preparation_awaits_only_through_steps() -> None:
    functions = _functions()
    assert len(functions) == 2
    names: set[str] = set()
    for function in functions:
        for node in ast.walk(function):
            assert not isinstance(node, (ast.AsyncWith, ast.AsyncFor)), f"{function.name}:{node.lineno}"
            if not isinstance(node, ast.Await):
                continue
            call = node.value
            assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                and call.func.attr in ("step", "keep") and isinstance(call.func.value, ast.Name) \
                and call.func.value.id == "token", f"{function.name}:{node.lineno} awaits outside token.step/keep"
            first = call.args[0]
            assert isinstance(first, ast.Constant) and isinstance(first.value, str)
            names.add(first.value)
    assert names == set(PREPARATION_STEPS)


class _Stale(_Session):
    def stale(self, signature: Any) -> bool:
        return self.started


def _setup(name: str, manager: ManagerAgent, session: _Session, agent_db: Any) -> _Session:
    """The state in which a turn reaches the step ``name``."""
    match name:
        case "switch_session":
            manager._session_loaded = True
            manager._session_id, manager._session_runtime = "old-session", "omp"
        case "claude_stop":
            session.started = True
            session.stale = lambda signature: True  # type: ignore[method-assign]
        case "recorder_finish":
            async def refuse() -> None:
                raise OSError("no such binary")
            session.on_start = refuse
        case "recorder_annotate":
            async def other_command() -> None:
                session.command = ["claude", "--other"]
            session.on_start = other_command
    return session


@pytest.mark.parametrize("name", PREPARATION_STEPS)
def test_a_stop_at_each_preparation_step(name: str, agent_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session(hold=False)
            manager, bus, queue = _manager(Path(tmp), session)
            _setup(name, manager, session, agent_db)
            hold = hold_at(monkeypatch, name)
            envelope = _web("你好")
            handling = asyncio.create_task(manager._handle(envelope))
            await asyncio.wait_for(hold.reached.wait(), 5)
            token = manager.turn_for(envelope.message_id)
            assert token is not None
            assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
            assert token.phase is TurnPhase.STOPPING
            hold.release()
            await asyncio.wait_for(handling, 2)
            assert session.turns == []
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped" and "error" not in reply
            assert not token.live and manager.turn_for(envelope.message_id) is None
            for call in agent_db.finish_record.await_args_list:
                assert call.kwargs["status"] != "completed"
            session.stale = lambda signature: False  # type: ignore[method-assign]
            session.on_start = None
            session.started = False
            manager._session_id = None
            monkeypatch.setattr("looper.agents.tiers.turn_token.FAULTS", None)
            await asyncio.wait_for(manager._handle(_web("再来一个", 1)), 5)
            (answer,) = _replies(queue)
            assert answer["text"] == "好的" and answer["outcome"] == "answered"
            await bus.shutdown()

    asyncio.run(run())


def test_a_stop_after_a_resident_result_while_persistence_is_blocked_ends_the_turn_and_signals_nothing(
    tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager_module, "STOP_HANDLING_SEC", 0.3, raising=False)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)
    claude = fake_claude(tmp_path / "claude", """
import json, os, subprocess, sys, time
sys.stdin.readline()
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess-1'}), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'ok'}), flush=True)
while not os.path.exists('spawn'):
    time.sleep(0.01)
child = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open('child.pid', 'w').write(str(child.pid))
sys.stdin.read()
""")

    async def run() -> None:
        session = ResidentClaude(label="manager")
        manager, bus, queue = _manager(tmp_path, session)  # type: ignore[arg-type]
        manager._config.claude_bin = claude
        persisting, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_run = session.run_turn

        async def run_turn(*args: Any, **kwargs: Any) -> Any:
            turn = await original_run(*args, **kwargs)
            returned.set()
            return turn

        async def blocked() -> None:
            if not persisting.is_set():
                persisting.set()
                await release.wait()

        monkeypatch.setattr(session, "run_turn", run_turn)
        monkeypatch.setattr(manager, "_save_session_state", blocked)
        signalled: list[int] = []
        real_signal = proc_tree._signal

        def signal_(process: Any, tree: Any, sig: int) -> None:
            signalled.append(sig)
            real_signal(process, tree, sig)

        monkeypatch.setattr(proc_tree, "_signal", signal_)
        (tmp_path / "spawn").touch()
        envelope = _web("你好")
        handling = asyncio.create_task(manager._handle(envelope))
        child = None
        try:
            await asyncio.wait_for(persisting.wait(), 8)
            await asyncio.wait_for(returned.wait(), 8)  # the real runtime dropped its cancellation callback
            await until(lambda: (tmp_path / "child.pid").exists(), timeout=2)
            child = int((tmp_path / "child.pid").read_text())
            assert _alive(child)
            assert session.started
            assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
            await asyncio.wait_for(asyncio.shield(handling), 5)
            assert not release.is_set()  # the stop does not wait for the database
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped" and "error" not in reply
            assert session.started and _alive(child)  # the turn is over; Claude and what it started are its own
            assert signalled == []
        finally:
            release.set()
            if not handling.done():
                handling.cancel()
            await asyncio.gather(handling, return_exceptions=True)
            await session.stop(kill=True)
            if child is not None and _alive(child):
                os.kill(child, signal.SIGKILL)
            await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("ending", ["answer", "stop", "error", "close_cancel"])
def test_a_bridge_write_during_event_drain_keeps_the_user_turn_until_finalization(
    ending: str, tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        session = _Session(hold=False)
        manager, bus, queue = _manager(tmp_path, session)
        turn_file = tmp_path / "turn.json"
        ran: list[Call] = []

        async def write(call: Call) -> dict[str, Any]:
            ran.append(call)
            return {"written": True}

        tool = Tool("t_write", "write", {"type": "object", "properties": {}}, Risk.WRITE, write)
        ops = ManagerOps(Platform(bus=bus, task_router=None, manager=manager), url="http://127.0.0.1:1",
                         tools=(tool,), turn_file=turn_file)
        ops._working_at = ops._clock()
        manager.ops = ops
        records = AsyncMock()
        monkeypatch.setattr("looper.agents.ops.service.db_actions.record", records)
        persisting, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        closing = asyncio.Event()
        original_run = session.run_turn
        original_close = manager_module.ClaudeRunRecorder.end_left_open

        async def close(self: Any, failure: BaseException | None = None) -> None:
            await original_close(self, failure)
            if ending == "close_cancel":
                closing.set()
                await asyncio.Event().wait()

        async def run_turn(text: str, on_event: Any, **kwargs: Any) -> Any:
            await on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "write-1", "name": "mcp__looper__t_write", "input": {}},
            ]}})
            turn = await original_run(text, on_event, **kwargs)
            returned.set()
            if ending == "error":
                await persisting.wait()
                raise OSError("resident read failed")
            return turn

        async def persist(self: Any, payload: dict[str, Any]) -> None:
            persisting.set()
            await release.wait()

        monkeypatch.setattr(session, "run_turn", run_turn)
        monkeypatch.setattr(manager_module.ClaudeRunRecorder, "persist", persist)
        monkeypatch.setattr(manager_module.ClaudeRunRecorder, "end_left_open", close)
        envelope = _web("write this")
        handling = asyncio.create_task(manager._handle(envelope))
        try:
            await asyncio.wait_for(returned.wait(), 5)
            await asyncio.wait_for(persisting.wait(), 5)
            if ending != "error":
                binding = json.loads(turn_file.read_text())["turn"]  # exactly what the MCP bridge reads
                result = await ops.call("t_write", {}, binding=binding, call_id="write-1")
                assert result["status"] == "ok", result
                assert ran[0].turn.message_id == envelope.message_id
                assert records.call_args.kwargs["message_id"] == envelope.message_id
                if ending == "stop":
                    assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
            release.set()
            if ending == "close_cancel":
                await asyncio.wait_for(closing.wait(), 5)
                assert json.loads(turn_file.read_text())["turn"] == binding
                handling.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await handling
            else:
                await asyncio.wait_for(handling, 5)
            assert json.loads(turn_file.read_text())["turn"] is None
            if ending != "close_cancel":
                (reply,) = _replies(queue)
                assert reply["outcome"] == {"answer": "answered", "stop": "stopped", "error": "failed"}[ending]
            assert session.started is (ending != "error")
        finally:
            release.set()
            handling.cancel()
            await asyncio.gather(handling, return_exceptions=True)
            await session.stop()
            await bus.shutdown()

    asyncio.run(run())


def test_a_stop_while_the_init_event_is_persisted_keeps_its_session(
    agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The database holds the persistence of the turn's first event (init, naming the session) until the stop has
    # given up on the handling: the session it names is still saved, so the next process resumes it.
    monkeypatch.setattr(manager_module, "STOP_HANDLING_SEC", 0.3, raising=False)

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session()
            manager, bus, queue = _manager(Path(tmp), session)
            envelope = _web("你好")
            persisting, release = asyncio.Event(), asyncio.Event()
            saved: list[str | None] = []

            async def blocked(self: Any, payload: dict[str, Any]) -> None:
                if not persisting.is_set():
                    persisting.set()
                    await release.wait()

            async def save() -> None:
                saved.append(manager._session_id)

            monkeypatch.setattr(manager_module.ClaudeRunRecorder, "persist", blocked)
            manager._save_session_state = save  # type: ignore[method-assign]
            handling = asyncio.create_task(manager._handle(envelope))
            try:
                await asyncio.wait_for(persisting.wait(), 5)
                await asyncio.wait_for(session.running.wait(), 5)
                assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
                await asyncio.wait_for(asyncio.shield(handling), 5)
                assert not release.is_set()  # the handling was given up on
                (reply,) = _replies(queue)
                assert reply["outcome"] == "stopped"
                assert manager._session_id == "sess-1"
                assert saved and saved[-1] == "sess-1"
            finally:
                release.set()
                await asyncio.gather(handling, return_exceptions=True)
                await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize(("streamed", "release_persist"), [(False, True), (True, True), (True, False)])
@pytest.mark.parametrize("cancel_escapes", [False, True])
def test_a_stopped_reply_keeps_assistant_text_or_falls_back_to_streamed_text(
    streamed: bool, release_persist: bool, cancel_escapes: bool,
    tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager_module, "STOP_HANDLING_SEC", 0.3)

    async def run() -> None:
        session = _Session()
        manager, bus, queue = _manager(tmp_path, session)
        persisting, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        prefix, suffix = "半截回复", "，完整答案"
        original_persist = manager_module.ClaudeRunRecorder.persist

        async def persist(self: Any, payload: dict[str, Any]) -> None:
            # Without deltas the entire assistant event queues behind init; with deltas it is its own write
            # that lags, after the streamed prefix has already reached the chat.
            if payload["type"] == ("assistant" if streamed else "system"):
                persisting.set()
                await release.wait()
            await original_persist(self, payload)

        async def run_turn(text: str, on_event: Any, *, cancel: Any, **kwargs: Any) -> ResidentTurn:
            cancel.on_request(stopped.set)
            await on_event({"type": "system", "subtype": "init", "session_id": "sess-1"})
            if streamed:
                await on_event({"type": "stream_event", "event": {
                    "delta": {"type": "text_delta", "text": prefix}}})
            await on_event({"type": "assistant", "message": {"content": [
                {"type": "text", "text": prefix}, {"type": "text", "text": suffix}]}})
            await stopped.wait()
            if cancel_escapes:
                raise TurnCancelled()
            return ResidentTurn(user_stop_result(), stopped=StopMethod.KILL, prompt_sent=True)

        monkeypatch.setattr(manager_module.ClaudeRunRecorder, "persist", persist)
        monkeypatch.setattr(session, "run_turn", run_turn)
        envelope = _web("你好")
        handling = asyncio.create_task(manager._handle(envelope))
        try:
            await asyncio.wait_for(persisting.wait(), 5)
            token = manager.turn_for(envelope.message_id)
            assert token is not None and token.partial == (prefix if streamed else "")
            assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
            if release_persist:
                release.set()
            await asyncio.wait_for(asyncio.shield(handling), 5)
            expected = prefix + suffix if release_persist else prefix
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped" and "error" not in reply
            assert reply["text"] == f"{expected}\n\n（已停止）"
            (finish,) = agent_db.finish_record.await_args_list
            assert finish.kwargs["output_text"] == expected
            assert finish.kwargs["status"] == "interrupted"
        finally:
            release.set()
            if not handling.done():
                handling.cancel()
            await asyncio.gather(handling, return_exceptions=True)
            await session.stop(kill=True)
            await bus.shutdown()

    asyncio.run(run())


def test_a_stop_during_the_user_message_persist_still_stores_it(agent_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session(hold=False)
            manager, bus, queue = _manager(Path(tmp), session)
            stored: list[str] = []

            async def persist(env: Any, role: str, content: str, message_id: str) -> None:
                stored.append(content)

            manager._persist_chat = persist  # type: ignore[method-assign]
            hold = hold_at(monkeypatch, "persist_user_message")
            envelope = _web("别丢了我")
            handling = asyncio.create_task(manager._handle(envelope))
            await asyncio.wait_for(hold.reached.wait(), 5)
            await manager.stop_turn(envelope.message_id, via="web")
            hold.release()
            await asyncio.wait_for(handling, 2)
            assert stored == ["别丢了我"]
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped"
            assert session.turns == []
            await bus.shutdown()

    asyncio.run(run())


def test_a_stop_while_the_events_are_handled_waits_for_the_handling_within_a_bound(
    agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The database holds the handling of the turn's events (persisting the session) after the runtime answered: the
    # stop is taken (no 409), and the turn ends stopped once the handling has had its time.
    monkeypatch.setattr(manager_module, "STOP_HANDLING_SEC", 0.3, raising=False)

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session(hold=False)
            manager, bus, queue = _manager(Path(tmp), session)
            envelope = _web("你好")
            persisting, release = asyncio.Event(), asyncio.Event()

            async def blocked() -> None:
                if not persisting.is_set():  # the first write, while the events are handled
                    persisting.set()
                    await release.wait()

            manager._save_session_state = blocked  # type: ignore[method-assign]
            handling = asyncio.create_task(manager._handle(envelope))
            try:
                await asyncio.wait_for(persisting.wait(), 5)
                assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
                await asyncio.wait_for(asyncio.shield(handling), 5)
                assert not release.is_set()
                (reply,) = _replies(queue)
                assert reply["outcome"] == "stopped"
            finally:
                release.set()
                await asyncio.gather(handling, return_exceptions=True)
                await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize(("held", "held_writes"), [
    ((manager_module.db_agent_sessions, "save_session"), 2),  # the handling's, then the stopped turn's
    ((manager_module.db_llm_records, "add_metadata"), 1),  # the stopped turn's annotation
], ids=["session", "record"])
def test_a_stop_while_the_database_holds_every_write_still_replies_and_frees_the_lock(
    held: tuple[Any, str], held_writes: int, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A row lock held elsewhere and never released holds every write to its table, the handling's and the stopped
    # turn's own: the turn still ends stopped within a bound, replies, and leaves the Manager's lock free.
    monkeypatch.setattr(manager_module, "STOP_HANDLING_SEC", 0.3, raising=False)
    monkeypatch.setattr(manager_module, "STOP_FINISH_SEC", 0.3, raising=False)

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session()
            manager, bus, queue = _manager(Path(tmp), session)
            envelope = _web("你好")
            unlocked = asyncio.Event()
            writes: list[Any] = []

            async def locked(*args: Any, **kwargs: Any) -> None:
                writes.append(args)
                await unlocked.wait()

            monkeypatch.setattr(*held, locked)
            handling = asyncio.create_task(manager._handle(envelope))
            try:
                await asyncio.wait_for(session.running.wait(), 5)
                assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
                await asyncio.wait_for(asyncio.shield(handling), 5)
                assert not unlocked.is_set() and len(writes) == held_writes  # the stopped turn's own was given up on
                (reply,) = _replies(queue)
                assert reply["outcome"] == "stopped" and "error" not in reply
                assert not manager.busy and manager._lock_owner is None
                assert manager.turn_for(envelope.message_id) is None
                (finish,) = agent_db.finish_record.await_args_list
                assert finish.kwargs["status"] == "interrupted"
            finally:
                unlocked.set()
                if not handling.done():
                    handling.cancel()
                await asyncio.gather(handling, return_exceptions=True)
                await bus.shutdown()

    asyncio.run(run())


class _Unreachable:
    """A database call once the network to the server drops: it never returns, and once cancelled it never ends
    either (asyncpg hands the connection back to its pool behind a shield, and resetting it waits on the server);
    here not even a second cancel ends it, until ``restore()``."""

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = 0  # cancelled, and still cleaning up
        self.reached = asyncio.Event()
        self._back = asyncio.Event()

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        self.reached.set()
        try:
            await self._back.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            while not self._back.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await self._back.wait()
            raise

    def restore(self) -> None:
        self._back.set()


def test_a_stop_while_the_database_is_unreachable_still_replies_and_frees_the_lock(
    agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The database drops off the network while the turn's events are handled: the session's write never returns, and
    # the handling the stop gives up on and cancels never ends either. Waiting for it is bounded all the same: the
    # turn ends stopped, replies, and leaves the Manager's lock free.
    monkeypatch.setattr(manager_module, "STOP_HANDLING_SEC", 0.3, raising=False)
    monkeypatch.setattr(manager_module, "STOP_FINISH_SEC", 0.3, raising=False)
    monkeypatch.setattr(cancel_module, "ABANDON_SEC", 0.3)

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session()
            manager, bus, queue = _manager(Path(tmp), session)
            envelope = _web("你好")
            database = _Unreachable()
            monkeypatch.setattr(manager_module.db_agent_sessions, "save_session", database)
            handling = asyncio.create_task(manager._handle(envelope))
            try:
                await asyncio.wait_for(session.running.wait(), 5)
                await asyncio.wait_for(database.reached.wait(), 5)
                assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
                await asyncio.wait_for(asyncio.shield(handling), 5)
                assert database.calls == database.cancelled == 2  # the handling's write, then the stopped turn's
                (reply,) = _replies(queue)
                assert reply["outcome"] == "stopped" and "error" not in reply
                assert not manager.busy and manager._lock_owner is None
                assert manager.turn_for(envelope.message_id) is None
            finally:
                database.restore()
                if not handling.done():
                    handling.cancel()
                await asyncio.gather(handling, return_exceptions=True)
                await bus.shutdown()

    asyncio.run(run())


def test_a_stop_while_a_preparation_read_finds_the_database_unreachable_still_replies_and_frees_the_lock(
    agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same outage while the turn reads its saved session, a step the stop abandons with the Manager's lock held:
    # the cancelled read never ends, and the stop waits for it only within a bound.
    monkeypatch.setattr(cancel_module, "ABANDON_SEC", 0.3)

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session(hold=False)
            manager, bus, queue = _manager(Path(tmp), session)
            envelope = _web("你好")
            database = _Unreachable()
            monkeypatch.setattr(manager_module.db_agent_sessions, "get_session", database)
            handling = asyncio.create_task(manager._handle(envelope))
            try:
                await asyncio.wait_for(database.reached.wait(), 5)
                assert manager._lock_owner is not None
                assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
                await asyncio.wait_for(asyncio.shield(handling), 5)
                assert database.calls == database.cancelled == 1
                (reply,) = _replies(queue)
                assert reply["outcome"] == "stopped" and "error" not in reply
                assert not manager.busy and manager._lock_owner is None
                assert manager.turn_for(envelope.message_id) is None and session.turns == []
            finally:
                database.restore()
                if not handling.done():
                    handling.cancel()
                await asyncio.gather(handling, return_exceptions=True)
                await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("closure", ["disable", "shutdown"])
@pytest.mark.parametrize("runtime_cancelled", [False, True], ids=["result", "cancelled"])
def test_closing_the_manager_while_events_drain_ends_the_handling(
    closure: str, runtime_cancelled: bool, tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import chat_settings
    from looper.agents.managers import ManagerFleet
    from looper.agents.supervisor import AgentSupervisor, cancel_and_wait

    monkeypatch.setattr(chat_settings, "_stop_enabled", False)
    monkeypatch.setattr(cancel_module, "ABANDON_SEC", 0.1)
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.1)
    monkeypatch.setattr("looper.agents.claude_cli.STOP_GRACE_SEC", 0.02)
    claude = fake_claude(tmp_path / "claude", """
import json, sys, time
sys.stdin.readline()
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess-1'}), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'ok'}), flush=True)
time.sleep(30)
""")

    async def run() -> None:
        session = ResidentClaude(label="manager")
        manager, bus, queue = _manager(tmp_path, session)  # type: ignore[arg-type]
        manager._config.claude_bin = claude
        manager._definition = dataclasses.replace(manager._definition, id="manager-review",
                                                  system_prompt=manager._system_prompt)
        row = dataclasses.asdict(manager._definition)
        agent_db.get_manager.return_value = row
        supervisor = AgentSupervisor(bus, tmp_path)
        supervisor.register(manager)
        fleet = ManagerFleet(platform=Platform(bus=bus, task_router=None, supervisor=supervisor,
                                              runtime_root=tmp_path), supervisor=supervisor, runtime_root=tmp_path,
                             url="http://127.0.0.1:1", config=manager._config, broadcaster=None)
        fleet._agents[manager.agent_id] = manager
        persisting, release, returned, cleaned = (asyncio.Event() for _ in range(4))
        event_task: asyncio.Task[Any] | None = None
        original_run = session.run_turn

        async def run_turn(*args: Any, **kwargs: Any) -> Any:
            turn = await original_run(*args, **kwargs)
            returned.set()
            if runtime_cancelled:
                kwargs["cancel"].request()
                raise TurnCancelled()
            return turn

        async def persist(self: Any, payload: dict[str, Any]) -> None:
            nonlocal event_task
            event_task = asyncio.current_task()
            persisting.set()
            await release.wait()

        async def cancel_again(task: asyncio.Task[Any]) -> None:
            await cancel_and_wait(task, again_after=0.05)

        monkeypatch.setattr(session, "run_turn", run_turn)
        monkeypatch.setattr(manager_module.ClaudeRunRecorder, "persist", persist)
        monkeypatch.setattr(manager_module, "cancel_and_wait", cancel_again)
        await manager.start(bus)
        envelope = dataclasses.replace(_web("hello"), target=manager.agent_address)
        process = None
        try:
            await bus.publish(envelope)
            await asyncio.wait_for(persisting.wait(), 5)
            await asyncio.wait_for(returned.wait(), 5)
            process, tree = session._process, session._tree
            assert process is not None and tree is not None
            real_end = tree.end

            async def slow_end() -> None:
                # The resident forgets _process before this await; its owner must survive the recancel interval.
                await asyncio.sleep(0.15)
                await real_end()
                cleaned.set()

            monkeypatch.setattr(tree, "end", slow_end)
            await asyncio.sleep(0)  # both runtime exits are now draining the shielded event task
            if closure == "disable":
                row["enabled"] = False
                await asyncio.wait_for(fleet.apply(manager.agent_id), 5)
                assert fleet.get(manager.agent_id) is None
            else:
                await asyncio.wait_for(supervisor.stop_all(), 5)
            assert cleaned.is_set() and process.returncode is not None
            assert not _alive(process.pid) and not session.started
            assert event_task is not None and event_task.done()
            assert manager._task is None and not manager.busy
            writes, sent = agent_db.save_session.await_count, queue.qsize()
            release.set()
            await asyncio.sleep(0)
            assert agent_db.save_session.await_count == writes and queue.qsize() == sent
        finally:
            release.set()
            await manager.stop()
            # Also reap the test-owned process when the regression interrupts the resident after forgetting it.
            if process is not None and process.returncode is None:
                await proc_tree.end(process, label="test cleanup")
            if event_task is not None:
                await asyncio.gather(event_task, return_exceptions=True)
            await bus.shutdown()

    asyncio.run(run())


def test_a_resident_read_failure_still_ends_the_process(
    tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.1)
    claude = fake_claude(tmp_path / "claude", """
import json, sys, time
sys.stdin.readline()
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess-1'}), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'ok'}), flush=True)
time.sleep(30)
""")

    async def run() -> None:
        session = ResidentClaude(label="manager")
        manager, bus, queue = _manager(tmp_path, session)  # type: ignore[arg-type]
        manager._config.claude_bin = claude
        original_run = session.run_turn
        process = None

        async def broken(*args: Any, **kwargs: Any) -> Any:
            nonlocal process
            await original_run(*args, **kwargs)
            process = session._process
            raise OSError("resident read failed")

        monkeypatch.setattr(session, "run_turn", broken)
        try:
            await asyncio.wait_for(manager._handle(_web("hello")), 5)
            assert process is not None and process.returncode is not None and not _alive(process.pid)
            assert not session.started and not manager.busy
            (reply,) = _replies(queue)
            assert reply["outcome"] == "failed" and "resident read failed" in reply["error"]
            (finish,) = agent_db.finish_record.await_args_list
            assert finish.kwargs["status"] == "failed"
        finally:
            await session.stop(kill=True)
            if process is not None and process.returncode is None:
                await proc_tree.end(process, label="test cleanup")
            await bus.shutdown()

    asyncio.run(run())


def test_a_stopped_reply_keeps_the_deltas_shown_before_persistence_blocked(
    tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager_module, "STOP_HANDLING_SEC", 0.1)

    async def run() -> None:
        session = _Session()
        manager, bus, queue = _manager(tmp_path, session)
        persisting, release = asyncio.Event(), asyncio.Event()
        original_persist = manager_module.ClaudeRunRecorder.persist

        async def persist(self: Any, payload: dict[str, Any]) -> None:
            if payload.get("type") == "assistant":
                persisting.set()
                await release.wait()
            await original_persist(self, payload)

        monkeypatch.setattr(manager_module.ClaudeRunRecorder, "persist", persist)
        envelope = _web("hello")
        handling = asyncio.create_task(manager._handle(envelope))
        try:
            await asyncio.wait_for(persisting.wait(), 5)
            shown = "".join(e.payload["text"] for e in queue._queue if e.payload.get("delta"))
            assert shown == "半截回复"
            await manager.stop_turn(envelope.message_id, via="web")
            await asyncio.wait_for(asyncio.shield(handling), 2)
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped" and reply["text"].startswith(shown)
            assert reply["text"].count(shown) == 1
        finally:
            release.set()
            handling.cancel()
            await asyncio.gather(handling, return_exceptions=True)
            await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["running", "recording"])
def test_disabling_a_manager_keeps_the_text_it_already_showed(
    phase: str, tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import chat_settings
    from looper.agents.managers import ManagerFleet
    from looper.agents.ops.service import Platform
    from looper.agents.supervisor import AgentSupervisor

    monkeypatch.setattr(chat_settings, "_stop_enabled", False)

    async def run() -> None:
        session = _Session(hold=phase == "running")
        manager, bus, queue = _manager(tmp_path, session)
        manager._definition = dataclasses.replace(manager._definition, id="manager-review",
                                                  system_prompt=manager._system_prompt)
        row = dataclasses.asdict(manager._definition)
        agent_db.get_manager.return_value = row
        supervisor = AgentSupervisor(bus, tmp_path)
        supervisor.register(manager)
        platform = Platform(bus=bus, task_router=None, supervisor=supervisor, runtime_root=tmp_path)
        fleet = ManagerFleet(platform=platform, supervisor=supervisor, runtime_root=tmp_path,
                             url="http://127.0.0.1:1", config=manager._config, broadcaster=None)
        fleet._agents[manager.agent_id] = manager
        recording, release = asyncio.Event(), asyncio.Event()

        async def finish(*args: Any, **kwargs: Any) -> None:
            if phase == "recording":
                recording.set()
                await release.wait()

        agent_db.finish_record.side_effect = finish
        await manager.start(bus)
        envelope = dataclasses.replace(_web("hello"), target=manager.agent_address)
        try:
            await bus.publish(envelope)
            await until(lambda: any(e.payload.get("delta") for e in queue._queue), timeout=5)
            if phase == "recording":
                await asyncio.wait_for(recording.wait(), 5)
                assert manager.turn_for(envelope.message_id) is None  # already settled, still writing its record
            row["enabled"] = False
            await asyncio.wait_for(fleet.apply(manager.agent_id), 2)
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped" and reply["text"].startswith("半截回复")
            assert fleet.get(manager.agent_id) is None
        finally:
            release.set()
            await manager.stop()
            await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("channel", ["telegram", "lark"])
def test_a_queued_stopped_message_replies_when_its_save_never_finishes(
    channel: str, tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address

    monkeypatch.setattr(manager_module, "STOP_FINISH_SEC", 0.1)
    monkeypatch.setattr(cancel_module, "ABANDON_SEC", 0.1)

    async def run() -> None:
        session = _Session(hold=False)
        manager, bus, _ = _manager(tmp_path, session)
        source = channel_address(channel)
        queue = bus.get_queue(source.agent_id)
        envelope = MessageEnvelope.create(
            source=source, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": "withdraw this", "channel_context": {"chat_id": "99", "user_id": "7"}},
        )
        database = _Unreachable()
        original_save = manager_module.db_chat.save_message

        async def save(**kwargs: Any) -> None:
            if kwargs["message_id"] == envelope.message_id:
                await database(**kwargs)
            else:
                await original_save(**kwargs)

        monkeypatch.setattr(manager_module.db_chat, "save_message", save)
        token = manager._register_turn(envelope)
        assert await manager.stop_turn(token.message_id, via=channel) is StopResult.STOPPING
        handling = asyncio.create_task(manager._handle(envelope))
        try:
            await asyncio.wait_for(database.reached.wait(), 5)
            await asyncio.wait_for(asyncio.shield(handling), 1)
            assert not manager.busy and session.turns == []
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped"
            assert "not written within" in caplog.text
            next_message = dataclasses.replace(envelope, message_id="next", correlation_id="next",
                                                payload={**envelope.payload, "text": "next"})
            await asyncio.wait_for(manager._handle(next_message), 2)
            assert len(session.turns) == 1
            (answer,) = _replies(queue)
            assert answer["outcome"] == "answered"
        finally:
            database.restore()
            handling.cancel()
            await asyncio.gather(handling, return_exceptions=True)
            await bus.shutdown()

    asyncio.run(run())
