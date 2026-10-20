"""A task's Planner opens a wait once its dependencies are done, keeps it out of the Workers, and saves, reads
and shows it.

The Planner's Claude is a stand-in (``planner_claude``); the Workers are a stand-in pool
(``tests.test_planner_agent.Harness``); the plan and its waits are kept in memory (``agent_db.store``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import waits
from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, RestartMode
from looper.agents.tiers import planner as planner_module
from looper.agents.tiers.planner import PlannerAgent
from looper.agents.waits import (
    GithubPrCheck, ReadyWhen, Wait, WaitLimits, WaitOutcome, WaitSpec, WaitState, WaitView,
)
from tests.test_planner_agent import WORK_OK, _project, activity, always, decide, drive, plan, reply, step

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


def _wait_state(h: Any, key: str) -> Any:
    """``key``'s wait state, or None until that step exists (``until`` polls before the plan does)."""
    step = _step(h, key)
    return step.wait.state if step is not None and step.wait is not None else None


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


def _cancel(step_id: str, resolution: str) -> dict[str, str]:
    return {"id": step_id, "action": "cancel", "resolution": resolution}


def _hold_after(first: str) -> Any:
    """Answer ``first`` and keep every later Worker running."""

    def outcome(envelope: MessageEnvelope) -> dict[str, Any] | None:
        return WORK_OK if envelope.payload.get("step_key") == first else None

    return outcome


async def _wake(h: Any, kind: MessageType = MessageType.WAIT_EVENT) -> None:
    await h.bus.publish(MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=kind,
        payload={"task_id": "t1"}, correlation_id="corr-1",
    ))


def _closed(agent_db: Any, key: str) -> list[dict[str, Any]]:
    step_id = _stored_steps(agent_db)[key]["id"]
    return [entry for entry in agent_db.store.journal["t1"]
            if entry["kind"] == "wait_closed" and entry["step_id"] == step_id]


def test_a_ready_wait_verifies_itself_and_its_resolution_reaches_the_next_step(
    planner_claude: Any, agent_db: Any,
) -> None:
    resolution = "x" * 400
    claude = planner_claude(plan(wait_step("w1"), step("s2", "w1")))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)
        turns = len(claude.turns())
        _stored_wait(agent_db, "w1").update(state="ready", outcome="ready", resolution=resolution)
        await _wake(h)
        await h.until(lambda: any(item.payload.get("step_key") == "s2" for item in h.work_items))
        assert len(claude.turns()) == turns

    h = drive(claude, _hold, scenario)

    stored = _stored_wait(agent_db, "w1")
    assert (stored["state"], stored["outcome"], stored["resolution"]) == ("done", "ready", resolution)
    assert isinstance(stored["closed_at"], datetime)
    finished = _step(h, "w1")
    assert finished.status == "verified" and finished.result == {"status": "ready", "summary": resolution}
    assert finished.wait.outcome is WaitOutcome.READY and finished.wait.state is WaitState.DONE
    assert _closed(agent_db, "w1")[0]["summary"] == f"wait done (ready): {resolution}"
    assert f"Wait w1 ready: {'x' * 299}…" in activity(agent_db)
    (item,) = [entry for entry in h.work_items if entry.payload["step_key"] == "s2"]
    assert item.payload["dependencies"] == [{"step": "w1", "summary": resolution, "changed_files": []}]
    assert not any("[WAIT]" in turn["text"] for turn in claude.turns())


def test_a_resumed_wait_keeps_its_outcome_and_does_not_ask_the_model(
    planner_claude: Any, agent_db: Any,
) -> None:
    claude = planner_claude(plan(wait_step("w1"), step("s2", "w1")))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)
        turns = len(claude.turns())
        _stored_wait(agent_db, "w1").update(state="ready", outcome="resumed", resolution="the user said go")
        await _wake(h)
        await h.until(lambda: any(item.payload.get("step_key") == "s2" for item in h.work_items))
        assert len(claude.turns()) == turns

    h = drive(claude, _hold, scenario)

    stored = _stored_wait(agent_db, "w1")
    assert (stored["state"], stored["outcome"], stored["resolution"]) == ("done", "resumed", "the user said go")
    assert _step(h, "w1").result == {"status": "ready", "summary": "the user said go"}
    assert _step(h, "w1").wait.outcome is WaitOutcome.RESUMED
    assert "Wait w1 resumed by the user" in activity(agent_db)
    assert not any(line.startswith("Wait w1 ready:") for line in activity(agent_db))
    assert _closed(agent_db, "w1")[0]["summary"] == "wait done (resumed): the user said go"
    assert not any("[WAIT]" in turn["text"] for turn in claude.turns())


def test_a_finished_step_keeps_its_result_when_a_stale_ready_row_arrives(
    planner_claude: Any, agent_db: Any,
) -> None:
    claude = planner_claude(
        plan(step("s1"), wait_step("w1"), step("s2")),
        decide("accept", waits=[_cancel("w1", "no longer needed")]),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held and _stored_steps(agent_db).get("w1", {}).get("status") == "verified")
        assert _step(h, "w1").result == {"status": "cancelled", "summary": "Cancelled: no longer needed"}
        assert len(_closed(agent_db, "w1")) == 1
        _stored_wait(agent_db, "w1").update(state="ready", outcome="ready", resolution="CI passed late")
        await _wake(h)
        await h.until(lambda: _stored_wait(agent_db, "w1")["state"] == "done"
                      and _stored_wait(agent_db, "w1")["resolution"] == "CI passed late")

    h = drive(claude, _hold_after("s1"), scenario)

    assert _step(h, "w1").result == {"status": "cancelled", "summary": "Cancelled: no longer needed"}
    assert _step(h, "w1").status == "verified"
    stored = _stored_wait(agent_db, "w1")
    assert (stored["state"], stored["outcome"], stored["resolution"]) == ("done", "ready", "CI passed late")
    assert len(_closed(agent_db, "w1")) == 1
    assert _closed(agent_db, "w1")[0]["summary"] == "wait done (cancelled): no longer needed"
    assert not any(line.startswith("Wait w1 ready:") for line in activity(agent_db))


def test_a_decision_cancels_a_pending_wait_before_the_replan_drops_it(
    planner_claude: Any, agent_db: Any,
) -> None:
    claude = planner_claude(
        plan(step("s1"), wait_step("w1", "s1"), step("s2", "s1")),
        decide("replan", reason="the wait is moot", steps=[step("s3")],
               waits=[_cancel("w1", "s1 made it unnecessary")]),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _stored_steps(agent_db).get("w1", {}).get("status") == "verified")

    h = drive(claude, _hold_after("s1"), scenario)

    stored = _stored_wait(agent_db, "w1")
    assert (stored["state"], stored["outcome"], stored["resolution"]) == ("done", "cancelled", "s1 made it unnecessary")
    assert isinstance(stored["closed_at"], datetime)
    assert _stored_steps(agent_db)["w1"]["status"] == "verified"
    assert _stored_steps(agent_db)["w1"]["result"] == {
        "status": "cancelled", "summary": "Cancelled: s1 made it unnecessary"}
    assert _stored_steps(agent_db)["s1"]["status"] == "verified"
    assert _stored_steps(agent_db)["s2"]["status"] == "skipped"
    assert "s3" in _stored_steps(agent_db)
    assert _closed(agent_db, "w1")[0]["summary"] == "wait done (cancelled): s1 made it unnecessary"
    assert "Wait w1 cancelled: s1 made it unnecessary" in activity(agent_db)
    assert h.final() is None


def test_cancelling_a_gate_does_not_skip_cancelling_a_wait(
    planner_claude: Any, agent_db: Any,
) -> None:
    agent_db.get_task_project.return_value = {**_project(), "user_gates": "on"}
    claude = planner_claude(
        plan(step("s1"), GATE, wait_step("w1")),
        decide("accept", gates=[{"id": "g1", "action": "cancel", "resolution": "s1 answered"}],
               waits=[_cancel("w1", "s1 answered the wait")]),
    )
    h = drive(claude, always(WORK_OK))

    final = h.final()
    assert final is not None and final.payload["status"] == "completed"
    gate = next(iter(agent_db.store.gates["t1"].values()))
    assert gate["state"] == "cancelled"
    stored = _stored_wait(agent_db, "w1")
    assert (stored["state"], stored["outcome"]) == ("done", "cancelled")
    assert _stored_steps(agent_db)["w1"]["status"] == "verified"
    assert _stored_steps(agent_db)["w1"]["result"]["summary"] == "Cancelled: s1 answered the wait"


def test_a_chat_reply_cancels_a_pending_wait_before_it_replans(
    planner_claude: Any, agent_db: Any,
) -> None:
    claude = planner_claude(
        plan(step("s1"), wait_step("w1", "s1")),
        reply({"reply": "dropping it", "action": "replan", "steps": [step("s3")],
               "waits": [_cancel("w1", "chat dropped it")]}),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held and _step(h, "w1").wait.state is WaitState.PENDING)
        await h.say("drop the wait")
        await h.until(lambda: _stored_steps(agent_db).get("w1", {}).get("status") == "verified")
        # stop() kills whatever is still running, so the worker has to still be held here.
        assert h.held and _step(h, "s1").status == "dispatched"

    h = drive(claude, _hold, scenario)

    assert _step(h, "s1").status == "dispatched"
    stored = _stored_wait(agent_db, "w1")
    assert (stored["state"], stored["outcome"], stored["resolution"]) == ("done", "cancelled", "chat dropped it")
    assert _stored_steps(agent_db)["w1"]["status"] == "verified"
    assert _stored_steps(agent_db)["w1"]["result"]["summary"] == "Cancelled: chat dropped it"


def test_a_restart_keeps_an_open_wait_and_cancels_a_pending_one_before_the_replan(
    planner_claude: Any, agent_db: Any,
) -> None:
    failed_at = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)
    claude = planner_claude(
        plan(step("s1"), wait_step("w_open"), wait_step("w_pending", "s1")),
        reply({"reply": "go on", "action": "replan", "steps": [step("s3")],
               "waits": [_cancel("w_pending", "restart dropped it")]}),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held and _step(h, "w_open").wait.state is WaitState.WAITING)
        stored = _stored_wait(agent_db, "w_open")
        deadline = stored["deadline_at"]
        stored["failures"], stored["failed_at"] = 2, failed_at
        await h.planner.stop()
        agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir)}
        h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
        await h.planner.start(h.bus)
        await h.bus.publish(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=MessageType.TASK_REQUEST,
            payload={"requirement": "build the calc package", "goal_id": "g1", "work_dir": str(h.work_dir),
                     "project_id": "proj-1", "restart": RestartMode.CONTINUE.value, "ended": "stopped"},
            correlation_id="corr-1"))
        await h.until(lambda: _stored_steps(agent_db).get("w_pending", {}).get("status") == "verified")
        opened = _step(h, "w_open").wait
        assert opened.state is WaitState.WAITING and opened.deadline_at == deadline
        assert opened.failures == 0 and opened.failed_at is None

    h = drive(claude, _hold, scenario)

    opened = _stored_wait(agent_db, "w_open")
    assert opened["state"] == "waiting" and opened["failures"] == 0 and opened["failed_at"] is None
    assert _stored_steps(agent_db)["w_open"]["status"] == "waiting"
    assert (_stored_wait(agent_db, "w_pending")["state"], _stored_wait(agent_db, "w_pending")["outcome"]) == (
        "done", "cancelled")
    assert _stored_steps(agent_db)["w_pending"]["status"] == "verified"
    open_id = _stored_steps(agent_db)["w_open"]["id"]
    assert not any(entry["step_id"] == open_id and "put it back to not started" in entry["summary"]
                   for entry in agent_db.store.journal["t1"])
    assert h.planner._plan is not None  # the restarted planner is the one the scenario left running


def test_a_room_reply_cancels_a_pending_wait_before_it_replans(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending: list[dict[str, Any]] = []

    async def planner_pending(task_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in pending]

    async def planner_delivered(task_id: str, ids: list[int]) -> None:
        pending.clear()

    room = SimpleNamespace(planner_reply=AsyncMock(return_value={"id": 9}), wake_planner_after=Mock())
    claude = planner_claude(
        plan(step("s1"), wait_step("w1", "s1")),
        reply({"reply": "dropped", "action": "replan", "steps": [step("s3")],
               "waits": [_cancel("w1", "the room ended it")]}),
    )

    async def scenario(h: Any) -> None:
        # The room is on without a platform token: start() only issues one when ops is set.
        monkeypatch.setattr(PlannerAgent, "_room", property(lambda self: room))
        monkeypatch.setattr(planner_module.db_room, "planner_pending", planner_pending)
        monkeypatch.setattr(planner_module.db_room, "planner_delivered", planner_delivered)
        await h.start()
        await h.until(lambda: h.held and _step(h, "w1").wait.state is WaitState.PENDING)
        pending.append({"id": 1, "sender_seat": "worker@s1", "kind": "question", "body": "stop waiting",
                        "audience_all": True})
        await _wake(h, MessageType.ROOM_MESSAGE)
        await h.until(lambda: _stored_steps(agent_db).get("w1", {}).get("status") == "verified")

    h = drive(claude, _hold, scenario)

    assert _step(h, "s1").status == "dispatched"
    stored = _stored_wait(agent_db, "w1")
    assert (stored["state"], stored["outcome"], stored["resolution"]) == ("done", "cancelled", "the room ended it")
    assert _stored_steps(agent_db)["w1"]["status"] == "verified"


def test_a_replan_skips_a_pending_wait_it_drops(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(
        plan(step("s1"), wait_step("w1", "s1"), wait_step("w2")),
        decide("replan", reason="the pending wait is moot", steps=[step("s3")]),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _stored_steps(agent_db).get("w1", {}).get("status") == "skipped")

    h = drive(claude, _hold_after("s1"), scenario)

    assert _step(h, "w1").wait.state is WaitState.SKIPPED
    assert _stored_wait(agent_db, "w1")["state"] == "skipped"
    assert _stored_wait(agent_db, "w1")["outcome"] is None
    assert _step(h, "w2").wait.state is WaitState.WAITING
    assert _stored_steps(agent_db)["w2"]["status"] == "waiting"


def test_claude_stops_when_the_task_only_waits_and_stays_while_work_runs(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(planner_module, "GATE_IDLE_STOP_SEC", 0)
    monkeypatch.setattr(planner_module, "GATE_IDLE_CHECK_SEC", 0.05)
    claude = planner_claude(plan(wait_step("w1")))
    stopped = "Planner Claude Code stopped while the task only waits; the next turn resumes its session"

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: stopped in activity(agent_db))

    drive(claude, _hold, scenario)

    assert len(claude.argvs()) == 1


def test_claude_stays_while_a_step_runs_and_a_wait_is_open(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(planner_module, "GATE_IDLE_STOP_SEC", 0)
    monkeypatch.setattr(planner_module, "GATE_IDLE_CHECK_SEC", 0.02)
    claude = planner_claude(plan(step("s1"), wait_step("w1")))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held and _step(h, "w1").wait.state is WaitState.WAITING)
        await asyncio.sleep(0.3)

    drive(claude, _hold, scenario)

    assert not any("stopped while the task only waits" in line for line in activity(agent_db))
    assert not any("stopped while the task waits" in line for line in activity(agent_db))
    assert len(claude.argvs()) == 1


@pytest.mark.parametrize("state", ["timed_out", "cancel_requested"])
def test_a_wait_that_is_not_ready_is_left_for_later(planner_claude: Any, agent_db: Any, state: str) -> None:
    claude = planner_claude(plan(wait_step("w1")))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)
        turns = len(claude.turns())
        _stored_wait(agent_db, "w1").update(state=state)
        await _wake(h)
        await asyncio.sleep(0.05)
        assert len(claude.turns()) == turns

    h = drive(claude, _hold, scenario)

    assert _stored_wait(agent_db, "w1")["state"] == state
    assert _step(h, "w1").wait.state is WaitState.WAITING
    assert _step(h, "w1").status == "waiting"
    assert not any("[WAIT]" in turn["text"] for turn in claude.turns())


def test_a_wait_read_that_fails_is_logged_and_the_wait_stays(
    planner_claude: Any, agent_db: Any, caplog: Any,
) -> None:
    claude = planner_claude(plan(wait_step("w1")))
    agent_db.for_planner.side_effect = RuntimeError("db down")

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)
        await _wake(h)
        await asyncio.sleep(0.05)

    with caplog.at_level(logging.WARNING, logger="looper.agents.tiers.planner"):
        drive(claude, _hold, scenario)

    assert _stored_wait(agent_db, "w1")["state"] == "waiting"
    assert "cannot read its waits" in caplog.text


def test_a_ready_row_for_an_unknown_step_is_skipped(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(plan(wait_step("w1"), step("s2", "w1")))
    original = agent_db.for_planner.side_effect

    async def rows(task_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        found = await original(task_id, **kwargs)
        return [{"step_id": "step-missing", "state": "ready", "outcome": "ready", "resolution": "ghost"}, *found]

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)
        agent_db.for_planner.side_effect = rows
        _stored_wait(agent_db, "w1").update(state="ready", outcome="ready", resolution="real")
        await _wake(h)
        await h.until(lambda: any(item.payload.get("step_key") == "s2" for item in h.work_items))

    h = drive(claude, _hold, scenario)

    assert _stored_wait(agent_db, "w1")["state"] == "done"
    assert _step(h, "w1").result == {"status": "ready", "summary": "real"}
    assert not any(item.payload.get("step_key") == "ghost" for item in h.work_items)
