"""Task workflow snapshots on the Planner's normal planning and dispatch path."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents.project_execution import ProjectExecution
from looper.agents.system_prompt import plan_task_section
from looper.db import task_roles
from tests.test_planner_agent import WORK_FAILED, WORK_OK, _project, always, decide, drive, plan, step


WORKFLOW = {"key": "reviewed", "name": "Reviewed work", "max_step_retries": 0,
            "review_all": False, "dependency_conditions": True, "escalate_on_exhaustion": True,
            "work_role": "dev", "planner_instructions": "Keep the changes focused.",
            "roles": [{"key": "dev", "kind": "work", "role_prompt": "Implement the change."}]}


def test_workflow_overrides_current_project_execution_without_changing_it() -> None:
    project = ProjectExecution.from_row({"max_step_retries": 4, "review_policy": "all",
                                         "dependency_conditions": "off", "user_gates": "off"})
    assert project.overlaid(None) is project
    effective = project.overlaid(WORKFLOW)
    assert effective.step_retries == 0 and not effective.review_all
    assert effective.conditions_on and effective.gates_on
    assert project.step_retries == 4 and project.review_all and not project.gates_on


def test_task_section_only_claims_the_workflow_rules_that_are_enforced() -> None:
    text = plan_task_section(task_id="t1", requirement="Build it.", project_name="P", work_dir="",
                             workflow={**WORKFLOW, "budget_usd": 3,
                                       "review_panel": {"members": ["review_a"]},
                                       "verification": {"role": "verify_staging", "max_rounds": 2}})
    assert "Workflow: Reviewed work" in text
    assert "Workflow rules (code enforces these):" in text
    assert "0 retries per step" in text and "Task spending cap: USD 3" in text
    assert "Work steps without a role use dev" in text
    assert "Workflow roles are fixed: dev (work)" in text
    assert "Workflow instructions (follow them; they are the operator's):\nKeep the changes focused." in text
    assert "review_a" not in text and "verify_staging" not in text


@pytest.mark.parametrize("explicit_role", [False, True])
@pytest.mark.parametrize("via,version", [("workflow", 1), ("user", 2)])
def test_workflow_roles_run_even_with_project_roles_off(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch, explicit_role: bool,
    via: str, version: int,
) -> None:
    agent_db.get_task_project.return_value = {**_project(), "planner_roles": "off", "task_workflow": WORKFLOW}
    role = {"task_id": "t1", "role_key": "dev", "version": version, "kind": "work", "via": via,
            "role_prompt": "Use the user's edited instructions." if via == "user" else "Implement the change.",
            "harness": None, "profile_id": None,
            "reason": "from workflow", "profile_name": None, "profile_allowed": False}
    monkeypatch.setattr(task_roles, "current", AsyncMock(return_value=[
        role, {**role, "role_key": "ad_hoc"}, {**role, "role_key": "old_workflow_role", "via": "workflow"}]))
    monkeypatch.setattr(task_roles, "get", AsyncMock(return_value=role))
    spec = {**step("s1"), **({"role": "dev"} if explicit_role else {})}
    claude = planner_claude(plan(spec), decide("accept"))
    h = drive(claude, always(WORK_OK))
    assert h.final().payload["status"] == "completed"
    assert h.work_items[0].payload["task_role"]["id"] == "dev"
    assert h.work_items[0].payload["task_role"]["version"] == version
    assert h.work_items[0].payload["task_role"]["role_prompt"] == role["role_prompt"]
    assert set(h.planner._roles) == {"dev"}
    assert "Workflow: Reviewed work" in claude.prompts()[0]
    assert '"role", a fixed workflow work role' in claude.prompts()[0]
    assert '"review_roles", fixed workflow' in claude.prompts()[0]
    assert "looper_task_role_set" not in claude.prompts()[0]


def test_workflow_retry_limit_is_used_for_the_result_decision(planner_claude: Any, agent_db: Any) -> None:
    agent_db.get_task_project.return_value = {
        **_project(), "max_step_retries": 3, "task_workflow": {**WORKFLOW, "roles": [], "work_role": None}}
    claude = planner_claude(plan(step("s1")), decide("retry", feedback="Try again"),
                            decide("fail", reason="No retries remain"))
    h = drive(claude, always(WORK_FAILED))
    assert h.final().payload["status"] == "failed"
    assert len(h.work_items) == 1
    assert "has used its 0 retries" in claude.turns()[2]["text"]
