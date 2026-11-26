"""``looper leads`` searches and administers sourced, unconfirmed notes directly in PostgreSQL.

``eval-export`` writes the leads the operator marked inaccurate, and a random sample of active ones, with the text of
their source messages, into a local JSONL file for the leads check's eval (``evals/processor --cases-file``): only to
a file outside any git work tree or one git ignores, so a real eval set is never committed.

Every command is refused inside a Looper agent run. Exit codes match the ledger CLI: 0 done, 1 empty,
2 usage, 3 disabled, 4 refused, 5 missing lead, 6 unusable database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
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
    return {"expired": "（已过期）", "inaccurate": "（不准确）", "promoted": "（已晋升）", "dropped": "（已被复核丢弃）"}[state]


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


SAMPLE_MAX = 500
# What ``eval-export`` writes of each source message: enough to render it as the check and the extraction read it.
_EXPORT_SOURCES = """
SELECT s.lead_id, e.seq, e.external_id, e.chat_id, e.parent_id, e.sender_id, e.sender_kind, e.sent_at, e.text,
       c.kind AS chat_kind, COALESCE(NULLIF(c.name, ''), c.chat_id) AS chat_name, COALESCE(p.name, '') AS sender_name,
       e.sender_id = src.user_open_id AS from_user
FROM message_lead_sources s
JOIN source_events e ON e.seq = s.event_seq
JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id
JOIN event_sources src ON src.id = e.source_id
LEFT JOIN source_people p ON p.source_id = e.source_id AND p.open_id = e.sender_id
WHERE s.lead_id = ANY($1::bigint[])
ORDER BY s.lead_id, e.sent_at, e.seq
"""


def resolved(path: Path) -> Path:
    """Where ``path`` really is: its nearest existing directory with every symbolic link resolved, then the rest of
    it (which does not exist yet, so it holds no link)."""
    target = path.expanduser().absolute()
    where = next((parent for parent in target.parents if parent.is_dir()), Path("/"))
    return Path(os.path.realpath(where)) / target.relative_to(where)


def committable(path: Path) -> str | None:
    """Why ``path`` could be committed, or None when it cannot: it is outside every work tree, or ``git
    check-ignore`` matches it, both judged where it really is (``resolved``), so a link under an ignored name cannot
    lead into a tracked place. The file itself being a symbolic link, or a git that cannot answer, is a reason."""
    if path.expanduser().absolute().is_symlink():
        return f"{path} is a symbolic link: write the eval set to a regular file"
    target = resolved(path)
    where = next((parent for parent in target.parents if parent.is_dir()), Path("/"))
    try:
        inside = subprocess.run(["git", "-C", str(where), "rev-parse", "--is-inside-work-tree"], capture_output=True,
                                text=True, timeout=30)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        ignored = subprocess.run(["git", "-C", str(where), "check-ignore", "-q", "--", str(target)],
                                 capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"cannot ask git whether {target} could be committed ({type(error).__name__})"
    if ignored.returncode == 0:
        return None
    return (f"{target} is inside a git work tree and git does not ignore it: write the eval set where it cannot be "
            "committed (evals/runs/, or outside the repository)")


async def _export(pool: asyncpg.Pool, out: Path, sample: int) -> tuple[int, str]:
    """Write every inaccurate lead (``inaccurate``) and ``sample`` random active ones (``presumed_good``) with their
    source messages to ``out``, one JSON object per line; only the counts are printed."""
    marked = await pool.fetch("SELECT id, kind, summary, detail, confidence FROM message_leads "
                              "WHERE state = 'inaccurate' ORDER BY id")
    good = await pool.fetch("SELECT id, kind, summary, detail, confidence FROM message_leads "
                            "WHERE state = 'active' ORDER BY random() LIMIT $1", sample) if sample else []
    leads_found = [(row, "inaccurate") for row in marked] + [(row, "presumed_good") for row in good]
    if not leads_found:
        return EMPTY, "没有可导出的线索（没有标为不准确的，也没有有效的）"
    sources: dict[int, list[dict[str, Any]]] = {}
    for row in await pool.fetch(_EXPORT_SOURCES, [row["id"] for row, _ in leads_found]):
        sources.setdefault(row["lead_id"], []).append(dict(row))
    lines = []
    for row, label in leads_found:
        found = sources.get(row["id"], [])
        refs = {item["external_id"]: f"s{number}" for number, item in enumerate(found, 1) if item["external_id"]}
        lines.append(json.dumps({
            "id": leads_model.format_lead_id(row["id"]), "label": label, "kind": row["kind"],
            "summary": row["summary"], "detail": row["detail"], "confidence": row["confidence"],
            "sources": [{"ref": f"s{number}", "chat": item["chat_name"], "chat_kind": item["chat_kind"],
                         "sender": item["sender_name"] or (item["sender_id"] or "")[:10],
                         "sender_kind": item["sender_kind"], "from_user": bool(item["from_user"]),
                         "reply_to": refs.get(item["parent_id"] or ""), "sent_at": item["sent_at"].isoformat(),
                         "text": item["text"] or ""} for number, item in enumerate(found, 1)]},
            ensure_ascii=False))
    code, target = _write_export(out, lines)
    if code != OK:
        return code, target
    return OK, (f"已导出 {len(lines)} 条到 {target}：不准确 {len(marked)} 条，看作正确 {len(good)} 条"
                "（含出处消息原文，只留在本机）")


def _write_export(out: Path, lines: list[str]) -> tuple[int, str]:
    # Written where it really is (checked by ``committable``), through a file of mode 600 beside it that replaces
    # it at once: a link is never followed, and a failure leaves nothing behind.
    target = resolved(out)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            return REFUSED, f"{out} is a symbolic link: write the eval set to a regular file"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write("".join(f"{line}\n" for line in lines))
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    except OSError as error:
        return USAGE, f"cannot write {out}: {error}"
    return OK, str(target)


async def _export_intents(pool: asyncpg.Pool, out: Path, chat: str, days: int) -> tuple[int, str]:
    """Export one chat's human windows and all sourced lead labels; identities and summaries stay out."""
    exact = await pool.fetch("SELECT source_id, chat_id FROM source_chats WHERE chat_id = $1 LIMIT 2", chat)
    chats = exact or await pool.fetch(
        "SELECT source_id, chat_id FROM source_chats WHERE name ILIKE '%' || $1 || '%' LIMIT 2", chat)
    if len(chats) != 1:
        raise ValueError("--chat must identify exactly one collected chat")
    sid, cid = chats[0]["source_id"], chats[0]["chat_id"]
    until = datetime.now().astimezone()
    since = until - timedelta(days=days)
    rows = await pool.fetch(
        "SELECT seq, external_id, parent_id, sender_id, sent_at, text FROM source_events "
        "WHERE source_id = $1 AND chat_id = $2 AND sender_kind = 'user' AND origin <> 'import' "
        "AND sent_at >= $3 AND sent_at < $4 ORDER BY seq", sid, cid, since, until)
    if not rows:
        return EMPTY, "没有可导出的消息"
    refs = {row["seq"]: f"x{number}" for number, row in enumerate(rows, 1)}
    external = {row["external_id"]: refs[row["seq"]] for row in rows}
    people: dict[str, str] = {}
    windows: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        text = row["text"] or ""
        if len(text) > 60_000:
            raise ValueError("a message exceeds the 60,000-character window limit")
        if messages and (len(messages) == 500 or size + len(text) > 60_000):
            windows.append({"type": "window", "number": len(windows) + 1, "messages": messages})
            messages, size = [], 0
        sender = people.setdefault(row["sender_id"], f"P{len(people) + 1}")
        messages.append({"ref": refs[row["seq"]], "sender": sender, "sent_at": row["sent_at"].isoformat(),
                         "text": text, "reply_to": external.get(row["parent_id"])})
        size += len(text)
    if messages:
        windows.append({"type": "window", "number": len(windows) + 1, "messages": messages})
    found = await pool.fetch(
        "SELECT l.id, l.kind, array_agg(s.event_seq ORDER BY s.event_seq) AS seqs "
        "FROM message_leads l JOIN message_lead_sources s ON s.lead_id = l.id "
        "WHERE s.event_seq = ANY($1::bigint[]) GROUP BY l.id, l.kind ORDER BY l.id", list(refs))
    labels = [{"type": "label", "lead": leads_model.format_lead_id(row["id"]), "kind": row["kind"],
               "expected": "intent" if row["kind"] in ("request", "issue") else "none",
               "sources": [refs[seq] for seq in row["seqs"]]} for row in found]
    code, destination = _write_export(out, [json.dumps(line, ensure_ascii=False) for line in [*windows, *labels]])
    if code != OK:
        return code, destination
    kinds = {kind: sum(row["kind"] == kind for row in labels) for kind in ("request", "issue")}
    return OK, (f"已导出 {len(windows)} 个窗口，{len(rows)} 条消息；请求 {kinds['request']} 条，"
                f"问题 {kinds['issue']} 条，其他 {len(labels) - sum(kinds.values())} 条标签"
                "（标签须在本机核对，只留在本机）")


async def _command(pool: asyncpg.Pool, args: Any) -> tuple[int, str]:
    if args.leads_command == "eval-export":
        if args.intents:
            return await _export_intents(pool, Path(args.out), args.chat, args.days)
        return await _export(pool, Path(args.out), args.sample)
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
    from ..cli import task_manager_guard

    if (code := task_manager_guard("leads", args.leads_command)) is not None:
        return code
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
        elif args.leads_command == "eval-export":
            selected = (args.chat is not None, args.days is not None, args.intents)
            if any(selected) and not all(selected):
                raise ValueError("--chat, --days and --intents must be given together")
            if args.intents and (not args.chat.strip() or not 1 <= args.days <= 30):
                raise ValueError("--chat must be nonempty and --days must be 1 to 30")
            if not 0 <= args.sample <= SAMPLE_MAX:
                raise ValueError(f"--sample must be 0 to {SAMPLE_MAX}")
        else:
            leads_model.parse_lead_id(args.lead)
            if args.leads_command == "mark" and args.note is not None and len(args.note.strip()) > 500:
                raise ValueError("note must be at most 500 characters")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return USAGE
    if args.leads_command == "eval-export" and (why := committable(Path(args.out))) is not None:
        logger.warning("leads eval-export refused reason=committable")
        print(why, file=sys.stderr)
        return REFUSED
    ops = load_ops(args)
    if ops is None:
        return NO_DATABASE
    code, text = asyncio.run(_run(ops.database_url, args))
    print(text, file=sys.stderr if code not in (OK, EMPTY) else sys.stdout)
    return code
