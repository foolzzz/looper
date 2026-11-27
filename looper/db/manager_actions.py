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
    category: str | None = None, receipt: str | None = None, sender_id: str | None = None,
    sender_kind: str | None = None, initiator_sender_id: str | None = None, initiator_kind: str | None = None,
    pool: asyncpg.Pool | None = None,
) -> int:
    """Add one call to the trail (``client_id``: the API client it came through; ``category`` and ``receipt``: what
    the call was and the line shown for it, written only while action receipts are on; ``sender_id`` and
    ``sender_kind``: the trusted sender of the turn it ran in and its audience class, ``initiator_*`` the same for
    the turn that asked, on a confirmed call); its id."""
    pool = pool or get_pool()
    values = [channel or "", chat or "", message_id or "", tool, _json(arguments) or "{}", risk, status, error_code,
              _json(result), confirmation_id, task_id, manager_id, client_id]
    columns = ("channel, chat, message_id, tool, arguments, risk, status, error_code, result, confirmation_id, "
               "task_id, manager_id, client_id")
    marks = "$1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9::jsonb, $10, $11, $12, $13"
    if category is not None:  # the columns stay out of the statement otherwise, as before they existed
        columns += ", category, receipt"
        marks += ", $14, $15"
        values += [category, receipt]
    for column, value in (("sender_id", sender_id), ("sender_kind", sender_kind),
                          ("initiator_sender_id", initiator_sender_id), ("initiator_kind", initiator_kind)):
        if value is not None:  # NULL is the column's default: a call nobody classified adds nothing
            values.append(value)
            columns += f", {column}"
            marks += f", ${len(values)}"
    return await pool.fetchval(f"INSERT INTO manager_actions ({columns}) VALUES ({marks}) RETURNING id", *values)


async def amend(action_id, *, status, error_code=None, result=None, pool=None) -> None:
    """Complete a call whose effect needs its already recorded id, in the effect's transaction."""
    await (pool or get_pool()).execute(
        "UPDATE manager_actions SET status=$2, error_code=$3, result=$4::jsonb WHERE id=$1",
        action_id, status, error_code, _json(result))


async def for_task(task_id: str, *, limit: int = 20, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The Manager's calls that acted on ``task_id`` (asked or done; not its errors), newest first, each with the
    API client it came through (``client_id`` and ``client_name``, None for none) and who started it (``sender_id``,
    ``sender_kind``; for a confirmed call also ``initiator_sender_id`` and ``initiator_kind``, None for unknown)."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT a.created_at, a.manager_id, a.channel, a.tool, a.arguments, a.risk, a.status, a.confirmation_id, "
        "a.client_id, c.name AS client_name, a.sender_id, a.sender_kind, a.initiator_sender_id, a.initiator_kind "
        "FROM manager_actions a LEFT JOIN api_clients c ON c.id = a.client_id "
        "WHERE a.task_id = $1 AND a.status <> 'error' ORDER BY a.id DESC LIMIT $2", task_id, limit,
    )
    return [{**dict(row), "arguments": json.loads(row["arguments"])} for row in rows]


async def recent_receipts(*, hours: int = 24, limit: int = 5, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The receipt lines of the last ``hours`` hours, newest first: ``{"receipt", "at"}``."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT receipt, created_at AS at FROM manager_actions WHERE receipt IS NOT NULL "
        "AND created_at >= now() - make_interval(hours => $1) ORDER BY id DESC LIMIT $2", hours, limit)
    return [dict(row) for row in rows]


async def count_by_category(days: int, *, pool: asyncpg.Pool | None = None) -> dict[str, dict[str, int]]:
    """Writes of the last ``days`` days that have a category: ``{category: {status: count}}``."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT category, status, count(*) AS n FROM manager_actions WHERE category IS NOT NULL "
        "AND created_at >= now() - make_interval(days => $1) GROUP BY 1, 2", days)
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        counts.setdefault(row["category"], {})[row["status"]] = row["n"]
    return counts
