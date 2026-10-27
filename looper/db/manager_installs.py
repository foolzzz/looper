"""Additive Manager capabilities, serialized with Manager soft deletion."""

from __future__ import annotations

from typing import Any

import asyncpg

from .connection import get_pool
from .library import _BY_NAME
from .managers import lock_installs


async def _lock(conn: asyncpg.Connection, manager_id: str) -> None:
    from ..agents.catalog import CatalogNotFound

    await lock_installs(manager_id, conn=conn)
    row = await conn.fetchrow("SELECT deleted_at FROM managers WHERE id = $1 FOR NO KEY UPDATE", manager_id)
    if row is None or row["deleted_at"] is not None:
        raise CatalogNotFound(f"no Manager {manager_id!r}")


async def list_installs(manager_id: str, *, conn: asyncpg.Connection | None = None) -> list[dict[str, Any]]:
    if conn is None:
        async with get_pool().acquire() as connection, connection.transaction():
            return await list_installs(manager_id, conn=connection)
    await _lock(conn, manager_id)
    rows = await conn.fetch(
        "SELECT s.*, i.kind, i.name FROM manager_library_installs s JOIN library_items i ON i.id = s.item_id "
        f"WHERE s.manager_id = $1 ORDER BY {_BY_NAME}, i.id", manager_id)
    return [dict(row) for row in rows]


async def inherited(manager_id: str, *, conn: asyncpg.Connection) -> list[dict[str, Any]]:
    await _lock(conn, manager_id)
    rows = await conn.fetch(
        "SELECT s.item_id, i.kind, i.name, s.enabled, s.updated_at FROM library_installs s "
        "JOIN library_items i ON i.id = s.item_id WHERE s.scope = 'global' AND s.enabled "
        "AND 'manager' = ANY(s.roles) AND i.kind IN ('plugin', 'skill') "
        f"ORDER BY {_BY_NAME}, i.id")
    return [dict(row) for row in rows]


async def put_install(manager_id: str, item_id: str, enabled: bool = True, *,
                      conn: asyncpg.Connection | None = None) -> None:
    from ..agents.catalog import CatalogError, CatalogNotFound

    if conn is None:
        async with get_pool().acquire() as connection, connection.transaction():
            return await put_install(manager_id, item_id, enabled, conn=connection)
    await _lock(conn, manager_id)
    if not isinstance(enabled, bool):
        raise CatalogError("enabled must be true or false")
    item = await conn.fetchrow("SELECT kind, origin, owner_project_id FROM library_items WHERE id=$1 FOR SHARE", item_id)
    if item is None:
        raise CatalogNotFound(f"no catalog item {item_id!r}")
    if item["kind"] not in ("plugin", "skill") or item["origin"] == "project" or item["owner_project_id"] is not None:
        raise CatalogError("Manager installs require a global catalog plugin or skill")
    await conn.execute(
        "INSERT INTO manager_library_installs (manager_id, item_id, enabled) VALUES ($1,$2,$3) "
        "ON CONFLICT (manager_id,item_id) DO UPDATE SET enabled=EXCLUDED.enabled, updated_at=now() "
        "WHERE manager_library_installs.enabled IS DISTINCT FROM EXCLUDED.enabled", manager_id, item_id, enabled)


async def delete_install(manager_id: str, item_id: str, *, conn: asyncpg.Connection | None = None) -> None:
    from ..agents.catalog import CatalogConflict, CatalogNotFound

    if conn is None:
        async with get_pool().acquire() as connection, connection.transaction():
            return await delete_install(manager_id, item_id, conn=connection)
    await _lock(conn, manager_id)
    if await conn.execute("DELETE FROM manager_library_installs WHERE manager_id=$1 AND item_id=$2", manager_id, item_id) == "DELETE 1":
        return
    if any(row["item_id"] == item_id for row in await inherited(manager_id, conn=conn)):
        raise CatalogConflict("an inherited global install cannot be deleted for one Manager")
    raise CatalogNotFound(f"no explicit install of {item_id!r} on Manager {manager_id!r}")


async def list_installed(manager_id: str) -> list[dict[str, Any]]:
    """Enabled runtime rows, including cold/disabled definitions but never deleted owners."""
    rows = await get_pool().fetch(
        "SELECT i.id, i.kind, i.name, i.origin, i.owner_project_id, i.sha256, 'manager' AS scope "
        "FROM manager_library_installs s JOIN managers d ON d.id=s.manager_id "
        "JOIN library_items i ON i.id=s.item_id WHERE s.manager_id=$1 AND s.enabled AND d.deleted_at IS NULL "
        f"ORDER BY {_BY_NAME}, i.id", manager_id)
    return [dict(row) for row in rows]


async def item_use_counts() -> dict[str, int]:
    rows = await get_pool().fetch("SELECT item_id, count(*) AS uses FROM manager_library_installs GROUP BY item_id")
    return {row["item_id"]: row["uses"] for row in rows}
