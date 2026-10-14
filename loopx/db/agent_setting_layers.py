"""The ``agent_setting_layers`` table: one row of agent settings per layer and place.

A layer is ``system``, ``role`` (with a role), ``project`` (with a project)
or ``role_project`` (with both). A NULL field is not set at its layer;
``env`` is a JSON list of ``{"key", "value"}`` (a null value unsets a key).
Code owns the fields' types, validation and resolution
(``loopx.agents.agent_settings``); this module only stores rows.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import asyncpg

from .connection import get_pool

FIELDS = ("harness_prompt", "model", "effort", "env", "run_budget_usd")


def _row(row: asyncpg.Record) -> dict[str, Any]:
    data = dict(row)
    if isinstance(data.get("env"), str):
        data["env"] = json.loads(data["env"])
    return data


async def list_layers(project_id: str | None = None, *, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """Every row outside projects (system and role layers), and ``project_id``'s rows when given."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT scope, role, project_id, harness_prompt, model, effort, env, run_budget_usd "
        "FROM agent_setting_layers WHERE project_id IS NULL OR project_id = $1",
        project_id,
    )
    return [_row(r) for r in rows]


async def put_layer(
    scope: str,
    role: str | None,
    project_id: str | None,
    values: Mapping[str, Any],
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Set the fields in ``values`` on one layer (``None`` unsets a field; ``env`` replaces the list).

    A row left with nothing set is removed. Raises ``asyncpg.ForeignKeyViolationError`` for an unknown project.
    """
    unknown = sorted(set(values) - set(FIELDS))
    if unknown:
        raise ValueError(f"not an agent setting: {unknown[0]}")
    if not values:
        return
    pool = pool or get_pool()
    stored = {k: (json.dumps(list(v or []), ensure_ascii=False) if k == "env" else v) for k, v in values.items()}
    columns = list(stored)
    params = [scope, role, project_id, *stored.values()]
    placeholders = [f"${i}" + ("::jsonb" if c == "env" else "") for i, c in enumerate(columns, start=4)]
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            f"INSERT INTO agent_setting_layers (scope, role, project_id, {', '.join(columns)}) "
            f"VALUES ($1, $2, $3, {', '.join(placeholders)}) "
            "ON CONFLICT (scope, (COALESCE(role, '')), (COALESCE(project_id, ''))) "
            f"DO UPDATE SET {assignments}, updated_at = now()",
            *params,
        )
        await conn.execute(
            "DELETE FROM agent_setting_layers WHERE scope = $1 AND role IS NOT DISTINCT FROM $2 "
            "AND project_id IS NOT DISTINCT FROM $3 AND harness_prompt IS NULL AND model IS NULL "
            "AND effort IS NULL AND env = '[]'::jsonb AND run_budget_usd IS NULL",
            scope, role, project_id,
        )
