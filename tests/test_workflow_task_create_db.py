"""Task creation snapshots workflow execution choices atomically at the API entry point."""
from types import SimpleNamespace

import pytest

from looper.agents import api_v1
from looper.agents.ops.planner import PlannerOps
from looper.agents.projects import RequestError, create_task_core
from looper.agents.project_execution import ProjectExecution
from looper.db import agent_setting_layers, projects, task_roles, tasks
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

PRESET = {"key": "reviewed", "name": "Reviewed", "planner_instructions": "Keep changes focused.",
          "roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement the change.",
                     "profile_id": "workflow-profile"}], "work_role": "dev",
          "profiles": {"worker": "workflow-profile"}, "budget_usd": 2.5,
          "max_step_retries": 1, "review_all": True, "dependency_conditions": True,
          "escalate_on_exhaustion": True}


@pytest.mark.parametrize("profile_source", ["role", "workflow", "selection"])
def test_confirmation_and_form_preview_share_workflow_roles_panel_and_verification(
    tmp_path, monkeypatch, profile_source,
):
    from looper.agents.agent_settings import Layer, load_agent_settings
    from tests.test_ops_tasks_db import _Chat

    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            preset = {**PRESET, "profiles": {}, "roles": [
                {"key": key, "kind": kind, "role_prompt": "Check the change.", "profile_id": profile}
                for key, kind, profile in (
                    ("dev", "work", "workflow-profile"),
                    ("review_codex", "review", "codex-review-profile"),
                    ("review_grok", "review", "grok-review-profile"),
                    ("suggest", "review", "suggestion-profile"),
                    ("verify", "work", "agent-claude-code-opus"))],
                "review_panel": {"members": ["review_codex", "review_grok", "suggest"],
                                 "accept_when": {"all_of": ["review_codex", "review_grok"]}},
                "distinct_reviewer_runtime": True, "verification": {"role": "verify", "max_rounds": 2}}
            await pool.execute("INSERT INTO agent_runtimes (id, name, type, capabilities) "
                               "SELECT 'rt-codex', 'Codex', 'codex', capabilities FROM agent_runtimes "
                               "WHERE id = 'rt-claude-code'")
            await pool.executemany("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                                  "SELECT $1, $2, 'rt-codex', id "
                                  "FROM llm_models WHERE provider_id = 'prov-codex-login' LIMIT 1", [
                                      ("codex-review-profile", "Codex review"),
                                      ("grok-review-profile", "Second review"),
                                      ("suggestion-profile", "Suggestions")])
            await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id, fallback_profile_id) "
                               "SELECT 'backup-profile', 'Backup profile', runtime_id, model_id, "
                               "'agent-claude-code-opus' FROM agent_profiles WHERE id = 'workflow-profile'")
            await pool.execute("UPDATE agent_profiles SET fallback_profile_id = 'backup-profile' "
                               "WHERE id = 'workflow-profile'")
            selected = {"worker": "agent-claude-code-opus"}
            if profile_source == "workflow":
                preset["profiles"] = selected
            choices = {"profiles": selected} if profile_source == "selection" else {}
            assert (await call("workflows.put", preset))[0] == 200
            status, preview = await call("workflows.preview", {
                "key": "reviewed", "project_id": project["id"], **choices})
            assert status == 200, preview
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/workflows/preview").result, preview["result"])

            chat = _Chat(tmp_path)
            (answer,) = await chat.say("Create it", ("looper_task_create", {
                "requirement": "Build", "title": "Build", "project": "Example", "workflow": "reviewed", **choices}))
            assert answer["status"] == "ok", answer
            effective = await load_agent_settings("worker", project["id"], answer["result"]["task_id"],
                                                  task_role=("dev", 1))
            assert effective.error is None
            assert effective.profile.id == ("workflow-profile" if profile_source == "role"
                                             else selected["worker"])
            assert effective.profile_source is (Layer.TASK_ROLE if profile_source == "role" else Layer.TASK)
            (block,) = chat.turns[0].blocks
            lines = block.splitlines()
            if profile_source == "role":
                assert any(line.startswith("- dev：Workflow profile（claude-code/sonnet）")
                           and "回落至 Backup profile（claude-code/sonnet），回落至 claude-code/opus" in line
                           for line in lines)
            else:
                assert "- dev：claude-code/opus（claude-code/opus）" in lines
            for key, name in (("review_codex", "Codex review"), ("review_grok", "Second review"),
                              ("suggest", "Suggestions")):
                assert any(line.startswith(f"- {key}：{name}（codex/") for line in lines)
            assert any(line.startswith("- verify：claude-code/opus（claude-code/opus）") and "只读" in line
                       for line in lines)
            for line in preview["result"]["lines"]:
                assert line in block
            assert any("必需席位：review_codex、review_grok" in line and "建议席位：suggest" in line
                       and "全部通过" in line for line in lines)
            assert "- Worker：" not in block and "- Reviewer：" not in block
            assert "每一步都要 Reviewer 评审通过" not in block
            assert len(lines) <= 12
            row = await tasks.get_task(answer["result"]["task_id"])
            assert row["workflow"]["review_panel"]["members"] == ["review_codex", "review_grok", "suggest"]
    run_db(test, monkeypatch, timeout=30)


def test_without_workflow_confirmation_retains_existing_role_and_review_lines(tmp_path, monkeypatch):
    from tests.test_ops_tasks_db import _Chat

    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            await setup(pool, call, tmp_path)
            chat = _Chat(tmp_path)
            (answer,) = await chat.say("Create it", ("looper_task_create", {
                "requirement": "Build", "title": "Build", "project": "Example", "workflow": "none"}))
            assert answer["status"] == "ok", answer
            (block,) = chat.turns[0].blocks
            assert "- Worker：claude-code/opus（effort 默认，来源：内置默认），agent teams 开" in block
            assert "- Reviewer：claude-code/opus（effort 默认，来源：内置默认），agent teams 开" in block
            assert "- 评审：由 Planner 决定哪些步骤要 Reviewer 评审" in block
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("source", ["project", "selection", "workflow"])
def test_preview_resolves_inherited_profiles_and_refuses_invalid_selections(tmp_path, monkeypatch, source):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            preset = {**PRESET, "profiles": {"worker": "workflow-profile"} if source == "workflow" else {},
                      "roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement."}]}
            assert (await call("workflows.put", preset))[0] == 200
            await agent_setting_layers.put_layer("role_project", "worker", project["id"],
                                                {"profile_id": "workflow-profile"})
            body = {"key": "reviewed", "project_id": project["id"]}
            if source == "selection":
                body["profiles"] = {"worker": "agent-claude-code-opus"}
            status, answer = await call("workflows.preview", body)
            assert status == 200, answer
            model = "opus" if source == "selection" else "sonnet"
            assert any(line.startswith("- dev：") and f"claude-code/{model}" in line
                       for line in answer["result"]["lines"])
            assert (await call("workflows.preview", {**body, "project_id": "missing"}))[0] == 404
            assert (await call("workflows.preview", {**body, "key": "missing"}))[0] == 404
            assert (await call("workflows.preview", {**body, "profiles": {"worker": "missing"}}))[0] == 400
            if source == "workflow":
                assert (await call("workflows.preview", {
                    **body, "profiles": {"worker": "agent-claude-code-opus"}}))[0] == 400
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
    run_db(test, monkeypatch, timeout=30)


async def setup(pool, call, tmp_path):
    await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                       "SELECT 'workflow-profile', 'Workflow profile', 'rt-claude-code', id "
                       "FROM llm_models WHERE provider_id = 'prov-claude-subscription' AND name = 'sonnet'")
    status, answer = await call("workflows.put", PRESET)
    assert status == 200, answer
    project = await projects.create_project(name="Example", work_dir=str(tmp_path),
                                            execution={"task_workspaces": "in_place"})
    await pool.execute("UPDATE projects SET default_workflow = 'reviewed' WHERE id = $1", project["id"])
    return project


def test_task_creation_preserves_workflow_roles_profiles_budget_and_effective_execution(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            status, answer = await call("tasks.create", {"project_id": project["id"], "requirement": "Build",
                                                        "workflow": "reviewed"})
            assert status == 200, answer
            task_id = answer["result"]["task_id"]
            row = await tasks.get_task(task_id)
            assert row["workflow"]["key"] == "reviewed" and row["budget_usd"] == 2.5
            role = (await task_roles.current(task_id))[0]
            assert (role["role_key"], role["via"], role["version"], role["profile_id"]) == (
                "dev", "workflow", 1, "workflow-profile")
            assert (await agent_setting_layers.profile_overrides(task_id))["worker"]["id"] == "workflow-profile"
            current = await projects.get_task_project(task_id)
            execution = ProjectExecution.from_row(current).overlaid(current["task_workflow"])
            assert execution.step_retries == 1 and execution.review_all and execution.conditions_on
            assert execution.gates_on and execution.task_budget_usd == 2.5
            # Replacement changes future selections, and start_over restores the original role version.
            assert (await call("workflows.put", {"key": "reviewed", "name": "Updated"}))[0] == 200
            await pool.execute("UPDATE tasks SET status = 'failed' WHERE id = $1", task_id)
            assert await tasks.reset_task_for_restart(task_id, keep_steps=False)
            assert (await task_roles.current(task_id))[0]["role_prompt"] == "Implement the change."
            assert (await tasks.get_task(task_id))["workflow"] == row["workflow"]
            status, answer = await call("tasks.detail", {"task_id": task_id})
            assert status == 200 and answer["result"]["workflow"] == row["workflow"], answer
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/tasks/detail").result, answer["result"])
    run_db(test, monkeypatch, timeout=45)


@pytest.mark.parametrize("selection,expected", [(None, "reviewed"), ("none", None)])
def test_project_default_is_used_unless_explicitly_disabled(tmp_path, monkeypatch, selection, expected):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            body = {"project_id": project["id"], "requirement": "Build"}
            if selection is not None:
                body["workflow"] = selection
            status, answer = await call("tasks.create", body)
            assert status == 200, answer
            snapshot = (await tasks.get_task(answer["result"]["task_id"]))["workflow"]
            assert (snapshot or {}).get("key") == expected
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("selection,expected", [
    ("alternate", "alternate"), (None, "reviewed"), ("none", None), ("missing", None),
])
def test_client_task_tool_applies_workflow_selection(tmp_path, monkeypatch, selection, expected):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            assert (await call("workflows.put", {**PRESET, "key": "alternate", "name": "Alternate"}))[0] == 200
            body = {"project": project["name"], "title": "Build", "requirement": "Build"}
            if selection is not None:
                body["workflow"] = selection
            status, answer = await call("/api/v1/ops/task_create", body, client=True)
            if selection == "missing":
                assert status == 400 and answer["error"]["code"] == "invalid", answer
                assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
                return
            assert status == 200, answer
            snapshot = (await tasks.get_task(answer["result"]["task_id"]))["workflow"]
            assert (snapshot or {}).get("key") == expected
    run_db(test, monkeypatch, timeout=30)


def test_conflicting_profile_is_invalid_and_creates_nothing(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
                               "SELECT 'other-profile', 'Other', runtime_id, model_id FROM agent_profiles "
                               "WHERE id = 'workflow-profile'")
            status, answer = await call("tasks.create", {"project_id": project["id"], "requirement": "Build",
                "profile_overrides": {"worker": "other-profile"}})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
    run_db(test, monkeypatch, timeout=30)


def test_template_excludes_workflow_selection_and_project_default(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            data = {"project_id": project["id"], "requirement": "Build"}
            with pytest.raises(RequestError, match="workflow and template"):
                await create_task_core(data, workflow="none", task_fields={"template": {"key": "report"}})
            result = await create_task_core(data, task_fields={"template": {"key": "report"}})
            assert (await tasks.get_task(result["task_id"]))["workflow"] is None
    run_db(test, monkeypatch, timeout=30)


def test_planner_cannot_replace_workflow_role(tmp_path, monkeypatch):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            status, answer = await call("tasks.create", {"project_id": project["id"], "requirement": "Build"})
            assert status == 200, answer
            task_id = answer["result"]["task_id"]
            await pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", task_id)
            await pool.execute("UPDATE projects SET planner_roles = 'on' WHERE id = $1", project["id"])
            ops = PlannerOps(SimpleNamespace(), url="http://127.0.0.1:9", dashboard_url="http://looper.test")
            token = ops.issue(task_id)
            ops.begin_turn(token)
            answer = await ops.call(f"Bearer {token}", "looper_task_role_set", {
                "id": "dev", "kind": "work", "role_prompt": "Replace the role."})
            assert answer["status"] == "error" and answer["error"]["code"] == "forbidden", answer
            assert len(await task_roles.versions(task_id)) == 1
    run_db(test, monkeypatch, timeout=30)


def test_fixed_workflow_role_profile_runs_when_project_restricts_planner_choices(tmp_path, monkeypatch):
    from looper.agents.agent_settings import load_agent_settings

    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            assert (await call("workflows.put", {**PRESET, "profiles": {}}))[0] == 200
            await pool.execute("INSERT INTO project_allowed_profiles (project_id, profile_id) "
                               "VALUES ($1, 'agent-claude-code-opus')", project["id"])
            status, answer = await call("tasks.create", {"project_id": project["id"], "requirement": "Build"})
            assert status == 200, answer
            task_id = answer["result"]["task_id"]
            settings = await load_agent_settings("worker", project["id"], task_id, task_role=("dev", 1))
            assert settings.profile.id == "workflow-profile"
            assert (await task_roles.current(task_id))[0]["profile_allowed"]
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("selection", [None, "reviewed", "none"], ids=["default", "explicit", "disabled"])
@pytest.mark.parametrize("overrides", [True, False], ids=["workflow-caps", "project-caps"])
def test_manager_start_confirmation_shows_effective_workflow_controls(tmp_path, monkeypatch, selection, overrides):
    from tests.test_ops_tasks_db import _Chat

    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            preset = {**PRESET, "review_all": overrides, "dependency_conditions": overrides,
                      "budget_usd": 0.0001 if overrides else None,
                      "max_step_retries": 0 if overrides else None}
            assert (await call("workflows.put", preset))[0] == 200
            await projects.update_project(project["id"], execution={
                "review_policy": "planner" if overrides else "all", "task_budget_usd": 12,
                "max_step_retries": 4, "dependency_conditions": "off" if overrides else "on",
                "task_budget_action": "wrap_up"})
            body = {"requirement": "Build", "title": "Build", "project": "Example"}
            if selection is not None:
                body["workflow"] = selection
            chat = _Chat(tmp_path)
            (answer,) = await chat.say("Create it", ("looper_task_create", body))
            assert answer["status"] == "ok", answer
            (block,) = chat.turns[0].blocks
            row = await tasks.get_task(answer["result"]["task_id"])
            effective = ProjectExecution.from_row(await projects.get_task_project(row["id"])).overlaid(row["workflow"])
            assert ("- 评审：每一步都要 Reviewer 评审通过" if effective.review_all else
                    "- 评审：由 Planner 决定哪些步骤要 Reviewer 评审") in block
            from looper.agents.project_execution import format_usd
            assert f"每个任务的花费上限：${format_usd(effective.task_budget_usd)}，达到时再让 Planner 总结一轮后结束" in block
            if selection != "none":
                assert "工作流：Reviewed（reviewed）" in block
                assert f"每步重试次数：{effective.step_retries}" in block
                assert f"依赖条件：{'开' if effective.conditions_on else '关'}" in block
            else:
                assert "工作流" not in block and "每步重试次数" not in block and "依赖条件" not in block
    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("new_project", [False, True], ids=["project-default", "new-project"])
@pytest.mark.parametrize("overrides", [True, False], ids=["workflow-caps", "project-caps"])
def test_client_confirmation_shows_effective_workflow_controls(tmp_path, monkeypatch, new_project, overrides):
    async def test(pool):
        async with v1_api(tmp_path, pool) as call:
            project = await setup(pool, call, tmp_path)
            preset = {**PRESET, "review_all": overrides, "dependency_conditions": overrides,
                      "budget_usd": 0.0001 if overrides else None,
                      "max_step_retries": 0 if overrides else None}
            assert (await call("workflows.put", preset))[0] == 200
            settings = {"review_policy": "planner" if overrides else "all", "task_budget_usd": 12,
                        "max_step_retries": 4, "dependency_conditions": "off" if overrides else "on"}
            await projects.update_project(project["id"], execution=settings)
            choice = ({"project": {"name": "New", "work_dir": str(tmp_path), **settings}, "workflow": "reviewed"}
                      if new_project else {"project_id": project["id"]})
            status, answer = await call("tasks.create", {"requirement": "Build", **choice}, client=True)
            assert status == 202, answer
            impact = answer["impact"]
            assert "- 工作流：Reviewed（reviewed）" in impact
            assert f"- task_budget_usd（每任务花费上限，USD）：{'0.0001' if overrides else '12'}" in impact
            assert f"- max_step_retries（每步重试次数）：{0 if overrides else 4}" in impact
            assert ("- review_policy（步骤评审）：all（每步须通过评审）" if overrides else
                    "- review_policy（步骤评审）：planner（由 Planner 决定）") in impact
            assert f"- dependency_conditions（Planner 组合步骤就绪条件）：{'on' if overrides else 'off'}" in impact
            assert await pool.fetchval("SELECT count(*) FROM tasks") == 0
            status, answer = await call("confirm", {"confirmation_id": answer["confirmation_id"]}, client=True)
            assert status == 200, answer
            row = await tasks.get_task(answer["result"]["task_id"])
            effective = ProjectExecution.from_row(await projects.get_task_project(row["id"])).overlaid(row["workflow"])
            assert effective.review_all is overrides and effective.conditions_on is overrides
            assert effective.task_budget_usd == (0.0001 if overrides else 12)
            assert effective.step_retries == (0 if overrides else 4)
            saved = await projects.get_project(row["project_id"])
            assert all(saved[k] == v for k, v in settings.items())
    run_db(test, monkeypatch, timeout=30)
