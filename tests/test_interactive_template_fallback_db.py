"""Interactive optional dependencies fall back; the scheduled resolver remains strict."""
from __future__ import annotations

import pytest

from looper.agents.managers_api import ManagerDefinition
from looper.agents.ops import Turn
from looper.agents.ops.service import ManagerOps, Platform, scope_for
from looper.agents.schedule_templates import effective_schedule
from looper.agents.task_templates import TemplateError
from looper.db import managers, projects, role_templates, tasks
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
OWNER = "manager-review"
PROFILE = "agent-role-profile"
CASES = ["healthy", "item", "allowlist", "capability", "missing", "duplicate", "roles_off", "malformed"]


async def selection(pool, case):
    project = await projects.create_project(name="Review", work_dir="/work", pool=pool)
    await pool.execute("UPDATE projects SET planner_roles='on' WHERE id=$1", project["id"])
    await pool.execute("INSERT INTO managers (id,name,system_prompt) VALUES ($1,'Review','Review changes.')", OWNER)
    await pool.execute("INSERT INTO agent_runtimes (id,name,type,capabilities) "
                       "SELECT 'role-runtime','role-runtime',type,capabilities FROM agent_runtimes WHERE id='rt-claude-code'")
    await pool.execute("INSERT INTO agent_profiles (id,name,runtime_id,model_id) "
                       "SELECT $1,$1,'role-runtime',model_id FROM agent_profiles WHERE id='agent-claude-code-opus'", PROFILE)
    await pool.execute("INSERT INTO library_items (id,kind,name,origin,sha256,size_bytes,file_count) VALUES "
                       "('role-skill','skill','review','ui','abc',0,0), "
                       "('retained-skill','skill','retained','ui','def',0,0)")
    await pool.execute("INSERT INTO library_installs (id,item_id,scope,roles) "
                       "VALUES ('role-install','role-skill','global',ARRAY['task_role']), "
                       "('retained-install','retained-skill','global',ARRAY['task_role'])")
    definition = {"kind": "review", "role_prompt": "Check sources.", "harness": "Review carefully.",
                  "profile_id": PROFILE, "items": ["role-skill", "retained-skill"]}
    await role_templates.put_template(project["id"], "check", definition, pool=pool)
    await managers.put_template(OWNER, "review", {"name": "Review", "project_id": project["id"],
                                "role_templates": ["check", "check"] if case == "duplicate" else ["check"]})
    if case == "item":
        await pool.execute("DELETE FROM library_installs WHERE id='role-install'")
    elif case == "allowlist":
        await projects.set_allowed_profiles(project["id"], ["agent-claude-code-opus"], pool=pool)
    elif case == "capability":
        await pool.execute("UPDATE agent_runtimes SET capabilities='{}' WHERE id='role-runtime'")
    elif case == "missing":
        await role_templates.delete_template(project["id"], "check", pool=pool)
    elif case == "roles_off":
        await pool.execute("UPDATE projects SET planner_roles='off' WHERE id=$1", project["id"])
    elif case == "malformed":
        await pool.execute("UPDATE project_role_templates SET role_prompt=' ' WHERE project_id=$1", project["id"])
    return project, {"role_key": "check", "template_key": "check", **definition}


@pytest.mark.parametrize("case", CASES)
def test_interactive_resolution(monkeypatch, case):
    async def test(pool):
        project, expected = await selection(pool, case)
        owner = await managers.get_manager(OWNER, pool=pool)
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="test",
                         scope=scope_for(ManagerDefinition.from_row(owner), ["review"]))
        turn = Turn(channel="web", chat="review", message_id="create")
        ops.begin_turn(turn, "turn")
        try:
            answer = await ops.call("looper_task_create", {"template": "review", "requirement": "Check it.",
                                    "title": "Review"}, binding="turn")
        finally:
            ops.end_turn(turn)
        if case in {"missing", "duplicate", "roles_off", "malformed"}:
            assert answer["status"] == "error", answer
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
            return
        assert answer["status"] == "ok", answer
        task = await tasks.get_task(answer["result"]["task_id"], pool=pool)
        if case == "item":
            expected["items"] = ["retained-skill"]
        elif case in {"allowlist", "capability"}:
            expected["profile_id"] = None
        assert task["template"]["roles"] == [expected]
        role = dict(await pool.fetchrow("SELECT * FROM task_roles WHERE task_id=$1", task["id"]))
        assert {key: role[key] for key in expected} == expected
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
        assert ("模板角色回退" in turn.blocks[0]) == (case != "healthy")
        if case in {"allowlist", "capability"}:
            assert "Reviewer" in turn.blocks[0]
        assert await pool.fetchval("SELECT count(*) FROM task_profile_overrides") == 0
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("case", CASES)
def test_scheduled_resolution_stays_strict(monkeypatch, case):
    async def test(pool):
        project, expected = await selection(pool, case)
        schedule = {"manager_id": OWNER, "project_id": project["id"], "template_key": "review",
                    "template_inputs": {}, "profile_overrides": {}, "requirement": "Check it."}
        if case == "healthy":
            assert (await effective_schedule(pool, schedule)).roles == [expected]
        else:
            with pytest.raises(TemplateError):
                await effective_schedule(pool, schedule)
        for table in ("tasks", "task_roles", "task_profile_overrides", "task_planner_profiles"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0
    run_db(test, monkeypatch, timeout=30)
