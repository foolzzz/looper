"""Agent configuration CRUD — DB-driven config for the agent-tiers system."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import asyncpg

from .connection import get_pool

_UPDATABLE_FIELDS = (
    "display_name",
    "role",
    "runtime",
    "llm_profile_id",
    "enabled",
    "system_prompt",
    "harness_prompt",
    "model",
    "max_turns",
    "timeout_seconds",
    "max_concurrency",
    "extra_args",
    "metadata",
)


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    for key in ("created_at", "updated_at"):
        if key in d and d[key] is not None:
            d[key] = d[key].isoformat()
    if "extra_args" in d and d["extra_args"] is not None:
        import json
        d["extra_args"] = json.loads(d["extra_args"])
    if "metadata" in d and d["metadata"] is not None:
        import json
        d["metadata"] = json.loads(d["metadata"])
    return d


async def get_config(agent_id: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM agent_configs WHERE agent_id = $1", agent_id,
    )
    if row is None:
        return None
    return _row_to_dict(row)


async def set_config(
    agent_id: str,
    fields: dict[str, Any],
    *,
    pool: asyncpg.Pool | None = None,
) -> dict[str, Any] | None:
    """Partial-patch update: only provided fields are changed."""
    pool = pool or get_pool()

    updates = {}
    for key in _UPDATABLE_FIELDS:
        if key in fields:
            updates[key] = fields[key]

    if not updates:
        return await get_config(agent_id, pool=pool)

    import json
    for key in ("extra_args", "metadata"):
        if key in updates and updates[key] is not None:
            updates[key] = json.dumps(updates[key])

    set_clauses = []
    values = []
    for i, (key, value) in enumerate(updates.items(), start=2):
        set_clauses.append(f"{key} = ${i}")
        values.append(value)

    set_clauses.append(f"updated_at = ${len(values) + 2}")
    values.append(datetime.now(timezone.utc))

    sql = f"UPDATE agent_configs SET {', '.join(set_clauses)} WHERE agent_id = $1"
    await pool.execute(sql, agent_id, *values)
    return await get_config(agent_id, pool=pool)


async def list_configs(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT * FROM agent_configs ORDER BY agent_id")
    return [_row_to_dict(r) for r in rows]


async def replace_default_prompt(
    agent_id: str,
    *,
    old: str,
    new: str,
    pool: asyncpg.Pool | None = None,
) -> bool:
    """Give a row the ``new`` default system prompt if it still holds the ``old`` one.

    A prompt someone edited is kept. Returns whether the row changed.
    """
    pool = pool or get_pool()
    result = await pool.execute(
        "UPDATE agent_configs SET system_prompt = $3, updated_at = now() "
        "WHERE agent_id = $1 AND system_prompt = $2",
        agent_id, old, new,
    )
    return result == "UPDATE 1"


async def reset_prompt(
    agent_id: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> dict[str, Any] | None:
    """Reset an agent's system prompt to the system-recommended ``default_prompt``.

    Returns the updated config, or ``None`` when there is no such agent (or it
    has no recommended prompt to reset to).
    """
    pool = pool or get_pool()
    result = await pool.execute(
        "UPDATE agent_configs SET system_prompt = default_prompt, updated_at = now() "
        "WHERE agent_id = $1 AND default_prompt <> ''",
        agent_id,
    )
    if result != "UPDATE 1":
        return None
    return await get_config(agent_id, pool=pool)


async def seed_defaults(
    defaults: list[dict[str, Any]],
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Insert default agent configs that don't exist yet."""
    pool = pool or get_pool()
    for cfg in defaults:
        agent_id = cfg["agent_id"]
        display_name = cfg.get("display_name", "")
        role = cfg.get("role", "")
        runtime = cfg.get("runtime", "claude-code")
        system_prompt = cfg.get("system_prompt", "")
        model = cfg.get("model")
        max_turns = cfg.get("max_turns", 0)
        timeout_seconds = cfg.get("timeout_seconds", 1200)
        max_concurrency = cfg.get("max_concurrency", 1)

        await pool.execute(
            """
            INSERT INTO agent_configs
                (agent_id, display_name, role, runtime, system_prompt, default_prompt, model,
                 max_turns, timeout_seconds, max_concurrency)
            VALUES ($1, $2, $3, $4, $5, $5, $6, $7, $8, $9)
            ON CONFLICT (agent_id) DO UPDATE SET
                display_name = CASE WHEN agent_configs.display_name = '' THEN EXCLUDED.display_name ELSE agent_configs.display_name END,
                -- An unedited row (its prompt still equals the recommended one) follows
                -- the new recommendation; an edited row keeps the user's prompt. An empty
                -- prompt is always (re)seeded.
                system_prompt = CASE
                    WHEN agent_configs.system_prompt = '' THEN EXCLUDED.system_prompt
                    WHEN agent_configs.system_prompt = agent_configs.default_prompt THEN EXCLUDED.system_prompt
                    ELSE agent_configs.system_prompt END,
                default_prompt = EXCLUDED.default_prompt,
                runtime = CASE WHEN agent_configs.runtime = 'claude-code' AND EXCLUDED.runtime != 'claude-code' THEN EXCLUDED.runtime ELSE agent_configs.runtime END
            """,
            agent_id,
            display_name,
            role,
            runtime,
            system_prompt,
            model,
            max_turns,
            timeout_seconds,
            max_concurrency,
        )
