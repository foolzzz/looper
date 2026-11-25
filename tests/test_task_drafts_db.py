"""Draft lifecycle, API boundaries and default-off compatibility on PostgreSQL."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from looper.agents import api_v1, projects as service, task_conversations_settings as settings
from looper.agents.action_levels import ActionLevel
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.ops import ErrorCode, OpError
from looper.agents.ops.service import MANAGER_TOOLS
from looper.agents.tiers.task import TaskRouter
from looper.db import cost_holds, projects, tasks
from tests.db_support import run_db, v1_api

GOLDEN = Path(__file__).parent / "fixtures" / "work_mode_off.json"


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(settings, "_enabled", False)
    monkeypatch.setattr(service, "_root", "")


async def project(pool, tmp_path, **fields):
    return await projects.create_project(name="Draft project", work_dir=str(tmp_path), pool=pool, **fields)


async def draft(pool, tmp_path, **data):
    owner = await project(pool, tmp_path)
    result = await service.create_task_core({"project_id": owner["id"], **data}, draft=True)
    return result["task_id"], owner


def test_create_draft_refused_when_off(monkeypatch, tmp_path):
    op = api_v1.find("POST", "/api/v1/tasks/create_draft")
    assert op.operator_only and op.risk == "write" and op.level is ActionLevel.INSIDE
    async def check(pool):
        owner = await project(pool, tmp_path)
        async with v1_api(tmp_path, pool) as call:
            status, body = await call("tasks.create_draft", {"project_id": owner["id"]})
        assert status == 409 and body["error"]["code"] == "conflict"
        assert body["error"]["message"] == "work mode is off (tasks.conversations.enabled)"
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
    run_db(check, monkeypatch)


def test_create_draft(monkeypatch, tmp_path):
    async def check(pool):
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('ledger.enabled','true')")
        owner = await project(pool, tmp_path)
        await pool.execute("INSERT INTO workflows(key,name,definition) VALUES ('draft-default','Draft default','{}')")
        await pool.execute("UPDATE projects SET default_workflow='draft-default' WHERE id=$1", owner["id"])
        settings.set_enabled(True)
        async with v1_api(tmp_path, pool) as call:
            status, body = await call("tasks.create_draft", {"project_id": owner["id"]})
            assert status == 200, body
            made = body["result"]
            assert set(made) == {"task_id", "status", "created_at", "project_id", "workspace"}
            assert made["status"] == "draft" and made["project_id"] == owner["id"]
            row = await tasks.get_task(made["task_id"])
            assert row["requirement"] == "" and row["title"] is None
            assert row["workflow"] is None
            assert row["work_dir"] == str(tmp_path)
            assert await pool.fetchval("SELECT count(*) FROM ledger_items WHERE task_id=$1", row["id"]) == 0
            assert (await call("tasks.create_draft", {}))[0] == 400
            assert (await call("tasks.create_draft", {"project_id": "missing-project"}))[0] in (400, 404)
            empty = await projects.create_project(name="No directory", pool=pool)
            assert (await call("tasks.create_draft", {"project_id": empty["id"]}))[0] == 400
            assert (await call("tasks.create_draft", {"project_id": owner["id"], "requirement": "hidden"}))[0] == 400
            assert (await call("tasks.create_draft", {"project_id": owner["id"]}, client=True))[0] == 403
    run_db(check, monkeypatch)


@pytest.mark.parametrize("data,kwargs", [
    ({"workflow": "none"}, {}), ({}, {"workflow": "none"}), ({}, {"template_name": "fixture"}),
    ({}, {"task_fields": {"template": {"key": "fixture"}}}),
    ({}, {"task_fields": {"schedule_id": "schedule-fixture"}}),
    ({"config": {"intent_rule_bound": True}}, {}),
])
def test_draft_rejects_workflow_template_schedule_and_rule(monkeypatch, tmp_path, data, kwargs):
    async def check(pool):
        owner = await project(pool, tmp_path)
        with pytest.raises(service.RequestError, match="a draft has no workflow, template, schedule or rule") as caught:
            await service.create_task_core({"project_id": owner["id"], **data}, draft=True, **kwargs)
        assert caught.value.status == service.HTTP_400
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
    run_db(check, monkeypatch)


def test_draft_not_active(monkeypatch, tmp_path):
    assert tasks.DRAFT not in tasks.ACTIVE_STATUSES
    assert tasks.DRAFT not in tasks.RESTARTABLE_STATUSES
    assert tasks.STATUS_FILTERS[tasks.DRAFT] == (tasks.DRAFT,)
    async def check(pool):
        task_id, owner = await draft(pool, tmp_path)
        assert tasks.task_group("draft") is tasks.TaskGroup.DRAFT
        assert await tasks.others_active_in(str(tmp_path), "other-task") is False
        assert await tasks.started_task_ids(besides=[]) == []
        with pytest.raises(service.RequestError) as caught:
            await service.check_task_control(task_id, "restart")
        assert caught.value.status == service.HTTP_409
        router = TaskRouter(runtime_root=tmp_path)
        router._bus = SimpleNamespace(publish=AsyncMock())
        await router._handle_restart(MessageEnvelope.create(source=MANAGER_ADDRESS, target=TASK_ROUTER_ADDRESS,
            message_type=MessageType.TASK_RESTART, payload={"task_id": task_id}))
        assert task_id not in router._tasks and task_id not in router._planners
        await tasks.create_task(task_id="positive-active", requirement="Running fixture", project_id=owner["id"],
                                status="executing", work_dir=str(tmp_path), pool=pool)
        assert await tasks.others_active_in(str(tmp_path), task_id) is True
        await tasks.create_task(task_id="positive-pending", requirement="Pending fixture", project_id=owner["id"],
                                status="pending", work_dir=str(tmp_path), pool=pool)
        assert await tasks.started_task_ids(besides=[]) == ["positive-pending"]
    run_db(check, monkeypatch)


def test_draft_start(monkeypatch, tmp_path):
    async def check(pool):
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('ledger.enabled','true')")
        task_id, _ = await draft(pool, tmp_path)
        bus = SimpleNamespace(publish=AsyncMock())
        router = SimpleNamespace(has_planner=lambda _: False)
        with pytest.raises(service.RequestError, match="a draft needs a requirement before it starts") as caught:
            await service.start_ready_task(task_id, bus, MANAGER_ADDRESS)
        assert caught.value.status == service.HTTP_400
        assert (await tasks.get_task(task_id))["status"] == "draft"
        bus.publish.assert_not_awaited()
        from looper.agents.ops import tasks as manager_tasks
        with pytest.raises(OpError) as caught:
            await manager_tasks._control(SimpleNamespace(arguments={"task_id": task_id, "action": "start"}))
        assert caught.value.code is ErrorCode.CONFLICT
        assert str(caught.value) == (f"task {task_id} is draft, not ready: start only starts a task that "
                                     "was created and never started")
        edited = await service.edit_requirement(task_id, {"requirement": "Build the draft."},
            task_router=router, bus=bus, source=MANAGER_ADDRESS, via=service.VIA_TASK_PAGE)
        assert edited["version"] == 2
        versions = await pool.fetch("SELECT * FROM task_requirement_versions WHERE task_id=$1 ORDER BY version", task_id)
        assert [(v["version"], v["text"]) for v in versions] == [(1, ""), (2, "Build the draft.")]
        assert all(v["delivered_at"] is not None for v in versions)
        await service.start_ready_task(task_id, bus, MANAGER_ADDRESS)
        bus.publish.assert_awaited_once()
        envelope = bus.publish.call_args.args[0]
        assert envelope.message_type is MessageType.TASK_START and envelope.payload == {"task_id": task_id}
        # The TaskRouter consumes TASK_START through this database transition.
        started = await tasks.start_ready_task(task_id)
        assert started["status"] == "pending"
        assert await tasks.start_ready_task(task_id) is None
        assert await pool.fetchval("SELECT count(*) FROM ledger_items WHERE task_id=$1", task_id) == 1
    run_db(check, monkeypatch)


def test_draft_start_needs_directory(monkeypatch, tmp_path):
    async def check(pool):
        task_id, _ = await draft(pool, tmp_path, requirement="Build fixture")
        await pool.execute("UPDATE tasks SET work_dir='' WHERE id=$1", task_id)
        bus = SimpleNamespace(publish=AsyncMock())
        with pytest.raises(service.RequestError, match="has no directory") as caught:
            await service.start_ready_task(task_id, bus, MANAGER_ADDRESS)
        assert caught.value.status == service.HTTP_400
        assert (await tasks.get_task(task_id))["status"] == "draft"
        bus.publish.assert_not_awaited()
    run_db(check, monkeypatch)


def test_draft_console_http_lifecycle(monkeypatch, tmp_path):
    import httpx

    from looper.agents.bus import MessageBus
    from looper.agents.message import channel_address
    from looper.agents.ops.clients import ClientOps
    from looper.agents.ops.service import Platform
    from looper.db import api_clients
    from tests.test_api_v1_auth import _serving
    from tests.test_operator_auth import TOKEN

    async def check(pool):
        await pool.execute("INSERT INTO system_settings(key,value) VALUES ('ledger.enabled','true')")
        owner = await project(pool, tmp_path)
        _, client_token = await api_clients.create("Draft lifecycle client", pool=pool)
        settings.set_enabled(True)
        bus = MessageBus(runtime_root=tmp_path)
        publish = AsyncMock(wraps=bus.publish)
        bus.publish = publish
        router = TaskRouter(runtime_root=tmp_path)
        router._bus = bus
        start = AsyncMock()
        monkeypatch.setattr(router, "_start", start)
        try:
            async with _serving(tmp_path, listen=True) as server:
                server._task_router = router
                server._supervisor = SimpleNamespace(bus=bus)
                server._web_adapter = SimpleNamespace(_address=channel_address("web"))
                server._client_ops = ClientOps(Platform(bus=bus, task_router=router, runtime_root=tmp_path))
                assert server._port != 8767 and server._port > 0
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server._port}",
                        headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10) as client:
                    response = await client.post("/api/v1/tasks/create_draft", json={"project_id": owner["id"]})
                    assert response.status_code == 200, response.text
                    task_id = response.json()["result"]["task_id"]
                    response = await client.post("/api/v1/ops/task_control/start", json={"task_id": task_id})
                    assert response.status_code == 400, response.text
                    assert response.json()["error"]["message"] == "a draft needs a requirement before it starts"
                    assert (await tasks.get_task(task_id))["status"] == "draft"
                    response = await client.post("/api/v1/tasks/requirement_update", json={
                        "task_id": task_id, "requirement": "Build the HTTP fixture."})
                    assert response.status_code == 200, response.text
                    response = await client.post("/api/v1/ops/task_control/start", json={"task_id": task_id},
                        headers={"Authorization": f"Bearer {client_token}"})
                    assert response.status_code == 409, response.text
                    assert response.json()["error"]["code"] == "conflict"
                    assert (await tasks.get_task(task_id))["status"] == "draft"
                    response = await client.post("/api/v1/ops/task_control/start", json={"task_id": task_id})
                    assert response.status_code == 200, response.text
                    assert (await tasks.get_task(task_id))["status"] == "draft"
                    messages = [call.args[0] for call in publish.await_args_list
                                if call.args[0].message_type is MessageType.TASK_START]
                    assert len(messages) == 1
                    await router._handle_start(messages[0])
                    assert (await tasks.get_task(task_id))["status"] == "pending"
                    start.assert_awaited_once()
                    await router._handle_start(messages[0])
                    start.assert_awaited_once()
                    assert await pool.fetchval("SELECT count(*) FROM ledger_items WHERE task_id=$1", task_id) == 1
        finally:
            await bus.shutdown()
    run_db(check, monkeypatch)


def test_draft_delete(monkeypatch, tmp_path):
    async def check(pool):
        task_id, _ = await draft(pool, tmp_path)
        async with v1_api(tmp_path, pool) as call:
            status, body = await call("tasks.delete", {"task_id": task_id})
        assert status == 200, body
        assert await tasks.get_task(task_id) is None
    run_db(check, monkeypatch)


def test_draft_shape_check(monkeypatch, tmp_path):
    from looper.db import bridge_upgrade, connection
    from looper.db.migrations import task_drafts
    async def check(pool):
        owner = await project(pool, tmp_path)
        for status in ("init", "executing", "completed"):
            await tasks.create_task(task_id=f"legacy-{status}", requirement="", status=status,
                                    project_id=owner["id"], pool=pool)
        async with pool.acquire() as conn:
            assert await task_drafts.pending(conn) is False
            assert await conn.fetchval("SELECT convalidated FROM pg_constraint WHERE conrelid='tasks'::regclass "
                                      "AND conname='tasks_draft_shape'") is True
            await connection.ensure_schema(conn)
            assert {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")} == {
                bridge_upgrade.BASELINE}
            await conn.execute("ALTER TABLE tasks DROP CONSTRAINT tasks_draft_shape")
            await conn.execute("DELETE FROM schema_migrations WHERE name=ANY($1::text[])",
                               ["task_drafts", "task_drafts_online"])
            assert await task_drafts.pending(conn) is True
            await task_drafts.apply(conn, sql="", state={})
            assert await task_drafts.pending(conn) is False
            await task_drafts.apply(conn, sql="", state={})
            assert await conn.fetchval("SELECT convalidated FROM pg_constraint WHERE conrelid='tasks'::regclass "
                                      "AND conname='tasks_draft_shape'") is False
            await connection._finish_online(conn)
            assert await conn.fetchval("SELECT convalidated FROM pg_constraint WHERE conrelid='tasks'::regclass "
                                      "AND conname='tasks_draft_shape'") is True
            assert {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")} == {
                bridge_upgrade.BASELINE, "task_drafts", "task_drafts_online"}
        for column, value in (("schedule_id", "fixture-schedule"), ("completed_at", "2026-01-01T00:00:00Z")):
            with pytest.raises(asyncpg.CheckViolationError) as caught:
                await pool.execute(f"INSERT INTO tasks(id,project_id,status,requirement,{column}) "
                                   "VALUES ('invalid-draft',$1,'draft','',$2)", owner["id"],
                                   value if column == "schedule_id" else datetime.fromisoformat(value))
            assert caught.value.constraint_name == "tasks_draft_shape"
        assert task_drafts.order == task_drafts.pending_order == task_drafts.online_order == 2050
    run_db(check, monkeypatch)


def test_cost_hold_leaves_draft(monkeypatch, tmp_path):
    async def check(pool):
        task_id, owner = await draft(pool, tmp_path)
        for name, status in ((task_id, "draft"), ("hold-ready", "init"), ("hold-live", "executing")):
            if name != task_id:
                await tasks.create_task(task_id=name, requirement="Fixture", status=status,
                                        project_id=owner["id"], pool=pool)
            await cost_holds.create_hold("task", name, "provider_priced_usd", 1, 2, "budget", task_id=name, pool=pool)
            assert (await tasks.get_task(name))["status"] == ("awaiting_budget" if status == "executing" else status)
    run_db(check, monkeypatch)


def test_task_page_counts_draft(monkeypatch, tmp_path):
    from looper.agents.overview import group_counts

    async def check(pool):
        task_id, owner = await draft(pool, tmp_path)
        await tasks.create_task(task_id="page-ready", requirement="Ready", status="init", project_id=owner["id"], pool=pool)
        async with v1_api(tmp_path, pool) as call:
            status, body = await call("tasks.page")
            assert status == 200, body
            page = body["result"]
            assert page["counts"]["draft"] == 1
            found = next(row for row in page["tasks"] if row["task_id"] == task_id)
            assert found["group"] == "draft"
            status, body = await call("tasks.page", {"status": "draft"})
            assert status == 200 and [row["task_id"] for row in body["result"]["tasks"]] == [task_id]
        assert await pool.fetchval("SELECT count(*) FROM tasks WHERE status=ANY($1::text[])", list(tasks.ACTIVE_STATUSES)) == 0
        counts = group_counts(await tasks.status_counts())
        assert counts["draft"] == 1
        assert counts["in_progress"] == counts["needs_you"] == 0
    run_db(check, monkeypatch)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_work_mode_off_freeze(monkeypatch, tmp_path):
    from looper.agents.overview import group_counts

    frozen = json.loads(GOLDEN.read_text())
    assert settings.enabled() is False
    assert group_counts([]) == {"needs_you": 0, "in_progress": 0, "ready": 0, "attention": 0,
                                "completed": 0, "cancelled": 0, "other": 0, "total": 0, "other_statuses": {}}
    op = api_v1.find("POST", "/api/v1/tasks/create")
    assert _canonical(op.request) == _canonical(frozen["request"])
    manager = [{"name": t.name, "schema": t.schema, "description": t.description} for t in MANAGER_TOOLS]
    assert _canonical(manager) == _canonical(frozen["manager_tools"])
    ids = {item["operationId"] for methods in api_v1.document()["paths"].values() for item in methods.values()}
    old = set(frozen["operation_ids"])
    assert ids - old == {"tasks.create_draft", "task_conversations.get", "task_conversations.update"}
    assert old - ids == set()
    async def check(pool):
        owner = await projects.create_project(name="Frozen project", work_dir=str(tmp_path), pool=pool)
        result = await api_v1.domain_for(op).run(None, op, {"project_id": owner["id"],
            "requirement": "  Build the frozen fixture.  ", "config": {"fixture": "fixed"},
            "goal_id": "frozen-goal", "workspace_mode": "in_place"}, api_v1.OPERATOR, {})
        row = dict(await pool.fetchrow("SELECT * FROM tasks WHERE id=$1", result["task_id"]))
        replacements = {str(tmp_path): "<workspace>", owner["id"]: "<project>", result["task_id"]: "<task>",
                        str(row["created_at"]): "<timestamp>", str(row["updated_at"]): "<timestamp>",
                        result["created_at"]: "<timestamp>"}
        def normalize(value):
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [normalize(item) for item in value]
            if value is None or isinstance(value, (bool, int, float)):
                return value
            return replacements.get(str(value), str(value))
        assert _canonical(normalize(result)) == _canonical(frozen["create_result"])
        assert _canonical(normalize(row)) == _canonical(frozen["create_row"])
    run_db(check, monkeypatch)
