"""Short transactions for finished-task retrospectives, independent of vault writing."""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from ..agents.memory_lessons_model import LessonSettings, overwrite_allowed
from ..agents.memory_model import Action, Actor, Kind, SourceClass, State, Via, fingerprint, format_id
from .memory import _event, _in_transaction
from .memory_lessons import transition_lesson

FAILURES = frozenset({"timeout", "execution", "parse", "lease_expired", "input", "configuration", "unavailable", "apply", "runtime", "rate_limited", "budget", "refused", "blocked"})
_TERMINAL = ("completed", "failed")
_COUNTS = frozenset({"invalid", "no_evidence", "secret", "tombstoned", "reinforced", "in_effect", "held",
                     "external_source", "overwrite_gate", "shadow", "project_cap"})


async def _settings(conn):
    from ..agents.memory_lessons import settings_view
    from ..agents.memory_lessons_model import parse_settings
    # Keep an operator update from changing permissions halfway through this transaction.
    await conn.fetchrow("SELECT value FROM system_settings WHERE key='memory_lessons' FOR SHARE")
    view = await settings_view(conn)
    return parse_settings(view["settings"])


async def activate(conn, *, now: datetime) -> None:
    """Capture the first activation boundary; safe inside the settings transaction."""
    await conn.execute("INSERT INTO memory_retro_activation(id,enabled_at,journal_highwater) "
                       "SELECT TRUE,$1,COALESCE(max(id),0) FROM task_journal ON CONFLICT DO NOTHING", now)


async def bootstrap(conn, *, now: datetime, limit: int = 100) -> bool:
    """Advance historical terminal-task cursors in bounded batches without retro runs."""
    async with conn.transaction():
        await activate(conn, now=now)
        activation = await conn.fetchrow("SELECT * FROM memory_retro_activation WHERE id FOR UPDATE")
        if activation["bootstrap_done"]:
            return True
        rows = await conn.fetch("SELECT id FROM tasks WHERE id>$1 AND status=ANY($2::text[]) "
                                "AND COALESCE(completed_at,updated_at)<=$3 ORDER BY id LIMIT $4", activation["bootstrap_after"],
                                list(_TERMINAL), activation["enabled_at"], limit)
        for row in rows:
            high = await conn.fetchval("SELECT COALESCE(max(id),0) FROM task_journal WHERE task_id=$1 AND id<=$2",
                                      row["id"], activation["journal_highwater"])
            await conn.execute("INSERT INTO memory_retro_cursors(task_id,last_to_id) VALUES($1,$2) "
                               "ON CONFLICT(task_id) DO UPDATE SET last_to_id=GREATEST("
                               "memory_retro_cursors.last_to_id,EXCLUDED.last_to_id)", row["id"], high)
        done = len(rows) < limit
        await conn.execute("UPDATE memory_retro_activation SET bootstrap_after=$1,bootstrap_done=$2 WHERE id",
                           rows[-1]["id"] if rows else activation["bootstrap_after"], done)
        return done


async def enqueue_finished(conn, task_id: str, status: str, *, settings: LessonSettings,
                           now: datetime) -> dict[str, Any] | None:
    """Pin only newly observed journal rows after a published terminal status."""
    if status not in _TERMINAL:
        return None
    async with conn.transaction():
        settings = await _settings(conn)
        if not settings.enabled:
            return None
        task = await conn.fetchrow("SELECT * FROM tasks WHERE id=$1 FOR UPDATE", task_id)
        if task is None or task["status"] not in _TERMINAL:
            return None
        mode = await conn.fetchval("SELECT memory_lessons FROM projects WHERE id=$1", task["project_id"])
        if mode != "on":
            return None
        await activate(conn, now=now)
        activation = await conn.fetchrow("SELECT * FROM memory_retro_activation WHERE id")
        await conn.execute("INSERT INTO memory_retro_cursors(task_id,last_to_id) VALUES($1,0) ON CONFLICT DO NOTHING",
                           task_id)
        cursor = await conn.fetchval("SELECT last_to_id FROM memory_retro_cursors WHERE task_id=$1 FOR UPDATE", task_id)
        # A listener can race bootstrap. Its pre-activation rows are still historical.
        if (task["completed_at"] or task["updated_at"]) <= activation["enabled_at"]:
            cursor = max(cursor, activation["journal_highwater"])
        bounds = await conn.fetchrow("SELECT min(id) AS first,max(id) AS last,count(*) AS total "
                                     "FROM task_journal WHERE task_id=$1 AND id>$2", task_id, cursor)
        if not bounds["total"]:
            return None
        reason = "few_entries" if bounds["total"] < settings.min_journal_entries else None
        row = await conn.fetchrow("INSERT INTO memory_retro_runs(task_id,journal_from_id,journal_to_id,state,"
                                  "skip_reason,created_at,finished_at) VALUES($1,$2,$3,$4,$5,$6,$7) "
                                  "ON CONFLICT(task_id,journal_to_id) DO NOTHING RETURNING *", task_id,
                                  bounds["first"], bounds["last"], "skipped" if reason else "pending", reason,
                                  now, now if reason else None)
        await conn.execute("UPDATE memory_retro_cursors SET last_to_id=$2 WHERE task_id=$1", task_id, bounds["last"])
        return dict(row) if row else None


async def recover_finished(conn, *, settings: LessonSettings, now: datetime, limit: int = 100) -> int:
    """Repair publish/enqueue crashes after bootstrap, never sweep pre-activation history."""
    if not settings.enabled:
        return 0
    activation = await conn.fetchrow("SELECT * FROM memory_retro_activation WHERE id")
    if activation is None or not activation["bootstrap_done"]:
        return 0
    rows = await conn.fetch("SELECT t.id,t.status FROM tasks t JOIN projects p ON p.id=t.project_id "
                           "WHERE t.status=ANY($1::text[]) AND p.memory_lessons='on' "
                           "AND COALESCE(t.completed_at,t.updated_at)>$2 "
                           "AND EXISTS(SELECT 1 FROM task_journal j LEFT JOIN memory_retro_cursors c ON c.task_id=t.id "
                           "WHERE j.task_id=t.id AND j.id>COALESCE(c.last_to_id,0) "
                           " ) ORDER BY t.updated_at,t.id LIMIT $3", list(_TERMINAL),
                           activation["enabled_at"], limit)
    total = 0
    for task in rows:
        total += await enqueue_finished(conn, task["id"], task["status"], settings=settings, now=now) is not None
    return total


async def _finish_streak(conn, task_id: str, *, failed: bool, failure: str = "execution") -> None:
    if failed:
        await conn.execute("INSERT INTO activity(task_id,worker,kind,summary) "
                           "VALUES($1,'memory_retro','memory_retro',$2)", task_id,
                           "Lessons: retrospective failed (" + failure + ")")
        await conn.execute("UPDATE memory_retro_activation SET failure_tasks=CASE WHEN $1=ANY(failure_tasks) "
                           "THEN failure_tasks WHEN cardinality(failure_tasks)<3 THEN array_append(failure_tasks,$1) ELSE failure_tasks END WHERE id", task_id)
    else:
        await conn.execute("UPDATE memory_retro_activation SET failure_tasks='{}',failure_notice_claimed=false WHERE id")


async def claim_failure_notice(conn, *, now: datetime | None = None) -> tuple[str, ...] | None:
    """Persist the notification claim before sending; a crash cannot send it twice."""
    row = await conn.fetchrow("UPDATE memory_retro_activation SET failure_notice_claimed=true WHERE id "
                              "AND cardinality(failure_tasks)>=3 AND NOT failure_notice_claimed RETURNING failure_tasks")
    return tuple(row["failure_tasks"]) if row else None


async def _settle_spend(conn, token, *, cost_usd, record_id=None) -> None:
    known = (isinstance(cost_usd, (int, float, Decimal)) and not isinstance(cost_usd, bool)
             and math.isfinite(cost_usd) and cost_usd >= 0)
    await conn.execute("UPDATE memory_retro_spend SET settled_usd=$2,state=$3,record_id=$4 "
                       "WHERE attempt_token=$1 AND state='reserved'", token,
                       Decimal(str(cost_usd)) if known else None, "settled" if known else "unknown", record_id)


async def claim_next(conn, *, settings: LessonSettings, now: datetime,
                     lease_seconds: int = 120) -> dict[str, Any] | None:
    """Fence expired attempts and reserve this attempt's full cap before leaving SQL."""
    async with conn.transaction():
        settings = await _settings(conn)
        # Serialize the independent daily ledger across processes, including concurrent claims of different tasks.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()),1930)")
        expired = await conn.fetch("SELECT * FROM memory_retro_runs WHERE state='running' AND lease_until<=$1 "
                                   "ORDER BY id FOR UPDATE SKIP LOCKED", now)
        for run in expired:
            await _settle_spend(conn, run["lease_token"], cost_usd=None)
            terminal = run["attempts"] >= 2
            await conn.execute("UPDATE memory_retro_runs SET state=$2,last_failure='lease_expired',lease_token=NULL,"
                               "lease_until=NULL,finished_at=$3 WHERE id=$1", run["id"],
                               "failed" if terminal else "pending", now if terminal else None)
            if terminal:
                await _finish_streak(conn, run["task_id"], failed=True, failure="lease_expired")
        while True:
            run = await conn.fetchrow("SELECT * FROM memory_retro_runs WHERE state='pending' ORDER BY id "
                                      "FOR UPDATE SKIP LOCKED LIMIT 1")
            if run is None:
                return None
            task = await conn.fetchrow("SELECT t.status,p.memory_lessons FROM tasks t JOIN projects p "
                                       "ON p.id=t.project_id WHERE t.id=$1", run["task_id"])
            reason = "disabled" if not settings.enabled else "project_off" if (
                task is None or task["memory_lessons"] != "on" or task["status"] not in _TERMINAL) else None
            day = now.astimezone().date()
            spent = await conn.fetchval("SELECT COALESCE(sum(CASE WHEN state='settled' THEN settled_usd "
                                       "ELSE reserved_usd END),0) FROM memory_retro_spend WHERE day=$1", day)
            cap = min(Decimal(str(settings.retro_budget_usd)), Decimal(str(settings.daily_budget_usd)) - spent)
            if cap < Decimal('.10') and reason is None:
                reason = "budget"
            if reason:
                await conn.execute("UPDATE memory_retro_runs SET state='skipped',skip_reason=$2,finished_at=$3 "
                                   "WHERE id=$1", run["id"], reason, now)
                continue
            token = uuid4()
            await conn.execute("INSERT INTO memory_retro_spend(attempt_token,run_id,day,reserved_usd,state) "
                               "VALUES($1,$2,$3,$4,'reserved')", token, run["id"], day, cap)
            row = dict(await conn.fetchrow("UPDATE memory_retro_runs SET state='running',attempts=attempts+1,"
                        "lease_token=$2,lease_until=$3,started_at=$4,finished_at=NULL WHERE id=$1 RETURNING *",
                        run["id"], token, now+timedelta(seconds=lease_seconds), now))
            row["budget_usd"] = float(cap)
            return row


async def settle_attempt(conn, run_id: int, token: UUID, *, cost_usd, record_id: int | None = None,
                         failure: str | None = None, now: datetime) -> bool:
    """Settle only the current lease; failed attempts retry once, with a fresh reservation."""
    if failure is not None and failure not in FAILURES:
        raise ValueError("invalid retrospective failure kind")
    async with conn.transaction():
        run = await conn.fetchrow("SELECT * FROM memory_retro_runs WHERE id=$1 AND state='running' "
                                  "AND lease_token=$2 AND lease_until>$3 FOR UPDATE", run_id, token, now)
        if run is None:
            return False
        await _settle_spend(conn, token, cost_usd=cost_usd, record_id=record_id)
        if failure:
            terminal = run["attempts"] >= 2
            await conn.execute("UPDATE memory_retro_runs SET state=$2,record_id=$3,last_failure=$4,"
                               "lease_token=NULL,lease_until=NULL,finished_at=$5 WHERE id=$1", run_id,
                               "failed" if terminal else "pending", record_id, failure, now if terminal else None)
            if terminal:
                await _finish_streak(conn, run["task_id"], failed=True, failure=failure)
        return True


async def load_input(conn, run, *, now: datetime, settings: LessonSettings) -> dict[str, Any]:
    task = await conn.fetchrow("SELECT * FROM tasks WHERE id=$1", run["task_id"])
    if task is None or task["status"] not in _TERMINAL:
        raise ValueError("retrospective task is no longer terminal")
    entries = await conn.fetch("SELECT * FROM task_journal WHERE task_id=$1 AND id BETWEEN $2 AND $3 ORDER BY id",
                               run["task_id"], run["journal_from_id"], run["journal_to_id"])
    active = await conn.fetch("SELECT * FROM memory_candidates WHERE destination='looper' AND project_id=$1 "
                              "AND state='in_effect' AND source_class='agent' AND valid_until>$2 "
                              "ORDER BY id LIMIT $3", task["project_id"], now, min(settings.project_cap, 60))
    held = await conn.fetch("SELECT * FROM memory_candidates WHERE destination='looper' AND project_id=$1 "
                            "AND state='held' AND source_class='agent' ORDER BY id LIMIT 20", task["project_id"])
    return {"task": dict(task), "entries": [dict(row) for row in entries],
            "lessons": [dict(row) for row in active], "held": [dict(row) for row in held]}


async def _trust(conn, task) -> SourceClass:
    inputs = task["untrusted_inputs"]
    if not isinstance(inputs, (list, tuple)) or any(value not in ("leads", "intent") for value in inputs):
        raise ValueError("invalid retrospective task provenance")
    external = bool(inputs) or await conn.fetchval("SELECT EXISTS(SELECT 1 FROM intents WHERE task_id=$1)", task["id"])
    return SourceClass.EXTERNAL if external else SourceClass.AGENT


async def _sources(conn, candidate_id: int, journal_ids) -> None:
    for journal_id in sorted(set(journal_ids)):
        await conn.execute("INSERT INTO memory_candidate_sources(candidate_id,kind,journal_id) "
                           "SELECT $1,'journal',$2 WHERE NOT EXISTS(SELECT 1 FROM memory_candidate_sources "
                           "WHERE candidate_id=$1 AND kind='journal' AND journal_id=$2)", candidate_id, journal_id)


async def create_lesson(conn, lesson, *, project_id: str, source_class: SourceClass, journal_ids,
                        state: State, reason, now: datetime, valid_days: int,
                        supersedes: int | None = None) -> int:
    """Dedicated initial-state path; never misrepresent system creation as held activation."""
    _in_transaction(conn)
    candidate_id = await conn.fetchval("INSERT INTO memory_candidates(kind,source_kind,source_class,destination,"
        "state,reason,statement,why,keywords,fingerprint,project_id,valid_until,supersedes,created_at,updated_at) "
        "VALUES('lesson','task',$1,'looper',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$11) RETURNING id", source_class.value,
        state.value, reason, lesson.statement, lesson.why, list(lesson.keywords), fingerprint(Kind.LESSON, lesson.statement),
        project_id, now+timedelta(days=valid_days), supersedes, now)
    await _sources(conn, candidate_id, journal_ids)
    await _event(conn, candidate_id, Action.PROPOSE, Actor.SYSTEM, Via.SYSTEM)
    await _event(conn, candidate_id, Action.ACTIVATE if state is State.IN_EFFECT else Action.HOLD, Actor.SYSTEM, Via.SYSTEM)
    return candidate_id


async def apply_reply(conn, run_id: int, token: UUID, reply, retro_input, *, settings: LessonSettings,
                      now: datetime, cost_usd=None, record_id: int | None = None) -> dict[str, Any] | None:
    """Revalidate and commit lessons, evidence, events, spend and the range together."""
    async with conn.transaction():
        settings = await _settings(conn)
        run = await conn.fetchrow("SELECT * FROM memory_retro_runs WHERE id=$1 AND state='running' "
                                  "AND lease_token=$2 AND lease_until>$3 FOR UPDATE", run_id, token, now)
        if run is None:
            return None
        task = await conn.fetchrow("SELECT * FROM tasks WHERE id=$1 FOR SHARE", run["task_id"])
        project = await conn.fetchrow("SELECT * FROM projects WHERE id=$1 FOR UPDATE", task["project_id"]) if task else None
        await _settle_spend(conn, token, cost_usd=cost_usd, record_id=record_id)
        reason = "disabled" if not settings.enabled else "project_off" if (
            task is None or task["status"] not in _TERMINAL or project is None or project["memory_lessons"] != "on") else None
        if reason:
            await conn.execute("UPDATE memory_retro_runs SET state='skipped',skip_reason=$2,finished_at=$3,"
                               "record_id=$4,lease_token=NULL,lease_until=NULL WHERE id=$1", run_id, reason, now, record_id)
            return {"state": "skipped", "skip_reason": reason}
        if hasattr(retro_input, "project_id") and retro_input.project_id != task["project_id"]:
            raise ValueError("retrospective project changed")
        trust = await _trust(conn, task)
        valid_journal = {row[0] for row in await conn.fetch("SELECT id FROM task_journal WHERE task_id=$1 "
                         "AND id BETWEEN $2 AND $3", task["id"], run["journal_from_id"], run["journal_to_id"])}
        if not set(retro_input.journal_ids.values()) <= valid_journal:
            raise ValueError("retrospective evidence changed")
        # Every candidate needed for deduplication or replacement is locked in id order, after the project lock.
        candidates = [dict(row) for row in await conn.fetch("SELECT * FROM memory_candidates WHERE destination='looper' "
                      "AND project_id=$1 AND state='in_effect' AND valid_until>$2 ORDER BY id FOR UPDATE", task["project_id"], now)]
        active = {row["id"]: row for row in candidates if row["state"] == "in_effect" and
                  row["source_class"] == "agent" and row["valid_until"] > now}
        by_fingerprint = {row["fingerprint"]: row for row in active.values()}
        occupied = len(active)
        counts = Counter({key: value for key, value in reply.counts.items()
                          if key in _COUNTS and isinstance(value, int) and not isinstance(value, bool) and value >= 0})
        result_ids = []
        for lesson in reply.lessons:
            fp = fingerprint(Kind.LESSON, lesson.statement)
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM memory_tombstones WHERE fingerprint=$1)", fp):
                counts["tombstoned"] += 1
                continue
            try:
                evidence = [retro_input.journal_ids[key] for key in lesson.evidence]
            except KeyError:
                raise ValueError("retrospective cites undisplayed evidence") from None
            if not evidence or not set(evidence) <= valid_journal:
                raise ValueError("retrospective has no valid evidence")
            target_id = retro_input.lesson_ids.get(lesson.same_as or lesson.supersedes)
            if (lesson.same_as or lesson.supersedes) and target_id not in active:
                counts["invalid"] += 1
                continue
            target = active.get(target_id)
            same = active.get(target_id) if lesson.same_as else by_fingerprint.get(fp)
            if trust is SourceClass.AGENT and same is None and await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM memory_candidates WHERE destination='looper' AND project_id=$1 "
                    "AND fingerprint=$2 AND source_class<>'external' AND (state<>'in_effect' OR valid_until<=$3))",
                    task["project_id"], fp, now):
                # Fresh system creation cannot bypass an existing held review or resurrect a terminal lesson.
                counts["invalid"] += 1
                continue
            if trust is SourceClass.AGENT and same:
                await _sources(conn, same["id"], evidence)
                # The first source's task is not a later confirmation. The durable ledger survives run cleanup.
                initial_task = await conn.fetchval("SELECT j.task_id FROM memory_candidate_sources s JOIN task_journal j "
                    "ON j.id=s.journal_id WHERE s.candidate_id=$1 ORDER BY s.id LIMIT 1", same["id"])
                reinforced = None
                if initial_task != task["id"]:
                    reinforced = await conn.fetchval("INSERT INTO memory_lesson_reinforcements(candidate_id,task_id) "
                        "VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING candidate_id", same["id"], task["id"])
                if reinforced:
                    await transition_lesson(conn, same["id"], State.IN_EFFECT, action=Action.REINFORCE,
                        actor=Actor.SYSTEM, via=Via.SYSTEM, expected_state=State.IN_EFFECT,
                        now=now, valid_days=settings.valid_days)
                    await conn.execute("INSERT INTO memory_lesson_stats(candidate_id,reinforced) VALUES($1,1) "
                        "ON CONFLICT(candidate_id) DO UPDATE SET reinforced=memory_lesson_stats.reinforced+1", same["id"])
                    counts["reinforced"] += 1
                await conn.execute("INSERT INTO memory_retro_task_refs VALUES($1,$2) ON CONFLICT DO NOTHING", run_id, same["id"])
                continue
            reason = ("external_source" if trust is SourceClass.EXTERNAL else
                      "overwrite_gate" if target and lesson.supersedes and not overwrite_allowed(
                          target["statement"], target["why"], lesson.statement, lesson.why) else
                      "shadow" if not settings.auto_activate else
                      "project_cap" if occupied >= settings.project_cap else None)
            state = State.HELD if reason else State.IN_EFFECT
            candidate_id = await create_lesson(conn, lesson, project_id=task["project_id"], source_class=trust,
                journal_ids=evidence, state=state, reason=reason, now=now, valid_days=settings.valid_days,
                supersedes=target_id if lesson.supersedes and trust is SourceClass.AGENT else None)
            await conn.execute("INSERT INTO memory_retro_task_refs VALUES($1,$2)", run_id, candidate_id)
            if state is State.IN_EFFECT:
                occupied += 1
                if target and lesson.supersedes:
                    await transition_lesson(conn, target_id, State.SUPERSEDED, action=Action.SUPERSEDE,
                        actor=Actor.SYSTEM, via=Via.SYSTEM, expected_state=State.IN_EFFECT,
                        now=now, valid_days=settings.valid_days)
                    occupied -= 1
                    active.pop(target_id)
                    by_fingerprint.pop(target["fingerprint"], None)
                new = {"id": candidate_id, "fingerprint": fp, "statement": lesson.statement,
                       "why": lesson.why}
                active[candidate_id] = new
                by_fingerprint[fp] = new
                result_ids.append(candidate_id)
            counts[state.value] += 1
            if reason:
                counts[reason] += 1
        safe_counts = dict(counts)
        await conn.execute("UPDATE memory_retro_runs SET state='done',counts=$2::jsonb,record_id=$3,finished_at=$4,"
                           "lease_token=NULL,lease_until=NULL,last_failure=NULL,completed_notified_at=$4 WHERE id=$1",
                           run_id, json.dumps(safe_counts), record_id, now)
        summary = (f"Lessons: {counts['in_effect']} in effect" +
                   (" (" + ", ".join(format_id(number) for number in result_ids) + ")" if result_ids else "") +
                   f", {counts['held']} held, {counts['reinforced']} reinforced: {reply.note}")
        await conn.execute("INSERT INTO activity(task_id,worker,kind,summary,created_at) "
                           "VALUES($1,'memory_retro','memory_retro',$2,$3)", task["id"], summary, now)
        await _finish_streak(conn, task["id"], failed=False)
        return {"state": "done", "counts": safe_counts, "candidate_ids": result_ids, "summary": summary}
