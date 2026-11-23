"""Project explorer limits preserve old reads and remain operator-only settings."""
import json
from pathlib import Path

import pytest

from looper.agents import config_transfer, projects as core
from looper.agents.http_server import _serialize
from looper.agents.ops import Turn
from looper.agents.ops import config as manager_config
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.project_execution import ProjectExecution, SUBAGENT_FIELDS, execution_fields
from looper.agents.v1_domains.task_create import NEW_PROJECT_FIELDS, _execution_impact
from looper.db import projects
from tests.db_support import run_db, v1_api

BASELINE = json.loads((Path(__file__).parent / "fixtures/project_subagents_off.json").read_text())
VALUES = {"subagents": "on", "subagent_budget_usd": 0.6, "subagent_task_budget_usd": 4.0,
          "subagents_per_run": 3, "subagents_per_task": 12}


def test_unset_execution_and_manager_compact_view_match_parent():
    assert ProjectExecution().view() == BASELINE["execution"]
    assert ProjectExecution.from_row(dict.fromkeys(SUBAGENT_FIELDS)).view() == BASELINE["execution"]
    old = manager_config._model_project(BASELINE["execution"])
    assert manager_config._model_project({**BASELINE["execution"], **VALUES, "subagents": "off"}) == old
    assert manager_config._model_project({**BASELINE["execution"], **VALUES}) == {**old, **VALUES}
    current = next(tool for tool in manager_config.TOOLS if tool.name == "looper_projects").listing()
    assert json.dumps(current, ensure_ascii=False) == json.dumps(BASELINE["manager_project_tool"], ensure_ascii=False)


@pytest.mark.parametrize("advisor", ["on", "off"])
@pytest.mark.parametrize("subagents", [None, "off"])
def test_manager_compact_view_off_is_byte_identical_to_parent(advisor, subagents):
    before = {**BASELINE["execution"], "advisor": advisor}
    expected = {key: value for key, value in before.items()
                if advisor == "on" or key not in manager_config.ADVISOR_FIELDS}
    current = manager_config._model_project({**before, **VALUES, "subagents": subagents})
    assert json.dumps(current, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)


def test_new_task_project_can_set_and_disclose_explorer_limits():
    assert set(SUBAGENT_FIELDS) <= set(NEW_PROJECT_FIELDS["properties"])
    impact = _execution_impact(ProjectExecution.from_row(VALUES), VALUES)
    assert all(f"- {field}（" in impact for field in SUBAGENT_FIELDS)
    old_impact = _execution_impact(ProjectExecution(), {})
    assert all(f"- {field}（" not in old_impact for field in SUBAGENT_FIELDS)


@pytest.mark.parametrize("field,value", [
    ("subagents", True), ("subagents", "ON"), ("subagent_budget_usd", 0), ("subagent_budget_usd", 5.01),
    ("subagent_budget_usd", float("nan")), ("subagent_task_budget_usd", 50.01),
    ("subagents_per_run", -1), ("subagents_per_run", 21), ("subagents_per_run", True),
    ("subagents_per_task", 101), ("subagents_per_task", 1.5),
])
def test_project_subagent_ranges_reject_invalid_values(field, value):
    with pytest.raises(ValueError, match=field):
        execution_fields({field: value})


def test_project_subagent_ranges_include_boundaries_and_preserve_null():
    for values in ({"subagents": "off", "subagent_budget_usd": .01, "subagent_task_budget_usd": .01,
                    "subagents_per_run": 0, "subagents_per_task": 0},
                   {"subagents": "on", "subagent_budget_usd": 5, "subagent_task_budget_usd": 50,
                    "subagents_per_run": 20, "subagents_per_task": 100}, dict.fromkeys(SUBAGENT_FIELDS)):
        assert execution_fields(values) == values


def test_project_update_readback_export_and_old_import(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Explorer example", pool=pool)
        before = _serialize(core.project_view((await projects.list_projects(pool=pool))[0]))
        assert not set(SUBAGENT_FIELDS).intersection(before)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("project_admin.list")
            assert status == 200 and answer["result"]["projects"] == [before], answer
            async with pool.acquire() as conn:
                off_export = "".join([chunk async for chunk in config_transfer.export_chunks(
                    conn, include_projects=True, include_secrets=False)])
            assert not set(SUBAGENT_FIELDS).intersection(json.loads(off_export)["projects"][0]["execution"])
            status, answer = await call("project_admin.update", {"project_id": project["id"], "fields": VALUES})
            assert status == 200, answer
            assert {key: answer["result"]["project"][key] for key in SUBAGENT_FIELDS} == VALUES
            status, answer = await call("project_admin.list")
            assert {key: answer["result"]["projects"][0][key] for key in SUBAGENT_FIELDS} == VALUES
            for field, value in (("subagents_per_run", 21), ("subagent_budget_usd", 5.01)):
                status, answer = await call("project_admin.update", {"project_id": project["id"], "fields": {field: value}})
                assert status == 400 and field in answer["error"]["message"], answer
            async with pool.acquire() as conn:
                exported = json.loads("".join([chunk async for chunk in config_transfer.export_chunks(
                    conn, include_projects=True, include_secrets=False)]))
            assert {key: exported["projects"][0]["execution"][key] for key in SUBAGENT_FIELDS} == VALUES
            status, answer = await call("project_admin.update", {
                "project_id": project["id"], "fields": dict.fromkeys(SUBAGENT_FIELDS)})
            assert status == 200 and not set(SUBAGENT_FIELDS).intersection(answer["result"]["project"]), answer
            async with pool.acquire() as conn, conn.transaction():
                await config_transfer.import_config(conn, exported, apply=True)
                assert {key: (await conn.fetchrow("SELECT * FROM projects WHERE id=$1", project["id"]))[key]
                        for key in SUBAGENT_FIELDS} == VALUES
                # A legacy file starts the imported project with NULL for every new field.
                await config_transfer.import_config(conn, {"format": config_transfer.FORMAT,
                    "projects": [{"name": "Legacy project"}]}, apply=True)
                legacy = await conn.fetchrow("SELECT * FROM projects WHERE name='Legacy project'")
                assert all(legacy[key] is None for key in SUBAGENT_FIELDS)
            status, answer = await call("project_admin.update", {
                "project_id": project["id"], "fields": dict.fromkeys(SUBAGENT_FIELDS)})
            assert status == 200, answer
            async with pool.acquire() as conn:
                reset_export = "".join([chunk async for chunk in config_transfer.export_chunks(
                    conn, include_projects=True, include_secrets=False)])
            reset = json.loads(reset_export)
            reset["projects"] = [p for p in reset["projects"] if p["name"] == project["name"]]
            original = json.loads(off_export)
            # Export timestamps describe the read, not the project configuration.
            assert isinstance(reset.pop("exported_at"), str)
            assert isinstance(original.pop("exported_at"), str)
            assert reset == original
    run_db(run, monkeypatch)


@pytest.mark.parametrize("field", SUBAGENT_FIELDS)
def test_manager_cannot_change_subagent_settings(field, tmp_path, monkeypatch):
    async def run(pool):
        project = await projects.create_project(name="Example", pool=pool)
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="test")
        turn = Turn(channel="web", chat="test", message_id="first")
        ops.begin_turn(turn)
        try:
            answer = await ops.call("looper_projects", {
                "action": "update", "project": "Example", field: VALUES[field]})
        finally:
            ops.end_turn(turn)
        assert answer["status"] == "error", answer
        assert field in answer["error"]["message"], answer
        saved = await projects.get_project(project["id"], pool=pool)
        assert all(saved[key] is None for key in SUBAGENT_FIELDS)
    run_db(run, monkeypatch)
