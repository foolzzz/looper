"""Panel results remain useful and bounded in the Manager's task tool."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import projects
from looper.agents.ops.tasks import TOOLS
from looper.db import llm_records, task_roles, tasks, user_gates


def manager_view(monkeypatch, seat):
    task = {"id": "task-example", "name": "Review a change", "status": "running", "steps": [
        {"id": "work", "step_key": "dev", "kind": "work", "status": "completed"},
        {"id": "seat", "step_key": "dev/reviewer", "kind": "review", "reviews": "work", **seat}],
        "journal": []}
    monkeypatch.setattr(tasks, "get_task_with_steps", AsyncMock(return_value=task))
    monkeypatch.setattr(llm_records, "task_costs", AsyncMock(return_value={}))
    monkeypatch.setattr(projects, "agents_view", AsyncMock(return_value={"roles": {}}))
    monkeypatch.setattr(projects, "task_readiness", AsyncMock(return_value=[]))
    monkeypatch.setattr(task_roles, "current", AsyncMock(return_value=[]))
    monkeypatch.setattr(user_gates, "list_for_task", AsyncMock(return_value=[]))
    tool = next(tool for tool in TOOLS if tool.name == "looper_tasks")
    return asyncio.run(tool.run(SimpleNamespace(arguments={"action": "get", "task_id": task["id"]},
                                                scope=SimpleNamespace(tasks=None))))


@pytest.mark.parametrize(("status", "result", "verdict"), [
    ("verified", {"passed": True}, "pass"),
    ("verified", {"passed": False}, "fail"),
    ("verified", {"passed": False, "status": "passed_by_adjudication"}, "pass"),
    ("failed", {"failure": "head_moved"}, "fail"),
    ("pending", None, "pending"),
    ("running", {"passed": True}, "pending"),
])
def test_manager_sees_each_seat_verdict(monkeypatch, status, result, verdict):
    view = manager_view(monkeypatch, {"status": status, "result": result})
    work, seat = view["steps"]
    assert "verdict" not in work
    assert seat["reviews"] == "dev"
    assert seat["verdict"] == verdict
    assert seat["blocking"] == seat["suggestions"] == 0
    assert seat["findings"] == []


def test_manager_gets_bounded_findings_counts_and_adjudication_reasons(monkeypatch):
    findings = [{"severity": "blocking" if index < 3 else "suggestion", "where": "calc.py:4",
                 "text": "Handle\nempty input"} for index in range(12)]
    findings[0]["where"] = "calc.py:4 " + "x" * 500
    findings[0]["text"] += "x" * 2000
    # A reason must remain visible even when its finding is beyond the five shown.
    for finding in findings[6:]:
        finding["reason"] = "Planner\nchecked the guard " + "x" * 2000
    view = manager_view(monkeypatch, {"status": "verified", "result": {"passed": False, "findings": findings}})
    seat = view["steps"][1]
    assert seat["blocking"] == 3 and seat["suggestions"] == 9
    assert len(seat["findings"]) == 5
    assert seat["findings"][1] == {"level": "blocking", "where": "calc.py:4", "text": "Handle empty input"}
    assert all(len(f["where"]) <= 120 and len(f["text"]) <= 200 for f in seat["findings"])
    assert len(seat["reasons"]) == 5
    assert seat["reasons"][0]["where"] == "calc.py:4"
    assert seat["reasons"][0]["reason"].startswith("Planner checked the guard")
    assert all(len(reason["reason"]) <= 300 for reason in seat["reasons"])
    assert "\\n" not in json.dumps(seat)
    assert len(json.dumps(seat)) < 4000
