"""The Claude session each resident agent resumes after a restart (``agent_sessions``).

One row per agent: ``manager``, or ``planner-<task_id>``. The session's
running totals are stored with its id, so a process that resumes the session
counts only its own spending (``run_usage.Usage``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import asyncpg

from .connection import get_pool


async def get_session(
    agent_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> dict[str, Any] | None:
    """``{"session_id", "session_totals"}`` saved for ``agent_id``; None when nothing is saved."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT session_id, session_totals FROM agent_sessions WHERE agent_id = $1", agent_id,
    )
    if row is None:
        return None
    totals = row["session_totals"]
    return {"session_id": row["session_id"], "session_totals": json.loads(totals) if isinstance(totals, str) else {}}


async def save_session(
    agent_id: str,
    session_id: str | None,
    session_totals: Mapping[str, Any],
    *,
    pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> None:
    """Replace ``agent_id``'s session; a None ``session_id`` makes its next turn start a new one."""
    pool = pool or get_pool()
    await pool.execute(
        """
        INSERT INTO agent_sessions (agent_id, session_id, session_totals, updated_at)
        VALUES ($1, $2, $3::jsonb, now())
        ON CONFLICT (agent_id) DO UPDATE
        SET session_id = EXCLUDED.session_id, session_totals = EXCLUDED.session_totals, updated_at = now()
        """,
        agent_id, session_id, json.dumps(dict(session_totals)),
    )
