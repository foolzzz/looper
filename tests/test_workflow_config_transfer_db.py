"""Portable workflow references and project defaults across fresh installations."""
import asyncio
import json

import pytest

from looper.agents.config_transfer import Action, export_chunks, run_import
from looper.agents.workflows import Workflow
from looper.db import projects, workflows
from looper.db.connection import ensure_schema
from tests.db_support import DSN, fresh_schema, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def exported(pool):
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        data = json.loads("".join([chunk async for chunk in export_chunks(
            conn, include_projects=True, include_secrets=False)]))
    data.pop("exported_at")
    return data


async def imported(pool, data, *, apply=True):
    async with pool.acquire() as conn:
        return await run_import(conn, {"format": "looper-config/v4", **data}, apply=apply)


def test_workflow_and_project_defaults_roundtrip_into_fresh_installation():
    async def test():
        async with fresh_schema() as source, fresh_schema() as destination:
            for pool in (source, destination):
                await ensure_schema(pool)
            await source.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                "SELECT 'transfer-profile', 'Development profile', 'rt-claude-code', id "
                "FROM llm_models WHERE provider_id = 'prov-claude-subscription' AND name = 'sonnet'")
            definition = {"key": "reviewed", "name": "Reviewed", "profiles": {"worker": "transfer-profile"},
                "roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement the change.",
                           "profile_id": "transfer-profile"}], "work_role": "dev", "dependency_conditions": True}
            await workflows.put(Workflow.from_dict(definition), pool=source)
            project = await projects.create_project(name="Example", execution={"dependency_conditions": "on"},
                                                    pool=source)
            await source.execute("UPDATE projects SET default_workflow = 'reviewed' WHERE id = $1", project["id"])
            first = await exported(source)
            assert first["workflows"][0]["profiles"] == {"worker": "Development profile"}
            assert first["workflows"][0]["roles"][0]["profile_id"] == "Development profile"
            assert first["projects"][0]["default_workflow"] == "reviewed"
            assert first["projects"][0]["execution"]["dependency_conditions"] == "on"
            plan = await imported(destination, first, apply=False)
            assert any(c.section == "workflows" and c.action is Action.ADDED for c in plan.changes)
            assert await destination.fetchval("SELECT count(*) FROM workflows") == 0
            plan = await imported(destination, first)
            assert not any(c.action is Action.SKIPPED for c in plan.changes), plan.view()
            assert await exported(destination) == first
            plan = await imported(destination, first)
            assert plan.writes == 0, plan.view()
    asyncio.run(asyncio.wait_for(test(), 45))


def test_workflow_import_replaces_same_key_and_skips_invalid_entries_with_reason():
    async def test(pool):
        await workflows.put(Workflow.from_dict({"key": "reviewed", "name": "Original"}), pool=pool)
        plan = await imported(pool, {"workflows": [
            {"key": "reviewed", "name": "Updated", "review_all": True},
            {"key": "invalid", "name": "Invalid", "work_role": "review_a"},
            {"key": "collision", "name": "UPDATED"}]})
        row = Workflow.from_row(await workflows.get("reviewed", pool=pool))
        assert row.name == "Updated" and row.review_all
        assert await workflows.get("invalid", pool=pool) is None
        assert await workflows.get("collision", pool=pool) is None
        rejected = {c.key: c.reason for c in plan.changes if c.action is Action.SKIPPED}
        assert "must name a workflow work role" in rejected["invalid"]
        assert "already in use" in rejected["collision"]
    run_db(test, timeout=30)


def test_missing_profiles_and_workflow_default_are_cleared_with_notes():
    async def test(pool):
        plan = await imported(pool, {"workflows": [{"key": "reviewed", "name": "Reviewed",
            "profiles": {"worker": "Absent profile"},
            "roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement the change.",
                       "profile_id": "Absent role profile"}]}],
            "projects": [{"name": "Example", "default_workflow": "absent"}]})
        saved = Workflow.from_row(await workflows.get("reviewed", pool=pool))
        assert saved.profiles == {} and saved.roles[0].profile_id is None
        assert (await projects.find_project_by_name("Example", pool=pool))["default_workflow"] is None
        assert any("Absent profile" in note for note in plan.notes)
        assert any("Absent role profile" in note for note in plan.notes)
        assert any("no valid workflow 'absent'" in note for note in plan.notes)
    run_db(test, timeout=30)


def test_import_rejects_fixed_developer_and_reviewer_using_same_runtime():
    async def test(pool):
        name = await pool.fetchval("SELECT name FROM agent_profiles WHERE id = 'agent-claude-code-opus'")
        assert name
        plan = await imported(pool, {"workflows": [{"key": "reviewed", "name": "Reviewed",
            "profiles": {"worker": name, "reviewer": name}, "distinct_reviewer_runtime": True}]})
        assert await workflows.get("reviewed", pool=pool) is None
        rejected = next(c for c in plan.changes if c.section == "workflows")
        assert rejected.action is Action.SKIPPED and "different runtime" in rejected.reason
    run_db(test, timeout=30)


def test_export_keeps_workflows_usable_after_their_profile_is_deleted(monkeypatch):
    async def test(pool):
        from looper.agents.profile_registry import delete_profile

        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
            "SELECT 'transfer-profile', 'Development profile', 'rt-claude-code', id "
            "FROM llm_models WHERE provider_id = 'prov-claude-subscription' AND name = 'sonnet'")
        await workflows.put(Workflow.from_dict({"key": "reviewed", "name": "Reviewed",
            "profiles": {"worker": "transfer-profile"},
            "roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement the change.",
                       "profile_id": "transfer-profile"}]}), pool=pool)
        assert (await exported(pool))["workflows"][0]["profiles"] == {"worker": "Development profile"}
        await delete_profile("transfer-profile")
        definition = (await exported(pool))["workflows"][0]
        assert definition["profiles"] == {} and definition["roles"][0]["profile_id"] is None
        assert Workflow.from_dict(definition).key == "reviewed"
    run_db(test, monkeypatch, timeout=30)
