"""The first message of a continuing schedule run is a fixed, bounded contract."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents.scheduler import continuing_run_preface
from looper.agents.tiers.planner import PlannerAgent, TaskPlan


OPENING = (
    "这是这个定时任务的一次新运行，任务编号是 task-new。上面这段对话都是更早的运行；那些运行的任务编号、步骤、计划和关卡都不属于这个任务。"
    "这个任务的需求和前置检查输出在后面（这一轮的 Task 一节）。做这次的需求；更早的运行只当作记忆。"
)
CLOSING = "不要沿用更早一次运行的步骤编号；这个任务的步骤用新的编号。"
FRESH = (
    "[PLAN] Plan the task in the Task section of your instructions.\n\n"
    "You may look at the project first, without changing it. Reply with the PLAN JSON: at least 1 step."
)


@pytest.mark.parametrize(("previous", "middle"), [
    (None, ""),
    ({"status": "completed", "summary": "做完了", "error": "ignored"}, "上一轮：完成 · 做完了\n\n"),
    ({"status": "failed", "summary": None, "error": " failed\nreason "}, "上一轮：失败 · failed reason\n\n"),
    ({"status": "timed_out", "summary": "x" * 301}, "上一轮：超时被停止 · " + "x" * 300 + "…\n\n"),
    ({"status": "skipped_gate", "error": None}, "上一轮：跳过（前置检查没有输出） · \n\n"),
])
def test_continuing_run_preface(previous, middle):
    assert continuing_run_preface("task-new", previous) == OPENING + "\n\n" + middle + CLOSING


def test_fresh_plan_message_is_byte_identical(tmp_path: Path):
    agent = PlannerAgent(task_id="task-new", runtime_root=tmp_path)
    plan = TaskPlan(plan_id="p", goal_id="g", requirement="do it", work_dir=str(tmp_path))
    assert agent._plan_message(plan).encode() == FRESH.encode()
    agent._schedule_session_id = plan.session_id = "stored"
    assert agent._plan_message(plan) == OPENING + "\n\n" + CLOSING + "\n\n" + FRESH
    agent._schedule_session_id = None
    assert agent._plan_message(plan).encode() == FRESH.encode()


@pytest.mark.parametrize("mode", ["ordinary", "fresh", "continuing", "parallel", "profile", "lost", "claim_error",
                                  "different_id"])
def test_planner_runtime_receives_resume_and_only_an_actual_matching_resume_sets_the_flag(
    monkeypatch, agent_db, planner_claude, mode,
):
    from looper.db import schedules, tasks
    from tests.test_planner_agent import WORK_OK, _project, activity, always, decide, drive, plan, step

    agent_db.get_task_project.return_value = {**_project(), **({"task_schedule_id": "sched-a"}
                                                             if mode != "ordinary" else {})}
    copied = mode in ("continuing", "profile", "lost", "different_id")
    claim = AsyncMock(return_value={
        "claimed": True, "session_id": "sess-p" if copied else None,
        "session_totals": {}, "runtime_type": "claude-code", "profile_id": None,
        "provider_id": "other-provider" if mode == "profile" else "prov-claude-subscription", "previous": None,
    })
    if mode == "parallel":
        claim.return_value = {"claimed": False, "reason": "parallel"}
    if mode == "claim_error":
        claim.side_effect = ConnectionError("claim unavailable")
    save, mark = AsyncMock(), AsyncMock()
    monkeypatch.setattr(schedules, "claim_planner_session", claim)
    monkeypatch.setattr(schedules, "save_planner_session", save)
    monkeypatch.setattr(tasks, "set_continues_schedule_session", mark)
    claude = planner_claude(plan(step("new-step")), decide("accept"), forget_sessions=mode == "lost")
    if mode == "different_id":
        body = Path(claude.bin + ".py")
        body.write_text(body.read_text().replace("sess-p", "sess-other"))
    result = drive(claude, always(WORK_OK)).final()
    assert result is not None and result.payload["status"] == "completed"
    first = claude.argvs()[0]
    assert ("--resume" in first) is (mode in ("continuing", "lost", "different_id"))
    if "--resume" in first:
        assert first[first.index("--resume") + 1] == "sess-p"
    continued = [call.args[1] for call in mark.await_args_list if call.args[1]]
    assert continued == ([True] if mode == "continuing" else [])
    if mode == "continuing":
        assert claude.turns()[0]["text"] == continuing_run_preface("t1", None) + "\n\n" + FRESH
        assert "Continuing the schedule's Planner session" in activity(agent_db)
    elif mode not in ("different_id", "lost"):
        assert claude.turns()[0]["text"] == FRESH
    assert all("这是这个定时任务的一次新运行" not in prompt for prompt in claude.prompts())
    lines = activity(agent_db)
    if mode == "parallel":
        assert "Schedule Planner session left as stored: this run overlaps (parallel) and starts fresh" in lines
    if mode == "profile":
        assert any("Schedule Planner session replaced: the stored session cannot be resumed (" in line
                   and "other-provider" in line for line in lines)
    if mode == "lost":
        assert "--resume" not in claude.argvs()[1]
        assert claude.turns()[0]["text"] == FRESH
        assert any("Schedule Planner session replaced: the stored session could not be resumed (" in line
                   and "sess-p" in line for line in lines)
    if mode == "claim_error":
        assert "Schedule Planner session not claimed: claim unavailable; this run starts fresh" in lines
    if mode in ("ordinary", "parallel", "claim_error"):
        save.assert_not_awaited()
    else:
        assert save.await_args_list[-1].args[1] == ("sess-other" if mode == "different_id" else "sess-p")
