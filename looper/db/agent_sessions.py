"""The Claude session each resident agent resumes after a restart (``agent_sessions``).

One row per agent: ``manager``, or ``planner-<task_id>``. The session's
running totals are stored with its id, so a process that resumes the session
counts only its own spending (``run_usage.Usage``), and so are the runtime
type and the provider it belongs to and the agent that ran it last
(``runtimes.can_resume``). ``clean_start`` marks a session the user dropped
(the Manager's ``/new``): the next one starts without a resume brief.
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
    """``{"session_id", "session_totals", "runtime_type", "provider_id", "session_agent_id", "clean_start"}`` saved
    for ``agent_id``; None when nothing is saved."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT session_id, session_totals, runtime_type, provider_id, session_agent_id, clean_start "
        "FROM agent_sessions WHERE agent_id = $1", agent_id,
    )
    if row is None:
        return None
    totals = row["session_totals"]
    return {"session_id": row["session_id"], "session_totals": json.loads(totals) if isinstance(totals, str) else {},
            "runtime_type": row["runtime_type"], "provider_id": row["provider_id"],
            "session_agent_id": row["session_agent_id"], "clean_start": row["clean_start"]}


async def save_session(
    agent_id: str,
    session_id: str | None,
    session_totals: Mapping[str, Any],
    *,
    runtime_type: str | None = None,
    provider_id: str | None = None,
    session_agent_id: str | None = None,
    clean_start: bool = False,
    pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> None:
    """Replace ``agent_id``'s session, with the runtime type and the provider it belongs to and the agent that ran
    it; a None ``session_id`` makes its next turn start a new one, without a resume brief when ``clean_start``."""
    pool = pool or get_pool()
    await pool.execute(
        """
        INSERT INTO agent_sessions (agent_id, session_id, session_totals, runtime_type, provider_id, session_agent_id,
                                    clean_start, updated_at)
        VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, now())
        ON CONFLICT (agent_id) DO UPDATE
        SET session_id = EXCLUDED.session_id, session_totals = EXCLUDED.session_totals,
            runtime_type = EXCLUDED.runtime_type, provider_id = EXCLUDED.provider_id,
            session_agent_id = EXCLUDED.session_agent_id, clean_start = EXCLUDED.clean_start, updated_at = now()
        """,
        agent_id, session_id, json.dumps(dict(session_totals)), runtime_type, provider_id, session_agent_id,
        clean_start,
    )


async def saved_sessions(*, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[tuple[str, str | None]]:
    """``(agent_id, runtime_type)`` of every agent with a saved session to resume."""
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT agent_id, runtime_type FROM agent_sessions WHERE session_id IS NOT NULL")
    return [(row["agent_id"], row["runtime_type"]) for row in rows]
