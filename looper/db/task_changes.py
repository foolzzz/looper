"""A task's changes once it exists: supplements and requirement edits, and whether its Planner has had each.

- A **supplement** is what the user added to a running task (a ``task_journal``
  entry of kind ``supplement``): sent to the Planner on the Chat page, or
  passed on by the Manager. Supplements are only added, never changed.
- A **requirement edit** replaces ``tasks.requirement`` and keeps every text it
  had (``task_requirement_versions``; version 1 is the text the task was
  created with, written with the first edit).

``delivered_at`` is when the task's Planner had the change in a turn; until
then the change is pending (``pending``) and the Planner's next turn gives it,
also after a failed turn or a restart (``mark_delivered`` runs only once the
turn's reply is done). A task without a Planner yet plans the latest text, so
its edits are delivered at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from .connection import get_pool

SUPPLEMENT = "supplement"


@dataclass(frozen=True)
class Supplement:
    id: int
    text: str
    via: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class RequirementEdit:
    """The latest version of an edited requirement its Planner has not had, and the version before the first such
    edit, which the Planner had."""

    version: int
    text: str
    before_version: int
    before_text: str


@dataclass(frozen=True)
class TaskChanges:
    """A task's changes its Planner has not had in a turn yet, oldest first."""

    supplements: tuple[Supplement, ...] = ()
    requirement: RequirementEdit | None = None

    @property
    def empty(self) -> bool:
        return not self.supplements and self.requirement is None


async def add_supplement(task_id: str, text: str, *, via: str, pool: asyncpg.Pool | None = None) -> int:
    """Add a supplement to ``task_id``, not yet delivered to its Planner; its journal id."""
    pool = pool or get_pool()
    return await pool.fetchval(
        "INSERT INTO task_journal (task_id, step_id, kind, summary, via) VALUES ($1, '', $2, $3, $4) RETURNING id",
        task_id, SUPPLEMENT, text, via,
    )


async def edit_requirement(
    task_id: str, text: str, *, via: str, delivered: bool, pool: asyncpg.Pool | None = None,
) -> int | None:
    """Make ``text`` the task's requirement and keep the old one; the new version's number, or None when there is
    no such task. The first edit also writes version 1, the text the task had. ``delivered``: the task has no
    Planner yet, so nothing has to give it the edit."""
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        current = await conn.fetchrow("SELECT requirement, created_at FROM tasks WHERE id = $1 FOR UPDATE", task_id)
        if current is None:
            return None
        latest = await conn.fetchval(
            "SELECT max(version) FROM task_requirement_versions WHERE task_id = $1", task_id)
        if latest is None:
            await conn.execute(
                "INSERT INTO task_requirement_versions (task_id, version, text, via, created_at, delivered_at) "
                "VALUES ($1, 1, $2, 'created', $3, $3)", task_id, current["requirement"], current["created_at"])
            latest = 1
        version = latest + 1
        await conn.execute(
            "INSERT INTO task_requirement_versions (task_id, version, text, via, delivered_at) "
            "VALUES ($1, $2, $3, $4, CASE WHEN $5 THEN now() END)", task_id, version, text, via, delivered)
        await conn.execute("UPDATE tasks SET requirement = $2, updated_at = now() WHERE id = $1", task_id, text)
    return version


async def pending(task_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> TaskChanges:
    """The task's changes its Planner has not had yet."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT id, summary, via, created_at FROM task_journal "
        "WHERE task_id = $1 AND kind = $2 AND delivered_at IS NULL ORDER BY id", task_id, SUPPLEMENT)
    versions = await pool.fetch(
        "SELECT version, text, delivered_at FROM task_requirement_versions WHERE task_id = $1 ORDER BY version",
        task_id)
    requirement = None
    undelivered = [row for row in versions if row["delivered_at"] is None]
    if undelivered:
        first = undelivered[0]["version"]
        before = next((row for row in reversed(versions) if row["version"] < first), versions[0])
        latest = undelivered[-1]
        requirement = RequirementEdit(latest["version"], latest["text"], before["version"], before["text"])
    return TaskChanges(
        tuple(Supplement(row["id"], row["summary"], row["via"], row["created_at"]) for row in rows), requirement)


async def mark_delivered(
    task_id: str, changes: TaskChanges, *, pool: asyncpg.Pool | None = None,
) -> None:
    """Record that the task's Planner had ``changes`` in a turn (a later change stays pending)."""
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        if changes.supplements:
            await conn.execute(
                "UPDATE task_journal SET delivered_at = now() WHERE task_id = $1 AND id = ANY($2::bigint[]) "
                "AND delivered_at IS NULL", task_id, [s.id for s in changes.supplements])
        if changes.requirement is not None:
            await conn.execute(
                "UPDATE task_requirement_versions SET delivered_at = now() "
                "WHERE task_id = $1 AND version <= $2 AND delivered_at IS NULL", task_id, changes.requirement.version)


async def requirement_versions(task_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """Every version of the task's requirement, oldest first; none for a task never edited."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT version, text, via, created_at, delivered_at FROM task_requirement_versions "
        "WHERE task_id = $1 ORDER BY version", task_id)
    return [dict(row) for row in rows]

