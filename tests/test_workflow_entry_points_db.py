"""Dashboard workflow defaults and immutable task readback through the public API."""

import pytest

from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_dashboard_project_default_creates_task_snapshot_and_can_be_cleared(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            assert (await call("workflows.put", {"key": "reviewed", "name": "Reviewed"}))[0] == 200
            status, answer = await call("project_admin.create", {
                "name": "Example", "work_dir": str(tmp_path), "task_workspaces": "in_place",
                "default_workflow": "reviewed"})
            assert status == 200, answer
            project = answer["result"]["project"]
            assert project["default_workflow"] == "reviewed"
            pid = project["project_id"]
            status, made = await call("tasks.create", {"project_id": pid, "requirement": "Build"})
            assert status == 200, made
            tid = made["result"]["task_id"]
            status, detail = await call("tasks.detail", {"task_id": tid})
            assert status == 200, detail
            assert detail["result"]["workflow"]["key"] == "reviewed"
            status, changed = await call("project_admin.update", {
                "project_id": pid, "fields": {"default_workflow": None}})
            assert status == 200 and changed["result"]["project"]["default_workflow"] is None, changed
            status, listed = await call("project_admin.list")
            assert status == 200 and listed["result"]["projects"][0]["default_workflow"] is None
            assert (await call("tasks.detail", {"task_id": tid}))[1]["result"]["workflow"]["key"] == "reviewed"

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("key", ["missing", "none"])
def test_dashboard_rejects_invalid_project_default_without_losing_existing_default(tmp_path, monkeypatch, key):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            assert (await call("workflows.put", {"key": "reviewed", "name": "Reviewed"}))[0] == 200
            status, made = await call("project_admin.create", {"name": "Example", "default_workflow": "reviewed"})
            assert status == 200, made
            status, refused = await call("project_admin.update", {
                "project_id": made["result"]["project"]["project_id"], "fields": {"default_workflow": key}})
            assert status == 400 and refused["error"]["code"] == "invalid", refused
            assert (await call("project_admin.list"))[1]["result"]["projects"][0]["default_workflow"] == "reviewed"

    run_db(test, monkeypatch, timeout=30)
