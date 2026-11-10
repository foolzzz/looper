"""Portable workflow references and project defaults across fresh installations."""
import asyncio
import json

import pytest

from looper.agents.config_transfer import Action, export_chunks, run_import
from looper.agents.workflows import Workflow
from looper.db import projects, workflows
from looper.db.connection import ensure_schema
from tests.db_support import DSN, fresh_schema, run_db, v1_api

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


@pytest.mark.parametrize("route", ["import_upload", "import_preview"])
def test_workflow_export_roundtrip_through_http_import(tmp_path, monkeypatch, route):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            status, created = await call("workflows.put", {
                "key": "reviewed", "name": "Reviewed", "review_all": True})
            assert status == 200, created
            status, exported = await call("config.export")
            assert status == 200, exported
            document = exported["result"]["document"]
            assert document["workflows"][0]["key"] == "reviewed"
            await pool.execute("DELETE FROM workflows")
            body = {"document": document} if route == "import_preview" else document
            status, staged = await call(f"config.{route}", body)
            assert status == 200, staged
            assert not staged["result"]["plan"]["applied"]
            assert await pool.fetchval("SELECT count(*) FROM workflows") == 0
            status, applied = await call("config.import_apply", {
                "import_id": staged["result"]["import_id"]})
            assert status == 200, applied
            assert applied["result"]["plan"]["applied"]
            status, restored = await call("workflows.get", {"key": "reviewed"})
            assert status == 200, restored
            assert {k: v for k, v in restored["result"]["workflow"].items() if k != "updated_at"} == {
                k: v for k, v in created["result"]["workflow"].items() if k != "updated_at"}
            status, reexported = await call("config.export")
            assert status == 200, reexported
            assert reexported["result"]["document"]["workflows"] == document["workflows"]

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("route", ["import_upload", "import_preview"])
def test_workflow_import_requires_operator_identity(tmp_path, monkeypatch, route):
    async def test(pool):
        await workflows.put(Workflow.from_dict({"key": "reviewed", "name": "Reviewed", "review_all": True}), pool=pool)
        before = await workflows.list_workflows(pool=pool)
        document = {"format": "looper-config/v4", "workflows": [
            {"key": "reviewed", "name": "Reviewed", "review_all": False}]}
        body = {"document": document} if route == "import_preview" else document
        async with v1_api(tmp_path, pool) as call:
            status, refused = await call(f"config.{route}", body, client=True)
            assert status == 403 and refused["error"]["code"] == "forbidden", refused
            assert await workflows.list_workflows(pool=pool) == before
            assert not list((tmp_path / "config-imports").glob("*.json"))
            # The same valid document still lets the operator replace the preset.
            status, staged = await call(f"config.{route}", body)
            assert status == 200, staged
            assert await workflows.list_workflows(pool=pool) == before
            status, applied = await call("config.import_apply", {"import_id": staged["result"]["import_id"]})
            assert status == 200 and applied["result"]["plan"]["applied"], applied
            saved = Workflow.from_row(await workflows.get("reviewed", pool=pool))
            assert saved.review_all is False

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["config.import_apply", "confirm"])
def test_client_workflow_import_is_rechecked_before_execution(tmp_path, monkeypatch, action):
    async def test(pool):
        await workflows.put(Workflow.from_dict({"key": "reviewed", "name": "Reviewed", "review_all": True}), pool=pool)
        before = await workflows.list_workflows(pool=pool)
        document = {"format": "looper-config/v4", "workflows": [],
                    "settings": {"scheduler": {"min_interval_seconds": 73}}}
        async with v1_api(tmp_path, pool) as call:
            status, staged = await call("config.import_preview", {"document": document}, client=True)
            assert status == 200, staged
            import_id = staged["result"]["import_id"]
            body = {"import_id": import_id}
            if action == "confirm":
                status, confirmation = await call("config.import_apply", body, client=True)
                assert status == 202, confirmation
                body = {"confirmation_id": confirmation["confirmation_id"]}
            document["workflows"] = [{"key": "reviewed", "name": "Reviewed", "review_all": False}]
            (tmp_path / "config-imports" / f"{import_id}.json").write_text(json.dumps(document))
            status, refused = await call(action, body, client=True)
            assert status == 403 and refused["error"]["code"] == "forbidden", refused
            assert await workflows.list_workflows(pool=pool) == before
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key='scheduler.min_interval_seconds'") is None

    run_db(test, monkeypatch, timeout=30)


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
