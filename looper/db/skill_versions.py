"""Stored rows of ``skill_versions``. ``looper.agents.skill_versions`` decides what a version is.

``item_id`` has no foreign key: deleting a catalog item leaves the rows.
``tombstone_at`` marks that deletion. Cleanup uses it, not ``created_at``, so a
living skill's older versions are kept.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

_COLUMNS = (
    "version, name, description, content, sha256, author_kind, author_ref, reason, created_at, tombstone_at"
)


async def next_number(conn: asyncpg.Connection, item_id: str) -> int:
    current = await conn.fetchval(
        "SELECT COALESCE(MAX(version), 0) FROM skill_versions WHERE item_id = $1", item_id,
    )
    return int(current) + 1


async def insert_version(
    conn: asyncpg.Connection, *, item_id: str, version: int, name: str, description: str, content: str,
    sha256: str, author_kind: str, author_ref: str | None, reason: str,
) -> None:
    await conn.execute(
        "INSERT INTO skill_versions (item_id, version, name, description, content, sha256, author_kind, "
        "author_ref, reason) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        item_id, version, name, description, content, sha256, author_kind, author_ref, reason,
    )


async def point(conn: asyncpg.Connection, item_id: str, version: int) -> None:
    await conn.execute("UPDATE library_items SET skill_version = $2 WHERE id = $1", item_id, version)


async def pointer(conn: Any, item_id: str) -> int | None:
    value = await conn.fetchval("SELECT skill_version FROM library_items WHERE id = $1", item_id)
    return None if value is None else int(value)


async def has_versions(conn: Any, item_id: str) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM skill_versions WHERE item_id = $1", item_id))


async def list_versions(conn: Any, item_id: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"SELECT {_COLUMNS} FROM skill_versions WHERE item_id = $1 ORDER BY version", item_id,
    )
    return [dict(row) for row in rows]


async def get_version(conn: Any, item_id: str, version: int) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM skill_versions WHERE item_id = $1 AND version = $2", item_id, version,
    )
    return dict(row) if row is not None else None


async def affected_projects(conn: Any, item_id: str) -> list[dict[str, str]]:
    """Projects that install the skill, by name then id."""
    rows = await conn.fetch(
        "SELECT p.id AS project_id, p.name AS project_name FROM library_installs s "
        "JOIN projects p ON p.id = s.project_id WHERE s.item_id = $1 AND s.project_id IS NOT NULL "
        'ORDER BY p.name COLLATE "C", p.id',
        item_id,
    )
    return [{"project_id": row["project_id"], "project_name": row["project_name"]} for row in rows]


async def purge_before(conn: Any, cutoff: datetime) -> int:
    """Delete tombstones strictly older than ``cutoff``. A living version has ``tombstone_at`` NULL."""
    result = await conn.execute(
        "DELETE FROM skill_versions WHERE tombstone_at IS NOT NULL AND tombstone_at < $1", cutoff,
    )
    return int(result.removeprefix("DELETE "))
