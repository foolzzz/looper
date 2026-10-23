"""The in-memory plan store of the ``agent_db`` fixture answers the Planner's and the user's writes as PostgreSQL does.

The same series of writes (a gate under discussion that a later write must not reopen, delivery that only goes
forward, a closed gate that keeps what reached it as it closed, a wait moved first) runs on the store and on a real
schema, and what each reads back is the same. A test on the store may then rely on these rules; whatever else only the
database's own tests (``test_user_gates_db``, ``test_waits_db``) can show.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from tests.db_support import DSN, run_db

OPENED = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
CLOSED = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
SPEC = {"max_wait_seconds": 3600, "not_before_seconds": 60, "not_before": None, "check": None, "every_seconds": None}
STEPS = [{"id": "step-s1", "index": 0, "key": "s1", "instruction": "explore", "status": "dispatched"},
         {"id": "step-g1", "index": 1, "key": "g1", "instruction": "storage", "status": "waiting_user", "kind": "gate"},
         {"id": "step-w1", "index": 2, "key": "w1", "instruction": "CI finishes", "status": "waiting", "kind": "wait",
          "depends_on": ["step-s1"]}]


def _gate(state: str, **extra: Any) -> dict[str, Any]:
    return {"step_id": "step-g1", "topic": "storage", "agenda": [], "findings": "", "state": state, "summary": None,
            "resolution": "", "ended_by_user": False, "delivered_through": 0, "turns": 0, "plan_change": None,
            "opened_at": OPENED, "closed_at": None, **extra}


def _wait(state: str, **extra: Any) -> dict[str, Any]:
    return {"step_id": "step-w1", "topic": "CI finishes", "spec": SPEC, "state": state, "outcome": None,
            "opened_at": OPENED, "due_at": None, "deadline_at": None, "next_check_at": None, "resolution": "",
            "extensions": 0, "failures": 0, "failed_at": None, "closed_at": None, **extra}


def _user(text: str) -> dict[str, Any]:
    return {"kind": "message", "text": text, "via": "dashboard"}


async def _snapshot(load: Any, thread_of: Any) -> dict[str, Any]:
    """What a read of the task shows, with message ids as their place in the discussion."""
    stored = await load("task-a")
    gate, wait = stored["gates"]["step-g1"], stored["waits"]["step-w1"]
    ids = [m["id"] for m in await thread_of("step-g1")]
    return {
        "gate": {key: gate[key] for key in ("state", "topic", "turns", "opened_at", "closed_at", "waiting_for_planner",
                                           "not_delivered", "step_key")}
                | {"delivered": sum(1 for i in ids if i <= gate["delivered_through"])},
        "thread": [(m["role"], m["kind"]) for m in await thread_of("step-g1")],
        "journal": [(e["kind"], e.get("via"), e["summary"]) for e in stored["journal"] if e["kind"] == "supplement"],
        "wait": wait["state"],
    }


async def _series(save: Any, load: Any, thread_of: Any, add_user_message: Any) -> list[dict[str, Any]]:
    seen = []

    async def snap() -> None:
        seen.append(await _snapshot(load, thread_of))

    await save("task-a", plan={"plan_id": "plan-1", "status": "executing"}, steps=STEPS, gates=[_gate("open")],
               waits=[_wait("waiting")])
    await snap()
    # The user's first message puts the gate under discussion.
    _, first = await add_user_message("task-a", "g1", _user("Use PostgreSQL"))
    await snap()
    # A write that still says open keeps it under discussion; its turns count goes up.
    await save("task-a", gates=[_gate("open", turns=1)])
    await snap()
    # The Planner answers: the message is delivered.
    await save("task-a", gates=[_gate("discussing", turns=1, delivered_through=first["id"])],
               gate_messages=[{"step_id": "step-g1", "role": "planner", "kind": "reply", "text": "OK"}])
    await snap()
    # A write that has not seen the delivery does not take it back, nor lower the turns.
    await save("task-a", gates=[_gate("discussing", turns=0, delivered_through=0)])
    await snap()
    # A second message reaches the gate while the turn that closes it runs.
    _, second = await add_user_message("task-a", "g1", _user("Also JSON"))
    summary = {"decisions": ["PostgreSQL"], "assumptions": [], "open": []}
    await save("task-a", gates=[_gate("passed", turns=1, delivered_through=first["id"], closed_at=CLOSED,
                                      summary=summary)])
    await snap()
    # A closed gate stays as it closed, and takes no message.
    await save("task-a", gates=[_gate("discussing", topic="other", closed_at=None)])
    refused = await add_user_message("task-a", "g1", _user("too late"))
    assert refused[1] is None and second is not None
    await snap()
    # A wait moved first is left alone by a write that expected it waiting.
    await save("task-a", waits=[{**_wait("timed_out"), "expect": ["waiting"]}])
    await save("task-a", waits=[{**_wait("done", outcome="ready", closed_at=CLOSED), "expect": ["waiting"]}])
    await snap()
    return seen


def test_the_agent_db_store_reads_back_a_series_of_writes_as_the_database_does(
    monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    store = agent_db.store
    in_memory = asyncio.run(_series(store.save, store.load, store.gate_thread, store.add_gate_message))
    if not DSN:
        pytest.skip("LOOPER_TEST_DATABASE_URL is not set")
    monkeypatch.undo()  # the fixture stands in for the modules below

    async def on_database(pool: Any) -> None:
        from looper.db import plans, projects, tasks, user_gates

        project = await projects.create_project(name="calc", work_dir="/w", pool=pool)
        await tasks.create_task(task_id="task-a", requirement="build calc", project_id=project["id"], work_dir="/w",
                                status="running", pool=pool)

        async def save(task_id: str, **writes: Any) -> None:
            await plans.save(task_id, pool=pool, **writes)

        async def load(task_id: str) -> Any:
            return await plans.load(task_id, pool=pool)

        async def thread_of(step_id: str) -> Any:
            return await user_gates.thread(step_id, pool=pool)

        async def add(task_id: str, label: str, message: Any) -> Any:
            return await user_gates.add_user_message(task_id, label, message, pool=pool)

        assert await _series(save, load, thread_of, add) == in_memory

    run_db(on_database, monkeypatch)
