"""Draining excludes unstarted account waiters but still waits for actual turns."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import admission, lanes_settings, session_routing
from looper.agents.drain import DrainScope, DrainState
from looper.agents.lanes import Lane
from looper.agents.managers import ManagerFleet
from looper.agents.message import MessageEnvelope, MessageType, worker_address
from looper.agents.tiers.planner import PlannerAgent, TaskPlan
from looper.agents.tiers.task import TaskRouter
from tests.api_manager_support import USAGE, _manager, _message
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
PROVIDER = "prov-api-test"


def _controller(monkeypatch, drain):
    controller = admission.Controller()
    controller.active = True
    controller.draining = lambda: drain.draining
    monkeypatch.setattr(admission, "_controller", controller)
    monkeypatch.setattr(admission, "slot", controller.slot)
    monkeypatch.setattr(admission, "_freeze_alert", AsyncMock())
    monkeypatch.setattr(session_routing, "routes", lambda _: False)
    return controller


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("lanes", [False, True])
def test_manager_account_waiter_does_not_hold_drain_and_runs_only_after_cancel(
    monkeypatch, tmp_path, frozen, lanes,
):
    async def run(pool):
        drain = DrainState()
        controller = _controller(monkeypatch, drain)
        monkeypatch.setattr(lanes_settings, "enabled", lambda: lanes)
        release = asyncio.Event()
        script = ResponseScript(events=text_events("Answered after drain.", usage=USAGE), gate=release)
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            await pool.execute("UPDATE llm_providers SET max_concurrency=1 WHERE id=$1", PROVIDER)
            fleet = object.__new__(ManagerFleet)
            fleet.agents = lambda: [chat.manager]
            drain.add_source(fleet.drain_waiting)
            if frozen:
                reset = datetime.now(timezone.utc) + timedelta(hours=1)
                await controller.transition(PROVIDER, admission.QuotaFailure(reset, None))
            async with AsyncExitStack() as stack:
                if not frozen:
                    await stack.enter_async_context(controller.slot(
                        PROVIDER, Lane.USER_TASK, owner="synthetic-worker"))
                await chat.bus.start()
                await chat.manager.start(chat.bus)
                message = _message("Keep this while draining.")
                await chat.bus.publish(message)
                await until(lambda: len(controller.waiters) == 1, "Manager is waiting for an account slot")
                assert chat.manager.busy
                assert chat.model.requests == []
                assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
                assert drain.begin(DrainScope.TURNS, 1)["phase"] == "drained"
            await controller.wake()
            assert not controller.running and len(controller.waiters) == 1
            assert message.message_id in {e.message_id for e in chat.bus.journal.undelivered()}
            if frozen:
                await controller.transition(PROVIDER, admission.OperatorClear())
            drain.cancel()
            await controller.wake()
            await asyncio.wait_for(script.gate_reached.wait(), 15)
            assert len(chat.model.requests) == 1
            assert await pool.fetchval("SELECT count(*) FROM llm_records") == 1
            assert drain.begin(DrainScope.TURNS, 1)["phase"] == "draining"
            release.set()
            await until(lambda: not chat.manager.busy and not chat.manager._router.pending(), "actual Manager turn ended")
            assert drain.poll()["phase"] == "drained"
            await controller.stop()

    run_db(run, monkeypatch, timeout=40)


def test_planner_exchange_waiter_does_not_hold_drain_but_queued_results_do(monkeypatch, tmp_path):
    async def run(pool):
        drain = DrainState()
        controller = _controller(monkeypatch, drain)
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await pool.execute("UPDATE llm_providers SET max_concurrency=1 WHERE id=$1", PROVIDER)
            planner = PlannerAgent(task_id="drain-planner", runtime_root=tmp_path)
            planner._plan = TaskPlan("drain-plan", "", "Keep working.", str(tmp_path))
            planner._admission_task_lane = Lane.USER_TASK
            planner._activity = AsyncMock()
            # _turn owns the real turn lock; the real exchange waits before reaching its recorder/runtime.
            process = SimpleNamespace(launch=SimpleNamespace(profile=SimpleNamespace(
                provider=SimpleNamespace(id=PROVIDER))))
            planner._run_turn = lambda text: planner._exchange(text, process, 30)
            router = TaskRouter(runtime_root=tmp_path)
            router._planners[planner._task_id] = planner
            router._bus = chat.bus
            drain.add_source(router.drain_waiting)
            async with controller.slot(PROVIDER, Lane.USER_TASK, owner="synthetic-worker"):
                pending = asyncio.create_task(planner._turn("[DECISION] Keep working."))
                try:
                    await until(lambda: len(controller.waiters) == 1, "Planner exchange waits for admission")
                    assert planner._turn_lock.locked()
                    assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
                    assert drain.begin(DrainScope.TURNS, 1)["phase"] == "drained"
                    # Results waiting to be persisted must still block restart even when the current exchange waits.
                    chat.bus.get_queue(planner.agent_id).put_nowait(MessageEnvelope.create(
                        source=worker_address("synthetic-worker"), target=planner.agent_address,
                        message_type=MessageType.WORK_RESULT, payload={"summary": "Delivered."}))
                    assert drain.poll()["waiting"]["planner_turns"] == [planner.agent_id]
                    chat.bus.get_queue(planner.agent_id).get_nowait()
                    assert drain.poll()["phase"] == "drained"
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            assert not controller.waiters and not controller.running
            await controller.stop()

    run_db(run, monkeypatch, timeout=30)
