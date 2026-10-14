"""Reads and writes for the configuration file (``loopx.agents.config_transfer``).

Every function takes a connection, so an export reads one snapshot and an
import applies in one transaction. Secrets (an MCP server's environment and
header values) are read here and leave only through the export's own rules.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

ROLE_AGENTS = ("manager", "planner", "worker", "reviewer")


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def has_schema(conn: asyncpg.Connection) -> bool:
    """Whether the database holds LoopX's tables (a new one does not until ``ensure_schema``)."""
    return await conn.fetchval("SELECT to_regclass('library_installs') IS NOT NULL")


# --- Reading ---


async def read_layers(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT scope, role, project_id, harness_prompt, model, effort, env, run_budget_usd FROM agent_setting_layers "
        "ORDER BY project_id NULLS FIRST, scope, role NULLS FIRST"
    )
    return [{**dict(row), "env": _json(row["env"])} for row in rows]


async def read_roles(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT agent_id, system_prompt, default_prompt, timeout_seconds, max_concurrency, enabled FROM agent_configs "
        "WHERE agent_id = ANY($1::text[]) ORDER BY agent_id", list(ROLE_AGENTS),
    )
    return [dict(row) for row in rows]


async def read_items(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Every catalog item without its files, with its owner project's name and, for an MCP server, its whole
    configuration (values included)."""
    rows = await conn.fetch(
        "SELECT i.id, i.kind, i.name, i.origin, i.owner_project_id, p.name AS owner_project_name, i.description, "
        "i.source, i.version, i.sha256, i.size_bytes, i.file_count, i.modified, m.transport, m.command, m.args, "
        "m.env, m.url, m.headers "
        "FROM library_items i LEFT JOIN projects p ON p.id = i.owner_project_id "
        "LEFT JOIN library_mcp_servers m ON m.item_id = i.id "
        "ORDER BY i.owner_project_id NULLS FIRST, i.kind, i.name COLLATE \"C\""
    )
    return [{**dict(row), "env": _json(row["env"]), "headers": _json(row["headers"])} for row in rows]


async def read_item_files(conn: asyncpg.Connection, item_id: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT path, content, sha256, executable FROM library_files WHERE item_id = $1 ORDER BY path COLLATE \"C\"",
        item_id,
    )
    return [dict(row) for row in rows]


async def read_installs(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Every install with its item's kind, name and owner project name, and its project's name."""
    rows = await conn.fetch(
        "SELECT s.id, s.item_id, s.scope, s.project_id, p.name AS project_name, s.roles, s.enabled, "
        "i.kind, i.name, o.name AS owner_project_name "
        "FROM library_installs s JOIN library_items i ON i.id = s.item_id "
        "LEFT JOIN projects p ON p.id = s.project_id LEFT JOIN projects o ON o.id = i.owner_project_id "
        "ORDER BY s.scope, p.name COLLATE \"C\" NULLS FIRST, i.kind, i.name COLLATE \"C\", o.name COLLATE \"C\""
    )
    return [dict(row) for row in rows]


async def read_channels(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT channel_id, channel_type, display_name, enabled, config FROM channel_configs ORDER BY channel_id"
    )
    return [{**dict(row), "config": _json(row["config"])} for row in rows]


async def read_projects(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    from .projects import EXECUTION_COLUMNS

    rows = await conn.fetch(
        f"SELECT id, name, description, background, {', '.join(EXECUTION_COLUMNS)} FROM projects "
        "ORDER BY lower(name)"
    )
    return [dict(row) for row in rows]


async def read_documents(conn: asyncpg.Connection, *, content: bool) -> list[dict[str, Any]]:
    """Every project's knowledge documents, oldest first within a project; ``content`` reads the stored text."""
    rows = await conn.fetch(
        "SELECT id, project_id, title, summary, source_kind, filename, path, size_bytes, sha256"
        f"{', content' if content else ''} FROM project_documents ORDER BY project_id, created_at, id"
    )
    return [dict(row) for row in rows]


# --- Writing (inside the import's transaction) ---


def new_item_id(kind: str, origin: str) -> str:
    prefix = "mcp" if kind == "mcp" else "skill" if origin == "project" else "lib"
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def write_item(
    conn: asyncpg.Connection, item_id: str | None, item: Mapping[str, Any], *, owner_project_id: str | None,
    files: Sequence[Mapping[str, Any]] | None, server: Mapping[str, Any] | None,
) -> str:
    """Add an item (``item_id`` None) or replace one's fields, keeping its id and installs; returns its id.

    ``item`` gives ``kind``, ``name``, ``origin``, ``source``, ``version``,
    ``description``, ``modified``, ``sha256``, ``size_bytes`` and
    ``file_count``; ``files`` (None: keep them) or ``server`` (an MCP
    server's ``library_mcp_servers`` columns) go with it.
    """
    values = [item["name"], item["origin"], item["source"], item["version"], item["description"], item["modified"],
              item["sha256"], item["size_bytes"], item["file_count"]]
    if item_id is None:
        item_id = new_item_id(item["kind"], item["origin"])
        await conn.execute(
            "INSERT INTO library_items (id, kind, name, origin, source, version, description, modified, sha256, "
            "size_bytes, file_count, owner_project_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
            item_id, item["kind"], *values, owner_project_id,
        )
    else:
        await conn.execute(
            "UPDATE library_items SET name = $2, origin = $3, source = $4, version = $5, description = $6, "
            "modified = $7, sha256 = $8, size_bytes = $9, file_count = $10, updated_at = now() WHERE id = $1",
            item_id, *values,
        )
    if server is not None:
        await conn.execute(
            "INSERT INTO library_mcp_servers (item_id, transport, command, args, env, url, headers) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb) ON CONFLICT (item_id) DO UPDATE SET "
            "transport = EXCLUDED.transport, command = EXCLUDED.command, args = EXCLUDED.args, env = EXCLUDED.env, "
            "url = EXCLUDED.url, headers = EXCLUDED.headers",
            item_id, server["transport"], server["command"], list(server["args"]),
            json.dumps(dict(server["env"]), ensure_ascii=False, sort_keys=True), server["url"],
            json.dumps(dict(server["headers"]), ensure_ascii=False, sort_keys=True),
        )
    if files is not None:
        await conn.execute("DELETE FROM library_files WHERE item_id = $1", item_id)
        await conn.copy_records_to_table(
            "library_files",
            records=[(item_id, f["path"], f["content"], f["sha256"], bool(f["executable"])) for f in files],
            columns=["item_id", "path", "content", "sha256", "executable"],
        )
    return item_id


async def write_install(
    conn: asyncpg.Connection, install_id: str | None, *, item_id: str, project_id: str | None,
    roles: Sequence[str], enabled: bool,
) -> None:
    """Add an install (``install_id`` None) or set an existing one's roles and enabled flag."""
    if install_id is None:
        from .library import insert_install

        await insert_install(conn, item_id=item_id, scope="project" if project_id else "global",
                             project_id=project_id, roles=roles, enabled=enabled)
    else:
        await conn.execute("UPDATE library_installs SET roles = $2, enabled = $3, updated_at = now() WHERE id = $1",
                           install_id, list(roles), enabled)


async def write_layer(
    conn: asyncpg.Connection, scope: str, role: str | None, project_id: str | None, values: Mapping[str, Any],
) -> None:
    """Make one layer exactly ``values`` (``harness_prompt``, ``model``, ``effort``, ``env``, ``run_budget_usd``)."""
    await conn.execute(
        "INSERT INTO agent_setting_layers (scope, role, project_id, harness_prompt, model, effort, env, run_budget_usd) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8) "
        "ON CONFLICT (scope, (COALESCE(role, '')), (COALESCE(project_id, ''))) DO UPDATE SET "
        "harness_prompt = EXCLUDED.harness_prompt, model = EXCLUDED.model, effort = EXCLUDED.effort, "
        "env = EXCLUDED.env, run_budget_usd = EXCLUDED.run_budget_usd, updated_at = now()",
        scope, role, project_id, values["harness_prompt"], values["model"], values["effort"],
        json.dumps(list(values["env"]), ensure_ascii=False), values["run_budget_usd"],
    )


async def write_role(conn: asyncpg.Connection, agent_id: str, values: Mapping[str, Any], *, exists: bool) -> None:
    """Set a role's prompt (None: its recommended one), timeout, concurrency and enabled flag.

    A role without a row gets one; serve's seeding then fills in its name and
    recommended prompt and keeps an edited one.
    """
    prompt = values["prompt"]
    if not exists:
        await conn.execute(
            "INSERT INTO agent_configs (agent_id, role, system_prompt, timeout_seconds, max_concurrency, enabled) "
            "VALUES ($1, $1, $2, $3, $4, $5)",
            agent_id, prompt or "", values["timeout_seconds"], values["max_concurrency"], values["enabled"],
        )
        return
    await conn.execute(
        "UPDATE agent_configs SET system_prompt = COALESCE($2, default_prompt), timeout_seconds = $3, "
        "max_concurrency = $4, enabled = $5, updated_at = now() WHERE agent_id = $1",
        agent_id, prompt, values["timeout_seconds"], values["max_concurrency"], values["enabled"],
    )


async def write_channel(
    conn: asyncpg.Connection, channel_id: str, channel_type: str, values: Mapping[str, Any], *, exists: bool,
) -> None:
    config = json.dumps(dict(values["config"]), ensure_ascii=False)
    if exists:
        await conn.execute(
            "UPDATE channel_configs SET display_name = $2, enabled = $3, config = $4::jsonb, updated_at = now() "
            "WHERE channel_id = $1", channel_id, values["display_name"], values["enabled"], config,
        )
    else:
        await conn.execute(
            "INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)", channel_id, channel_type, values["display_name"],
            values["enabled"], config,
        )


async def write_document(
    conn: asyncpg.Connection, document_id: str | None, project_id: str, values: Mapping[str, Any],
) -> None:
    """Add a knowledge document (``document_id`` None) or replace one's source and text, keeping its id."""
    fields = [values["title"], values["summary"], values["source_kind"], values["filename"], values.get("content"),
              values.get("path"), values["size_bytes"], values["sha256"]]
    if document_id is None:
        await conn.execute(
            # The documents of one import keep the file's order (oldest first), which created_at gives.
            "INSERT INTO project_documents (id, project_id, title, summary, source_kind, filename, content, path, "
            "size_bytes, sha256, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, clock_timestamp())",
            f"doc-{uuid.uuid4().hex[:12]}", project_id, *fields,
        )
    else:
        await conn.execute(
            "UPDATE project_documents SET title = $3, summary = $4, source_kind = $5, filename = $6, content = $7, "
            "path = $8, size_bytes = $9, sha256 = $10, updated_at = now() WHERE id = $1 AND project_id = $2",
            document_id, project_id, *fields,
        )
