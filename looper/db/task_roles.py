"""The ``task_roles`` table: every version of the roles a task's Planner defines (``looper.agents.task_roles``).

A role's current version is its newest; a new one is added with its
``role_defined`` journal entry (and, for one the user wrote, the supplement
that tells the Planner), in one transaction. A version records who wrote it
(``via``: ``planner`` or ``user``) and the project role template it was
copied from (``template_key``). Planning the task anew deletes them
(``tasks.reset_task_for_restart``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import asyncpg

from . import journal as db_journal
from .connection import get_pool

# A role's version with its profile's name, and whether its task's project allows that profile now.
_VERSION = (
    "SELECT r.task_id, r.role_key, r.version, r.kind, r.profile_id, r.role_prompt, r.harness, r.reason, "
    "r.via, r.template_key, r.items, r.created_at, a.name AS profile_name, "
    "(l.profile_id IS NOT NULL) AS profile_allowed "
    "FROM task_roles r JOIN tasks k ON k.id = r.task_id LEFT JOIN agent_profiles a ON a.id = r.profile_id "
    "LEFT JOIN project_allowed_profiles l ON l.project_id = k.project_id AND l.profile_id = r.profile_id "
)


async def project_tasks(project_id: str, since: datetime | None = None, *, pool: asyncpg.Pool | None = None,
                        ) -> list[str]:
    """The tasks of project ``project_id`` whose Planner defined a role, oldest first; those created ``since``
    when given."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT t.id FROM tasks t WHERE t.project_id = $1 AND ($2::timestamptz IS NULL OR t.created_at >= $2) "
        "AND EXISTS (SELECT 1 FROM task_roles r WHERE r.task_id = t.id) ORDER BY t.created_at, t.id",
        project_id, since)
    return [row["id"] for row in rows]


async def versions(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """Every version of task ``task_id``'s roles: by role, in the order they were first defined, oldest version
    first."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"{_VERSION} WHERE r.task_id = $1 ORDER BY "
        "(SELECT min(f.created_at) FROM task_roles f WHERE f.task_id = r.task_id AND f.role_key = r.role_key), "
        "r.role_key, r.version", task_id)
    return [dict(row) for row in rows]


async def current(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The current version of each of task ``task_id``'s roles, in the order they were first defined."""
    latest: dict[str, dict[str, Any]] = {}
    for row in await versions(task_id, pool=pool):
        latest[row["role_key"]] = row
    return list(latest.values())


async def get(task_id: str, key: str, version: int | None = None, *,
              pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """Version ``version`` of task ``task_id``'s role ``key`` (its current one without a version); None when there
    is none."""
    pool = pool or get_pool()
    if version is None:
        row = await pool.fetchrow(f"{_VERSION} WHERE r.task_id = $1 AND r.role_key = $2 ORDER BY r.version DESC "
                                  "LIMIT 1", task_id, key)
    else:
        row = await pool.fetchrow(f"{_VERSION} WHERE r.task_id = $1 AND r.role_key = $2 AND r.version = $3",
                                  task_id, key, version)
    return dict(row) if row else None


async def add_version(
    task_id: str, key: str, values: Mapping[str, Any], entry: Mapping[str, Any] | Sequence[Mapping[str, Any]], *,
    after: int, pool: asyncpg.Pool | None = None,
) -> int | None:
    """Add version ``after`` + 1 of task ``task_id``'s role ``key`` (``kind``, ``profile_id``, ``role_prompt``,
    ``harness``, ``reason``, ``via``, ``template_key``, ``items``) with its journal ``entry`` (or entries), in one
    transaction;
    its version. ``after`` is the version the writer read (0 for a new role): None, and nothing written, when the
    role's newest version is another by now. Raises ``asyncpg.UniqueViolationError`` when a concurrent writer took
    the same version."""
    pool = pool or get_pool()
    entries = [entry] if isinstance(entry, Mapping) else list(entry)
    async with pool.acquire() as conn, conn.transaction():
        version = await conn.fetchval(
            "INSERT INTO task_roles (task_id, role_key, version, kind, profile_id, role_prompt, harness, reason, "
            "via, template_key, items) "
            "SELECT $1, $2, $10::int + 1, $3, $4, $5, $6, $7, $8, $9, $11 "
            "WHERE (SELECT COALESCE(max(version), 0) FROM task_roles WHERE task_id = $1 AND role_key = $2) = $10 "
            "RETURNING version",
            task_id, key, values["kind"], values.get("profile_id"), values["role_prompt"], values.get("harness"),
            values.get("reason") or "", values.get("via") or "planner", values.get("template_key"), after,
            list(values.get("items") or ()),
        )
        if version is None:
            return None
        await db_journal.insert(conn, task_id, entries)
    return int(version)
