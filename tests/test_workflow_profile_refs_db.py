"""Workflow profile grants survive deletion attempts and edits of task roles."""
import asyncpg
import pytest

from looper.agents import profile_registry, workflows as workflow_service
from looper.agents import api_v1
from looper.agents.agent_settings import load_agent_settings
from looper.agents.workflows import Workflow
from looper.db import profile_registry as db_profiles, projects, task_roles, tasks, workflows
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def seed(pool):
    await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
        "SELECT 'workflow-profile', 'Workflow profile', 'rt-claude-code', id "
        "FROM llm_models WHERE provider_id = 'prov-claude-subscription' AND name = 'sonnet'")


def preset(binding):
    return {"key": "reviewed", "name": "Reviewed", **binding}


@pytest.mark.parametrize("binding", [
    {"profiles": {"worker": "workflow-profile"}},
    {"roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement.",
                "profile_id": "workflow-profile"}]},
])
def test_workflow_only_reference_is_visible_protected_and_usable(tmp_path, monkeypatch, binding):
    async def test(pool):
        await seed(pool)
        monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})
        async with v1_api(tmp_path, pool) as call:
            assert (await call("workflows.put", preset(binding)))[0] == 200
            status, listing = await call("registry.view")
            assert status == 200, listing
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/registry/view").result, listing["result"])
            profile = next(p for p in listing["result"]["profiles"] if p["profile_id"] == "workflow-profile")
            assert profile["used_by"]["workflows"] == ["Reviewed"]
            status, answer = await call("profiles.delete", {"profile_id": "workflow-profile"})
            assert status == 409 and "Reviewed" in answer["error"]["message"], answer
            project = await projects.create_project(name="Example", work_dir=str(tmp_path),
                execution={"task_workspaces": "in_place"})
            status, answer = await call("tasks.create", {"project_id": project["id"],
                "requirement": "Build", "workflow": "reviewed"})
            assert status == 200, answer
            # Releasing a preset's reference updates usage immediately; no stale reference cache.
            assert (await call("workflows.put", preset({})))[0] == 200
            assert not (await db_profiles.profile_usage("workflow-profile")).get("workflow-profile", {}).get("workflows")
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("key,kind,profile_id,allowed", [
    ("dev", "work", "workflow-profile", True),
    ("dev", "work", "other-profile", False),
    ("other", "work", "workflow-profile", False),
    ("dev", "review", "workflow-profile", False),
])
def test_role_readback_and_runtime_use_only_the_exact_snapshot_grant(monkeypatch, key, kind, profile_id, allowed):
    async def test(pool):
        await seed(pool)
        await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
            "SELECT 'other-profile', 'Other profile', runtime_id, model_id FROM agent_profiles "
            "WHERE id = 'workflow-profile'")
        project = await projects.create_project(name="Restricted")
        await pool.execute("INSERT INTO project_allowed_profiles (project_id, profile_id) "
            "VALUES ($1, 'agent-claude-code-opus')", project["id"])
        await tasks.create_task(task_id="workflow-task", requirement="Build", project_id=project["id"],
            workflow=preset({"roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement.",
                                       "profile_id": "workflow-profile"}]}))
        # Includes user versions already stored before the edit validator was fixed.
        await task_roles.add_version("workflow-task", key, {"kind": kind, "profile_id": profile_id,
            "role_prompt": "Edited.", "via": "user"},
            {"step_id": "", "kind": "role_defined", "summary": "Edited."}, after=0)
        role = await task_roles.get("workflow-task", key)
        assert role["profile_allowed"] is allowed
        agent = "worker" if kind == "work" else "reviewer"
        inherited = await load_agent_settings(agent, project["id"], "workflow-task")
        effective = await load_agent_settings(agent, project["id"], "workflow-task", task_role=(key, 1))
        assert effective.profile.id == (profile_id if allowed else inherited.profile.id)
    run_db(test, monkeypatch, timeout=30)


def test_profile_deleted_after_workflow_validation_cannot_leave_a_dangling_reference(tmp_path, monkeypatch):
    async def test(pool):
        await seed(pool)
        check = workflow_service.check_profiles

        async def check_then_delete(workflow):
            await check(workflow)
            await profile_registry.delete_profile("workflow-profile")

        monkeypatch.setattr(workflow_service, "check_profiles", check_then_delete)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("workflows.put", preset({"profiles": {"worker": "workflow-profile"}}))
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert await workflows.get("reviewed") is None
    run_db(test, monkeypatch, timeout=30)


def test_workflow_save_holds_profile_reference_until_commit(monkeypatch):
    async def test(pool):
        await seed(pool)
        async with pool.acquire() as writer, writer.transaction():
            await workflows.put(Workflow.from_dict(preset({"profiles": {"worker": "workflow-profile"}})), pool=writer)
            async with pool.acquire() as deleter, deleter.transaction():
                await deleter.execute("SET LOCAL lock_timeout = '100ms'")
                with pytest.raises(asyncpg.LockNotAvailableError):
                    async with deleter.transaction():
                        await deleter.execute("DELETE FROM agent_profiles WHERE id = 'workflow-profile'")
        with pytest.raises(profile_registry.RegistryConflict):
            await profile_registry.delete_profile("workflow-profile")
    run_db(test, monkeypatch, timeout=30)


def test_workflow_saved_after_delete_preflight_still_blocks_deletion(tmp_path, monkeypatch):
    async def test(pool):
        await seed(pool)
        delete = db_profiles.delete_profile_unchosen

        async def save_then_delete(*args, **kwargs):
            await workflows.put(Workflow.from_dict(preset({"profiles": {"worker": "workflow-profile"}})))
            return await delete(*args, **kwargs)

        monkeypatch.setattr(db_profiles, "delete_profile_unchosen", save_then_delete)
        with pytest.raises(profile_registry.RegistryConflict):
            await profile_registry.delete_profile("workflow-profile")
        assert await db_profiles.get_profile("workflow-profile") is not None
        await workflows.delete("reviewed")
        monkeypatch.setattr(db_profiles, "delete_profile_unchosen", delete)
        assert await db_profiles.profile_usage("workflow-profile") == {}
        assert await profile_registry.delete_profile("workflow-profile") == {"deleted": "workflow-profile"}
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("edit", [{"role_prompt": "Implement carefully."},
    {"role_prompt": "Implement.", "harness": "Check your work."}])
def test_workflow_role_edit_retains_snapshot_profile_grant(tmp_path, monkeypatch, edit):
    async def test(pool):
        await seed(pool)
        monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})
        async with v1_api(tmp_path, pool) as call:
            definition = preset({"roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement.",
                "profile_id": "workflow-profile"}]})
            assert (await call("workflows.put", definition))[0] == 200
            project = await projects.create_project(name="Restricted", work_dir=str(tmp_path),
                execution={"task_workspaces": "in_place"})
            await pool.execute("INSERT INTO project_allowed_profiles (project_id, profile_id) "
                "VALUES ($1, 'agent-claude-code-opus')", project["id"])
            status, answer = await call("tasks.create", {"project_id": project["id"],
                "requirement": "Build", "workflow": "reviewed"})
            assert status == 200, answer
            task_id = answer["result"]["task_id"]
            # The task's grant is immutable even if the operator replaces or deletes the preset.
            await workflows.delete("reviewed")
            for version in (2, 3):
                status, answer = await call("tasks.role_update", {"task_id": task_id, "key": "dev",
                    "harness": None, **edit, "role_prompt": edit["role_prompt"] + str(version),
                    "profile_id": "workflow-profile" if version == 2 else "Workflow profile"})
                assert status == 200, answer
                role = await task_roles.get(task_id, "dev")
                assert role["via"] == "user" and role["version"] == version and role["profile_allowed"]
                effective = await load_agent_settings("worker", project["id"], task_id, task_role=("dev", version))
                assert effective.profile.id == "workflow-profile"
            # The workflow's grant cannot authorize an unrelated profile.
            await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                "SELECT 'other-profile', 'Other profile', runtime_id, model_id FROM agent_profiles "
                "WHERE id = 'workflow-profile'")
            status, answer = await call("tasks.role_update", {"task_id": task_id, "key": "dev",
                "role_prompt": "Use another profile.", "harness": None, "profile_id": "other-profile"})
            assert status == 400, answer
            assert (await task_roles.get(task_id, "dev"))["version"] == 3
    run_db(test, monkeypatch, timeout=30)
