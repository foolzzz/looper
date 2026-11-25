"""Legacy task assignment is explicit, write-once and audited atomically."""

import json

import pytest

from looper.agents import api_v1
from looper.agents.ops import ErrorCode, OpError
from looper.agents.v1_domains import session_tools
from looper.db import connection, tasks
from looper.db.migrations import task_origin_session
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
SESSION = "group:channel-lark:chat-a"


def test_assignment_is_once_only_and_audited(monkeypatch):
    async def check(pool):
        await pool.execute("INSERT INTO projects (id,name) VALUES ('project-one','One')")
        await tasks.create_task(task_id='task-old', requirement='requirement', project_id='project-one')
        await pool.execute("INSERT INTO manager_sessions(manager_id, session_key, kind,channel_id,chat) "
                           "VALUES('manager',$1,'group','channel-lark','chat-a')", SESSION)
        op = api_v1.find("POST", "/api/v1/manager_sessions/assign_task")
        args = {"task_id": "task-old", "session": SESSION, "acknowledge": True}
        assert op.operator_only and op.confirms and op.risk == "high"
        assert await session_tools.run(None, op, args, api_v1.OPERATOR, {}) == {
            "task_id": "task-old", "origin_session": SESSION}
        assert (await tasks.get_task('task-old'))['origin_session'] == SESSION
        audit = await pool.fetchrow("SELECT * FROM manager_actions WHERE tool='manager_sessions.assign_task'")
        assert audit['status'] == 'ok' and audit['task_id'] == 'task-old'
        assert json.loads(audit['arguments']) == args
        for changed in ({}, {"session": "dm:channel-lark:other"}):
            with pytest.raises(OpError):
                await session_tools.run(None, op, args | changed, api_v1.OPERATOR, {})
        await tasks.create_task(task_id='task-unassigned', requirement='requirement', project_id='project-one')
        with pytest.raises(OpError) as exc:
            await session_tools.run(None, op, args | {"task_id": "task-unassigned", "acknowledge": False},
                                    api_v1.OPERATOR, {})
        assert exc.value.code is ErrorCode.INVALID
        assert (await tasks.get_task('task-unassigned'))['origin_session'] is None
    run_db(check, monkeypatch)


def test_assignment_rolls_back_if_audit_fails(monkeypatch):
    async def check(pool):
        await pool.execute("INSERT INTO projects (id,name) VALUES ('project-one','One')")
        await tasks.create_task(task_id='task-old', requirement='requirement', project_id='project-one')
        await pool.execute("INSERT INTO manager_sessions(manager_id,session_key,kind,channel_id,chat) "
                           "VALUES('manager',$1,'group','channel-lark','chat-a')", SESSION)

        async def fail(**kwargs):
            raise RuntimeError("audit unavailable")
        monkeypatch.setattr(session_tools.db_actions, 'record', fail)
        with pytest.raises(RuntimeError):
            await session_tools.run(None, api_v1.find("POST", "/api/v1/manager_sessions/assign_task"),
                                    {"task_id": "task-old", "session": SESSION, "acknowledge": True},
                                    api_v1.OPERATOR, {})
        assert (await tasks.get_task('task-old'))['origin_session'] is None
    run_db(check, monkeypatch)


def test_origin_session_upgrade_is_idempotent(monkeypatch):
    async def check(pool):
        await pool.execute("ALTER TABLE tasks DROP COLUMN origin_session CASCADE")
        assert await task_origin_session.pending(pool)
        await connection.ensure_schema(pool)
        assert not await task_origin_session.pending(pool)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval("SELECT to_regclass('idx_tasks_origin_session') IS NOT NULL")
    run_db(check, monkeypatch)


@pytest.mark.parametrize("kind", ["other_manager", "missing_task", "missing_session", "non_operator"])
def test_assignment_refuses_ineligible_targets_without_changes(monkeypatch, kind):
    async def check(pool):
        await pool.execute("INSERT INTO projects (id,name) VALUES ('project-one','One')")
        await pool.execute("INSERT INTO managers(id,name,system_prompt) VALUES ('manager-other','Other','Manage other tasks')")
        await tasks.create_task(task_id='task-old', requirement='requirement', project_id='project-one',
                                manager_id='manager-other' if kind == 'other_manager' else 'manager')
        if kind != 'missing_session':
            await pool.execute("INSERT INTO manager_sessions(manager_id,session_key,kind,channel_id,chat) "
                               "VALUES('manager',$1,'group','channel-lark','chat-a')", SESSION)
        op = api_v1.find("POST", "/api/v1/manager_sessions/assign_task")
        args = {"task_id": "missing" if kind == 'missing_task' else "task-old",
                "session": SESSION, "acknowledge": True}
        principal = api_v1.Principal('client', client_id='client-synthetic') if kind == 'non_operator' else api_v1.OPERATOR
        with pytest.raises(OpError):
            await session_tools.run(None, op, args, principal, {})
        assert (await tasks.get_task('task-old'))['origin_session'] is None
        assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
    run_db(check, monkeypatch)
