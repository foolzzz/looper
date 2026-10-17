"""User gates on a real PostgreSQL: the Planner's writes keep a gate under discussion and a closed one as they are;
the user's messages move an open gate to discussing and wait for the Planner until a turn had them; a gate's
conclusion is a supplement delivered with it; reminders come at the project's interval only while the gate waits
for the user; the dashboard's API takes messages only while a gate waits; a task whose gate waits counts as
needing the user.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_ops_tasks_db import _project, _task
from tests.test_usage_api_db import DSN, _run, _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

AGENDA = [{"id": "q1", "question": "Which database?", "options": ["PostgreSQL", "SQLite"], "status": "open",
           "answer": ""}]


def _gate(step_id: str = "step-g1", state: str = "pending", **extra: Any) -> dict[str, Any]:
    return {"step_id": step_id, "topic": "storage", "agenda": AGENDA, "findings": "", "state": state,
            "summary": None, "resolution": "", "ended_by_user": False, "delivered_through": 0, "turns": 0,
            "plan_change": None, "opened_at": None, "closed_at": None, **extra}


def _gate_step(step_id: str = "step-g1", key: str = "g1", status: str = "pending") -> dict[str, Any]:
    return {"id": step_id, "index": 1, "key": key, "instruction": "storage", "status": status, "kind": "gate"}


async def _plan_with_gate(pool: Any, task_id: str = "task-a", **gate: Any) -> None:
    from looper.db import plans

    await plans.save(task_id, plan={"plan_id": "plan-1", "status": "executing"},
                     steps=[{"id": "step-s1", "index": 0, "key": "s1", "instruction": "explore", "status": "dispatched"},
                            _gate_step(status="waiting_user" if gate.get("state") in ("open", "discussing")
                                       else "pending")],
                     gates=[_gate(**gate)], pool=pool)


async def _user(task_id: str, text: str, **extra: Any) -> tuple[Any, Any]:
    from looper.db import user_gates

    return await user_gates.add_user_message(task_id, "g1", {"kind": "message", "text": text, "via": "dashboard",
                                                             **extra})


def test_the_planners_writes_keep_a_discussion_and_a_closed_gate_as_they_are(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from datetime import datetime, timezone

        from looper.db import plans, user_gates

        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        opened = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
        await _plan_with_gate(pool, state="open", opened_at=opened)
        stored = await plans.load("task-a", pool=pool)
        assert stored is not None and stored["gates"]["step-g1"]["state"] == "open"
        assert [s["kind"] for s in stored["steps"]] == ["work", "gate"]

        # The user's first message: the gate is under discussion, and it waits for the Planner.
        gate, message = await _user("task-a", "Use PostgreSQL")
        assert message is not None and gate["state"] == "discussing" and gate["waiting_for_planner"] is True
        assert gate["first_reply_at"] is not None
        # A write of the Planner's that still says open (its turn read the gate before the message) keeps it under
        # discussion, and its delivery mark only goes forward.
        await plans.save("task-a", gates=[_gate(state="open", opened_at=opened, delivered_through=0, turns=1)],
                         pool=pool)
        (row,) = await user_gates.list_for_task("task-a", pool=pool)
        assert (row["state"], row["turns"], row["opened_at"]) == ("discussing", 1, opened)
        assert [m["text"] for m in await user_gates.pending("task-a", pool=pool)] == ["Use PostgreSQL"]

        await plans.save("task-a", gates=[_gate(state="discussing", delivered_through=message["id"], turns=1)],
                         gate_messages=[{"step_id": "step-g1", "role": "planner", "kind": "reply", "text": "OK"}],
                         pool=pool)
        assert await user_gates.pending("task-a", pool=pool) == []
        (row,) = await user_gates.list_for_task("task-a", pool=pool)
        assert row["waiting_for_planner"] is False
        # A write that has not seen the delivery does not take it back.
        await plans.save("task-a", gates=[_gate(state="discussing", delivered_through=0, turns=1)], pool=pool)
        assert await user_gates.pending("task-a", pool=pool) == []

        # It passes; a later write cannot reopen or change it, and it takes no message.
        summary = {"decisions": ["PostgreSQL"], "assumptions": [], "open": []}
        await plans.save("task-a", gates=[_gate(state="passed", summary=summary, delivered_through=message["id"],
                                                turns=1, closed_at=opened)], pool=pool)
        await plans.save("task-a", gates=[_gate(state="discussing", topic="other")], pool=pool)
        (row,) = await user_gates.list_for_task("task-a", pool=pool)
        assert (row["state"], row["topic"], row["summary"]) == ("passed", "storage", summary)
        gate, refused = await _user("task-a", "one more thing")
        assert refused is None and gate["state"] == "passed"
        assert await user_gates.add_user_message("task-a", "nope", {"kind": "message", "text": "x"}) == (None, None)
        assert [(m["role"], m["kind"]) for m in await user_gates.thread("step-g1", pool=pool)] == [
            ("user", "message"), ("planner", "reply")]

    _run(test, monkeypatch)


def test_a_gates_conclusion_is_a_supplement_delivered_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.tiers.journal import JournalEntry, JournalKind
        from looper.db import journal, plans, task_changes

        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _plan_with_gate(pool, state="discussing")
        conclusion = JournalEntry("step-g1", "g1", "Settled with the user at gate g1 (storage):\nDecisions:\n- PG",
                                  kind=JournalKind.SUPPLEMENT, via="gate")
        closed = JournalEntry("step-g1", "g1", "gate passed", kind=JournalKind.GATE_CLOSED)
        await plans.save("task-a", journal=[conclusion.row(), closed.row()], pool=pool)

        entries = await journal.list_entries("task-a", pool=pool)
        supplement = next(e for e in entries if e["kind"] == "supplement")
        assert supplement["via"] == "gate" and supplement["delivered_at"] is not None
        assert next(e for e in entries if e["kind"] == "gate_closed")["delivered_at"] is None
        # Delivered: no CHAT turn gives it to the Planner again.
        assert (await task_changes.pending("task-a", pool=pool)).empty

    _run(test, monkeypatch)


def test_reminders_come_at_the_projects_interval_while_the_gate_waits_for_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        from datetime import datetime, timedelta, timezone

        from looper.agents.gate_service import GateReminders
        from looper.db import user_gates

        project = await _project(pool)
        await _task(pool, "task-a", "waiting_user", project)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=25)
        await _plan_with_gate(pool, state="open", opened_at=long_ago)
        reminders = GateReminders()

        assert await reminders.run_once() == 1
        (row,) = await user_gates.list_for_task("task-a", pool=pool)
        assert row["reminders"] == 1 and row["last_reminded_at"] is not None
        thread = await user_gates.thread("step-g1", pool=pool)
        assert [(m["role"], m["kind"]) for m in thread] == [("looper", "reminder")]
        assert thread[0]["text"].startswith("Still waiting for you (25 h): storage")
        activity = await pool.fetch("SELECT summary FROM activity WHERE task_id = 'task-a'")
        assert [r["summary"] for r in activity] == ["Gate g1 still waits for the user (waited 25 h); reminder noted"]
        # Just reminded: not again until the interval passes once more.
        assert await reminders.run_once() == 0

        await pool.execute("UPDATE task_user_gates SET last_reminded_at = $1", long_ago)
        # A message of the user's waits for the Planner, not for the user: no reminder.
        await _user("task-a", "PostgreSQL")
        await pool.execute("UPDATE task_user_gate_messages SET created_at = $1", long_ago)
        assert await user_gates.due_reminders(pool=pool) == []
        await pool.execute("UPDATE task_user_gates SET delivered_through = (SELECT max(id) FROM task_user_gate_messages)")
        assert len(await user_gates.due_reminders(pool=pool)) == 1
        # A project that reminds every 48 hours, or never, and a stopped task get none.
        await pool.execute("UPDATE projects SET gate_reminder_hours = 48")
        assert await user_gates.due_reminders(pool=pool) == []
        await pool.execute("UPDATE projects SET gate_reminder_hours = 0")
        assert await user_gates.due_reminders(pool=pool) == []
        await pool.execute("UPDATE projects SET gate_reminder_hours = NULL; UPDATE tasks SET status = 'stopped'")
        assert await user_gates.due_reminders(pool=pool) == []

    _run(test, monkeypatch)


class _Bus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, envelope: Any) -> None:
        self.published.append(envelope)


async def _api(method: str, path: str, body: Any = None, *, live: bool = False,
               query: dict[str, str] | None = None) -> tuple[int, Any, _Bus]:
    """Call the API as the dashboard does; ``live``: the task has a Planner."""
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.message import channel_address
    from looper.agents.tiers.task import TaskRouter

    bus = _Bus()
    router = TaskRouter(runtime_root=Path(tempfile.mkdtemp()))
    router.has_planner = lambda task_id: live  # type: ignore[method-assign]
    server = AgentHTTPServer(web_adapter=SimpleNamespace(_address=channel_address("web")),
                             supervisor=SimpleNamespace(bus=bus), task_router=router)
    writer = _Writer()
    await server._route_api(method, path, query or {}, b"" if body is None else json.dumps(body).encode(), writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload), bus


def test_the_dashboard_writes_in_a_gate_that_waits_and_its_planner_is_woken(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.message import MessageType

        project = await _project(pool)
        await _task(pool, "task-a", "waiting_user", project)
        await _plan_with_gate(pool, state="open")

        status, answer, bus = await _api("POST", "/api/tasks/task-a/gates/g1/messages",
                                         {"text": "PostgreSQL", "agenda_item": "q1", "option": "PostgreSQL"}, live=True)
        assert status == 200 and answer["delivered"] == "queued", answer
        assert answer["gate"]["state"] == "discussing" and answer["gate"]["waiting_on"] == "planner"
        assert (answer["message"]["agenda_item"], answer["message"]["option"]) == ("q1", "PostgreSQL")
        (woken,) = bus.published
        assert woken.message_type is MessageType.GATE_MESSAGE and woken.target.agent_id == "planner-task-a"

        # A stopped task keeps the message for its restart; the end of the discussion is a message too.
        await pool.execute("UPDATE tasks SET status = 'stopped'")
        status, answer, bus = await _api("POST", "/api/tasks/task-a/gates/g1/end", {})
        assert status == 200 and answer["delivered"] == "kept" and answer["message"]["kind"] == "end"
        assert bus.published == []

        status, detail, _ = await _api("GET", "/api/tasks/task-a")
        (gate,) = detail["gates"]
        assert (gate["step"], gate["state"], gate["topic"], gate["agenda"][0]["id"]) == ("g1", "discussing",
                                                                                          "storage", "q1")
        status, thread, _ = await _api("GET", "/api/tasks/task-a/gates/g1/messages")
        assert status == 200 and [(m["kind"], m["text"]) for m in thread["messages"]] == [
            ("message", "PostgreSQL"), ("end", "")]

    _run(test, monkeypatch)


@pytest.mark.parametrize(("path", "body", "status", "error"), [
    ("/api/tasks/task-a/gates/g1/messages", {"text": ""}, 400, "text must be a non-empty string"),
    ("/api/tasks/task-a/gates/g1/messages", {"text": "x" * 8001}, 400, "text has 8001 characters; at most 8000"),
    ("/api/tasks/task-a/gates/g1/messages", {"text": "x", "agenda_item": "q1"}, 400,
     "agenda_item and option go together"),
    ("/api/tasks/task-a/gates/g1/messages", {"text": "x", "agenda_item": "q1", "option": "Oracle"}, 400,
     "option 'Oracle' is not one of q1's options"),
    ("/api/tasks/task-a/gates/g1/messages", {"text": "x", "agenda_item": "q7", "option": "a"}, 400,
     "agenda_item 'q7' is not an item of this gate's agenda"),
    ("/api/tasks/task-a/gates/g9/messages", {"text": "x"}, 404, "task task-a has no gate g9"),
    ("/api/tasks/task-zz/gates/g1/messages", {"text": "x"}, 404, "task not found"),
])
def test_the_dashboard_cannot_write_what_a_gate_does_not_take(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any], status: int, error: str,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "waiting_user", project)
        await _plan_with_gate(pool, state="open")

        got, answer, _ = await _api("POST", path, body)
        assert (got, answer["error"][:len(error)]) == (status, error)

    _run(test, monkeypatch)


def test_a_gate_that_does_not_wait_or_a_completed_task_takes_no_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _plan_with_gate(pool, state="pending")

        status, answer, _ = await _api("POST", "/api/tasks/task-a/gates/g1/messages", {"text": "x"})
        assert (status, answer["error"]) == (409, "gate g1 is pending: it does not wait for the user")
        await pool.execute("UPDATE tasks SET status = 'completed'")
        status, answer, _ = await _api("POST", "/api/tasks/task-a/gates/g1/end", {})
        assert (status, answer["error"]) == (409, "the task has completed: its gates no longer take messages")

    _run(test, monkeypatch)


def test_a_task_whose_gate_waits_needs_the_user_and_stays_active(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import tasks

        project = await _project(pool)
        await _task(pool, "task-a", "waiting_user", project)
        await _task(pool, "task-b", "executing", project)

        assert [t["id"] for t in await tasks.list_tasks(group=tasks.TaskGroup.NEEDS_YOU, pool=pool)] == ["task-a"]
        assert [t["id"] for t in await tasks.list_tasks(group=tasks.TaskGroup.IN_PROGRESS, pool=pool)] == ["task-b"]
        assert "waiting_user" in tasks.ACTIVE_STATUSES
        status, listing, _ = await _api("GET", "/api/tasks", query={"group": "needs_you"})
        assert status == 200 and [(t["task_id"], t["group"]) for t in listing["tasks"]] == [("task-a", "needs_you")]

    _run(test, monkeypatch)


def test_the_projects_gate_settings_are_checked_and_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        status, answer, _ = await _api("PUT", f"/api/projects/{project['id']}", {"user_gates": "maybe"})
        assert status == 400 and "user_gates must be one of off, on" in answer["error"]
        status, answer, _ = await _api("PUT", f"/api/projects/{project['id']}", {"gate_reminder_hours": 200})
        assert status == 400 and "gate_reminder_hours must be a whole number of hours from 0" in answer["error"]
        status, answer, _ = await _api("PUT", f"/api/projects/{project['id']}",
                                       {"user_gates": "on", "gate_reminder_hours": 0})
        assert status == 200, answer
        row = await pool.fetchrow("SELECT user_gates, gate_reminder_hours FROM projects WHERE id = $1",
                                  project["id"])
        assert (row["user_gates"], row["gate_reminder_hours"]) == ("on", 0)

    _run(test, monkeypatch)
