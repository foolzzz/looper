"""Regression coverage for budget I/O and concurrent task Manager lifecycle changes."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from looper.agents import agent_settings, task_budget, task_manager_settings
from looper.agents.tiers import task_manager as tm
from looper.db import agent_sessions, tasks
from tests.db_support import run_db, until, v1_api
from tests.task_manager_support import NativeTaskManager, Sink, submit_and_wait


def test_cost_read_failure_allows_turn_and_records_unverifiable(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    async def check(db):
        task_id, _, _ = await native.create(db)
        monkeypatch.setattr(task_budget, "gate", AsyncMock(side_effect=OSError("synthetic unavailable cost")))
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            sink = await submit_and_wait(pool, task_id)
            assert sink.finals == [("onetwo", "success")]
            assert [r["text"] for r in native.records("input")] == ["reply"]
            assert (await tasks.get_task(task_id))["status"] == "draft"
            warnings = await db.fetch("SELECT kind,summary FROM activity WHERE task_id=$1", task_id)
            assert any(r["kind"] == "error" and "budget_unverifiable" in r["summary"] for r in warnings)
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["close", "shutdown"])
def test_lifecycle_waits_for_dispatch_lock(monkeypatch, tmp_path, action):
    native = NativeTaskManager(monkeypatch, tmp_path)
    async def check(db):
        first, _, _ = await native.create(db, "first-task")
        second, _, _ = await native.create(db, "second-task")
        third, _, _ = await native.create(db, "third-task")
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        entered, release, changing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        work = []
        try:
            await submit_and_wait(pool, first)
            idle = pool.agent(first).resident
            stop = idle.try_stop_if_idle
            async def paused_stop():
                entered.set()
                await release.wait()
                return await stop()
            monkeypatch.setattr(idle, "try_stop_if_idle", paused_stop)
            sink = Sink()
            work.append(asyncio.create_task(pool.submit(second, "reply", message_id="second", sink=sink)))
            await entered.wait()
            async def change():
                changing.set()
                await (pool.close(second) if action == "close" else pool.shutdown())
            work.append(asyncio.create_task(change()))
            await changing.wait()
            await asyncio.sleep(0)
            # Neither lifecycle action may remove dispatch's request while eviction yields.
            assert [r.task_id for r in pool._pending] == [second]
            release.set()
            results = await asyncio.gather(*work, return_exceptions=True)
            assert not any(isinstance(result, BaseException) for result in results), results
            assert pool._pending == []
            if action == "close":
                await submit_and_wait(pool, third)
            else:
                assert not any(a.resident.started for a in pool.agents.values())
                assert not pool.in_turn(second)
        finally:
            release.set()
            await asyncio.gather(*work, return_exceptions=True)
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_native_session_is_saved_at_init_before_turn_finishes(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            await pool.submit(task_id, "wait", message_id="wait", sink=Sink())
            await until(lambda: pool.agent(task_id).session_id is not None)
            saved = await agent_sessions.get_session(f"task-manager-{task_id}")
            assert saved and saved["session_id"] == pool.agent(task_id).session_id
            assert saved["profile_id"] == native.settings.profile.id
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_profile_operation_is_operator_only_and_resumes_idle_session(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    original_load = agent_settings.load_agent_settings
    monkeypatch.setattr(task_manager_settings, "load_task_manager_settings", lambda task: original_load(
        agent_settings.AgentType.TASK_MANAGER, task_id=task["id"], project_id=task["project_id"], manager_session=False))
    monkeypatch.setattr(agent_settings, "_login_problem", AsyncMock(return_value=None))
    async def check(db):
        await task_manager_settings.seed_defaults(db)
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            async with v1_api(tmp_path, db) as call:
                status, answer = await call("tasks.task_manager_profile_set", {"task_id": task_id,
                    "profile_id": "agent-claude-code-opus"}, client=True)
                assert status == 403, answer
                status, answer = await call("tasks.task_manager_profile_set", {"task_id": "missing-task",
                    "profile_id": None})
                assert status == 404, answer
                status, answer = await call("tasks.task_manager_profile_set", {"task_id": task_id,
                    "profile_id": "missing-profile"})
                assert status == 400, answer
                await submit_and_wait(pool, task_id)
                saved = await agent_sessions.get_session(f"task-manager-{task_id}")
                status, answer = await asyncio.wait_for(call("tasks.task_manager_profile_set", {
                    "task_id": task_id, "profile_id": "agent-claude-code-opus"}), 3)
                assert status == 200, answer
                assert not pool.agent(task_id).resident.started
                await submit_and_wait(pool, task_id, message_id="switched")
                command = pool.agent(task_id).resident.command
                assert command[command.index("--model") + 1] == "opus"
                assert command[command.index("--resume") + 1] == saved["session_id"]
                await pool.submit(task_id, "wait", message_id="busy", sink=Sink())
                status, answer = await call("tasks.task_manager_profile_set", {"task_id": task_id, "profile_id": None})
                assert status == 409, answer
                assert (await task_manager_settings.load_task_manager_settings(await tasks.get_task(task_id))).model == "opus"
                await pool.stop_turn(task_id, "busy")
                await until(lambda: not pool.in_turn(task_id))
                status, answer = await call("tasks.task_manager_profile_set", {"task_id": task_id, "profile_id": None})
                assert status == 200, answer
                assert (await task_manager_settings.load_task_manager_settings(await tasks.get_task(task_id))).model == "claude-sonnet-5-5"
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)
