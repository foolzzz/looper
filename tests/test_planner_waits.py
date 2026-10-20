"""A task's Planner opens a wait once its dependencies are done, keeps it out of the Workers, and saves, reads
and shows it. A wait that timed out or that the user cancelled is a [WAIT] turn: extend, proceed or fail.

The Planner's Claude is a stand-in (``planner_claude``); the Workers are a stand-in pool
(``tests.test_planner_agent.Harness``); the plan and its waits are kept in memory (``agent_db.store``).

A scheduled run's stop time is read once per event. These tests pin that clock, so a boundary is
not one second early or late.
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
from looper.agents.message import (
    TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, RestartMode, channel_address,
)
from looper.agents.tiers import planner as planner_module
from looper.agents.tiers.planner import PlannerAgent, PlannerTurnError
from looper.agents.tiers.planner_protocol import TurnKind
from looper.agents.waits import (
    GithubPrCheck, ReadyWhen, Wait, WaitLimits, WaitOutcome, WaitSpec, WaitState, WaitView,
)
from tests.test_planner_agent import (
    FINAL, FINAL_RESULT, WORK_OK, _project, activity, always, decide, drive, plan, reply, step,
)

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


@pytest.mark.parametrize(("state", "outcome"), [
    ("ready", "resumed"), ("cancel_requested", None), ("timed_out", None),
])
def test_restart_preserves_wait_moves_after_loading_the_plan(
    planner_claude: Any, agent_db: Any, state: str, outcome: str | None,
) -> None:
    claude = planner_claude(plan(wait_step("w1")))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)
        await h.planner.stop()
        agent_db.store.tasks["t1"] = {"requirement": "build the calc package", "work_dir": str(h.work_dir)}
        h.planner = PlannerAgent(task_id="t1", runtime_root=h.work_dir.parent, claude_bin=claude.bin)
        await h.planner.start(h.bus)
        refresh = h.planner._refresh_project
        observed = []

        async def race(plan_: Any) -> None:
            await refresh(plan_)
            # The restart holds a waiting snapshot; a user or the loop moves the persisted row meanwhile.
            assert plan_.steps[0].wait.state is WaitState.WAITING
            _stored_wait(agent_db, "w1").update(state=state, outcome=outcome, resolution="concurrent move")

        async def ask(*args: Any, **kwargs: Any) -> None:
            stored = _stored_wait(agent_db, "w1")
            observed.append((stored["state"], stored["outcome"], stored["resolution"]))
            assert observed[-1] == (state, outcome, "concurrent move")

        h.planner._refresh_project = race
        h.planner._ask = ask
        await h.planner._restart_plan(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address, message_type=MessageType.TASK_REQUEST,
            payload={"ended": "stopped"}, correlation_id="corr-1"))
        assert len(observed) == 1

    drive(claude, _hold, scenario)


def test_opening_a_command_wait_keeps_its_command_out_of_activity_and_journal(
    planner_claude: Any, agent_db: Any,
) -> None:
    secret = "PRIVATE_COMMAND_TOKEN"
    command = f"test -n {secret}"
    agent_db.get_task_project.return_value = {**_project(), "wait_commands": "on"}
    claude = planner_claude(plan(wait_step(check={"type": "command", "command": command})))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING
                      and any(line.startswith("Wait w1 opened:") for line in activity(agent_db)))

    drive(claude, _hold, scenario)
    assert _stored_wait(agent_db, "w1")["spec"]["check"]["command"] == command
    assert secret not in "\n".join(activity(agent_db))
    assert secret not in "\n".join(entry["summary"] for entry in agent_db.store.journal["t1"])


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


# --- A timed-out or cancelled wait is one [WAIT] turn ---


def _wait_reply(action: str, answer: str = "going on", *, chat: str = "none", **fields: Any) -> str:
    """A [WAIT] reply. ``max_wait_seconds``, ``resolution`` and ``reason`` belong to the wait; the rest is CHAT."""
    wait = {"action": action}
    for key in ("max_wait_seconds", "resolution", "reason"):
        if key in fields:
            wait[key] = fields.pop(key)
    return reply({"reply": answer, "action": chat, "wait": wait, **fields})


def _arm(agent_db: Any, key: str, state: str, **fields: Any) -> None:
    """Move the stored wait ``key`` to ``state`` the way the loop or the user would, without telling the Planner."""
    _stored_wait(agent_db, key).update(state=state, **fields)


def _wait_turns(claude: Any) -> list[str]:
    """The [WAIT] decision turns (a repair turn starts with ``[WAIT]`` too, but not ``[WAIT] Wait``)."""
    return [turn["text"] for turn in claude.turns() if turn["text"].startswith("[WAIT] Wait ")]


def _entries(agent_db: Any, kind: str, key: str | None = None) -> list[dict[str, Any]]:
    rows = agent_db.store.journal.get("t1", [])
    if key is not None:
        step_id = _stored_steps(agent_db)[key]["id"]
        rows = [entry for entry in rows if entry.get("step_id") == step_id]
    return [entry for entry in rows if entry["kind"] == kind]


async def _open_then(h: Any, agent_db: Any, key: str, state: str, **fields: Any) -> None:
    """Wait until ``key`` is waiting, then move its row and wake the Planner."""
    await h.start()
    await h.until(lambda: _wait_state(h, key) is WaitState.WAITING)
    _arm(agent_db, key, state, **fields)
    await _wake(h)


def test_a_timed_out_wait_is_extended(planner_claude: Any, agent_db: Any) -> None:
    answer = "R" * 350
    claude = planner_claude(plan(wait_step("w1")), _wait_reply("extend", answer, max_wait_seconds=86400))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)
        goals = len(_goals(h))
        # Two recorded failures, and the second automatic retry (300 s) is already due, so this event decides.
        _arm(agent_db, "w1", "timed_out", failures=2,
             failed_at=datetime.now(timezone.utc) - timedelta(seconds=301))
        await _wake(h)
        await h.until(lambda: _stored_wait(agent_db, "w1")["state"] == "waiting"
                      and _stored_wait(agent_db, "w1")["extensions"] == 1)
        assert len(_goals(h)) > goals

    drive(claude, _hold, scenario)

    stored = _stored_wait(agent_db, "w1")
    assert stored["failures"] == 0 and stored["failed_at"] is None
    assert abs((stored["deadline_at"] - stored["next_check_at"]).total_seconds() - 86400) < 2
    assert abs((stored["next_check_at"] - datetime.now(timezone.utc)).total_seconds()) < 30
    assert _stored_steps(agent_db)["w1"]["status"] == "waiting"
    assert _entries(agent_db, "wait_extended", "w1")[0]["summary"].startswith(f"wait extended by 1 d: {answer[:20]}")
    lines = activity(agent_db)
    assert f"Planner on wait w1 (extend): {'R' * 299}…" in lines
    assert "Wait w1 extended by 1 d" in lines
    message = _wait_turns(claude)[0]
    assert message.startswith(
        '[WAIT] Wait w1 "Author pushes fixes" timed out: it waited 3 d (max_wait_seconds 259200) '
        "and its check never reported ready.")
    assert "Allowed actions: none, add_steps, replan." in message
    assert "forward (to a running step)" not in message


def test_proceeding_starts_the_steps_that_depend_on_the_wait(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(
        plan(wait_step("w1"), step("s2", "w1")),
        _wait_reply("proceed", "the author will not push", resolution="head will not change"),
    )

    async def scenario(h: Any) -> None:
        await _open_then(h, agent_db, "w1", "timed_out")
        await h.until(lambda: any(item.payload.get("step_key") == "s2" for item in h.work_items))
        return h

    h = drive(claude, _hold, scenario)

    stored = _stored_wait(agent_db, "w1")
    assert (stored["state"], stored["outcome"], stored["resolution"]) == ("done", "proceeded", "head will not change")
    assert stored["closed_at"] is not None
    assert _stored_steps(agent_db)["w1"]["status"] == "verified"
    assert _step(h, "w1").result == {"status": "proceeded", "summary": "head will not change"}
    assert _closed(agent_db, "w1")[-1]["summary"] == "wait done (proceeded): head will not change"
    assert "Planner on wait w1 (proceed): the author will not push" in activity(agent_db)
    report = next(item.payload for item in h.work_items if item.payload.get("step_key") == "s2")
    assert report["dependencies"] == [{"step": "w1", "summary": "head will not change", "changed_files": []}]


def test_failing_a_wait_fails_the_task_with_the_reason(planner_claude: Any, agent_db: Any) -> None:
    reason = "the pull request was closed"
    claude = planner_claude(plan(wait_step("w1")), _wait_reply("fail", "stopping", reason=reason))

    async def scenario(h: Any) -> None:
        await _open_then(h, agent_db, "w1", "timed_out")
        await h.finished()

    h = drive(claude, _hold, scenario)

    stored = _stored_wait(agent_db, "w1")
    assert stored["state"] == "failed" and stored["outcome"] is None and stored["closed_at"] is not None
    assert _stored_steps(agent_db)["w1"]["status"] == "failed"
    assert _step(h, "w1").result == {"status": "failed", "summary": reason, "error": reason}
    assert _closed(agent_db, "w1")[-1]["summary"] == f"wait failed: {reason}"
    assert not _entries(agent_db, "step_failed")
    final = h.final()
    assert final is not None and final.payload["status"] == "failed"
    assert f"Wait w1: {reason}" in final.payload["summary"]
    assert f"- w1 (failed): failed: {reason}" in final.payload["summary"]
    assert f"Task failed: Wait w1: {reason}" in activity(agent_db)
    assert "Planner on wait w1 (fail): stopping" in activity(agent_db)


def test_extending_a_cancelled_wait_is_repaired(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(
        plan(wait_step("w1")),
        _wait_reply("extend", "more time", max_wait_seconds=86400),
        _wait_reply("fail", "stopping", reason="the user cancelled"),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)
        h.planner._plan.origin_channel_id = channel_address("lark").agent_id
        _arm(agent_db, "w1", "cancel_requested", notice="sent")
        await _wake(h)
        await h.finished()

    h = drive(claude, _hold, scenario)

    message = _wait_turns(claude)[0]
    assert message.startswith(
        '[WAIT] Wait w1 "Author pushes fixes": the user cancelled this wait from the dashboard.')
    assert "The user was told" not in message and "The task page shows it." not in message
    repair = next(turn["text"] for turn in claude.turns() if "broke the reply contract" in turn["text"])
    assert "the user cancelled this wait: proceed, fail, or change the plan" in repair
    assert h.final() is not None and h.final().payload["status"] == "failed"


def test_proceeding_on_the_last_open_step_needs_a_final_summary(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(
        plan(wait_step("w1")),
        _wait_reply("proceed", "go", resolution="done waiting"),
        _wait_reply("proceed", "go", resolution="done waiting", final=FINAL),
    )

    async def scenario(h: Any) -> None:
        await _open_then(h, agent_db, "w1", "timed_out")
        await h.finished()

    h = drive(claude, _hold, scenario)

    message = _wait_turns(claude)[0]
    assert ('This wait is the task\'s last open step: proceeding without adding steps completes the task, '
            'and then the reply must carry "final", the task\'s final summary.') in message
    repair = next(turn["text"] for turn in claude.turns() if "broke the reply contract" in turn["text"])
    assert 'proceeding without adding steps completes the task, so the reply must carry "final"' in repair
    final = h.final()
    assert final is not None and final.payload["status"] == "completed" and final.payload["summary"] == FINAL_RESULT


def test_a_final_summary_is_refused_while_another_step_is_open(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(
        plan(wait_step("w1"), step("s2", "w1")),
        _wait_reply("proceed", "go", resolution="go on", final=FINAL),
        _wait_reply("proceed", "go", resolution="go on"),
    )

    async def scenario(h: Any) -> None:
        await _open_then(h, agent_db, "w1", "timed_out")
        await h.until(lambda: any(item.payload.get("step_key") == "s2" for item in h.work_items))

    h = drive(claude, _hold, scenario)

    assert "last open step" not in _wait_turns(claude)[0]
    repair = next(turn["text"] for turn in claude.turns() if "broke the reply contract" in turn["text"])
    assert "final is only for proceeding when this wait is the last open step and no steps are added" in repair
    assert h.final() is None
    assert _stored_steps(agent_db)["s2"]["status"] == "dispatched"


def test_steps_added_as_a_wait_proceeds_are_saved_with_it(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import plans

    claude = planner_claude(
        plan(wait_step("w1")),
        _wait_reply("proceed", "adding", resolution="need one more", chat="add_steps", steps=[step("s2")]),
    )
    save = plans.save
    saves: list[dict[str, Any]] = []

    async def recorded(task_id: str, **kwargs: Any) -> None:
        saves.append(kwargs)
        await save(task_id, **kwargs)

    monkeypatch.setattr(plans, "save", recorded)

    async def scenario(h: Any) -> None:
        await _open_then(h, agent_db, "w1", "timed_out")
        await h.until(lambda: "s2" in _stored_steps(agent_db))

    drive(claude, _hold, scenario)

    matches = [item for item in saves
               if any(wait.get("state") == "done" and wait.get("outcome") == "proceeded"
                      for wait in item.get("waits") or ())
               and any(row.get("key") == "s2" for row in item.get("steps") or ())]
    assert len(matches) == 1
    (close,) = matches
    assert close["waits"][0]["expect"] == ["timed_out"]
    assert {row["key"] for row in close["steps"]} == {"w1", "s2"}
    assert [entry["kind"] for entry in close["journal"]] == ["wait_closed", "decision"]


def test_an_empty_replan_after_proceeding_completes_the_task(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(
        plan(wait_step("w1"), step("s2", "w1")),
        _wait_reply("proceed", "drop the rest", resolution="nothing else to do", chat="replan", steps=[],
                    final=FINAL),
    )

    async def scenario(h: Any) -> None:
        await _open_then(h, agent_db, "w1", "timed_out")
        await h.finished()

    h = drive(claude, _hold, scenario)

    assert _stored_steps(agent_db)["s2"]["status"] == "skipped"
    assert _stored_wait(agent_db, "w1")["outcome"] == "proceeded"
    final = h.final()
    assert final is not None and final.payload["status"] == "completed" and final.payload["summary"] == FINAL_RESULT


def test_a_wait_reply_can_forward_to_a_running_step(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(
        plan(step("s1"), wait_step("w1")),
        _wait_reply("extend", "still waiting", max_wait_seconds=86400, chat="forward", step="s1",
                    text="look at the new comment"),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held and _wait_state(h, "w1") is WaitState.WAITING)
        _arm(agent_db, "w1", "timed_out")
        await _wake(h)
        await h.until(lambda: h.forwarded)

    h = drive(claude, _hold, scenario)

    message = _wait_turns(claude)[0]
    assert "Allowed actions: none, add_steps, replan, forward (to a running step)." in message
    assert "last open step" not in message
    assert h.forwarded[-1].payload["text"] == (
        "A message from the user, passed on by the Planner while you work on this step. "
        "Take it into account, then continue:\n\nlook at the new comment")
    assert any("look at the new comment" in line for line in activity(agent_db))
    assert _stored_wait(agent_db, "w1")["state"] == "waiting"
    assert _stored_wait(agent_db, "w1")["extensions"] == 1


def test_a_failed_wait_turn_retries_twice_then_again_on_a_later_event(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(waits, "RETRY_WAIT_SEC", (0.4, 0.4))
    monkeypatch.setattr(planner_module, "GATE_IDLE_CHECK_SEC", 0.3)
    broken = reply({"reply": "hmm", "action": "none"})
    claude = planner_claude(plan(wait_step("w1")), *[broken] * 8)
    seen: list[str] = []

    async def scenario(h: Any) -> None:
        publish = h.bus.publish

        async def spy(envelope: MessageEnvelope, *args: Any, **kwargs: Any) -> Any:
            if envelope.message_type is MessageType.SYSTEM_EVENT:
                seen.append(str(envelope.payload.get("type")))
            return await publish(envelope, *args, **kwargs)

        h.bus.publish = spy
        await _open_then(h, agent_db, "w1", "timed_out")
        await h.until(lambda: _stored_wait(agent_db, "w1").get("failures") == 1)
        await _wake(h)
        await asyncio.sleep(0.05)
        assert _stored_wait(agent_db, "w1")["failures"] == 1
        await h.until(lambda: _stored_wait(agent_db, "w1").get("failures") == 3)
        stalled = len(claude.turns())
        await asyncio.sleep(0.5)
        assert _stored_wait(agent_db, "w1")["failures"] == 3 and len(claude.turns()) == stalled
        await _wake(h)
        await h.until(lambda: _stored_wait(agent_db, "w1").get("failures") == 4)

    drive(claude, _hold, scenario)

    stored = _stored_wait(agent_db, "w1")
    assert stored["state"] == "timed_out" and stored["failed_at"] is not None
    assert _stored_steps(agent_db)["w1"]["status"] == "waiting"
    reason = "invalid reply after a repair turn: wait must be an object"
    for number in range(1, 5):
        assert f"Planner could not decide on wait w1 (failure {number}): {reason}" in activity(agent_db)
    assert "gate_retry" in seen and "wait_retry" not in seen


def test_a_failed_wait_turn_does_not_save_when_the_wait_moved(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import plans

    claude = planner_claude(plan(wait_step("w1")), reply({"reply": "unused", "action": "none"}))
    save = plans.save
    saves: list[dict[str, Any]] = []
    original = PlannerAgent._ask

    async def ask(self: PlannerAgent, kind: TurnKind, message: str, parse: Any, run: Any = None) -> Any:
        if kind is TurnKind.WAIT:
            _stored_wait(agent_db, "w1")["state"] = "waiting"
            raise PlannerTurnError("nope")
        return await original(self, kind, message, parse, run)

    async def recorded(task_id: str, **kwargs: Any) -> None:
        saves.append(kwargs)
        await save(task_id, **kwargs)

    monkeypatch.setattr(PlannerAgent, "_ask", ask)
    monkeypatch.setattr(plans, "save", recorded)

    async def scenario(h: Any) -> None:
        await _open_then(h, agent_db, "w1", "timed_out")
        await h.until(lambda: any("could not decide on wait w1" in line for line in activity(agent_db)))

    drive(claude, _hold, scenario)

    stored = _stored_wait(agent_db, "w1")
    assert stored["state"] == "waiting" and int(stored.get("failures") or 0) == 0
    assert "Planner could not decide on wait w1 (failure 1): nope" in activity(agent_db)
    assert any(wait.get("expect") == ["timed_out"] for item in saves for wait in item.get("waits") or ())


_EXTEND_RULE = ('- wait "extend": wait max_wait_seconds more from now (1 to 604800); the check goes on. '
                "Not after the user cancelled.")
_PROCEED_RULE = ('- wait "proceed": stop waiting; the steps that depend on it start now. '
                 '"resolution" (required) says why; they read it.')
_FAIL_RULE = '- wait "fail": the task cannot go on; "reason" (required).'
_GATE_RULE = '- To ask the user first, proceed and add a gate (action "replan") that the waiting steps depend on.'
_ACTION_RULE = ('- action: as in [CHAT]. "final" as in [DECISION], when the message says proceeding without new '
                "steps completes the task.")
_WAIT_JSON = '{"reply": "...", "action": "none", "wait": {"action": "extend", "max_wait_seconds": 86400}}'
_LAST_OPEN = ('This wait is the task\'s last open step: proceeding without adding steps completes the task, '
              'and then the reply must carry "final", the task\'s final summary.')


def test_the_wait_message_reports_a_checked_timeout(planner_claude: Any, agent_db: Any) -> None:
    output = "HEAD\n" + ("x" * 3000) + "\nTAIL"
    claude = planner_claude(
        plan(step("s1"), wait_step("w1", "s1"), step("s2", "w1")),
        decide("accept"),
        _wait_reply("extend", "more time", max_wait_seconds=86400),
    )
    checked = datetime(2026, 10, 19, 9, 10, tzinfo=timezone.utc)
    seen: dict[str, Any] = {}

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)
        seen["deadline"] = _step(h, "w1").wait.deadline_at
        h.planner._plan.origin_channel_id = channel_address("lark").agent_id
        _arm(agent_db, "w1", "timed_out", checks=431, last_checked_at=checked, last_result="not_ready",
             last_output=output, last_error="disk full\nstill full", notice="sent")
        await _wake(h)
        await h.until(lambda: _wait_turns(claude))

    drive(claude, always(WORK_OK), scenario)
    deadline = seen["deadline"]

    message = _wait_turns(claude)[0]
    order = [
        '[WAIT] Wait w1 "Author pushes fixes" timed out: it waited 3 d (max_wait_seconds 259200) '
        "and its check never reported ready.",
        "Check: github_pr owner/name#12, ready_when head_changed, every 600 s; 431 checks, "
        "the last at 09:10 UTC: not ready.",
        "Last output (capped):",
        "Last error: disk full\nstill full",
        "The user was told in their Lark chat.",
        f"- w1 [wait timed_out: {DESCRIBE}, deadline {_stamp(deadline)}]",
        "Running now: none",
        "Progress and findings",
        "Steps waiting for it: s2.",
        'Reply in the CHAT shape plus "wait":',
        _WAIT_JSON,
        _EXTEND_RULE,
        _PROCEED_RULE,
        _FAIL_RULE,
        _GATE_RULE,
        _ACTION_RULE,
        "Allowed actions: none, add_steps, replan.",
        "Reply with the WAIT JSON.",
    ]
    at = [message.index(part) for part in order]
    assert at == sorted(at)
    capped = message.split("Last output (capped): ", 1)[1].split("\nLast error:", 1)[0]
    assert "HEAD" not in message and capped.endswith("\nTAIL") and "\n" in capped
    assert len(capped.encode("utf-8")) <= 2048
    progress = message.split("Progress and findings", 1)[1].split("Steps waiting for it:", 1)[0]
    assert "did it" in progress
    assert _LAST_OPEN not in message
    assert claude.prompts()
    assert all(_WAIT_JSON not in prompt for prompt in claude.prompts())


def test_the_wait_message_for_a_timeout_without_a_check(planner_claude: Any, agent_db: Any) -> None:
    claude = planner_claude(plan(time_wait("w1")), _wait_reply("extend", "more time", max_wait_seconds=86400))

    async def scenario(h: Any) -> None:
        await _open_then(h, agent_db, "w1", "timed_out")
        await h.until(lambda: _stored_wait(agent_db, "w1")["extensions"] == 1)

    drive(claude, _hold, scenario)

    message = _wait_turns(claude)[0]
    assert message.startswith(
        '[WAIT] Wait w1 "Author pushes fixes" timed out: it waited 1 d (max_wait_seconds 86400) '
        "and its time passed.")
    assert "Check:" not in message and "Last output" not in message and "Last error" not in message
    assert "The user was told" not in message and "The task page shows it." not in message
    assert _stored_wait(agent_db, "w1")["next_check_at"] is None


def test_the_wait_message_describes_a_command_check(
    planner_claude: Any, agent_db: Any,
) -> None:
    command = {"id": "w1", "kind": "wait", "topic": TOPIC, "depends_on": [], "max_wait_seconds": 3600,
               "every_seconds": 300, "check": {"type": "command", "command": "test -f ready"}}
    claude = planner_claude(plan(command), _wait_reply("extend", "more time", max_wait_seconds=3600))
    agent_db.get_task_project.return_value = {**_project(), "wait_commands": "on"}

    async def scenario(h: Any) -> None:
        await _open_then(h, agent_db, "w1", "timed_out", checks=2,
                         last_checked_at=datetime(2026, 10, 19, 9, 10, tzinfo=timezone.utc), last_result="error")
        await h.until(lambda: _wait_turns(claude))

    drive(claude, _hold, scenario)

    message = _wait_turns(claude)[0]
    assert message.startswith(
        '[WAIT] Wait w1 "Author pushes fixes" timed out: it waited 1 h (max_wait_seconds 3600) '
        "and its check never reported ready.")
    assert ("Check: command `test -f ready`, every 300 s; 2 checks, the last at 09:10 UTC: error." in message)


@pytest.mark.parametrize(("notice", "channel", "expected"), [
    ("sent", "web", "The user was told."),
    ("sent", "telegram", "The user was told in their Telegram chat."),
    ("dashboard", "web", "The task page shows it."),
    ("failed", "web", "Telling the user failed. The task page shows it."),
])
def test_the_wait_message_says_where_the_timeout_notice_went(
    planner_claude: Any, agent_db: Any, notice: str, channel: str, expected: str,
) -> None:
    claude = planner_claude(plan(time_wait("w1")), _wait_reply("extend", "more time", max_wait_seconds=86400))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)
        h.planner._plan.origin_channel_id = channel_address(channel).agent_id
        _arm(agent_db, "w1", "timed_out", notice=notice)
        await _wake(h)
        await h.until(lambda: _wait_turns(claude))

    drive(claude, _hold, scenario)

    message = _wait_turns(claude)[0]
    assert expected in message
    if notice != "sent":
        assert "The user was told" not in message


@pytest.mark.parametrize("project", [{"user_gates": "off"}, {"task_schedule_id": "sched-1"}])
def test_the_wait_message_omits_the_gate_hint_when_gates_are_not_allowed(
    planner_claude: Any, agent_db: Any, project: dict[str, Any],
) -> None:
    claude = planner_claude(plan(wait_step("w1")), _wait_reply("extend", "more time", max_wait_seconds=86400))
    agent_db.get_task_project.return_value = {**_project(), **project}

    async def scenario(h: Any) -> None:
        await _open_then(h, agent_db, "w1", "timed_out")
        await h.until(lambda: _stored_wait(agent_db, "w1")["extensions"] == 1)

    drive(claude, _hold, scenario)

    message = _wait_turns(claude)[0]
    assert _GATE_RULE not in message
    assert _EXTEND_RULE in message and _PROCEED_RULE in message and _FAIL_RULE in message
    assert "Reply with the WAIT JSON." in message


# --- A scheduled run's timeout caps every check of a wait ---


_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
_FIRED = _NOW - timedelta(hours=1)
_RUN_DEADLINE = _FIRED + timedelta(hours=2)  # 13:00 UTC; one hour left at _NOW
_RUN = {"run_timeout_seconds": 2 * 3600, "fired_at": _FIRED}
_REFUSAL = (
    "this task is a scheduled run that is stopped at 2026-10-19 13:00 UTC (its run timeout): "
    "a wait may last at most 3600 s from now; wait less, or decide without waiting"
)


class _Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def advance(self, **delta: float) -> None:
        self.at += timedelta(**delta)


def _freeze(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    from looper.agents.tiers import planner_protocol

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return clock.at if tz is None else clock.at.astimezone(tz)

    monkeypatch.setattr(planner_module, "datetime", Frozen)
    monkeypatch.setattr(planner_protocol, "datetime", Frozen)


def _running(monkeypatch: pytest.MonkeyPatch, row: Any) -> list[str]:
    calls: list[str] = []

    async def running_run_of_task(task_id: str, *, pool: Any = None) -> Any:
        calls.append(task_id)
        if isinstance(row, BaseException):
            raise row
        return row

    monkeypatch.setattr("looper.db.schedules.running_run_of_task", running_run_of_task)
    return calls


def _scheduled(agent_db: Any, on: bool = True) -> None:
    project = _project()
    if on:
        project["task_schedule_id"] = "sched-1"
    agent_db.get_task_project.return_value = project


def _repairs(claude: Any) -> list[str]:
    return [turn["text"] for turn in claude.turns() if "broke the reply contract" in turn["text"]]


def _run_line(seconds: int) -> str:
    return (f"This scheduled run is stopped at 2026-10-19 13:00 UTC (its run timeout): "
            f"extend by at most {seconds} s.")


def test_run_deadline_refuses_a_plan_past_the_run_and_accepts_the_repair(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(_NOW)
    _freeze(monkeypatch, clock)
    calls = _running(monkeypatch, _RUN)
    _scheduled(agent_db)
    claude = planner_claude(
        plan(wait_step("w1", max_wait_seconds=3 * 3600)),
        plan(wait_step("w1", max_wait_seconds=1800)),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)

    drive(claude, _hold, scenario)

    assert calls == ["t1"]  # the repair is the same event, so the run is not read again
    assert _repairs(claude) == [f"[PLAN] Your last reply broke the reply contract: step w1: {_REFUSAL}\n"
                                 "Answer the same request again. End with exactly one ```json block in the PLAN shape."]
    stored = _stored_wait(agent_db, "w1")
    assert stored["spec"]["max_wait_seconds"] == 1800
    assert stored["deadline_at"] - stored["opened_at"] == timedelta(seconds=1800)
    assert stored["deadline_at"] <= _RUN_DEADLINE


def test_run_deadline_caps_a_wait_opened_after_the_plan(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thirty minutes was legal when the plan was checked. Opening fifty-five minutes later still stops with the run,
    and the earliest time stays where the wait put it."""
    clock = _Clock(_NOW)
    _freeze(monkeypatch, clock)
    calls = _running(monkeypatch, dict(_RUN))
    _scheduled(agent_db)
    claude = planner_claude(
        plan(step("s1"), wait_step("w1", "s1", max_wait_seconds=1800, not_before_seconds=600)),
        decide("accept"),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held and _wait_state(h, "w1") is WaitState.PENDING)
        assert calls == ["t1"]
        clock.advance(minutes=55)
        await h.release(WORK_OK)
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)
        assert calls == ["t1", "t1"]  # the next event reads the run again

    drive(claude, _hold, scenario)

    opened = _stored_wait(agent_db, "w1")
    assert opened["opened_at"] == _NOW + timedelta(minutes=55)
    assert opened["due_at"] == opened["opened_at"] + timedelta(seconds=600)
    assert opened["due_at"] > _RUN_DEADLINE  # the earliest time is not pulled back to the run
    assert opened["next_check_at"] == opened["opened_at"]
    assert opened["deadline_at"] == _RUN_DEADLINE
    assert opened["spec"]["not_before_seconds"] == 600
    assert not _repairs(claude)


def test_run_deadline_refuses_a_long_extend_and_caps_a_late_one(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(_NOW)
    _freeze(monkeypatch, clock)
    calls = _running(monkeypatch, dict(_RUN))
    _scheduled(agent_db)
    original = PlannerAgent._close_wait

    async def close_wait(self: PlannerAgent, plan_: Any, step_: Any, reply_: Any, trigger: Any) -> None:
        # The extend was checked at _NOW. Writing it happens after the clock has moved.
        clock.advance(minutes=50)
        await original(self, plan_, step_, reply_, trigger)

    monkeypatch.setattr(PlannerAgent, "_close_wait", close_wait)
    claude = planner_claude(
        plan(wait_step("w1", max_wait_seconds=1800)),
        _wait_reply("extend", "too long", max_wait_seconds=3 * 3600),
        _wait_reply("extend", "a bit longer", max_wait_seconds=1200),
    )
    due: dict[str, datetime] = {}

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)
        assert calls == ["t1"]
        due["at"] = _step(h, "w1").wait.due_at
        due["opened"] = _step(h, "w1").wait.opened_at
        _arm(agent_db, "w1", "timed_out")
        await _wake(h)
        await h.until(lambda: _stored_wait(agent_db, "w1")["extensions"] == 1)
        assert calls == ["t1", "t1"]  # the repair parses twice and still reads the run once

    drive(claude, _hold, scenario)

    assert _REFUSAL in _repairs(claude)[0]
    stored = _stored_wait(agent_db, "w1")
    assert stored["state"] == "waiting" and stored["extensions"] == 1
    assert stored["due_at"] == due["at"] == due["opened"]
    assert stored["deadline_at"] == _RUN_DEADLINE
    assert stored["next_check_at"] == _NOW + timedelta(minutes=50)
    assert stored["spec"]["max_wait_seconds"] == 1800


@pytest.mark.parametrize("action", ["add_steps", "replan"])
def test_run_deadline_reaches_a_chat_wait(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    clock = _Clock(_NOW)
    _freeze(monkeypatch, clock)
    _running(monkeypatch, dict(_RUN))
    _scheduled(agent_db)
    long = wait_step("w1", max_wait_seconds=3 * 3600)
    short = wait_step("w1", max_wait_seconds=1800)
    claude = planner_claude(
        plan(step("s1")),
        reply({"reply": "wait for the author", "action": action, "steps": [long]}),
        reply({"reply": "wait for the author", "action": action, "steps": [short]}),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held)
        await h.say("please wait for the author")
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)

    drive(claude, _hold, scenario)

    assert _repairs(claude) and _REFUSAL in _repairs(claude)[0]
    assert _stored_wait(agent_db, "w1")["spec"]["max_wait_seconds"] == 1800


def test_run_deadline_reaches_a_decision_replan(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(_NOW)
    _freeze(monkeypatch, clock)
    _running(monkeypatch, dict(_RUN))
    _scheduled(agent_db)
    long = wait_step("w1", max_wait_seconds=3 * 3600)
    short = wait_step("w1", max_wait_seconds=1800)
    claude = planner_claude(
        plan(step("s1")),
        decide("replan", reason="wait for the author", steps=[long]),
        decide("replan", reason="wait for the author", steps=[short]),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held)
        await h.release(WORK_OK)
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)

    drive(claude, _hold, scenario)

    assert _repairs(claude) and _REFUSAL in _repairs(claude)[0]
    assert _stored_wait(agent_db, "w1")["spec"]["max_wait_seconds"] == 1800


def test_run_deadline_is_named_in_the_wait_message(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seconds left are measured when the message is built, not when the event first read the run."""
    clock = _Clock(_NOW)
    _freeze(monkeypatch, clock)
    calls = _running(monkeypatch, dict(_RUN))
    _scheduled(agent_db)
    original = PlannerAgent._wait_turn
    reread: list[int] = []

    async def wait_turn(self: PlannerAgent, plan_: Any, step_: Any, row: Any) -> None:
        before = len(calls)
        clock.advance(minutes=10)  # the decision in this same event already read the run
        await original(self, plan_, step_, row)
        reread.append(len(calls) - before)

    monkeypatch.setattr(PlannerAgent, "_wait_turn", wait_turn)
    claude = planner_claude(
        plan(step("s1"), wait_step("w1", max_wait_seconds=1800)),
        decide("accept"),
        _wait_reply("extend", "a little longer", max_wait_seconds=60),
    )

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: h.held and _wait_state(h, "w1") is WaitState.WAITING)
        assert calls == ["t1"]
        _arm(agent_db, "w1", "timed_out", checks=4,
             last_checked_at=datetime(2026, 10, 19, 11, 0, tzinfo=timezone.utc),
             last_result="not_ready", last_output="still waiting", last_error="disk full", notice="dashboard")
        await h.release(WORK_OK)
        await h.until(lambda: _stored_wait(agent_db, "w1")["extensions"] == 1)

    drive(claude, _hold, scenario)

    assert calls == ["t1", "t1"] and reread == [0]
    message = _wait_turns(claude)[0]
    line = _run_line(3000)
    order = [
        '[WAIT] Wait w1 "Author pushes fixes" timed out:',
        "Check: github_pr owner/name#12, ready_when head_changed",
        "Last output (capped): still waiting",
        "Last error: disk full",
        "The task page shows it.",
        line,
        "Plan:",
    ]
    at = [message.index(part) for part in order]
    assert at == sorted(at)
    assert message.index(line) < message.index("\nPlan:")


def test_run_deadline_is_named_when_the_wait_has_no_check(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(_NOW)
    _freeze(monkeypatch, clock)
    _running(monkeypatch, dict(_RUN))
    _scheduled(agent_db)
    bare = {"id": "w1", "kind": "wait", "topic": TOPIC, "depends_on": [],
            "max_wait_seconds": 1800, "not_before_seconds": 60}
    claude = planner_claude(plan(bare), _wait_reply("extend", "more time", max_wait_seconds=60))

    async def scenario(h: Any) -> None:
        await _open_then(h, agent_db, "w1", "timed_out")
        await h.until(lambda: _wait_turns(claude))

    drive(claude, _hold, scenario)

    lines = _wait_turns(claude)[0].splitlines()
    assert lines[0] == ('[WAIT] Wait w1 "Author pushes fixes" timed out: it waited 30 min '
                        "(max_wait_seconds 1800) and its time passed.")
    assert lines[1] == _run_line(3600)
    assert lines[2] == ""
    assert lines[3] == "Plan:"
    assert "Check:" not in lines[1]


@pytest.mark.parametrize(("scheduled", "row", "reads"), [
    (False, {"run_timeout_seconds": 7200, "fired_at": _FIRED}, 0),
    (True, None, 1),
    (True, {"run_timeout_seconds": None, "fired_at": _FIRED}, 1),
    (True, {"run_timeout_seconds": 7200, "fired_at": None}, 1),
    (True, RuntimeError("db down"), 1),
], ids=["not-scheduled", "no-run", "no-timeout", "no-fired-at", "db-error"])
def test_run_deadline_is_empty_when_the_run_does_not_limit_it(
    planner_claude: Any, agent_db: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any,
    scheduled: bool, row: Any, reads: int,
) -> None:
    clock = _Clock(_NOW)
    _freeze(monkeypatch, clock)
    calls = _running(monkeypatch, row)
    _scheduled(agent_db, scheduled)
    claude = planner_claude(plan(wait_step("w1", max_wait_seconds=3 * 3600)))

    async def scenario(h: Any) -> None:
        await h.start()
        await h.until(lambda: _wait_state(h, "w1") is WaitState.WAITING)

    with caplog.at_level(logging.WARNING, logger="looper.agents.tiers.planner"):
        drive(claude, _hold, scenario)

    assert len(calls) == reads
    assert not _repairs(claude)
    stored = _stored_wait(agent_db, "w1")
    assert stored["spec"]["max_wait_seconds"] == 3 * 3600
    assert stored["deadline_at"] - stored["opened_at"] == timedelta(hours=3)
    if isinstance(row, BaseException):
        assert "cannot read its scheduled run" in caplog.text
    else:
        assert "cannot read its scheduled run" not in caplog.text
