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

SECRET_KEY = re.compile(r"key|token|secret|password|credential", re.IGNORECASE)
MASK = "***"


def masked(value: Any) -> Any:
    """Mask secret-looking fields and credential values in ``{key, value}`` entries, at any depth."""
    if isinstance(value, Mapping):
        credential_entry = SECRET_KEY.search(str(value.get("key", "")))
        return {k: MASK if v is not None and (SECRET_KEY.search(str(k)) or k == "value" and credential_entry)
                else masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(item) for item in value]
    return value


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(masked(value), ensure_ascii=False, default=str)


async def record(
    *, channel: str | None, chat: str | None, message_id: str | None, tool: str, arguments: Mapping[str, Any],
    risk: str, status: str, error_code: str | None = None, result: Any = None, confirmation_id: str | None = None,
    task_id: str | None = None, manager_id: str = "manager", client_id: str | None = None,
    pool: asyncpg.Pool | None = None,
) -> int:
    """Add one call to the trail (``client_id``: the API client it came through); its id."""
    pool = pool or get_pool()
    return await pool.fetchval(
        "INSERT INTO manager_actions (channel, chat, message_id, tool, arguments, risk, status, error_code, result, "
        "confirmation_id, task_id, manager_id, client_id) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9::jsonb, $10, $11, $12, $13) "
        "RETURNING id",
        channel or "", chat or "", message_id or "", tool, _json(arguments) or "{}", risk, status, error_code,
        _json(result), confirmation_id, task_id, manager_id, client_id,
    )


async def for_task(task_id: str, *, limit: int = 20, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The Manager's calls that acted on ``task_id`` (asked or done; not its errors), newest first, each with the
    API client it came through (``client_id`` and ``client_name``, None for none)."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT a.created_at, a.manager_id, a.channel, a.tool, a.arguments, a.risk, a.status, a.confirmation_id, "
        "a.client_id, c.name AS client_name FROM manager_actions a LEFT JOIN api_clients c ON c.id = a.client_id "
        "WHERE a.task_id = $1 AND a.status <> 'error' ORDER BY a.id DESC LIMIT $2", task_id, limit,
    )
    return [{**dict(row), "arguments": json.loads(row["arguments"])} for row in rows]
