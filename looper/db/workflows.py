"""Operator workflows; task snapshots are independent of later replacement or deletion."""
from __future__ import annotations

import json
from typing import Any

import asyncpg

from .connection import _connection, get_pool


async def profile_references(profile_id: str | None = None, *, pool: Any = None) -> list[dict[str, Any]]:
    """Profile selections in presets, including custom roles, read from their authoritative definitions."""
    rows = await (pool or get_pool()).fetch(
        "SELECT DISTINCT w.key, w.name, p.profile_id FROM workflows w CROSS JOIN LATERAL ("
        "SELECT value AS profile_id FROM jsonb_each_text(COALESCE(w.definition->'profiles', '{}'::jsonb)) "
        "UNION SELECT r->>'profile_id' "
        "FROM jsonb_array_elements(COALESCE(w.definition->'roles', '[]'::jsonb)) r) p "
        "WHERE p.profile_id IS NOT NULL AND ($1::text IS NULL OR p.profile_id = $1) ORDER BY w.key",
        profile_id)
    return [dict(row) for row in rows]


def role_profile_allowed_sql(task: str, role: str) -> str:
    """A role's exact profile grant in its task's immutable workflow, independent of who edited the version."""
    return (f"EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE({task}.workflow->'roles', '[]'::jsonb)) wr "
            f"WHERE wr->>'key' = {role}.role_key AND wr->>'kind' = {role}.kind "
            f"AND wr->>'profile_id' = {role}.profile_id)")


async def list_workflows(*, pool: Any = None) -> list[dict[str, Any]]:
    return [dict(row) for row in await (pool or get_pool()).fetch("SELECT * FROM workflows ORDER BY key")]


async def get(key: str, *, pool: Any = None) -> dict[str, Any] | None:
    row = await (pool or get_pool()).fetchrow("SELECT * FROM workflows WHERE key = $1", key)
    return dict(row) if row else None


async def put(workflow: Any, *, pool: Any = None) -> dict[str, Any]:
    raw = workflow.snapshot()
    definition = {key: value for key, value in raw.items() if key not in ("key", "name", "description")}
    profiles = sorted(set(workflow.profiles.values()) | {r.profile_id for r in workflow.roles if r.profile_id})
    async with _connection(pool or get_pool()) as conn, conn.transaction():
        # Like an FK: deletion locks the same rows before rechecking preset references.
        present = await conn.fetch("SELECT id FROM agent_profiles WHERE id = ANY($1::text[]) "
                                   "ORDER BY id FOR KEY SHARE", profiles)
        if len(present) != len(profiles):
            raise asyncpg.ForeignKeyViolationError("a workflow's Agent Profile was removed meanwhile")
        row = await conn.fetchrow(
            "INSERT INTO workflows (key, name, description, definition) VALUES ($1, $2, $3, $4::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description, "
            "definition = EXCLUDED.definition, updated_at = now() RETURNING *",
            workflow.key, workflow.name, workflow.description, json.dumps(definition))
    return dict(row)


async def delete(key: str, *, pool: Any = None) -> bool:
    # The project's FK clears only its default; tasks keep their JSON snapshot.
    return await (pool or get_pool()).execute("DELETE FROM workflows WHERE key = $1", key) == "DELETE 1"
