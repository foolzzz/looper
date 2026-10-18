"""The ``project_role_templates`` table: the role templates of a project (``looper.agents.role_api``).

A template is checked like a task role before it is stored; its tasks' Planners copy it as a task role
(``ops.planner``, ``looper_task_role_set``'s ``template``). Deleting a project deletes its templates.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import asyncpg

from .connection import get_pool

# A template with its profile's name, and whether its project allows that profile now.
_TEMPLATE = (
    "SELECT t.project_id, t.template_key, t.kind, t.description, t.role_prompt, t.harness, t.profile_id, "
    "t.updated_at, a.name AS profile_name, (l.profile_id IS NOT NULL) AS profile_allowed "
    "FROM project_role_templates t LEFT JOIN agent_profiles a ON a.id = t.profile_id "
    "LEFT JOIN project_allowed_profiles l ON l.project_id = t.project_id AND l.profile_id = t.profile_id "
)
_FIELDS = ("kind", "description", "role_prompt", "harness", "profile_id")


async def list_templates(project_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
                         ) -> list[dict[str, Any]]:
    """Project ``project_id``'s templates, by key."""
    pool = pool or get_pool()
    rows = await pool.fetch(f"{_TEMPLATE} WHERE t.project_id = $1 ORDER BY t.template_key", project_id)
    return [dict(row) for row in rows]


async def get_template(project_id: str, key: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow(f"{_TEMPLATE} WHERE t.project_id = $1 AND t.template_key = $2", project_id, key)
    return dict(row) if row else None


async def put_template(project_id: str, key: str, values: Mapping[str, Any], *,
                       pool: asyncpg.Pool | asyncpg.Connection | None = None) -> None:
    """Create template ``key`` of project ``project_id``, or replace it, with ``values`` (``kind``,
    ``description``, ``role_prompt``, ``harness``, ``profile_id``). Raises ``asyncpg.ForeignKeyViolationError`` for
    an unknown project or profile."""
    pool = pool or get_pool()
    await pool.execute(
        "INSERT INTO project_role_templates (project_id, template_key, kind, description, role_prompt, harness, "
        "profile_id) VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (project_id, template_key) DO UPDATE SET "
        "kind = EXCLUDED.kind, description = EXCLUDED.description, role_prompt = EXCLUDED.role_prompt, "
        "harness = EXCLUDED.harness, profile_id = EXCLUDED.profile_id, updated_at = now()",
        project_id, key, *(values.get(name) if name != "description" else values.get(name) or "" for name in _FIELDS))


async def delete_template(project_id: str, key: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Delete template ``key`` of project ``project_id``; whether there was one."""
    pool = pool or get_pool()
    return await pool.execute("DELETE FROM project_role_templates WHERE project_id = $1 AND template_key = $2",
                              project_id, key) == "DELETE 1"
