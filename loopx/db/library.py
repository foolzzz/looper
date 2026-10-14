"""The capability catalog: ``library_items`` and ``library_files`` (what was acquired) and ``library_installs``
(where it loads).

``loopx.agents.library`` owns what an item is (scanning, validation, caps,
version hashes) and how a run loads it, ``loopx.agents.catalog`` what an
install may be; this module stores and reads them.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from .connection import get_pool

# An MCP server's configuration comes with its item, but of its environment and headers only the names: the
# values are secrets and are read by get_mcp_server(s) alone.
_ITEM_COLUMNS = (
    "i.id, i.kind, i.name, i.origin, i.owner_project_id, p.name AS owner_project_name, i.description, i.source, "
    "i.version, i.sha256, i.size_bytes, i.file_count, i.modified, i.imported_at, i.updated_at, "
    "m.transport AS mcp_transport, m.command AS mcp_command, m.args AS mcp_args, m.url AS mcp_url, "
    "ARRAY(SELECT k FROM jsonb_object_keys(m.env) k ORDER BY k COLLATE \"C\") AS mcp_env_keys, "
    "ARRAY(SELECT k FROM jsonb_object_keys(m.headers) k ORDER BY k COLLATE \"C\") AS mcp_header_keys"
)
_ITEMS = ("library_items i LEFT JOIN projects p ON p.id = i.owner_project_id "
          "LEFT JOIN library_mcp_servers m ON m.item_id = i.id")
_INSTALL_COLUMNS = (
    "s.id, s.item_id, s.scope, s.project_id, p.name AS project_name, s.roles, s.enabled, s.created_at, s.updated_at"
)
_INSTALLS = "library_installs s LEFT JOIN projects p ON p.id = s.project_id"
# Byte order, whatever the database's collation: the same order as the names sorted in Python.
_BY_NAME = 'i.kind, i.name COLLATE "C", i.owner_project_id NULLS FIRST'

Executor = asyncpg.Pool | asyncpg.Connection


def _label(row: Mapping[str, Any]) -> str:
    return f"{row['kind']}:{row['name']}"


def new_install_id() -> str:
    return f"inst-{uuid.uuid4().hex[:12]}"


# --- Items ---


async def list_items(*, pool: Executor | None = None) -> list[dict[str, Any]]:
    """Every item without its files: plugins first, then by name."""
    pool = pool or get_pool()
    rows = await pool.fetch(f"SELECT {_ITEM_COLUMNS} FROM {_ITEMS} ORDER BY {_BY_NAME}")
    return [dict(row) for row in rows]


async def get_item(item_id: str, *, pool: Executor | None = None) -> dict[str, Any] | None:
    """One item without its files; None when there is none."""
    pool = pool or get_pool()
    row = await pool.fetchrow(f"SELECT {_ITEM_COLUMNS} FROM {_ITEMS} WHERE i.id = $1", item_id)
    return dict(row) if row is not None else None


async def get_item_files(item_id: str, *, pool: Executor | None = None) -> list[dict[str, Any]]:
    """An item's files: ``path``, ``content``, ``sha256`` and ``executable``, by path."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT path, content, sha256, executable FROM library_files WHERE item_id = $1 ORDER BY path", item_id,
    )
    return [dict(row) for row in rows]


async def get_file_listing(item_id: str, *, pool: Executor | None = None) -> list[dict[str, Any]]:
    """An item's files without their content: ``path``, ``size_bytes`` and ``executable``, by path."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT path, octet_length(content) AS size_bytes, executable FROM library_files WHERE item_id = $1 "
        "ORDER BY path", item_id,
    )
    return [dict(row) for row in rows]


async def get_file(item_id: str, path: str, *, pool: Executor | None = None) -> str | None:
    """The content of one of an item's files; None when it has no such file."""
    pool = pool or get_pool()
    return await pool.fetchval("SELECT content FROM library_files WHERE item_id = $1 AND path = $2", item_id, path)


async def delete_item(item_id: str, *, pool: Executor | None = None) -> bool:
    """Remove an item with its files and installs; False when there is none."""
    pool = pool or get_pool()
    return await pool.execute("DELETE FROM library_items WHERE id = $1", item_id) == "DELETE 1"


async def replace_snapshot(
    items: Sequence[Mapping[str, Any]], *, pool: asyncpg.Pool | None = None,
) -> dict[str, Any]:
    """Make the plugins and skills imported from this machine (origin ``machine``) exactly ``items``, in one
    transaction.

    Each item gives ``kind``, ``name``, ``source``, ``version``, ``sha256``,
    ``size_bytes``, ``file_count`` and ``files`` (``path``, ``content``,
    ``sha256``, ``executable``). A new item is added, installed nowhere; one
    whose ``sha256`` changed gets its new files and keeps its id and its
    installs; an unchanged one keeps its files (its source and version labels
    are refreshed); an item that is no longer given is removed with its files
    and installs. Items of any other origin, and MCP servers
    (``replace_mcp_snapshot``), are left alone. Returns the
    ``added``, ``updated`` and ``removed`` items as ``kind:name`` labels and the
    ``unchanged`` count.
    """
    pool = pool or get_pool()
    added: list[str] = []
    updated: list[str] = []
    unchanged = 0
    async with pool.acquire() as conn, conn.transaction():
        existing = {
            (row["kind"], row["name"]): dict(row)
            for row in await conn.fetch(
                "SELECT id, kind, name, sha256 FROM library_items WHERE origin = 'machine' AND kind <> 'mcp' FOR UPDATE"
            )
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
                    "INSERT INTO library_items (id, kind, name, source, version, sha256, size_bytes, file_count, "
                    "origin) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'machine')",
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


# --- Items uploaded, taken from git or written in the UI (loopx.agents.catalog_sources) ---


async def _write_files(conn: asyncpg.Connection, item_id: str, files: Sequence[Mapping[str, Any]]) -> None:
    await conn.execute("DELETE FROM library_files WHERE item_id = $1", item_id)
    await conn.copy_records_to_table(
        "library_files",
        records=[(item_id, f["path"], f["content"], f["sha256"], f["executable"]) for f in files],
        columns=["item_id", "path", "content", "sha256", "executable"],
    )


async def insert_item(conn: asyncpg.Connection, item: Mapping[str, Any], *, origin: str) -> dict[str, Any]:
    """Add an item outside any project, installed nowhere; returns it.

    ``item`` gives ``kind``, ``name``, ``source``, ``version``,
    ``description``, ``sha256``, ``size_bytes``, ``file_count`` and ``files``.
    Raises ``asyncpg.UniqueViolationError`` when an item outside projects has its kind and name.
    """
    item_id = f"lib-{uuid.uuid4().hex[:12]}"
    await conn.execute(
        "INSERT INTO library_items (id, kind, name, source, version, sha256, size_bytes, file_count, origin, "
        "description) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        item_id, item["kind"], item["name"], item["source"], item["version"], item["sha256"], item["size_bytes"],
        item["file_count"], origin, item["description"],
    )
    await _write_files(conn, item_id, item["files"])
    row = await get_item(item_id, pool=conn)
    assert row is not None
    return row


async def replace_item(
    conn: asyncpg.Connection, item_id: str, item: Mapping[str, Any], *, modified: bool,
) -> dict[str, Any]:
    """Replace an item's name, labels, description and files, keeping its id, origin and installs; returns it.

    ``item`` is as ``insert_item`` takes it. Raises ``asyncpg.UniqueViolationError`` when the new name is taken.
    """
    await conn.execute(
        "UPDATE library_items SET name = $2, source = $3, version = $4, description = $5, sha256 = $6, "
        "size_bytes = $7, file_count = $8, modified = $9, updated_at = now() WHERE id = $1",
        item_id, item["name"], item["source"], item["version"], item["description"], item["sha256"],
        item["size_bytes"], item["file_count"], modified,
    )
    await _write_files(conn, item_id, item["files"])
    row = await get_item(item_id, pool=conn)
    assert row is not None
    return row


async def catalog_bytes(*, pool: Executor | None = None) -> int:
    """The size of every item in the catalog."""
    pool = pool or get_pool()
    return int(await pool.fetchval("SELECT COALESCE(sum(size_bytes), 0) FROM library_items"))


# --- MCP servers: an item of kind 'mcp' and its library_mcp_servers row ---


def _jsonb(mapping: Mapping[str, str]) -> str:
    return json.dumps(dict(mapping), ensure_ascii=False, sort_keys=True)


async def insert_mcp(
    conn: asyncpg.Connection, *, item_id: str, name: str, description: str, origin: str, source: str,
    server: Mapping[str, Any], sha256: str, size_bytes: int,
) -> None:
    """Add an MCP server (``server`` holds the ``library_mcp_servers`` columns), installed nowhere.

    Raises ``asyncpg.UniqueViolationError`` when the name is taken.
    """
    await conn.execute(
        "INSERT INTO library_items (id, kind, name, source, version, sha256, size_bytes, file_count, origin, "
        "description) VALUES ($1, 'mcp', $2, $3, '', $4, $5, 0, $6, $7)",
        item_id, name, source, sha256, size_bytes, origin, description,
    )
    await conn.execute(
        "INSERT INTO library_mcp_servers (item_id, transport, command, args, env, url, headers) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb)",
        item_id, server["transport"], server["command"], list(server["args"]), _jsonb(server["env"]),
        server["url"], _jsonb(server["headers"]),
    )


async def update_mcp(
    conn: asyncpg.Connection, item_id: str, *, name: str, description: str, server: Mapping[str, Any],
    sha256: str, size_bytes: int, source: str | None = None,
) -> None:
    """Replace an MCP server's name, description and configuration; ``source`` too when given."""
    await conn.execute(
        "UPDATE library_items SET name = $2, description = $3, sha256 = $4, size_bytes = $5, "
        "source = COALESCE($6, source), updated_at = now() WHERE id = $1",
        item_id, name, description, sha256, size_bytes, source,
    )
    await conn.execute(
        "UPDATE library_mcp_servers SET transport = $2, command = $3, args = $4, env = $5::jsonb, url = $6, "
        "headers = $7::jsonb WHERE item_id = $1",
        item_id, server["transport"], server["command"], list(server["args"]), _jsonb(server["env"]),
        server["url"], _jsonb(server["headers"]),
    )


_MCP_COLUMNS = ("i.id, i.name, i.description, i.origin, i.sha256, m.transport, m.command, m.args, m.env, m.url, "
                "m.headers")


async def get_mcp_server(
    item_id: str, *, pool: Executor | None = None, for_update: bool = False,
) -> dict[str, Any] | None:
    """An MCP server's item and whole configuration, values included; None when it is not one."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        f"SELECT {_MCP_COLUMNS} FROM library_items i JOIN library_mcp_servers m ON m.item_id = i.id "
        f"WHERE i.id = $1{' FOR UPDATE OF i, m' if for_update else ''}", item_id,
    )
    return dict(row) if row is not None else None


async def get_mcp_servers(item_ids: Sequence[str], *, pool: Executor | None = None) -> dict[str, dict[str, Any]]:
    """The whole configuration of each MCP server in ``item_ids``, values included, by item id."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT {_MCP_COLUMNS} FROM library_items i JOIN library_mcp_servers m ON m.item_id = i.id "
        "WHERE i.id = ANY($1::text[])", list(item_ids),
    )
    return {row["id"]: dict(row) for row in rows}


async def replace_mcp_snapshot(
    servers: Sequence[Mapping[str, Any]], *, servers_max: int, pool: asyncpg.Pool | None = None,
) -> dict[str, Any]:
    """Make the MCP servers imported from this machine exactly ``servers``, in one transaction.

    Each gives ``name``, ``source``, ``server`` (the ``library_mcp_servers``
    columns), ``sha256`` and ``size_bytes``. A new one is added, installed
    nowhere; a changed one gets its new configuration and keeps its id, its
    description and its installs; one no longer given is removed with its
    installs. A server added here (another origin) keeps its name: an
    imported one of that name is not imported (``taken``). Raises
    ``CatalogError`` when the catalog would hold more than ``servers_max``.
    """
    from ..agents.catalog import CatalogError

    pool = pool or get_pool()
    added: list[str] = []
    updated: list[str] = []
    taken: list[str] = []
    unchanged = 0
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('loopx.catalog.mcp'))")
        rows = await conn.fetch(f"SELECT {_MCP_COLUMNS} FROM library_items i JOIN library_mcp_servers m "
                                "ON m.item_id = i.id FOR UPDATE OF i, m")
        existing = {row["name"]: dict(row) for row in rows if row["origin"] == "machine"}
        others = {row["name"] for row in rows if row["origin"] != "machine"}
        given = set()
        for entry in servers:
            name = entry["name"]
            if name in others:
                taken.append(name)
                continue
            given.add(name)
            current = existing.get(name)
            if current is None:
                await insert_mcp(conn, item_id=f"mcp-{uuid.uuid4().hex[:12]}", name=name, description="",
                                 origin="machine", source=entry["source"], server=entry["server"],
                                 sha256=entry["sha256"], size_bytes=entry["size_bytes"])
                added.append(f"mcp:{name}")
            elif current["sha256"] != entry["sha256"]:
                await update_mcp(conn, current["id"], name=name, description=current["description"],
                                 server=entry["server"], sha256=entry["sha256"], size_bytes=entry["size_bytes"],
                                 source=entry["source"])
                updated.append(f"mcp:{name}")
            else:
                unchanged += 1
        removed = sorted(name for name in existing if name not in given)
        if removed:
            await conn.execute("DELETE FROM library_items WHERE id = ANY($1::text[])",
                               [existing[name]["id"] for name in removed])
        if len(others) + len(given) > servers_max:
            raise CatalogError(f"the catalog would hold {len(others) + len(given)} MCP servers, over its cap of "
                               f"{servers_max}; nothing was imported")
    return {"added": added, "updated": updated, "removed": [f"mcp:{name}" for name in removed],
            "unchanged": unchanged, "taken": taken}


# --- Project-authored skills: an item owned by a project, with one file, SKILL.md ---


async def list_owned_items(project_id: str, *, pool: Executor | None = None) -> list[dict[str, Any]]:
    """The items the project wrote, by name."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT {_ITEM_COLUMNS} FROM {_ITEMS} WHERE i.owner_project_id = $1 ORDER BY {_BY_NAME}", project_id,
    )
    return [dict(row) for row in rows]


async def insert_owned_skill(
    conn: asyncpg.Connection, *, item_id: str, project_id: str, name: str, description: str,
    file: Mapping[str, Any], sha256: str, roles: Sequence[str], enabled: bool,
) -> dict[str, Any]:
    """Add a project-authored skill (``file`` is its SKILL.md) and install it in its project; returns the item."""
    content = str(file["content"])
    await conn.execute(
        "INSERT INTO library_items (id, kind, name, source, version, sha256, size_bytes, file_count, origin, "
        "owner_project_id, description) VALUES ($1, 'skill', $2, '', '', $3, $4, 1, 'project', $5, $6)",
        item_id, name, sha256, len(content.encode("utf-8")), project_id, description,
    )
    await conn.execute(
        "INSERT INTO library_files (item_id, path, content, sha256, executable) VALUES ($1, $2, $3, $4, false)",
        item_id, file["path"], content, file["sha256"],
    )
    await insert_install(conn, item_id=item_id, scope="project", project_id=project_id, roles=roles, enabled=enabled)
    row = await get_item(item_id, pool=conn)
    assert row is not None
    return row


async def update_owned_skill(
    conn: asyncpg.Connection, item_id: str, *, name: str, description: str, file: Mapping[str, Any], sha256: str,
) -> dict[str, Any]:
    """Replace a project-authored skill's name, description and SKILL.md; returns the item."""
    content = str(file["content"])
    await conn.execute(
        "UPDATE library_items SET name = $2, description = $3, sha256 = $4, size_bytes = $5, updated_at = now() "
        "WHERE id = $1",
        item_id, name, description, sha256, len(content.encode("utf-8")),
    )
    await conn.execute("DELETE FROM library_files WHERE item_id = $1", item_id)
    await conn.execute(
        "INSERT INTO library_files (item_id, path, content, sha256, executable) VALUES ($1, $2, $3, $4, false)",
        item_id, file["path"], content, file["sha256"],
    )
    row = await get_item(item_id, pool=conn)
    assert row is not None
    return row


async def project_skill_names(
    project_id: str, *, pool: Executor | None = None,
) -> list[dict[str, Any]]:
    """The project-authored skills that load in the project as ``project:<name>``, or would once installed.

    That is, the ones the project wrote and the ones written elsewhere that
    are installed in it: ``id``, ``name`` and ``owner_project_id``.
    """
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT i.id, i.name, i.owner_project_id FROM library_items i WHERE i.origin = 'project' "
        "AND (i.owner_project_id = $1 OR EXISTS (SELECT 1 FROM library_installs s WHERE s.item_id = i.id "
        "AND s.project_id = $1))",
        project_id,
    )
    return [dict(row) for row in rows]


# --- Installs ---


async def list_installs(
    *, item_ids: Sequence[str] | None = None, project_id: str | None = None, pool: Executor | None = None,
) -> list[dict[str, Any]]:
    """Installs, global ones first, then by project name: all, those of ``item_ids``, or those in ``project_id``
    and the global ones."""
    pool = pool or get_pool()
    where, args = [], []
    if item_ids is not None:
        args.append(list(item_ids))
        where.append(f"s.item_id = ANY(${len(args)}::text[])")
    if project_id is not None:
        args.append(project_id)
        where.append(f"(s.scope = 'global' OR s.project_id = ${len(args)})")
    rows = await pool.fetch(
        f"SELECT {_INSTALL_COLUMNS} FROM {_INSTALLS} {'WHERE ' + ' AND '.join(where) if where else ''} "
        "ORDER BY s.scope, p.name COLLATE \"C\" NULLS FIRST, s.id",
        *args,
    )
    return [dict(row) for row in rows]


async def get_install(install_id: str, *, pool: Executor | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow(f"SELECT {_INSTALL_COLUMNS} FROM {_INSTALLS} WHERE s.id = $1", install_id)
    return dict(row) if row is not None else None


async def insert_install(
    conn: Executor, *, item_id: str, scope: str, project_id: str | None, roles: Sequence[str], enabled: bool,
) -> dict[str, Any]:
    """Install an item; returns the install. Raises ``asyncpg.UniqueViolationError`` when it is there already."""
    install_id = new_install_id()
    await conn.execute(
        "INSERT INTO library_installs (id, item_id, scope, project_id, roles, enabled) VALUES ($1, $2, $3, $4, $5, $6)",
        install_id, item_id, scope, project_id, list(roles), enabled,
    )
    row = await get_install(install_id, pool=conn)
    assert row is not None
    return row


async def update_install(
    install_id: str, *, roles: Sequence[str] | None = None, enabled: bool | None = None, pool: Executor | None = None,
) -> dict[str, Any] | None:
    """Change an install's roles or enabled flag; the install, or None when there is none."""
    pool = pool or get_pool()
    updated = await pool.fetchval(
        "UPDATE library_installs SET roles = COALESCE($2, roles), enabled = COALESCE($3, enabled), "
        "updated_at = now() WHERE id = $1 RETURNING id",
        install_id, list(roles) if roles is not None else None, enabled,
    )
    return await get_install(install_id, pool=pool) if updated else None


async def delete_install(install_id: str, *, pool: Executor | None = None) -> bool:
    pool = pool or get_pool()
    return await pool.execute("DELETE FROM library_installs WHERE id = $1", install_id) == "DELETE 1"


async def list_installed(role: str, project_id: str | None, *, pool: Executor | None = None) -> list[dict[str, Any]]:
    """The installs one run of ``role`` in ``project_id`` loads: the enabled ones naming the role, global or in
    the project.

    Each row is an install with its item's ``id``, ``kind``, ``name``,
    ``origin``, ``owner_project_id`` and ``sha256``: plugins first, then by
    name, the global install before the project's. An item installed in both
    places has two rows.
    """
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT i.id, i.kind, i.name, i.origin, i.owner_project_id, i.sha256, s.scope "
        "FROM library_installs s JOIN library_items i ON i.id = s.item_id "
        "WHERE s.enabled AND $1 = ANY(s.roles) AND (s.scope = 'global' OR s.project_id = $2) "
        f"ORDER BY {_BY_NAME}, s.scope",
        role, project_id,
    )
    return [dict(row) for row in rows]
