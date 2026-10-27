"""Static schedule template execution and gate consistency on disposable PostgreSQL."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from looper.agents import managers_api, schedules
from looper.db import managers, tasks, role_templates, schedules as db_schedules
from tests.db_support import DSN
from tests.schedule_support import run_scheduled

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
OWNER = "manager-example"
PROFILE = "agent-claude-code-opus"
INPUT = {"key": "url", "label": "Source URL", "kind": "url", "required": True, "hint": "https://example.org"}


async def setup(env, **fields):
    await env.pool.execute("INSERT INTO agent_profiles (id,name,runtime_id,model_id) "
                           "SELECT 'agent-example-other','Example other',runtime_id,model_id FROM agent_profiles WHERE id=$1", PROFILE)
    await env.pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ($1, 'Example', 'Handle example tasks.')", OWNER)
    await managers.put_template(OWNER, "report", {"name": "Report", "project_id": env.project_id,
        "requirement": "Inspect {{url}}.", "inputs": [INPUT], "planner_instructions": "Keep a concise report.", **fields})


async def create(env, **fields):
    return await env.schedule(manager_id=OWNER, template_key="report", template_inputs={"url": "https://example.org"}, **fields)


async def fire(env, schedule):
    rid = await env.scheduler.run_now(schedule["schedule_id"])
    await env.scheduler.idle()
    return await db_schedules.get_run(rid)


def test_atomic_snapshot_roles_profiles_edit_and_start_over(monkeypatch, tmp_path):
    async def test(env):
        await env.pool.execute("UPDATE projects SET planner_roles = 'on' WHERE id = $1", env.project_id)
        await role_templates.put_template(env.project_id, "check", {"kind": "review", "role_prompt": "Check sources."}, pool=env.pool)
        await setup(env, role_templates=["check"], reviewer_profile_id=PROFILE)
        schedule = await create(env, task_limits={"worker": 2})
        assert schedule["template_name"] == "Report"
        first = await fire(env, schedule)
        assert first["status"] == "running", first
        task = await tasks.get_task(first["task_id"])
        assert task["manager_id"] == OWNER and task["workspace_mode"] == "in_place"
        assert task["requirement"].startswith("Inspect https://example.org.\n\nLook at the new alerts.")
        snapshot = task["template"]
        assert snapshot["name"] == "Report" and snapshot["planner_instructions"] == "Keep a concise report."
        assert snapshot["inputs"] == [{**INPUT, "pattern": "", "value": "https://example.org"}]
        assert snapshot["roles"][0]["role_prompt"] == "Check sources."
        assert await env.pool.fetchval("SELECT profile_id FROM task_profile_overrides WHERE task_id = $1", task["id"]) == PROFILE
        assert await env.pool.fetchval("SELECT max_concurrency FROM task_concurrency_limits WHERE task_id = $1", task["id"]) == 2
        assert tuple(await env.pool.fetchrow("SELECT version, via FROM task_roles WHERE task_id=$1", task["id"])) == (1, "template")
        await env.end_task(task["id"], "completed")
        await managers.put_template(OWNER, "report", {"name": "New report", "planner_instructions": "New instructions."})
        second = await fire(env, schedule)
        assert (await tasks.get_task(second["task_id"]))["template"]["name"] == "New report"
        assert (await tasks.get_task(task["id"]))["template"] == snapshot
        await env.pool.execute("UPDATE task_roles SET role_prompt='Changed' WHERE task_id=$1", task["id"])
        await tasks.reset_task_for_restart(task["id"], pool=env.pool)
        assert await env.pool.fetchval("SELECT role_prompt FROM task_roles WHERE task_id=$1", task["id"]) == "Check sources."
        with pytest.raises(schedules.RequestError, match="used by schedule") as exc:
            await managers_api.delete_template(OWNER, "report")
        assert exc.value.status == schedules.HTTP_409
        assert (await db_schedules.get_schedule(schedule["schedule_id"]))["template_key"] == "report"
        assert await managers.get_template(OWNER, "report") is not None
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("inputs,reason", [({}, "needs Source URL"), ({"other": "x"}, "no input"),
    ({"url": "file:///tmp/x"}, "http or https"), ({"url": "x" * 2001}, "at most 2000"),
    ({"url": 1}, "static strings")])
def test_invalid_inputs_once(monkeypatch, tmp_path, inputs, reason):
    async def test(env):
        await setup(env)
        with pytest.raises(schedules.RequestError, match=reason):
            await schedules.create_schedule(env.project_id, {"name": "Example", "cron": "0 9 * * *",
                "requirement": "Report", "manager_id": OWNER, "template_key": "report", "template_inputs": inputs})
        assert await env.pool.fetchval("SELECT count(*) FROM schedules") == 0
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("pattern,value,reason", [(r"https://example.org/[0-9]+", "https://example.org/a", "must look like"),
    ("(a+)+$", "a" * 64 + "!", "took too long")])
def test_regex_subprocess_boundary(monkeypatch, tmp_path, pattern, value, reason):
    async def test(env):
        await setup(env, inputs=[{"key": "url", "label": "Source", "pattern": pattern}])
        with pytest.raises(schedules.RequestError, match=reason):
            await schedules.create_schedule(env.project_id, {"name": "Report", "cron": "0 9 * * *",
                "requirement": "Report", "manager_id": OWNER, "template_key": "report", "template_inputs": {"url": value}})
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("change,reason", [("project", "different project"), ("owner", "no template"),
    ("directory", "sets work_dir"), ("profile", "Agent Profile"), ("roles", "roles off"),
    ("allowlist", "project's list"), ("items", "installed")])
def test_fixed_settings_and_mutable_dependencies(monkeypatch, tmp_path, change, reason):
    async def test(env):
        await setup(env)
        row = {"manager_id": OWNER, "template_key": "report", "template_inputs": {"url": "https://example.org"},
               "name": "Report", "cron": "0 9 * * *", "requirement": "Report"}
        if change == "project":
            from looper.db import projects
            other = await projects.create_project(name="Other", work_dir=env.work_dir, pool=env.pool)
            await managers.put_template(OWNER, "report", {"name": "Report", "project_id": other["id"]})
        elif change == "owner":
            row["manager_id"] = "manager"
        elif change == "directory":
            row["work_dir"] = "/other"
            await managers.put_template(OWNER, "report", {"name": "Report", "work_dir": env.work_dir})
        elif change == "profile":
            row["profile_overrides"] = {"reviewer": "agent-example-other"}
            await managers.put_template(OWNER, "report", {"name": "Report", "reviewer_profile_id": PROFILE})
        elif change == "allowlist":
            await env.pool.execute("INSERT INTO project_allowed_profiles (project_id,profile_id) VALUES ($1, 'agent-example-other')", env.project_id)
            await managers.put_template(OWNER, "report", {"name": "Report", "reviewer_profile_id": PROFILE})
        else:
            await role_templates.put_template(env.project_id, "check", {"kind": "review", "role_prompt": "Check."}, pool=env.pool)
            await managers.put_template(OWNER, "report", {"name": "Report", "role_templates": ["check"]})
            if change == "items":
                await env.pool.execute("UPDATE projects SET planner_roles='on' WHERE id=$1", env.project_id)
                await env.pool.execute("UPDATE project_role_templates SET items='{missing}'")
        with pytest.raises(schedules.RequestError, match=reason):
            await schedules.create_schedule(env.project_id, row)
        assert await env.pool.fetchval("SELECT count(*) FROM schedules") == 0
    run_scheduled(test, monkeypatch, tmp_path)


def test_policy_clear_and_patch_validation(monkeypatch, tmp_path):
    async def test(env):
        await setup(env)
        await env.pool.execute("UPDATE managers SET task_policy='templates_only' WHERE id=$1", OWNER)
        with pytest.raises(schedules.RequestError, match="requires a template"):
            await schedules.create_schedule(env.project_id, {"name": "No template", "cron": "0 9 * * *", "requirement": "Report", "manager_id": OWNER})
        schedule = await create(env)
        assert (await schedules.update_schedule(schedule["schedule_id"], {"enabled": True}))["schedule"]["enabled"]
        with pytest.raises(schedules.RequestError, match="requires a template"):
            await schedules.update_schedule(schedule["schedule_id"], {"template_key": None})
        await env.pool.execute("UPDATE managers SET task_policy='free' WHERE id=$1", OWNER)
        cleared = (await schedules.update_schedule(schedule["schedule_id"], {"template_key": None}))["schedule"]
        assert cleared["template_key"] is None and cleared["template_inputs"] == {}
        with pytest.raises(schedules.RequestError, match="go with"):
            await schedules.update_schedule(schedule["schedule_id"], {"template_inputs": {}})
    run_scheduled(test, monkeypatch, tmp_path)


def test_changed_dependency_and_insert_failure_leave_zero_task_writes(monkeypatch, tmp_path):
    async def test(env):
        await setup(env)
        schedule = await create(env)
        await managers.put_template(OWNER, "report", {"name": "Report", "inputs": [{"key": "new", "label": "New"}]})
        run = await fire(env, schedule)
        assert run["status"] == "failed" and "no input" in run["error"]
        await managers.put_template(OWNER, "report", {"name": "Report", "inputs": [INPUT]})
        from looper.db import agent_setting_layers
        monkeypatch.setattr(agent_setting_layers, "set_task_limits", AsyncMock(side_effect=ValueError("insert refused")))
        await schedules.update_schedule(schedule["schedule_id"], {"task_limits": {"worker": 2}})
        run = await fire(env, schedule)
        assert run["status"] == "failed" and "insert refused" in run["error"]
        for table in ("tasks", "task_roles", "task_profile_overrides", "task_concurrency_limits"):
            assert await env.pool.fetchval(f"SELECT count(*) FROM {table}") == 0
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("mutate", [False, True])
def test_actual_gate_uses_template_directory_and_rejects_changed_configuration(monkeypatch, tmp_path, mutate):
    async def test(env):
        fixed = tmp_path / "fixed"
        fixed.mkdir()
        await setup(env, work_dir=str(fixed))
        await env.pool.execute("UPDATE projects SET work_dir='' WHERE id=$1", env.project_id)
        schedule = await create(env, gate_command="pwd; touch started; while [ ! -f release ]; do sleep 0.02; done", gate_timeout_seconds=5)
        rid = await env.scheduler.run_now(schedule["schedule_id"])
        from tests.db_support import until
        await until(lambda: (fixed / "started").exists(), timeout=5)
        if mutate:
            await managers.put_template(OWNER, "report", {"name": "Report", "planner_instructions": "Changed."})
        (fixed / "release").touch()
        await env.scheduler.idle()
        run = await db_schedules.get_run(rid)
        if mutate:
            assert run["status"] == "failed" and "changed" in run["error"]
            assert await env.pool.fetchval("SELECT count(*) FROM tasks") == 0
            assert run["gate_output"] is None
        else:
            assert run["status"] == "running", run
            task = await tasks.get_task(run["task_id"])
            assert task["work_dir"] == str(fixed) and str(fixed) in task["requirement"]
    run_scheduled(test, monkeypatch, tmp_path)


def test_render_once_context_headroom_and_mode_fallback(monkeypatch, tmp_path):
    async def test(env):
        await setup(env, requirement="Input {{url}}", inputs=[{"key": "url", "label": "Input"}])
        await env.pool.execute("UPDATE projects SET task_workspaces='worktree' WHERE id=$1", env.project_id)
        schedule = await env.schedule(manager_id=OWNER, template_key="report", template_inputs={"url": "{{url}}"}, requirement="r" * (schedules.REQUIREMENT_MAX_CHARS - 17))
        run = await fire(env, schedule)
        task = await tasks.get_task(run["task_id"])
        assert task["requirement"].startswith("Input {{url}}") and task["workspace_mode"] == "worktree"
        from looper.agents.scheduler import run_requirement, Firing, Trigger, Carried
        from looper.agents.system_prompt import TASK_REQUIREMENT_LIMIT
        from looper.agents.task_templates import render_requirement, TaskTemplate
        base = render_requirement(TaskTemplate.from_row(await managers.get_template(OWNER, "report")),
                                  {"url": "{{url}}"}, schedule["requirement"])
        assert 0 <= schedules.REQUIREMENT_MAX_CHARS - len(base) < 100
        text = run_requirement({**schedule, "requirement": base},
            Firing(Trigger.MANUAL, env.clock()), [], description="Daily", gate_output="g" * 20000,
            carried=[Carried("old", "failed", env.clock(), "c" * 20000)])
        assert len(text) <= TASK_REQUIREMENT_LIMIT and "gggg" in text and "cccc" in text
        with pytest.raises(schedules.RequestError, match="rendered"):
            await schedules.update_schedule(schedule["schedule_id"], {"requirement": "r" * schedules.REQUIREMENT_MAX_CHARS})
        await env.end_task(task["id"], "completed")
        await managers.put_template(OWNER, "report", {"name": "Report", "workspace_mode": "in_place"})
        next_run = await fire(env, schedule)
        assert (await tasks.get_task(next_run["task_id"]))["workspace_mode"] == "in_place"
        assert (await tasks.get_task(task["id"]))["workspace_mode"] == "worktree"
    run_scheduled(test, monkeypatch, tmp_path)


def test_manager_export_contract_includes_v4_templates_and_omitted_bots(monkeypatch, tmp_path):
    async def test(env):
        from types import SimpleNamespace
        from looper.agents.ops.config import _config_export, EXPORT_NOTE, TOOLS
        await setup(env)
        result = await _config_export(SimpleNamespace(platform=SimpleNamespace(runtime_root=tmp_path),
                                                      arguments={"include_projects": True}))
        assert result["format"] == "looper-config/v4" and result["bots_left_out"] == 0
        import json
        document = json.loads(Path(result["path"]).read_text())
        assert next(m for m in document["managers"] if m["id"] == OWNER)["templates"][0]["key"] == "report"
        tool = next(t for t in TOOLS if t.name == "looper_config_export")
        for text in (EXPORT_NOTE, tool.description):
            assert "looper-config/v4" in text and "Manager definitions and task templates" in text
    from pathlib import Path
    run_scheduled(test, monkeypatch, tmp_path)


def test_final_gate_recheck_rejects_a_dependency_changed_after_first_comparison(monkeypatch, tmp_path):
    async def test(env):
        from looper.agents import schedule_templates
        await env.pool.execute("UPDATE projects SET planner_roles='on' WHERE id=$1", env.project_id)
        await role_templates.put_template(env.project_id, "check", {"kind": "review", "role_prompt": "Check."}, pool=env.pool)
        await setup(env, role_templates=["check"])
        schedule = await create(env, gate_command="printf 'public input'")
        original = schedule_templates.effective_schedule
        reads = 0
        async def change_before_final_read(conn, row, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 4:  # launch, gate start, completion comparison, final task read
                await conn.execute("UPDATE project_role_templates SET role_prompt='New contract' WHERE project_id=$1", env.project_id)
            return await original(conn, row, **kwargs)
        monkeypatch.setattr(schedule_templates, "effective_schedule", change_before_final_read)
        run = await fire(env, schedule)
        assert reads == 4 and run["status"] == "failed" and "changed" in run["error"]
        assert run["task_id"] is None and run["gate_output"] is None
        assert await env.pool.fetchval("SELECT count(*) FROM tasks") == 0
    run_scheduled(test, monkeypatch, tmp_path)


def test_no_template_uses_the_original_requirement_profiles_and_workspace(monkeypatch, tmp_path):
    async def test(env):
        from looper.agents.scheduler import run_requirement, Firing, Trigger
        from looper.agents import cron
        from looper.agents.project_execution import ProjectExecution
        from looper.db import projects
        schedule = await env.schedule()
        run = await fire(env, schedule)
        task = await tasks.get_task(run["task_id"])
        expected = run_requirement(schedule, Firing(Trigger.MANUAL, run["scheduled_for"]), [],
                                   description=cron.describe(cron.parse(schedule["cron"]), schedule["timezone"]))
        assert task["requirement"] == expected and task["template"] is None
        assert task["manager_id"] == "manager" and task["work_dir"] == env.work_dir
        assert task["workspace_mode"] == ProjectExecution.from_row(await projects.get_project(env.project_id)).task_mode.value
        assert await env.pool.fetchval("SELECT count(*) FROM task_profile_overrides") == 0
        assert await env.pool.fetchval("SELECT count(*) FROM task_roles") == 0
    run_scheduled(test, monkeypatch, tmp_path)
