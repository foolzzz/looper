"""The Claude session each resident agent resumes after a restart (``agent_sessions``).

One row per agent: ``manager``, or ``planner-<task_id>``. The session's
running totals are stored with its id, so a process that resumes the session
counts only its own spending (``run_usage.Usage``), and so are the runtime
type and the provider it belongs to and the Agent Profile that ran it last
(``runtimes.can_resume``). ``clean_start`` marks a session the user dropped
(the Manager's ``/new``): the next one starts without a resume brief. ``selected_profile_id`` is the Agent Profile the user chose for the
Manager's current session (``select_profile``); ``save_session`` never writes it.
``pending_handoff`` survives a failed first request, until the new session stores its context.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import asyncpg

from .connection import _connection, get_pool


async def get_session(
    agent_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> dict[str, Any] | None:
    """``{"session_id", "session_totals", "runtime_type", "provider_id", "profile_id", "clean_start",
    "selected_profile_id"}`` saved for ``agent_id``; None when nothing is saved.
    Includes ``pending_handoff`` only while a summary awaits the new session."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT session_id, session_totals, runtime_type, provider_id, profile_id, clean_start, "
        "selected_profile_id, pending_handoff "
        "FROM agent_sessions WHERE agent_id = $1", agent_id,
    )
    if row is None:
        return None
    totals = row["session_totals"]
    saved = {"session_id": row["session_id"], "session_totals": json.loads(totals) if isinstance(totals, str) else {},
             "runtime_type": row["runtime_type"], "provider_id": row["provider_id"],
             "profile_id": row["profile_id"], "clean_start": row["clean_start"],
             "selected_profile_id": row["selected_profile_id"]}
    if row["pending_handoff"] is not None:
        saved["pending_handoff"] = row["pending_handoff"]
    return saved


async def selects_profile(
    agent_id: str, profile_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> bool:
    """Whether ``agent_id``'s current session has ``profile_id`` selected."""
    pool = pool or get_pool()
    return bool(await pool.fetchval(
        "SELECT 1 FROM agent_sessions WHERE agent_id = $1 AND selected_profile_id = $2", agent_id, profile_id))


async def manager_sessions_selecting(
    profile_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> list[str]:
    """The ids of the Managers (``manager``, ``manager-…``) whose current session has ``profile_id`` selected."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT agent_id FROM agent_sessions WHERE (agent_id = 'manager' OR agent_id LIKE 'manager-%') "
        "AND selected_profile_id = $1 ORDER BY agent_id", profile_id)
    return [r["agent_id"] for r in rows]


async def select_profile(
    agent_id: str, profile_id: str | None, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> None:
    """Set the Agent Profile the user chose for ``agent_id``'s current session (None: follow Settings).

    Writes only ``selected_profile_id``; with no row yet, inserts one without a session.
    """
    pool = pool or get_pool()
    await pool.execute(
        """
        INSERT INTO agent_sessions (agent_id, selected_profile_id, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (agent_id) DO UPDATE SET selected_profile_id = EXCLUDED.selected_profile_id, updated_at = now()
        """,
        agent_id, profile_id,
    )


async def save_session(
    agent_id: str,
    session_id: str | None,
    session_totals: Mapping[str, Any],
    *,
    runtime_type: str | None = None,
    provider_id: str | None = None,
    profile_id: str | None = None,
    clean_start: bool = False,
    pending_handoff: str | None = None,
    pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> None:
    """Replace ``agent_id``'s session, with the runtime type and the provider it belongs to and the Agent Profile
    that ran it; a None ``session_id`` makes its next turn start a new one, without a resume brief when
    ``clean_start``."""
    from . import api_sessions

    pool = pool or get_pool()
    async with _connection(pool) as conn, conn.transaction():
        old = await conn.fetchrow(
            "SELECT session_id, runtime_type FROM agent_sessions WHERE agent_id = $1 FOR UPDATE", agent_id)
        if old and old["runtime_type"] == "api" and old["session_id"] is not None and old["session_id"] != session_id:
            await api_sessions.delete(old["session_id"], pool=conn)
        await conn.execute(
            """
            INSERT INTO agent_sessions (agent_id, session_id, session_totals, runtime_type, provider_id, profile_id,
                                        clean_start, pending_handoff, updated_at)
            VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, now())
            ON CONFLICT (agent_id) DO UPDATE
            SET session_id = EXCLUDED.session_id, session_totals = EXCLUDED.session_totals,
                runtime_type = EXCLUDED.runtime_type, provider_id = EXCLUDED.provider_id,
                profile_id = EXCLUDED.profile_id, clean_start = EXCLUDED.clean_start,
                pending_handoff = EXCLUDED.pending_handoff, updated_at = now()
            """,
            agent_id, session_id, json.dumps(dict(session_totals)), runtime_type, provider_id, profile_id,
            clean_start, pending_handoff,
        )


async def other_live_task_has_session(
    session_id: str, task_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
) -> bool:
    """Whether a live task other than ``task_id`` stores ``session_id`` on its Planner (``planner-<id>``).

    Live is ``tasks.ACTIVE_STATUSES``. A restored schedule run uses this so two Planners do not continue one
    native session after reset has cleared the schedule's holder.
    """
    from .tasks import ACTIVE_STATUSES

    if not session_id:
        return False
    pool = pool or get_pool()
    return bool(await pool.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM tasks t
            JOIN agent_sessions s ON s.agent_id = 'planner-' || t.id
            WHERE t.id <> $1 AND s.session_id = $2 AND t.status = ANY($3::text[])
        )
        """,
        task_id, session_id, list(ACTIVE_STATUSES),
    ))


async def saved_sessions(*, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[tuple[str, str | None]]:
    """``(agent_id, runtime_type)`` of every agent with a saved session to resume."""
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT agent_id, runtime_type FROM agent_sessions WHERE session_id IS NOT NULL")
    return [(row["agent_id"], row["runtime_type"]) for row in rows]
