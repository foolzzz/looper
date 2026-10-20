"""A task's Planner opens a wait once its dependencies are done, keeps it out of the Workers, and saves, reads
and shows it.

The Planner's Claude is a stand-in (``planner_claude``); the Workers are a stand-in pool
(``tests.test_planner_agent.Harness``); the plan and its waits are kept in memory (``agent_db.store``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from looper.agents import waits
from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.tiers.planner import PlannerAgent
from looper.agents.waits import (
    GithubPrCheck, ReadyWhen, Wait, WaitLimits, WaitSpec, WaitState, WaitView,
)
from tests.test_planner_agent import WORK_OK, _project, activity, always, decide, drive, plan, step

TOPIC = "Author pushes fixes"
CHECK = {"type": "github_pr", "repo": "owner/name", "number": 12, "ready_when": "head_changed"}
DESCRIBE = "github_pr owner/name#12 head_changed every 10 min; at most 3 d"
MAX_WAIT = 259200
TIME_DESCRIBE = "after 1 h; at most 1 d"
GATE = {"id": "g1", "kind": "gate", "topic": "storage", "depends_on": [],
        "agenda": [{"question": "Which database?", "options": ["PostgreSQL", "SQLite"]}]}


def wait_step(step_id: str = "w1", *depends_on: str, **extra: Any) -> dict[str, Any]:
    row = {"id": step_id, "kind": "wait", "topic": TOPIC, "depends_on": list(depends_on),
           "max_wait_seconds": MAX_WAIT, "check": dict(CHECK)}
    row.update(extra)
    return row


def time_wait(step_id: str, *depends_on: str) -> dict[str, Any]:
    return {"id": step_id, "kind": "wait", "topic": TOPIC, "depends_on": list(depends_on),
            "max_wait_seconds": 86400, "not_before_seconds": 3600}


def _hold(envelope: MessageEnvelope) -> None:
    """Keep every Worker running."""
    return None


def _goals(h: Any) -> list[MessageEnvelope]:
    return [e for e in h.statuses if e.message_type == MessageType.GOAL_STATUS]


def _goal_statuses(h: Any) -> list[str]:
    return [e.payload["status"] for e in _goals(h)]


def _task_plan(h: Any) -> Any:
    plan_ = h.planner._plan
    assert plan_ is not None
    return plan_


def _step(h: Any, key: str) -> Any:
    plan_ = h.planner._plan
    if plan_ is None:
        return None
    return next((s for s in plan_.steps if s.label == key), None)


def _stored_steps(agent_db: Any) -> dict[str, dict[str, Any]]:
    return {row["key"]: row for row in agent_db.store.steps.get("t1", {}).values()}


def _stored_wait(agent_db: Any, key: str) -> dict[str, Any]:
    step_id = _stored_steps(agent_db)[key]["id"]
    return agent_db.store.waits["t1"][step_id]


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _checked_spec() -> WaitSpec:
    return WaitSpec(TOPIC, MAX_WAIT, check=GithubPrCheck("owner/name", 12, ReadyWhen.HEAD_CHANGED),
                    every_seconds=600)


def test_a_wait_opens_when_its_dependencies_are_done_and_other_work_runs(
    planner_claude: Any, agent_db: Any,
) -> None:
    claude = planner_claude(plan(step("s1"), step("s2"), wait_step("w1", "s1")), decide("accept"))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: [e.payload["step_key"] for e in h.held] == ["s1", "s2"] and _goals(h))
        assert _step(h, "w1").wait.state is WaitState.PENDING
        assert all("等待中" not in e.payload["progress"] for e in _goals(h))
        assert _goals(h)[-1].payload["status"] == "executing"

        await h.release(WORK_OK)
        await h.until(lambda: _step(h, "w1").wait.state is WaitState.WAITING and len(h.held) == 1
                      and any("等待中" in e.payload["progress"] for e in _goals(h)))

    h = drive(claude, _hold, scenario)

    opened = _step(h, "w1").wait
    assert opened.spec == _checked_spec()
    assert opened.deadline_at - opened.opened_at == timedelta(seconds=MAX_WAIT)
    assert opened.due_at == opened.next_check_at == opened.opened_at
    stored_step = _stored_steps(agent_db)["w1"]
    assert (stored_step["kind"], stored_step["status"], stored_step["review"]) == ("wait", "waiting", False)
    assert stored_step["acceptance_criteria"] == [] and stored_step["instruction"] == TOPIC
    stored = _stored_wait(agent_db, "w1")
    assert stored["spec"]["every_seconds"] == 600
    assert [e.payload["step_key"] for e in h.work_items] == ["s1", "s2"]
    (entry,) = [e for e in agent_db.store.journal["t1"] if e["kind"] == "wait_opened"]
    assert entry["summary"] == f"wait opened: {TOPIC} ({DESCRIBE})"
    assert f"Wait w1 opened: {TOPIC} ({DESCRIBE})" in activity(agent_db)
    last = _goals(h)[-1]
    assert last.payload["status"] == "executing"
    assert last.payload["progress"] == "1/3 完成, 1 执行中, 1 等待中"


def test_a_wait_is_not_dispatched_or_reviewed_and_takes_no_worker_slot(
    planner_claude: Any, agent_db: Any,
) -> None:
    agent_db.get_task_project.return_value = {**_project(), "review_policy": "all", "step_workspaces": "worktree"}
    claude = planner_claude(plan(step("s1"), step("s2"), wait_step("w1")))
    rounds: list[list[str]] = []
    dispatched: list[str] = []

    async def scenario(h: Any) -> None:
        real_round = h.planner._plan_round
        real_dispatch = h.planner._dispatch_step

        async def plan_round(plan_: Any, ready: list[Any]) -> Any:
            rounds.append([s.label for s in ready])
            return await real_round(plan_, ready)

        async def dispatch_step(plan_: Any, step_: Any, *, base: Any = None, same_attempt: bool = False) -> None:
            dispatched.append(step_.label)
            await real_dispatch(plan_, step_, base=base, same_attempt=same_attempt)

        h.planner._plan_round = plan_round
        h.planner._dispatch_step = dispatch_step
        await h.start()
        await h.until(lambda: len(h.work_items) == 2 and any(line.startswith("Wait w1 opened:")
                                                             for line in activity(agent_db)))

    h = drive(claude, _hold, scenario)

    assert rounds == [["s1", "s2"]] and dispatched == ["s1", "s2"]
    assert [item.payload["step_key"] for item in h.work_items] == ["s1", "s2"]
    assert h.reviews == []
    stored = _stored_steps(agent_db)
    assert {key: row["review"] for key, row in stored.items()} == {"s1": True, "s2": True, "w1": False}
    assert stored["w1"]["kind"] == "wait" and stored["w1"]["instruction"] == TOPIC
    assert stored["w1"]["acceptance_criteria"] == []
    notes = activity(agent_db)
    assert sum(line.startswith("Wait w1 opened:") for line in notes) == 1
    assert sum(line.startswith("Worker dispatched") for line in notes) == 2


def test_a_wait_is_saved_and_read_back_with_its_state(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(plan(wait_step("w1")))
    checked_at = datetime(2026, 10, 19, tzinfo=timezone.utc)
    seen: dict[str, Any] = {}

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: (found := _step(h, "w1")) is not None and found.wait is not None
                      and found.wait.state is WaitState.WAITING)
        wait = _step(h, "w1").wait
        stored = _stored_wait(agent_db, "w1")
        assert stored["state"] == "waiting" and stored["topic"] == TOPIC and stored["outcome"] is None
        assert stored["resolution"] == "" and stored["extensions"] == 0 and stored["failures"] == 0
        assert stored["closed_at"] is None and stored["failed_at"] is None and "expect" not in stored
        assert isinstance(stored["opened_at"], datetime) and stored["opened_at"] == wait.opened_at
        assert stored["due_at"] == stored["next_check_at"] == stored["opened_at"]
        assert stored["deadline_at"] - stored["opened_at"] == timedelta(seconds=MAX_WAIT)
        assert stored["spec"]["every_seconds"] == 600 and stored["spec"]["check"] == CHECK
        assert stored["spec"]["max_wait_seconds"] == MAX_WAIT and stored["spec"]["not_before"] is None

        stored.update(state="ready", baseline={"head": "abc"}, checks=2, last_checked_at=checked_at,
                      last_result="not_ready", last_output="open", last_error="", notice="quiet")
        wait.resolution = "should not land"
        await h.planner._save(_task_plan(h), _step(h, "w1"), expect={_step(h, "w1").step_id: ["waiting"]})
        stored = _stored_wait(agent_db, "w1")
        assert stored["state"] == "ready" and stored["resolution"] == "" and stored["checks"] == 2
        assert stored["notice"] == "quiet" and stored["baseline"] == {"head": "abc"} and "expect" not in stored

        await h.planner._save(_task_plan(h), _step(h, "w1"))
        stored = _stored_wait(agent_db, "w1")
        assert stored["state"] == "waiting" and stored["resolution"] == "should not land"
        assert stored["checks"] == 2 and stored["last_result"] == "not_ready" and stored["last_output"] == "open"
        assert stored["last_error"] == "" and stored["notice"] == "quiet" and stored["baseline"] == {"head": "abc"}
        assert isinstance(stored["last_checked_at"], datetime) and isinstance(stored["opened_at"], datetime)
        assert "expect" not in stored
        seen["spec"] = wait.spec
        seen["opened_at"] = wait.opened_at
        seen["due_at"] = wait.due_at
        seen["deadline_at"] = wait.deadline_at
        seen["next_check_at"] = wait.next_check_at

        await h.planner.stop()
        agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir)}
        h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
        await h.planner.start(h.bus)
        await h.bus.publish(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=MessageType.TASK_RESUME,
            payload={"task_id": "t1"}, correlation_id="corr-1",
        ))
        await h.until(lambda: any(line.startswith("Planner resumed") for line in activity(agent_db)))
        loaded = _step(h, "w1").wait
        assert loaded == Wait(seen["spec"], state=WaitState.WAITING, opened_at=seen["opened_at"],
                              due_at=seen["due_at"], deadline_at=seen["deadline_at"],
                              next_check_at=seen["next_check_at"], resolution="should not land")
        assert [e["kind"] for e in agent_db.store.journal["t1"]].count("wait_opened") == 1

    drive(claude, _hold, scenario)


def test_a_missing_wait_row_is_pending_and_warned(planner_claude: Any, agent_db: Any, caplog: Any) -> None:
    original = waits._limits
    claude = planner_claude(plan(wait_step("w1")))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _stored_steps(agent_db).get("w1", {}).get("status") == "waiting")
        await h.planner.stop()
        agent_db.store.waits["t1"].clear()
        waits._limits = WaitLimits(max_wait_seconds=12345)
        h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
        await h.planner.start(h.bus)
        loaded = _step(h, "w1")
        assert loaded.status == "pending" and loaded.wait.state is WaitState.PENDING
        assert loaded.wait.spec == WaitSpec(TOPIC, 12345)
        assert any("wait step w1 has no saved row; keeping it pending" in r.getMessage() for r in caplog.records)

    try:
        with caplog.at_level(logging.WARNING, logger="looper.agents.tiers.planner"):
            drive(claude, _hold, scenario)
    finally:
        waits._limits = original


def test_plan_lines_show_a_pending_wait_and_an_open_one(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(plan(step("s1"), wait_step("w_open"), time_wait("w_pending", "s1")), decide("accept"))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held and _step(h, "w_open").wait.state is WaitState.WAITING)
        opened = _step(h, "w_open")
        pending = _step(h, "w_pending")
        stamp = _stamp(opened.wait.deadline_at)
        lines = h.planner._plan_lines(_task_plan(h))
        assert f"- w_open [wait waiting: {DESCRIBE}, deadline {stamp}]" in lines
        assert f"- w_pending [wait, not opened: {TIME_DESCRIBE}]" in lines
        shown = _task_plan(h).view().waits
        assert shown["w_open"] == WaitView(WaitState.WAITING) and shown["w_pending"] == WaitView(WaitState.PENDING)
        assert "s1" not in shown and opened.started and not pending.started
        await h.release(WORK_OK)
        await h.until(lambda: any(t["text"].startswith("[DECISION]") for t in claude.turns()))

    drive(claude, _hold, scenario)

    decision = next(t["text"] for t in claude.turns() if t["text"].startswith("[DECISION]"))
    assert f"[wait waiting: {DESCRIBE}, deadline " in decision
    assert f"- w_pending [wait, not opened: {TIME_DESCRIBE}]" in decision


def test_only_open_waits_report_waiting(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(plan(wait_step("w1"), wait_step("w2")))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _goal_statuses(h)[-1:] == ["waiting"])

    h = drive(claude, _hold, scenario)

    assert _goal_statuses(h)[-1] == "waiting"
    assert _goals(h)[-1].payload["progress"] == "0/2 完成, 2 等待中"
    assert "waiting_user" not in _goal_statuses(h)
    assert h.work_items == []


def test_a_running_step_keeps_the_plan_status_while_waits_are_open(
    planner_claude: Any, agent_db: Any,
) -> None:
    claude = planner_claude(plan(step("s1"), wait_step("w1"), wait_step("w2"), time_wait("w3", "s1")))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held and _goals(h) and "等待中" in _goals(h)[-1].payload["progress"])

    h = drive(claude, _hold, scenario)

    assert _goal_statuses(h)[-1] == "executing"
    assert "waiting" not in _goal_statuses(h)
    assert _goals(h)[-1].payload["progress"] == "0/4 完成, 1 执行中, 2 等待中"
    assert [s.label for s in _task_plan(h).open_waits()] == ["w1", "w2"]


def test_a_gate_waiting_for_the_user_wins_over_an_open_wait(planner_claude: Any, agent_db: Any) -> None:
    agent_db.get_task_project.return_value = {**_project(), "user_gates": "on"}
    claude = planner_claude(plan(step("s1"), GATE, wait_step("w1")))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held and _goals(h) and "等待中" in _goals(h)[-1].payload["progress"])

    h = drive(claude, _hold, scenario)

    assert _goal_statuses(h)[-1] == "waiting_user"
    assert "waiting" not in _goal_statuses(h)
    assert _goals(h)[-1].payload["progress"] == "0/3 完成, 1 执行中, 1 等待用户, 1 等待中"


def test_a_failed_task_names_an_open_wait_as_not_over(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(plan(step("s1"), wait_step("w1")), decide("fail", reason="cannot go on"))

    h = drive(claude, always(WORK_OK))

    final = h.final()
    assert final is not None and final.payload["status"] == "failed"
    assert "- w1 (a wait not over)" in final.payload["summary"]
