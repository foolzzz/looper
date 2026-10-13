"""The ``library_items`` and ``library_files`` tables: the imported plugins and skills.

``loopx.agents.library`` owns what an item is (scanning, validation, caps,
version hashes); this module stores and reads it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from .connection import get_pool

_ITEM_COLUMNS = "id, kind, name, source, version, sha256, enabled, size_bytes, file_count, imported_at, updated_at"


def _label(row: Mapping[str, Any]) -> str:
    return f"{row['kind']}:{row['name']}"


async def list_items(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """Every item, plugins first, then by name; without their files."""
    pool = pool or get_pool()
    rows = await pool.fetch(f"SELECT {_ITEM_COLUMNS} FROM library_items ORDER BY kind, name")
    return [dict(row) for row in rows]


async def list_enabled_items(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The enabled items' ``id``, ``kind``, ``name`` and ``sha256``, plugins first, then by name."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT id, kind, name, sha256 FROM library_items WHERE enabled ORDER BY kind, name"
    )
    return [dict(row) for row in rows]


async def get_item_files(item_id: str, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """An item's files: ``path``, ``content``, ``sha256`` and ``executable``, by path."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT path, content, sha256, executable FROM library_files WHERE item_id = $1 ORDER BY path", item_id,
    )
    return [dict(row) for row in rows]


async def set_enabled(item_id: str, enabled: bool, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """Enable or disable an item; its row, or None when there is no such item."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        f"UPDATE library_items SET enabled = $2, updated_at = now() WHERE id = $1 RETURNING {_ITEM_COLUMNS}",
        item_id, enabled,
    )
    return dict(row) if row is not None else None


async def replace_snapshot(
    items: Sequence[Mapping[str, Any]], *, pool: asyncpg.Pool | None = None,
) -> dict[str, Any]:
    """Make the library hold exactly ``items``, in one transaction.

    Each item gives ``kind``, ``name``, ``source``, ``version``, ``sha256``,
    ``size_bytes``, ``file_count`` and ``files`` (``path``, ``content``,
    ``sha256``, ``executable``). A new item is added enabled; one whose
    ``sha256`` changed gets its new files and keeps its id and its enabled
    flag; an unchanged one keeps its files (its source and version labels
    are refreshed); an item that is no longer given is removed with its files.
    Returns the ``added``, ``updated`` and ``removed`` items as
    ``kind:name`` labels and the ``unchanged`` count.
    """
    pool = pool or get_pool()
    added: list[str] = []
    updated: list[str] = []
    unchanged = 0
    async with pool.acquire() as conn, conn.transaction():
        existing = {
            (row["kind"], row["name"]): dict(row)
            for row in await conn.fetch("SELECT id, kind, name, sha256 FROM library_items FOR UPDATE")
        }
        given = set()
        for item in items:
            key = (item["kind"], item["name"])
            given.add(key)
            current = existing.get(key)
            if current is not None and current["sha256"] == item["sha256"]:
                await conn.execute(
                    "UPDATE library_items SET source = $2, version = $3 WHERE id = $1 "
                    "AND (source <> $2 OR version <> $3)",
                    current["id"], item["source"], item["version"],
                )
                unchanged += 1
                continue
            if current is None:
                item_id = f"lib-{uuid.uuid4().hex[:12]}"
                await conn.execute(
                    "INSERT INTO library_items (id, kind, name, source, version, sha256, size_bytes, file_count) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    item_id, item["kind"], item["name"], item["source"], item["version"], item["sha256"],
                    item["size_bytes"], item["file_count"],
                )
                added.append(_label(item))
            else:
                item_id = current["id"]
                await conn.execute(
                    "UPDATE library_items SET source = $2, version = $3, sha256 = $4, size_bytes = $5, "
                    "file_count = $6, imported_at = now(), updated_at = now() WHERE id = $1",
                    item_id, item["source"], item["version"], item["sha256"], item["size_bytes"], item["file_count"],
                )
                await conn.execute("DELETE FROM library_files WHERE item_id = $1", item_id)
                updated.append(_label(item))
            await conn.copy_records_to_table(
                "library_files",
                records=[(item_id, f["path"], f["content"], f["sha256"], f["executable"]) for f in item["files"]],
                columns=["item_id", "path", "content", "sha256", "executable"],
            )
        removed = sorted(key for key in existing if key not in given)
        if removed:
            await conn.execute(
                "DELETE FROM library_items WHERE id = ANY($1::text[])", [existing[key]["id"] for key in removed],
            )
    return {
        "added": added,
        "updated": updated,
        "removed": [f"{kind}:{name}" for kind, name in removed],
        "unchanged": unchanged,
    }
