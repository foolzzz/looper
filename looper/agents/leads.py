"""Searching message leads (message-leads-intents RFC, section 7.1): what ``looper leads search``, the operator's
``leads.search`` and the Manager's ``looper_leads_search`` all return.

``search`` reads ``db.message_leads`` and shapes each lead as ``LEAD_FIELDS`` say: no message text, the chat's and the
sender's names as they are now, every text that came from messages without its @ markup
(``subscriptions.strip_mentions``). Callers drop the fields they do not show; none adds its own query.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from ..db import message_leads as store
from . import leads_model
from .subscriptions import strip_mentions

# A lead as every surface gets it (``--json``, ``leads.search``; the Manager tool drops the ids).
LEAD_FIELDS = ("id", "kind", "summary", "detail", "confidence", "state", "valid_until", "superseded_by",
               "sources", "sources_total")
SOURCE_FIELDS = ("chat", "chat_id", "chat_kind", "sender", "sender_id", "sent_at", "message_id", "link")
# Where a chat opens in the Lark client (AppLink). Message-level links are not verified yet (RFC section 17).
CHAT_LINK = "https://applink.feishu.cn/client/chat/open?openChatId={chat}"


def chat_link(chat_id: str) -> str | None:
    """The link that opens ``chat_id`` in the Lark client; None for a chat id it cannot be (not ``oc_``)."""
    return CHAT_LINK.format(chat=quote(chat_id, safe="")) if chat_id.startswith("oc_") else None


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def source_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"chat": strip_mentions(row["chat_name"] or "") or row["chat_id"], "chat_id": row["chat_id"],
            "chat_kind": row["chat_kind"], "sender": strip_mentions(row["sender_name"] or "") or row["sender_id"],
            "sender_id": row["sender_id"], "sent_at": _iso(row["sent_at"]), "message_id": row["external_id"],
            "link": chat_link(row["chat_id"])}


def lead_view(row: Mapping[str, Any], sources: Sequence[Mapping[str, Any]], *, now: datetime) -> dict[str, Any]:
    """One lead as ``LEAD_FIELDS`` name it: at most ``SOURCES_SHOWN`` sources, oldest first, and how many it has."""
    return {"id": leads_model.format_lead_id(row["id"]), "kind": row["kind"],
            "summary": strip_mentions(row["summary"]), "detail": strip_mentions(row["detail"] or ""),
            "confidence": row["confidence"], "state": leads_model.shown_state(row["state"], row["valid_until"], now),
            "valid_until": _iso(row["valid_until"]),
            "superseded_by": leads_model.format_lead_id(row["superseded_by"]) if row["superseded_by"] else None,
            "sources": [source_view(source) for source in sources[:store.SOURCES_SHOWN]],
            "sources_total": len(sources)}


async def search(*, words: Sequence[str] = (), chat: str | None = None, person: str | None = None,
                 since: datetime | None = None, kind: str | None = None, state: str = "active", limit: int = 10,
                 source_id: str | None = None, now: datetime | None = None, db: Any = None) -> dict[str, Any]:
    """``{"notice", "leads": [lead_view, ...], "more"}``: the leads ``db.message_leads.search_rows`` finds, best first,
    and how many more match than were returned, matched as ``leads.search_mode`` says (with pg_trgm when it is
    installed). ``ValueError`` for a ``state`` or ``kind`` it does not know."""
    if kind is not None and kind not in {k.value for k in leads_model.LeadKind}:
        raise ValueError(f"kind must be one of {', '.join(k.value for k in leads_model.LeadKind)}")
    now = now or datetime.now(timezone.utc)
    fulltext = await leads_model.search_mode(db) is leads_model.SearchMode.FULLTEXT
    trigram = await store.trigram_schema(db) if fulltext else None
    rows, total = await store.search_rows(now=now, words=words, chat=chat, person=person, since=since, kind=kind,
                                          state=state, limit=limit, source_id=source_id, fulltext=fulltext,
                                          trigram=trigram, db=db)
    sources = await store.source_rows([row["id"] for row in rows], db=db)
    return {"notice": leads_model.NOTICE,
            "leads": [lead_view(row, sources.get(row["id"], []), now=now) for row in rows],
            "more": max(0, total - len(rows))}
