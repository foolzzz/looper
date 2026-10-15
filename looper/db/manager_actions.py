"""The audit trail of what the Manager did through its platform tools (``agents.ops``), one row per call.

Argument values under a secret-looking key (``SECRET_KEY``) are stored masked; a read's result is not stored,
only that it ran.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import asyncpg

from .connection import get_pool

SECRET_KEY = re.compile(r"key|token|secret|password", re.IGNORECASE)
MASK = "***"


def masked(value: Any) -> Any:
    """``value`` with every value under a secret-looking key replaced by ``MASK``, at any depth."""
    if isinstance(value, Mapping):
        return {k: MASK if SECRET_KEY.search(str(k)) and v is not None else masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(item) for item in value]
    return value


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(masked(value), ensure_ascii=False, default=str)


async def record(
    *, channel: str | None, chat: str | None, message_id: str | None, tool: str, arguments: Mapping[str, Any],
    risk: str, status: str, error_code: str | None = None, result: Any = None, confirmation_id: str | None = None,
    task_id: str | None = None, pool: asyncpg.Pool | None = None,
) -> int:
    """Add one call to the trail; its id."""
    pool = pool or get_pool()
    return await pool.fetchval(
        "INSERT INTO manager_actions (channel, chat, message_id, tool, arguments, risk, status, error_code, result, "
        "confirmation_id, task_id) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9::jsonb, $10, $11) RETURNING id",
        channel or "", chat or "", message_id or "", tool, _json(arguments) or "{}", risk, status, error_code,
        _json(result), confirmation_id, task_id,
    )


async def for_task(task_id: str, *, limit: int = 20, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The Manager's calls that acted on ``task_id`` (asked or done; not its errors), newest first."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT created_at, channel, tool, arguments, risk, status, confirmation_id FROM manager_actions "
        "WHERE task_id = $1 AND status <> 'error' ORDER BY id DESC LIMIT $2", task_id, limit,
    )
    return [{**dict(row), "arguments": json.loads(row["arguments"])} for row in rows]
