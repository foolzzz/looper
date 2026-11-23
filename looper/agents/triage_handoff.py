"""Hand a triage run's important events to the Manager (event triage RFC, sections 4, 5.5 and 6.1).

In the same transaction that stores a run's events, each important event that owns at least one of the run's window
messages (``db.triage_events.owned_counts``) gets one ``manager_events`` row of kind ``triage``. An important event
fully covered by another owns none, so it has no row: its messages are already told under the event that owns them.
The row waits for the Manager's turn (``pending``) in the operator's direct chat, else the dashboard chat; with
neither it falls back at once (``no_chat``) and the event stays for the brief. A row holds ids only: the turn reads
the event and its messages when it runs, so a message deleted meanwhile is not told.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db import chat as db_chat, manager_events as store, triage_events
from .completion_events import WEB_CHANNEL_ID
from .managers_api import DEFAULT_MANAGER_ID
from .operator_identity import operator_dm_target
from .triage_model import Category

logger = logging.getLogger(__name__)

PAYLOAD_VERSION = "triage_v1"


def dedupe_key(event_id: int) -> str:
    return f"triage:{event_id}"


async def _web_session(conn: Any) -> str | None:
    """The operator's most recently active dashboard chat session with the default Manager: filtered to the
    operator's dashboard first (not another channel's, not an API client's, not a Planner thread), then the newest."""
    return await conn.fetchval(
        "SELECT m.session_id FROM chat_messages m LEFT JOIN chat_threads t USING (session_id) "
        "WHERE m.channel = 'web' AND NOT starts_with(m.session_id, $2) AND t.client_id IS NULL "
        "AND coalesce(t.manager_id, 'manager') = $1 "
        "GROUP BY m.session_id ORDER BY max(m.created_at) DESC, max(m.id) DESC LIMIT 1",
        DEFAULT_MANAGER_ID, db_chat.PLANNER_THREAD_PREFIX)


async def hand_off(conn: Any, run_id: str) -> list[int]:
    """Write the handoff of each important event of ``run_id`` that owns a window message and has none yet; the
    ``manager_events`` ids written. Called after the run's window is stored, in the same transaction."""
    owned = await triage_events.owned_counts(run_id, db=conn)
    events = [event for event in await triage_events.for_run(run_id, db=conn)
              if event.category is Category.IMPORTANT and event.manager_event_id is None and owned.get(event.id)]
    if not events:
        return []
    dm = await operator_dm_target()
    session = None if dm is not None else await _web_session(conn)
    if dm is not None:
        where = {"target_kind": "dm", "channel_id": dm.channel_id, "chat": dm.chat, "session_key": None}
    elif session is not None:
        where = {"target_kind": "web", "channel_id": WEB_CHANNEL_ID, "chat": session, "session_key": session}
    else:
        where = {"target_kind": "web", "channel_id": WEB_CHANNEL_ID, "chat": "", "session_key": None}
    state, reason = ("pending", None) if where["chat"] else ("fallback", store.Reason.NO_CHAT.value)
    written = []
    for event in events:
        recorded = await store.record(
            manager_id=DEFAULT_MANAGER_ID, task_id=None, origin="chat", dedupe_key=dedupe_key(event.id),
            payload={"version": PAYLOAD_VERSION, "event_id": event.id, "run_id": run_id,
                     "messages": owned[event.id]},
            fallback=[], state=state, reason=reason, kind=store.EventKind.TRIAGE.value, pool=conn, **where)
        await conn.execute("UPDATE triage_events SET manager_event_id = $2 WHERE id = $1 AND manager_event_id IS NULL",
                           event.id, recorded.row["id"])
        if recorded.inserted and state == "fallback":  # settled at once: the event waits for the brief
            await conn.execute("UPDATE manager_events SET finished_at = now() WHERE id = $1", recorded.row["id"])
        written.append(recorded.row["id"])
    logger.info("triage run %s: %d important event(s) handed to the Manager (%s)", run_id, len(written),
                state if reason is None else f"{state}/{reason}")
    return written
