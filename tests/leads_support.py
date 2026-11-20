"""What the lead and intent tests share: synthetic leads and intents over the subscription tests' synthetic events
(``tests.subscription_support``: ``source``, ``chat``, ``person``, ``add``)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from tests import subscription_support as stream

UTC = timezone.utc
SOURCE = stream.SOURCE


async def lead(pool: Any, sources: Sequence[int], *, kind: str = "decision", summary: str = "Ship on Friday",
               detail: str = "", confidence: str = "high", state: str = "active", valid_until: datetime | None = None,
               people: Sequence[tuple[str, str]] = (), source_id: str = SOURCE) -> int:
    """A stored lead over the events ``sources`` (their seqs); ``people`` are (open_id, role) pairs."""
    sent = await pool.fetch("SELECT sent_at FROM source_events WHERE seq = ANY($1::bigint[])", list(sources))
    times = sorted(row["sent_at"] for row in sent)
    lead_id = await pool.fetchval(
        "INSERT INTO message_leads (source_id, kind, summary, detail, confidence, state, valid_until, marked_via, "
        "first_sent_at, last_sent_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
        source_id, kind, summary, detail, confidence, state,
        valid_until or datetime.now(UTC) + timedelta(days=90), "cli" if state == "inaccurate" else None,
        times[0], times[-1])
    for seq in sources:
        await pool.execute("INSERT INTO message_lead_sources (lead_id, event_seq) VALUES ($1, $2)", lead_id, seq)
    for open_id, role in people:
        await pool.execute("INSERT INTO message_lead_people (lead_id, open_id, role) VALUES ($1, $2, $3)",
                           lead_id, open_id, role)
    return lead_id


async def intent(pool: Any, sources: Sequence[int], *, type_key: str = "dev_task", title: str = "Fix the login page",
                 summary: str = "Someone asked the user to fix the login page error.", state: str = "pending",
                 confidence: str = "high", merged_into: int | None = None, source_id: str = SOURCE,
                 **columns: Any) -> int:
    """A stored intent over the events ``sources``; ``columns`` sets any other column of ``intents``."""
    chat = await pool.fetchval("SELECT chat_id FROM source_events WHERE seq = $1", sources[0])
    handled = state not in ("pending", "merged")
    values = {"source_id": source_id, "type_key": type_key, "type_version": 1, "title": title, "summary": summary,
              "confidence": confidence, "state": state, "chat_id": chat, "merged_into": merged_into,
              "handled_at": datetime.now(UTC) if handled else None, "handled_via": "dashboard" if handled else None,
              **columns}
    names = list(values)
    intent_id = await pool.fetchval(
        f"INSERT INTO intents ({', '.join(names)}) VALUES ({', '.join(f'${i + 1}' for i in range(len(names)))}) "
        "RETURNING id", *values.values())
    for seq in sources:
        await pool.execute("INSERT INTO intent_sources (intent_id, event_seq) VALUES ($1, $2)", intent_id, seq)
    return intent_id
