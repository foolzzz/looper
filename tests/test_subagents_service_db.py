"""Explorer admission and lifecycle on a disposable PostgreSQL database."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents import subagents
from looper.agents.agent_settings import AgentSettings
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.spawn import ChildResult, SpawnParent
from looper.db import agent_spawns, llm_records
from tests.db_support import run_db


async def setup(pool, root, monkeypatch, **project_values):
    from looper.db.projects import create_project
    project = await create_project(name="Exploration", pool=pool)
    await pool.execute("UPDATE projects SET subagents = 'on' WHERE id = $1", project["id"])
    for key, value in project_values.items():
        await pool.execute(f"UPDATE projects SET {key} = $2 WHERE id = $1", project["id"], value)
    await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) VALUES ('t', $1, 'Inspect source', 'running')", project["id"])
    record = await llm_records.start_record(agent_id="worker-parent", task_id="t", pool=pool)
    settings = AgentSettings(profile=builtin_resolved_profile())
    monkeypatch.setattr(subagents, "load_agent_settings", AsyncMock(return_value=settings))
    parent = subagents.Spawner(SpawnParent("t", record_id=record), "worker", root)
    service = subagents.SpawnService(root / "runtime", pool=pool)
    return service, parent


def brief():
    return {"goal": "Find the parser", "deliverable": "Paths and evidence", "scope": ["."]}


def test_same_key_and_different_keys_reserve_without_overselling(tmp_path, monkeypatch):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch, subagent_task_budget_usd=.5)
        rows = await asyncio.gather(*(service.admit(parent, brief(), key="same") for _ in range(2)))
        assert rows[0]["id"] == rows[1]["id"]
        with pytest.raises(subagents.Refused) as exc:
            await service.admit(parent, brief(), key="different")
        assert exc.value.reason.value == "budget"
        await service.cancel(rows[0]["id"])
        next_row = await service.admit(parent, brief(), key="after-cancel")
        assert next_row["budget_usd"] == .5
        assert await pool.fetchval("SELECT count(*) FROM agent_spawns") == 2
    run_db(check, monkeypatch)


@pytest.mark.parametrize("role,expected", [("reviewer", "role"), ("explorer", "nesting")])
def test_role_and_nesting_rejected_without_reservation(tmp_path, monkeypatch, role, expected):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch)
        with pytest.raises(subagents.Refused) as exc:
            await service.admit(replace(parent, role=role), brief(), key="bad")
        assert exc.value.reason.value == expected
        assert await pool.fetchval("SELECT count(*) FROM agent_spawns") == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize("bad", [{"goal": "g", "deliverable": "d", "scope": ["../outside"]},
                                 {"goal": "g", "deliverable": "d", "scope": ["/absolute"]},
                                 {"goal": "", "deliverable": "d", "scope": ["."]}])
def test_invalid_brief_does_not_reserve(tmp_path, monkeypatch, bad):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch)
        with pytest.raises(subagents.Refused):
            await service.admit(parent, bad, key="bad")
        assert await pool.fetchval("SELECT count(*) FROM agent_spawns") == 0
    run_db(check, monkeypatch)


def test_unknown_parent_spend_and_known_parent_remaining(tmp_path, monkeypatch):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch)
        parent = replace(parent, budget_usd=.2, spent_usd=None)
        row = await service.admit(parent, brief(), key="unknown")
        assert row["budget_usd"] == .5 and row["parent_spend_known"] is False
        await service.cancel(row["id"])
        row = await service.admit(replace(parent, spent_usd=.05), brief(), key="known")
        assert row["budget_usd"] == pytest.approx(.15)
        assert row["parent_spend_known"] is True
    run_db(check, monkeypatch)


def test_start_failure_releases_reservation_and_breaker(tmp_path, monkeypatch):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch, subagent_task_budget_usd=.5)
        monkeypatch.setattr(subagents.spawn, "run_child", AsyncMock(side_effect=OSError("cannot start")))
        for index in range(3):
            row = await service.admit(parent, brief(), key=str(index))
            result = await service.run(row["id"])
            assert result["state"] == "failed" and result["failure_cause"] == "start_failure"
        with pytest.raises(subagents.Refused) as exc:
            await service.admit(parent, brief(), key="breaker")
        assert exc.value.reason.value == "breaker"
    run_db(check, monkeypatch)


def test_cancel_cascade_and_restart_never_spawn(tmp_path, monkeypatch):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch)
        queued = await service.admit(parent, brief(), key="queued")
        asking = await service.admit(parent, brief(), key="asking")
        await agent_spawns.move(asking["id"], agent_spawns.SpawnState.ASKING,
                                expect=agent_spawns.SpawnState.QUEUED,
                                child_process={"pid": 100001, "started": "same-start"}, pool=pool)
        end = AsyncMock()
        monkeypatch.setattr(service, "_end_process", end)
        runner = AsyncMock(side_effect=AssertionError("recovery must never spawn"))
        monkeypatch.setattr(subagents.spawn, "run_child", runner)
        await service.recover()
        assert (await agent_spawns.get(queued["id"], pool=pool))["state"] == "queued"
        assert (await agent_spawns.get(asking["id"], pool=pool))["cancel_reason"] == "server"
        assert end.await_count == 1 and runner.await_count == 0
        await service.cascade(task_id="t", reason=subagents.CancelReason.TASK_ENDED)
        assert (await agent_spawns.get(queued["id"], pool=pool))["state"] == "cancelled"
    run_db(check, monkeypatch)


def test_finishing_recovery_books_once_and_does_not_cancel(tmp_path, monkeypatch):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch)
        row = await service.admit(parent, brief(), key="done")
        record = await llm_records.start_record(agent_id="explorer-child", task_id="t", run_kind="explorer",
                                               parent_record_id=parent.parent.record_id, pool=pool)
        await llm_records.finish_record(record, status="completed", output_text='not JSON', cost_usd=.03, pool=pool)
        await agent_spawns.move(row["id"], agent_spawns.SpawnState.ASKING,
                                expect=agent_spawns.SpawnState.QUEUED, child_record_id=record, pool=pool)
        await agent_spawns.move(row["id"], agent_spawns.SpawnState.FINISHING,
                                expect=agent_spawns.SpawnState.ASKING, pool=pool)
        await service.cancel(row["id"])
        await service.recover()
        await service.recover()
        result = await agent_spawns.get(row["id"], pool=pool)
        assert result["state"] == "answered" and result["spent_usd"] == .03
        assert result["result"]["verified"] is False
    run_db(check, monkeypatch)


def test_different_concurrent_keys_cannot_oversell_task_budget(tmp_path, monkeypatch):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch, subagent_task_budget_usd=.5)
        outcomes = await asyncio.gather(
            service.admit(parent, brief(), key='first'),
            service.admit(parent, brief(), key='second'), return_exceptions=True)
        assert sum(isinstance(item, dict) for item in outcomes) == 1
        refusal = next(item for item in outcomes if isinstance(item, subagents.Refused))
        assert refusal.reason is subagents.RefusalReason.BUDGET
        assert await pool.fetchval("SELECT sum(budget_usd) FROM agent_spawns") == .5
        assert await pool.fetchval("SELECT count(*) FROM activity WHERE kind='subagent_refused'") == 1
    run_db(check, monkeypatch)


def test_database_and_lock_failures_are_retryable_without_spawning(tmp_path, monkeypatch):
    import asyncpg
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch)
        launcher = AsyncMock()
        monkeypatch.setattr(subagents.spawn, 'run_child', launcher)
        for error in (asyncpg.LockNotAvailableError('lock timeout'),
                      asyncpg.ConnectionDoesNotExistError('disconnected')):
            monkeypatch.setattr(service, '_admit', AsyncMock(side_effect=error))
            with pytest.raises(subagents.Unavailable, match='same key'):
                await service.admit(parent, brief(), key='retry')
        assert not launcher.called
        assert await pool.fetchval("SELECT count(*) FROM agent_spawns") == 0
    run_db(check, monkeypatch)


@pytest.mark.parametrize('reason', list(subagents.CancelReason))
def test_queued_cancellation_records_each_typed_reason(tmp_path, monkeypatch, reason):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch)
        row = await service.admit(parent, brief(), key='cancel')
        cancelled = await service.cancel(row['id'], reason=reason)
        assert cancelled['state'] == 'cancelled' and cancelled['cancel_reason'] == reason.value
        assert cancelled['finished_at'] is not None
        assert (await agent_spawns.commitment('t', parent.parent.record_id, pool=pool))['task_reserved'] == 0
    run_db(check, monkeypatch)


def test_parent_cascade_preserves_other_parents_children(tmp_path, monkeypatch):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch)
        record = await llm_records.start_record(agent_id='worker-other', task_id='t', pool=pool)
        other = replace(parent, parent=SpawnParent('t', record_id=record))
        mine = await service.admit(parent, brief(), key='mine')
        theirs = await service.admit(other, brief(), key='theirs')
        await service.cascade(parent_record_id=parent.parent.record_id,
                              reason=subagents.CancelReason.PARENT_ENDED)
        assert (await agent_spawns.get(mine['id'], pool=pool))['cancel_reason'] == 'parent_ended'
        assert (await agent_spawns.get(theirs['id'], pool=pool))['state'] == 'queued'
    run_db(check, monkeypatch)


def test_subtree_spend_soft_stops_parent_and_manager_cost_is_excluded(tmp_path, monkeypatch):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch, task_budget_usd=.6)
        manager = await llm_records.start_record(agent_id='manager', task_id='t', pool=pool)
        await llm_records.finish_record(manager, status='completed', cost_usd=5, pool=pool)
        row = await service.admit(parent, brief(), key='child', budget_usd=.5)
        assert row['budget_usd'] == .5
        child = await llm_records.start_record(agent_id='explorer-test', task_id='t', run_kind='explorer',
                                              parent_record_id=parent.parent.record_id, pool=pool)
        await llm_records.finish_record(child, status='completed', cost_usd=.2, pool=pool)
        await agent_spawns.move(row['id'], agent_spawns.SpawnState.ASKING,
                                expect=agent_spawns.SpawnState.QUEUED, child_record_id=child, pool=pool)
        await agent_spawns.move(row['id'], agent_spawns.SpawnState.FINISHING,
                                expect=agent_spawns.SpawnState.ASKING, pool=pool)
        await service.finish(row['id'])
        service.stop_parent = AsyncMock()
        await service.enforce_parent_budget(parent.parent.record_id, .5, spent_usd=.31)
        service.stop_parent.assert_awaited_once_with(parent.parent.record_id)
        service.stop_parent.reset_mock()
        await service.enforce_parent_budget(parent.parent.record_id, .5, spent_usd=.1)
        service.stop_parent.assert_not_awaited()
    run_db(check, monkeypatch)


@pytest.mark.parametrize('change', ['task', 'project'])
@pytest.mark.parametrize('full', [False, True])
def test_run_rechecks_task_and_project_after_preparation(tmp_path, monkeypatch, change, full):
    from looper.agents import knowledge

    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch)
        row = await service.admit(parent, brief(), key='stale-launch')
        if full:
            other = await service.admit(parent, brief(), key='occupied')
            await agent_spawns.move(other['id'], agent_spawns.SpawnState.ASKING,
                                    expect=agent_spawns.SpawnState.QUEUED, pool=pool)
            await pool.execute("INSERT INTO task_concurrency_limits(task_id,role,max_concurrency) VALUES('t','subagent',1)")
        original = knowledge.load_knowledge

        async def change_during_preparation(*args, **kwargs):
            result = await original(*args, **kwargs)
            if change == 'task':
                await pool.execute("UPDATE tasks SET status='cancelled' WHERE id='t'")
            else:
                await pool.execute("UPDATE projects SET subagents='off' WHERE id=(SELECT project_id FROM tasks WHERE id='t')")
            return result

        monkeypatch.setattr(knowledge, 'load_knowledge', change_during_preparation)
        no_spawn = AsyncMock(side_effect=AssertionError('stale queued explorer must not launch'))
        monkeypatch.setattr(subagents.spawn, 'run_child', no_spawn)
        result = await service.run(row['id'])
        assert result['state'] == 'cancelled' and result['cancel_reason'] == 'task_ended'
        assert result['child_record_id'] is None and result['finished_at'] is not None
        assert (await agent_spawns.commitment('t', parent.parent.record_id, pool=pool))['task_reserved'] == (.5 if full else 0)
        no_spawn.assert_not_awaited()
    run_db(check, monkeypatch)


@pytest.mark.parametrize('cost', [.02, None])
def test_timeout_is_classified_and_unknown_cost_is_charged_conservatively(tmp_path, monkeypatch, cost):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch)
        row = await service.admit(parent, brief(), key='timeout')
        child = await llm_records.start_record(agent_id='explorer-timeout', task_id='t', run_kind='explorer',
                                              parent_record_id=parent.parent.record_id, pool=pool)
        await llm_records.finish_record(child, status='failed', cost_usd=cost, pool=pool)
        await agent_spawns.move(row['id'], agent_spawns.SpawnState.ASKING,
                                expect=agent_spawns.SpawnState.QUEUED, child_record_id=child, pool=pool)
        await agent_spawns.move(row['id'], agent_spawns.SpawnState.FINISHING,
                                expect=agent_spawns.SpawnState.ASKING,
                                result={'child': {'failure': 'timeout'}}, pool=pool)
        result = await service.finish(row['id'])
        assert result['state'] == 'failed'
        assert result['failure_class'] == 'judgment' and result['failure_cause'] == 'timeout'
        assert result['spent_usd'] == (cost if cost is not None else row['budget_usd'])
        assert (await agent_spawns.commitment('t', parent.parent.record_id, pool=pool))['task_reserved'] == 0
    run_db(check, monkeypatch)


def test_run_waits_for_project_switch_transaction(tmp_path, monkeypatch):
    from looper.agents import knowledge

    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch)
        row = await service.admit(parent, brief(), key='project-lock')
        prepared = asyncio.Event()
        original = knowledge.load_knowledge
        no_spawn = AsyncMock(side_effect=AssertionError('disabled project must not launch'))
        monkeypatch.setattr(subagents.spawn, 'run_child', no_spawn)
        async with pool.acquire() as conn:
            transaction = conn.transaction()
            await transaction.start()

            async def prepare_with_pending_switch(*args, **kwargs):
                result = await original(*args, **kwargs)
                await conn.execute("UPDATE projects SET subagents='off' WHERE id=(SELECT project_id FROM tasks WHERE id='t')")
                prepared.set()
                return result

            monkeypatch.setattr(knowledge, 'load_knowledge', prepare_with_pending_switch)
            running = asyncio.create_task(service.run(row['id']))
            try:
                await asyncio.wait_for(prepared.wait(), 10)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(running), .1)
            finally:
                await transaction.commit()
                result = await asyncio.wait_for(running, 10)
        assert result['state'] == 'cancelled' and result['cancel_reason'] == 'task_ended'
        no_spawn.assert_not_awaited()
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("metadata", [
    {"model_reported": "claude-haiku-5-5"},
    {"model_reported": "claude-opus-5-5", "unknown_price_models": ["claude-haiku-5-5", "other-unknown"]},
])
def test_unknown_task_spend_warns_when_reserving_an_explorer(tmp_path, monkeypatch, caplog, metadata):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch, task_budget_usd=1)
        unknown = await llm_records.start_record(agent_id="worker-previous", task_id="t", pool=pool)
        await llm_records.finish_record(unknown, status="completed", model="haiku", metadata=metadata, pool=pool)
        await service.admit(parent, brief(), key="unknown-task-cost", budget_usd=.1)
        assert "claude-haiku-5-5" in caplog.text and "task spend cap" in caplog.text
        if metadata.get("unknown_price_models"):
            assert "other-unknown" in caplog.text
    run_db(check, monkeypatch)


@pytest.mark.parametrize("metadata", [
    {"model_reported": "claude-haiku-5-5"},
    {"model_reported": "claude-opus-5-5", "unknown_price_models": ["claude-haiku-5-5", "other-unknown"]},
])
def test_unknown_parent_spend_warns_during_child_settlement(tmp_path, monkeypatch, caplog, metadata):
    async def check(pool):
        service, parent = await setup(pool, tmp_path, monkeypatch)
        import json
        await pool.execute("UPDATE llm_records SET model='haiku', metadata=$2::jsonb WHERE id=$1",
                           parent.parent.record_id, json.dumps(metadata))
        await service.enforce_parent_budget(parent.parent.record_id, .5)
        assert "claude-haiku-5-5" in caplog.text and "parent spend cap" in caplog.text
        if metadata.get("unknown_price_models"):
            assert "other-unknown" in caplog.text
    run_db(check, monkeypatch)
