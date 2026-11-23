"""Attention rows name a failed required check only while that failure is the current evidence."""
from __future__ import annotations

from looper.agents import projects as view
from looper.agents.acceptance_checks import CommandCheck
from looper.db import plans, projects, step_check_runs as runs, tasks
from tests.db_support import run_db


async def _task(pool, task_id: str, status: str) -> None:
    project = await projects.create_project(name=task_id, pool=pool)
    await tasks.create_task(task_id=task_id, requirement=task_id, project_id=project["id"], status=status, pool=pool)


async def _steps(pool, task_id: str, steps: list[dict]) -> None:
    await plans.save(task_id, steps=steps, pool=pool)


def _step(step_id: str, index: int, checks: list[CommandCheck]) -> dict:
    return {"id": step_id, "index": index, "key": step_id, "instruction": step_id,
            "acceptance_criteria": ["done"], "checks": [check.row() for check in checks]}


async def _finish(pool, task_id: str, step_id: str, check: CommandCheck, state: runs.CheckState, **extra) -> None:
    row = await runs.create_run(task_id, step_id, check.row(), 1, pool=pool)
    await runs.finish_run(row["id"], state, pool=pool, **extra)


def test_failed_checks_follow_the_current_required_evidence(monkeypatch) -> None:
    async def test(pool) -> None:
        c1, c2 = CommandCheck("c1", 1, "false"), CommandCheck("c2", 1, "false")
        advisory = CommandCheck("c-adv", 1, "false", required=False)
        erred = CommandCheck("c-err", 1, "false")
        stale = CommandCheck("c-stale", 1, "false")
        cleared = CommandCheck("c-clear", 1, "true")

        await _task(pool, "task-fail", "failed")
        await _steps(pool, "task-fail", [
            _step("step-a", 0, [c1, advisory]),
            _step("step-b", 1, [c2, erred, stale]),
        ])
        await _finish(pool, "task-fail", "step-a", c1, runs.CheckState.FAILED)
        await _finish(pool, "task-fail", "step-a", advisory, runs.CheckState.FAILED)
        await _finish(pool, "task-fail", "step-b", c2, runs.CheckState.FAILED)
        await _finish(pool, "task-fail", "step-b", erred, runs.CheckState.ERROR, error_kind=runs.ErrorKind.TIMEOUT)
        await _finish(pool, "task-fail", "step-b", stale, runs.CheckState.STALE)

        await _task(pool, "task-clear", "failed")
        await _steps(pool, "task-clear", [_step("step-clear", 0, [cleared])])
        await _finish(pool, "task-clear", "step-clear", cleared, runs.CheckState.FAILED)
        await _finish(pool, "task-clear", "step-clear", cleared, runs.CheckState.PASSED)

        await _task(pool, "task-advisory", "stopped")
        await _steps(pool, "task-advisory", [_step("step-adv", 0, [advisory])])
        await _finish(pool, "task-advisory", "step-adv", advisory, runs.CheckState.FAILED)

        await _task(pool, "task-running", "running")
        await _steps(pool, "task-running", [_step("step-run", 0, [c1])])
        await _finish(pool, "task-running", "step-run", c1, runs.CheckState.FAILED)

        await _task(pool, "task-plain", "failed")

        page = {row["task_id"]: row for row in (await view.list_tasks_page())["tasks"]}
        assert page["task-fail"]["failed_checks"] == ["c1", "c2"]
        assert page["task-fail"]["failed_check_step"] == "step-a"
        assert "c-adv" not in page["task-fail"]["failed_checks"]
        assert "c-err" not in page["task-fail"]["failed_checks"]
        assert "c-stale" not in page["task-fail"]["failed_checks"]

        quiet = [page["task-clear"], page["task-advisory"], page["task-running"], page["task-plain"]]
        assert all("failed_checks" not in row and "failed_check_step" not in row for row in quiet)
        assert set(page["task-plain"]) == set(page["task-clear"]) == set(page["task-advisory"])
        assert set(page["task-fail"]) == set(page["task-plain"]) | {"failed_checks", "failed_check_step"}

        compact = await view.list_tasks_page(compact=True, limit=20)
        assert all("failed_checks" not in row and "failed_check_step" not in row for row in compact["tasks"])
        detail = await view.task_detail("task-fail")
        assert "failed_checks" not in detail and "failed_check_step" not in detail

    run_db(test, monkeypatch)
