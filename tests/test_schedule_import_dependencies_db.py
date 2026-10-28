"""Schedule imports validate the same final role/profile dependencies as execution."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from looper.agents import config_transfer as transfer
from looper.agents.library import item_sha256
from looper.db import config_transfer as db_transfer
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _document(pool):
    async with pool.acquire() as conn:
        profile = next(row for row in await db_transfer.read_profiles(conn)
                       if row["id"] == "agent-claude-code-opus")
    content = "---\nname: inspect\ndescription: Inspect changes\n---\nInspect the changes.\n"
    files = [{"path": "SKILL.md", "content": content, "executable": False,
              "sha256": hashlib.sha256(content.encode()).hexdigest()}]
    item = {"kind": "skill", "name": "inspect", "origin": "upload", "source": "",
            "files": files, "sha256": item_sha256(files)}
    return {"format": transfer.V4_FORMAT, "profiles": [{**transfer.profile_entry(profile), "name": "Role profile"}],
            "catalog": [item], "projects": [{"name": "Example", "execution": {"planner_roles": "on"},
                "installs": [{"item": {"kind": "skill", "name": "inspect"}, "roles": ["task_role"]}],
                "role_templates": [{"key": "inspect", "kind": "work", "role_prompt": "Inspect carefully.",
                                    "profile": "Role profile", "items": [{"kind": "skill", "name": "inspect"}]}],
                "schedules": [{"name": "Nightly", "cron": "0 9 * * *", "timezone": "UTC",
                               "requirement": "Inspect changes.", "manager": "manager-review", "template_key": "review"}]}],
            "managers": [{"id": "manager-review", "name": "Review", "system_prompt": "Review changes.",
                          "enabled": False, "templates": [{"key": "review", "name": "Review", "project": "Example",
                              "requirement": "Inspect changes.", "role_templates": ["inspect"]}]}]}


async def _run(pool, document, *, apply=True):
    async with pool.acquire() as conn:
        return await transfer.run_import(conn, document, apply=apply)


def _schedule(plan):
    return next(change for change in plan.changes if change.section == "project.schedules")


async def _state(pool):
    return {table: list(await pool.fetch(f"SELECT * FROM {table} ORDER BY 1")) for table in
            ("projects", "agent_profiles", "library_items", "library_installs", "schedules", "schedule_runs", "tasks")}


@pytest.mark.parametrize("stale,reason", [("allowlist", "project's list"), ("fixed_allowlist", "project's list"),
                                          ("override_allowlist", "project's list"), ("item", "installed"),
                                          ("roles_off", "roles off"), ("capability", "cannot run")])
def test_retained_template_import_rejects_dependencies_without_changing_schedule(monkeypatch, stale, reason):
    async def test(pool):
        document = await _document(pool)
        assert _schedule(await _run(pool, document)).action is transfer.Action.ADDED
        project = await pool.fetchval("SELECT id FROM projects WHERE name='Example'")
        from looper.db.tasks import create_task
        await create_task(task_id="task-retained", project_id=project, requirement="Earlier inspection.",
                          status="completed", schedule_id=await pool.fetchval("SELECT id FROM schedules"), pool=pool)
        await pool.execute("UPDATE schedules SET enabled=true, planner_session_id='session-retained'")
        await pool.execute("INSERT INTO schedule_runs (id,schedule_id,task_id,scheduled_for,trigger,status) "
                           "SELECT 'srun-retained',id,'task-retained',now(),'manual','completed' FROM schedules")
        incoming = copy.deepcopy(document)
        incoming.pop("managers")  # Its retained template still needs strict dependency validation.
        incoming.pop("profiles")
        incoming.pop("catalog")
        entry = incoming["projects"][0]
        entry.pop("installs")
        entry.pop("role_templates")
        entry["schedules"][0]["requirement"] = "Inspect the latest changes."
        if stale.endswith("allowlist"):
            await pool.execute("INSERT INTO project_allowed_profiles (project_id,profile_id) "
                               "VALUES ($1,'agent-claude-code-opus')", project)
            if stale != "allowlist":
                await pool.execute("UPDATE project_role_templates SET profile_id='agent-claude-code-opus'")
            if stale == "fixed_allowlist":
                await pool.execute("UPDATE manager_task_templates SET worker_profile_id="
                                   "(SELECT id FROM agent_profiles WHERE name='Role profile')")
            elif stale == "override_allowlist":
                entry["schedules"][0]["profiles"] = {"worker": "Role profile"}
        elif stale == "item":
            await pool.execute("UPDATE library_installs SET enabled=false")
        elif stale == "roles_off":
            entry["execution"]["planner_roles"] = "off"
        else:
            await pool.execute("UPDATE agent_runtimes SET capabilities=ARRAY['resident_session','resume'] "
                               "WHERE id=(SELECT runtime_id FROM agent_profiles WHERE name='Role profile')")
        before = await _state(pool)
        preview = await _run(pool, incoming, apply=False)
        assert _schedule(preview).action is transfer.Action.SKIPPED
        assert reason in _schedule(preview).reason
        assert await _state(pool) == before
        applied = await _run(pool, incoming)
        assert applied.view() == {**preview.view(), "applied": True}
        after = await _state(pool)
        assert all(after[table] == before[table] for table in before if table != "projects")
        assert await pool.fetchval("SELECT enabled AND planner_session_id='session-retained' FROM schedules")
        if stale == "roles_off":
            assert await pool.fetchval("SELECT planner_roles FROM projects WHERE id=$1", project) == "off"
    run_db(test, monkeypatch, timeout=30)


def test_same_file_project_role_profile_item_install_preview_apply_and_export(monkeypatch):
    from looper.db import projects
    original = projects.allowed_profiles

    async def stored_profiles(project_id, **kwargs):
        assert not project_id.startswith("new:")
        return await original(project_id, **kwargs)

    monkeypatch.setattr(projects, "allowed_profiles", stored_profiles)
    async def test(pool):
        document = await _document(pool)
        before = await _state(pool)
        preview = await _run(pool, document, apply=False)
        assert _schedule(preview).action is transfer.Action.ADDED
        assert await _state(pool) == before
        applied = await _run(pool, document)
        assert applied.view() == {**preview.view(), "applied": True}
        resolved = await pool.fetchrow("SELECT profile_id,items FROM project_role_templates")
        assert resolved["profile_id"] == await pool.fetchval("SELECT id FROM agent_profiles WHERE name='Role profile'")
        assert resolved["items"] == [await pool.fetchval("SELECT id FROM library_items WHERE name='inspect'")]
        async with pool.acquire() as conn:
            exported = json.loads("".join([chunk async for chunk in transfer.export_chunks(
                conn, include_projects=True, include_secrets=False)]))
        role = exported["projects"][0]["role_templates"][0]
        assert role["profile"] == "Role profile" and role["items"] == [{"kind": "skill", "name": "inspect"}]
        assert exported["projects"][0]["schedules"][0]["template_key"] == "review"
        assert not await pool.fetchval("SELECT enabled FROM schedules")
        assert _schedule(await _run(pool, exported, apply=False)).action is transfer.Action.UNCHANGED
    run_db(test, monkeypatch, timeout=30)
