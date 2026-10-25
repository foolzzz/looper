"""The ``managers`` table (definitions), ``manager_task_templates`` and ``chat_threads`` (who owns a chat thread).

A Manager is the default one (``manager``) or a scenario Manager (``manager-<key>``). A scenario Manager is only
ever soft-deleted (``deleted_at``): its row stays so its chat threads keep their owner and its id is never reused.
Nothing here validates; ``looper.agents.manager_defs`` does. Soft delete only updates the row, so the foreign keys
of ``tasks`` and ``manager_task_templates`` never fire: creating a task or writing a template locks the Manager's
row (``lock_for_task``, ``put_template``) and the delete locks it too, so whichever transaction locks first wins.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

import asyncpg

from .chat import claim_thread, thread_owner  # noqa: F401 (the thread ownership helpers live with the chat)
from .connection import get_pool

DEFAULT_MANAGER_ID = "manager"

# The columns update_manager and insert_manager write.
_FIELDS = ("name", "description", "system_prompt", "profile_id", "tools", "task_scope", "task_policy", "enabled")
_TEMPLATE_FIELDS = ("name", "description", "project_id", "work_dir", "workspace_mode", "requirement",
                    "planner_instructions", "planner_profile_id", "worker_profile_id", "reviewer_profile_id",
                    "role_templates", "inputs")

_SELECT = ("SELECT m.*, p.name AS profile_name FROM managers m "
           "LEFT JOIN agent_profiles p ON p.id = m.profile_id ")


def _manager(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


def _template(row: Mapping[str, Any]) -> dict[str, Any]:
    template = dict(row)
    inputs = template.get("inputs")
    template["inputs"] = json.loads(inputs) if isinstance(inputs, str) else list(inputs or [])
    template["role_templates"] = list(template.get("role_templates") or [])
    return template


async def list_managers(*, include_deleted: bool = False,
                        pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[dict[str, Any]]:
    """Every Manager not deleted (with ``include_deleted``, the deleted ones too), the default first, then by
    name; each with its ``profile_name``."""
    pool = pool or get_pool()
    rows = await pool.fetch(f"{_SELECT} WHERE ($1::boolean OR m.deleted_at IS NULL) ORDER BY (m.id <> 'manager'), lower(m.name), m.id",
                           include_deleted)
    return [_manager(row) for row in rows]


async def get_manager(manager_id: str, *, include_deleted: bool = False,
                      pool: asyncpg.Pool | asyncpg.Connection | None = None) -> dict[str, Any] | None:
    """Manager ``manager_id``; a deleted one only with ``include_deleted``."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        f"{_SELECT} WHERE m.id = $1 AND ($2::boolean OR m.deleted_at IS NULL)", manager_id, include_deleted)
    return _manager(row) if row else None


async def key_used(manager_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> bool:
    """Whether a row has this id, deleted or not (an id is never reused)."""
    pool = pool or get_pool()
    return bool(await pool.fetchval("SELECT EXISTS (SELECT 1 FROM managers WHERE id = $1)", manager_id))


async def insert_manager(values: Mapping[str, Any], *, conn: asyncpg.Connection) -> dict[str, Any]:
    """Insert a Manager: ``id`` and the ``_FIELDS`` given (the rest take the column defaults)."""
    columns = ["id"] + [name for name in _FIELDS if name in values]
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    await conn.execute(f"INSERT INTO managers ({', '.join(columns)}) VALUES ({placeholders})",
                       *(values[name] for name in columns))
    row = await get_manager(values["id"], pool=conn)
    assert row is not None
    return row


async def update_manager(manager_id: str, values: Mapping[str, Any], *,
                         conn: asyncpg.Connection) -> dict[str, Any] | None:
    """Set the keys given in ``values`` on a Manager not deleted and bump its ``revision``; None when there is
    none."""
    names = [name for name in _FIELDS if name in values]
    sets = [f"{name} = ${i}" for i, name in enumerate(names, 2)]
    sets += ["updated_at = now()", "revision = revision + 1"]
    updated = await conn.fetchval(
        f"UPDATE managers SET {', '.join(sets)} WHERE id = $1 AND deleted_at IS NULL RETURNING id",
        manager_id, *(values[name] for name in names))
    return await get_manager(manager_id, pool=conn) if updated else None


async def lock_managers(conn: asyncpg.Connection) -> None:
    """The one lock every create and every ``enabled: true`` update takes before counting the enabled Managers.
    ``FOR NO KEY UPDATE`` serializes them against each other, not against task creation's ``FOR KEY SHARE`` on
    the default row."""
    await conn.execute("SELECT 1 FROM managers WHERE id = 'manager' FOR NO KEY UPDATE")


async def lock_for_update(manager_id: str, *, conn: asyncpg.Connection) -> dict[str, Any] | None:
    """Manager ``manager_id`` (deleted ones too) locked ``FOR UPDATE``; None when there is none."""
    row = await conn.fetchrow("SELECT * FROM managers WHERE id = $1 FOR UPDATE", manager_id)
    return _manager(row) if row else None


async def lock_for_task(manager_id: str, *, conn: asyncpg.Connection) -> bool:
    """Lock the row ``FOR KEY SHARE`` (it conflicts with ``delete_manager``, not with ``lock_managers``): whether
    the Manager exists, is enabled and is not deleted. First thing in the transaction that inserts a task."""
    return bool(await conn.fetchval(
        "SELECT enabled AND deleted_at IS NULL FROM managers WHERE id = $1 FOR KEY SHARE", manager_id))


async def delete_manager(manager_id: str) -> Literal["deleted", "missing", "has_tasks", "has_bots", "has_bindings"]:
    """Soft-delete a scenario Manager in one transaction that holds its row lock: ``"has_tasks"`` (nothing written)
    when a task belongs to it, ``"has_bots"`` when a channel bot does, ``"has_bindings"`` when a chat is explicitly
    bound to it; ``"missing"`` for the default Manager, an unknown or a deleted one. Its templates and
    its session go, its profile is let go; its chat threads and messages stay."""
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        deleted_at = await conn.fetchrow(
            "SELECT deleted_at FROM managers WHERE id = $1 AND id <> 'manager' FOR UPDATE", manager_id)
        if deleted_at is None or deleted_at["deleted_at"] is not None:
            return "missing"
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM tasks WHERE manager_id = $1)", manager_id):
            return "has_tasks"
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM channel_configs WHERE manager_id = $1)", manager_id):
            return "has_bots"
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM manager_chat_bindings WHERE manager_id = $1)", manager_id):
            return "has_bindings"
        await conn.execute("DELETE FROM manager_task_templates WHERE manager_id = $1", manager_id)
        await conn.execute("DELETE FROM agent_sessions WHERE agent_id = $1", manager_id)
        await conn.execute(
            "UPDATE managers SET deleted_at = now(), enabled = false, profile_id = NULL, updated_at = now() "
            "WHERE id = $1", manager_id)
    return "deleted"


async def count_enabled(*, conn: asyncpg.Connection | None = None) -> int:
    pool = conn or get_pool()
    return await pool.fetchval("SELECT count(*) FROM managers WHERE enabled AND deleted_at IS NULL")


async def has_tasks(manager_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> bool:
    """Whether a task belongs to the Manager (the API's early check; ``delete_manager`` decides)."""
    pool = pool or get_pool()
    return bool(await pool.fetchval("SELECT EXISTS (SELECT 1 FROM tasks WHERE manager_id = $1)", manager_id))


async def revision_of(manager_id: str, *,
                      pool: asyncpg.Pool | asyncpg.Connection | None = None) -> tuple[int, bool] | None:
    """``(revision, enabled and not deleted)`` of the Manager; None when there is none."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT revision, enabled AND deleted_at IS NULL AS live FROM managers WHERE id = $1", manager_id)
    return (row["revision"], row["live"]) if row else None


async def list_templates(manager_id: str, *,
                         pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[dict[str, Any]]:
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT * FROM manager_task_templates WHERE manager_id = $1 ORDER BY key", manager_id)
    return [_template(row) for row in rows]


async def get_template(manager_id: str, key: str, *,
                       pool: asyncpg.Pool | asyncpg.Connection | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM manager_task_templates WHERE manager_id = $1 AND key = $2", manager_id, key)
    return _template(row) if row else None


async def _lock_for_template(conn: asyncpg.Connection, manager_id: str) -> bool:
    """Lock the Manager's row ``FOR NO KEY UPDATE`` (it waits for ``delete_manager`` and the other way round):
    whether the Manager exists and is not deleted."""
    row = await conn.fetchrow("SELECT deleted_at FROM managers WHERE id = $1 FOR NO KEY UPDATE", manager_id)
    return row is not None and row["deleted_at"] is None


async def _bump(conn: asyncpg.Connection, manager_id: str) -> None:
    await conn.execute("UPDATE managers SET revision = revision + 1, updated_at = now() WHERE id = $1", manager_id)


async def put_template(manager_id: str, key: str, values: Mapping[str, Any]) -> dict[str, Any] | None:
    """Create template ``key`` of the Manager or replace it with ``values`` (``_TEMPLATE_FIELDS``; the ones not
    given take their defaults) and bump the Manager's ``revision``; None, nothing written, when the Manager is
    missing or deleted."""
    given = {name: values[name] for name in _TEMPLATE_FIELDS if name in values}
    if "inputs" in given:
        given["inputs"] = json.dumps(list(given["inputs"] or []))
    if "role_templates" in given:
        given["role_templates"] = list(given["role_templates"] or [])
    columns = ["manager_id", "key", *given]
    params = [manager_id, key, *given.values()]
    placeholders = [f"${i}" + ("::jsonb" if name == "inputs" else "") for i, name in enumerate(columns, 1)]
    updates = [f"{name} = EXCLUDED.{name}" for name in given] + ["updated_at = now()"]
    async with get_pool().acquire() as conn, conn.transaction():
        if not await _lock_for_template(conn, manager_id):
            return None
        await conn.execute(
            f"INSERT INTO manager_task_templates ({', '.join(columns)}) VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT (manager_id, key) DO UPDATE SET {', '.join(updates)}", *params)
        await _bump(conn, manager_id)
        return await get_template(manager_id, key, pool=conn)


async def delete_template(manager_id: str, key: str) -> Literal["deleted", "missing", "no_manager"]:
    """Delete template ``key`` of the Manager and bump its ``revision``; ``"no_manager"`` when the Manager is
    missing or deleted, ``"missing"`` (nothing written) when it has no such template."""
    async with get_pool().acquire() as conn, conn.transaction():
        if not await _lock_for_template(conn, manager_id):
            return "no_manager"
        if await conn.execute("DELETE FROM manager_task_templates WHERE manager_id = $1 AND key = $2",
                              manager_id, key) != "DELETE 1":
            return "missing"
        await _bump(conn, manager_id)
        return "deleted"


async def template_profile_roles(profile_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> set[str]:
    """The roles ``profile_id`` is selected for by a Manager not deleted: ``manager`` when it is a definition's
    profile, ``planner`` / ``worker`` / ``reviewer`` for the template columns that name it."""
    pool = pool or get_pool()
    roles: set[str] = set()
    if await pool.fetchval("SELECT EXISTS (SELECT 1 FROM managers WHERE deleted_at IS NULL AND profile_id = $1)",
                           profile_id):
        roles.add("manager")
    row = await pool.fetchrow(
        "SELECT coalesce(bool_or(t.planner_profile_id = $1), false) AS planner, "
        "coalesce(bool_or(t.worker_profile_id = $1), false) AS worker, "
        "coalesce(bool_or(t.reviewer_profile_id = $1), false) AS reviewer "
        "FROM manager_task_templates t JOIN managers m ON m.id = t.manager_id "
        "WHERE m.deleted_at IS NULL AND $1 IN (t.planner_profile_id, t.worker_profile_id, t.reviewer_profile_id)",
        profile_id)
    roles.update(role for role in ("planner", "worker", "reviewer") if row and row[role])
    return roles


async def template_using_project(project_id: str, *,
                                 pool: asyncpg.Pool | asyncpg.Connection | None = None) -> tuple[str, str] | None:
    """``(template name, Manager name)`` of a template that names the project; None when none does."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT t.name AS template, m.name AS manager FROM manager_task_templates t "
        "JOIN managers m ON m.id = t.manager_id WHERE t.project_id = $1 ORDER BY m.id, t.key LIMIT 1", project_id)
    return (row["template"], row["manager"]) if row else None
