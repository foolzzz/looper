"""``looper msgflow``: one-off work on the message stream that does not belong in the server.

``import`` reads an outside collector's per-chat files from their start and stores the last ``--days`` days of them
(``--dry-run`` stores nothing and reports what it would). It takes the source's writer lock; while the server is
in a pass it says so and exits.

``leads-backfill`` turns the history before a leads subscription started into leads (message-leads-intents RFC,
decision D-②). By default it only counts: the in-scope events of the last ``--days`` days up to where the
subscription started, by chat kind and day, its windows and what they would cost. ``--run --budget-usd X`` runs
them oldest first through the subscription's own rendering, processor, parsing and storing (leads marked
``backfill``), keeps its progress after each window (``leads.backfill.<schedule id>``) and stops before a window
would pass the budget; run again, it resumes. A window a prompt hook blocks, or whose reply cannot be read when asked
twice (``extraction.ask``), is skipped, listed with the progress, and the backfill goes on; any other processor
failure stops it there.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

IMPORT_MAX_DAYS = 3650
OK, USAGE, TRIAGE_OFF, NOT_FOUND, NO_DATABASE = 0, 2, 3, 5, 6


def format_report(report: Any, *, days: int, dry_run: bool, per_file: bool = False) -> str:
    verb = "would store" if dry_run else "stored"
    files = [f for f in report.file_reports if not f.baselined]
    total = {name: sum(getattr(f, name) for f in files) for name in
             ("lines", "distinct", "new", "duplicates", "skipped", "skipped_by_deletion", "bad_lines", "rescans")}
    unreconciled = [f for f in files if not f.reconciled]
    lines = [
        f"{'dry run: ' if dry_run else ''}import of the last {days} day{'s' if days != 1 else ''}: {report.status}",
        f"  files: {report.files} read, {report.files_failed} failed, {total['rescans']} read again, "
        f"{sum(f.incomplete_tail for f in files)} with an unterminated last line",
        f"  messages read: {total['lines']} ({total['distinct']} distinct ids)",
        f"  {verb} {total['new']}, already stored or repeated {total['duplicates']}, skipped {total['skipped']} "
        "(a Looper bot's own, an excluded chat, older than the days asked), "
        f"skipped by deletion {total['skipped_by_deletion']}",
        f"  reconciliation: {'every file adds up (read = new + already stored + skipped + skipped by deletion)' if not unreconciled else str(len(unreconciled)) + ' files do not add up: ' + ', '.join(f.chat_id for f in unreconciled[:10])}",
    ]
    if total["bad_lines"]:
        lines.append(f"  lines that were not a message: {total['bad_lines']}")
    if per_file:
        lines += [f"  {f.chat_id}: read {f.lines}, distinct {f.distinct}, new {f.new}, already stored {f.duplicates}, "
                  f"skipped {f.skipped}, skipped by deletion {f.skipped_by_deletion}, bad {f.bad_lines}, rescans {f.rescans}"
                  f"{', unterminated tail' if f.incomplete_tail else ''}" for f in files]
    if report.error:
        lines.append(f"  error: {report.error}")
    return "\n".join(lines)


async def _import(database_url: str, *, source_id: str | None, days: int, dry_run: bool,
                  per_file: bool = False) -> tuple[int, str]:
    from ..db import message_stream as store
    from ..db.connection import close_pool, init_pool
    from .source_service import own_bot_app_ids, run_bridge_pass

    pool = await init_pool(database_url, min_size=1, max_size=3)
    try:
        if not await pool.fetchval("SELECT to_regclass('event_sources') IS NOT NULL"):
            return 1, "the database has no message stream tables yet: start `looper serve` once to upgrade it"
        if not await pool.fetchval(
            "SELECT to_regclass('source_deletions') IS NOT NULL AND EXISTS (SELECT 1 FROM pg_attribute "
            "WHERE attrelid = to_regclass('event_sources') AND attname = 'retention_last_at' AND NOT attisdropped) "
            "AND EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('source_runs') "
            "AND attname = 'skipped_by_deletion' AND NOT attisdropped)"
        ):
            return 1, "the database has no deletion support yet: start `looper serve` once to upgrade it"
        sources = await store.list_sources()
        found = [s for s in sources if source_id in (None, s["id"])]
        if source_id is None and len(sources) > 1:
            return 1, f"there are {len(sources)} sources ({', '.join(s['id'] for s in sources)}); name one with --source"
        if not found:
            return 1, "no message source is set up: create it from the dashboard's Message stream page"
        source = found[0]
        if not source["bridge_dir"]:
            return 1, f"source {source['id']} has no bridge directory to import from"
        report = await run_bridge_pass(source, days=days, dry_run=dry_run, own_apps=await own_bot_app_ids())
        if report.status == "skipped_busy":
            return 1, f"source {source['id']} is being written by another pass; try again in a moment"
        return (0 if report.status in ("ok", "partial") else 1), format_report(report, days=days, dry_run=dry_run, per_file=per_file)
    finally:
        await close_pool()


def import_history(database_url: str, *, source_id: str | None, days: int, dry_run: bool,
                   per_file: bool = False) -> int:
    code, text = asyncio.run(_import(database_url, source_id=source_id, days=days, dry_run=dry_run,
                                     per_file=per_file))
    print(text, file=sys.stderr if code else sys.stdout)
    return code


async def _delete_message(database_url, *, source_id, message_id, runtime_root, dry_run=False):
    from ..db import message_stream as store
    from ..db.connection import close_pool, init_pool
    from .ops import OpError
    from .v1_domains.message_stream import preview_deletion, confirm_deletion, deletion_target

    pool = await init_pool(database_url, min_size=1, max_size=3)
    try:
        if not await pool.fetchval("SELECT to_regclass('source_deletions') IS NOT NULL"):
            print("the database has no deletion support yet: start `looper serve` once to upgrade it", file=sys.stderr)
            return 1
        sources = await store.list_sources()
        found = [source for source in sources if source_id in (None, source["id"])]
        if len(found) != 1:
            print("name an existing, unambiguous message source with --source", file=sys.stderr)
            return 1
        target = deletion_target({"scope": "message", "message_id": message_id})
        confirmations = {}
        kwargs = {"runtime_root": runtime_root, "confirmations": confirmations, "owner": "cli", "db": pool}
        preview = await preview_deletion(found[0]["id"], target, **kwargs)
        backups = preview["backups"]
        print(f"Delete message {message_id}: {preview['events']} stored event(s).")
        for limit in preview["limits"]:
            print(f"  {limit}")
        print(f"  Upgrade backups: {backups['count']}; earliest: {backups['earliest_at'] or 'none'}")
        if dry_run:
            return 0
        try:
            answer = await asyncio.to_thread(input, "Type delete to permanently delete this message: ")
        except (EOFError, OSError):
            answer = ""
        if answer.strip() != "delete":
            print("Cancelled; no message was deleted.")
            return 1
        result = await confirm_deletion(found[0]["id"], target, preview["confirmation_token"], **kwargs)
        print(f"Deleted {result['deleted']} stored event(s); the tombstone prevents reimport.")
        return 0
    except OpError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        await close_pool()


BACKFILL_MAX_DAYS = 365
BACKFILL_WINDOW = 500
BACKFILL_KEY = "leads.backfill.{}"


async def _leads_schedule(pool: Any, schedule_id: str | None) -> tuple[dict[str, Any] | None, str]:
    rows = await pool.fetch("SELECT s.id, s.name FROM schedules s JOIN subscriptions u ON u.schedule_id = s.id "
                            "WHERE u.output_kind = 'leads' ORDER BY s.created_at, s.id")
    if schedule_id is not None:
        rows = [row for row in rows if row["id"] == schedule_id]
        if not rows:
            return None, f"no leads subscription {schedule_id!r}"
    if not rows:
        return None, "there is no leads subscription: create one on the dashboard, from its preset"
    if len(rows) > 1:
        return None, (f"there are {len(rows)} leads subscriptions ({', '.join(row['id'] for row in rows)}); "
                      "name one with --schedule")
    from ..db import schedules as db_schedules

    return await db_schedules.get_schedule(rows[0]["id"]), ""


def _usd(value: float) -> str:
    return f"${value:.3f}" if value < 0.1 else f"${value:.2f}" if value < 10 else f"${value:.1f}"


async def _backfill(database_url: str, *, days: int, schedule_id: str | None, run: bool, budget_usd: float | None,
                    claude_bin: str, runtime_root: Any) -> tuple[int, str]:
    import json
    import math
    from collections import Counter
    from datetime import datetime, timedelta, timezone

    from ..db import connection, subscriptions as db_subs, system_settings
    from . import extraction, leads_model, model_call, processor
    from .agent_settings import AgentType
    from .source_service import own_bot_app_ids

    pool = await connection.init_pool(database_url, min_size=1, max_size=4)
    holder = None
    try:
        if not await pool.fetchval("SELECT to_regclass('message_leads') IS NOT NULL"):
            return 1, "the database has no message lead tables yet: start `looper serve` once to upgrade it"
        schedule, problem = await _leads_schedule(pool, schedule_id)
        if schedule is None:
            return 1, problem
        sid, sub, zone_name = schedule["id"], schedule["subscription"], schedule["timezone"]
        if run:
            # One runner per subscription, from before its progress is read until it ends (a session lock, released
            # when its connection closes even if this process dies).
            holder = await pool.acquire()
            if not await holder.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", backfill_lock(sid)):
                return 1, (f"another `looper msgflow leads-backfill --run` of {sid} is running; wait for it to end, "
                           "then run this again to resume")
        earliest = await pool.fetchval(
            "SELECT sr.from_seq FROM subscription_runs sr JOIN schedule_runs r ON r.id = sr.run_id "
            "WHERE r.schedule_id = $1 ORDER BY r.created_at, r.id LIMIT 1", sid)
        start = earliest if earliest is not None else sub["cursor_seq"]
        key = BACKFILL_KEY.format(sid)
        stored = (await system_settings.get_settings([key], pool=pool)).get(key)
        progress = {"done_seq": 0, "spent_usd": 0.0, "windows": 0, **(json.loads(stored) if stored else {})}
        now = datetime.now(timezone.utc)
        scope = {**sub, "max_age_minutes": days * 1440, "max_events": BACKFILL_WINDOW}
        own_apps = await own_bot_app_ids()
        tally = await db_subs.extraction_tally(pool, scope, output_kind="leads", since=now - timedelta(days=days),
                                               to_seq=start, own_apps=own_apps, include_import=True,
                                               zone_name=zone_name, from_seq=progress["done_seq"])
        count = sum(row["events"] for row in tally)
        by_kind: dict[str, int] = {}
        by_day: dict[Any, int] = {}
        for row in tally:
            by_kind[row["chat_kind"]] = by_kind.get(row["chat_kind"], 0) + row["events"]
            by_day[row["day"]] = by_day.get(row["day"], 0) + row["events"]
        low, high = extraction.estimate(count, math.ceil(count / BACKFILL_WINDOW))
        lines = [f"{'' if run else 'dry run: '}leads backfill of {schedule['name']} ({sid}), the last {days} days "
                 f"up to seq {start}" + (f", resuming after seq {progress['done_seq']} ({progress['windows']} windows, "
                                        f"{_usd(progress['spent_usd'])} spent)" if progress["done_seq"] else ""),
                 f"  in-scope events: {count:,} ("
                 + (", ".join(f"{kind} {n:,}" for kind, n in sorted(by_kind.items())) or "none") + ")",
                 f"  windows: {math.ceil(count / BACKFILL_WINDOW)} of at most {BACKFILL_WINDOW} events",
                 f"  estimate: {_usd(low)}–{_usd(high)} (measured on {extraction.COST_MEASURED_ON}: "
                 f"{extraction.COST_PER_RUN_USD[0]:g}–{extraction.COST_PER_RUN_USD[1]:g} USD per window and "
                 f"{extraction.COST_PER_EVENT_USD:g} USD per event)"]
        measured = await pool.fetchrow(
            "SELECT sum(r.cost_usd) AS cost, sum(sr.events_selected) AS events FROM schedule_runs r "
            "JOIN subscription_runs sr ON sr.run_id = r.id WHERE r.schedule_id = $1 AND r.status = 'completed' "
            "AND r.cost_usd IS NOT NULL AND sr.events_selected > 0", sid)
        if measured["events"]:
            mean = float(measured["cost"]) / measured["events"]
            lines.append(f"  estimate: {_usd(count * mean)} (实测均价 {mean:.5f} USD per event over "
                         f"{measured['events']:,} events of its runs)")
        lines += [f"  {day}: {n:,}" for day, n in sorted(by_day.items())]
        if not run:
            return 0, "\n".join(lines)
        assert budget_usd is not None
        totals = {"new": 0, "merged": 0, "superseded": 0, "dropped": 0}
        # ``budget_usd`` bounds this run of the command; ``total`` is every run's, kept with the progress.
        done, total, windows = progress["done_seq"], float(progress["spent_usd"]), progress["windows"]
        # Every run's skipped windows, kept with the progress: their seqs, events and why (``extraction.SkipReason``).
        skipped: list[dict[str, Any]] = list(progress.get("skipped") or [])
        spent, events, valid_days = 0.0, 0, await leads_model.valid_days(pool)
        stopped = ""
        while True:
            picked = await db_subs.extraction_window(pool, scope, output_kind="leads", from_seq=done, to_seq=start,
                                                     now=now, own_apps=own_apps, include_import=True)
            if not picked["rows"]:
                done = start
                break
            # The next window's cost, by the running mean of this backfill's (the measured top rate before any).
            estimate = (len(picked["rows"]) * spent / events if events
                        else extraction.estimate(len(picked["rows"]), 1)[1])
            if spent + estimate > budget_usd:
                stopped = f"stopped before a window of {len(picked['rows'])} events would pass the budget"
                break
            # The estimate only decides whether to start; the processor's own cap is what is left of the budget.
            cap = min(schedule["run_budget_usd"], budget_usd - spent)
            found = await extraction.batch(pool, "leads", scope, picked, zone_name=zone_name, now=now,
                                           own_apps=own_apps, to_seq=start,
                                           known_since=max(row["sent_at"] for row in picked["rows"]))
            reference = {"kind": "leads_backfill", "schedule_id": sid, "from_seq": done, "to_seq": found.last_seq,
                         "events": len(found.rows), "context": found.context, "known": found.known}

            async def ask(reminder: str, budget: float, timeout_s: float) -> processor.ProcessorRun:
                return model_call.processor_result(await model_call.call(model_call.ProcessorCall(
                    owner=f"{sid}:backfill",
                    instructions=schedule["requirement"] + (f"\n\n{reminder}" if reminder else ""),
                    input_text=found.text, input_ref=reference, quiet_word=extraction.QUIET_WORD, budget_usd=budget,
                    timeout_s=timeout_s, runtime_root=runtime_root, claude_bin=claude_bin,
                    project_id=schedule["project_id"], schedule_id=sid, contract=extraction.LEADS_CONTRACT,
                    metadata={"backfill": {**reference, "reminded": bool(reminder)}},
                    role=AgentType.PROCESSOR), parse=str))

            asked = await extraction.ask(
                ask, lambda reply: extraction.parse(
                    "leads", reply, found.refs, now=now.astimezone(extraction._zone(zone_name)),
                    valid_days=valid_days, enabled_types=(), app_seqs=found.app_seqs),
                budget_usd=cap, timeout_s=float(processor.PROCESSOR_TIMEOUT_SEC))
            spent += asked.cost_usd or 0.0
            total += asked.cost_usd or 0.0
            if asked.parsed is None and asked.skipped is None:
                stopped = f"the processor gave no reply ({asked.result.failure.value}): {asked.result.detail}"
            async with connection.transaction() as conn:
                if asked.skipped is not None:  # never the whole backfill held on one window: on to the next
                    skipped.append({"from_seq": done, "to_seq": found.last_seq, "events": len(found.rows),
                                    "reason": asked.skipped.value})
                elif not stopped:
                    counts = await extraction.store_leads(conn, asked.parsed, source_id=sub["source_id"], run_id=None,
                                                          record_id=asked.result.record_id, backfill=True,
                                                          more=found.more)
                    for name in ("new", "merged", "superseded"):
                        totals[name] += counts[name]
                    totals["dropped"] += sum(counts["dropped"].values())
                if not stopped:
                    done, windows, events = found.last_seq, windows + 1, events + len(found.rows)
                await conn.execute(
                    "INSERT INTO system_settings (key, value) VALUES ($1, $2) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", key,
                    json.dumps({"done_seq": done, "spent_usd": round(total, 6), "windows": windows,
                                "skipped": skipped}))
            if stopped or not found.more and done >= start:
                break
        lines.append(f"ran {windows - progress['windows']} windows: {totals['new']} new leads, {totals['merged']} "
                     f"merged, {totals['superseded']} superseded, {totals['dropped']} dropped; spent {_usd(spent)} of "
                     f"{_usd(budget_usd)} ({_usd(total)} in all); done up to seq {done} of {start}")
        if new_skips := skipped[len(progress.get("skipped") or []):]:
            reasons = Counter(skip["reason"] for skip in new_skips)
            lines.append(f"  skipped {len(new_skips)} windows ("
                         + ", ".join(f"{reason} {n}" for reason, n in sorted(reasons.items()))
                         + f"), {sum(skip['events'] for skip in new_skips):,} events: nothing stored from them; "
                         f"their seqs are listed with the progress ({key})")
        if stopped:
            lines.append(f"  {stopped}; run it again to resume")
        return (1 if stopped and "budget" not in stopped else 0), "\n".join(lines)
    finally:
        if holder is not None:
            await holder.execute("SELECT pg_advisory_unlock_all()")
            await pool.release(holder)
        await connection.close_pool()


def backfill_lock(schedule_id: str) -> str:
    """The advisory lock's key (``hashtextextended``) that one backfill runner of a subscription holds."""
    return f"looper.leads.backfill:{schedule_id}"


def leads_backfill(args: Any, load_ops: Any) -> int:
    if not 1 <= args.days <= BACKFILL_MAX_DAYS:
        print(f"error: --days is a whole number from 1 to {BACKFILL_MAX_DAYS}", file=sys.stderr)
        return 2
    if args.run and not (args.budget_usd and args.budget_usd > 0):
        print("error: --run needs --budget-usd: the most the backfill may spend", file=sys.stderr)
        return 2
    ops = load_ops(args)
    if ops is None:
        return 1
    code, text = asyncio.run(_backfill(ops.database_url, days=args.days, schedule_id=args.schedule, run=args.run,
                                       budget_usd=args.budget_usd, claude_bin=args.claude or "claude",
                                       runtime_root=ops.runtime_root))
    print(text, file=sys.stderr if code else sys.stdout)
    return code


async def _why(database_url: str, ref: str, *, as_json: bool) -> tuple[int, str]:
    import asyncpg

    from ..db.connection import close_pool, init_pool
    from .triage_why import (
        AmbiguousMessage, Case, MessageNotFound, UnusableRef, format_lines, why,
    )

    try:
        pool = await init_pool(database_url, min_size=1, max_size=2)
    except (OSError, asyncpg.PostgresError, TimeoutError, ValueError) as error:
        return NO_DATABASE, f"cannot reach the database: {error}"
    try:
        if not await pool.fetchval("SELECT to_regclass('source_events') IS NOT NULL"):
            return NO_DATABASE, "the database has no message stream tables yet: start `looper serve` once to upgrade it"
        try:
            answer = await why(pool, ref, now=datetime.now(timezone.utc))
        except UnusableRef:
            return USAGE, "消息引用无法识别：需要飞书消息 ID、消息链接或数字 seq"
        except MessageNotFound:
            return NOT_FOUND, "没有这条消息"
        except AmbiguousMessage:
            return USAGE, "这个消息 ID 存在于多个来源，请改用 seq"
        text = (json.dumps(answer.as_dict(), ensure_ascii=False, indent=2) if as_json
                else "\n".join(format_lines(answer)))
        return (TRIAGE_OFF if answer.case is Case.TRIAGE_OFF else OK), text
    except (OSError, asyncpg.PostgresError, TimeoutError) as error:
        return NO_DATABASE, f"the database failed: {error}"
    finally:
        await close_pool()


def why_message(args: Any, load_ops: Any) -> int:
    """Answer from the database directly, including while ``looper serve`` is stopped."""
    ops = load_ops(args)
    if ops is None:
        return NO_DATABASE
    code, text = asyncio.run(_why(ops.database_url, args.message, as_json=args.json))
    print(text, file=sys.stderr if code not in (OK, TRIAGE_OFF) else sys.stdout)
    return code


def run(args: Any, load_ops: Any) -> int:
    if args.msgflow_command == "why":
        return why_message(args, load_ops)
    if args.msgflow_command == "delete":
        ops = load_ops(args)
        if ops is None:
            return 1
        return asyncio.run(_delete_message(ops.database_url, source_id=args.source, message_id=args.message,
                                          runtime_root=ops.runtime_root, dry_run=args.dry_run))
    if getattr(args, "msgflow_command", "import") == "leads-backfill":
        return leads_backfill(args, load_ops)
    if not 1 <= args.days <= IMPORT_MAX_DAYS:
        print(f"error: --days is a whole number from 1 to {IMPORT_MAX_DAYS}", file=sys.stderr)
        return 2
    ops = load_ops(args)
    if ops is None:
        return 1
    return import_history(ops.database_url, source_id=args.source, days=args.days, dry_run=args.dry_run,
                          per_file=args.per_file)


__all__: Sequence[str] = ("run", "import_history", "leads_backfill")
