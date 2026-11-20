"""``looper leads`` searches and administers sourced, unconfirmed notes directly in PostgreSQL.

Every command is refused inside a Looper agent run. Exit codes match the ledger CLI: 0 done, 1 empty,
2 usage, 3 disabled, 4 refused, 5 missing lead, 6 unusable database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import date, datetime, time, timedelta
from typing import Any

import asyncpg

from .. import child_env
from ..db import lead_admin
from . import leads, leads_model

logger = logging.getLogger(__name__)
OK, EMPTY, USAGE, DISABLED, REFUSED, NOT_FOUND, NO_DATABASE = 0, 1, 2, 3, 4, 5, 6
_JSON_FIELDS = ("id", "kind", "summary", "detail", "confidence", "state", "valid_until", "sources")
_JSON_SOURCE_FIELDS = ("chat", "sender", "sent_at", "message_id", "link")


def _since(text: str | None, days: int | None) -> datetime | None:
    if text is not None and days is not None:
        raise ValueError("--since and --days cannot be used together")
    if days is not None:
        if not 1 <= days <= 3650:
            raise ValueError("--days must be 1 to 3650")
        return datetime.now().astimezone() - timedelta(days=days)
    if text is None:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("--since must be YYYY-MM-DD")
    return datetime.combine(date.fromisoformat(text), time()).astimezone()


def _state(item: dict[str, Any]) -> str:
    state = item["state"]
    if state == "active":
        return f"（有效至 {datetime.fromisoformat(item['valid_until']).astimezone():%Y-%m-%d}）"
    if state == "superseded":
        target = f" → {item['superseded_by']}" if item["superseded_by"] else ""
        return f"（已被推翻{target}）"
    return {"expired": "（已过期）", "inaccurate": "（不准确）", "promoted": "（已晋升）"}[state]


def _lines(item: dict[str, Any]) -> list[str]:
    lines = [f"{item['id']} [{leads_model.LEAD_KIND_ZH[item['kind']]}·"
             f"{leads_model.CONFIDENCE_ZH[item['confidence']]}] {item['summary']}{_state(item)}"]
    if item["sources"]:
        first = item["sources"][0]
        sent = datetime.fromisoformat(first["sent_at"]).astimezone()
        source = f"  出处：《{first['chat']}》{first['sender']} {sent:%Y-%m-%d %H:%M} · {first['message_id']}"
        if first["link"]:
            source += f" · {first['link']}"
        if item["sources_total"] > 1:
            source += f"（另 {item['sources_total'] - 1} 条）"
        lines.append(source)
    return lines


async def _command(pool: asyncpg.Pool, args: Any) -> tuple[int, str]:
    if args.leads_command == "search":
        query = {"words": args.words, "chat": args.chat, "person": args.person, "since": args.since_time,
                 "kind": args.kind, "state": args.state, "limit": args.limit}
        result = await leads.search(**query, db=pool)
        if args.json:
            items = [{**{key: item[key] for key in _JSON_FIELDS if key != "sources"},
                      "sources": [{key: source[key] for key in _JSON_SOURCE_FIELDS} for source in item["sources"]]}
                     for item in result["leads"]]
            return (OK if items else EMPTY), json.dumps(
                {"notice": result["notice"], "query": query, "leads": items, "more": result["more"]},
                ensure_ascii=False, indent=2, default=lambda value: value.isoformat())
        if not result["leads"]:
            return EMPTY, f"没有找到线索（查询：{' '.join(args.words) or '全部'}）"
        lines = [result["notice"], *(line for item in result["leads"] for line in _lines(item))]
        if result["more"]:
            lines.append(f"另有 {result['more']} 条，用 --limit 看更多")
        return OK, "\n".join(lines)
    number = leads_model.parse_lead_id(args.lead)
    name = leads_model.format_lead_id(number)
    if args.leads_command == "mark":
        found = await lead_admin.mark_inaccurate(number, via="cli", note=args.note, db=pool)
        done = f"{name} 已标为不准确"
    else:
        found = await lead_admin.delete_lead(number, db=pool)
        done = f"{name} 已删除"
    return (OK, done) if found else (NOT_FOUND, f"没有这条线索：{name}")


async def _run(database_url: str, args: Any) -> tuple[int, str]:
    from ..db.connection import close_pool, init_pool

    try:
        pool = await init_pool(database_url, min_size=1, max_size=2)
    except (OSError, asyncpg.PostgresError, TimeoutError, ValueError) as error:
        return NO_DATABASE, f"cannot reach the database: {error}"
    try:
        if not await pool.fetchval("SELECT to_regclass('message_leads') IS NOT NULL"):
            return NO_DATABASE, "the database has no message leads tables yet: start `looper serve` once to upgrade it"
        if args.leads_command != "delete" and not await leads_model.leads_enabled(db=pool):
            return DISABLED, "消息线索没有启用：在控制台的消息流页打开"
        try:
            return await _command(pool, args)
        except lead_admin.LeadConflictError as error:
            return REFUSED, str(error)
        except ValueError as error:
            return USAGE, str(error)
    except (OSError, asyncpg.PostgresError, TimeoutError) as error:
        return NO_DATABASE, f"the database failed: {error}"
    finally:
        await close_pool()


def run(args: Any, load_ops: Any) -> int:
    if os.environ.get(child_env.AGENT_RUN_ENV):
        logger.warning("leads cli refused reason=agent_run")
        print("消息线索只供操作者查询和修改，请在自己的终端运行", file=sys.stderr)
        return REFUSED
    try:
        if args.leads_command == "search":
            if not 1 <= args.limit <= 50:
                raise ValueError("--limit must be 1 to 50")
            args.since_time = _since(args.since, args.days)
            if args.kind is not None and args.kind not in {kind.value for kind in leads_model.LeadKind}:
                raise ValueError("unknown lead kind")
            if args.state not in leads_model.SEARCH_STATES:
                raise ValueError("unknown lead state")
        else:
            leads_model.parse_lead_id(args.lead)
            if args.leads_command == "mark" and args.note is not None and len(args.note.strip()) > 500:
                raise ValueError("note must be at most 500 characters")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return USAGE
    ops = load_ops(args)
    if ops is None:
        return NO_DATABASE
    code, text = asyncio.run(_run(ops.database_url, args))
    print(text, file=sys.stderr if code not in (OK, EMPTY) else sys.stdout)
    return code
