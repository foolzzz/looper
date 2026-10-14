"""The ``agent_setting_layers`` table: one row of agent settings per layer and place; and a task's agents.

A layer is ``system``, ``role`` (with a role), ``project`` (with a project)
or ``role_project`` (with both). A NULL field is not set at its layer;
``env`` is a JSON list of ``{"key", "value"}`` (a null value unsets a key);
``agent_id`` is the agent its roles run. A task may choose the agent of some
of its roles (``task_agent_overrides``). Code owns the fields' types, validation and
resolution (``loopx.agents.agent_settings``); this module only stores rows.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import asyncpg

from .agent_registry import AGENT_JOIN, AGENT_JSON
from .connection import get_pool

FIELDS = ("harness_prompt", "agent_id", "effort", "env", "run_budget_usd")


def _row(row: asyncpg.Record) -> dict[str, Any]:
    data = dict(row)
    for key in ("env", "agent"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    return data


async def list_layers(
    project_id: str | None = None, *, task_id: str | None = None, pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """Every row outside projects (system and role layers), and ``project_id``'s rows when given, each with its
    ``agent`` (``agent_registry.AGENT_JSON``, or None); with ``task_id``, also a ``task`` row per role the task
    chose an agent for."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT l.scope, l.role, l.project_id, l.harness_prompt, l.agent_id, l.effort, l.env, l.run_budget_usd, "
        f"(SELECT {AGENT_JSON} FROM {AGENT_JOIN} WHERE a.id = l.agent_id) AS agent "
        "FROM agent_setting_layers l WHERE l.project_id IS NULL OR l.project_id = $1",
        project_id,
    )
    layers = [_row(r) for r in rows]
    if task_id:
        overrides = await pool.fetch(
            f"SELECT t.role, t.agent_id, (SELECT {AGENT_JSON} FROM {AGENT_JOIN} WHERE a.id = t.agent_id) AS agent "
            "FROM task_agent_overrides t WHERE t.task_id = $1", task_id,
        )
        layers += [{"scope": "task", "task_id": task_id, **_row(r)} for r in overrides]
    return layers


async def put_layer(
    scope: str,
    role: str | None,
    project_id: str | None,
    values: Mapping[str, Any],
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Set the fields in ``values`` on one layer (``None`` unsets a field; ``env`` replaces the list).

    A row left with nothing set is removed. Raises ``asyncpg.ForeignKeyViolationError`` for an unknown project
    or agent.
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
            "AND project_id IS NOT DISTINCT FROM $3 AND harness_prompt IS NULL AND agent_id IS NULL "
            "AND effort IS NULL AND env = '[]'::jsonb AND run_budget_usd IS NULL",
            scope, role, project_id,
        )


async def set_agent_overrides(conn: asyncpg.Connection, task_id: str, agents: Mapping[str, str]) -> None:
    """Give task ``task_id`` the agent of each role in ``agents`` (role -> agent id)."""
    await conn.executemany(
        "INSERT INTO task_agent_overrides (task_id, role, agent_id) VALUES ($1, $2, $3)",
        [(task_id, role, agent_id) for role, agent_id in agents.items()],
    )


async def agent_overrides(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, dict[str, Any]]:
    """The agents task ``task_id`` chose, by role (``AGENT_JSON``)."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT t.role, (SELECT {AGENT_JSON} FROM {AGENT_JOIN} WHERE a.id = t.agent_id) AS agent "
        "FROM task_agent_overrides t WHERE t.task_id = $1 ORDER BY t.role", task_id,
    )
    return {r["role"]: _row(r)["agent"] for r in rows}


async def selected_roles(agent_id: str, *, pool: asyncpg.Pool | None = None) -> list[tuple[str, str | None]]:
    """Where agent ``agent_id`` is selected: (scope, role) of each layer, ('task', role) of each task's choice."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT scope, role FROM agent_setting_layers WHERE agent_id = $1 "
        "UNION SELECT 'task', role FROM task_agent_overrides WHERE agent_id = $1", agent_id,
    )
    return [(r["scope"], r["role"]) for r in rows]
