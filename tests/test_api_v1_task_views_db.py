"""Full v1 task reads, filter/cursor parity and saved configuration readback."""

import json

import pytest

from looper.agents import api_v1, projects
from looper.agents.task_workspaces import TaskWorktree
from looper.agents.ops.service import _plain
from looper.db import journal, task_changes, task_worktrees, step_workspaces
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_api_v1_auth import clients as clients
from tests.test_api_v1_task_configuration_db import _configured, _post
from tests.test_manager_schedule_tools_db import _schedule
from tests.test_operator_auth import TOKEN
from tests.test_ops_tasks_db import _project, _task
from tests.test_planner_tools_db import _steps
from tests.test_task_manager_attribution_db import TEMPLATE
from tests.test_usage_api_db import _run_record

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("filter_name", ["project_id", "schedule_id", "status", "group", "query"])
def test_page_filters_counts_and_cursor_keep_full_page_semantics(tmp_path, monkeypatch, clients, filter_name):
    async def run(pool):
        project, other = await _project(pool), await _project(pool, "other")
        schedule = (await _schedule(project, "Nightly"))["schedule_id"]
        for task, status, owner, text in [("a", "init", project, "calculator"),
            ("b", "init", project, "calculator"), ("c", "failed", other, "unrelated")]:
            await _task(pool, f"task-{task}", status, owner, requirement=text)
        await pool.execute("UPDATE tasks SET schedule_id=$1 WHERE id IN ('task-a','task-b')", schedule)
        filters = {filter_name: {"project_id": project["id"], "schedule_id": schedule,
            "status": "init", "group": "ready", "query": "  CALCULATOR  "}[filter_name], "limit": 1}
        expected_counts = {"init": 2, "failed": 1} if filter_name == "status" else {"init": 2}
        async with _serving(tmp_path) as server:
            for operator in (False, True):
                status, body = await _post(server, "tasks/page", filters, operator=operator)
                assert status == 200, body
                first = api_v1.validate_arguments(api_v1.find("POST", "/api/v1/tasks/page").result, body["result"])
                assert [t["task_id"] for t in first["tasks"]] == ["task-b"]
                assert first["counts"] == expected_counts and first["next_before"] == "task-b"
                status, body = await _post(server, "tasks/page", {**filters, "before": first["next_before"]},
                                           operator=operator)
                assert status == 200 and body["result"]["next_before"] is None
                assert [t["task_id"] for t in body["result"]["tasks"]] == ["task-a"]
                assert body["result"]["counts"] == expected_counts
                assert first["tasks"][0]["schedule_name"] == "Nightly"
    run_db(run, monkeypatch, timeout=30)


def test_full_detail_retains_history_ownership_workspaces_and_saved_core_fields(tmp_path, monkeypatch, clients):
    async def run(pool):
        await _task(pool, "task-a", "init", await _project(pool))
        await pool.execute("UPDATE tasks SET template=$1::jsonb WHERE id='task-a'", json.dumps(TEMPLATE))
        await pool.execute("INSERT INTO api_clients (id,name,token_sha256) VALUES ('client-owner','Owner','hash')")
        await pool.execute("UPDATE tasks SET client_id='client-owner' WHERE id='task-a'")
        async with _configured(tmp_path) as server:
            config = {"nested": {"enabled": False, "limits": [0, 2]}, "verify": True}
            status, body = await _post(server, "tasks/configure", {"task_id": "task-a", "config": config,
                "work_dir": str(tmp_path), "requirement": "Build calculator and tests"})
            assert status == 200, body
            await pool.execute("INSERT INTO agent_profiles (id,name,runtime_id,model_id) VALUES "
                "('profile-choice','Choice','rt-claude-code',(SELECT id FROM llm_models "
                "WHERE provider_id='prov-claude-subscription' AND name='sonnet'))")
            status, body = await _post(server, "tasks/agents", {"task_id": "task-a",
                "profiles": {"worker": "profile-choice"}, "concurrency": {"worker": 2}})
            assert status == 200, body
            await _steps(pool, "task-a", s1="verified", s2="pending")
            await task_changes.add_supplement("task-a", "s" * 320, via="chat", pool=pool)
            await journal.insert(pool, "task-a", [{"step_id": "step-s1", "kind": "step_accepted", "summary": "Built"},
                {"step_id": "", "kind": "progress", "summary": json.dumps({"done": "Built", "now": "Tests",
                    "next": "Review", "risks": "None", "estimate": "Soon"})}])
            await _run_record(pool, "worker-one", task_id="task-a", cost=0.5)
            await task_worktrees.save(TaskWorktree("task-a", str(tmp_path), "", str(tmp_path / "tree"),
                                                  "looper/task-a/task", "a" * 40).row())
            await step_workspaces.save({"id": 1, "task_id": "task-a", "step_id": "step-s1", "repo_dir": str(tmp_path),
                "subdir": "", "target_branch": "main", "path": str(tmp_path / "step"), "branch": "step",
                "base_commit": "a" * 40, "checkpoint": False, "state": "active", "detail": "",
                "links": [], "merge_files": []})
            views = []
            for operator in (False, True):
                status, answer = await _post(server, "tasks/detail", {"task_id": "task-a"}, operator=operator)
                assert status == 200, answer
                views.append(api_v1.validate_arguments(api_v1.find("POST", "/api/v1/tasks/detail").result,
                                                       answer["result"]))
            detail = views[0]
            assert views[0] == views[1]  # Read attribution does not change the task projection.
            assert detail["config"] == config and detail["work_dir"] == str(tmp_path)
            assert detail["requirement"] == "Build calculator and tests"
            assert detail["requirement_versions"][-1]["text"] == detail["requirement"]
            assert detail["steps"][0]["id"] == "step-s1" and detail["journal"] and detail["progress"]
            assert detail["client"] == {"id": "client-owner", "name": "Owner"}
            assert detail["manager_id"] == "manager" and detail["manager_name"] and detail["template"] == TEMPLATE
            assert detail["cost"]["cost_usd"] == 0.5 and detail["agents"]["roles"]["worker"]["concurrency"]
            assert detail["profile_overrides"]["worker"]["profile_id"] == "profile-choice"
            assert detail["agents"]["roles"]["worker"]["concurrency"]["task"] == 2
            assert set(detail) == set(api_v1.find("POST", "/api/v1/tasks/detail").result["required"])
            assert detail["task_workspace"]["worktree"]["commands"]
            assert detail["step_workspaces"]["workspaces"][0]["step_key"] == "s1"
            status, compact = await _post(server, "ops/tasks/get", {"task_id": "task-a"})
            assert status == 200 and compact["result"] == _plain(await projects.task_detail("task-a", compact=True))
            assert "config" not in compact["result"] and "task_workspace" not in compact["result"]
            assert len(compact["result"]["supplements"][0]["text"]) < len(detail["supplements"][0]["text"])
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action,arguments,legacy,code", [
    ("page", {"before": "task-absent"}, "/api/tasks", "invalid"),
    ("detail", {"task_id": "task-absent"}, "/api/tasks/task-a", "not_found"),
])
def test_unknown_reads_are_typed_and_old_routes_are_gone_with_positive_control(
        tmp_path, monkeypatch, clients, action, arguments, legacy, code):
    async def run(pool):
        await _task(pool, "task-a", "init", await _project(pool))
        async with _serving(tmp_path) as server:
            status, body = await _post(server, f"tasks/{action}", arguments)
            assert status == (400 if code == "invalid" else 404) and body["error"]["code"] == code
            status, body = await _post(server, f"tasks/{action}", {} if action == "page" else {"task_id": "task-a"})
            assert status == 200 and body["result"]
            assert (await _send(server, f"GET {legacy}", _bearer(TOKEN)))[0] == 404
    run_db(run, monkeypatch, timeout=30)
