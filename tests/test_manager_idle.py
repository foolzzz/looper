"""Idle suspension preserves an enabled Manager and uses the normal session resume path."""
from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.managers import ManagerFleet
from looper.agents.managers_api import ManagerDefinition, ManagerTools, TaskPolicy, TaskScope
from looper.agents.message import MessageEnvelope, MessageType, channel_address
from looper.agents.ops.service import Platform
from looper.agents.run_usage import Usage
from looper.agents.supervisor import AgentSupervisor
from looper.agents.tiers import manager as module
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from tests.db_support import until
from tests.test_manager_reply import _fake_claude, _resident


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def definition(**changes):
    return dataclasses.replace(ManagerDefinition(
        id="manager-review", name="Review", description="", system_prompt="Answer briefly.", profile_id=None,
        tools=ManagerTools.TASKS, task_scope=TaskScope.OWN, task_policy=TaskPolicy.FREE,
        enabled=True, builtin=False, revision=1, idle_timeout_seconds=60), **changes)


def message(agent, text="hello"):
    return MessageEnvelope.create(source=channel_address("web"), target=agent.agent_address,
                                  message_type=MessageType.USER_MESSAGE, payload={"text": text})


class Resident:
    started = True
    can_interrupt = False

    def __init__(self):
        self.closed = 0

    async def stop(self):
        self.started = False

    async def try_stop_if_idle(self):
        await self.stop()
        self.closed += 1
        return True


@pytest.fixture
def manager(tmp_path, monkeypatch, agent_db):
    clock = Clock()
    row = dataclasses.asdict(definition())
    monkeypatch.setattr(module.db_managers, "get_manager", AsyncMock(side_effect=lambda *a, **k: row))
    agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path,
                         definition=definition(), clock=clock)
    agent._claude = Resident()
    return agent, clock, row


def test_deadline_closes_only_resident_and_keeps_manager_healthy(manager, tmp_path):
    async def run():
        agent, clock, _ = manager
        bus = MessageBus(runtime_root=tmp_path)
        await agent.start(bus)
        try:
            assert not await agent.try_suspend_idle(59)
            assert await agent.try_suspend_idle(60)
            assert not agent.process_started and agent.is_healthy()
            assert agent._task is not None and not agent._task.done()
        finally:
            await agent.stop()
            await bus.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("default,timeout", [(False, 0), (True, 60)])
def test_zero_and_default_manager_are_exempt(manager, tmp_path, default, timeout):
    async def run():
        agent, _, _ = manager
        agent._definition = dataclasses.replace(agent._definition, id="manager" if default else agent.agent_id,
                                                idle_timeout_seconds=timeout)
        agent._running = True
        assert not await agent.try_suspend_idle(100000)
        assert agent._claude.closed == 0
    asyncio.run(run())


@pytest.mark.parametrize("blocking", ["turn", "queue", "handling", "live"])
def test_busy_admission_resets_deadline_without_waiting(manager, tmp_path, blocking):
    async def run():
        agent, _, _ = manager
        bus = MessageBus(runtime_root=tmp_path)
        agent._bus, agent._running = bus, True
        envelope = message(agent)
        if blocking == "turn":
            await agent._turn_lock.acquire()
        elif blocking == "queue":
            bus.get_queue(agent.agent_id).put_nowait(envelope)
        elif blocking == "handling":
            agent._handling = envelope
        else:
            agent._register_turn(envelope)
        try:
            assert not await asyncio.wait_for(agent.try_suspend_idle(60), 1)
            assert agent._last_activity == 60 and agent._claude.closed == 0
        finally:
            if agent._turn_lock.locked():
                agent._turn_lock.release()
            agent._handling = None
            await agent.stop()
            await bus.shutdown()
    asyncio.run(run())


def test_unknown_runtime_quiescence_keeps_resident(manager):
    async def run():
        agent, _, _ = manager
        agent._running = True
        agent._claude = SimpleNamespace(started=True)
        assert not await agent.try_suspend_idle(60)
        assert agent.process_started
    asyncio.run(run())


def test_close_failure_keeps_session_and_retries_without_content_in_log(manager, caplog):
    async def run():
        agent, _, _ = manager
        agent._running = True
        agent._session_id, agent._session_totals = "saved", Usage(cost_usd=1.25)
        close = AsyncMock(side_effect=[OSError("private model output"), True])
        agent._claude.try_stop_if_idle = close
        assert not await agent.try_suspend_idle(60)
        assert agent._session_id == "saved" and agent._session_totals.cost_usd == 1.25
        assert await agent.try_suspend_idle(65)
        assert "private model output" not in caplog.text
        assert agent.agent_id in caplog.text
    asyncio.run(run())


def test_message_during_close_waits_before_preparing_resident(manager, tmp_path, monkeypatch):
    async def run():
        agent, clock, _ = manager
        bus = MessageBus(runtime_root=tmp_path)
        entered, finish, answered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await finish.wait()
            agent._claude.started = False
            return True
        async def answer(*args):
            assert not agent._claude.started
            answered.set()
        monkeypatch.setattr(agent._claude, "try_stop_if_idle", close)
        monkeypatch.setattr(agent, "_answer_user_message", answer)
        await agent.start(bus)
        closing = asyncio.create_task(agent.try_suspend_idle(60))
        try:
            await entered.wait()
            clock.now = 61
            await bus.publish(message(agent))
            await until(lambda: agent._handling is not None)
            assert not answered.is_set()
            finish.set()
            assert await closing
            await asyncio.wait_for(answered.wait(), 2)
        finally:
            finish.set()
            await closing
            await agent.stop()
            await bus.shutdown()
    asyncio.run(run())


def test_next_message_resumes_id_and_incremental_totals(manager, tmp_path, agent_db):
    async def run():
        agent, clock, _ = manager
        agent._config.claude_bin = _fake_claude(tmp_path, _resident())
        agent._claude = module.CLAUDE_CODE.resident_session(agent.agent_id)
        bus = MessageBus(runtime_root=tmp_path)
        await agent.start(bus)
        try:
            await agent._handle(message(agent, "remember amber"))
            assert agent._session_id == "sess-1"
            assert agent._session_totals.cost_usd == 0.25
            clock.now = 60
            assert await agent.try_suspend_idle(clock())
            saved = agent_db.store.sessions[agent.agent_id]
            assert saved["session_id"] == "sess-1"
            await agent._handle(message(agent, "what color"))
            assert "--resume" in agent._claude.command
            assert agent._session_id == "sess-1" and agent._session_totals.cost_usd == 0.5
            costs = [c.kwargs["cost_usd"] for c in agent_db.finish_record.await_args_list]
            assert costs == [0.25, 0.25]
        finally:
            await agent.stop()
            await bus.shutdown()
    asyncio.run(run())


def test_actual_user_turn_crossing_expiry_finishes_before_new_deadline(manager, tmp_path, monkeypatch):
    async def run():
        agent, clock, _ = manager
        agent._config.claude_bin = _fake_claude(tmp_path, _resident())
        agent._claude = module.CLAUDE_CODE.resident_session(agent.agent_id)
        entered, finish = asyncio.Event(), asyncio.Event()
        original = agent._claude.run_turn
        async def held(*args, **kwargs):
            entered.set()
            await finish.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(agent._claude, "run_turn", held)
        bus = MessageBus(runtime_root=tmp_path)
        await agent.start(bus)
        turn = asyncio.create_task(agent._handle(message(agent)))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            clock.now = 60
            assert agent.busy and not await agent.try_suspend_idle(clock())
            assert agent.process_started
            finish.set()
            await asyncio.wait_for(turn, 5)
            assert agent._session_totals.cost_usd == 0.25
            assert not await agent.try_suspend_idle(119)
            assert await agent.try_suspend_idle(120)
        finally:
            finish.set()
            await asyncio.gather(turn, return_exceptions=True)
            await agent.stop()
            await bus.shutdown()
    asyncio.run(run())


def test_fleet_owns_one_sweep_and_shutdown_awaits_it(tmp_path, monkeypatch, agent_db):
    async def run():
        bus = MessageBus(runtime_root=tmp_path)
        supervisor = AgentSupervisor(bus, tmp_path)
        platform = Platform(bus=bus, task_router=SimpleNamespace(), supervisor=supervisor, runtime_root=tmp_path)
        fleet = ManagerFleet(platform=platform, supervisor=supervisor, runtime_root=tmp_path,
                             url="http://127.0.0.1:1", config=ManagerConfig(work_dir=tmp_path), broadcaster=None)
        try:
            await fleet.load()
            sweep = fleet._idle_task
            assert sweep is not None and not sweep.done()
            await fleet.stop()
            assert sweep.done() and fleet._idle_task is None
        finally:
            await fleet.stop()
            await supervisor.stop_all()
            await bus.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("retire", ["disable", "delete"])
def test_sweep_keeps_sleeping_owner_routable_and_retires_without_a_timer(tmp_path, monkeypatch, agent_db, retire):
    async def run():
        from looper.agents import managers as fleet_module
        clock = Clock()
        rows = [dataclasses.asdict(definition())]
        monkeypatch.setattr(module.db_managers, "list_managers", AsyncMock(return_value=rows))
        monkeypatch.setattr(module.db_managers, "get_manager", AsyncMock(side_effect=lambda mid, **k:
                            rows[0] if rows and mid == rows[0]["id"] else None))
        monkeypatch.setattr(fleet_module, "IDLE_SWEEP_SECONDS", 0.01)
        bus = MessageBus(runtime_root=tmp_path)
        supervisor = AgentSupervisor(bus, tmp_path)
        platform = Platform(bus=bus, task_router=SimpleNamespace(), supervisor=supervisor, runtime_root=tmp_path)
        fleet = ManagerFleet(platform=platform, supervisor=supervisor, runtime_root=tmp_path,
                             url="http://127.0.0.1:1", config=ManagerConfig(work_dir=tmp_path),
                             broadcaster=None, clock=clock)
        try:
            await fleet.load()
            agent = fleet.get("manager-review")
            agent._claude = resident = Resident()
            await supervisor.start_all()
            clock.now = 60
            await until(lambda: resident.closed == 1)
            assert fleet.is_running(agent.agent_id)
            assert fleet.state(agent.agent_id) == {
                "running": False, "busy": False, "healthy": True, "sync": "ok"}
            report_seen = asyncio.Event()
            async def handle_report(envelope):
                assert envelope.message_type == MessageType.TASK_ACCEPTED
                report_seen.set()
            monkeypatch.setattr(agent, "_handle", handle_report)
            await bus.publish(MessageEnvelope.create(source=channel_address("web"), target=agent.agent_address,
                              message_type=MessageType.TASK_ACCEPTED, payload={"task_id": "t"}))
            await asyncio.wait_for(report_seen.wait(), 1)
            assert not fleet.default._claude.started
            if retire == "delete":
                rows.clear()
            else:
                rows[0]["enabled"] = False
            await fleet.apply(agent.agent_id)
            assert not fleet.is_running(agent.agent_id) and agent._task is None
            assert resident.closed == 1
        finally:
            await fleet.stop()
            await supervisor.stop_all()
            await bus.shutdown()
    asyncio.run(run())


def test_fleet_timeout_change_applies_without_restarting_resident(tmp_path, monkeypatch, agent_db):
    async def run():
        row = dataclasses.asdict(definition(idle_timeout_seconds=1800))
        monkeypatch.setattr(module.db_managers, "list_managers", AsyncMock(return_value=[row]))
        monkeypatch.setattr(module.db_managers, "get_manager", AsyncMock(side_effect=lambda *a, **k: row))
        bus = MessageBus(runtime_root=tmp_path)
        supervisor = AgentSupervisor(bus, tmp_path)
        platform = Platform(bus=bus, task_router=SimpleNamespace(), supervisor=supervisor, runtime_root=tmp_path)
        fleet = ManagerFleet(platform=platform, supervisor=supervisor, runtime_root=tmp_path,
                             url="http://127.0.0.1:1", config=ManagerConfig(work_dir=tmp_path),
                             broadcaster=None, clock=Clock())
        try:
            await fleet.load()
            await supervisor.start_all()
            agent = fleet.get("manager-review")
            agent._claude = Resident()
            assert not await agent.try_suspend_idle(60)
            row["idle_timeout_seconds"] = 60
            await fleet.apply(agent.agent_id)
            assert await agent.try_suspend_idle(60)
        finally:
            await fleet.stop()
            await supervisor.stop_all()
            await bus.shutdown()
    asyncio.run(run())


def test_fleet_shutdown_joins_close_before_finishing(tmp_path, monkeypatch, agent_db):
    async def run():
        from looper.agents import managers as fleet_module
        clock = Clock()
        row = dataclasses.asdict(definition())
        monkeypatch.setattr(module.db_managers, "list_managers", AsyncMock(return_value=[row]))
        monkeypatch.setattr(module.db_managers, "get_manager", AsyncMock(return_value=row))
        monkeypatch.setattr(fleet_module, "IDLE_SWEEP_SECONDS", 0.01)
        bus = MessageBus(runtime_root=tmp_path)
        supervisor = AgentSupervisor(bus, tmp_path)
        platform = Platform(bus=bus, task_router=SimpleNamespace(), supervisor=supervisor, runtime_root=tmp_path)
        fleet = ManagerFleet(platform=platform, supervisor=supervisor, runtime_root=tmp_path,
                             url="http://127.0.0.1:1", config=ManagerConfig(work_dir=tmp_path),
                             broadcaster=None, clock=clock)
        entered, finish = asyncio.Event(), asyncio.Event()
        stopping = None
        async def close():
            entered.set()
            await finish.wait()
            return True
        try:
            await fleet.load()
            await supervisor.start_all()
            agent = fleet.get("manager-review")
            agent._claude = Resident()
            monkeypatch.setattr(agent._claude, "try_stop_if_idle", close)
            clock.now = 60
            await asyncio.wait_for(entered.wait(), 1)
            sweep = fleet._idle_task
            stopping = asyncio.create_task(fleet.stop())
            await until(lambda: fleet._idle_task is None)
            assert not stopping.done() and agent._lifecycle_gate.locked()
            finish.set()
            await asyncio.wait_for(stopping, 1)
            assert sweep.done() and not agent._lifecycle_gate.locked()
        finally:
            finish.set()
            if stopping is not None:
                await stopping
            await fleet.stop()
            await supervisor.stop_all()
            await bus.shutdown()
    asyncio.run(run())
