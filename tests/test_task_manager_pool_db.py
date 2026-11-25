"""Admission, idle timing and write exclusion on the native resident path."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from looper.agents import projects as project_service
from looper.agents.message import channel_address
from looper.agents.tiers import task_manager as tm
from tests.db_support import run_db, until
from tests.task_manager_support import NativeTaskManager, Sink


def test_prepare_obeys_cap_and_reclaims_lru(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    async def check(db):
        now = [0.0]
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 2, clock=lambda: now[0])
        try:
            for index in range(3):
                task_id, _, _ = await native.create(db, f"task-{index}")
                now[0] = index
                agent = pool.agent(task_id)
                await agent.prepare()
                await agent.resident.run_turn("reply", AsyncMock())
                assert sum(a.resident.started for a in pool.agents.values()) <= 2
            assert not pool.agent("task-0").resident.started
            assert pool.agent("task-1").resident.started and pool.agent("task-2").resident.started
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_provider_changes_restart_even_when_model_is_unchanged(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            agent = pool.agent(task_id)
            await agent.prepare()
            first_pid = agent.resident.pid
            profile = native.settings.profile
            model = replace(profile.resolved_model, provider=replace(profile.resolved_model.provider,
                env=(("SYNTHETIC_PROVIDER", "changed"),)))
            native.settings = replace(native.settings, profile=replace(profile, resolved_model=model))
            await agent.prepare()
            assert agent.resident.pid != first_pid
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_real_turn_busy_queue_and_write_exclusion(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    async def check(db):
        task_id, _, _ = await native.create(db)
        other, _, _ = await native.create(db, "other-task")
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            first, second, third = Sink(), Sink(), Sink()
            assert (await pool.submit(task_id, "wait", message_id="first", sink=first)).state == "started"
            await until(lambda: len(native.records("input")) == 1)
            assert pool.in_turn(task_id)
            bus = type("Bus", (), {"publish": AsyncMock()})()
            with pytest.raises(project_service.RequestError, match="task Manager is working") as error:
                await project_service.start_ready_task(task_id, bus, channel_address("web"), pool=db)
            assert error.value.status.startswith("409")
            bus.publish.assert_not_awaited()
            assert (await pool.submit(task_id, "second", message_id="second", sink=second)).state == "queued"
            assert ("queued", {"reason": "turn", "position": 1}) in second.events
            assert (await pool.submit(other, "third", message_id="third", sink=third)).state == "queued"
            assert ("queued", {"reason": "cap", "position": 2}) in third.events
            assert await pool.stop_turn(task_id, "first")
            await second.done()
            await third.done()
            assert [row["text"] for row in native.records("input")] == ["wait", "second", "third"]
            await until(lambda: not pool.in_turn(other))
            await project_service.start_ready_task(task_id, bus, channel_address("web"), pool=db)
            bus.publish.assert_awaited_once()
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)


def test_idle_reclaim_measures_from_turn_end(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    async def check(db):
        now = [0.0]
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1, clock=lambda: now[0])
        task_id, _, _ = await native.create(db)
        try:
            sink = Sink()
            await pool.submit(task_id, "wait", message_id="first", sink=sink)
            await until(lambda: len(native.records("input")) == 1)
            now[0] = 1900
            await pool.reclaim()
            assert pool.agent(task_id).resident.started and pool.in_turn(task_id)
            assert await pool.stop_turn(task_id, "first")
            await sink.done()
            await until(lambda: not pool.in_turn(task_id))
            assert pool.agent(task_id).last_used == 1900
            now[0] += 1799
            await pool.reclaim()
            assert pool.agent(task_id).resident.started
            now[0] += 1
            await pool.reclaim()
            assert not pool.agent(task_id).resident.started
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)
