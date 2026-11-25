"""Network pause persistence, bounded waiting and public readback."""
from datetime import datetime, timedelta, timezone

from looper.db import network_pauses, plans
from looper.agents import attention, metrics, projects
from tests.db_support import run_db


def test_existing_database_adds_pause_storage_idempotently(monkeypatch):
    from looper.db.connection import ensure_schema

    async def body(pool):
        async with pool.acquire() as conn:
            await conn.execute('DROP TABLE task_network_pauses')
            await conn.execute("DELETE FROM schema_migrations WHERE name='network_pauses'")
            await ensure_schema(conn)
            await ensure_schema(conn)
            assert await conn.fetchval("SELECT to_regclass('task_network_pauses') IS NOT NULL")
            assert await conn.fetchval("SELECT count(*) FROM schema_migrations WHERE name='network_pauses'") == 1
            assert await conn.fetchval("SELECT count(*) FROM pg_indexes WHERE schemaname=current_schema() "
                                       "AND indexname='idx_task_network_pauses_active'") == 1
    run_db(body, monkeypatch)


def test_pause_survives_reload_and_preserves_original_deadline(monkeypatch):
    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project','Network recovery')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                           "('task','project','Keep the workspace','waiting')")
        await plans.save('task', plan={'plan_id': 'plan', 'status': 'planning'}, pool=pool)
        assert await attention._failure_holds(None) is attention.NotRead.OFF
        assert 'network_pause' not in await projects.task_detail('task')
        assert 'network_pause' not in await metrics.compute(7, metrics.Filters(), pool=pool)
        started = datetime.now(timezone.utc) - timedelta(seconds=30)
        deadline = started + timedelta(hours=2)
        first = await network_pauses.put('task', {'reason': 'Model API unavailable', 'started_at': started,
            'deadline': deadline, 'continuation': {'kind': 'planner', 'endpoint': {'host': 'api.example.test'}}}, pool=pool)
        updated = await network_pauses.put('task', {'reason': 'Model API unavailable',
            'deadline': deadline + timedelta(hours=1), 'continuation': {'kind': 'planner'}}, pool=pool)
        assert updated['id'] == first['id']
        loaded = await network_pauses.get('task', pool=pool)
        assert loaded['started_at'] == started and loaded['deadline'] == deadline
        assert loaded['continuation'] == {'kind': 'planner'}
        assert await plans.resumable_task_ids(['waiting'], pool=pool) == ['task']
        public = network_pauses.view(first)
        assert 'continuation' not in public and 'endpoint' not in public
        assert public['reason'] == 'Model API unavailable'
        assert (await projects.task_detail('task'))['network_pause'] == public
        assert (await network_pauses.stats(pool=pool))['paused'] == 1
        cleared = await network_pauses.clear('task', pool=pool)
        assert cleared['ended_at'] is not None
        assert await network_pauses.clear('task', pool=pool) is None
        assert await network_pauses.get('task', pool=pool) is None
        assert 'network_pause' not in await projects.task_detail('task')
        stats = await network_pauses.stats(pool=pool)
        assert stats['pause_count'] == stats['resume_count'] == 1
        assert stats['duration_seconds'] >= 30 and stats['paused'] == 0
        newer = await network_pauses.put('task', {'reason': 'New outage', 'deadline': deadline,
                                                   'continuation': {'kind': 'step'}}, pool=pool)
        assert newer['id'] != first['id']
        assert await network_pauses.expire('task', pause_id=first['id'], pool=pool) is None
        assert await network_pauses.clear('task', pause_id=first['id'], pool=pool) is None
        assert (await network_pauses.get('task', pool=pool))['state'] == 'paused'
    run_db(body, monkeypatch)


def test_expired_pause_appears_once_in_needs_you_and_stop_removes_it(monkeypatch):
    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project','Network recovery')")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                           "('task','project','Recover after an outage','waiting_user')")
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('engine.network_pause.enabled','true')")
        now = datetime.now(timezone.utc)
        await network_pauses.put('task', {'reason': 'Model API unavailable', 'deadline': now,
                                          'continuation': {'kind': 'step'}}, pool=pool)
        assert await network_pauses.needs_attention(pool=pool) == []
        await network_pauses.expire('task', pool=pool)
        await network_pauses.expire('task', pool=pool)
        items, count = await attention._failure_holds(None)
        assert count == len(items) == 1
        assert items[0]['context'] == 'task'
        reason = (await network_pauses.get('task', pool=pool))['reason']
        assert items[0]['facts']['reason'] == reason == network_pauses.DEADLINE_REASON
        assert items[0]['facts']['kind'] == 'network'
        assert attention._line('failure_holds', items[0], now).count(reason) == 1
        assert (await network_pauses.stats(pool=pool))['expired_count'] == 1
        metrics_view = await metrics.compute(7, metrics.Filters(), pool=pool)
        assert metrics_view['network_pause']['waiting_user'] == 1
        await pool.execute("UPDATE tasks SET status='stopped' WHERE id='task'")
        assert await network_pauses.needs_attention(pool=pool) == []
    run_db(body, monkeypatch)
