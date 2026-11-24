"""Project lesson modes are operator settings, portable without changing model views."""

import json

import pytest

from looper.agents import config_transfer
from looper.agents.ops.config import _model_project
from looper.agents.project_execution import ProjectExecution, execution_fields, omit_unset_subagents
from looper.agents.v1_domains.task_create import NEW_PROJECT_FIELDS, _execution_impact
from tests.db_support import run_db, v1_api


def test_lesson_mode_validation_and_execution_projection():
    for mode in (None, "off", "on"):
        assert execution_fields({"memory_lessons": mode}) == {"memory_lessons": mode}
        view = ProjectExecution.from_row({"memory_lessons": mode}).view()
        assert view.get("memory_lessons") == ("on" if mode == "on" else None)
        assert omit_unset_subagents({"memory_lessons": mode}) == (
            {} if mode is None else {"memory_lessons": mode})
        assert _model_project({"name": "sample", "memory_lessons": mode}) == {"name": "sample"}
    for mode in (True, False, 1, "draft", "", [], {}):
        with pytest.raises(ValueError, match="memory_lessons"):
            execution_fields({"memory_lessons": mode})
    assert not ProjectExecution.from_row({"memory_lessons": "on"}).lessons_on
    assert _execution_impact(ProjectExecution.from_row({"memory_lessons": "on"}), {}) == (
        _execution_impact(ProjectExecution(), {}))
    assert NEW_PROJECT_FIELDS["properties"]["memory_lessons"] == {
        "type": ["string", "null"], "enum": ["off", "on", None]}
    assert "- memory_lessons（项目任务经验设置）：on" in _execution_impact(
        ProjectExecution.from_row({"memory_lessons": "on"}), {"memory_lessons": "on"})


def test_project_lesson_mode_survives_export_import_without_default_keys(monkeypatch, tmp_path):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            for name, mode in (("unset", None), ("disabled", "off"), ("enabled", "on")):
                body = {"name": name, "memory_lessons": mode} if mode else {"name": name}
                status, answer = await call("project_admin.create", body)
                assert status == 200, answer
                project = answer["result"]["project"]
                assert project.get("memory_lessons") == mode
                if mode is None:
                    assert "memory_lessons" not in project
            status, answer = await call("project_admin.list")
            assert status == 200, answer
            projects = {item["name"]: item for item in answer["result"]["projects"]}
            assert projects["disabled"]["memory_lessons"] == "off"
            enabled_id = projects["enabled"]["project_id"]
            for invalid in ("draft", "", True, 0):
                status, answer = await call("project_admin.update", {
                    "project_id": enabled_id, "fields": {"memory_lessons": invalid}})
                assert status == 400, answer
            assert await pool.fetchval("SELECT memory_lessons FROM projects WHERE id=$1", enabled_id) == "on"
            async with pool.acquire() as conn:
                document = json.loads("".join([part async for part in config_transfer.export_chunks(
                    conn, include_projects=True, include_secrets=False)]))
                exported = {item["name"]: item for item in document["projects"]}
                assert "memory_lessons" not in exported["unset"]["execution"]
                assert exported["disabled"]["execution"]["memory_lessons"] == "off"
                assert exported["enabled"]["execution"]["memory_lessons"] == "on"
                await conn.execute("UPDATE projects SET memory_lessons=NULL")
                plan = await config_transfer.run_import(conn, document, apply=True)
                assert not plan.view()["counts"].get("projects", {}).get("skipped", 0), plan.view()
                invalid = {"format": config_transfer.FORMAT, "projects": [
                    {"name": "enabled", "execution": {"memory_lessons": "draft"}}]}
                plan = await config_transfer.run_import(conn, invalid, apply=True)
                assert plan.view()["counts"]["projects"]["skipped"] == 1
            assert dict(await pool.fetch("SELECT name,memory_lessons FROM projects")) == {
                "unset": None, "disabled": "off", "enabled": "on"}
            status, answer = await call("project_admin.update", {
                "project_id": enabled_id, "fields": {"memory_lessons": None}})
            assert status == 200 and "memory_lessons" not in answer["result"]["project"], answer
            assert await pool.fetchval("SELECT memory_lessons FROM projects WHERE id=$1", enabled_id) is None

    run_db(check, monkeypatch, timeout=60)
