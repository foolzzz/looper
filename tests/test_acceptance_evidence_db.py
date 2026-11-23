"""Append-only acceptance evidence and its operator-only HTTP readback."""
import asyncio
import json

import pytest

from looper.agents import api_v1
from looper.agents.action_levels import ActionLevel
from looper.db import connection, plans, projects, step_check_runs as evidence, tasks
from tests.db_support import DSN, drop_tables, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
CHECK = {"id": "c1", "criterion": 1, "type": "command", "command": "printf measured", "required": True}


async def seed(pool):
    owner = await projects.create_project(name="Evidence fixture", pool=pool)
    await tasks.create_task(task_id="task-evidence", requirement="Measure", project_id=owner["id"], pool=pool)
    await tasks.create_task(task_id="task-other", requirement="Other", project_id=owner["id"], pool=pool)
    await plans.save("task-evidence", plan={"plan_id": "p", "status": "executing"}, steps=[{
        "id": "step-evidence", "index": 0, "key": "s1", "instruction": "Measure",
        "acceptance_criteria": ["works"], "checks": [CHECK]}, {
        "id": "step-empty", "index": 1, "key": "s2", "instruction": "Other",
        "acceptance_criteria": ["works"]}], pool=pool)


def test_rounds_are_append_only_and_numbered_atomically(monkeypatch):
    async def run(pool):
        await seed(pool)
        first = await evidence.create_run("task-evidence", "step-evidence", CHECK, 0,
            head_sha="head", content_sha="content", guarded=True, pool=pool)
        assert first["state"] == "running" and first["run"] == 1 and first["guarded"]
        done = await evidence.finish_run(first["id"], evidence.CheckState.PASSED, exit_code=0, duration_ms=17,
            output_tail="measured", output_bytes=8, output_sha256="digest", pool=pool)
        assert done["state"] == "passed" and done["finished_at"] is not None
        assert done["output_tail"] == "measured" and done["output_bytes"] == 8
        assert await evidence.finish_run(first["id"], evidence.CheckState.ERROR,
            error_kind=evidence.ErrorKind.INTERRUPTED, pool=pool) is None
        rounds = await asyncio.gather(*(evidence.create_run("task-evidence", "step-evidence", CHECK, 0,
            pool=pool) for _ in range(2)))
        assert sorted(row["run"] for row in rounds) == [2, 3]
        retry = await evidence.create_run("task-evidence", "step-evidence", CHECK, 1, pool=pool)
        assert retry["run"] == 1
        rows = await evidence.list_runs("task-evidence", "step-evidence", pool=pool)
        assert len(rows) == 4 and rows[0] == done
        assert await evidence.list_runs("task-other", pool=pool) == []
        with pytest.raises(ValueError, match="step"):
            await evidence.create_run("task-other", "step-evidence", CHECK, 0, pool=pool)
        with pytest.raises(ValueError):
            await evidence.finish_run(retry["id"], evidence.CheckState.RUNNING, pool=pool)
        with pytest.raises(ValueError):
            await evidence.finish_run(retry["id"], evidence.CheckState.ERROR, pool=pool)
        with pytest.raises(ValueError):
            await evidence.finish_run(retry["id"], evidence.CheckState.PASSED,
                error_kind=evidence.ErrorKind.TIMEOUT, pool=pool)
    run_db(run, monkeypatch)


def test_restart_interrupts_only_running_rows_and_preserves_history(monkeypatch):
    async def run(pool):
        await seed(pool)
        old = await evidence.create_run("task-evidence", "step-evidence", CHECK, 0, pool=pool)
        done = await evidence.finish_run(old["id"], evidence.CheckState.FAILED, exit_code=1,
            protected_changed=["tests/test_report.py"], pool=pool)
        running = await evidence.create_run("task-evidence", "step-evidence", CHECK, 1, pool=pool)
        recovered = await evidence.recover_interrupted(pool=pool)
        assert len(recovered) == 1 and recovered[0]["id"] == running["id"]
        assert recovered[0]["state"] == "error" and recovered[0]["error_kind"] == "interrupted"
        assert recovered[0]["finished_at"] is not None
        assert await evidence.recover_interrupted(pool=pool) == []
        rows = await evidence.list_runs("task-evidence", pool=pool)
        assert rows[0] == done
        rerun = await evidence.create_run("task-evidence", "step-evidence", CHECK, 1, pool=pool)
        assert rerun["run"] == 2
        await tasks.delete_task("task-evidence", pool=pool)
        assert await evidence.list_runs("task-evidence", pool=pool) == []
    run_db(run, monkeypatch)


def test_operator_http_readback_includes_definitions_and_metadata_only(tmp_path, monkeypatch):
    async def run(pool):
        await seed(pool)
        row = await evidence.create_run("task-evidence", "step-evidence", CHECK, 0, pool=pool)
        await evidence.finish_run(row["id"], evidence.CheckState.PASSED, exit_code=0,
            artifacts=[{"name": "1.json", "mime": "application/json", "bytes": 2, "sha256": "hash"}], pool=pool)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("task_evidence.get", {"task": "task-evidence"}, client=True)
            assert status == 403, answer
            for selector in (None, "s1", "step-evidence"):
                arguments = {"task": "task-evidence"}
                if selector is not None:
                    arguments["step"] = selector
                status, answer = await call("task_evidence.get", arguments)
                assert status == 200, answer
                result = answer["result"]
                assert result["task_id"] == "task-evidence"
                measured = next(step for step in result["steps"] if step["step_id"] == "step-evidence")
                assert measured["checks"] == [CHECK]
                run = measured["runs"][0]
                assert run["state"] == "passed" and run["guarded"] is False
                assert run["artifacts"] == [{"name": "1.json", "mime": "application/json", "bytes": 2, "sha256": "hash"}]
                assert "path" not in json.dumps(result) and "content" not in run["artifacts"][0]
            status, answer = await call("task_evidence.get", {"task": "task-evidence", "step": "s2"})
            assert status == 200 and answer["result"]["steps"][0]["runs"] == []
            for arguments in ({"task": "missing"}, {"task": "task-evidence", "step": "missing"}):
                status, answer = await call("task_evidence.get", arguments)
                assert status == 404, answer
        operation = api_v1.find("POST", "/api/v1/task_evidence/get")
        assert operation.operator_only and operation.level is ActionLevel.INSIDE and operation.tool is None
    run_db(run, monkeypatch)


def test_evidence_migration_is_additive_and_idempotent(monkeypatch):
    async def run(pool):
        await seed(pool)
        restore = await drop_tables(pool, "step_check_runs")
        await pool.execute("DELETE FROM schema_migrations WHERE name = 'step_check_runs'")
        before = await pool.fetchval("SELECT to_jsonb(s)::text FROM task_steps s WHERE id='step-evidence'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            await restore()
            assert not await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
        assert await pool.fetchval("SELECT to_jsonb(s)::text FROM task_steps s WHERE id='step-evidence'") == before
        row = await evidence.create_run("task-evidence", "step-evidence", CHECK, 0, pool=pool)
        assert row["run"] == 1
    run_db(run, monkeypatch)
