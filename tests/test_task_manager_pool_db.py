"""Admission, idle timing and write exclusion on the native resident path."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from looper.agents import projects as project_service
from looper.agents.message import channel_address
from looper.agents.tiers import task_manager as tm
from tests.db_support import run_db, until
from tests.task_manager_support import NativeTaskManager, Sink, submit_and_wait


def test_queued_active_counts_working_managers_not_residents(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    async def check(db):
        task_id, _, _ = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        # Admission can wait for an occupied resident even when no Manager has a turn.
        monkeypatch.setattr(pool, "_slot", AsyncMock(return_value=False))
        try:
            sink = Sink()
            assert (await pool.submit(task_id, "reply", message_id="queued", sink=sink)).state == "queued"
            assert sink.events == [("queued", {"reason": "cap", "position": 1, "active": 0})]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch)


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
            assert ("queued", {"reason": "turn", "position": 1, "active": 1}) in second.events
            assert (await pool.submit(other, "third", message_id="third", sink=third)).state == "queued"
            assert ("queued", {"reason": "cap", "position": 2, "active": 1}) in third.events
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
            native.transcript.with_name("readback-override.json").write_text('{"init_delay": 1.5}')
            await pool.submit(task_id, "wait", message_id="first", sink=sink)
            await until(lambda: len(native.records("input")) == 1)
            # A stop before the turn's init has passed refuses the turn and ends the process (the readback's gate), so
            # this test of the idle clock waits for the init, which the delayed stand-in sends later than the input.
            await until(lambda: pool.agent(task_id).session_id is not None)
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
