"""Wait steps on a real PostgreSQL: plan writes and readback, conditional state transitions, checks only while
waiting, bounded check output, and due waits only on active tasks. The schema accepts wait steps and journal
entries, and a project's wait-command policy follows its execution settings.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import pytest

from tests.db_support import DSN, run_db
from tests.test_ops_tasks_db import _project, _task

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
SPEC = {"max_wait_seconds": 3600, "not_before_seconds": 60, "not_before": None, "check": None,
        "every_seconds": None}


def _wait(step_id: str = "step-w1", state: str = "pending", **extra: Any) -> dict[str, Any]:
    return {"step_id": step_id, "topic": "CI finishes", "spec": SPEC, "state": state, "outcome": None,
            "opened_at": None, "due_at": None, "deadline_at": None, "next_check_at": None, "resolution": "",
            "extensions": 0, "failures": 0, "failed_at": None, "closed_at": None, **extra}


async def _plan_with_wait(pool: Any, task_id: str = "task-a", step_id: str = "step-w1", **wait: Any) -> None:
    from looper.db import plans

    await plans.save(task_id, plan={"plan_id": f"plan-{task_id}", "status": "executing"},
                     steps=[{"id": f"step-{task_id}-s1", "index": 0, "key": "s1", "instruction": "explore"},
                            {"id": step_id, "index": 1, "key": "w1", "instruction": "CI finishes", "kind": "wait",
                             "depends_on": [f"step-{task_id}-s1"], "status": "waiting"}],
                     waits=[_wait(step_id, **wait)], pool=pool)


def test_plans_keep_waits_and_read_them_in_plan_order(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import plans, waits

        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _plan_with_wait(pool, state="waiting", opened_at=NOW, due_at=NOW + timedelta(seconds=60),
                              deadline_at=NOW + timedelta(hours=1), next_check_at=NOW)
        stored = await plans.load("task-a", pool=pool)
        assert stored is not None and [s["kind"] for s in stored["steps"]] == ["work", "wait"]
        row = stored["waits"]["step-w1"]
        assert (row["state"], row["spec"], row["opened_at"], row["depends_on"]) == ("waiting", SPEC, NOW, ["s1"])
        assert await waits.get("task-a", "w1", pool=pool) == row
        assert await waits.get("task-a", "missing", pool=pool) is None
        await plans.save("task-a", steps=[{"id": "step-w0", "index": 0, "key": "w0", "kind": "wait",
                                           "instruction": "earlier"}], waits=[_wait("step-w0")], pool=pool)
        assert [r["step_key"] for r in await waits.list_for_task("task-a", pool=pool)] == ["w0", "w1"]

    run_db(test, monkeypatch)


def test_planner_write_expect_keeps_a_row_someone_else_moved(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import plans, waits

        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _plan_with_wait(pool, state="ready", outcome="ready")
        await plans.save("task-a", waits=[_wait(state="waiting", expect=["timed_out"])], pool=pool)
        assert (await waits.get("task-a", "w1", pool=pool))["state"] == "ready"
        await plans.save("task-a", waits=[_wait(state="done", outcome="ready", expect=["ready"],
                                                resolution="checks passed", closed_at=NOW)], pool=pool)
        row = await waits.get("task-a", "w1", pool=pool)
        assert (row["state"], row["outcome"], row["resolution"]) == ("done", "ready", "checks passed")

    run_db(test, monkeypatch)


def test_due_waits_are_waiting_due_and_in_active_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import waits

        project = await _project(pool)
        check_spec = {**SPEC, "check": {"type": "github_pr", "repo": "owner/name", "number": 12,
                                       "ready_when": "head_changed"}, "every_seconds": 300}
        later = NOW + timedelta(hours=1)
        cases = [("check", "running", "waiting", check_spec, later, NOW, later),
                 ("deadline", "running", "waiting", check_spec, later, later, NOW),
                 ("time", "running", "waiting", SPEC, NOW, later, later),
                 ("future", "running", "waiting", SPEC, later, later, later),
                 ("checked", "running", "waiting", check_spec, NOW, later, later),
                 ("stopped", "stopped", "waiting", SPEC, NOW, NOW, NOW),
                 ("ready", "running", "ready", SPEC, NOW, NOW, NOW)]
        for name, status, state, spec, due_at, next_check_at, deadline_at in cases:
            await _task(pool, name, status, project)
            await _plan_with_wait(pool, name, f"step-{name}", state=state, spec=spec, due_at=due_at,
                                  next_check_at=next_check_at, deadline_at=deadline_at,
                                  outcome="ready" if state in ("ready", "done") else None)
        rows = await waits.due(NOW, ["running"], pool=pool)
        assert {r["task_id"] for r in rows} == {"check", "deadline", "time"}
        assert all((r["work_dir"], r["project_id"], r["step_key"]) == ("/w", project["id"], "w1") for r in rows)
        assert len(await waits.due(NOW, ["running"], limit=1, pool=pool)) == 1
        assert await waits.due(NOW, [], pool=pool) == []

    run_db(test, monkeypatch)


def test_future_time_waits_do_not_fill_the_due_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.waits import Wait, WaitSpec, WaitState, open_times
        from looper.db import waits

        project = await _project(pool)
        for number in range(101):
            task_id = f"task-{number}"
            await _task(pool, task_id, "running", project)
            spec = WaitSpec("time", 3600 if number < 100 else 7200,
                            not_before_seconds=600 if number < 100 else 60)
            due_at, deadline_at, next_check_at = open_times(spec, NOW)
            row = Wait(spec, state=WaitState.WAITING, opened_at=NOW, due_at=due_at,
                       deadline_at=deadline_at, next_check_at=next_check_at).row(f"step-{number}")
            await _plan_with_wait(pool, task_id, row.pop("step_id"), **row)

        for _ in range(2):
            rows = await waits.due(NOW + timedelta(seconds=60), ["running"], pool=pool)
            assert [row["task_id"] for row in rows] == ["task-100"]

    run_db(test, monkeypatch)


def test_check_record_and_move_only_apply_in_the_expected_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import waits

        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _plan_with_wait(pool, state="waiting")
        assert await waits.record_check("step-w1", at=NOW, result="not_ready", output="pending", error="",
                                        next_check_at=NOW + timedelta(seconds=300), baseline={"head": "abc"}, pool=pool)
        assert await waits.record_check("step-w1", at=NOW, result="error", output="retry", error="exit 1",
                                        next_check_at=NOW + timedelta(seconds=600), pool=pool)
        row = await waits.get("task-a", "w1", pool=pool)
        assert (row["checks"], row["last_output"], row["last_error"], row["baseline"], row["last_checked_at"]) == (
            2, "retry", "exit 1", {"head": "abc"}, NOW)
        assert await waits.move("step-w1", to="ready", expect=["timed_out"], outcome="ready", pool=pool) is None
        assert (await waits.get("task-a", "w1", pool=pool))["state"] == "waiting"
        moved = await waits.move("step-w1", to="ready", resolution="CI passed", outcome="ready", pool=pool)
        assert (moved["state"], moved["outcome"], moved["resolution"]) == ("ready", "ready", "CI passed")
        assert await waits.move("step-w1", to="cancel_requested", pool=pool) is None
        assert not await waits.record_check("step-w1", at=NOW, result="ready", output="late", error="",
                                            next_check_at=None, baseline={"head": "late"}, pool=pool)
        row = await waits.get("task-a", "w1", pool=pool)
        assert (row["checks"], row["last_output"], row["baseline"]) == (2, "retry", {"head": "abc"})
        await waits.set_notice("step-w1", "dashboard", pool=pool)
        assert (await waits.get("task-a", "w1", pool=pool))["notice"] == "dashboard"
        assert [r["step_id"] for r in await waits.for_planner("task-a", pool=pool)] == ["step-w1"]

    run_db(test, monkeypatch)


# ``move`` asks for the task's status among the active ones, whatever the move: a status of each kind (one that ended,
# one that never started) and the moves, each once.
@pytest.mark.parametrize(("status", "to", "outcome"), [
    ("stopped", "timed_out", None), ("init", "ready", "resumed"), ("completed", "cancel_requested", None),
])
def test_wait_moves_require_an_active_task(
    monkeypatch: pytest.MonkeyPatch, status: str, to: str, outcome: str | None,
) -> None:
    async def test(pool: Any) -> None:
        from looper.db import tasks, waits

        project = await _project(pool)
        for task_id, task_status in (("inactive", status), ("active", "waiting")):
            await _task(pool, task_id, task_status, project)
            await _plan_with_wait(pool, task_id, f"step-{task_id}", state="waiting")
        before = await waits.get("inactive", "w1", pool=pool)
        assert await waits.move("step-inactive", to=to, outcome=outcome,
                                resolution="late result", pool=pool) is None
        assert await waits.get("inactive", "w1", pool=pool) == before
        assert (await tasks.get_task("inactive", pool=pool))["status"] == status
        moved = await waits.move("step-active", to=to, outcome=outcome, pool=pool)
        assert moved is not None and (moved["state"], moved["outcome"]) == (to, outcome)

    run_db(test, monkeypatch)


@pytest.mark.parametrize(("output", "expected"), [
    ("old" + "x" * 2048, "x" * 2048),
    ("等" * 1000, "\ufffd\ufffd" + "等" * 680),
    ("等" * 1000 + "x", "\ufffd" + "等" * 681 + "x"),
    ("等" * 1000 + "xy", "等" * 682 + "xy"),
    ("\ufffd" * 1000, "\ufffd" * 682),
])
def test_check_output_is_capped_to_2048_utf8_bytes(
    monkeypatch: pytest.MonkeyPatch, output: str, expected: str,
) -> None:
    async def test(pool: Any) -> None:
        from looper.db import waits

        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _plan_with_wait(pool, state="waiting")
        assert await waits.record_check("step-w1", at=NOW, result="not_ready", output=output, error="",
                                        next_check_at=NOW, pool=pool)
        row = await waits.get("task-a", "w1", pool=pool)
        assert row["last_output"] == expected
        assert len(row["last_output"].encode()) <= 2048

    run_db(test, monkeypatch)


def test_wait_step_and_journal_kinds_survive_repeated_schema_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.tiers.journal import JournalEntry, JournalKind
        from looper.db import connection, journal, plans

        await connection.ensure_schema(pool)
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _plan_with_wait(pool)
        entries = [JournalEntry("step-w1", "w1", "wait changed", kind=kind).row()
                   for kind in (JournalKind.WAIT_OPENED, JournalKind.WAIT_EXTENDED, JournalKind.WAIT_CLOSED)]
        await plans.save("task-a", journal=entries, pool=pool)
        assert [r["kind"] for r in await journal.list_entries("task-a", pool=pool)] == [
            "wait_opened", "wait_extended", "wait_closed"]

    run_db(test, monkeypatch)


def test_project_wait_commands_round_trip_through_execution_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.project_execution import ProjectExecution, execution_fields
        from looper.db import projects

        project = await projects.create_project(name="calc", execution=execution_fields({"wait_commands": "on"}),
                                                pool=pool)
        await _task(pool, "task-a", "running", project)
        execution = ProjectExecution.from_row(await projects.get_task_project("task-a", pool=pool))
        assert execution.wait_commands_on and execution.view()["wait_commands"] == "on"
        assert await projects.update_project(project["id"], execution=execution_fields({"wait_commands": "off"}),
                                              pool=pool)
        assert not ProjectExecution.from_row(await projects.get_task_project("task-a", pool=pool)).wait_commands_on
        assert await projects.update_project(project["id"], execution=execution_fields({"wait_commands": None}),
                                              pool=pool)
        row = await projects.get_project(project["id"], pool=pool)
        assert row["wait_commands"] is None and not ProjectExecution.from_row(row).wait_commands_on

    run_db(test, monkeypatch)


def test_ready_requires_an_outcome_and_other_open_states_cannot_carry_one(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import plans, waits

        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        with pytest.raises(asyncpg.CheckViolationError) as rejected:
            await _plan_with_wait(pool, state="ready")
        assert rejected.value.constraint_name == "task_waits_outcome_done"
        with pytest.raises(ValueError, match="outcome"):
            await waits.move("step-w1", to="ready", pool=pool)
        with pytest.raises(ValueError, match="outcome"):
            await waits.move("step-w1", to="ready", outcome="", pool=pool)

        for outcome in ("ready", "resumed"):
            task_id, step_id = f"task-{outcome}", f"step-{outcome}"
            await _task(pool, task_id, "running", project)
            await _plan_with_wait(pool, task_id, step_id, state="waiting")
            moved = await waits.move(step_id, to="ready", outcome=outcome, resolution=f"became {outcome}", pool=pool)
            assert moved is not None
            assert (moved["state"], moved["outcome"], moved["resolution"]) == ("ready", outcome, f"became {outcome}")

        for state in ("pending", "waiting", "timed_out", "cancel_requested", "failed", "skipped"):
            await _task(pool, f"task-{state}", "running", project)
            with pytest.raises(asyncpg.CheckViolationError) as rejected:
                await _plan_with_wait(pool, f"task-{state}", f"step-{state}", state=state, outcome="ready")
            assert rejected.value.constraint_name == "task_waits_outcome_done"
        await _task(pool, "task-done-open", "running", project)
        with pytest.raises(asyncpg.CheckViolationError) as rejected:
            await _plan_with_wait(pool, "task-done-open", "step-done-open", state="done")
        assert rejected.value.constraint_name == "task_waits_outcome_done"
        await _task(pool, "task-done", "running", project)
        await _plan_with_wait(pool, "task-done", "step-done", state="done", outcome="cancelled",
                              resolution="stopped", closed_at=NOW)
        done = await plans.load("task-done", pool=pool)
        assert done is not None and done["waits"]["step-done"]["outcome"] == "cancelled"

    run_db(test, monkeypatch)


def test_a_ready_move_race_lets_only_one_side_win(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import waits

        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _plan_with_wait(pool, state="waiting")

        async def go(outcome: str) -> dict[str, Any] | None:
            return await waits.move("step-w1", to="ready", outcome=outcome, resolution=outcome, pool=pool)

        ready, resumed = await asyncio.gather(go("ready"), go("resumed"))
        winners = [row for row in (ready, resumed) if row is not None]
        assert len(winners) == 1 and [ready, resumed].count(None) == 1
        stored = await waits.get("task-a", "w1", pool=pool)
        assert stored["state"] == "ready" and stored["outcome"] == winners[0]["outcome"]
        assert stored["outcome"] in ("ready", "resumed") and stored["resolution"] == stored["outcome"]

    run_db(test, monkeypatch)
