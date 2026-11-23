"""Outbound review activation at the Manager's send and runtime retirement boundaries."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import completion_events, outbound_hook
from looper.agents.claude_cli import ResidentClaude
from looper.agents.managers import ManagerFleet
from looper.agents.runtimes.cancel import TurnCancel
from tests.db_support import until
from tests.fake_claude import fake_claude
from tests.test_manager_new_session import _web
from tests.test_manager_stop import _manager, _Session


@pytest.mark.parametrize("enable_before_send", [False, True])
def test_handoff_checks_the_switch_in_the_task_that_sends(
    tmp_path: Path, agent_db: Any, monkeypatch: pytest.MonkeyPatch, enable_before_send: bool,
) -> None:
    async def run() -> None:
        resident = _Session(hold=False)
        manager, bus, _ = _manager(tmp_path, resident)
        monkeypatch.setattr(outbound_hook, "_known", False)
        await manager._handle(_web("hello"))
        sent: list[bool] = []
        original_run = resident.run_turn

        async def send(*args: Any, **kwargs: Any) -> Any:
            sent.append(outbound_hook._known)
            return await original_run(*args, **kwargs)

        def enabled() -> bool:
            value = outbound_hook._known
            if enable_before_send:
                # Runs at the next task boundary, before wait_for's child task starts on Python 3.11.
                asyncio.get_running_loop().call_soon(setattr, outbound_hook, "_known", True)
            return value

        monkeypatch.setattr(resident, "run_turn", send)
        monkeypatch.setattr(outbound_hook, "known_enabled", enabled)
        try:
            await manager._handoff(SimpleNamespace(describe=lambda: "profile changed"), manager._process, TurnCancel())
            await asyncio.sleep(0)
            assert outbound_hook._known is enable_before_send
            assert True not in sent, "a hookless handoff was sent after activation"
            if not enable_before_send:
                assert sent == [False]  # positive control: handoffs still work while off
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("current_hooked", [False, True])
def test_activation_waits_for_every_retired_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current_hooked: bool,
) -> None:
    async def run() -> None:
        manager, bus, _ = _manager(tmp_path, _Session(hold=False))
        monkeypatch.setattr(completion_events, "END_SECONDS", 0.01)
        finish_turns = asyncio.Event()
        runs, cleanups, releases = [], [], []

        async def stubborn() -> str:
            while not finish_turns.is_set():
                try:
                    await finish_turns.wait()
                except asyncio.CancelledError:
                    pass
            return "done"

        manager._claude_runtime = SimpleNamespace(resident_session=lambda _: _Session(hold=False))
        try:
            for _ in range(2):
                entered, release = asyncio.Event(), asyncio.Event()
                # Capture each runtime's cleanup events independently.
                resident = _Session(hold=False)
                async def stop(*, kill: bool = False, _entered=entered, _release=release, _resident=resident) -> None:
                    _resident.started = False
                    _entered.set()
                    await _release.wait()
                resident.stop = stop
                manager._claude = resident
                turn = asyncio.create_task(stubborn())
                runs.append(turn)
                await asyncio.sleep(0)
                token = SimpleNamespace(request_stop=lambda _: False, abandoned=False)
                await manager._end_late_completion(turn, uuid.uuid4(), {"token": token})
                assert token.abandoned
                await until(entered.is_set)
                cleanups.extend(task for task in manager._late_turns if task not in runs and task not in cleanups)
                releases.append(release)
            current = manager._claude
            current.started = current_hooked
            manager._process = SimpleNamespace(launch=SimpleNamespace(tool_hook=object() if current_hooked else None))
            enabling = asyncio.create_task(manager.require_tool_hook())
            try:
                await asyncio.wait({enabling}, timeout=0.03)
                assert not enabling.done(), "activation returned while retired descendants were still alive"
                releases[0].set()
                await asyncio.wait({enabling}, timeout=0.03)
                assert not enabling.done(), "activation waited for only one retired runtime"
                releases[1].set()
                await asyncio.wait_for(enabling, 1)
                assert current.started is current_hooked
            finally:
                for release in releases:
                    release.set()
                await asyncio.gather(enabling, return_exceptions=True)
        finally:
            finish_turns.set()
            await asyncio.gather(*runs, *cleanups, return_exceptions=True)
            await manager.stop()
            await bus.shutdown()

    asyncio.run(run())


def test_fleet_stops_every_manager_and_reports_cleanup_failures() -> None:
    async def run() -> None:
        stopped: list[str] = []

        async def failed() -> None:
            stopped.append("failed")
            raise OSError("tree cleanup failed")

        async def healthy() -> None:
            stopped.append("healthy")

        fleet = ManagerFleet.__new__(ManagerFleet)
        fleet.agents = lambda: [SimpleNamespace(agent_id="failed", require_tool_hook=failed),
                                SimpleNamespace(agent_id="healthy", require_tool_hook=healthy)]
        with pytest.raises(ExceptionGroup) as error:
            await fleet.require_tool_hooks()
        assert stopped == ["failed", "healthy"]
        assert any(isinstance(exc, OSError) for exc in error.value.exceptions)

    asyncio.run(run())


def test_replacing_a_resident_joins_its_old_cleanup_without_stopping_its_new_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        resident = ResidentClaude(label="manager")
        manager, bus, _ = _manager(tmp_path, resident)
        binary = fake_claude(tmp_path / "resident", "import sys\nsys.stdin.read()\n")
        release = asyncio.Event()
        old_cleanup = resident._stop_process

        async def delayed(*args: Any, **kwargs: Any) -> None:
            await release.wait()
            await old_cleanup(*args, **kwargs)

        monkeypatch.setattr(resident, "_stop_process", delayed)
        enabling = None
        try:
            await resident.start([binary], cwd=str(tmp_path), signature="old")
            old_pid = resident.pid
            await manager._stop_replaced_session()
            await resident.start([binary], cwd=str(tmp_path), signature="new")
            new_pid = resident.pid
            assert new_pid is not None and new_pid != old_pid
            manager._process = SimpleNamespace(launch=SimpleNamespace(tool_hook=object()))
            enabling = asyncio.create_task(manager.require_tool_hook())
            await asyncio.wait({enabling}, timeout=0.03)
            assert not enabling.done()
            release.set()
            await asyncio.wait_for(enabling, 5)
            assert resident.started and resident.pid == new_pid
        finally:
            release.set()
            if enabling is not None:
                await asyncio.gather(enabling, return_exceptions=True)
            await manager.stop()
            await bus.shutdown()

    asyncio.run(run())
