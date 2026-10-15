"""The projects API and creating a task in a project, with the database stubbed.

Every task belongs to one project. A task's work directory is the one given,
else its project's default; with neither the request is refused.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

from looper.agents.http_server import AgentHTTPServer
from looper.agents.tiers.task import Task, TaskRouter
from looper.db import connection as db_connection, llm_records as db_llm_records, projects as db_projects, tasks as db_tasks

ACTIVE = ("pending", "assigned", "running", "executing")
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


class _Store:
    """An in-memory stand-in for the projects and tasks tables."""

    def __init__(self) -> None:
        self.projects: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.list_calls: list[dict[str, Any]] = []
        self.transactions = 0

    def add_project(self, project_id: str, name: str, work_dir: str = "", background: str = "") -> None:
        self.projects[project_id] = {
            "id": project_id, "name": name, "description": "", "work_dir": work_dir, "background": background,
            "created_at": NOW, "updated_at": NOW,
        }

    def add_task(self, task_id: str, project_id: str, status: str = "init") -> None:
        self.tasks[task_id] = {
            "id": task_id, "requirement": "r", "goal_id": "", "status": status, "phase": "pending",
            "planner_id": None, "correlation_id": "", "work_dir": "/w", "project_id": project_id,
            "created_at": NOW, "completed_at": None,
        }

    def _with_counts(self, row: dict[str, Any]) -> dict[str, Any]:
        tasks = [t for t in self.tasks.values() if t["project_id"] == row["id"]]
        return {**row, "task_count": len(tasks), "active_count": sum(t["status"] in ACTIVE for t in tasks)}

    def _name_taken(self, name: str, other_than: str = "") -> bool:
        return any(p["name"].lower() == name.lower() and p["id"] != other_than for p in self.projects.values())

    async def list_projects(self, **_: Any) -> list[dict[str, Any]]:
        rows = sorted(self.projects.values(), key=lambda p: p["name"].lower())
        return [self._with_counts(p) for p in rows]

    async def get_project(self, project_id: str, **_: Any) -> dict[str, Any] | None:
        row = self.projects.get(project_id)
        return self._with_counts(row) if row else None

    async def create_project(self, *, name: str, description: str = "", work_dir: str = "", background: str = "",
                             execution: dict[str, Any] | None = None, pool: Any = None) -> dict[str, Any]:
        if self._name_taken(name):
            raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
        project_id = f"proj-{len(self.projects):012d}"
        self.add_project(project_id, name, work_dir, background)
        self.projects[project_id].update(description=description, **(execution or {}))
        return dict(self.projects[project_id])

    async def update_project(self, project_id: str, *, name: str | None = None, description: str | None = None,
                             work_dir: str | None = None, background: str | None = None,
                             execution: dict[str, Any] | None = None, pool: Any = None) -> bool:
        row = self.projects.get(project_id)
        if row is None:
            return False
        row.update(execution or {})
        if name is not None and self._name_taken(name, other_than=project_id):
            raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
        for key, value in (("name", name), ("description", description), ("work_dir", work_dir),
                           ("background", background)):
            if value is not None:
                row[key] = value
        return True

    async def delete_project(self, project_id: str, **_: Any) -> bool:
        if any(t["project_id"] == project_id for t in self.tasks.values()):
            raise asyncpg.ForeignKeyViolationError("still referenced from table tasks")
        return self.projects.pop(project_id, None) is not None

    async def create_task(self, *, task_id: str, project_id: str, work_dir: str = "", status: str = "pending",
                          pool: Any = None, **fields: Any) -> dict[str, Any]:
        self.add_task(task_id, project_id, status)
        self.tasks[task_id].update(work_dir=work_dir, pool=pool, **fields)
        return {"task_id": task_id, "created_at": NOW.isoformat()}

    async def set_task_project(self, task_id: str, project_id: str, **_: Any) -> bool:
        if task_id not in self.tasks:
            return False
        self.tasks[task_id]["project_id"] = project_id
        return True

    async def list_tasks(self, *, limit: int = 50, offset: int = 0, project_id: str | None = None, **_: Any) -> list[dict[str, Any]]:
        self.list_calls.append({"limit": limit, "project_id": project_id})
        rows = [t for t in self.tasks.values() if project_id is None or t["project_id"] == project_id]
        return [{**t, "project_name": self.projects[t["project_id"]]["name"]} for t in rows]

    async def get_task(self, task_id: str, **_: Any) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return dict(row) if row else None

    async def get_task_with_steps(self, task_id: str, **_: Any) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        if row is None:
            return None
        return {**row, "project_name": self.projects[row["project_id"]]["name"], "steps": []}

    async def task_costs(self, task_ids: list[str], **_: Any) -> dict[str, dict[str, Any]]:
        """No runs recorded yet: every task has cost nothing."""
        return {task_id: {"cost_usd": 0.0, "runs": 0} for task_id in task_ids}

    async def profile_overrides(self, task_id: str, **_: Any) -> dict[str, Any]:
        """No task here chose an Agent Profile of its own."""
        return {}

    @asynccontextmanager
    async def transaction(self) -> Any:
        self.transactions += 1
        yield "conn"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    fake = _Store()
    for name in ("list_projects", "get_project", "create_project", "update_project", "delete_project"):
        monkeypatch.setattr(db_projects, name, getattr(fake, name))
    for name in ("create_task", "set_task_project", "list_tasks", "get_task", "get_task_with_steps"):
        monkeypatch.setattr(db_tasks, name, getattr(fake, name))
    monkeypatch.setattr(db_connection, "transaction", fake.transaction)
    monkeypatch.setattr(db_llm_records, "task_costs", fake.task_costs)
    from looper.db import agent_setting_layers

    monkeypatch.setattr(agent_setting_layers, "profile_overrides", fake.profile_overrides)
    return fake


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


def _server(router: TaskRouter | None = None) -> AgentHTTPServer:
    if router is None:
        router = TaskRouter(runtime_root=Path(tempfile.mkdtemp()))
    return AgentHTTPServer(web_adapter=SimpleNamespace(), task_router=router, supervisor=SimpleNamespace())


def _call(method: str, path: str, body: Any = None, *, query: dict[str, str] | None = None,
          server: AgentHTTPServer | None = None) -> tuple[int, Any]:
    """Route one API request; return the status code and the decoded JSON body."""
    raw = body if isinstance(body, bytes) else (b"" if body is None else json.dumps(body).encode())
    writer = _Writer()
    asyncio.run((server or _server())._route_api(method, path, query or {}, raw, writer))
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


class TestProjectEndpoints:
    def test_create_returns_the_project(self, store: _Store) -> None:
        status, body = _call("POST", "/api/projects", {"name": "  tetris ", "description": "game", "work_dir": "~/tetris"})
        assert status == 201
        assert body["project_id"].startswith("proj-")
        assert (body["name"], body["description"]) == ("tetris", "game")
        assert body["work_dir"] == os.path.join(os.path.expanduser("~"), "tetris")
        assert (body["task_count"], body["active_count"]) == (0, 0)
        assert set(body) == {"project_id", "name", "description", "work_dir", "background", "created_at",
                             "updated_at", "task_count", "active_count", "task_budget_usd", "task_budget_action",
                             "supervise_interval_seconds", "review_policy", "distill_lessons"}

    def test_work_dir_is_optional(self, store: _Store) -> None:
        status, body = _call("POST", "/api/projects", {"name": "notes"})
        assert status == 201 and (body["work_dir"], body["background"]) == ("", "")

    @pytest.mark.parametrize("body", [
        b"{not json", [], {}, {"name": ""}, {"name": "   "}, {"name": 7}, {"name": "x" * 81},
        {"name": "a", "work_dir": "relative/dir"}, {"name": "a", "description": 3},
        {"name": "a", "background": 5}, {"name": "a", "worker_harness_prompt": "x"},
    ])
    def test_create_rejects_a_bad_request(self, store: _Store, body: Any) -> None:
        status, response = _call("POST", "/api/projects", body)
        assert status == 400 and response["error"]
        assert store.projects == {}

    def test_create_accepts_an_80_character_name(self, store: _Store) -> None:
        assert _call("POST", "/api/projects", {"name": "x" * 80})[0] == 201

    def test_create_rejects_a_taken_name_whatever_its_case(self, store: _Store) -> None:
        store.add_project("proj-a", "Tetris")
        status, body = _call("POST", "/api/projects", {"name": "TETRIS"})
        assert status == 409 and "TETRIS" in body["error"]

    def test_list_reports_a_database_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def broken(**_: Any) -> list[dict[str, Any]]:
            raise ConnectionError("database is down")

        monkeypatch.setattr(db_projects, "list_projects", broken)
        assert _call("GET", "/api/projects") == (500, {"error": "database unavailable"})

    def test_update_changes_only_the_given_fields(self, store: _Store) -> None:
        store.add_project("proj-a", "tetris", "/srv/tetris")
        status, body = _call("PUT", "/api/projects/proj-a", {"description": "falling blocks"})
        assert status == 200
        assert (body["name"], body["description"], body["work_dir"]) == ("tetris", "falling blocks", "/srv/tetris")

    def test_update_with_an_empty_work_dir_clears_the_default(self, store: _Store) -> None:
        store.add_project("proj-a", "tetris", "/srv/tetris")
        status, body = _call("PUT", "/api/projects/proj-a", {"name": "Tetris", "work_dir": ""})
        assert status == 200 and (body["name"], body["work_dir"]) == ("Tetris", "")

    @pytest.mark.parametrize("body", [b"[", {}, {"name": ""}, {"work_dir": "rel"}, {"description": None}])
    def test_update_rejects_a_bad_request(self, store: _Store, body: Any) -> None:
        store.add_project("proj-a", "tetris")
        assert _call("PUT", "/api/projects/proj-a", body)[0] == 400

    def test_update_of_an_unknown_project_is_404(self, store: _Store) -> None:
        assert _call("PUT", "/api/projects/proj-nope", {"name": "x"})[0] == 404

    def test_update_to_a_taken_name_is_409(self, store: _Store) -> None:
        store.add_project("proj-a", "tetris")
        store.add_project("proj-b", "notes")
        assert _call("PUT", "/api/projects/proj-b", {"name": "Tetris"})[0] == 409

    def test_delete_an_empty_project(self, store: _Store) -> None:
        store.add_project("proj-a", "tetris")
        assert _call("DELETE", "/api/projects/proj-a") == (200, {"deleted": "proj-a"})
        assert store.projects == {}

    def test_delete_of_a_project_with_tasks_is_409(self, store: _Store) -> None:
        store.add_project("proj-a", "tetris")
        store.add_task("task-1", "proj-a", "completed")
        status, body = _call("DELETE", "/api/projects/proj-a")
        assert status == 409 and "task" in body["error"]
        assert "proj-a" in store.projects

    def test_delete_of_an_unknown_project_is_404(self, store: _Store) -> None:
        assert _call("DELETE", "/api/projects/proj-nope")[0] == 404


class TestProjectBackground:
    """A project's background goes into its task agents' system prompt; up to 10000 characters."""

    @pytest.mark.parametrize(("raw", "stored"), [
        ("", ""),
        ("\n \n\t\n", ""),
        ("x", "x"),
        ("\r\n\r\n  x\r\n\r\ny\r\n", "  x\r\n\r\ny"),
        # The first line keeps its indentation and inner blank lines stay.
        ("\n \n  - use uv\n\n  - Python 3.12\n\n\t\n", "  - use uv\n\n  - Python 3.12"),
    ])
    def test_only_the_blank_lines_around_it_are_trimmed(self, raw: str, stored: str) -> None:
        from looper.agents.projects import project_background

        assert project_background(raw) == stored

    def test_the_limit_is_10000_characters(self, store: _Store) -> None:
        assert _call("POST", "/api/projects", {"name": "a", "background": "x" * 10000})[0] == 201
        status, body = _call("POST", "/api/projects", {"name": "b", "background": "x" * 10001})
        assert status == 400 and "10000" in body["error"]
        assert [p["name"] for p in store.projects.values()] == ["a"]
        # Blank lines around it do not count.
        assert _call("POST", "/api/projects", {"name": "c", "background": "\n\n" + "x" * 10000 + "\n\n"})[0] == 201

    def test_put_sets_it_and_an_empty_string_clears_it(self, store: _Store) -> None:
        store.add_project("proj-a", "calc", "/srv/calc", background="old")
        status, body = _call("PUT", "/api/projects/proj-a", {"background": "Use uv.\n"})
        assert status == 200 and body["background"] == "Use uv."
        assert (body["name"], body["work_dir"]) == ("calc", "/srv/calc")  # the rest is kept
        status, body = _call("PUT", "/api/projects/proj-a", {"background": ""})
        assert status == 200 and body["background"] == ""

    def test_a_new_project_made_with_a_task_takes_it(self, store: _Store) -> None:
        status, body = _call("POST", "/api/tasks/create", {
            "requirement": "r", "work_dir": "/tmp/calc",
            "project": {"name": "calc", "background": " Use uv.\n"},
        })
        assert status == 201
        assert store.projects[body["project_id"]]["background"] == " Use uv."


class TestMovedProjectFields:
    """A project's harness overrides, model and per-run cap are agent settings now (its layers)."""

    @pytest.mark.parametrize("field", ["planner_harness_prompt", "worker_harness_prompt", "model", "run_budget_usd"])
    def test_create_update_and_a_new_project_with_a_task_refuse_them(self, store: _Store, field: str) -> None:
        status, body = _call("POST", "/api/projects", {"name": "calc", field: "x"})
        assert status == 400 and "agent-settings" in body["error"] and store.projects == {}
        store.add_project("proj-c", "c")
        assert _call("PUT", "/api/projects/proj-c", {"name": "renamed", field: "x"})[0] == 400
        assert store.projects["proj-c"]["name"] == "c"
        status, _ = _call("POST", "/api/tasks/create", {"requirement": "r", "work_dir": "/tmp/calc",
                                                        "project": {"name": "calc", field: "x"}})
        assert status == 400 and list(store.projects) == ["proj-c"]


class TestTaskCreate:
    def test_in_an_existing_project_uses_its_directory(self, store: _Store) -> None:
        store.add_project("proj-a", "tetris", "/srv/tetris")
        status, body = _call("POST", "/api/tasks/create", {"requirement": " build it ", "project_id": "proj-a"})
        assert status == 201
        assert body["project_id"] == "proj-a" and body["status"] == "init"
        task = store.tasks[body["task_id"]]
        assert (task["project_id"], task["work_dir"], task["requirement"], task["status"]) == (
            "proj-a", "/srv/tetris", "build it", "init")
        assert store.transactions == 0

    def test_an_explicit_directory_wins_over_the_projects(self, store: _Store) -> None:
        store.add_project("proj-a", "tetris", "/srv/tetris")
        status, body = _call("POST", "/api/tasks/create",
                             {"requirement": "r", "project_id": "proj-a", "work_dir": "/tmp/other/"})
        assert status == 201 and store.tasks[body["task_id"]]["work_dir"] == "/tmp/other"

    def test_with_a_new_project_creates_both_in_one_transaction(self, store: _Store) -> None:
        status, body = _call("POST", "/api/tasks/create", {
            "requirement": "r", "project": {"name": " notes ", "description": "d", "work_dir": "~/notes"},
            "config": {"k": 1}, "goal_id": "g",
        })
        assert status == 201
        (project,) = store.projects.values()
        notes_dir = os.path.join(os.path.expanduser("~"), "notes")
        assert (project["name"], project["description"], project["work_dir"]) == ("notes", "d", notes_dir)
        task = store.tasks[body["task_id"]]
        assert body["project_id"] == project["id"] == task["project_id"]
        assert (task["work_dir"], task["config"], task["goal_id"], task["pool"]) == (notes_dir, {"k": 1}, "g", "conn")
        assert store.transactions == 1

    def test_a_new_project_keeps_only_its_own_directory(self, store: _Store) -> None:
        status, body = _call("POST", "/api/tasks/create",
                             {"requirement": "r", "project": {"name": "notes"}, "work_dir": "/tmp/notes"})
        assert status == 201
        assert store.projects[body["project_id"]]["work_dir"] == ""
        assert store.tasks[body["task_id"]]["work_dir"] == "/tmp/notes"

    @pytest.mark.parametrize("fields", [
        {},
        {"project_id": None, "project": None},
        {"project_id": "", "project": None},
        {"project_id": "proj-a", "project": {"name": "notes"}},
    ])
    def test_needs_exactly_one_project_field(self, store: _Store, fields: dict[str, Any]) -> None:
        store.add_project("proj-a", "tetris", "/srv/tetris")
        status, body = _call("POST", "/api/tasks/create", {"requirement": "r", "work_dir": "/tmp", **fields})
        assert status == 400 and "project" in body["error"]
        assert store.tasks == {}

    @pytest.mark.parametrize("fields", [
        {"project": "notes"},
        {"project": {"name": ""}},
        {"project": {"name": "notes", "work_dir": "rel"}},
        {"project_id": "proj-a", "work_dir": "relative/dir"},
        {"project_id": "proj-a", "work_dir": 5},
        {"project_id": 5},
        {"project_id": "proj-a", "config": "{}"},
    ])
    def test_rejects_bad_project_directory_or_config_fields(self, store: _Store, fields: dict[str, Any]) -> None:
        store.add_project("proj-a", "tetris", "/srv/tetris")
        assert _call("POST", "/api/tasks/create", {"requirement": "r", **fields})[0] == 400
        assert store.tasks == {} and len(store.projects) == 1

    @pytest.mark.parametrize("project", [{"project_id": "proj-a"}, {"project": {"name": "notes"}}])
    def test_without_any_directory_is_refused(self, store: _Store, project: dict[str, Any]) -> None:
        store.add_project("proj-a", "tetris")  # no default directory
        status, body = _call("POST", "/api/tasks/create", {"requirement": "r", "work_dir": " ", **project})
        assert status == 400
        assert body["error"] == "a work directory is required: set one on the task or on its project"
        assert store.tasks == {} and list(store.projects) == ["proj-a"]

    def test_requires_a_requirement(self, store: _Store) -> None:
        store.add_project("proj-a", "tetris", "/srv/tetris")
        assert _call("POST", "/api/tasks/create", {"requirement": " ", "project_id": "proj-a"})[0] == 400

    def test_unknown_project_id_is_404(self, store: _Store) -> None:
        status, _ = _call("POST", "/api/tasks/create", {"requirement": "r", "project_id": "proj-nope", "work_dir": "/tmp"})
        assert status == 404 and store.tasks == {}

    def test_a_new_project_with_a_taken_name_is_409(self, store: _Store) -> None:
        store.add_project("proj-a", "Notes", "/srv/notes")
        status, _ = _call("POST", "/api/tasks/create", {"requirement": "r", "project": {"name": "notes"}, "work_dir": "/tmp"})
        assert status == 409
        assert store.tasks == {} and list(store.projects) == ["proj-a"]


class TestTaskProject:
    def test_a_running_task_moves_and_its_live_entry_follows(self, store: _Store) -> None:
        store.add_project("proj-a", "tetris")
        store.add_project("proj-b", "notes")
        store.add_task("task-1", "proj-a", "running")
        router = TaskRouter(runtime_root=Path(tempfile.mkdtemp()))
        live = Task(task_id="task-1", requirement="r", goal_id="", work_dir="/w", project_id="proj-a", status="running")
        router._tasks = {"task-1": live}

        status, body = _call("PUT", "/api/tasks/task-1/project", {"project_id": "proj-b"}, server=_server(router))

        assert (status, body) == (200, {"task_id": "task-1", "project_id": "proj-b", "project_name": "notes"})
        assert store.tasks["task-1"]["project_id"] == "proj-b"
        assert live.project_id == "proj-b" and live.status == "running"

    def test_a_task_the_router_does_not_hold_moves_too(self, store: _Store) -> None:
        store.add_project("proj-a", "tetris")
        store.add_project("proj-b", "notes")
        store.add_task("task-1", "proj-a", "completed")
        assert _call("PUT", "/api/tasks/task-1/project", {"project_id": "proj-b"})[0] == 200
        assert store.tasks["task-1"]["project_id"] == "proj-b"

    @pytest.mark.parametrize(("task_id", "body", "status"), [
        ("task-1", {"project_id": "proj-nope"}, 404),
        ("task-nope", {"project_id": "proj-b"}, 404),
        ("task-1", {}, 400),
        ("task-1", {"project_id": ""}, 400),
        ("task-1", b"{", 400),
    ])
    def test_rejects(self, store: _Store, task_id: str, body: Any, status: int) -> None:
        store.add_project("proj-a", "tetris")
        store.add_project("proj-b", "notes")
        store.add_task("task-1", "proj-a")
        assert _call("PUT", f"/api/tasks/{task_id}/project", body)[0] == status
        assert store.tasks["task-1"]["project_id"] == "proj-a"


class TestTaskReads:
    def test_list_passes_the_project_filter_and_shows_the_project(self, store: _Store) -> None:
        store.add_project("proj-a", "tetris")
        store.add_project("proj-b", "notes")
        store.add_task("task-1", "proj-a")
        store.add_task("task-2", "proj-b")
        status, body = _call("GET", "/api/tasks", query={"project_id": "proj-b"})
        assert status == 200
        assert store.list_calls == [{"limit": 50, "project_id": "proj-b"}]
        (task,) = body["tasks"]
        assert (task["task_id"], task["project_id"], task["project_name"], task["work_dir"]) == (
            "task-2", "proj-b", "notes", "/w")
        assert len(_call("GET", "/api/tasks")[1]["tasks"]) == 2  # without a filter, every project's
        assert store.list_calls[-1] == {"limit": 50, "project_id": None}

    def test_detail_shows_the_project(self, store: _Store) -> None:
        store.add_project("proj-a", "tetris")
        store.add_task("task-1", "proj-a")
        status, body = _call("GET", "/api/tasks/task-1")
        assert status == 200 and (body["project_id"], body["project_name"]) == ("proj-a", "tetris")


class TestStartReadyTask:
    """``POST /api/tasks/<id>/start`` starts a ready task through the TaskRouter (``TASK_START``), and only one."""

    @staticmethod
    def _call(task_id: str) -> tuple[int, Any, list[Any]]:
        from looper.agents.message import channel_address

        published: list[Any] = []

        async def publish(envelope: Any) -> None:
            published.append(envelope)

        server = AgentHTTPServer(web_adapter=SimpleNamespace(_address=channel_address("web")),
                                 task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())),
                                 supervisor=SimpleNamespace(bus=SimpleNamespace(publish=publish)))
        status, body = _call("POST", f"/api/tasks/{task_id}/start", server=server)
        return status, body, published

    def test_a_ready_task_is_started_through_the_router(self, store: _Store) -> None:
        from looper.agents.message import TASK_ROUTER_ADDRESS, MessageType

        store.add_project("proj-a", "tetris")
        store.add_task("task-1", "proj-a", "init")
        status, body, (envelope,) = self._call("task-1")
        assert (status, body) == (200, {"started": "task-1", "project_id": "proj-a"})
        assert (envelope.message_type, envelope.target, envelope.payload) == (
            MessageType.TASK_START, TASK_ROUTER_ADDRESS, {"task_id": "task-1"})

    @pytest.mark.parametrize("task_status", ["pending", "running", "stopped", "failed", "cancelled", "completed"])
    def test_a_task_that_is_not_ready_is_400(self, store: _Store, task_status: str) -> None:
        store.add_project("proj-a", "tetris")
        store.add_task("task-1", "proj-a", task_status)
        status, body, published = self._call("task-1")
        assert (status, published) == (400, [])
        assert task_status in body["error"]

    def test_an_unknown_task_is_404(self, store: _Store) -> None:
        status, _, published = self._call("task-nope")
        assert (status, published) == (404, [])
