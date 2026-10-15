"""Every agent the Chat page can show: the Manager, each task's Planner and its step Workers."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import chat_targets
from looper.agents.chat_targets import build_chat_targets, list_chat_targets
from looper.agents.tiers.planner import PlannerAgent
from looper.agents.tiers.task import Task, TaskRouter

T0 = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)


def _task(task_id: str, minutes: int, status: str = "completed") -> dict[str, Any]:
    return {"id": task_id, "requirement": f"do {task_id}", "name": f"{task_id} title", "status": status,
            "created_at": T0 + timedelta(minutes=minutes)}


def _step(task_id: str, index: int, worker_id: str | None) -> dict[str, Any]:
    return {"id": f"{task_id}-s{index}", "task_id": task_id, "step_index": index,
            "instruction": f"step {index} of {task_id}", "worker_id": worker_id}


def _run(agent_id: str, task_id: str, minutes: int, status: str = "completed",
         step_id: str | None = None) -> dict[str, Any]:
    return {"agent_id": agent_id, "task_id": task_id, "status": status,
            "created_at": T0 + timedelta(minutes=minutes), "step_id": step_id}


def _ids(targets: list[dict[str, Any]]) -> list[str]:
    return [t["id"] for t in targets]


class TestBuildChatTargets:
    def test_manager_first_then_tasks_newest_first(self) -> None:
        targets = build_chat_targets(
            tasks=[_task("t1", 0), _task("t2", 5)], live_planners=[], steps=[], runs=[],
        )
        assert _ids(targets) == ["manager", "planner-t2", "planner-t1"]
        assert targets[0] == {"kind": "manager", "id": "manager", "can_send": True}

    def test_only_a_live_planner_takes_messages(self) -> None:
        targets = build_chat_targets(
            tasks=[_task("t1", 0, "running"), _task("t2", 5, "stopped")],
            live_planners=[{"task_id": "t1", "planner_id": "planner-t1",
                            "requirement": "do t1", "name": "t1 title", "status": "running"}],
            steps=[], runs=[],
        )
        planners = {t["task_id"]: t for t in targets if t["kind"] == "planner"}
        assert planners["t1"]["can_send"] is True
        assert planners["t2"]["can_send"] is False
        assert planners["t2"] == {"kind": "planner", "id": "planner-t2", "task_id": "t2",
                                  "requirement": "do t2", "name": "t2 title", "status": "stopped",
                                  "can_send": False}

    def test_each_step_lists_its_own_worker_and_reviewer(self) -> None:
        targets = build_chat_targets(
            tasks=[_task("t1", 0)],
            live_planners=[],
            steps=[_step("t1", 1, "worker-b"), _step("t1", 0, "worker-a")],
            runs=[
                _run("worker-a", "t1", 1),
                _run("reviewer-a", "t1", 2, step_id="t1-s0"),
                _run("worker-b", "t1", 3, status="running"),
                _run("reviewer-x", "t1", 4),  # its step is not known
            ],
        )
        assert _ids(targets) == ["manager", "planner-t1", "worker-a", "reviewer-a", "worker-b", "reviewer-x"]
        by_id = {t["id"]: t for t in targets}
        assert by_id["worker-a"] == {
            "kind": "worker", "id": "worker-a", "task_id": "t1", "status": "completed",
            "step_index": 0, "instruction": "step 0 of t1",
            "started_at": T0 + timedelta(minutes=1), "can_send": False,
        }
        assert (by_id["reviewer-a"]["kind"], by_id["reviewer-a"]["step_index"]) == ("reviewer", 0)
        assert (by_id["worker-b"]["status"], by_id["worker-b"]["step_index"]) == ("running", 1)
        assert (by_id["reviewer-x"]["step_index"], by_id["reviewer-x"]["instruction"]) == (None, "")

    def test_retried_step_keeps_the_earlier_worker(self) -> None:
        # task_steps holds only the latest worker; the run's own step id places the first one.
        targets = build_chat_targets(
            tasks=[_task("t1", 0)],
            live_planners=[],
            steps=[_step("t1", 0, "worker-new")],
            runs=[_run("worker-old", "t1", 1, status="failed", step_id="t1-s0"),
                  _run("worker-new", "t1", 2)],
        )
        workers = [t for t in targets if t["kind"] == "worker"]
        assert [(w["id"], w["step_index"], w["status"]) for w in workers] == [
            ("worker-old", 0, "failed"), ("worker-new", 0, "completed"),
        ]

    def test_agent_with_several_runs_is_listed_once_with_its_latest_status(self) -> None:
        targets = build_chat_targets(
            tasks=[_task("t1", 0)], live_planners=[], steps=[_step("t1", 0, "worker-a")],
            runs=[_run("worker-a", "t1", 1, status="failed"), _run("worker-a", "t1", 4, status="running")],
        )
        workers = [t for t in targets if t["kind"] == "worker"]
        assert len(workers) == 1
        assert workers[0]["status"] == "running"
        assert workers[0]["started_at"] == T0 + timedelta(minutes=1)


class TestListChatTargets:
    def _router(self, tmp: str) -> TaskRouter:
        router = TaskRouter(runtime_root=Path(tmp))
        router._planners = {"t1": PlannerAgent(task_id="t1", runtime_root=Path(tmp))}
        return router

    def test_reads_tasks_steps_and_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        async def list_tasks(*, limit: int) -> list[dict[str, Any]]:
            seen["limit"] = limit
            return [_task("t2", 0)]

        async def list_steps_for_tasks(task_ids: list[str]) -> list[dict[str, Any]]:
            seen["steps"] = sorted(task_ids)
            return [_step("t2", 0, "worker-a")]

        async def list_task_runs(task_ids: list[str]) -> list[dict[str, Any]]:
            seen["runs"] = sorted(task_ids)
            return [_run("worker-a", "t2", 1)]

        monkeypatch.setattr(chat_targets.db_tasks, "list_tasks", list_tasks)
        monkeypatch.setattr(chat_targets.db_steps, "list_steps_for_tasks", list_steps_for_tasks)
        monkeypatch.setattr(chat_targets.llm_records, "list_task_runs", list_task_runs)
        with tempfile.TemporaryDirectory() as tmp:
            targets = asyncio.run(list_chat_targets(self._router(tmp)))

        # The live task t1 is looked up too, though it is not among the recent rows.
        assert seen == {"limit": chat_targets.RECENT_TASKS, "steps": ["t1", "t2"], "runs": ["t1", "t2"]}
        assert _ids(targets) == ["manager", "planner-t1", "planner-t2", "worker-a"]

    def test_database_failure_still_lists_live_agents(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def broken(**_: Any) -> list[dict[str, Any]]:
            raise ConnectionError("database is down")

        monkeypatch.setattr(chat_targets.db_tasks, "list_tasks", broken)
        with tempfile.TemporaryDirectory() as tmp:
            router = self._router(tmp)
            # A live Planner's entry comes from its task; a queued task has no Planner to chat with.
            router._tasks = {
                "t1": Task(task_id="t1", requirement="build it", goal_id="", work_dir="", project_id="proj-1",
                           status="running", planner_id="planner-t1"),
                "t2": Task(task_id="t2", requirement="later", goal_id="", work_dir="", project_id="proj-1"),
            }
            targets = asyncio.run(list_chat_targets(router))

        assert _ids(targets) == ["manager", "planner-t1"]
        assert targets[1]["can_send"] is True
        assert (targets[1]["requirement"], targets[1]["name"], targets[1]["status"]) == ("build it", "build it",
                                                                                         "running")
