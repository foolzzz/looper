"""A stop at each await of the Manager's turn preparation and start (``ManagerAgent._prepare_turn`` and
``_start_turn``): every await there is a ``token.step`` or ``token.keep``, so the turn ends stopped at once."""

from __future__ import annotations

import ast
import asyncio
import inspect
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import pytest

from looper.agents.tiers import manager as manager_module
from looper.agents.tiers.manager import PREPARATION_STEPS, ManagerAgent, StopResult
from looper.agents.tiers.turn_token import TurnPhase
from tests.stop_faults import hold_at
from tests.test_manager_new_session import _until, _web
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


def test_a_stop_while_the_events_are_handled_ends_the_turns_processes(agent_db: Any) -> None:
    # run_turn has returned an answer (and dropped its cancel) when the stop comes: nothing else ends what the turn
    # started, so the Manager must, before it replies stopped.
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session(hold=False)
            manager, bus, queue = _manager(Path(tmp), session)
            envelope = _web("你好")

            async def stop_while_persisting() -> None:
                if session.ended_turns == 0 and manager.turn_for(envelope.message_id) is not None \
                        and not manager.turn_for(envelope.message_id).cancel.requested:  # type: ignore[union-attr]
                    assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING

            manager._save_session_state = stop_while_persisting  # type: ignore[method-assign]
            await asyncio.wait_for(manager._handle(envelope), 5)
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped"
            assert session.ended_turns == 1
            await bus.shutdown()

    asyncio.run(run())
