"""Operator-initiated memory proposals whose seed is copied from a message lead by code."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..db import memory as db_memory, message_leads
from . import leads, leads_model, memory, memory_settings
from .advice_lint import _SECRETS
from .leads_model import LeadKind, LeadState
from .memory_model import ALLOWED_CLASSES, Actor, EvidenceKind, Kind, SourceClass, Via, format_id
from .subscriptions import strip_mentions

KIND_MAP = {LeadKind.DECISION: Kind.DECISION, LeadKind.COMMITMENT: Kind.COMMITMENT, LeadKind.FACT: Kind.FACT}
EXTERNAL_KINDS = tuple(k for k in Kind if SourceClass.EXTERNAL in ALLOWED_CLASSES[k])


@dataclass(frozen=True)
class LeadSeed:
    lead_id: int
    kind: Kind
    statement: str
    why: str
    keywords: list[str]


async def seed_from_lead(conn: Any, lead_id: int, kind: str | None) -> LeadSeed:
    row = await conn.fetchrow("SELECT * FROM message_leads WHERE id = $1 FOR SHARE", lead_id)
    name = leads_model.format_lead_id(lead_id)
    if row is None:
        raise db_memory.MemoryRefused(f"no message lead {name}", code="not_found")
    if row["kind"] not in KIND_MAP:
        raise db_memory.MemoryRefused(
            f"{name} is a {row['kind']}: only decisions, commitments and facts can be kept", code="invalid")
    if kind is not None and kind not in EXTERNAL_KINDS:
        raise db_memory.MemoryRefused(
            "a message lead can never become a preference, a correction or a lesson", code="class")
    state = leads_model.shown_state(row["state"], row["valid_until"], datetime.now(timezone.utc))
    if state not in (LeadState.ACTIVE, leads_model.EXPIRED):
        detail = (f" by {leads_model.format_lead_id(row['superseded_by'])}" if state == LeadState.SUPERSEDED else
                  f" as {row['promoted_ref']}" if state == LeadState.PROMOTED else "")
        raise db_memory.MemoryRefused(f"{name} is {state}{detail}", code="state", state=state)
    statement = strip_mentions(row["summary"]).strip()
    why = strip_mentions(row["detail"] or "").strip()[:300]
    if _SECRETS.search(statement) or _SECRETS.search(why):
        raise db_memory.MemoryRefused(
            "Looper never stores credentials in memory (Looper 不往记忆里存凭据)", code="invalid")
    sources = [leads.source_view(source) for source in (await message_leads.source_rows([lead_id], db=conn)).get(
        lead_id, ())]
    keywords, seen = [], set()
    for field in ("sender", "chat"):
        for source in sources:
            word = source[field].strip()[:40]
            if word and word.casefold() not in seen:
                seen.add(word.casefold())
                keywords.append(word)
    return LeadSeed(lead_id, Kind(kind) if kind is not None else KIND_MAP[row["kind"]], statement, why,
                    keywords[:6] or [statement[:40]])


async def propose_from_lead(lead: str, *, kind: str | None = None, actor: Actor, via: Via,
                            manager_id: str | None = None, db: Any = None) -> dict:
    raw = await memory_settings.stored(db)
    sources = raw.get("sources") if raw else None
    if not raw or raw.get("enabled") is not True or not isinstance(sources, Mapping) or sources.get("leads") is not True:
        raise db_memory.MemoryRefused("the message lead memory source is disabled", code="disabled")
    settings, problem = await memory_settings.load(db)
    if settings is None:
        raise db_memory.MemoryRefused(problem or "memory_write is not configured", code="invalid")
    try:
        lead_id = leads_model.parse_lead_id(lead)
    except ValueError as error:
        raise db_memory.MemoryRefused(str(error), code="invalid") from None
    async with db_memory.transaction(db) as conn:
        # SHARE protects the source; this per-lead lock also serializes the open-candidate check and insert.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended("
                           "current_schema() || ':memory_lead:' || $1, 0))", str(lead_id))
        seed = await seed_from_lead(conn, lead_id, kind)
        if seed.kind not in settings.targets:
            raise db_memory.MemoryRefused(
                f"kind {seed.kind} has no target in the memory_write setting", code="invalid")
        busy = await conn.fetchval(
            "SELECT c.id FROM memory_candidates c JOIN memory_candidate_sources s ON s.candidate_id = c.id "
            "WHERE s.lead_id = $1 AND c.state NOT IN ('written', 'rejected', 'dropped') LIMIT 1", lead_id)
        if busy is not None:
            raise db_memory.MemoryRefused(
                f"{leads_model.format_lead_id(lead_id)} is already proposed as {format_id(busy)}", code="busy")
        number = await db_memory.create(conn, kind=seed.kind, statement=seed.statement, why=seed.why,
                                        keywords=seed.keywords, evidence=[db_memory.Evidence(
                                            EvidenceKind.LEAD, lead_id=lead_id)], actor=actor, via=via,
                                        manager_id=manager_id, untrusted=True)
        return {"candidate": memory.detail_view(await db_memory.get(number, db=conn))}
