"""A stop at each await of the Manager's turn preparation and start (``ManagerAgent._prepare_turn`` and
``_start_turn``): every await there is a ``token.step`` or ``token.keep``, so the turn ends stopped at once."""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import signal
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents import proc_tree
from looper.agents.claude_cli import ResidentClaude
from looper.agents.tiers import manager as manager_module
from looper.agents.tiers.manager import PREPARATION_STEPS, ManagerAgent, StopResult
from looper.agents.tiers.turn_token import TurnPhase
from tests.stop_faults import hold_at
from tests.stop_matrix_support import _alive
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


@pytest.mark.parametrize("late_child", [False, True])
def test_stop_after_a_resident_result_ends_detached_processes_while_event_persistence_is_blocked(
    tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch, late_child: bool,
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
        if not late_child:
            (tmp_path / "spawn").touch()
        envelope = _web("你好")
        handling = asyncio.create_task(manager._handle(envelope))
        child = None
        original_end, original_strays = proc_tree._end, None
        try:
            await asyncio.wait_for(persisting.wait(), 8)
            await asyncio.wait_for(returned.wait(), 8)  # the real runtime dropped its cancellation callback
            if late_child:
                # The main end's snapshot precedes a tool's fork; the stray snapshot follows it, so it reaches
                # the new child through the resident. Finish that scan after the old cleanup budget as well.
                main_ended = asyncio.Event()
                assert session._tree is not None
                original_strays = session._tree._strays

                async def end(process: Any, **kwargs: Any) -> None:
                    await original_end(process, **kwargs)
                    if process is session._process:
                        (tmp_path / "spawn").touch()
                        await _until(lambda: (tmp_path / "child.pid").exists(), timeout=2)
                        main_ended.set()

                async def strays(*args: Any, **kwargs: Any) -> Any:
                    await asyncio.wait_for(main_ended.wait(), 2)
                    found = await original_strays(*args, **kwargs)
                    if not kwargs.get("ended"):
                        await asyncio.sleep(2 * proc_tree.TERM_GRACE_SEC)
                    return found

                monkeypatch.setattr(proc_tree, "_end", end)
                monkeypatch.setattr(session._tree, "_strays", strays)
            else:
                await _until(lambda: (tmp_path / "child.pid").exists(), timeout=2)
                child = int((tmp_path / "child.pid").read_text())
                assert _alive(child)
            assert session.started
            assert await manager.stop_turn(envelope.message_id, via="web") is StopResult.STOPPING
            await _until(lambda: (tmp_path / "child.pid").exists(), timeout=2)
            child = int((tmp_path / "child.pid").read_text())
            await _until(lambda: not _alive(child), timeout=2)
            assert not release.is_set()  # cleanup does not depend on the database becoming available
            await asyncio.wait_for(asyncio.shield(handling), 5)
            (reply,) = _replies(queue)
            assert reply["outcome"] == "stopped" and "error" not in reply
            assert session.started  # the resident and its baseline survive this turn's stop
        finally:
            release.set()
            if original_strays is not None and session._tree is not None:
                monkeypatch.setattr(session._tree, "_strays", original_strays)
                monkeypatch.setattr(proc_tree, "_end", original_end)
            if not handling.done():
                handling.cancel()
            await asyncio.gather(handling, return_exceptions=True)
            await session.stop(kill=True)
            if child is not None and _alive(child):
                os.kill(child, signal.SIGKILL)
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


def test_a_stop_while_the_events_are_handled_ends_the_processes_at_once_and_waits_for_the_handling_within_a_bound(
    agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The database holds the handling of the turn's events (persisting the session): the stop ends the turn's
    # processes at once, without waiting for it, and the turn ends stopped once the handling has had its time.
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
                await _until(lambda: session.ended_turns == 1, timeout=2)
                assert not release.is_set()
                await asyncio.wait_for(asyncio.shield(handling), 5)
                (reply,) = _replies(queue)
                assert reply["outcome"] == "stopped"
                assert session.ended_turns == 1
            finally:
                release.set()
                await asyncio.gather(handling, return_exceptions=True)
                await bus.shutdown()

    asyncio.run(run())
