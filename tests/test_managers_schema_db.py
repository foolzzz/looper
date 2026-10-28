"""Manager definitions, task templates, task attribution and chat thread owners (``looper/db/managers.py``).

The database tests run only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _profile_id(pool: Any) -> str:
    return await pool.fetchval("SELECT id FROM agent_profiles WHERE builtin ORDER BY id LIMIT 1")


async def _new_profile(pool: Any, profile_id: str) -> str:
    await pool.execute(
        "INSERT INTO agent_profiles (id, name, runtime_id, model_id) "
        "SELECT $1, $1, runtime_id, model_id FROM agent_profiles WHERE builtin ORDER BY id LIMIT 1",
        profile_id)
    return profile_id


async def _manager(pool: Any, key: str, **values: Any) -> dict[str, Any]:
    from looper.db import managers

    row = {"id": f"manager-{key}", "name": key.title(), "system_prompt": f"Be {key}.", **values}
    async with pool.acquire() as conn:
        return await managers.insert_manager(row, conn=conn)


async def _task(pool: Any, task_id: str, manager_id: str = "manager") -> None:
    from looper.db import projects, tasks

    project = await projects.create_project(name=f"p-{task_id}")
    await tasks.create_task(task_id=task_id, requirement="x", project_id=project["id"], manager_id=manager_id,
                            pool=pool)


def test_default_row_seeded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import connection

    async def body(pool: Any) -> None:
        await connection.ensure_schema(pool)
        rows = await pool.fetch("SELECT * FROM managers")
        assert [r["id"] for r in rows] == ["manager"]
        row = rows[0]
        assert (row["tools"], row["task_scope"], row["enabled"], row["system_prompt"]) == ("all", "all", True, None)
        assert row["revision"] == 0 and row["deleted_at"] is None

    run_db(body, monkeypatch)


def test_default_row_refuses_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(pool: Any) -> None:
        profile = await _profile_id(pool)
        for sql, args in (("tools='tasks'", ()), ("task_scope='own'", ()), ("enabled=false", ()),
                          ("system_prompt='x'", ()), ("profile_id=$1", (profile,))):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(f"UPDATE managers SET {sql} WHERE id = 'manager'", *args)
        await _task(pool, "t1")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await pool.execute("DELETE FROM managers WHERE id = 'manager'")

    run_db(body, monkeypatch)


def test_insert_and_constraints(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import managers

    async def body(pool: Any) -> None:
        row = await _manager(pool, "review", name="Review", system_prompt="Review code.")
        assert (row["tools"], row["task_scope"]) == ("tasks", "own")
        for i, (tools, scope) in enumerate([("all", "all"), ("all", "own"), ("tasks", "all"), ("tasks", "own")]):
            made = await _manager(pool, f"combo-{'abcd'[i]}", tools=tools, task_scope=scope)
            assert (made["tools"], made["task_scope"]) == (tools, scope)
        with pytest.raises(asyncpg.CheckViolationError, match="managers_task_scope_check"):
            await _manager(pool, "scope", task_scope="some")
        for bad in ("manager-", "manager-A", "review", "planner-x"):
            with pytest.raises(asyncpg.CheckViolationError, match="managers_id_check"):
                async with pool.acquire() as conn:
                    await managers.insert_manager({"id": bad, "name": "n" + bad, "system_prompt": "p"}, conn=conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await _manager(pool, "empty", system_prompt="  ")
        with pytest.raises(asyncpg.UniqueViolationError):
            await _manager(pool, "other", name=" review ")

    run_db(body, monkeypatch)


def test_task_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import tasks

    async def body(pool: Any) -> None:
        await _manager(pool, "review")
        await _task(pool, "t1")
        await _task(pool, "t2", "manager-review")
        first = await tasks.get_task("t1")
        assert (first["manager_id"], first["template"], first["origin_thread"]) == ("manager", None, None)
        project_id = first["project_id"]
        await tasks.create_task(task_id="t3", requirement="x", project_id=project_id,
                                manager_id="manager-review", template={"key": "pr"}, pool=pool)
        third = await tasks.get_task("t3")
        assert (third["manager_id"], third["template"]) == ("manager-review", {"key": "pr"})
        assert {t["id"] for t in await tasks.list_tasks(manager_id="manager-review")} == {"t2", "t3"}
        assert {t["id"] for t in await tasks.list_tasks()} == {"t1", "t2", "t3"}
        assert (await tasks.list_tasks(manager_id="manager-review"))[0]["template"] in ({"key": "pr"}, None)

    run_db(body, monkeypatch)


def test_session_profile_and_via(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import agent_sessions

    async def body(pool: Any) -> None:
        profile = await _profile_id(pool)
        await agent_sessions.select_profile("manager-review", profile)
        with pytest.raises(asyncpg.CheckViolationError):
            await agent_sessions.select_profile("planner-t1", profile)
        await _task(pool, "t1")
        await pool.execute("INSERT INTO task_roles (task_id, role_key, version, kind, role_prompt, via) "
                           "VALUES ('t1', 'r', 1, 'work', 'p', 'template')")

    run_db(body, monkeypatch)


def test_threads_first_owner_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import managers

    async def body(pool: Any) -> None:
        await _manager(pool, "review")
        assert await managers.thread_owner("s1") is None
        assert await managers.claim_thread("s1", "manager") == "manager"
        assert await managers.claim_thread("s1", "manager-review") == "manager"
        assert await managers.claim_thread("s9", "manager-review") == "manager-review"
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await pool.execute("DELETE FROM managers WHERE id = 'manager-review'")

    run_db(body, monkeypatch)


def test_soft_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import agent_sessions, chat, managers

    async def body(pool: Any) -> None:
        await _manager(pool, "review")
        await managers.put_template("manager-review", "pr", {"name": "PR"})
        await agent_sessions.select_profile("manager-review", await _profile_id(pool))
        await managers.claim_thread("s2", "manager-review")
        await chat.save_message(session_id="s2", message_id="m1", role="user", content="hi")
        assert await managers.delete_manager("manager-review") == "deleted"
        assert await managers.list_templates("manager-review") == []
        assert await agent_sessions.get_session("manager-review") is None
        row = await pool.fetchrow("SELECT * FROM managers WHERE id = 'manager-review'")
        assert row["deleted_at"] is not None and row["enabled"] is False
        assert await managers.thread_owner("s2") == "manager-review"
        assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE session_id = 's2'") == 1
        assert "manager-review" not in [m["id"] for m in await managers.list_managers()]
        assert await managers.get_manager("manager-review") is None
        assert (await managers.get_manager("manager-review", include_deleted=True))["id"] == "manager-review"
        assert await managers.key_used("manager-review") is True
        assert await managers.key_used("manager-none") is False
        assert await managers.delete_manager("manager-review") == "missing"
        assert await managers.delete_manager("manager") == "missing"
        await _manager(pool, "again", name="Review")
        with pytest.raises(asyncpg.UniqueViolationError):
            await _manager(pool, "review", name="Other")

    run_db(body, monkeypatch)


def test_soft_delete_frees_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import managers

    async def body(pool: Any) -> None:
        profile = await _new_profile(pool, "p-test")
        await _manager(pool, "review", profile_id=profile)
        assert await managers.delete_manager("manager-review") == "deleted"
        assert (await managers.get_manager("manager-review", include_deleted=True))["profile_id"] is None
        await pool.execute("DELETE FROM agent_profiles WHERE id = $1", profile)

    run_db(body, monkeypatch)


def test_delete_with_tasks_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import agent_sessions, managers

    async def body(pool: Any) -> None:
        await _manager(pool, "xx")
        await managers.put_template("manager-xx", "pr", {"name": "PR"})
        await agent_sessions.select_profile("manager-xx", await _profile_id(pool))
        await _task(pool, "t1", "manager-xx")
        assert await managers.has_tasks("manager-xx") is True
        assert await managers.delete_manager("manager-xx") == "has_tasks"
        assert (await managers.get_manager("manager-xx"))["enabled"] is True
        assert len(await managers.list_templates("manager-xx")) == 1
        assert await agent_sessions.get_session("manager-xx") is not None
        assert await managers.lock_for_task("manager-nope", conn=pool) is False

    run_db(body, monkeypatch)


async def _pending(task: asyncio.Task[Any]) -> bool:
    await asyncio.sleep(0.3)
    return not task.done()


def test_delete_vs_task_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import managers, projects, tasks

    async def body(pool: Any) -> None:
        project = await projects.create_project(name="p")
        await _manager(pool, "xx")
        # (a) the creator locks first: the delete waits, then sees the task.
        conn = await pool.acquire()
        tx = conn.transaction()
        await tx.start()
        assert await managers.lock_for_task("manager-xx", conn=conn) is True
        await tasks.create_task(task_id="t1", requirement="x", project_id=project["id"], manager_id="manager-xx",
                                pool=conn)
        deleting = asyncio.create_task(managers.delete_manager("manager-xx"))
        assert await _pending(deleting)
        await tx.commit()
        await pool.release(conn)
        assert await asyncio.wait_for(deleting, 5) == "has_tasks"

        # (b) the delete locks first: the creator waits, then sees it deleted.
        await _manager(pool, "yy")
        conn = await pool.acquire()
        tx = conn.transaction()
        await tx.start()
        await conn.fetchrow("SELECT deleted_at FROM managers WHERE id = 'manager-yy' FOR UPDATE")
        await conn.execute("UPDATE managers SET deleted_at = now(), enabled = false WHERE id = 'manager-yy'")
        creating = asyncio.create_task(managers.lock_for_task("manager-yy", conn=pool))
        assert await _pending(creating)
        await tx.commit()
        await pool.release(conn)
        assert await asyncio.wait_for(creating, 5) is False

        # (c) lock_managers does not block the default Manager's task creation.
        conn = await pool.acquire()
        tx = conn.transaction()
        await tx.start()
        await managers.lock_managers(conn)
        assert await asyncio.wait_for(managers.lock_for_task("manager", conn=pool), 2) is True
        await tx.rollback()
        await pool.release(conn)

    run_db(body, monkeypatch)


def test_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import managers

    async def body(pool: Any) -> None:
        await _manager(pool, "xx")
        assert await managers.revision_of("manager-xx") == (0, True)
        async with pool.acquire() as conn:
            updated = await managers.update_manager("manager-xx", {"task_scope": "all"}, conn=conn)
        assert updated["revision"] == 1 and updated["task_scope"] == "all"
        await managers.put_template("manager-xx", "pr", {"name": "PR", "inputs": [{"name": "a"}],
                                                        "role_templates": ["r"]})
        assert (await managers.get_template("manager-xx", "pr"))["inputs"] == [{"name": "a"}]
        assert await managers.revision_of("manager-xx") == (2, True)
        assert await managers.delete_template("manager-xx", "pr") == "deleted"
        assert await managers.delete_template("manager-xx", "pr") == "missing"
        assert await managers.revision_of("manager-xx") == (3, True)
        assert await managers.revision_of("manager-none") is None
        await managers.delete_manager("manager-xx")
        assert (await managers.revision_of("manager-xx"))[1] is False
        async with pool.acquire() as conn:
            assert await managers.update_manager("manager-xx", {"enabled": True}, conn=conn) is None
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE managers SET enabled = true WHERE id = 'manager-xx'")

    run_db(body, monkeypatch)


def test_connection_writes_leave_identical_configuration_and_revisions_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import managers

    async def body(pool: Any) -> None:
        async with pool.acquire() as conn, conn.transaction():
            await managers.insert_manager({"id": "manager-xx", "name": "XX", "system_prompt": "Help."}, conn=conn)
            template = {"name": "PR", "inputs": [], "role_templates": []}
            original = await managers.put_template("manager-xx", "pr", template, conn=conn, only_if_changed=True)
            revision = await managers.revision_of("manager-xx", pool=conn)
            manager = await managers.get_manager("manager-xx", pool=conn)
            stored = await managers.put_template("manager-xx", "pr", template, conn=conn, only_if_changed=True)
            updated = await managers.update_manager("manager-xx", {"name": "XX"}, conn=conn, only_if_changed=True)
            assert stored == original and updated == manager
            assert await managers.revision_of("manager-xx", pool=conn) == revision
            await managers.put_template("manager-xx", "pr", {**template, "name": "Changed"}, conn=conn,
                                        only_if_changed=True)
            await managers.update_manager("manager-xx", {"description": "Changed"}, conn=conn, only_if_changed=True)
            assert await managers.revision_of("manager-xx", pool=conn) == (revision[0] + 2, True)

    run_db(body, monkeypatch, max_size=1, timeout=10)


def test_template_write_vs_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import managers

    async def body(pool: Any) -> None:
        await _manager(pool, "xx")
        # (a) the delete locks first: the template write waits, then writes nothing.
        conn = await pool.acquire()
        tx = conn.transaction()
        await tx.start()
        await conn.fetchrow("SELECT deleted_at FROM managers WHERE id = 'manager-xx' FOR UPDATE")
        await conn.execute("UPDATE managers SET deleted_at = now(), enabled = false WHERE id = 'manager-xx'")
        writing = asyncio.create_task(managers.put_template("manager-xx", "pr", {"name": "PR"}))
        assert await _pending(writing)
        await tx.commit()
        await pool.release(conn)
        assert await asyncio.wait_for(writing, 5) is None
        assert await pool.fetchval("SELECT count(*) FROM manager_task_templates WHERE manager_id = 'manager-xx'") == 0
        revision = await managers.revision_of("manager-xx")
        assert await managers.delete_template("manager-xx", "pr") == "no_manager"
        assert await managers.revision_of("manager-xx") == revision

        # (b) the template write locks first: the delete waits, then removes the template too.
        await _manager(pool, "yy")
        conn = await pool.acquire()
        tx = conn.transaction()
        await tx.start()
        await conn.fetchrow("SELECT deleted_at FROM managers WHERE id = 'manager-yy' FOR NO KEY UPDATE")
        await conn.execute("INSERT INTO manager_task_templates (manager_id, key, name) VALUES ('manager-yy', 'pr', 'PR')")
        deleting = asyncio.create_task(managers.delete_manager("manager-yy"))
        assert await _pending(deleting)
        await tx.commit()
        await pool.release(conn)
        assert await asyncio.wait_for(deleting, 5) == "deleted"
        assert await pool.fetchval("SELECT count(*) FROM manager_task_templates WHERE manager_id = 'manager-yy'") == 0

    run_db(body, monkeypatch)
