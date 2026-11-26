"""A task's durable network hold; completed rows retain recovery counts and durations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from .connection import get_pool
from .tasks import ACTIVE_STATUSES


class State(StrEnum):
    PAUSED = 'paused'
    WAITING_USER = 'waiting_user'


DEADLINE_REASON = 'Model API network pause reached its deadline; check connectivity and stop/restart the task'


def _row(row) -> dict[str, Any] | None:
    if row is None:
        return None
    found = dict(row)
    if isinstance(found.get('continuation'), str):
        found['continuation'] = json.loads(found['continuation'])
    return found


def view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Public status only: continuation may carry a private endpoint or saved turn."""
    return {key: row[key] for key in ('id', 'state', 'reason', 'started_at', 'deadline')}


async def get(task_id: str, *, pool=None) -> dict[str, Any] | None:
    return _row(await (pool or get_pool()).fetchrow(
        'SELECT * FROM task_network_pauses WHERE task_id=$1 AND ended_at IS NULL', task_id))


async def put(task_id: str, value: Mapping[str, Any], *, pool=None) -> dict[str, Any]:
    """Open a pause, or update its continuation without extending its original deadline."""
    state = State(value.get('state', State.PAUSED))
    started = value.get('started_at') or datetime.now(timezone.utc)
    row = await (pool or get_pool()).fetchrow(
        "INSERT INTO task_network_pauses(task_id,state,reason,started_at,deadline,continuation) "
        "VALUES ($1,$2,$3,$4,$5,$6::jsonb) "
        "ON CONFLICT(task_id) WHERE ended_at IS NULL DO UPDATE SET "
        "reason=EXCLUDED.reason, continuation=EXCLUDED.continuation RETURNING *",
        task_id, state.value, value['reason'], started, value['deadline'],
        json.dumps(value.get('continuation') or {}))
    return _row(row)


async def expire(task_id: str, *, pause_id: int | None = None, pool=None) -> dict[str, Any] | None:
    """The same active row becomes a single human wait; no duplicate reminder is created."""
    return _row(await (pool or get_pool()).fetchrow(
        "UPDATE task_network_pauses SET state='waiting_user',reason=$3 "
        "WHERE task_id=$1 AND ended_at IS NULL AND ($2::bigint IS NULL OR id=$2) RETURNING *",
        task_id, pause_id, DEADLINE_REASON))


async def clear(task_id: str, *, reason: str = 'resumed', pause_id: int | None = None, pool=None) -> dict[str, Any] | None:
    return _row(await (pool or get_pool()).fetchrow(
        'UPDATE task_network_pauses SET ended_at=now(),end_reason=$2 '
        'WHERE task_id=$1 AND ended_at IS NULL AND ($3::bigint IS NULL OR id=$3) RETURNING *', task_id, reason, pause_id))


async def needs_attention(*, pool=None) -> list[dict[str, Any]]:
    rows = await (pool or get_pool()).fetch(
        "SELECT n.* FROM task_network_pauses n JOIN tasks t ON t.id=n.task_id "
        "WHERE n.ended_at IS NULL AND n.state='waiting_user' AND t.status=ANY($1::text[]) "
        "ORDER BY n.started_at,n.id", list(ACTIVE_STATUSES))
    return [_row(row) for row in rows]


async def stats(*, pool=None) -> dict[str, Any]:
    row = await (pool or get_pool()).fetchrow(
        "SELECT count(*) AS pause_count, count(*) FILTER (WHERE end_reason='resumed') AS resume_count, "
        "count(*) FILTER (WHERE state='waiting_user') AS expired_count, "
        "count(*) FILTER (WHERE ended_at IS NULL AND state='paused') AS paused, "
        "count(*) FILTER (WHERE ended_at IS NULL AND state='waiting_user') AS waiting_user, "
        "COALESCE(sum(GREATEST(0,extract(epoch FROM (LEAST(COALESCE(ended_at,now()),deadline)-started_at)))),0)::float8 "
        "AS duration_seconds FROM task_network_pauses")
    return dict(row)
