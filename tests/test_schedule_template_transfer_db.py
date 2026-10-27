"""Portable static schedule template selections on disposable PostgreSQL schemas."""

from __future__ import annotations

import json
from typing import Any

import pytest

from looper.agents import config_transfer as transfer
from looper.db.connection import ensure_schema
from tests.db_support import DSN, fresh_schema, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _document(**selection: Any) -> dict[str, Any]:
    templates = [{"key": key, "name": key.title(), "project": "Example",
                  "requirement": "Review {{url}}.", "planner_instructions": "Check public reports.",
                  "inputs": [{"key": "url", "label": "Report URL", "kind": "url", "required": required}]}
                 for key, required in (("report", True), ("optional", False), ("required", True))]
    return {"format": transfer.V4_FORMAT, "managers": [{"id": "manager-review", "name": "Review",
            "enabled": False, "system_prompt": "Review public example reports.", "templates": templates}],
            "projects": [{"name": "Example", "schedules": [{"name": "Nightly", "cron": "0 9 * * *",
                "timezone": "UTC", "requirement": "Record a concise summary.", "manager": "manager-review",
                **selection}]}]}


async def _run(pool: Any, document: dict[str, Any], *, apply: bool = True) -> Any:
    async with pool.acquire() as conn:
        return await transfer.run_import(conn, document, apply=apply)


async def _export(pool: Any) -> dict[str, Any]:
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        return json.loads("".join([chunk async for chunk in transfer.export_chunks(
            conn, include_projects=True, include_secrets=False)]))


async def _state(pool: Any) -> dict[str, Any]:
    return {table: list(await pool.fetch(f"SELECT * FROM {table} ORDER BY 1"))
            for table in ("managers", "manager_task_templates", "projects", "schedules", "schedule_runs", "tasks")}


def _change(plan: Any) -> Any:
    return next(change for change in plan.changes if change.section == "project.schedules")


def test_template_schedule_preview_has_zero_writes_and_apply_roundtrips_selection(monkeypatch):
    async def test(pool):
        document = _document(template_key="report", template_inputs={"url": "https://example.org/report"})
        before = await _state(pool)
        preview = await _run(pool, document, apply=False)
        assert _change(preview).action is transfer.Action.ADDED
        assert await _state(pool) == before
        applied = await _run(pool, document)
        assert applied.view() == {**preview.view(), "applied": True}
        source = await pool.fetchrow("SELECT * FROM schedules")
        assert source["template_key"] == "report"
        assert json.loads(source["template_inputs"]) == {"url": "https://example.org/report"}
        assert not source["enabled"]
        exported = await _export(pool)
        schedule = exported["projects"][0]["schedules"][0]
        assert schedule["template_key"] == "report"
        assert schedule["template_inputs"] == {"url": "https://example.org/report"}
        assert "runs" not in schedule and "template" not in schedule
        async with fresh_schema() as target:
            await ensure_schema(target)
            await _run(target, exported)
            stored = await target.fetchrow("SELECT project_id, manager_id, template_key, template_inputs FROM schedules")
            assert stored["project_id"] != source["project_id"]
            assert stored["manager_id"] == source["manager_id"] == "manager-review"
            assert stored["template_key"] == "report"
            assert json.loads(stored["template_inputs"]) == schedule["template_inputs"]
            assert await target.fetchval("SELECT count(*) FROM schedule_runs") == 0
            assert (await _export(target))["projects"][0]["schedules"] == exported["projects"][0]["schedules"]

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("selection,reason", [
    ({"template_key": "missing", "template_inputs": {"url": "https://example.org/report"}}, "missing"),
    ({"template_key": "report", "template_inputs": {}}, "needs Report URL"),
    ({"template_inputs": {"url": "https://example.org/report"}}, "template_key"),
])
def test_unresolved_explicit_template_or_inputs_are_visible_without_free_form_fallback(monkeypatch, selection, reason):
    async def test(pool):
        document = _document(**selection)
        for apply in (False, True):
            plan = await _run(pool, document, apply=apply)
            change = _change(plan)
            assert change.action is transfer.Action.SKIPPED
            assert reason in change.reason
            assert await pool.fetchval("SELECT count(*) FROM schedules") == 0
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0

    run_db(test, monkeypatch, timeout=25)


@pytest.mark.parametrize("selection,key,inputs", [
    ({}, "report", {"url": "https://example.org/stored"}),
    ({"template_key": "report"}, "report", {"url": "https://example.org/stored"}),
    ({"template_key": "optional"}, "optional", {}),
    ({"template_key": None}, None, {}),
], ids=["omitted", "same-key", "changed-key", "null"])
def test_existing_schedule_template_selection_merges_exactly(monkeypatch, selection, key, inputs):
    async def test(pool):
        await _run(pool, _document(template_key="report", template_inputs={"url": "https://example.org/stored"}))
        before = await pool.fetchrow("SELECT * FROM schedules")
        document = _document(**selection)
        del document["managers"]  # Resolve already-imported owner/templates without rewriting their definitions.
        document["projects"][0]["schedules"][0]["requirement"] = "Summarize the latest public report."
        preview = await _run(pool, document, apply=False)
        assert _change(preview).action is transfer.Action.CHANGED
        assert await pool.fetchrow("SELECT * FROM schedules") == before
        assert (await _run(pool, document)).view() == {**preview.view(), "applied": True}
        stored = await pool.fetchrow("SELECT template_key, template_inputs FROM schedules")
        assert stored["template_key"] == key
        assert json.loads(stored["template_inputs"]) == inputs

    run_db(test, monkeypatch, timeout=25)


def test_changed_key_without_inputs_checks_new_required_values_and_keeps_existing_schedule(monkeypatch):
    async def test(pool):
        await _run(pool, _document(template_key="report", template_inputs={"url": "https://example.org/stored"}))
        before = await pool.fetchrow("SELECT * FROM schedules")
        document = _document(template_key="required")
        del document["managers"]
        plan = await _run(pool, document)
        assert _change(plan).action is transfer.Action.SKIPPED and "needs Report URL" in _change(plan).reason
        assert await pool.fetchrow("SELECT * FROM schedules") == before

    run_db(test, monkeypatch, timeout=20)


def test_default_only_schedule_export_omits_default_template_extensions(monkeypatch):
    async def test(pool):
        document = _document()
        document.pop("managers")
        document["format"] = transfer.V3_FORMAT
        document["projects"][0]["schedules"][0].pop("manager")
        await _run(pool, document)
        exported = await _export(pool)
        assert exported["format"] == transfer.V3_FORMAT
        schedule = exported["projects"][0]["schedules"][0]
        assert "template_key" not in schedule and "template_inputs" not in schedule
        assert _change(await _run(pool, exported)).action is transfer.Action.UNCHANGED

    run_db(test, monkeypatch, timeout=20)


@pytest.mark.parametrize("conflict", ["profile", "directory"])
def test_import_refuses_schedule_settings_conflicting_with_fixed_template(monkeypatch, conflict):
    async def test(pool):
        await _run(pool, _document(template_key="report", template_inputs={"url": "https://example.org/stored"}))
        document = _document(template_key="report")
        del document["managers"]
        schedule = document["projects"][0]["schedules"][0]
        if conflict == "profile":
            await pool.execute("INSERT INTO agent_profiles (id,name,runtime_id,model_id) "
                               "SELECT 'agent-example-other','Schedule other',runtime_id,model_id "
                               "FROM agent_profiles WHERE id='agent-claude-code-opus'")
            await pool.execute("UPDATE manager_task_templates SET reviewer_profile_id='agent-claude-code-opus'")
            schedule["profiles"] = {"reviewer": "Schedule other"}
        else:
            await pool.execute("UPDATE manager_task_templates SET work_dir='/srv/fixed'")
            await pool.execute("UPDATE schedules SET work_dir='/srv/current'")
        before = await pool.fetchrow("SELECT * FROM schedules")
        for apply in (False, True):
            change = _change(await _run(pool, document, apply=apply))
            assert change.action is transfer.Action.SKIPPED
            assert ("Agent Profile" if conflict == "profile" else "work_dir") in change.reason
            assert await pool.fetchrow("SELECT * FROM schedules") == before
    run_db(test, monkeypatch, timeout=25)
