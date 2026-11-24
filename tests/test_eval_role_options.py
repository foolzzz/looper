"""Role comparison options, through the fake runner and isolated server."""

from contextlib import contextmanager
from dataclasses import replace
import json

import pytest

from evals.prompt_regression import __main__ as cli
from evals.prompt_regression import metrics, runner
from tests.db_support import DSN


@pytest.mark.parametrize("roles", ["on", "off"])
def test_role_options_parse_and_reach_the_runner(tmp_path, monkeypatch, roles):
    spec = {"agent": {"name": "comparison Worker"}}
    path = tmp_path / "worker.json"
    path.write_text(json.dumps(spec))
    seen = {}

    def run(**kwargs):
        seen.update(kwargs)
        return {"tasks": []}, tmp_path / "result.json"

    monkeypatch.setattr(runner, "run", run)
    monkeypatch.setattr(cli, "_report", lambda *_, **__: None)
    assert cli.main(["run", "--pg-url", "unused", "--tasks", "single_step",
                     "--planner-roles", roles, "--worker-agent", str(path)]) == 0
    assert seen["planner_roles"] == roles and seen["worker_agent"] == spec
    assert seen["mode"] == "fake"
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["run", "--planner-roles", "invalid"])
    defaults = cli._parser().parse_args(["run"])
    assert defaults.planner_roles is None and defaults.worker_agent is None


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("roles", ["on", "off"])
def test_roles_override_every_selected_task_through_fake_server(tmp_path, monkeypatch, roles):
    original_post = runner.Api.post
    projects = []

    def post(api, path, body):
        if path == "/api/v1/ops/task_control/start":
            detail = original_post(api, "/api/v1/tasks/detail", {"task_id": body["task_id"]})["result"]
            project = next(project for project in original_post(api, "/api/v1/project_admin/list", {})
                           ["result"]["projects"] if project["project_id"] == detail["project_id"])
            projects.append(project["planner_roles"])
        return original_post(api, path, body)

    monkeypatch.setattr(runner.Api, "post", post)
    task = runner.select_tasks(["single_step"])[0]
    tasks = (replace(task, id="roles_default_off", roles_on=False),
             replace(task, id="roles_default_on", roles_on=True))
    result, path = runner.run(mode="fake", tasks=tasks, admin_url=DSN, out_dir=tmp_path,
                              planner_roles=roles)
    assert result["aborted"] is None, result
    assert projects == [roles, roles]
    assert [row["planner_roles"] for row in result["tasks"]] == [roles, roles]
    assert all(row["completion_checks_passed"] for row in result["tasks"])
    assert json.loads(path.read_text()) == result


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_worker_profile_is_registered_once_and_only_used_by_workers_in_fake_mode(tmp_path, monkeypatch):
    original_post = runner.Api.post
    bindings, profiles = [], {}

    def post(api, path, body):
        result = original_post(api, path, body)
        if path == "/api/v1/agent_settings/update_role":
            bindings.append(body)
        if path == "/api/v1/runs/get":
            record = result["result"]["record"]
            profiles.setdefault(metrics.role(record), set()).add(record["metadata"]["profile"]["name"])
        return result

    monkeypatch.setattr(runner.Api, "post", post)
    spec = {"runtime": {"type": "claude-code"}, "provider": {"provider_id": "prov-claude-subscription"},
            "model": {"name": "sonnet", "protocol": "anthropic-messages"},
            "agent": {"name": "eval/comparison-worker"}}
    result, _ = runner.run(mode="fake", tasks=runner.select_tasks(["reviewed_step"]),
                            admin_url=DSN, out_dir=tmp_path, worker_agent=spec, reps=2,
                            agent="claude-code/opus")
    assert result["aborted"] is None, result
    assert len(result["tasks"]) == 2
    assert len(bindings) == 1 and bindings[0]["role"] == "worker"
    assert profiles["worker"] == {"eval/comparison-worker"}
    assert profiles["planner"] == profiles["reviewer"] == {result["agent"]}
    for row in result["tasks"]:
        assert row["worker_agent"]["name"] == "eval/comparison-worker"
        assert row["worker_agent"]["profile_id"] == bindings[0]["values"]["profile_id"]
        assert row["worker_agent"]["model"]["name"] == "sonnet"


@pytest.mark.parametrize("status,check_problem,expect,passed", [
    ("completed", None, {"min_roles_defined": 1}, True),
    ("completed", "file check failed", {}, False),
    ("failed", None, {}, False),
])
def test_completion_score_ignores_only_flow_bounds(tmp_path, status, check_problem, expect, passed):
    args = dict(detail={"status": status}, activity=[], records=[], events={}, changes={},
                work_dir=tmp_path, check_problem=check_problem, expect=expect, now=0)
    normal = metrics.task_metrics(**args)
    recorded = metrics.task_metrics(**args, record_completion_checks=True)
    assert recorded.pop("completion_checks_passed") is passed
    assert recorded == normal  # Default fields and scoring remain identical.


def test_absent_options_preserve_serialized_report(tmp_path, monkeypatch):
    """Freeze the default report envelope, with deterministic case data and run metadata."""
    @contextmanager
    def database(*_, **__):
        yield "unused", 0

    @contextmanager
    def server(**_):
        yield None

    monkeypatch.setattr(runner, "fresh_database", database)
    monkeypatch.setattr(runner, "running_server", server)
    monkeypatch.setattr(runner, "install_claude", lambda *_, **__: None)
    monkeypatch.setattr(runner, "_git", lambda: {})
    monkeypatch.setattr(runner, "select_agent", lambda *_, **__: {"name": "default"})
    monkeypatch.setattr(runner, "run_task", lambda _, task, *a, **kw: {
        "id": task.id, "outcome": "pass", "success": True})
    result, path = runner.run(mode="fake", tasks=runner.select_tasks(["single_step"]),
                              admin_url="unused", out_dir=tmp_path)
    expected = {"schema": runner.SCHEMA, "mode": "fake", "model": None, "protocol": None,
                "cost_basis": None, "agent": "default", "provider_preset": None,
                "started_at": result["started_at"], "runtime_version": None, "git": {},
                "caps": {"max_budget_usd": None, "total_cap_usd": None}, "step_workspaces": None,
                "user_gates": None, "untrusted_fence": False, "reps": 1, "stopped_by_cap": False,
                "aborted": None, "total_cost_usd": 0.0, "tasks": [
                    {"id": "single_step", "rep": 1, "outcome": "pass", "success": True, "user_gates": "on"}]}
    assert path.read_text() == json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
