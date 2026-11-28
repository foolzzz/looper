"""Confirmed one-off development-request recognition; never advances a subscription cursor."""
from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import asyncpg

from ..db import connection, schedules as db_schedules, subscriptions as db_subs
from ..db.intents import IntentRefused
from . import cron, extraction, intents, leads_model, model_call, processor, reminders, subscriptions
from .agent_settings import AgentType
from .ops import ErrorCode, OpError
from .supervisor import cancel_tasks

RECOGNIZE_MAX_EVENTS = 2000
RECOGNIZE_MAX_USD = 1.0
RECOGNIZE_DEFAULT_DAYS = 7
RECOGNIZE_MAX_DAYS = 30
RESULT_LINES = 20
logger = logging.getLogger(__name__)
_active: IntentRecognitions | None = None

_RANGE = """FROM source_events e JOIN source_chats c ON c.source_id=e.source_id AND c.chat_id=e.chat_id
    WHERE e.source_id=$1 AND e.chat_id=$2 AND e.sender_kind='user' AND e.origin IN ('live','history')
      AND e.sent_at >= $3 AND e.sent_at < $4 AND c.inclusion='included' AND c.extraction='included'"""


def _time(value: datetime | str | None, default: datetime) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value or default
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("recognition times must include a timezone")
    return result


async def _enabled(db: Any) -> None:
    if not await leads_model.intents_enabled(db) or not await leads_model.on_request_enabled(db):
        raise IntentRefused("turn on intents and on-request recognition first", code="conflict")


async def _schedule(db: Any, source_id: str) -> dict:
    rows = await db.fetch("SELECT schedule_id FROM subscriptions WHERE source_id=$1 AND output_kind='intents'",
                           source_id)
    for row in rows:
        schedule = await db_schedules.get_schedule(row["schedule_id"], pool=db)
        sub = schedule["subscription"]
        if (sub["target_confirmed_at"] is not None
                and sub["target_confirmed_version"] == await subscriptions.config_version(schedule)):
            return schedule
    raise IntentRefused("confirm an intents subscription for this source first", code="conflict")


async def preview(*, source_id: str | None = None, chat_id: str, since: datetime | str | None = None,
                  until: datetime | str | None = None, db: Any = None, now: datetime | None = None) -> dict:
    db, now = intents._db(db), now or datetime.now(timezone.utc)
    await _enabled(db)
    sid = await intents._source_id(source_id, db)
    chat = await intents._followable(db, sid, chat_id)
    end = _time(until, now)
    start = _time(since, end - timedelta(days=RECOGNIZE_DEFAULT_DAYS))
    if end > now or start >= end or end - start > timedelta(days=RECOGNIZE_MAX_DAYS):
        raise ValueError("choose a past range of at most 30 days, with since before until")
    schedule = await _schedule(db, sid)
    n = await db.fetchval(f"SELECT count(*) {_RANGE}", sid, chat_id, start, end)
    effective = start
    if n > RECOGNIZE_MAX_EVENTS:
        effective = await db.fetchval(
            f"SELECT e.sent_at {_RANGE} ORDER BY e.sent_at DESC,e.seq DESC OFFSET $5 LIMIT 1",
            sid, chat_id, start, end, RECOGNIZE_MAX_EVENTS - 1)
    low, high = extraction.estimate(min(n, RECOGNIZE_MAX_EVENTS), max(1, math.ceil(min(n, RECOGNIZE_MAX_EVENTS) / 500)))
    return {"chat": {"chat_id": chat_id, "name": chat["name"]}, "range_from": start.isoformat(),
            "range_to": end.isoformat(), "effective_from": effective.isoformat(), "events": n,
            "truncated": "messages" if n > RECOGNIZE_MAX_EVENTS else None,
            "estimate_usd": [round(min(low, RECOGNIZE_MAX_USD), 4), round(min(high, RECOGNIZE_MAX_USD), 4)],
            "schedule_id": schedule["id"]}


def _id(value: str | int) -> int:
    raw = str(value).removeprefix("R-").removeprefix("r-")
    if not raw.isascii() or not raw.isdecimal() or not 0 < int(raw) < 2**63:
        raise ValueError("recognition must be R- followed by its number")
    return int(raw)


async def _view(row: Mapping, db: Any) -> dict:
    found = await db.fetch(
        "SELECT i.id,i.type_key,i.title,i.state,v.name FROM intents i JOIN intent_type_versions v "
        "ON v.type_key=i.type_key AND v.version=i.type_version WHERE i.recognition_id=$1 ORDER BY i.id", row["id"])
    name = await db.fetchval("SELECT name FROM source_chats WHERE source_id=$1 AND chat_id=$2",
                             row["source_id"], row["chat_id"])
    return {"id": f"R-{row['id']}", "source_id": row["source_id"], "chat_id": row["chat_id"],
            "name": subscriptions.strip_mentions(name or row["chat_id"]),
            **{key: row[key] for key in ("state", "events_in_range", "events_read", "truncated", "cost_usd")},
            **{key: intents._iso(row[key]) for key in ("range_from", "range_to", "created_at", "finished_at")},
            "error": subscriptions.strip_mentions(row["error"]) if row["error"] else None,
            "intents": [{"id": leads_model.format_intent_id(i["id"]), "type": {"key": i["type_key"],
                "name": subscriptions.strip_mentions(i["name"])}, "title": subscriptions.strip_mentions(i["title"]),
                "state": i["state"]} for i in found]}


async def status(recognition_id: str | int, *, db: Any = None) -> dict:
    db = intents._db(db)
    row = await db.fetchrow("SELECT * FROM intent_recognitions WHERE id=$1", _id(recognition_id))
    if row is None:
        raise IntentRefused("unknown recognition", code="not_found")
    return {"recognition": await _view(row, db)}


async def list_recognitions(*, source_id: str | None = None, limit: int = 20, db: Any = None) -> dict:
    db = intents._db(db)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    sid = await intents._source_id(source_id, db)
    rows = await db.fetch("SELECT * FROM intent_recognitions WHERE source_id=$1 ORDER BY id DESC LIMIT $2", sid, limit)
    return {"enabled": await leads_model.on_request_enabled(db), "recognitions": [await _view(row, db) for row in rows]}


async def settings_put(enabled: bool, *, source_id: str | None = None, db: Any = None) -> dict:
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    db = intents._db(db)
    sid = await intents._source_id(source_id, db)
    await db.execute("INSERT INTO system_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2",
                     leads_model.INTENTS_ON_REQUEST_KEY, str(enabled).lower())
    logger.info("on-request intent recognition enabled=%s", enabled)
    return await list_recognitions(source_id=sid, db=db)


def result_text(row: Mapping, found: list[dict], zone: str) -> str:
    tz = ZoneInfo(zone)
    start, end = (_time(row[k], datetime.now(timezone.utc)).astimezone(tz) for k in ("range_from", "range_to"))
    rid = row["id"] if str(row["id"]).startswith("R-") else f"R-{row['id']}"
    complete = "识别完成" if row["state"] == "done" else "识别未完成"
    lines = [f"🔎 {rid} {complete}：《{row['name']}》{start:%m-%d %H:%M} – {end:%m-%d %H:%M}，"
             f"读了 {row['events_read']} 条人发的消息，识别出 {len(found)} 条开发需求"]
    lines.extend(f"{item['id']} · {item['type']['name']} · {item['title']}" for item in found[:RESULT_LINES])
    if len(found) > RESULT_LINES:
        lines.append(f"其余 {len(found) - RESULT_LINES} 条见意图页")
    if not found:
        lines.append("没有识别出开发需求")
    if row.get("truncated"):
        counts = row.get("counts") or {}
        if isinstance(counts, str):
            counts = json.loads(counts)
        reason = "只取最新 2,000 条" if row["truncated"] == "messages" else "达到 $1 花费上限"
        lines.append(f"已截断：{reason}；实际覆盖 {counts.get('effective_from', row['range_from'])} – "
                     f"{counts.get('covered_to', row['range_from'])}")
    if row.get("error"):
        lines.append(f"原因：{row['error']}")
    if found:
        iid = found[0]["id"]
        lines.append(f"回复「建 {iid}」建任务，「驳回 {iid}」驳回。")
    return subscriptions.strip_mentions("\n".join(lines))


class IntentRecognitions:
    def __init__(self, registry: Any, *, runtime_root: Path, claude_bin: str = "claude", drain: Any = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._registry, self._runtime_root, self._claude_bin = registry, runtime_root, claude_bin
        self._drain, self._clock = drain, clock or (lambda: datetime.now(timezone.utc))
        self._tasks: set[asyncio.Task] = set()
        self._running: dict[asyncio.Task, int] = {}
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        await connection.get_pool().execute("UPDATE intent_recognitions SET state='failed',finished_at=$1,"
            "error='interrupted by a restart; run it again' WHERE state='running'", self._clock())

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._tasks)
        ids = list(self._running.values())
        await cancel_tasks(tasks)
        if ids:
            await connection.get_pool().execute("UPDATE intent_recognitions SET state='failed',finished_at=$2,"
                "error='interrupted by a restart; run it again' WHERE id=ANY($1::bigint[]) AND state='running'",
                ids, self._clock())

    async def begin(self, *, source_id: str | None = None, chat_id: str, since: datetime | str | None = None,
                    until: datetime | str | None = None, requested_via: str, reply: tuple[str, str] | None = None) -> dict:
        if self._stopping or self._drain is not None and self._drain.draining:
            raise OpError(ErrorCode.UNAVAILABLE, "recognition is stopping; try again after restart")
        if requested_via not in ("manager", "dashboard"):
            raise ValueError("invalid requested_via")
        pool = connection.get_pool()
        sid = await intents._source_id(source_id, pool)
        info = await preview(source_id=sid, chat_id=chat_id, since=since, until=until, db=pool, now=self._clock())
        try:
            row = await pool.fetchrow(
                "INSERT INTO intent_recognitions(source_id,chat_id,schedule_id,range_from,range_to,requested_via,"
                "reply_channel,reply_chat,events_in_range,truncated,counts) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *",
                sid, chat_id, info["schedule_id"], datetime.fromisoformat(info["range_from"]),
                datetime.fromisoformat(info["range_to"]), requested_via, reply[0] if reply else None,
                reply[1] if reply else None, info["events"], info["truncated"],
                json.dumps({"effective_from": info["effective_from"]}))
        except asyncpg.UniqueViolationError:
            raise IntentRefused("a recognition is already running for this source", code="conflict") from None
        view = await _view(row, pool)
        task = asyncio.create_task(self._run(row["id"]), name=f"intent-recognition-{row['id']}")
        self._tasks.add(task)
        self._running[task] = row["id"]
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda done: self._running.pop(done, None))
        logger.info("intent recognition R-%s started source=%s events=%s", row["id"], sid, info["events"])
        return {"recognition": view}

    async def _run(self, rid: int) -> None:
        pool = connection.get_pool()
        row = await pool.fetchrow("SELECT * FROM intent_recognitions WHERE id=$1", rid)
        zone, error = cron.local_zone_name(), None
        try:
            schedule = await db_schedules.get_schedule(row["schedule_id"], pool=pool)
            if schedule is None:
                raise IntentRefused("the confirmed subscription was deleted")
            sub, zone = schedule["subscription"], schedule["timezone"]
            snapshot = {"subscription": sub, "config_version": sub["target_confirmed_version"],
                        "confirmed_at": sub["target_confirmed_at"].isoformat(), "config_epoch": sub["config_epoch"]}
            # Capture exact membership once: time ties and history arriving during the run cannot exceed the cap.
            chosen = await pool.fetch(f"SELECT {db_subs._WINDOW_COLUMNS},true AS via_group {_RANGE} "
                "ORDER BY e.sent_at DESC,e.seq DESC LIMIT $5", row["source_id"], row["chat_id"], row["range_from"],
                row["range_to"], RECOGNIZE_MAX_EVENTS)
            floor = min((r["sent_at"] for r in chosen), default=row["range_from"])
            remaining = sorted([{**dict(r), "read_from": floor} for r in chosen], key=lambda r: r["seq"])
            allowed = {r["seq"] for r in remaining}
            types = await extraction.intent_types(pool, rules_on=False)
            valid_days = await leads_model.valid_days(pool)
            totals, dropped, cost, read = Counter(), Counter(), 0.0, 0
            counts = {"effective_from": floor.isoformat()}
            while remaining:
                if self._stopping or self._drain is not None and self._drain.draining:
                    raise IntentRefused("recognition stopped for restart; run it again")
                await _enabled(pool)
                if problem := await extraction.confirmation_problem(pool, schedule["id"], snapshot):
                    raise IntentRefused(problem)
                await intents._followable(pool, row["source_id"], row["chat_id"])
                picked = {"rows": remaining[:500], "last_seq": remaining[min(len(remaining), 500)-1]["seq"],
                          "more": len(remaining) > 500}
                found = await extraction.batch(pool, "intents", sub, picked, zone_name=zone, now=self._clock(),
                    own_apps=(), to_seq=max(allowed), context_seqs=allowed)
                reference = {"kind": "intent_recognition", "recognition_id": rid, "events": len(found.rows)}

                async def run(reminder: str, budget_usd: float, timeout_s: float) -> processor.ProcessorRun:
                    return model_call.processor_result(await model_call.call(model_call.ProcessorCall(
                        owner=f"intent-recognition-{rid}", instructions=extraction.instructions("intents",
                            schedule["requirement"], types, group=True, rules=False) + (f"\n\n{reminder}" if reminder else ""),
                        input_text=found.text, input_ref=reference, quiet_word=extraction.QUIET_WORD,
                        budget_usd=budget_usd, timeout_s=timeout_s, runtime_root=self._runtime_root,
                        claude_bin=self._claude_bin, project_id=schedule["project_id"], schedule_id=schedule["id"],
                        contract=extraction.CONTRACTS["intents"], role=AgentType.PROCESSOR), parse=str))

                timeout = schedule["run_timeout_seconds"] if schedule["run_timeout_mode"] == "seconds" else processor.PROCESSOR_TIMEOUT_SEC
                asked = await extraction.ask(run, lambda reply: extraction.parse("intents", reply, found.refs,
                    now=self._clock().astimezone(ZoneInfo(zone)), valid_days=valid_days,
                    enabled_types={t["key"] for t in types}, app_seqs=found.app_seqs,
                    group_seqs={r["seq"] for r in found.rows}, group_types=leads_model.GROUP_INTENT_TYPES),
                    budget_usd=max(0, RECOGNIZE_MAX_USD-cost), timeout_s=float(timeout))
                cost += asked.cost_usd or 0.0
                await pool.execute("UPDATE intent_recognitions SET cost_usd=$2 WHERE id=$1 AND state='running'", rid, cost)
                if asked.cost_usd is None:
                    raise IntentRefused("processor cost is unknown; stopped to preserve the run budget")
                async with connection.transaction() as conn:
                    live = await conn.fetchrow("SELECT state FROM intent_recognitions WHERE id=$1 FOR UPDATE", rid)
                    if live is None or live["state"] != "running":
                        return
                    if asked.parsed is not None and asked.skipped is None:
                        await conn.execute("SELECT 1 FROM schedules WHERE id=$1 FOR NO KEY UPDATE", schedule["id"])
                        await _enabled(conn)
                        if problem := await extraction.confirmation_problem(conn, schedule["id"], snapshot,
                                                                            profile=asked.result.profile):
                            raise IntentRefused(problem)
                        batch_counts = await extraction.store_intents(conn, asked.parsed, source_id=row["source_id"],
                            schedule_id=schedule["id"], run_id=None, record_id=asked.result.record_id, now=self._clock(),
                            more=found.more, via=leads_model.IntentVia.ON_REQUEST, recognition_id=rid)
                        dropped.update(batch_counts.pop("dropped", {}))
                        totals.update({k: v for k, v in batch_counts.items() if type(v) is int})
                        read += len(found.rows)
                        counts = {**totals, "dropped": dict(dropped), "effective_from": floor.isoformat(),
                                  "covered_to": max(r["sent_at"] for r in chosen if r["seq"] <= found.last_seq).isoformat()}
                        await conn.execute("UPDATE intent_recognitions SET events_read=$2,counts=$3 WHERE id=$1",
                                           rid, read, json.dumps(counts))
                if asked.skipped is not None or asked.parsed is None:
                    raise IntentRefused(str(asked.skipped or asked.result.failure) + ": " + (asked.result.detail or "processor skipped the window"))
                remaining = [r for r in remaining if r["seq"] > found.last_seq]
                if cost >= RECOGNIZE_MAX_USD and remaining:
                    await pool.execute("UPDATE intent_recognitions SET truncated='budget' WHERE id=$1", rid)
                    break
        except asyncio.CancelledError:
            error = "interrupted by a restart; run it again"
        except Exception as exc:
            # Persist a bounded operator-facing reason; never copy model input or raw runtime logs.
            error = str(exc) if isinstance(exc, (IntentRefused, ValueError)) else "recognition failed; check the server log"
            logger.warning("intent recognition R-%s failed (%s)", rid, type(exc).__name__)
        await pool.execute("UPDATE intent_recognitions SET state=$2,error=$3,finished_at=$4 WHERE id=$1 AND state='running'",
                           rid, "failed" if error else "done", error, self._clock())
        row = await pool.fetchrow("SELECT * FROM intent_recognitions WHERE id=$1", rid)
        if row is None:
            return
        logger.info("intent recognition R-%s ended state=%s events=%s cost=%s", rid, row["state"], row["events_read"], row["cost_usd"])
        if row["reply_chat"] and not self._stopping:
            view = await _view(row, pool)
            try:
                await self._registry.post(row["reply_channel"], reminders.target(row["reply_chat"]),
                                          result_text({**view, "counts": row["counts"]}, view["intents"], zone))
            except Exception:
                logger.warning("intent recognition R-%s result delivery failed", rid)


def install(instance: IntentRecognitions) -> None:
    global _active
    _active = instance


def service() -> IntentRecognitions:
    if _active is None:
        raise OpError(ErrorCode.UNAVAILABLE, "recognition runs only in looper serve")
    return _active
