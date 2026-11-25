"""Work-list contracts through PostgreSQL and authenticated v1 HTTP."""

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import api_v1
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable PostgreSQL")
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def seed(pool, task_id, status="running", *, project="p", activity=0, schedule=None, at=NOW):
    await pool.execute("INSERT INTO projects(id,name) VALUES ($1,$1) ON CONFLICT DO NOTHING", project)
    await pool.execute("INSERT INTO tasks(id,requirement,project_id,status,schedule_id,created_at,updated_at) "
                       "VALUES ($1,$2,$3,$4,$5,$6,$6)", task_id, "x" * 240, project, status, schedule, at)
    seq = 0
    for _ in range(activity):
        seq = await pool.fetchval("INSERT INTO activity(task_id,kind,created_at) VALUES ($1,'progress',$2) "
                                  "RETURNING seq", task_id, at + timedelta(minutes=1))
    return seq


def test_open_section_order_and_fields(tmp_path, monkeypatch):
    async def run(pool):
        await seed(pool, "waiting", "waiting_user", activity=1)
        await seed(pool, "gate", activity=1, at=NOW + timedelta(hours=1))
        await seed(pool, "other", project="q")
        await pool.execute("UPDATE tasks SET title='Named',goal_id='Goal' WHERE id='gate'")
        await pool.execute("UPDATE tasks SET goal_id='Fallback' WHERE id='waiting'")
        await pool.execute("INSERT INTO task_steps(id,task_id,step_index,instruction) VALUES ('g','gate',0,'gate')")
        await pool.execute("INSERT INTO task_user_gates(step_id,task_id,topic,state) VALUES ('g','gate','Decision','open')")
        async with v1_api(tmp_path, pool) as call:
            code, body = await call("tasks.work_list", {"section": "open", "project_id": "p"})
            assert code == 200, body
            result = body["result"]
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/tasks/work_list").result, result)
            assert result["counts"] == {"running": 1, "waiting_user": 1}
            assert result["next_cursor"] is None
            rows = result["items"]
            assert [r["task_id"] for r in rows] == ["gate", "waiting"]
            assert [r["title"] for r in rows] == ["Named", "Fallback"]
            assert [r["group"] for r in rows] == ["in_progress", "needs_you"]
            assert [r["waiting_gates"] for r in rows] == [1, 0]
            assert all(r["needs_you"] and r["unseen"] for r in rows)
            assert set(rows[0]) == {"task_id", "title", "requirement_head", "project_id", "project_name",
                "status", "group", "last_activity_at", "last_seq", "waiting_gates", "needs_you", "unseen",
                "run_no", "created_at", "ended_at"}
            assert rows[0]["requirement_head"] == "x" * 200
            assert rows[0]["last_activity_at"] == (NOW + timedelta(hours=1, minutes=1)).isoformat()
            assert rows[0]["ended_at"] is None and rows[0]["run_no"] == 1
            code, body = await call("tasks.work_list", {"section": "open", "project_id": "p", "limit": 1})
            assert code == 200 and body["result"]["next_cursor"]
            code, body = await call("tasks.work_list", {"section": "open", "project_id": "p", "limit": 1,
                                                     "cursor": body["result"]["next_cursor"]})
            assert code == 200 and [r["task_id"] for r in body["result"]["items"]] == ["waiting"]
            assert body["result"]["next_cursor"] is None
            code, body = await call("tasks.work_list", {"section": "open", "statuses": ["waiting_user"]})
            assert code == 200 and body["result"]["counts"] == {"waiting_user": 1}
            code, body = await call("tasks.work_list", {"section": "open", "project_id": "missing"})
            assert code == 200 and body["result"] == {"items": [], "counts": {}, "next_cursor": None}
    run_db(run, monkeypatch)


def test_full_list_no_cap(tmp_path, monkeypatch):
    async def run(pool):
        for i in range(120):
            # Ties exercise the id half of the keyset as well as its timestamp.
            await seed(pool, f"t{i:03}", "completed", at=NOW + timedelta(minutes=i // 2))
        await pool.execute("UPDATE tasks SET completed_at=updated_at,updated_at=updated_at+interval '1 day'")
        async with v1_api(tmp_path, pool) as call:
            ids, cursor = [], None
            for size in (50, 50, 20):
                args = {"section": "ended", "limit": 50, **({"cursor": cursor} if cursor else {})}
                code, body = await call("tasks.work_list", args)
                assert code == 200, body
                page = body["result"]
                assert len(page["items"]) == size and page["counts"] == {"completed": 120}
                ids.extend(r["task_id"] for r in page["items"])
                assert all(r["ended_at"] < r["last_activity_at"] for r in page["items"])
                cursor = page["next_cursor"]
                assert bool(cursor) == (size == 50)
            assert ids == [f"t{i:03}" for i in reversed(range(120))]
            assert len(set(ids)) == 120
    run_db(run, monkeypatch)


def test_schedule_folding(tmp_path, monkeypatch):
    async def run(pool):
        await seed(pool, "manual")
        for sid, failure in (("s1", 0), ("s2", 3)):
            await pool.execute("INSERT INTO schedules(id,name,project_id,cron,timezone,requirement,consecutive_failures) "
                               "VALUES ($1,$1,'p','* * * * *','UTC','scheduled',$2)", sid, failure)
            await seed(pool, sid + "old", schedule=sid, at=NOW)
            await seed(pool, sid + "new", "failed" if failure else "completed", schedule=sid,
                       at=NOW + timedelta(hours=1))
        async with v1_api(tmp_path, pool) as call:
            code, body = await call("tasks.work_list", {"section": "schedules", "limit": 1})
            assert code == 200 and body["result"]["next_cursor"]
            row = body["result"]["schedules"][0]
            assert row["last_task_id"] == row["schedule_id"] + "new"
            assert row["running"]
            cursor = body["result"]["next_cursor"]
            code, second = await call("tasks.work_list", {"section": "schedules", "limit": 1, "cursor": cursor})
            assert code == 200 and second["result"]["next_cursor"] is None
            assert {rows[0]["schedule_id"] for rows in (body["result"]["schedules"], second["result"]["schedules"])} == {"s1", "s2"}
            schedules = {r["schedule_id"]: r for r in body["result"]["schedules"] + second["result"]["schedules"]}
            assert schedules["s2"]["last_status"] == "failed" and schedules["s2"]["consecutive_failures"] == 3
            assert schedules["s1"]["last_status"] == "completed" and schedules["s1"]["consecutive_failures"] == 0
            await pool.execute("INSERT INTO schedules(id,name,project_id,cron,timezone,requirement) "
                               "VALUES ('empty','empty','p','* * * * *','UTC','no runs')")
            code, body = await call("tasks.work_list", {"section": "schedules", "project_id": "p"})
            empty = next(r for r in body["result"]["schedules"] if r["schedule_id"] == "empty")
            assert code == 200 and empty["last_task_id"] is None and not empty["running"]
            for section, expected in (("open", ["manual"]), ("ended", [])):
                code, body = await call("tasks.work_list", {"section": section})
                assert code == 200 and [r["task_id"] for r in body["result"]["items"]] == expected
    run_db(run, monkeypatch)


def test_unseen_per_operator(tmp_path, monkeypatch):
    async def run(pool):
        seq = await seed(pool, "active", activity=1)
        await seed(pool, "ended", "completed", activity=1)
        await seed(pool, "quiet")
        await pool.execute("INSERT INTO task_seen(principal,task_id,seen_activity_seq) VALUES ('someone-else','active',$1)", seq)
        async with v1_api(tmp_path, pool) as call:
            async def unseen():
                code, body = await call("tasks.work_list", {"section": "open"})
                assert code == 200, body
                return {r["task_id"]: r["unseen"] for r in body["result"]["items"]}
            assert await unseen() == {"active": True, "quiet": False}
            code, body = await call("tasks.mark_seen", {"task_id": "active", "seq": seq})
            assert code == 200, body
            assert not (await unseen())["active"]
            await call("tasks.mark_seen", {"task_id": "active", "seq": 0})
            assert await pool.fetchval("SELECT seen_activity_seq FROM task_seen WHERE principal='operator' AND task_id='active'") == seq
            assert not (await unseen())["active"]
            await pool.execute("INSERT INTO activity(task_id,kind) VALUES ('active','progress')")
            assert (await unseen())["active"]
            code, body = await call("tasks.work_list", {"section": "ended"})
            assert code == 200 and not body["result"]["items"][0]["unseen"]
            code, body = await call("tasks.mark_seen", {"task_id": "missing", "seq": 0})
            assert code == 404 and body["error"]["code"] == "not_found"
    run_db(run, monkeypatch)


@pytest.mark.parametrize("cursor", ["garbage", base64.b64encode(b'[]').decode(),
    base64.b64encode(json.dumps(["open", "bad-time", "id"]).encode()).decode(),
    base64.b64encode(json.dumps(["open", "2026-01-01T00:00:00", "id"]).encode()).decode(),
    base64.b64encode(json.dumps(["open", NOW.isoformat(), "\x00"]).encode()).decode(),
    base64.b64encode(json.dumps(["open", NOW.isoformat(), "\ud800"]).encode()).decode(),
    base64.b64encode(json.dumps(["ended", NOW.isoformat(), "id"]).encode()).decode()])
def test_bad_cursor(tmp_path, monkeypatch, cursor):
    async def run(pool):
        await seed(pool, "positive")
        async with v1_api(tmp_path, pool) as call:
            assert (await call("tasks.work_list", {"section": "open"}))[0] == 200
            code, body = await call("tasks.work_list", {"section": "open", "cursor": cursor})
            assert code == 400 and body["error"]["code"] == "invalid"
            assert body["error"]["field"] == "cursor"
    run_db(run, monkeypatch)


@pytest.mark.parametrize("action,args", [
    ("tasks.work_list", {"section": "open", "unknown": True}),
    ("tasks.work_list", {"section": "schedules", "statuses": ["failed"]}),
    ("tasks.work_list", {"section": "open", "statuses": []}),
    ("tasks.work_list", {"section": "open", "limit": 101}),
    ("tasks.mark_seen", {"task_id": "task", "seq": -1}),
    ("tasks.mark_seen", {"task_id": "task", "seq": 2**63}),
])
def test_invalid_request(tmp_path, monkeypatch, action, args):
    async def run(pool):
        async with v1_api(tmp_path, pool) as call:
            code, body = await call(action, args)
            assert code == 400 and body["error"]["code"] == "invalid", body
    run_db(run, monkeypatch)


def test_operator_only(tmp_path, monkeypatch):
    async def run(pool):
        await seed(pool, "task")
        async with v1_api(tmp_path, pool) as call:
            for action, args in (("tasks.work_list", {"section": "open"}),
                                 ("tasks.mark_seen", {"task_id": "task", "seq": 0})):
                code, body = await call(action, args, client=True)
                assert code == 403 and body["error"]["code"] == "forbidden", body
                assert "operator" in body["error"]["message"]
                assert (await call(action, args))[0] == 200
    run_db(run, monkeypatch)


def test_query_count(monkeypatch):
    async def run(pool):
        from looper.db import work_list
        for i in range(20):
            await seed(pool, f"task{i}", activity=1)
        for limit in (1, 20):
            queries = []
            async with pool.acquire() as conn:
                def capture(query):
                    queries.append(query.query)
                conn.add_query_logger(capture)
                monkeypatch.setattr(work_list, "get_pool", lambda: conn)
                result = await work_list.open_section(api_v1.OPERATOR, project_id=None, statuses=None, cursor=None, limit=limit)
                await asyncio.sleep(0)
                assert len(result["items"]) == limit
                assert len(queries) <= 3, queries
                conn.remove_query_logger(capture)
    run_db(run, monkeypatch)


def test_migration(monkeypatch):
    async def run(pool):
        from looper.db import connection
        from looper.db.migrations import task_work_list
        await seed(pool, "existing")
        await pool.execute("DROP TABLE task_seen; DROP INDEX idx_tasks_open; DROP INDEX idx_tasks_ended; "
                           "DELETE FROM schema_migrations WHERE name IN ('task_work_list','task_work_list_online')")
        assert (task_work_list.order, task_work_list.pending_order, task_work_list.online_order) == (2060, 2060, 2060)
        async with pool.acquire() as conn:
            assert await task_work_list.pending(conn)
            async with conn.transaction():
                await task_work_list.apply(conn, sql="", state={})
            assert await conn.fetchval("SELECT to_regclass('task_seen') IS NOT NULL")
            queries = []
            conn.add_query_logger(lambda q: queries.append(q.query))
            await connection._finish_online(conn)
            await asyncio.sleep(0)
            assert sum(q.startswith("CREATE INDEX CONCURRENTLY") for q in queries) == 2
            assert await conn.fetchval("SELECT bool_and(indisvalid) FROM pg_index WHERE indexrelid "
                                      "IN ('idx_tasks_open'::regclass,'idx_tasks_ended'::regclass)")
            queries.clear()
            assert not await task_work_list.pending(conn)
            await task_work_list.apply(conn, sql="", state={})
            await connection._finish_online(conn)
            await asyncio.sleep(0)
            assert not any(q.startswith(("CREATE", "INSERT", "ALTER", "DROP")) for q in queries)
            assert await conn.fetchval("SELECT count(*) FROM tasks WHERE id='existing'") == 1
    run_db(run, monkeypatch)
