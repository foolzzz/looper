"""``looper memory`` reads, rejects, retries and deletes candidates, and checks what a write needs, directly in
PostgreSQL, also with the service stopped (memory-write-path RFC, section 9.2).

A candidate holds the operator's own words, so every command is refused inside a Looper agent run, as ``looper
leads`` is. There is no ``confirm``: a candidate is confirmed only on the dashboard or in the operator's direct chat.
Exit codes: 0 done, 1 nothing found (or a check failed), 2 usage, 3 not configured, 4 refused, 5 no such candidate,
6 database unusable, 7 state does not allow the action.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

from .. import child_env
from ..db import memory as db_memory
from . import memory, memory_actions, memory_model as model, memory_settings, memory_lessons
from .ops import OpError

logger = logging.getLogger(__name__)
OK, EMPTY, USAGE, NOT_CONFIGURED, REFUSED, NOT_FOUND, NO_DATABASE, WRONG_STATE = 0, 1, 2, 3, 4, 5, 6, 7
LIMIT_MAX = 200

KIND_ZH = {"preference": "偏好", "feedback": "反馈", "decision": "决定", "lesson": "经验", "fact": "事实",
           "commitment": "承诺", "person": "人物", "project": "项目"}
STATE_ZH = {"drafting": "起草中", "pending": "待确认", "confirmed": "已确认", "writing": "写入中", "written": "已写入",
            "rejected": "已驳回", "expired": "已过期", "conflict": "冲突", "blocked": "受阻", "dropped": "已丢弃",
            "held": "待复核", "in_effect": "生效中", "revoked": "已撤销", "superseded": "已被取代"}
SOURCE_ZH = {"manager_turn": "Manager 对话", "journal": "任务日志", "lead": "消息线索"}


def _local(text: str | None) -> str:
    return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M") if text else ""


def _state(item: dict[str, Any]) -> str:
    label = STATE_ZH[item["state"]]
    if item["reason"]:
        label += f"：{item['reason']}"
    if item["state"] == "pending" and item["expires_at"]:
        label += f"，{_local(item['expires_at'])} 过期"
    if item["state"] == "written" and item["commit"]:
        label += f"，commit {item['commit'][:12]}"
    return f"（{label}）"


def _line(item: dict[str, Any]) -> str:
    title = item["title"] or item["statement"] or "（文字已清除）"
    where = f" → {item['note_path']}" if item["note_path"] else ""
    return f"{item['id']} [{KIND_ZH[item['kind']]}] {title}{where}{_state(item)}"


def _detail(item: dict[str, Any]) -> list[str]:
    lines = [_line(item)]
    if item["statement"]:
        lines.append(f"  陈述：{item['statement']}")
    if item["why"]:
        lines.append(f"  理由：{item['why']}")
    flags = [name for name, on in (("来自消息", item["untrusted"]), ("来源里有指令", item["flagged"]),
                                   ("可能是硬规则", item["hard_rule"])) if on]
    if flags:
        lines.append(f"  标记：{'、'.join(flags)}")
    for source in item["sources"]:
        where = source["task_id"] or source["lead"] or " · ".join(
            part for part in (source["channel"], _local(source["said_at"])) if part)
        lines.append(f"  出处：{SOURCE_ZH[source['kind']]} {where}".rstrip())
    if item["current"]:
        current = item["current"]
        lines.append(f"  修订 {current['revision']}（{current['made_by']}）：{current['title']}")
        for change in current["changes"]:
            stats = current["diff_stats"].get(change["path"], {})
            counted = f" +{stats.get('added', 0)} -{stats.get('removed', 0)}" if stats else ""
            lines.append(f"    {change['op']} {change['path']}{counted}")
    for event in item["events"]:
        note = f"：{event['note']}" if event["note"] else ""
        lines.append(f"  {_local(event['at'])} {event['action']} · {event['actor']} · {event['via']}{note}")
    return lines


def _check_lines(result: dict[str, Any]) -> list[str]:
    lines = [f"{'✓' if item['ok'] else '✗'} {item['name']}: {item['detail']}".rstrip(": ")
             for item in result["checks"]]
    if result["configured"]:
        lines.append("记忆写入" + ("已打开" if result["enabled"] else "没有打开"))
    return lines


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


async def _command(pool: asyncpg.Pool, args: Any) -> tuple[int, str]:
    if args.memory_command == "lessons":
        result = await memory_lessons.list_view(project=args.project, state=args.state, limit=args.limit,
                                                 before=getattr(args, "before", None), db=pool)
        code = OK if result["lessons"] else EMPTY
        if args.json:
            return code, _dump(result)
        lines = [_line(item["candidate"]) for item in result["lessons"]]
        if result["more"]:
            lines.append("还有更多，用 --before 继续查看")
        return code, "\n".join(lines) if lines else "没有项目经验"
    if args.memory_command in {"reject", "retry", "delete", "revoke", "renew"}:
        try:
            service = memory_lessons if args.memory_command in {"revoke", "renew"} else memory_actions
            result = await service.act(args.memory_command, args.candidate, via=model.Via.CLI, db=pool,
                                       reason=getattr(args, "reason", None))
        except db_memory.MemoryRefused as error:
            return {"not_found": NOT_FOUND, "state": WRONG_STATE, "stale": WRONG_STATE,
                    "disabled": NOT_CONFIGURED, "invalid": USAGE}.get(error.code, REFUSED), str(error)
        if args.memory_command == "delete":
            return OK, f"{result['candidate']} 已删除，只留指纹墓碑；vault 笔记不变"
        return OK, _line(result["candidate"])
    if args.memory_command == "list":
        result = await memory.list_view(state=args.state, kind=args.kind, limit=args.limit, db=pool)
        if args.json:
            return (OK if result["candidates"] else EMPTY), _dump(result)
        if not result["candidates"]:
            return EMPTY, f"没有记忆候选（{args.state}）"
        lines = [_line(item) for item in result["candidates"]]
        if result["more"]:
            lines.append("还有更多，用 --limit 看")
        return OK, "\n".join(lines)
    if args.memory_command == "show":
        found = await memory.get_view(args.candidate, db=pool)
        if found is None:
            return NOT_FOUND, f"没有这条记忆候选：{args.candidate}"
        return OK, _dump(found) if args.json else "\n".join(_detail(found))
    result = await memory_settings.check(runtime_root=args.runtime_root_path, db=pool)
    text = _dump(result) if args.json else "\n".join(_check_lines(result))
    if not result["configured"]:
        return NOT_CONFIGURED, text
    return (OK if result["ok"] else EMPTY), text


async def _run(database_url: str, args: Any) -> tuple[int, str]:
    from ..db.connection import close_pool, init_pool

    try:
        pool = await init_pool(database_url, min_size=1, max_size=2)
    except (OSError, asyncpg.PostgresError, TimeoutError, ValueError) as error:
        return NO_DATABASE, f"cannot reach the database: {error}"
    try:
        if not await pool.fetchval("SELECT to_regclass('memory_candidates') IS NOT NULL"):
            return NO_DATABASE, "the database has no memory tables yet: start `looper serve` once to upgrade it"
        try:
            return await _command(pool, args)
        except ValueError as error:
            return USAGE, str(error)
        except OpError as error:
            return NOT_FOUND if error.code.value == "not_found" else REFUSED, str(error)
    except (OSError, asyncpg.PostgresError, TimeoutError) as error:
        return NO_DATABASE, f"the database failed: {error}"
    finally:
        await close_pool()


def run(args: Any, load_ops: Any) -> int:
    if os.environ.get(child_env.AGENT_RUN_ENV):
        logger.warning("memory cli refused reason=agent_run")
        print("记忆候选只供操作者查看，请在自己的终端运行", file=sys.stderr)
        return REFUSED
    try:
        if args.memory_command in {"list", "lessons"}:
            if not 1 <= args.limit <= LIMIT_MAX:
                raise ValueError(f"--limit must be 1 to {LIMIT_MAX}")
            if args.memory_command == "lessons":
                if args.state != "all" and args.state not in model.LOOPER_STATES:
                    raise ValueError("state must be all or a lesson state")
            else:
                memory.states_for(args.state)
            if getattr(args, "kind", None) is not None and args.kind not in set(model.Kind):
                raise ValueError(f"unknown kind {args.kind!r}: one of {', '.join(model.Kind)}")
        elif args.memory_command in {"show", "reject", "retry", "delete", "revoke", "renew"}:
            model.parse_id(args.candidate)
            if args.memory_command in {"reject", "revoke"} and args.reason is not None and len(args.reason) > model.NOTE_MAX_CHARS:
                raise ValueError(f"--reason has at most {model.NOTE_MAX_CHARS} characters")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return USAGE
    ops = load_ops(args)
    if ops is None:
        return NO_DATABASE
    args.runtime_root_path = Path(ops.runtime_root)
    code, text = asyncio.run(_run(ops.database_url, args))
    print(text, file=sys.stdout if code in (OK, EMPTY) else sys.stderr)
    return code
