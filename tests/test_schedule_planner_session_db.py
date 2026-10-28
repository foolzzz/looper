"""Schedule session ownership and real Planner resume arguments on disposable PostgreSQL."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents.message import MessageEnvelope, MessageType, TASK_ROUTER_ADDRESS
from looper.agents.run_usage import Usage
from looper.agents.session_switch import session_lost
from looper.agents.tiers.planner import PlannerAgent
from looper.db import agent_sessions, projects, schedules, tasks
from tests.db_support import DSN
from tests.schedule_support import run_scheduled
from tests.test_planner_agent import plan, step
from tests.test_schedule_planner_session import FRESH, SCHEDULE_RUN_REQUIREMENT_HEADING, with_run_requirement

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _task(env: Any, sid: str, task_id: str) -> None:
    await tasks.create_task(task_id=task_id, requirement="Check the new input.", project_id=env.project_id,
                            work_dir=env.work_dir)
    await env.pool.execute("UPDATE tasks SET schedule_id = $2, status = 'running' WHERE id = $1", task_id, sid)


async def _store(task_id: str, session_id: str, cost: float = 0.25, *, provider: str | None = None) -> None:
    await schedules.save_planner_session(task_id, session_id, Usage(cost_usd=cost).to_dict(),
                                         runtime_type="claude-code", provider_id=provider, profile_id=None)


async def _row(env: Any, sid: str) -> dict[str, Any]:
    return dict(await env.pool.fetchrow("SELECT * FROM schedules WHERE id = $1", sid))


async def _phases(env: Any, task_id: str) -> list[str]:
    return [row["summary"] for row in await env.pool.fetch(
        "SELECT summary FROM activity WHERE task_id = $1 AND kind = 'phase' ORDER BY seq", task_id)]


async def _plan(env: Any, task_id: str, claude: Any) -> PlannerAgent:
    agent = PlannerAgent(task_id=task_id, runtime_root=Path(env.work_dir) / task_id, claude_bin=claude.bin)
    agent._bus = env.bus
    # This test exercises the real planning/runtime/persistence path; executing the resulting Workers is separate.
    agent._dispatch_ready = AsyncMock()
    envelope = MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=agent.agent_address, message_type=MessageType.TASK_REQUEST,
        payload={"requirement": "Check the new input.", "work_dir": env.work_dir, "project_id": env.project_id},
    )
    try:
        await agent._plan_and_dispatch(envelope)
        assert agent._plan is not None and agent._plan.status == "executing"
    finally:
        await agent._claude.stop()
    return agent


def test_second_and_later_runs_resume_the_stored_id_with_own_cost_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, planner_claude: Any,
) -> None:
    claude = planner_claude(plan(step("new-step")))

    async def test(env: Any) -> None:
        sid = (await env.schedule())["schedule_id"]
        since = None
        for index in range(3):
            task_id = f"task-{index}"
            await _task(env, sid, task_id)
            agent = await _plan(env, task_id, claude)
            own = await agent_sessions.get_session(agent.agent_id)
            row = await _row(env, sid)
            assert own["session_id"] == row["planner_session_id"] == "sess-p"
            assert own["session_totals"]["cost_usd"] == (index + 1) * 0.25
            assert await env.pool.fetchval(
                "SELECT continues_schedule_session FROM tasks WHERE id = $1", task_id) is (index > 0)
            argv = claude.argvs()[index]
            message = claude.turns()[index]["text"]
            if index:
                assert argv[argv.index("--resume") + 1] == "sess-p"
                assert message.endswith(FRESH) and message.startswith("这是这个定时任务的一次新运行")
                assert f"{SCHEDULE_RUN_REQUIREMENT_HEADING}\nCheck the new input." in message
                assert "上一轮：完成 · previous result" in message
                assert row["planner_session_since"] == since
                assert "Continuing the schedule's Planner session" in await _phases(env, task_id)
            else:
                assert "--resume" not in argv
                assert message == with_run_requirement("Check the new input.")
                since = row["planner_session_since"]
            assert "这是这个定时任务的一次新运行" not in claude.prompts()[index]
            assert SCHEDULE_RUN_REQUIREMENT_HEADING not in claude.prompts()[index]
            records = await env.pool.fetch("SELECT cost_usd FROM llm_records WHERE task_id = $1", task_id)
            assert [r["cost_usd"] for r in records] == [0.25]
            await env.pool.execute("UPDATE tasks SET status = 'completed' WHERE id = $1", task_id)
            now = datetime.now(timezone.utc)
            run_id = await schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=now, trigger="manual",
                                                status="completed", task_id=task_id, finished_at=now)
            await schedules.update_run(env.pool, run_id, summary="previous result")

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("overlap", ["skip", "queue"])
def test_reset_revokes_write_through_but_preserves_the_inflight_tasks_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, planner_claude: Any, overlap: str,
) -> None:
    claude = planner_claude(plan(step("step-a")))

    async def test(env: Any) -> None:
        sid = (await env.schedule(overlap=overlap))["schedule_id"]
        await _task(env, sid, "task-old")
        agent = await _plan(env, "task-old", claude)
        assert await env.ok("POST", "/api/v1/schedule_admin/reset_planner_session", {"schedule_id": sid}) == {"cleared": True}
        assert await env.ok("POST", "/api/v1/schedule_admin/reset_planner_session", {"schedule_id": sid}) == {"cleared": False}
        agent._plan.session_totals = Usage(cost_usd=9)
        await agent._save_session(agent._plan)
        own = await agent_sessions.get_session(agent.agent_id)
        assert own["session_id"] == "sess-p" and own["session_totals"]["cost_usd"] == 9
        row = await _row(env, sid)
        assert row["planner_session_id"] is None and row["planner_session_task_id"] is None
        assert row["planner_session_since"] is None
        assert json.loads(row["planner_session_totals"]) == {}
        await _task(env, sid, "task-next")
        await _plan(env, "task-next", claude)
        assert "--resume" not in claude.argvs()[-1]
        assert claude.turns()[-1]["text"] == with_run_requirement("Check the new input.")
        # Even after another run claims it, an old writer cannot overwrite the new holder's session.
        await agent._save_session(agent._plan)
        assert json.loads((await _row(env, sid))["planner_session_totals"])["cost_usd"] == 0.25

    run_scheduled(test, monkeypatch, tmp_path)


def test_parallel_runs_neither_copy_nor_write_the_stored_conversation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, planner_claude: Any,
) -> None:
    first, second = planner_claude(plan(step("a"))), planner_claude(plan(step("b")))
    for runtime, session_id in ((first, "parallel-session-a"), (second, "parallel-session-b")):
        body = Path(runtime.bin + ".py")
        body.write_text(body.read_text().replace("sess-p", session_id))

    async def test(env: Any) -> None:
        sid = (await env.schedule())["schedule_id"]
        await _task(env, sid, "task-before")
        await schedules.claim_planner_session("task-before")
        await _store("task-before", "stored-before-parallel")
        await env.ok("POST", "/api/v1/schedule_admin/update", {"schedule_id": sid, "fields": {"overlap": "parallel"}})
        before = await _row(env, sid)
        for task_id, claude in (("task-parallel-a", first), ("task-parallel-b", second)):
            await _task(env, sid, task_id)
            await _plan(env, task_id, claude)
            assert "--resume" not in claude.argvs()[0]
            assert claude.turns()[0]["text"] == FRESH
            assert "Schedule Planner session left as stored: this run overlaps (parallel) and starts fresh" in \
                await _phases(env, task_id)
            own = await agent_sessions.get_session(f"planner-{task_id}")
            assert own["session_id"] != before["planner_session_id"]
            assert not await env.pool.fetchval("SELECT continues_schedule_session FROM tasks WHERE id = $1", task_id)
        assert await _row(env, sid) == before
        assert (await agent_sessions.get_session("planner-task-parallel-a"))["session_id"] != \
            (await agent_sessions.get_session("planner-task-parallel-b"))["session_id"]
        # A mode edit while a holder runs must prevent its later writes too.
        await _store("task-before", "must-not-write")
        assert await _row(env, sid) == before

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("fallback", ["profile", "missing", "claim"])
def test_each_fallback_records_its_reason_and_only_unusable_sessions_are_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, planner_claude: Any, fallback: str,
) -> None:
    claude = planner_claude(plan(step("a")), forget_sessions=fallback == "missing")

    async def test(env: Any) -> None:
        sid = (await env.schedule())["schedule_id"]
        await _task(env, sid, "task-before")
        await schedules.claim_planner_session("task-before")
        await _store("task-before", "obsolete-session", provider="other-provider" if fallback == "profile" else None)
        await env.pool.execute("UPDATE tasks SET status = 'completed' WHERE id = 'task-before'")
        before = await _row(env, sid)
        await _task(env, sid, "task-new")
        if fallback == "claim":
            with monkeypatch.context() as patch:
                patch.setattr(schedules, "claim_planner_session", AsyncMock(side_effect=ConnectionError("claim unavailable")))
                await _plan(env, "task-new", claude)
        else:
            await _plan(env, "task-new", claude)
        assert not await env.pool.fetchval("SELECT continues_schedule_session FROM tasks WHERE id = 'task-new'")
        phases = await _phases(env, "task-new")
        if fallback == "claim":
            assert "Schedule Planner session not claimed: claim unavailable; this run starts fresh" in phases
            assert await _row(env, sid) == before
        else:
            phrase = "cannot be resumed" if fallback == "profile" else "could not be resumed"
            assert any(f"Schedule Planner session replaced: the stored session {phrase} (" in p for p in phases)
            reason = "other-provider" if fallback == "profile" else "obsolete-session"
            assert any(phrase in p for p in phases)
            assert fallback == "profile" or not any(reason in p for p in phases)  # the raw session id stays out of the activity
            assert (await _row(env, sid))["planner_session_id"] == "sess-p"
            await env.pool.execute("UPDATE tasks SET status = 'completed' WHERE id = 'task-new'")
            await _task(env, sid, "task-after")
            replacement = planner_claude(plan(step("replacement")))
            await _plan(env, "task-after", replacement)
            assert replacement.argvs()[0][replacement.argvs()[0].index("--resume") + 1] == "sess-p"
        sent = claude.turns()[-1]["text"]
        if fallback == "claim":
            assert sent == FRESH
        else:
            # The stored session could not be resumed. This run still writes the replacement, so its
            # requirement is in the new session and the continuing preface is not.
            assert sent == with_run_requirement("Check the new input.")
            assert "这是这个定时任务的一次新运行" not in sent
        if fallback == "missing":
            assert claude.argvs()[0][claude.argvs()[0].index("--resume") + 1] == "obsolete-session"
            assert "--resume" not in claude.argvs()[1]
            assert await env.pool.fetchval("SELECT count(*) FROM activity WHERE task_id = 'task-new' AND kind = 'error'")
        else:
            assert "--resume" not in claude.argvs()[0]

    run_scheduled(test, monkeypatch, tmp_path)


def test_restarted_holder_stores_the_replacement_session_and_the_next_run_continues_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, planner_claude: Any,
) -> None:
    """The holder restarts, and its stored session cannot be resumed because the provider changed.

    The schedule stores the new session, and the next run resumes it.
    """
    first = planner_claude(plan(step("a")))
    restarted_claude = planner_claude("kept")
    following = planner_claude(plan(step("next")))

    async def test(env: Any) -> None:
        sid = (await env.schedule())["schedule_id"]
        label = f"planner-schedule-{sid}"
        await _task(env, sid, "task-holder")
        await _plan(env, "task-holder", first)
        await env.pool.execute(
            "UPDATE schedules SET planner_session_id = 'old-session', planner_session_provider = 'other-provider' "
            "WHERE id = $1", sid)
        await env.pool.execute(
            "UPDATE agent_sessions SET session_id = 'old-session', provider_id = 'other-provider' "
            "WHERE agent_id = 'planner-task-holder'")
        agent = PlannerAgent(task_id="task-holder", runtime_root=Path(env.work_dir) / "restart",
                             claude_bin=restarted_claude.bin)
        agent._bus = env.bus
        agent._dispatch_ready = AsyncMock()
        try:
            await agent._load_plan()
            assert agent._plan is not None and agent._plan.session_id == "old-session"
            assert agent._schedule_session_writes is True and not agent._schedule_directory_adopted
            assert await agent._turn("Continue after the provider change") == "kept"
            assert agent._claude.label == label
            assert agent._schedule_session_writes is True and agent._schedule_directory_adopted is True
        finally:
            await agent._claude.stop()
        assert "--resume" not in restarted_claude.argvs()[0]
        row = await _row(env, sid)
        assert row["planner_session_id"] == "sess-p"
        assert row["planner_session_task_id"] == "task-holder"
        assert row["planner_session_provider"] == "prov-claude-subscription"
        assert await env.pool.fetchval(
            "SELECT planner_session_label FROM tasks WHERE id = 'task-holder'") == label
        await env.pool.execute("UPDATE tasks SET status = 'completed' WHERE id = 'task-holder'")
        await _task(env, sid, "task-after")
        await _plan(env, "task-after", following)
        argv = following.argvs()[0]
        assert argv[argv.index("--resume") + 1] == "sess-p"
        assert await env.pool.fetchval(
            "SELECT continues_schedule_session FROM tasks WHERE id = 'task-after'") is True
        assert (await _row(env, sid))["planner_session_id"] == "sess-p"

    run_scheduled(test, monkeypatch, tmp_path)


def test_active_holder_is_not_stolen_and_handoff_replacement_survives_edits_and_deletion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, planner_claude: Any,
) -> None:
    claude = planner_claude(plan(step("a")))

    async def test(env: Any) -> None:
        sid = (await env.schedule())["schedule_id"]
        await _task(env, sid, "task-holder")
        agent = await _plan(env, "task-holder", claude)
        await _task(env, sid, "task-replan")
        denied = await schedules.claim_planner_session("task-replan")
        assert denied == {"claimed": False, "reason": "another active task holds it"}
        assert await agent_sessions.get_session("planner-task-replan") is None
        await _store("task-replan", "wrong-holder")
        assert (await _row(env, sid))["planner_session_id"] == "sess-p"
        await agent._drop_session(agent._plan, session_lost("compacted session unavailable"))
        agent._plan.session_id = "replacement-session"
        await agent._save_session(agent._plan)
        before = await _row(env, sid)
        await env.ok("POST", "/api/v1/schedule_admin/update", {"schedule_id": sid, "fields": {
            "requirement": "New requirement", "cron": "*/10 * * * *", "gate_command": "echo input"}})
        after = await _row(env, sid)
        assert {k: v for k, v in before.items() if k.startswith("planner_session_")} == \
            {k: v for k, v in after.items() if k.startswith("planner_session_")}
        assert await tasks.delete_task("task-holder")
        assert (await _row(env, sid))["planner_session_task_id"] is None
        assert (await _row(env, sid))["planner_session_id"] == "replacement-session"
        assert (await schedules.claim_planner_session("task-replan"))["session_id"] == "replacement-session"

    run_scheduled(test, monkeypatch, tmp_path)


def test_claimed_label_survives_reset_and_only_a_live_task_occupies_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Claim persists the storage label; reset clears the holder and not that label.

    A live task that still has the session occupies it. A failed or completed task, and a deleted one, does not.
    """
    session = "session-shared"

    async def test(env: Any) -> None:
        sid = (await env.schedule())["schedule_id"]
        label = f"planner-schedule-{sid}"
        await _task(env, sid, "task-a")
        assert await env.pool.fetchval("SELECT planner_session_label FROM tasks WHERE id = 'task-a'") is None
        assert (await schedules.claim_planner_session("task-a"))["claimed"] is True
        assert await env.pool.fetchval("SELECT planner_session_label FROM tasks WHERE id = 'task-a'") == label
        await _store("task-a", session)
        await agent_sessions.save_session("planner-task-a", session, {}, runtime_type="claude-code")
        await env.pool.execute("UPDATE tasks SET status = 'failed' WHERE id = 'task-a'")

        await _task(env, sid, "task-b")
        claimed = await schedules.claim_planner_session("task-b")
        assert claimed["claimed"] is True and claimed["session_id"] == session
        assert await env.pool.fetchval("SELECT planner_session_label FROM tasks WHERE id = 'task-b'") == label
        assert (await agent_sessions.get_session("planner-task-b"))["session_id"] == session

        await _task(env, sid, "task-denied")
        denied = await schedules.claim_planner_session("task-denied")
        assert denied == {"claimed": False, "reason": "another active task holds it"}
        assert await env.pool.fetchval("SELECT planner_session_label FROM tasks WHERE id = 'task-denied'") is None

        await _task(env, sid, "task-c")
        await agent_sessions.save_session("planner-task-c", session, {}, runtime_type="claude-code")
        await env.pool.execute(
            "UPDATE tasks SET status = 'completed', planner_session_label = $2 WHERE id = $1",
            "task-c", "planner-schedule-gone",
        )

        assert await schedules.reset_planner_session(sid) is True
        row = await _row(env, sid)
        assert row["planner_session_id"] is None and row["planner_session_task_id"] is None
        assert await env.pool.fetchval("SELECT planner_session_label FROM tasks WHERE id = 'task-a'") == label
        assert await env.pool.fetchval("SELECT planner_session_label FROM tasks WHERE id = 'task-b'") == label
        assert await env.pool.fetchval("SELECT planner_session_label FROM tasks WHERE id = 'task-c'") == \
            "planner-schedule-gone"
        assert (await agent_sessions.get_session("planner-task-b"))["session_id"] == session

        assert await agent_sessions.other_live_task_has_session(session, "task-a") is True
        assert await agent_sessions.other_live_task_has_session(session, "task-b") is False
        assert (await projects.get_task_project("task-b"))["task_planner_session_label"] == label
        assert set(await tasks.live_planner_session_labels()) == {(label, "claude-code")}

        assert await tasks.delete_task("task-b")
        assert set(await tasks.live_planner_session_labels()) == set()

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_task_can_persist_its_own_planner_session_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Giving up a shared conversation stores ``planner-<task>`` where a project read finds it."""

    async def test(env: Any) -> None:
        sid = (await env.schedule())["schedule_id"]
        await _task(env, sid, "task-a")
        await tasks.set_planner_session_label("task-a", "planner-task-a")
        assert await env.pool.fetchval(
            "SELECT planner_session_label FROM tasks WHERE id = 'task-a'") == "planner-task-a"
        assert (await projects.get_task_project("task-a"))["task_planner_session_label"] == "planner-task-a"

    run_scheduled(test, monkeypatch, tmp_path)
