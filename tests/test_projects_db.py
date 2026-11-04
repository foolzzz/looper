"""Current projects, CRUD and task creation on disposable PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from tests.db_support import DSN, fresh_schema

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _run(test: Callable[[Any], Awaitable[None]], *, max_size: int = 2) -> None:
    from looper.db.connection import ensure_schema

    async def _main() -> None:
        async with fresh_schema(max_size=max_size) as pool:
            await ensure_schema(pool)
            await test(pool)

    asyncio.run(_main())


def test_a_new_database_starts_with_no_projects() -> None:
    async def _test(pool: Any) -> None:
        assert await pool.fetchval("SELECT count(*) FROM projects") == 0

    _run(_test)


@pytest.mark.parametrize(("name", "directory"), [
    (" ..Hello / WORLD---v1.. ", "hello-world-v1"), ("中文项目", None),
    ("Keep_This.v1", "keep_this.v1"),
])
def test_projects_root_creates_a_default_directory(monkeypatch, tmp_path, name, directory):
    from looper.agents import projects as api
    from looper.db import connection, projects, tasks

    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        monkeypatch.setattr(api, "_root", str(tmp_path / "projects"))
        mask = os.umask(0o077)
        try:
            made = await api.create_project({"name": name, "work_dir": "  "})
        finally:
            os.umask(mask)
        path = tmp_path / "projects" / (directory or made["project_id"])
        assert made["work_dir"] == str(path)
        assert path.is_dir() and path.stat().st_mode & 0o777 == 0o755
        assert (await projects.get_project(made["project_id"]))["work_dir"] == str(path)
        task = await api.create_task_core({"requirement": "Use this directory", "project_id": made["project_id"]})
        assert task["work_dir"] == str(path)
        assert task["workspace"]["workspace"] == str(path)
        assert (await tasks.get_task(task["task_id"]))["work_dir"] == str(path)

    _run(check)


def test_projects_root_import_preserves_explicit_and_existing_directories(monkeypatch, tmp_path):
    from looper.agents import config_transfer, projects as api
    from looper.db import projects

    async def check(pool):
        monkeypatch.setattr(api, "_root", str(tmp_path / "root"))
        old = await projects.create_project(name="Old", work_dir="/srv/original", pool=pool)
        explicit = tmp_path / "explicit"
        data = {"format": "looper-config/v4", "projects": [
            {"name": "Imported"}, {"name": "Explicit", "work_dir": str(explicit)},
            {"name": "Old", "work_dir": str(explicit)},
        ]}
        async with pool.acquire() as conn, conn.transaction():
            await config_transfer.import_config(conn, data, apply=False)
        assert not (tmp_path / "root").exists()
        async with pool.acquire() as conn, conn.transaction():
            await config_transfer.import_config(conn, data, apply=True)
        assert (await projects.find_project_by_name("Imported", pool=pool))["work_dir"] == str(tmp_path / "root/imported")
        assert (tmp_path / "root/imported").is_dir()
        assert (await projects.find_project_by_name("Explicit", pool=pool))["work_dir"] == str(explicit)
        assert not explicit.exists()
        assert (await projects.get_project(old["id"], pool=pool))["work_dir"] == "/srv/original"

    _run(check)


def test_projects_root_collision_explicit_directory_and_existing_projects(monkeypatch, tmp_path):
    from looper.agents import projects as api
    from looper.db import connection, projects

    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        monkeypatch.setattr(api, "_root", "")
        old = await api.create_project({"name": "Old"})
        assert old["work_dir"] == ""
        monkeypatch.setattr(api, "_root", str(tmp_path))
        first = await api.create_project({"name": "A B"})
        second = await api.create_project({"name": "A+B"})
        third = await api.create_project({"name": "A/B"})
        assert [p["work_dir"] for p in (first, second, third)] == [str(tmp_path / n) for n in ("a-b", "a-b-2", "a-b-3")]
        explicit = tmp_path / "explicit"
        made = await api.create_project({"name": "Custom", "work_dir": str(explicit)})
        assert made["work_dir"] == str(explicit) and not explicit.exists()
        await api.update_project(old["project_id"], {"name": "Old renamed"})
        assert (await projects.get_project(old["project_id"]))["work_dir"] == ""
        await api.update_project(first["project_id"], {"name": "New name"})
        assert (await projects.get_project(first["project_id"]))["work_dir"] == first["work_dir"]

    _run(check)


def test_projects_root_serializes_concurrent_slug_collisions(monkeypatch, tmp_path):
    from looper.agents import projects as api
    from looper.db import connection

    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        monkeypatch.setattr(api, "_root", str(tmp_path))
        made = await asyncio.gather(api.create_project({"name": "A B"}), api.create_project({"name": "A+B"}))
        assert {p["work_dir"] for p in made} == {str(tmp_path / "a-b"), str(tmp_path / "a-b-2")}

    _run(check)


@pytest.mark.parametrize("existing", ["absent", "directory", "symlink", "populated"])
def test_projects_root_duplicate_name_preserves_only_existing_or_populated_directories(monkeypatch, tmp_path, existing):
    from looper.agents import projects as api
    from looper.db import connection, projects

    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        monkeypatch.setattr(api, "_root", str(tmp_path))
        original = await api.create_project({"name": "Duplicate"})
        path = tmp_path / "duplicate-2"
        if existing == "directory":
            path.mkdir()
        elif existing == "symlink":
            target = tmp_path / "target"
            target.mkdir()
            path.symlink_to(target, target_is_directory=True)
        elif existing == "populated":
            create = projects.create_project

            async def fail_after_write(**kwargs):
                (path / "keep.txt").write_text("keep")
                return await create(**kwargs)

            monkeypatch.setattr(projects, "create_project", fail_after_write)
        with pytest.raises(api.RequestError) as error:
            await api.create_project({"name": "DUPLICATE"})
        assert error.value.status == api.HTTP_409
        assert path.exists() == (existing != "absent")
        if existing == "symlink":
            assert path.is_symlink() and target.is_dir()
        if existing == "populated":
            assert (path / "keep.txt").read_text() == "keep"
        assert (tmp_path / "duplicate").is_dir()
        assert (await projects.get_project(original["project_id"]))["work_dir"] == original["work_dir"]
        assert await pool.fetchval("SELECT count(*) FROM projects") == 1

    _run(check)


@pytest.mark.parametrize("entry", ["project", "task", "import"])
def test_projects_root_removes_its_directory_when_commit_fails(monkeypatch, tmp_path, entry):
    import asyncpg
    from looper.agents import config_transfer, projects as api
    from looper.db import connection

    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        monkeypatch.setattr(api, "_root", str(tmp_path))
        await pool.execute("""
            CREATE FUNCTION refuse_project_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'commit refused' USING ERRCODE = '23514'; END $$;
            CREATE CONSTRAINT TRIGGER refuse_project_commit AFTER INSERT ON projects
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION refuse_project_commit();
        """)
        with pytest.raises(asyncpg.CheckViolationError, match="commit refused"):
            if entry == "project":
                await api.create_project({"name": "Rolled back"})
            elif entry == "task":
                await api.create_task_core({"requirement": "r", "project": {"name": "Rolled back"}})
            else:
                async with pool.acquire() as conn:
                    await config_transfer.run_import(conn, {"format": "looper-config/v4", "projects": [
                        {"name": "Rolled back"}, {"name": "Also rolled back"}]}, apply=True)
        assert not (tmp_path / "rolled-back").exists()
        assert not (tmp_path / "also-rolled-back").exists()
        assert await pool.fetchval("SELECT count(*) FROM projects") == 0
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 0

    _run(check)


def test_projects_root_task_creation_does_not_need_a_second_connection(monkeypatch, tmp_path):
    from looper.agents import projects as api
    from looper.db import connection

    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        monkeypatch.setattr(api, "_root", str(tmp_path))
        made = await asyncio.wait_for(asyncio.gather(*(
            api.create_task_core({"requirement": "Use defaults", "project": {"name": name}})
            for name in ("A B", "A+B")
        )), timeout=20)
        assert {p["work_dir"] for p in made} == {str(tmp_path / "a-b"), str(tmp_path / "a-b-2")}

    _run(check, max_size=1)


def test_projects_root_respects_existing_directory_ownership_and_permissions(monkeypatch, tmp_path):
    from looper.agents import projects as api
    from looper.db import connection, projects

    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        monkeypatch.setattr(api, "_root", str(tmp_path))
        owned = tmp_path / "Owned"
        owned.mkdir()
        await projects.create_project(name="Explicit owner", work_dir=str(owned), pool=pool)
        insensitive = (tmp_path / "owned").exists()
        made = await api.create_project({"name": "Owned"})
        assert made["work_dir"] == str(tmp_path / ("owned-2" if insensitive else "owned"))
        existing = tmp_path / "reuse"
        existing.mkdir(mode=0o700)
        made = await api.create_project({"name": "Reuse"})
        assert made["work_dir"] == str(existing) and existing.stat().st_mode & 0o777 == 0o700
        target = tmp_path / "unclaimed"
        target.mkdir(mode=0o700)
        link = tmp_path / "link"
        link.symlink_to(target, target_is_directory=True)
        made = await api.create_project({"name": "Link"})
        assert made["work_dir"] == str(link) and link.is_symlink()
        assert target.stat().st_mode & 0o777 == 0o700

    _run(check)


@pytest.mark.parametrize("problem", ["permissions", "file", "symlink-loop"])
def test_projects_root_failure_is_a_400_without_a_row(monkeypatch, tmp_path, problem):
    from looper.agents import projects as api
    from looper.db import connection

    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        root = tmp_path / "root"
        monkeypatch.setattr(api, "_root", str(root))
        if problem == "permissions":
            root.mkdir(mode=0o555)
        elif problem == "file":
            root.write_text("not a directory")
        else:
            root.symlink_to(root)
        try:
            with pytest.raises(api.RequestError) as error:
                await api.create_project({"name": "Blocked"})
            assert error.value.status == api.HTTP_400
            assert str(root / "blocked") in str(error.value) and "[Errno" in str(error.value)
            assert await pool.fetchval("SELECT count(*) FROM projects") == 0
        finally:
            if problem == "permissions":
                root.chmod(0o755)

    _run(check)


def test_projects_root_applies_to_a_task_creating_its_project_atomically(monkeypatch, tmp_path):
    from looper.agents import projects as api
    from looper.db import connection, projects, tasks

    async def check(pool):
        monkeypatch.setattr(connection, "_pool", pool)
        monkeypatch.setattr(api, "_root", str(tmp_path))
        made = await api.create_task_core({"requirement": "Use defaults", "project": {"name": "New project"}})
        assert made["work_dir"] == str(tmp_path / "new-project")
        assert (await projects.get_project(made["project_id"]))["work_dir"] == made["work_dir"]

        async def fail(**kwargs):
            raise RuntimeError("task insert failed")

        monkeypatch.setattr(tasks, "create_task", fail)
        with pytest.raises(RuntimeError, match="task insert failed"):
            await api.create_task_core({"requirement": "r", "project": {"name": "No row"}})
        assert await pool.fetchval("SELECT count(*) FROM projects") == 1
        assert not (tmp_path / "no-row").exists()

    _run(check)


def test_project_crud_and_counts() -> None:
    import asyncpg

    from looper.db import projects, tasks

    async def _test(pool: Any) -> None:
        tetris = await projects.create_project(name="Tetris", work_dir="/srv/tetris", pool=pool)
        assert re.fullmatch(r"proj-[0-9a-f]{12}", tetris["id"])
        with pytest.raises(asyncpg.UniqueViolationError):
            await projects.create_project(name="tetris", pool=pool)
        notes = await projects.create_project(name="notes", description="d", pool=pool)

        for task_id, project, status in [("task-1", tetris, "running"), ("task-2", tetris, "completed"),
                                         ("task-3", notes, "init")]:
            await tasks.create_task(task_id=task_id, requirement="r", work_dir="/w", status=status,
                                    project_id=project["id"], pool=pool)

        listed = await projects.list_projects(pool=pool)
        assert [(p["name"], p["task_count"], p["active_count"]) for p in listed] == [
            ("notes", 1, 0), ("Tetris", 2, 1)]
        got = await projects.get_project(tetris["id"], pool=pool)
        assert (got["name"], got["work_dir"], got["task_count"], got["active_count"]) == ("Tetris", "/srv/tetris", 2, 1)
        assert await projects.get_project("proj-000000000000", pool=pool) is None
        assert (await projects.find_project_by_name("TETRIS", pool=pool))["id"] == tetris["id"]
        assert await projects.find_project_by_name("tetri", pool=pool) is None

        with pytest.raises(asyncpg.UniqueViolationError):
            await projects.update_project(notes["id"], name="TETRIS", pool=pool)
        assert await projects.update_project(tetris["id"], name="TETRIS", work_dir="", pool=pool)
        got = await projects.get_project(tetris["id"], pool=pool)
        assert (got["name"], got["description"], got["work_dir"]) == ("TETRIS", "", "")
        assert got["updated_at"] > got["created_at"]
        assert not await projects.update_project("proj-000000000000", name="x", pool=pool)

        assert await tasks.set_task_project("task-3", tetris["id"], pool=pool)
        assert not await tasks.set_task_project("task-nope", tetris["id"], pool=pool)
        in_tetris = await tasks.list_tasks(project_id=tetris["id"], pool=pool)
        assert sorted(t["id"] for t in in_tetris) == ["task-1", "task-2", "task-3"]
        assert {t["project_name"] for t in in_tetris} == {"TETRIS"}
        assert await tasks.list_tasks(project_id=notes["id"], pool=pool) == []
        assert len(await tasks.list_tasks(pool=pool)) == 3
        detail = await tasks.get_task_with_steps("task-3", pool=pool)
        assert (detail["project_id"], detail["project_name"], detail["steps"]) == (tetris["id"], "TETRIS", [])

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await projects.delete_project(tetris["id"], pool=pool)
        assert await projects.delete_project(notes["id"], pool=pool)
        assert not await projects.delete_project(notes["id"], pool=pool)

        # Only the running task was active; startup stops it.
        assert await tasks.mark_orphaned_tasks_stopped(pool=pool) == 1
        assert (await tasks.get_task("task-1", pool=pool))["status"] == "stopped"

    _run(_test)


def test_a_ready_task_is_listed_left_alone_at_startup_and_starts_once() -> None:
    from looper.db import plans, projects, tasks

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="tetris", work_dir="/srv/tetris", pool=pool)
        for task_id, status in (("task-ready", tasks.READY), ("task-run", "running")):
            await tasks.create_task(task_id=task_id, requirement="r", work_dir="/w", status=status,
                                    project_id=project["id"], pool=pool)
        assert [t["id"] for t in await tasks.list_tasks(status=tasks.READY, pool=pool)] == ["task-ready"]
        # A server start stops what the previous one left running and resumes nothing that is ready.
        assert await plans.resumable_task_ids(tasks.ACTIVE_STATUSES, pool=pool) == []
        assert await tasks.mark_orphaned_tasks_stopped(pool=pool) == 1
        assert (await tasks.get_task("task-ready", pool=pool))["status"] == tasks.READY

        started = await tasks.start_ready_task("task-ready", pool=pool)
        assert (started["id"], started["status"], started["project_id"]) == ("task-ready", "pending", project["id"])
        for task_id in ("task-ready", "task-run", "task-nope"):  # started already, not ready, unknown
            assert await tasks.start_ready_task(task_id, pool=pool) is None
        assert (await tasks.get_task("task-run", pool=pool))["status"] == "stopped"

    _run(_test)


def test_task_with_a_new_project_is_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import projects as api
    from looper.db import connection, tasks

    async def _test(pool: Any) -> None:
        monkeypatch.setattr(connection, "_pool", pool)

        created = await api.create_task_core({"requirement": "r", "project": {"name": "Notes", "work_dir": "/srv/notes"}})
        row = await tasks.get_task(created["task_id"], pool=pool)
        assert (row["project_id"], row["work_dir"], row["status"]) == (created["project_id"], "/srv/notes", "init")

        with pytest.raises(api.RequestError) as taken:
            await api.create_task_core({"requirement": "r", "project": {"name": "notes"}, "work_dir": "/tmp"})
        assert taken.value.status.startswith("409")

        async def failing_insert(**_: Any) -> None:
            raise RuntimeError("task insert failed")

        monkeypatch.setattr(tasks, "create_task", failing_insert)
        with pytest.raises(RuntimeError):
            await api.create_task_core({"requirement": "r", "project": {"name": "orphan"}, "work_dir": "/tmp"})
        assert await pool.fetchval("SELECT array_agg(name) FROM projects") == ["Notes"]
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1

    _run(_test)


def test_background_round_trips_and_is_read_through_the_task() -> None:
    from looper.db import projects, tasks

    async def _test(pool: Any) -> None:
        text = "  - use uv\n\n  - Python 3.12 · 中文"
        calc = await projects.create_project(name="calc", background=text, pool=pool)
        assert calc["background"] == text
        assert (await projects.get_project(calc["id"], pool=pool))["background"] == text
        plain = await projects.create_project(name="plain", pool=pool)
        assert plain["background"] == ""
        assert [p["background"] for p in await projects.list_projects(pool=pool)] == [text, ""]

        # Changing another field keeps it; "" clears it.
        assert await projects.update_project(calc["id"], name="Calc", pool=pool)
        assert (await projects.get_project(calc["id"], pool=pool))["background"] == text
        assert await projects.update_project(calc["id"], background="", pool=pool)
        assert (await projects.get_project(calc["id"], pool=pool))["background"] == ""
        assert await projects.update_project(calc["id"], background="v2", pool=pool)

        # The project a task's agents read is the task's current one.
        await tasks.create_task(task_id="task-1", requirement="r", work_dir="/w", project_id=calc["id"], pool=pool)
        got = await projects.get_task_project("task-1", pool=pool)
        assert (got["id"], got["name"], got["description"], got["background"]) == (calc["id"], "Calc", "", "v2")
        assert await tasks.set_task_project("task-1", plain["id"], pool=pool)
        assert (await projects.get_task_project("task-1", pool=pool))["id"] == plain["id"]
        assert await projects.get_task_project("task-nope", pool=pool) is None

    _run(_test)


def test_execution_settings_are_stored_through_the_task_and_cleared() -> None:
    """The execution columns as the API stores them: given ones set, None back to NULL, the rest kept."""
    import asyncpg

    from looper.agents.project_execution import ProjectExecution, execution_fields
    from looper.db import projects, tasks

    async def _test(pool: Any) -> None:
        settings = execution_fields({"task_budget_usd": 2, "task_budget_action": "wrap_up", "supervise_interval_seconds": 0,
                                     "review_policy": "all", "distill_lessons": "draft", "planner_roles": "on",
                                     "wait_commands": "on",
                                     "role_text_max_chars": 600, "user_gates": "on", "gate_reminder_hours": 0})
        calc = await projects.create_project(name="calc", execution=settings, pool=pool)
        await tasks.create_task(task_id="task-1", requirement="r", work_dir="/w", project_id=calc["id"], pool=pool)
        assert ProjectExecution.from_row(await projects.get_task_project("task-1", pool=pool)).view() == {
            **settings, "task_budget_usd": 2.0, "step_workspaces": None, "workspace_links": None,
            "max_step_retries": None, "agent_room": None, "room_post_per_minute": None,
            "room_broadcasts_per_run": None, "room_message_max_chars": None, "room_messages_per_task": None,
            "room_ping_pong_rounds": None, "room_wait_max_seconds": None, "room_consult_budget_usd": None,
            "room_consult_timeout_seconds": None, "room_consults_per_task": None, "task_workspaces": None,
            "operator_notes": None, "vault": None}

        assert ProjectExecution.from_row(await projects.get_task_project("task-1", pool=pool)).wait_commands_on
        assert await projects.update_project(calc["id"], execution={"task_budget_usd": None, "wait_commands": None,
                                                                    "supervise_interval_seconds": 60}, pool=pool)
        row = await projects.get_project(calc["id"], pool=pool)
        assert (row["task_budget_usd"], row["supervise_interval_seconds"], row["review_policy"]) == (None, 60, "all")
        assert row["wait_commands"] is None and not ProjectExecution.from_row(row).wait_commands_on
        plain = await projects.create_project(name="plain", pool=pool)
        assert all(plain[column] is None for column in projects.EXECUTION_COLUMNS if column != "distill_lessons")
        assert plain["distill_lessons"] == "off"  # never unset
        with pytest.raises(asyncpg.CheckViolationError):  # the table refuses what validation would
            await projects.update_project(plain["id"], execution={"review_policy": "some"}, pool=pool)

    _run(_test)


def test_task_config_is_written_and_read_as_an_object() -> None:

    from looper.db import projects, tasks

    async def _test(pool: Any) -> None:
        project = await projects.create_project(name="p", pool=pool)
        await tasks.create_task(task_id="task-1", requirement="r", project_id=project["id"], config={"a": 1},
                                status="init", pool=pool)
        await tasks.update_task_config("task-1", config={"a": 2}, pool=pool)
        assert await pool.fetchval("SELECT jsonb_typeof(config) FROM tasks") == "object"

        assert (await tasks.get_task("task-1", pool=pool))["config"] == {"a": 2}
        assert (await tasks.get_task_with_steps("task-1", pool=pool))["config"] == {"a": 2}
        assert [t["config"] for t in await tasks.list_tasks(pool=pool)] == [{"a": 2}]

    _run(_test)
