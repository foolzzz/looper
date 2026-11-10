"""Workflow API permissions, replacement and snapshot preservation on disposable PostgreSQL."""

import json

import pytest

from looper.agents import api_v1
from looper.db import projects, tasks
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

PRESET = {"key": "reviewed", "name": "Reviewed", "description": "Independent review.",
    "roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement the requested change."},
              {"key": "review_a", "kind": "review", "role_prompt": "Check the acceptance criteria."}],
    "work_role": "dev", "review_panel": {"members": ["review_a"], "accept_when": "review_a"},
    "max_step_retries": 3, "review_all": True, "dependency_conditions": True}


def test_operator_can_replace_and_delete_without_changing_task_snapshots(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("workflows.put", PRESET)
            assert status == 200, answer
            snapshot = answer["result"]["workflow"]
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/workflows/put").result, answer["result"])
            assert snapshot["max_step_retries"] == 3 and snapshot["roles"][0]["key"] == "dev"
            project = await projects.create_project(name="example", work_dir=str(tmp_path))
            await pool.execute("UPDATE projects SET default_workflow = 'reviewed' WHERE id = $1", project["id"])
            await tasks.create_task(task_id="task-snapshot", requirement="Check", project_id=project["id"])
            await pool.execute("UPDATE tasks SET workflow = $1 WHERE id = 'task-snapshot'", json.dumps(snapshot))

            status, answer = await call("workflows.put", {"key": "reviewed", "name": "Updated"})
            assert status == 200, answer
            replaced = answer["result"]["workflow"]
            assert replaced["name"] == "Updated" and replaced["roles"] == []
            assert replaced["max_step_retries"] is None and replaced["review_panel"] is None
            status, answer = await call("workflows.get", {"key": "reviewed"})
            assert status == 200 and answer["result"]["workflow"] == replaced
            status, answer = await call("workflows.list")
            assert status == 200 and answer["result"]["workflows"] == [replaced]
            status, answer = await call("workflows.delete", {"key": "reviewed"})
            assert status == 200 and answer["result"]["deleted"] == "reviewed", answer
            assert await pool.fetchval("SELECT default_workflow FROM projects WHERE id = $1", project["id"]) is None
            assert json.loads(await pool.fetchval("SELECT workflow FROM tasks WHERE id = 'task-snapshot'")) == snapshot
            assert (await call("workflows.list"))[1]["result"]["workflows"] == []
            status, answer = await call("workflows.get", {"key": "reviewed"})
            assert status == 404 and answer["error"]["code"] == "not_found"

    run_db(test, monkeypatch, timeout=30)


def test_clients_cannot_read_or_write_workflow_presets(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            for action, body in (("list", {}), ("get", {"key": "reviewed"}),
                                 ("put", PRESET), ("delete", {"key": "reviewed"})):
                status, answer = await call("workflows." + action, body, client=True)
                assert status == 403 and answer["error"]["code"] == "forbidden", answer
            assert await pool.fetchval("SELECT count(*) FROM workflows") == 0

    run_db(test, monkeypatch, timeout=30)


def test_workflow_validation_failure_leaves_no_preset(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("workflows.put", {**PRESET, "work_role": "review_a"})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await pool.fetchval("SELECT count(*) FROM workflows") == 0

    run_db(test, monkeypatch, timeout=30)


def test_workflow_names_are_unique_ignoring_case(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            assert (await call("workflows.put", PRESET))[0] == 200
            status, answer = await call("workflows.put", {"key": "other", "name": "REVIEWED"})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await pool.fetchval("SELECT count(*) FROM workflows") == 1

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("budget", [0.00001, 100_000.0001, 0, -1])
def test_invalid_budget_is_rejected_before_storage(tmp_path, monkeypatch, budget):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            # A valid write proves this request path can store a preset.
            assert (await call("workflows.put", {**PRESET, "budget_usd": 0.0001}))[0] == 200
            status, answer = await call("workflows.put", {**PRESET, "budget_usd": budget})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert "budget_usd" in answer["error"]["message"]
            saved = (await call("workflows.get", {"key": PRESET["key"]}))[1]["result"]["workflow"]
            assert saved["budget_usd"] == 0.0001
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("source", ["get", "list"])
def test_put_accepts_readback_body_and_ignores_server_timestamp(tmp_path, monkeypatch, source):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            assert (await call("workflows.put", PRESET))[0] == 200
            status, answer = await call("workflows." + source, {"key": PRESET["key"]} if source == "get" else {})
            assert status == 200, answer
            body = answer["result"]["workflow"] if source == "get" else answer["result"]["workflows"][0]
            body.update(name="Edited", updated_at="2000-01-01T00:00:00Z")
            status, answer = await call("workflows.put", body)
            assert status == 200, answer
            saved = answer["result"]["workflow"]
            assert {k: v for k, v in saved.items() if k != "updated_at"} == {
                k: v for k, v in body.items() if k != "updated_at"}
            assert saved["updated_at"] != body["updated_at"]
    run_db(test, monkeypatch, timeout=30)
