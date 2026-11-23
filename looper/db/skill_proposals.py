"""Stored rows of ``skill_proposals`` and ``skill_proposal_events``. ``looper.agents.skill_proposals`` decides
what a proposal is and who may change it.

Every function takes the connection of a transaction the caller owns, so a status change, its event and the
skill write it belongs to commit together.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

_COLUMNS = (
    "id, project_id, action, item_id, base_sha256, name, description, content, content_sha256, why, evidence, "
    "proposer, status, decided_at, decision_note, created_at, expires_at, confirmed_event_id"
)


def _row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if isinstance(data.get("evidence"), str):
        data["evidence"] = json.loads(data["evidence"])
    return data


async def project_mode(conn: Any, project_id: str) -> str | None:
    return await conn.fetchval("SELECT skill_proposals FROM projects WHERE id = $1", project_id)


async def set_project_mode(conn: Any, project_id: str, mode: str | None) -> bool:
    result = await conn.execute(
        "UPDATE projects SET skill_proposals = $2, updated_at = now() WHERE id = $1", project_id, mode,
    )
    return result == "UPDATE 1"


async def insert(
    conn: Any, *, project_id: str, action: str, item_id: str | None, base_sha256: str | None, name: str,
    description: str, content: str, content_sha256: str, why: str, evidence: list[dict[str, str]], proposer: str,
    expires_at: datetime,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        "INSERT INTO skill_proposals (project_id, action, item_id, base_sha256, name, description, content, "
        "content_sha256, why, evidence, proposer, status, expires_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, 'pending', $12) "
        f"RETURNING {_COLUMNS}",
        project_id, action, item_id, base_sha256, name, description, content, content_sha256, why,
        json.dumps(evidence), proposer, expires_at,
    )
    return _row(row) or {}


async def get(conn: Any, proposal_id: int, *, lock: bool = False) -> dict[str, Any] | None:
    return _row(await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM skill_proposals WHERE id = $1" + (" FOR UPDATE" if lock else ""), proposal_id,
    ))


async def list_rows(conn: Any, project_id: str, statuses: list[str], limit: int) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"SELECT {_COLUMNS} FROM skill_proposals WHERE project_id = $1 AND status = ANY($2::text[]) "
        "ORDER BY id DESC LIMIT $3",
        project_id, statuses, limit,
    )
    return [_row(row) or {} for row in rows]


async def pending_named(conn: Any, project_id: str, name: str) -> dict[str, Any] | None:
    """The one pending proposal of a name in a project (a partial unique index allows at most one), locked."""
    return _row(await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM skill_proposals WHERE project_id = $1 AND name = $2 AND status = 'pending' "
        "FOR UPDATE", project_id, name,
    ))


async def add_event(
    conn: Any, proposal_id: int, *, action: str, actor_kind: str, via: str, content_sha256: str | None,
    note: str | None = None,
) -> int:
    return int(await conn.fetchval(
        "INSERT INTO skill_proposal_events (proposal_id, action, actor_kind, via, content_sha256, note) "
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
        proposal_id, action, actor_kind, via, content_sha256, note,
    ))


async def events(conn: Any, proposal_id: int) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT action, actor_kind, via, content_sha256, note, at FROM skill_proposal_events "
        "WHERE proposal_id = $1 ORDER BY id", proposal_id,
    )
    return [dict(row) for row in rows]


async def set_status(
    conn: Any, proposal_id: int, status: str, *, at: datetime | None, note: str | None = None,
) -> None:
    await conn.execute(
        "UPDATE skill_proposals SET status = $2, decided_at = $3, decision_note = $4 WHERE id = $1",
        proposal_id, status, at, note,
    )


async def set_confirmed(
    conn: Any, proposal_id: int, status: str, *, event_id: int, content_sha256: str, at: datetime,
) -> None:
    await conn.execute(
        "UPDATE skill_proposals SET status = $2, decided_at = $3, confirmed_event_id = $4, "
        "confirmed_actor = 'user', confirmed_sha256 = $5 WHERE id = $1",
        proposal_id, status, at, event_id, content_sha256,
    )


async def set_content(
    conn: Any, proposal_id: int, *, name: str, description: str, content: str, content_sha256: str,
) -> None:
    await conn.execute(
        "UPDATE skill_proposals SET name = $2, description = $3, content = $4, content_sha256 = $5 WHERE id = $1",
        proposal_id, name, description, content, content_sha256,
    )


async def restore_pending(conn: Any, proposal_id: int, *, expires_at: datetime) -> None:
    await conn.execute(
        "UPDATE skill_proposals SET status = 'pending', decided_at = NULL, decision_note = NULL, "
        "expires_at = $2 WHERE id = $1", proposal_id, expires_at,
    )


async def expire_before(conn: Any, now: datetime) -> list[dict[str, Any]]:
    """Mark every pending proposal whose expiry is not after ``now`` expired; returns ``id``, ``content_sha256``."""
    rows = await conn.fetch(
        "UPDATE skill_proposals SET status = 'expired', decided_at = $1 "
        "WHERE status = 'pending' AND expires_at <= $1 RETURNING id, content_sha256", now,
    )
    return [dict(row) for row in rows]


async def pending_summary(conn: Any) -> list[dict[str, Any]]:
    """Pending proposals with their project's name, oldest first: what the daily notice names."""
    rows = await conn.fetch(
        "SELECT s.id, s.project_id, p.name AS project_name, s.name, s.action, s.why FROM skill_proposals s "
        "JOIN projects p ON p.id = s.project_id WHERE s.status = 'pending' ORDER BY s.id",
    )
    return [dict(row) for row in rows]
