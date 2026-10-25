"""Full v1 task reads, filter/cursor parity and saved configuration readback."""

import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from looper.agents import api_v1, projects
from looper.agents.ops import OpError
from looper.agents.task_workspaces import TaskWorktree
from looper.agents.ops.service import _plain
from looper.agents.user_gates import AgendaItem, AgendaStatus, Gate, GateState, GateSummary
from looper.db import journal, task_changes, task_worktrees, step_workspaces, user_gates
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


@pytest.mark.parametrize("gate_state", [GateState.PENDING, GateState.OPEN, GateState.PASSED])
def test_full_detail_retains_history_ownership_workspaces_and_saved_core_fields(
        tmp_path, monkeypatch, clients, gate_state):
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
            gate = Gate("Delivery format", (AgendaItem(
                "q1", "Which format?", () if gate_state is GateState.PENDING else ("JSON", "CSV"),
                AgendaStatus.RESOLVED if gate_state.closed else AgendaStatus.OPEN,
                "JSON" if gate_state.closed else ""),), state=gate_state)
            if gate_state is not GateState.PENDING:
                gate.opened_at = datetime.now(timezone.utc)
            if gate_state.closed:
                gate.summary = GateSummary(("Use JSON",), ("No legacy reader",), ("Confirm retention",))
                gate.plan_change = {"added": ["export"], "dropped": ["csv"], "stopped": ["legacy"]}
                gate.closed_at = datetime.now(timezone.utc)
            await pool.execute("UPDATE task_steps SET kind='gate', status=$1 WHERE id='step-s2'",
                               gate_state.step_status)
            async with pool.acquire() as conn:
                await user_gates.write(conn, "task-a", [gate.row("step-s2")])
            saved_gate = await user_gates.get("task-a", "s2")
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
            schema = api_v1.find("POST", "/api/v1/tasks/detail").result
            # Listen on an ephemeral loopback port: read the real HTTP envelope and DB projection.
            await server.start()
            try:
                status, _, document = await _send(server, "GET /api/v1/openapi.json", _bearer(TOKEN))
                assert status == 200
                assert document["components"]["schemas"]["TaskDetail"] == schema
                envelope_schema = document["components"]["schemas"]["OpOk"]
                for operator in (False, True):
                    status, answer = await _post(server, "tasks/detail", {"task_id": "task-a"}, operator=operator)
                    assert status == 200 and answer["status"] == "ok", answer
                    api_v1.validate_arguments(envelope_schema, answer)
                    views.append(api_v1.validate_arguments(schema, answer["result"]))
                status, compact = await _post(server, "ops/tasks/get", {"task_id": "task-a"})
            finally:
                await server.stop()
            detail = views[0]
            assert views[0] == views[1]  # Read attribution does not change the task projection.
            assert len(detail["gates"]) == 1
            projected_gate = detail["gates"][0]
            assert projected_gate["state"] == gate_state.value
            assert projected_gate["agenda"] == [item.view() for item in gate.agenda]
            assert projected_gate["summary"] == (gate.summary.view() if gate.summary else None)
            assert projected_gate["plan_change"] == gate.plan_change
            assert projected_gate["waiting_on"] == ("user" if gate_state.waiting else None)
            assert projected_gate["channel_posts"] is None
            for field in ("opened_at", "first_reply_at", "closed_at", "retry_at"):
                value = getattr(gate, field) if field != "retry_at" else gate.retry_at()
                assert projected_gate[field] == (value.isoformat() if value else None)
            assert await user_gates.get("task-a", "s2") == saved_gate
            if gate_state.closed:
                # Both objects remain independently nullable and accept empty string arrays.
                for field in ("summary", "plan_change"):
                    for value in (None, {key: [] for key in projected_gate[field]}):
                        valid = deepcopy(detail)
                        valid["gates"][0][field] = value
                        assert api_v1.validate_arguments(schema, valid) == valid
                # Reject bad types in copies of the actual response, without changing saved gates.
                invalid_fields = [
                    (("agenda", 0, "options"), [{"id": "json", "label": "JSON"}]),
                    (("agenda", 0, "options"), [1]),
                    (("agenda", 0, "options"), [None]),
                    (("agenda", 0, "options"), None),
                    (("summary",), "Use JSON"),
                    (("summary",), []),
                    (("plan_change",), "added export"),
                    (("plan_change",), []),
                    (("state",), 1),
                ]
                for field in ("summary", "plan_change"):
                    for name in projected_gate[field]:
                        invalid_fields.extend([((field, name), "not an array"), ((field, name), [1])])
                    incomplete = dict(projected_gate[field])
                    incomplete.pop(next(iter(incomplete)))
                    invalid_fields.append(((field,), incomplete))
                for path, value in invalid_fields:
                    invalid = deepcopy(detail)
                    parent = invalid["gates"][0]
                    for key in path[:-1]:
                        parent = parent[key]
                    parent[path[-1]] = value
                    with pytest.raises(OpError, match="declared schema"):
                        api_v1.validate_arguments(schema, invalid)
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
