"""Keyset work-list projections; at most three statements per page."""

import base64
import binascii
import json
from datetime import datetime

from .connection import get_pool
from .tasks import TaskGroup, task_group


def seen_principal(principal):
    """The single operator identity; future operator identities belong here."""
    return "operator"


def decode_cursor(cursor, section):
    if cursor is None:
        return None, None
    try:
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (not isinstance(value, list) or len(value) != 3 or value[0] != section
                or not isinstance(value[1], str) or not isinstance(value[2], str) or not value[2]):
            raise ValueError
        at = datetime.fromisoformat(value[1])
        if at.tzinfo is None or "\x00" in value[2]:
            raise ValueError
        value[2].encode("utf-8")
        return at, value[2]
    except (ValueError, TypeError, binascii.Error, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid work-list cursor for this section.") from exc


def _cursor(section, at, row_id):
    return base64.urlsafe_b64encode(json.dumps([section, at.isoformat(), row_id]).encode()).decode()


def _task_sql(ended):
    predicate = ("t.schedule_id IS NULL AND t.status " + ("IN" if ended else "NOT IN")
                 + " ('completed','failed','stopped','cancelled') "
                 "AND ($1::text IS NULL OR t.project_id=$1) AND ($2::text[] IS NULL OR t.status=ANY($2))")
    end = "COALESCE(t.completed_at,t.attempt_ended_at,t.updated_at)"
    columns = ("t.id, t.title, t.goal_id, left(t.requirement,200) AS requirement_head, t.project_id, "
               "p.name AS project_name, t.status, t.run_no, t.created_at, "
               "GREATEST(t.updated_at,a.created_at) AS last_activity_at, COALESCE(a.seq,0) AS last_seq, "
               + (end if ended else "NULL::timestamptz") + " AS ended_at")
    activity = ("LEFT JOIN LATERAL (SELECT seq,created_at FROM activity WHERE task_id=t.id "
                "ORDER BY seq DESC LIMIT 1) a ON true")
    # Ended pages use the ordered partial index before probing activity for their rows.
    page = (f"SELECT {columns} FROM tasks t JOIN projects p ON p.id=t.project_id {activity} "
            f"WHERE {predicate} AND ($3::timestamptz IS NULL OR ({end},t.id)<($3,$4)) "
            f"ORDER BY {end} DESC,t.id DESC LIMIT $5") if ended else (
        f"SELECT * FROM (SELECT {columns} FROM tasks t JOIN projects p ON p.id=t.project_id "
        f"{activity} WHERE {predicate}) s WHERE $3::timestamptz IS NULL OR "
        "(s.last_activity_at,s.id)<($3,$4) ORDER BY s.last_activity_at DESC,s.id DESC LIMIT $5")
    return ("WITH counts AS (SELECT COALESCE(jsonb_object_agg(status,n),'{}'::jsonb) AS counts FROM "
            f"(SELECT t.status,count(*) AS n FROM tasks t WHERE {predicate} GROUP BY t.status) c), "
            f"page AS ({page}) SELECT page.*,counts.counts FROM counts LEFT JOIN page ON true "
            f"ORDER BY page.{'ended_at' if ended else 'last_activity_at'} DESC,page.id DESC")


_OPEN_SQL = _task_sql(False)
_ENDED_SQL = _task_sql(True)
_SCHEDULE_SQL = """
SELECT * FROM (
    SELECT s.id AS schedule_id,s.name,s.project_id,p.name AS project_name,s.enabled,
           s.consecutive_failures,s.last_success_at,lt.id AS last_task_id,lt.status AS last_status,
           COALESCE(lt.completed_at,lt.attempt_ended_at,lt.updated_at) AS last_ended_at,
           COALESCE(lt.created_at,s.created_at) AS sort_at,
           EXISTS (SELECT 1 FROM tasks r WHERE r.schedule_id=s.id
                   AND r.status NOT IN ('completed','failed','stopped','cancelled')) AS running
    FROM schedules s JOIN projects p ON p.id=s.project_id
    LEFT JOIN LATERAL (SELECT * FROM tasks WHERE schedule_id=s.id ORDER BY created_at DESC LIMIT 1) lt ON true
    WHERE ($1::text IS NULL OR s.project_id=$1)
) rows
WHERE $2::timestamptz IS NULL OR sort_at<$2 OR (sort_at=$2 AND schedule_id>$3)
ORDER BY sort_at DESC,schedule_id LIMIT $4
"""


async def _section(principal, *, project_id, statuses, cursor, limit, ended):
    section = "ended" if ended else "open"
    at, row_id = decode_cursor(cursor, section)
    pool = get_pool()
    records = await pool.fetch(_ENDED_SQL if ended else _OPEN_SQL, project_id, statuses, at, row_id, limit + 1)
    counts = json.loads(records[0]["counts"])
    rows = [dict(row) for row in records if row["id"] is not None]
    more = len(rows) > limit
    rows = rows[:limit]
    ids = [row["id"] for row in rows]
    gates, seen = {}, {}
    if ids:
        gates = {r["task_id"]: r["n"] for r in await pool.fetch(
            "SELECT task_id,count(*) AS n FROM task_user_gates WHERE task_id=ANY($1) "
            "AND state IN ('open','discussing') GROUP BY task_id", ids)}
        seen = {r["task_id"]: r["seen_activity_seq"] for r in await pool.fetch(
            "SELECT task_id,seen_activity_seq FROM task_seen WHERE principal=$1 AND task_id=ANY($2)",
            seen_principal(principal), ids)}
    next_cursor = (_cursor(section, rows[-1]["ended_at" if ended else "last_activity_at"], rows[-1]["id"])
                   if more else None)
    for row in rows:
        task_id = row.pop("id")
        row.pop("counts")
        row["task_id"] = task_id
        goal_id = row.pop("goal_id")
        row["title"] = row["title"] or goal_id or task_id
        row["group"] = task_group(row["status"]).value
        row["waiting_gates"] = gates.get(task_id, 0)
        row["needs_you"] = row["group"] == TaskGroup.NEEDS_YOU.value or row["waiting_gates"] > 0
        row["unseen"] = row["last_seq"] > seen.get(task_id, 0) if task_id in seen else not ended and row["last_seq"] > 0
    return {"items": rows, "counts": counts, "next_cursor": next_cursor}


async def open_section(principal, *, project_id=None, statuses=None, cursor=None, limit=50):
    return await _section(principal, project_id=project_id, statuses=statuses, cursor=cursor, limit=limit, ended=False)


async def ended_section(principal, *, project_id=None, statuses=None, cursor=None, limit=50):
    return await _section(principal, project_id=project_id, statuses=statuses, cursor=cursor, limit=limit, ended=True)


async def schedule_section(*, project_id=None, cursor=None, limit=50):
    at, row_id = decode_cursor(cursor, "schedules")
    rows = [dict(r) for r in await get_pool().fetch(_SCHEDULE_SQL, project_id, at, row_id, limit + 1)]
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _cursor("schedules", rows[-1]["sort_at"], rows[-1]["schedule_id"]) if more else None
    for row in rows:
        row.pop("sort_at")
    return {"schedules": rows, "next_cursor": next_cursor}


async def mark_seen(principal, task_id, seq):
    # INSERT..SELECT also turns an unknown task into a typed not-found at the API boundary.
    return await get_pool().fetchval("""
        INSERT INTO task_seen(principal,task_id,seen_activity_seq)
        SELECT $1,id,$3 FROM tasks WHERE id=$2
        ON CONFLICT (principal,task_id) DO UPDATE
        SET seen_activity_seq=GREATEST(task_seen.seen_activity_seq,EXCLUDED.seen_activity_seq),seen_at=now()
        RETURNING seen_activity_seq
    """, seen_principal(principal), task_id, seq)
