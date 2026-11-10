"""Operator workflows; task snapshots are independent of later replacement or deletion."""
from __future__ import annotations

import json
from typing import Any

from .connection import get_pool


async def list_workflows(*, pool: Any = None) -> list[dict[str, Any]]:
    return [dict(row) for row in await (pool or get_pool()).fetch("SELECT * FROM workflows ORDER BY key")]


async def get(key: str, *, pool: Any = None) -> dict[str, Any] | None:
    row = await (pool or get_pool()).fetchrow("SELECT * FROM workflows WHERE key = $1", key)
    return dict(row) if row else None


async def put(workflow: Any, *, pool: Any = None) -> dict[str, Any]:
    raw = workflow.snapshot()
    definition = {key: value for key, value in raw.items() if key not in ("key", "name", "description")}
    row = await (pool or get_pool()).fetchrow(
        "INSERT INTO workflows (key, name, description, definition) VALUES ($1, $2, $3, $4::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description, "
        "definition = EXCLUDED.definition, updated_at = now() RETURNING *",
        workflow.key, workflow.name, workflow.description, json.dumps(definition))
    return dict(row)


async def delete(key: str, *, pool: Any = None) -> bool:
    # The project's FK clears only its default; tasks keep their JSON snapshot.
    return await (pool or get_pool()).execute("DELETE FROM workflows WHERE key = $1", key) == "DELETE 1"
