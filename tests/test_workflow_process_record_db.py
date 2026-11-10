"""Task process records survive router persistence and each task read surface."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops import Turn
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.tiers.task import Task, TaskRouter
from looper.agents.workflow_reporting import REMOTE_SAFETY
from looper.db import connection, projects, tasks
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("status", ["completed", "failed"])
@pytest.mark.parametrize("record_size", [None, 1600, 9000])
def test_router_record_reaches_task_detail_and_manager_get(monkeypatch, tmp_path, status, record_size):
    async def run(pool):
        project = await projects.create_project(name="Reporting", work_dir=str(tmp_path), pool=pool)
        task_id = "task-reporting"
        await tasks.create_task(task_id=task_id, requirement="Check the change", project_id=project["id"],
                                work_dir=str(tmp_path), status="running", pool=pool)
        record = None if record_size is None else {
            "sections": {"Roles": ["- Profile " + "evidence " * (record_size // 9) + "tail-evidence"],
                         "Reviews": [], "Verification": [], "Escalations": []},
            "remote_safety": REMOTE_SAFETY, "omitted": 0}
        bus = MessageBus(runtime_root=tmp_path)
        router = TaskRouter(runtime_root=tmp_path)
        router._bus = bus
        router._tasks[task_id] = Task.from_row(task_id, await tasks.get_task(task_id, pool=pool))
        summary = "Delivered " + "details " * 200 + "\nProcess record: see the task page"
        try:
            await router._handle_planner_status(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.GOAL_STATUS,
                payload={"task_id": task_id, "status": status, "summary": summary,
                         **({"process_record": record} if record is not None else {})}))
            stored = await tasks.get_task(task_id, pool=pool)
            assert stored["status"] == status
            assert stored["process_record"] == record
            assert stored["result_summary" if status == "completed" else "error"] == summary
            async with v1_api(tmp_path / "http", pool) as call:
                http_status, detail = await call("tasks.detail", {"task_id": task_id})
                assert http_status == 200, detail
                assert detail["result"]["process_record"] == record

            question = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
                message_type=MessageType.USER_MESSAGE, payload={"text": "Show the task"},
                metadata={"origin_channel_id": "channel-web"})
            turn = Turn(channel="web", chat="reporting-thread", message_id=question.message_id,
                        envelope=question, channel_id="channel-web")
            ops = ManagerOps(Platform(bus=bus, task_router=router), url="http://127.0.0.1:9")
            ops.begin_turn(turn)
            try:
                answer = await ops.call("looper_tasks", {"action": "get", "task_id": task_id})
            finally:
                ops.end_turn(turn)
            assert answer["status"] == "ok", answer
            compact = answer["result"]
            assert len(compact["result"]) <= 1000
            if record is None:
                assert "process_record" not in compact
            else:
                text = compact["process_record"]
                assert len(text) <= 6000
                assert "Profile evidence" not in text and "tail-evidence" not in text
                assert REMOTE_SAFETY in text and "已省略 1 条记录。" in text

            # Completed tasks cannot restart; failed tasks start over without a stale outcome.
            monkeypatch.setattr(router, "_start", AsyncMock())
            await router._handle_restart(MessageEnvelope.create(
                source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS, message_type=MessageType.TASK_RESTART,
                payload={"task_id": task_id, "mode": "start_over"}))
            restarted = await tasks.get_task(task_id, pool=pool)
            if status == "completed":
                assert restarted == stored
                router._start.assert_not_awaited()
            else:
                assert restarted["status"] == "pending"
                assert restarted["process_record"] is None
                assert restarted["result_summary"] is None and restarted["error"] is None
                router._start.assert_awaited_once()
        finally:
            await bus.shutdown()

    run_db(run, monkeypatch, timeout=30)


def test_process_record_baseline_upgrade_is_additive_and_runs_once(monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Existing", pool=pool)
        await tasks.create_task(task_id="task-existing", requirement="Keep this task", project_id=project["id"], pool=pool)
        await pool.execute("ALTER TABLE tasks DROP COLUMN process_record")
        await pool.execute("DELETE FROM schema_migrations WHERE name = 'task_process_record'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        row = await tasks.get_task("task-existing", pool=pool)
        assert row["requirement"] == "Keep this task" and row["process_record"] is None
        record = {"sections": {name: [] for name in ("Roles", "Reviews", "Verification", "Escalations")},
                  "remote_safety": REMOTE_SAFETY, "omitted": 0}
        await tasks.update_task_status("task-existing", "completed", process_record=record, pool=pool)
        await connection.ensure_schema(pool)
        assert (await tasks.get_task("task-existing", pool=pool))["process_record"] == record
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name = 'task_process_record'") == 1
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)

    run_db(run, monkeypatch, timeout=30)


def test_process_record_runs_preserve_reported_model_metadata(monkeypatch):
    from looper.agents.v1_domains.runs import _model_fields
    from looper.db import llm_records

    async def run(pool):
        cases = [({}, None), ({"model_display": "cursor-actual"}, "cursor-actual"),
                 ({"model_display": "cursor-display", "model_reported": "cursor-reported"}, "cursor-reported"),
                 ({"model_display": "cursor-display", "model_reported": None}, None),
                 ({"model_display": "Auto Balance", "routed_model": "Grok 4.7"}, "Grok 4.7"),
                 ({"model_reported": "Auto Balance", "routed_model": "Grok 4.7"}, "Grok 4.7"),
                 ({"model_display": None}, None),
                 *[({"model_display": "cursor-legacy", "profile": empty,
                     "agent": {"runtime": {"type": "cursor-agent"}}}, "cursor-legacy")
                   for empty in (None, {})]]
        expected = []
        for fields, reported in cases:
            metadata = {"profile": {"runtime": {"type": "cursor-agent"}},
                        "model_choice": {"model": "auto"}, **fields, "system_prompt": "Excluded prompt"}
            rid = await llm_records.start_record(agent_id="worker-model", task_id="task-model",
                                                 metadata=metadata, pool=pool)
            await llm_records.finish_record(rid, status="completed", model="auto", pool=pool)
            expected.append((rid, fields, reported))

        rows = await llm_records.process_record_runs("task-model", pool=pool)
        assert len(rows) == len(expected)
        for row, (rid, fields, reported) in zip(rows, expected):
            assert row["id"] == rid and row["model"] == "auto"
            assert {key: row["metadata"][key] for key in ("model_display", "model_reported", "routed_model")
                    if key in row["metadata"]} == {key: fields[key] for key in ("model_display", "model_reported", "routed_model")
                                                 if key in fields}
            assert row["metadata"]["model_choice"] == {"model": "auto"}
            assert "system_prompt" not in row["metadata"]
            assert _model_fields(row)["model_reported"] == reported

    run_db(run, monkeypatch, timeout=30)
