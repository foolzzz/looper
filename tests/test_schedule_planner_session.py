"""The first message of a continuing schedule run is a fixed, bounded contract."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents.scheduler import continuing_run_preface
from looper.agents.tiers.planner import PlanStep, PlannerAgent, TaskPlan


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
                                  "different_id", "mark_error", "holder_busy"])
def test_planner_runtime_receives_resume_and_only_an_actual_matching_resume_sets_the_flag(
    monkeypatch, agent_db, planner_claude, mode,
):
    from looper.db import schedules, tasks
    from tests.test_planner_agent import WORK_OK, _project, activity, always, decide, drive, plan, step

    agent_db.get_task_project.return_value = {**_project(), **({"task_schedule_id": "sched-a"}
                                                             if mode != "ordinary" else {})}
    from looper.agents.runtimes import CLAUDE_CODE

    labels = Mock(wraps=CLAUDE_CODE.resident_session)
    monkeypatch.setattr(CLAUDE_CODE, "resident_session", labels)
    copied = mode in ("continuing", "profile", "lost", "different_id", "mark_error")
    claim = AsyncMock(return_value={
        "claimed": True, "session_id": "sess-p" if copied else None,
        "session_totals": {}, "runtime_type": "claude-code", "profile_id": None,
        "provider_id": "other-provider" if mode == "profile" else "prov-claude-subscription", "previous": None,
    })
    if mode == "parallel":
        claim.return_value = {"claimed": False, "reason": "parallel"}
    if mode == "claim_error":
        claim.side_effect = ConnectionError("claim unavailable")
    if mode == "holder_busy":
        claim.return_value = {"claimed": False, "reason": "another run holds the session"}
    save, mark = AsyncMock(), AsyncMock()
    if mode == "mark_error":
        async def mark_session(_task_id, continues):
            if continues:
                raise ConnectionError("mark unavailable")
        mark.side_effect = mark_session
    monkeypatch.setattr(schedules, "claim_planner_session", claim)
    monkeypatch.setattr(schedules, "save_planner_session", save)
    monkeypatch.setattr(tasks, "set_continues_schedule_session", mark)
    claude = planner_claude(plan(step("new-step")), decide("accept"), forget_sessions=mode == "lost")
    if mode == "different_id":
        body = Path(claude.bin + ".py")
        body.write_text(body.read_text().replace("sess-p", "sess-other"))
    harness = drive(claude, always(WORK_OK))
    result = harness.final()
    assert result is not None and result.payload["status"] == "completed"
    first = claude.argvs()[0]
    assert ("--resume" in first) is (mode in ("continuing", "lost", "different_id", "mark_error"))
    if "--resume" in first:
        assert first[first.index("--resume") + 1] == "sess-p"
    continued = [call.args[1] for call in mark.await_args_list if call.args[1]]
    assert continued == ([True] if mode in ("continuing", "mark_error") else [])
    if mode == "continuing":
        assert claude.turns()[0]["text"] == continuing_run_preface("t1", None) + "\n\n" + FRESH
        assert "Continuing the schedule's Planner session" in activity(agent_db)
    elif mode not in ("different_id", "lost", "mark_error"):
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
                   for line in lines)
        assert all("sess-p" not in line for line in lines if "could not be resumed" in line)
    if mode == "different_id":
        assert harness.planner._schedule_session_id is None
        assert any("Schedule Planner session replaced:" in line and "different session" in line for line in lines)
    if mode == "mark_error":
        assert any("could not record" in line and "continu" in line for line in lines)
    assert all("sess-p" not in line and "sess-other" not in line for line in lines)
    if mode == "claim_error":
        assert "Schedule Planner session not claimed: claim unavailable; this run starts fresh" in lines
    if mode in ("ordinary", "parallel", "claim_error", "holder_busy"):
        save.assert_not_awaited()
    else:
        assert save.await_args_list[-1].args[1] == ("sess-other" if mode == "different_id" else "sess-p")
    holder = mode not in ("ordinary", "parallel", "claim_error", "holder_busy")
    assert labels.call_args.args == (("planner-schedule-sched-a" if holder else "planner-t1"),)
    assert all(call.args[0] == "planner-t1" for call in agent_db.save_session.await_args_list)
    assert all(call.kwargs["agent_id"] == "planner-t1" for call in agent_db.start_record.await_args_list)


@pytest.mark.parametrize("holder", ["task-old", "task-other", None])
@pytest.mark.parametrize("stored_session", ["saved-session", "replacement-session", None])
def test_loaded_schedule_plan_respects_current_session_holder(
    monkeypatch, agent_db, planner_claude, tmp_path, holder, stored_session,
):
    from looper.db import schedules
    from tests.test_planner_agent import _project, activity

    agent_db.get_task_project.return_value = {**_project(), "task_schedule_id": "sched-a"}
    get_schedule = AsyncMock(return_value={"planner_session_id": stored_session,
                                          "planner_session_task_id": holder})
    monkeypatch.setattr(schedules, "get_schedule", get_schedule)
    save = AsyncMock()
    monkeypatch.setattr(schedules, "save_planner_session", save)
    claude = planner_claude("reply retained")
    shared_elsewhere = stored_session == "saved-session" and holder not in (None, "task-old")

    async def check():
        old = PlannerAgent(task_id="task-old", runtime_root=tmp_path)
        plan = TaskPlan(plan_id="p", goal_id="g", requirement="do it", work_dir=str(tmp_path), status="failed",
                        session_id="saved-session", session_runtime="claude-code",
                        session_provider="prov-claude-subscription",
                        steps=[PlanStep("step-a", 0, "do it")])
        await old._save(plan, *plan.steps, plan_row=True)
        await old._save_session(plan)
        agent_db.store.tasks["task-old"] = {"requirement": "do it", "work_dir": str(tmp_path)}
        agent = PlannerAgent(task_id="task-old", runtime_root=tmp_path, claude_bin=claude.bin)
        try:
            await agent._load_plan(("failed",))
            assert agent._plan is not None
            assert agent._plan.session_id == (None if shared_elsewhere else "saved-session")
            assert agent._schedule_session_writes is (holder == "task-old")
            if shared_elsewhere:
                assert agent._pending_switch is not None
                assert any("another task" in entry.summary for entry in agent._plan.journal)
                assert agent_db.store.sessions["planner-task-old"]["session_id"] is None
            assert await agent._turn("Continue the saved plan") == "reply retained"
            if holder == "task-old":
                assert save.await_count > 0
                assert all(call.args[0] == "task-old" for call in save.await_args_list)
            else:
                save.assert_not_awaited()
        finally:
            await agent._claude.stop()

    asyncio.run(check())
    assert ("--resume" in claude.argvs()[0]) is not shared_elsewhere
    text = claude.turns()[0]["text"]
    assert text.startswith("[RESUME]") is shared_elsewhere
    if shared_elsewhere:
        assert "another task" in text
        assert any("another task" in line for line in activity(agent_db))
    assert all("saved-session" not in line for line in activity(agent_db)
               if "another task" in line)


def test_restore_after_reset_does_not_resume_a_session_a_live_run_still_has(
    monkeypatch, agent_db, planner_claude, tmp_path,
):
    """A failed with session S, B claimed it and is still running, then the schedule was reset.

    Reset clears the holder, so the holder check does nothing. Restoring A must still drop S.
    """
    from looper.db import schedules
    from tests.test_planner_agent import _project, activity

    session = "session-shared"
    agent_db.get_task_project.return_value = {**_project(), "task_schedule_id": "sched-a"}
    monkeypatch.setattr(schedules, "get_schedule", AsyncMock(return_value={
        "planner_session_id": None, "planner_session_task_id": None,
    }))
    monkeypatch.setattr(schedules, "save_planner_session", AsyncMock())
    claude = planner_claude("reply retained")
    agent_db.store.tasks["task-b"] = {"status": "running", "requirement": "the later run", "work_dir": str(tmp_path)}
    agent_db.store.sessions["planner-task-b"] = {
        "session_id": session, "session_totals": {}, "runtime_type": "claude-code",
        "profile_id": None, "clean_start": False,
    }

    async def check():
        failed = PlannerAgent(task_id="task-a", runtime_root=tmp_path)
        plan = TaskPlan(plan_id="p", goal_id="g", requirement="do it", work_dir=str(tmp_path), status="failed",
                        session_id=session, session_runtime="claude-code",
                        session_provider="prov-claude-subscription",
                        steps=[PlanStep("step-a", 0, "do it")])
        await failed._save(plan, *plan.steps, plan_row=True)
        await failed._save_session(plan)
        agent_db.store.tasks["task-a"] = {"status": "failed", "requirement": "do it", "work_dir": str(tmp_path)}
        agent = PlannerAgent(task_id="task-a", runtime_root=tmp_path, claude_bin=claude.bin)
        try:
            await agent._load_plan(("failed",))
            assert agent._plan is not None and agent._plan.session_id is None
            assert agent_db.store.sessions["planner-task-a"]["session_id"] is None
            assert agent_db.store.sessions["planner-task-b"]["session_id"] == session
            assert await agent._turn("Continue the saved plan") == "reply retained"
        finally:
            await agent._claude.stop()

    asyncio.run(check())
    assert "--resume" not in claude.argvs()[0]
    lines = [line for line in activity(agent_db) if "in use by another run" in line]
    assert lines
    assert all(session not in line for line in lines)


def test_restart_after_reset_resumes_with_the_persisted_storage_label(
    monkeypatch, agent_db, planner_claude, tmp_path,
):
    """Claim writes the storage label; reset then a new Planner still resumes under that label.

    Cleanup keeps a live task's label directory and removes one whose task is gone.
    """
    from looper.agents import read_only_copies
    from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
    from looper.agents.runtimes import CLAUDE_CODE
    from looper.agents.runtimes.per_turn import session_dir_name
    from looper.db import agent_sessions, tasks
    from tests.test_planner_agent import WORK_OK, _project, decide, drive, hold_first, plan, step

    label = "planner-schedule-sched-a"
    deleted = "planner-schedule-sched-gone"
    persisted = {"label": None}

    def project(_task_id: str) -> dict:
        return {**_project(), "task_schedule_id": "sched-a", "task_planner_session_label": persisted["label"]}

    agent_db.get_task_project.side_effect = project

    async def claim(task_id: str) -> dict:
        persisted["label"] = label  # what claim_planner_session writes on the task
        assert task_id == "t1"
        return {"claimed": True, "session_id": "sess-p", "session_totals": {}, "runtime_type": "claude-code",
                "provider_id": "prov-claude-subscription", "profile_id": None, "previous": None}

    from looper.db import schedules

    monkeypatch.setattr(schedules, "claim_planner_session", claim)
    monkeypatch.setattr(schedules, "save_planner_session", AsyncMock())
    monkeypatch.setattr(schedules, "get_schedule", AsyncMock(return_value={
        "planner_session_id": None, "planner_session_task_id": None, "planner_session_runtime": None,
    }))
    monkeypatch.setattr(tasks, "set_continues_schedule_session", AsyncMock())
    seen: list[str] = []
    resident = CLAUDE_CODE.resident_session

    def spy(name: str):
        seen.append(name)
        return resident(name)

    monkeypatch.setattr(CLAUDE_CODE, "resident_session", spy)
    claude = planner_claude(plan(step("only")), decide("accept"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: persisted["label"] == label and h.planner._plan is not None
                      and h.planner._plan.status == "executing" and h.held)
        await h.planner.stop()
        # The in-flight run was cut off while a decision was pending; reset has cleared the holder.
        # A restart reads the task row for the directory the turn runs in.
        agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir),
                                      "status": "running"}
        saved = next(iter(agent_db.store.steps["t1"].values()))
        saved["status"] = "deciding"
        saved["result"] = dict(WORK_OK)
        h.planner = type(h.planner)(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
        await h.planner.start(h.bus)
        await h.bus.publish(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=MessageType.TASK_RESUME,
            payload={"task_id": "t1"}, correlation_id="corr-1",
        ))
        await h.until(lambda: len(claude.argvs()) >= 2 and "--resume" in claude.argvs()[-1])

    h = drive(claude, hold_first(), scenario)
    assert h.planner._claude.label == label
    assert seen[-1] == label
    resumed = claude.argvs()[-1]
    assert resumed[resumed.index("--resume") + 1] == "sess-p"

    kept = tmp_path / ".local" / "codex" / "sessions" / session_dir_name(label)
    gone = tmp_path / ".local" / "codex" / "sessions" / session_dir_name(deleted)
    for directory in (kept, gone):
        directory.mkdir(parents=True)
        (directory / "state").write_text("saved conversation")
    monkeypatch.setattr(agent_sessions, "saved_sessions", AsyncMock(return_value=[]))
    monkeypatch.setattr(schedules, "list_schedules", AsyncMock(return_value=[
        {"id": "sched-a", "planner_session_id": None, "planner_session_runtime": "codex"},
    ]))
    monkeypatch.setattr(tasks, "live_planner_session_labels", AsyncMock(return_value=[(label, "codex")]))
    monkeypatch.setattr(read_only_copies, "command_lines", lambda: [])
    removed = asyncio.run(read_only_copies.reclaim_unused(tmp_path))
    assert gone in removed and kept not in removed
    assert (kept / "state").read_text() == "saved conversation"
    assert not gone.exists()
