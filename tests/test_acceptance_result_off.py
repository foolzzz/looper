"""Per-output freezes recorded before result evidence was added, with no measured rows."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import schedule_notify, task_notice
from looper.agents.acceptance_checks import CommandCheck
from looper.agents.tiers import planner
from looper.db import step_check_runs


FIXTURE = Path(__file__).parent / "fixtures" / "acceptance_result_off.json"
BASE_SHA = "9912920e45b6519749547974e953716c0b6c7c32"
FINAL = ("## Delivered\nA report and its reader.\n\n## How to use\nOpen the report.\n\n"
         "## Verification\nThe reader was checked.\n\n## Open issues\nNone.")
DELIVERY = "## Process record\nDelivery is ready for review."
CHANNELS = {
    "lark_direct": (task_notice.NotifyChannel.LARK, False),
    "lark_group": (task_notice.NotifyChannel.LARK, True),
    "telegram": (task_notice.NotifyChannel.TELEGRAM, False),
}


async def published(tmp_path, monkeypatch, ending, saved_checks=False):
    """Run the real Planner ending, isolating persistence and model closeout only."""
    agent = planner.PlannerAgent(task_id="task-fixture", runtime_root=tmp_path)
    agent._bus = SimpleNamespace(publish=AsyncMock())
    agent._save = AsyncMock()
    agent._activity = AsyncMock()
    agent._remove_workspaces = AsyncMock()
    agent._vault_closeout = AsyncMock()
    agent._process_record = AsyncMock(return_value=None)
    monkeypatch.setattr(planner, "update_task_phase", AsyncMock())
    monkeypatch.setattr(step_check_runs, "list_runs", AsyncMock(return_value=[]))
    steps = [planner.PlanStep(
        f"step-{number}", number, f"Create report part {number}.", key=f"s{number}",
        status="verified", acceptance_criteria=["The part can be read."],
        result={"status": "completed", "summary": f"Report part {number}."},
        checks=(CommandCheck("c1", 1, "test -f report.txt"),) if saved_checks else (),
    ) for number in range(1, 4)]
    plan = planner.TaskPlan("plan-fixture", "goal-fixture", "Create a report and its reader.",
                            "fixture-work", steps=steps, final_summary=FINAL)
    if ending == "completed":
        await agent._complete_plan(plan)
    else:
        steps[1].status = "failed"
        steps[1].result = {"status": "failed", "error": "Reader failed."}
        steps[2].status, steps[2].result = "pending", None
        await agent._fail_plan(plan, reason="The reader could not be completed.")
    (call,) = agent._bus.publish.await_args_list
    return call.args[0].payload


def notice(ending, summary, channel_name):
    channel, group = CHANNELS[channel_name]
    return task_notice.notice_text(
        "task-fixture", "Create a report and its reader.", ending, summary + "\n\n" + DELIVERY,
        channel=channel, group=group, dashboard_url="https://dashboard.example.invalid",
        delivery_summary=DELIVERY, extra="Branch: report-fixture")


def scheduled(ending, summary, format_name):
    schedule = {"id": "schedule-fixture", "name": "Daily report", "timezone": "UTC",
                "notify_format": format_name}
    run = {"id": "run-fixture", "status": ending, "summary": summary, "task_id": "task-fixture",
           "scheduled_for": datetime(2026, 11, 22, 12, tzinfo=timezone.utc), "cost_usd": 0}
    return schedule_notify.run_text(schedule, "Report project", run, base="https://dashboard.example.invalid")


def frozen():
    data = json.loads(FIXTURE.read_text())
    assert data["generated_at"] == BASE_SHA
    return data


@pytest.mark.parametrize("saved_checks", [False, True], ids=["no-checks", "off-saved-checks-no-runs"])
@pytest.mark.parametrize("ending", ["completed", "failed"])
def test_off_planner_summary_is_frozen(tmp_path, monkeypatch, ending, saved_checks):
    payload = asyncio.run(published(tmp_path, monkeypatch, ending, saved_checks))
    assert payload["summary"] == frozen()["planner"][ending]["summary"]


@pytest.mark.parametrize("saved_checks", [False, True], ids=["no-checks", "off-saved-checks-no-runs"])
@pytest.mark.parametrize("ending", ["completed", "failed"])
def test_off_planner_payload_keys_are_frozen(tmp_path, monkeypatch, ending, saved_checks):
    payload = asyncio.run(published(tmp_path, monkeypatch, ending, saved_checks))
    assert sorted(payload) == frozen()["planner"][ending]["keys"]
    assert "acceptance" not in payload


@pytest.mark.parametrize("saved_checks", [False, True], ids=["no-checks", "off-saved-checks-no-runs"])
@pytest.mark.parametrize("ending", ["completed", "failed"])
@pytest.mark.parametrize("channel_name", CHANNELS)
def test_off_channel_notice_is_frozen(tmp_path, monkeypatch, ending, channel_name, saved_checks):
    payload = asyncio.run(published(tmp_path, monkeypatch, ending, saved_checks))
    assert notice(ending, payload["summary"], channel_name) == frozen()["notices"][ending][channel_name]


@pytest.mark.parametrize("ending", ["completed", "failed"])
@pytest.mark.parametrize("format_name", ["report", "deliverable"])
def test_off_schedule_notice_is_frozen(ending, format_name):
    data = frozen()
    assert scheduled(ending, data["planner"][ending]["summary"], format_name) == data["schedules"][ending][format_name]
