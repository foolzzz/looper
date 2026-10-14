"""The ``project_skills`` table: a project's own Claude Code skills.

``loopx.agents.project_skills`` owns what a skill is (validation, caps, the
plugin it is materialized into); this module stores and reads it. Changes to
one project's skills go through ``locked_project``, the project's row lock
that its knowledge documents use too.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from .connection import get_pool
from .project_documents import locked_project

__all__ = ["delete_skill", "insert_skill", "list_enabled_skills", "list_skills", "locked_project", "update_skill"]

# Every column but the content, which can be 64 KiB: the API shows its size instead.
_COLUMNS = (
    "id, project_id, name, description, enabled, octet_length(content) AS size_bytes, created_at, updated_at"
)
_UPDATABLE = ("name", "description", "content", "enabled")
# Byte order, whatever the database's collation: the same order as the names sorted in Python.
_BY_NAME = 'ORDER BY name COLLATE "C"'


async def list_skills(
    project_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> list[dict[str, Any]]:
    """The project's skills without their content, by name."""
    pool = pool or get_pool()
    rows = await pool.fetch(f"SELECT {_COLUMNS} FROM project_skills WHERE project_id = $1 {_BY_NAME}", project_id)
    return [dict(row) for row in rows]


async def list_enabled_skills(project_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The project's enabled skills with what a run writes: ``id``, ``name``, ``description``, ``content``."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT id, name, description, content FROM project_skills WHERE project_id = $1 AND enabled {_BY_NAME}",
        project_id,
    )
    return [dict(row) for row in rows]


async def insert_skill(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    name: str,
    description: str,
    content: str,
    enabled: bool = True,
) -> dict[str, Any]:
    """Add a skill; returns its row without the content."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO project_skills (id, project_id, name, description, content, enabled)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING {_COLUMNS}
        """,
        f"skill-{uuid.uuid4().hex[:12]}", project_id, name, description, content, enabled,
    )
    return dict(row)


async def update_skill(
    conn: asyncpg.Connection, project_id: str, skill_id: str, **fields: Any,
) -> dict[str, Any] | None:
    """Set the given fields (from name, description, content and enabled).

    Returns the row without the content; None when the project has no such skill.
    """
    unknown = set(fields) - set(_UPDATABLE)
    if unknown:
        raise ValueError(f"project_skills has no updatable column {sorted(unknown)[0]!r}")
    names = [name for name in _UPDATABLE if name in fields]
    assignments = ", ".join(f"{name} = ${index}" for index, name in enumerate(names, start=3))
    row = await conn.fetchrow(
        f"UPDATE project_skills SET {assignments}{', ' if names else ''}updated_at = now() "
        f"WHERE project_id = $1 AND id = $2 RETURNING {_COLUMNS}",
        project_id, skill_id, *(fields[name] for name in names),
    )
    return dict(row) if row is not None else None


async def delete_skill(project_id: str, skill_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> bool:
    """Remove a skill; False when the project has no such skill."""
    pool = pool or get_pool()
    result = await pool.execute("DELETE FROM project_skills WHERE project_id = $1 AND id = $2", project_id, skill_id)
    return result == "DELETE 1"
