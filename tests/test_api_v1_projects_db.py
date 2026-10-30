"""Authenticated project CRUD, execution inheritance and cascade deletion confirmation on PostgreSQL."""

import pytest

from looper.agents import api_v1, schedules
from looper.db import projects
from tests.db_support import DSN, run_db
from tests.test_api_v1_project_role_templates_db import _api
from tests.test_ops_tasks_db import _task

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
EXECUTION = {
    "task_budget_usd": 2.5, "task_budget_action": "wrap_up", "supervise_interval_seconds": 60,
    "review_policy": "all", "distill_lessons": "draft", "step_workspaces": "worktree",
    "workspace_links": [".venv", ".env"], "task_workspaces": "in_place", "operator_notes": "off",
    "vault": "on", "max_step_retries": 3, "planner_roles": "on", "wait_commands": "on",
    "role_text_max_chars": 500, "user_gates": "off", "gate_reminder_hours": 12, "agent_room": "off",
    "room_post_per_minute": 4, "room_broadcasts_per_run": 2, "room_message_max_chars": 500,
    "room_messages_per_task": 20, "room_ping_pong_rounds": 2, "room_wait_max_seconds": 10,
    "room_consult_budget_usd": 0.25, "room_consult_timeout_seconds": 30, "room_consults_per_task": 2,
}
INTEGERS = [field for field, value in EXECUTION.items() if type(value) is int]


@pytest.mark.parametrize("client", [False, True], ids=["operator", "client"])
def test_real_crud_reads_back_changes_and_deletes_only_after_required_confirmation(tmp_path, monkeypatch, client):
    async def run(pool):
        keep = await projects.create_project(name="Keep", pool=pool)
        async with _api(tmp_path, pool) as call:
            status, answer = await call("project_admin.create", {"name": "Example", "description": "Description",
                "work_dir": "/srv/example", "background": "Background"}, client=client)
            assert status == 200, answer
            made = answer["result"]["project"]
            pid = made["project_id"]
            assert made["task_workspaces"] == "worktree" and made["distill_lessons"] == "off"
            assert made["created_at"] and made["updated_at"]
            status, changed = await call("project_admin.update", {"project_id": pid, "fields": {
                "name": "Renamed", "description": "Changed", "work_dir": "", "background": ""}}, client=client)
            assert status == 200, changed
            view = changed["result"]["project"]
            assert [view[field] for field in ("name", "description", "work_dir", "background")] == [
                "Renamed", "Changed", "", ""]
            status, listed = await call("project_admin.list", client=client)
            assert status == 200 and view in listed["result"]["projects"]
            status, deleted = await call("project_admin.delete", {"project_id": pid}, client=client)
            if client:
                assert status == 202 and deleted["status"] == "needs_confirmation", deleted
                assert pid in deleted["impact"] or "Renamed" in deleted["impact"]
                assert await projects.get_project(pid, pool=pool)
                status, deleted = await call("confirm", {"confirmation_id": deleted["confirmation_id"]}, client=True)
            assert status == 200 and deleted["result"] == {"deleted": pid}, deleted
            assert await projects.get_project(pid, pool=pool) is None
            assert (await projects.get_project(keep["id"], pool=pool))["name"] == "Keep"
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("field,value,expected", [
    *[(field, value, value) for field, value in EXECUTION.items()],
    *[(field, None, "off" if field == "distill_lessons" else None) for field in EXECUTION],
    ("task_budget_usd", "2.5", 2.5), ("room_consult_budget_usd", "0.25", 0.25),
    ("distill_lessons", True, "write"), ("distill_lessons", False, "off"), ("distill_lessons", "", "off"),
])
def test_execution_fields_have_canonical_create_update_and_db_readback(tmp_path, monkeypatch, field, value, expected):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            status, answer = await call("project_admin.create", {"name": "Example", field: value})
            assert status == 200 and answer["result"]["project"][field] == expected, answer
            pid = answer["result"]["project"]["project_id"]
            alternate = EXECUTION[field] if expected is None else None
            if field == "distill_lessons":
                alternate = "draft" if expected == "off" else "off"
            await projects.update_project(pid, execution={field: alternate}, pool=pool)
            status, answer = await call("project_admin.update", {"project_id": pid, "fields": {field: value}}, client=True)
            assert status == 200 and answer["result"]["project"][field] == expected, answer
            row = await projects.get_project(pid, pool=pool)
            assert row[field] == expected
            status, answer = await call("project_admin.list", client=True)
            assert status == 200 and answer["result"]["projects"][0][field] == expected, answer
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("field", INTEGERS)
@pytest.mark.parametrize("invalid", [True, "1", 1.5])
def test_integer_execution_fields_reject_nonintegers_without_persisting(tmp_path, monkeypatch, field, invalid):
    async def run(pool):
        row = await projects.create_project(name="Example", execution={field: EXECUTION[field]}, pool=pool)
        async with _api(tmp_path, pool) as call:
            status, answer = await call("project_admin.update", {"project_id": row["id"],
                "fields": {"description": "Must not persist", field: invalid}}, client=True)
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            saved = await projects.get_project(row["id"], pool=pool)
            assert saved[field] == EXECUTION[field] and saved["description"] == ""
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["create", "update"])
def test_name_collision_is_conflict_and_preserves_original_project(tmp_path, monkeypatch, action):
    async def run(pool):
        original = await projects.create_project(name="Original", background="Keep", pool=pool)
        before = await projects.get_project(original["id"], pool=pool)
        other = await projects.create_project(name="Other", pool=pool)
        body = {"name": "Original", "background": "Must not persist"}
        if action == "update":
            body = {"project_id": other["id"], "fields": body}
        async with _api(tmp_path, pool) as call:
            status, answer = await call("project_admin." + action, body, client=True)
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert await projects.get_project(original["id"], pool=pool) == before
            assert (await projects.get_project(other["id"], pool=pool))["name"] == "Other"
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("occupancy", ["task", "schedule", "manager_template"])
def test_project_delete_confirms_tasks_and_schedules_but_refuses_template_use(tmp_path, monkeypatch, occupancy):
    async def run(pool):
        row = await projects.create_project(name="Occupied", pool=pool)
        if occupancy == "task":
            await _task(pool, "task-example", "running", row)
        elif occupancy == "schedule":
            await schedules.create_schedule(row["id"], {"name": "Hourly", "cron": "0 * * * *",
                "timezone": "UTC", "requirement": "Read reports", "work_dir": "/srv/example"})
        else:
            await pool.execute("INSERT INTO manager_task_templates (manager_id, key, name, project_id) "
                "VALUES ('manager', 'example', 'Example', $1)", row["id"])
        async with _api(tmp_path, pool) as call:
            status, answer = await call("project_admin.delete", {"project_id": row["id"]}, client=True)
            if occupancy == "manager_template":
                assert status == 409 and answer["error"]["code"] == "conflict", answer
            else:
                assert status == 202 and answer["status"] == "needs_confirmation", answer
                assert ("停止它正在运行的 1 个任务" if occupancy == "task" else
                        "1 个定时任务（含运行记录）") in answer["impact"]
            assert await projects.get_project(row["id"], pool=pool)
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("fields", [{}, {"unknown": True}, {"name": " Example "}])
def test_empty_or_unknown_patch_is_invalid_without_changes(tmp_path, monkeypatch, fields):
    async def run(pool):
        row = await projects.create_project(name="Example", pool=pool)
        before = await projects.get_project(row["id"], pool=pool)
        async with _api(tmp_path, pool) as call:
            status, answer = await call("project_admin.update", {"project_id": row["id"], "fields": fields})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await projects.get_project(row["id"], pool=pool) == before
            status, answer = await call("project_admin.create", {"name": "Unknown", "unknown": True})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("method,action", [("GET", "list"), ("POST", "create"), ("PUT", "update"), ("DELETE", "delete")])
def test_retired_project_routes_have_authenticated_same_server_v1_positive_controls(tmp_path, monkeypatch, method, action):
    async def run(pool):
        row = await projects.create_project(name="Example", pool=pool)
        body = {"name": "Created"} if action == "create" else {"project_id": row["id"]}
        if action == "update":
            body["fields"] = {"description": "Changed"}
        async with _api(tmp_path, pool) as call:
            status, answer = await call("project_admin." + action, {} if action == "list" else body)
            assert status == 200, answer
            path = "/api/projects" + ("/" + row["id"] if method in {"PUT", "DELETE"} else "")
            assert (await call(path, body, method=method))[0] == 404
    run_db(run, monkeypatch, timeout=30)


def test_existing_tool_operations_keep_compact_list_full_project_and_name_deletion(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            for action, body in (("create", {"name": "Example", "background": "Background"}), ("list", {}),
                ("get", {"project": "Example"}), ("update", {"project": "Example", "background": "Changed"}),
                ("delete", {"project": "Example"})):
                path = "/api/v1/ops/projects/" + action
                status, answer = await call(path, body)
                assert status == 200, answer
                result = answer["result"]
                api_v1.validate_arguments(api_v1.find("POST", path).result, result)
                if action == "list":
                    assert result == {"projects": [{"project": "Example", "work_dir": None, "tasks": 0,
                        "active_tasks": 0, "schedules": 0, "background": "Background"}]}
                elif action == "delete":
                    assert result == {"deleted": "Example"}
                else:
                    assert result["project"]["name"] == "Example" and result["project"]["project_id"]
                    assert result["project"]["background"] == ("Changed" if action == "update" else "Background")
    run_db(run, monkeypatch, timeout=30)
