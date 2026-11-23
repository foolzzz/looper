"""Actual run entry points must wait for account admission, including restart adoption."""

import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from looper import child_env
from looper.agents import admission
from looper.agents.lanes import Lane
from tests.db_support import DSN, run_db, until

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
PROVIDER = "prov-claude-subscription"


def _controller(monkeypatch, *, frozen=True):
    controller = admission.Controller()
    controller.active = True
    if frozen:
        controller.states[PROVIDER] = admission.Account(
            state=admission.AdmissionState.FROZEN,
            until=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    monkeypatch.setattr(admission, "_controller", controller)
    for name in ("slot", "register", "release", "transition"):
        monkeypatch.setattr(admission, name, getattr(controller, name))
    return controller


def test_processor_waits_before_recording_or_starting(monkeypatch, tmp_path):
    from looper.agents.serve import seed_role_configs
    from tests.test_processor_db import _run

    monkeypatch.setenv("PROCESSOR_SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {"PROCESSOR_SEEN"})

    async def test(pool):
        controller = _controller(monkeypatch)
        task = asyncio.create_task(_run(tmp_path))
        try:
            await until(lambda: controller.waiters or task.done(), "processor account wait")
            assert len(controller.waiters) == 1
            assert controller.waiters[0].lane is Lane.BACKGROUND
            assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
            assert not (tmp_path / "seen.jsonl").exists()
            await controller.transition(PROVIDER, admission.OperatorClear())
            result = await asyncio.wait_for(task, 15)
            assert result.reply == "The canary deploy failed twice."
            assert not controller.running and not controller.waiters
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    run_db(test, monkeypatch, setup=seed_role_configs, timeout=25)


def test_spawn_waits_before_child_record_and_preserves_parent_lane(monkeypatch, tmp_path):
    from tests.test_agent_spawns_db import _spawn

    monkeypatch.setenv("SPAWN_SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {"SPAWN_SEEN", "SPAWN_MODE"})

    async def test(pool):
        controller = _controller(monkeypatch)
        task = asyncio.create_task(_spawn(pool, tmp_path, monkeypatch))
        try:
            await until(lambda: controller.waiters or task.done(), "child account wait")
            assert len(controller.waiters) == 1
            assert controller.waiters[0].lane is Lane.USER_TASK
            assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE parent_record_id IS NOT NULL") == 0
            assert not (tmp_path / "seen.jsonl").exists()
            await controller.transition(PROVIDER, admission.OperatorClear())
            outcome, child, parent = await asyncio.wait_for(task, 15)
            assert outcome.child.output == "Use a process pool."
            assert not controller.running and not controller.waiters
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    run_db(test, monkeypatch, timeout=25)


def test_consult_waits_before_launching_its_fork(monkeypatch, tmp_path):
    from looper.agents.room import RoomService
    from looper.agents.run_usage import Usage
    from looper.agents.runtimes.pricing import usage_event
    from tests.run_handle_support import ScriptedRun
    from tests.test_room_consult_db import _host_consults, _question, _ran, _service, _settled
    from tests.test_room_db import _Pool, _room_task

    async def test(pool):
        controller = _controller(monkeypatch)
        await _room_task(pool)
        await _ran(pool, tmp_path / "work")
        question = await _question(pool, "Which field?")
        handle = ScriptedRun([
            {"type": "system", "subtype": "init", "session_id": "consult-fork"},
            usage_event("claude-haiku-4-5", Usage(input_tokens=0, output_tokens=2000,
                                                 cache_read_tokens=0, cache_creation_tokens=0)),
            {"type": "result", "subtype": "success", "total_cost_usd": 0.01,
             "result": json.dumps({"answers": [{"reply_to": question["id"], "body": "qty"}]})},
        ])
        _host_consults(monkeypatch, handle)
        room = RoomService(batch_seconds=0)
        service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
        session = tmp_path / "source-session"
        session.mkdir()
        monkeypatch.setattr(service, "_session_dir", lambda *args: session)
        try:
            member = (await room._context("task-r")).member("backend_rd@s1")
            assert await service.ask("task-r", member, question) is None
            await until(lambda: controller.waiters or handle.launch_calls or not service._running,
                        "consult account wait")
            assert len(controller.waiters) == 1
            assert controller.waiters[0].lane is Lane.USER_TASK
            assert handle.launch_calls == 0
            assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE agent_id LIKE 'consult-%'") == 0
            await controller.transition(PROVIDER, admission.OperatorClear())
            row = await _settled(pool)
            assert row["state"] == "answered", row
            assert handle.launch_calls == 1
            await until(lambda: not service._running, "consult cleanup")
            assert not controller.running and not controller.waiters
        finally:
            await service.stop()

    run_db(test, monkeypatch, timeout=25)


def test_adopted_run_registers_recovery_capacity_and_releases_it(monkeypatch, tmp_path):
    from looper import components
    from tests.test_kept_adopt_db import _adopt, _detach, _finished, _pools

    components.loaded_manifest()

    async def test(pool):
        async with _pools(pool, tmp_path, monkeypatch) as case:
            await _detach(case)
            controller = _controller(monkeypatch, frozen=False)
            await pool.execute("UPDATE llm_providers SET max_concurrency=1 WHERE id=$1", PROVIDER)
            await _adopt(case)
            grant = controller.running.get(case.run_id)
            assert grant is not None, "adopted process must consume account capacity"
            assert (grant.provider_id, grant.lane) == (PROVIDER, Lane.RECOVERY)
            waiter = asyncio.create_task(controller.acquire(PROVIDER, Lane.USER_TASK, "next-worker"))
            try:
                await until(lambda: controller.waiters or waiter.done(), "adopted capacity wait")
                assert not waiter.done()
                (tmp_path / "release").touch()
                await _finished(case)
                await asyncio.wait_for(waiter, 5)
                assert case.run_id not in controller.running
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                await controller.release("next-worker")

    run_db(test, monkeypatch, timeout=35)
