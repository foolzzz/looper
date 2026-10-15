"""The ``agent_setting_layers`` table: one row of agent settings per layer and place; and a task's profiles.

A layer is ``system``, ``role`` (with a role), ``project`` (with a project)
or ``role_project`` (with both). A NULL field is not set at its layer;
``env`` is a JSON list of ``{"key", "value"}`` (a null value unsets a key);
``profile_id`` is the Agent Profile its roles run (its model and effort with it); ``max_concurrency`` (a task
role's role or role-in-a-project layer only) how many of the role run at once in one project. A task may choose
the profile of some of its roles (``task_profile_overrides``). Code owns the fields' types, validation and
resolution (``looper.agents.agent_settings``); this module only stores rows.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import asyncpg

from .profile_registry import PROFILE_JOIN, PROFILE_JSON
from .connection import get_pool

FIELDS = ("harness_prompt", "profile_id", "env", "run_budget_usd", "max_concurrency")


def _row(row: asyncpg.Record) -> dict[str, Any]:
    data = dict(row)
    for key in ("env", "profile"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    return data


async def list_layers(
    project_id: str | None = None, *, task_id: str | None = None, pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """Every row outside projects (system and role layers), and ``project_id``'s rows when given, each with its
    ``profile`` (``profile_registry.PROFILE_JSON``, or None); with ``task_id``, also a ``task`` row per role the task
    chose a profile for."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT l.scope, l.role, l.project_id, l.harness_prompt, l.profile_id, l.env, l.run_budget_usd, "
        f"l.max_concurrency, "
        f"(SELECT {PROFILE_JSON} FROM {PROFILE_JOIN} WHERE a.id = l.profile_id) AS profile "
        "FROM agent_setting_layers l WHERE l.project_id IS NULL OR l.project_id = $1",
        project_id,
    )
    layers = [_row(r) for r in rows]
    if task_id:
        overrides = await pool.fetch(
            f"SELECT t.role, t.profile_id, (SELECT {PROFILE_JSON} FROM {PROFILE_JOIN} WHERE a.id = t.profile_id) "
            "AS profile FROM task_profile_overrides t WHERE t.task_id = $1", task_id,
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
    or Agent Profile.
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
            "AND project_id IS NOT DISTINCT FROM $3 AND harness_prompt IS NULL AND profile_id IS NULL "
            "AND env = '[]'::jsonb AND run_budget_usd IS NULL AND max_concurrency IS NULL",
            scope, role, project_id,
        )


async def set_profile_overrides(conn: asyncpg.Connection, task_id: str, profiles: Mapping[str, str]) -> None:
    """Give task ``task_id`` the Agent Profile of each role in ``profiles`` (role -> profile id)."""
    await conn.executemany(
        "INSERT INTO task_profile_overrides (task_id, role, profile_id) VALUES ($1, $2, $3)",
        [(task_id, role, profile_id) for role, profile_id in profiles.items()],
    )


async def set_task_profiles(task_id: str, profiles: Mapping[str, str | None], *,
                            pool: asyncpg.Pool | None = None) -> None:
    """Change task ``task_id``'s choice of Agent Profile for each role in ``profiles`` (role -> profile id; None
    drops the task's choice of that role), in one transaction. Raises ``asyncpg.ForeignKeyViolationError`` for an
    unknown task or profile."""
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        for role, profile_id in profiles.items():
            if profile_id is None:
                await conn.execute("DELETE FROM task_profile_overrides WHERE task_id = $1 AND role = $2",
                                   task_id, role)
            else:
                await conn.execute(
                    "INSERT INTO task_profile_overrides (task_id, role, profile_id) VALUES ($1, $2, $3) "
                    "ON CONFLICT (task_id, role) DO UPDATE SET profile_id = EXCLUDED.profile_id",
                    task_id, role, profile_id)


async def profile_overrides(task_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, dict[str, Any]]:
    """The Agent Profiles task ``task_id`` chose, by role (``PROFILE_JSON``)."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT t.role, (SELECT {PROFILE_JSON} FROM {PROFILE_JOIN} WHERE a.id = t.profile_id) AS profile "
        "FROM task_profile_overrides t WHERE t.task_id = $1 ORDER BY t.role", task_id,
    )
    return {r["role"]: _row(r)["profile"] for r in rows}


async def selected_roles(profile_id: str, *, pool: asyncpg.Pool | None = None) -> list[tuple[str, str | None]]:
    """Where Agent Profile ``profile_id`` is selected: (scope, role) of each layer, ('task', role) of each task's
    choice and ('schedule', role) of each schedule's."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT scope, role FROM agent_setting_layers WHERE profile_id = $1 "
        "UNION SELECT 'task', role FROM task_profile_overrides WHERE profile_id = $1 "
        "UNION SELECT 'schedule', role FROM schedule_profile_overrides WHERE profile_id = $1", profile_id,
    )
    return [(r["scope"], r["role"]) for r in rows]
