"""``GET /api/gates``: the user gates of all tasks (gates-page RFC D1-D6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tests.test_ops_tasks_db import _project, _task
from tests.test_usage_api_db import DSN, _run
from tests.test_user_gates_db import _api, _gate, _gate_step, _user

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

NOW = datetime.now(timezone.utc)


async def _gates(pool: Any, task_id: str, *states: str, opened_at: datetime | None = None) -> None:
    """Save a plan for ``task_id`` with one gate per state, ``<task>-g<i>`` / key ``g<i>`` (``g1`` the first)."""
    from looper.db import plans

    steps = [{"id": "step-s1-" + task_id, "index": 0, "key": "s1", "instruction": "explore", "status": "dispatched"}]
    gates = []
    for i, state in enumerate(states, 1):
        step_id = f"{task_id}-g{i}"
        steps.append({**_gate_step(step_id, f"g{i}"), "index": i})
        waiting = state in ("open", "discussing")
        gates.append(_gate(step_id, state, opened_at=(opened_at or NOW) if waiting or state != "pending" else None,
                           closed_at=None if waiting or state == "pending" else NOW,
                           summary={"decisions": [], "assumptions": [], "open": []} if state == "passed" else None))
    await plans.save(task_id, plan={"plan_id": "plan-" + task_id, "status": "executing"}, steps=steps, gates=gates,
                     pool=pool)


def _ids(answer: dict[str, Any]) -> list[str]:
    return [g["gate"]["step_id"] for g in answer["gates"]]


async def _list(**query: str) -> dict[str, Any]:
    status, answer, _ = await _api("GET", "/api/gates", query=query)
    assert status == 200, answer
    return answer


def test_states_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _task(pool, "task-b", "completed", project)
        await _task(pool, "task-c", "cancelled", project)
        await _task(pool, "task-d", "running", project)
        await _gates(pool, "task-a", "open", "discussing", "passed", "pending")
        await _gates(pool, "task-b", "open")
        await _gates(pool, "task-c", "open")
        await _gates(pool, "task-d", "cancelled", "skipped")

        counts = {"open": 2, "waiting_user": 2, "waiting_planner": 0, "closed": 3, "all": 8}
        answer = await _list()
        assert sorted(_ids(answer)) == ["task-a-g1", "task-a-g2"]
        assert (answer["total"], answer["counts"], answer["limit"], answer["offset"]) == (2, counts, 50, 0)
        answer = await _list(state="all")
        assert len(answer["gates"]) == 8 and answer["total"] == 8 and answer["counts"] == counts
        answer = await _list(state="closed")
        assert sorted(_ids(answer)) == ["task-a-g3", "task-d-g1", "task-d-g2"]
        assert answer["total"] == 3 and answer["counts"] == counts

    _run(test, monkeypatch)


def test_waiting_on(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _gates(pool, "task-a", "open")
        answer = await _list()
        assert answer["gates"][0]["gate"]["waiting_on"] == "user"

        await _user("task-a", "PostgreSQL")
        answer = await _list()
        assert answer["gates"][0]["gate"]["waiting_on"] == "planner"
        assert (answer["counts"]["waiting_user"], answer["counts"]["waiting_planner"]) == (0, 1)
        assert _ids(await _list(waiting_on="planner")) == ["task-a-g1"]
        user_only = await _list(waiting_on="user")
        assert user_only["gates"] == [] and user_only["total"] == 0 and user_only["counts"]["open"] == 1

        await pool.execute("UPDATE tasks SET status = 'stopped' WHERE id = 'task-a'")
        answer = await _list()
        assert answer["gates"][0]["gate"]["waiting_on"] == "user"
        assert (answer["counts"]["waiting_user"], answer["counts"]["waiting_planner"]) == (1, 0)
        assert _ids(await _list(waiting_on="user")) == ["task-a-g1"]

    _run(test, monkeypatch)


def test_project_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        one = await _project(pool, "one")
        two = await _project(pool, "two", "/w2")
        await _task(pool, "task-a", "running", one)
        await _task(pool, "task-b", "running", two)
        await _gates(pool, "task-a", "open", "passed")
        await _gates(pool, "task-b", "open")
        answer = await _list(project_id=one["id"], state="all")
        assert sorted(_ids(answer)) == ["task-a-g1", "task-a-g2"]
        assert answer["counts"] == {"open": 1, "waiting_user": 1, "waiting_planner": 0, "closed": 1, "all": 2}
        answer = await _list(project_id=two["id"])
        assert _ids(answer) == ["task-b-g1"] and answer["counts"]["all"] == 1

    _run(test, monkeypatch)


def test_order_and_paging(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        for n, age in (("a", 3), ("b", 1), ("c", 2)):
            await _task(pool, f"task-{n}", "running", project)
            await _gates(pool, f"task-{n}", "open", opened_at=NOW - timedelta(hours=age))
        assert _ids(await _list()) == ["task-b-g1", "task-c-g1", "task-a-g1"]
        answer = await _list(limit="1", offset="1")
        assert _ids(answer) == ["task-c-g1"] and answer["total"] == 3 and (answer["limit"], answer["offset"]) == (1, 1)
        answer = await _list(state="all", limit="1", offset="1")
        assert _ids(answer) == ["task-c-g1"] and answer["total"] == 3

    _run(test, monkeypatch)


def test_row_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import user_gates

        project = await _project(pool, "calc")
        await _task(pool, "task-a", "running", project, "build calc\nmore")
        await _gates(pool, "task-a", "open")
        (row,) = (await _list())["gates"]
        assert row["task"] == {"task_id": "task-a", "name": "build calc", "status": "running", "group": "in_progress",
                               "project_id": project["id"], "project_name": "calc"}
        assert (row["gate"]["step"], row["gate"]["topic"], row["agenda_open"]) == ("g1", "storage", 1)
        assert row["gate"]["channel_posts"] is None and row["last_message"] is None

        await _user("task-a", "PostgreSQL")
        await user_gates.record_reminder("task-a-g1", "task-a", "still waiting", pool=pool)
        (row,) = (await _list())["gates"]
        last = row["last_message"]
        assert (last["role"], last["via"]) == ("user", "dashboard") and last["created_at"]

    _run(test, monkeypatch)


@pytest.mark.parametrize("query", [
    {"state": "bogus"}, {"waiting_on": "user", "state": "closed"}, {"waiting_on": "nobody"},
    {"limit": "0"}, {"limit": "201"}, {"offset": "-1"}, {"offset": "x"},
])
def test_bad_queries_are_refused(monkeypatch: pytest.MonkeyPatch, query: dict[str, str]) -> None:
    async def test(pool: Any) -> None:
        status, answer, _ = await _api("GET", "/api/gates", query=query)
        assert status == 400 and answer["error"]

    _run(test, monkeypatch)
