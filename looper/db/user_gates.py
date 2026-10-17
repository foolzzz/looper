"""A task's user gates (``task_user_gates``) and their discussions (``task_user_gate_messages``).

The Planner writes its gates with the steps they belong to (``plans.write``
calls ``write`` in the same transaction): a closed gate stays as it closed,
and a gate under discussion never goes back to open. The user's messages come
from the dashboard (``add_user_message``), which moves an open gate to
``discussing`` and stamps ``first_reply_at``; the Planner takes the ones its
turns have not had (``pending``, after ``delivered_through``). A message that
reaches a gate while the turn that closes it runs is no turn's: the close's
transaction makes it a supplement of the task (``via`` ``gate_late``), which
the Planner's next CHAT turn gets. Whether a gate waits for the user or for
the Planner is not stored: it waits for the Planner while one of the user's
messages is undelivered and no turn failed on it (``failed_through``). The
types and states are ``agents.user_gates``'s.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from .connection import get_pool
from .tasks import ACTIVE_STATUSES

WAITING = ("open", "discussing")
CLOSED = ("passed", "cancelled", "skipped")
# The supplement a message that reached a gate as it closed becomes.
LATE_VIA = "gate_late"

_WRITE = f"""
INSERT INTO task_user_gates (step_id, task_id, topic, agenda, findings, state, summary, resolution, ended_by_user,
                             plan_change, delivered_through, turns, opened_at, closed_at, failures, failed_through,
                             failed_at)
VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::jsonb, $8, $9, $10::jsonb, $11, $12, $13, $14, $15, $16, $17)
ON CONFLICT (step_id) DO UPDATE
SET topic = EXCLUDED.topic, agenda = EXCLUDED.agenda, findings = EXCLUDED.findings,
    state = CASE WHEN EXCLUDED.state = 'open' AND task_user_gates.state = 'discussing' THEN 'discussing'
                 ELSE EXCLUDED.state END,
    summary = EXCLUDED.summary, resolution = EXCLUDED.resolution, ended_by_user = EXCLUDED.ended_by_user,
    plan_change = EXCLUDED.plan_change,
    delivered_through = GREATEST(task_user_gates.delivered_through, EXCLUDED.delivered_through),
    turns = GREATEST(task_user_gates.turns, EXCLUDED.turns), failures = EXCLUDED.failures,
    failed_through = EXCLUDED.failed_through, failed_at = EXCLUDED.failed_at,
    opened_at = COALESCE(task_user_gates.opened_at, EXCLUDED.opened_at),
    closed_at = COALESCE(task_user_gates.closed_at, EXCLUDED.closed_at), updated_at = now()
WHERE task_user_gates.state NOT IN {CLOSED!r}
"""

_ADD_MESSAGE = """
INSERT INTO task_user_gate_messages (step_id, task_id, role, kind, text, author, via, agenda_item, option, channel,
                                     chat, external_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
RETURNING *
"""

# A gate's row with its step's label and place, whether one of the user's messages waits for the Planner (no turn
# failed on it), and how many no turn has taken since one failed on them.
_GATES = """
SELECT g.*, s.step_key, s.step_index, s.depends_on,
       EXISTS (SELECT 1 FROM task_user_gate_messages m WHERE m.step_id = g.step_id AND m.role = 'user'
               AND m.id > GREATEST(g.delivered_through, g.failed_through)) AS waiting_for_planner,
       (SELECT count(*) FROM task_user_gate_messages m WHERE m.step_id = g.step_id AND m.role = 'user'
        AND m.id > g.delivered_through AND m.id <= g.failed_through) AS not_delivered
FROM task_user_gates g JOIN task_steps s ON s.id = g.step_id
"""
# The user's messages in a closed gate that no turn of the Planner had: they reached it while the turn that closed
# it ran.
_LATE = f"""
SELECT m.id, m.kind, m.text, m.agenda_item, m.option, s.step_key
FROM task_user_gates g
JOIN task_user_gate_messages m ON m.step_id = g.step_id AND m.role = 'user' AND m.id > g.delivered_through
JOIN task_steps s ON s.id = g.step_id
WHERE g.step_id = $1 AND g.state IN {CLOSED!r}
ORDER BY m.id
"""


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def _gate(row: Mapping[str, Any]) -> dict[str, Any]:
    gate = dict(row)
    for name in ("agenda", "summary", "plan_change"):
        if isinstance(gate.get(name), str):
            gate[name] = json.loads(gate[name])
    gate["agenda"] = list(gate.get("agenda") or [])
    if "depends_on" in gate:
        gate["depends_on"] = list(gate.get("depends_on") or [])
    return gate


def _message_args(task_id: str, message: Mapping[str, Any]) -> tuple[Any, ...]:
    return (message["step_id"], task_id, message["role"], message["kind"], message.get("text") or "",
            message.get("author"), message.get("via") or "", message.get("agenda_item"), message.get("option"),
            message.get("channel"), message.get("chat"), message.get("external_id"))


async def write(conn: asyncpg.Connection, task_id: str, gates: Sequence[Mapping[str, Any]],
                messages: Sequence[Mapping[str, Any]] = ()) -> None:
    """Write the Planner's ``gates`` (``agents.user_gates.Gate.row``) and add its ``messages`` (each ``step_id``,
    ``role``, ``kind``, ``text``), on ``conn`` in the caller's transaction, after the gates' steps. A closed gate's
    messages that no turn had become supplements of the task (``_keep_late``)."""
    if gates:
        await conn.executemany(_WRITE, [
            (gate["step_id"], task_id, gate["topic"], _json(gate.get("agenda") or []), gate.get("findings") or "",
             gate["state"], _json(gate.get("summary")), gate.get("resolution") or "",
             bool(gate.get("ended_by_user")), _json(gate.get("plan_change")), int(gate.get("delivered_through") or 0),
             int(gate.get("turns") or 0), gate.get("opened_at"), gate.get("closed_at"), int(gate.get("failures") or 0),
             int(gate.get("failed_through") or 0), gate.get("failed_at"))
            for gate in gates
        ])
    if messages:
        await conn.executemany(_ADD_MESSAGE.replace("RETURNING *", ""), [_message_args(task_id, m) for m in messages])
    for gate in gates:
        if gate["state"] in CLOSED:
            await _keep_late(conn, task_id, gate["step_id"])


def late_text(label: str, message: Mapping[str, Any]) -> str:
    """The supplement a message of the user's that reached gate ``label`` as it closed becomes; empty for one that
    says nothing (an end of the discussion without text)."""
    text = str(message.get("text") or "").strip()
    if message.get("option") and message.get("agenda_item") and text == message["option"]:
        text = f"chose \"{text}\" for {message['agenda_item']}"
    return f"Written in gate {label} as it closed: {text}" if text else ""


async def _keep_late(conn: asyncpg.Connection, task_id: str, step_id: str) -> None:
    """The user's messages in the closed gate on ``step_id`` that no turn had (they reached it while the turn that
    closed it ran): each one that says something becomes a supplement of the task, not yet delivered, with a note in
    the discussion, and the gate counts them delivered. The write that closed it locked its row
    (``add_user_message`` locks it too), so a message the user adds after this is refused: the gate no longer
    waits."""
    late = await conn.fetch(_LATE, step_id)
    if not late:
        return
    texts = [text for row in late if (text := late_text(row["step_key"], row))]
    if texts:
        await conn.executemany(
            "INSERT INTO task_journal (task_id, step_id, kind, summary, via) VALUES ($1, $2, 'supplement', $3, $4)",
            [(task_id, step_id, text, LATE_VIA) for text in texts])
        await conn.execute(_ADD_MESSAGE.replace("RETURNING *", ""), *_message_args(task_id, {
            "step_id": step_id, "role": "looper", "kind": "late",
            "text": f"{len(texts)} message(s) reached this gate as it closed: the Planner gets them as a supplement "
                    "of the task."}))
    await conn.execute("UPDATE task_user_gates SET delivered_through = $2, updated_at = now() WHERE step_id = $1",
                       step_id, max(int(row["id"]) for row in late))


async def list_for_task(task_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None
                        ) -> list[dict[str, Any]]:
    """The task's gates in plan order, each with its step's ``step_key``, ``step_index`` and ``depends_on``,
    ``waiting_for_planner`` and ``not_delivered``."""
    pool = pool or get_pool()
    rows = await pool.fetch(_GATES + " WHERE g.task_id = $1 ORDER BY s.step_index, s.created_at", task_id)
    return [_gate(row) for row in rows]


async def get(task_id: str, label: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None
              ) -> dict[str, Any] | None:
    """The task's gate whose step is labelled ``label``; None when it has none."""
    pool = pool or get_pool()
    row = await pool.fetchrow(_GATES + " WHERE g.task_id = $1 AND s.step_key = $2", task_id, label)
    return _gate(row) if row else None


async def pending(task_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[dict[str, Any]]:
    """The user's messages in the task's waiting gates that no turn of the Planner has had, oldest first."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT m.* FROM task_user_gate_messages m JOIN task_user_gates g ON g.step_id = m.step_id "
        "WHERE g.task_id = $1 AND g.state = ANY($2::text[]) AND m.role = 'user' AND m.id > g.delivered_through "
        "ORDER BY m.id", task_id, list(WAITING))
    return [dict(row) for row in rows]


async def thread(step_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[dict[str, Any]]:
    """A gate's discussion, oldest first."""
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT * FROM task_user_gate_messages WHERE step_id = $1 ORDER BY id", step_id)
    return [dict(row) for row in rows]


async def add_user_message(
    task_id: str, label: str, message: Mapping[str, Any], *, pool: asyncpg.Pool | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Add the user's ``message`` (``kind`` ``message`` or ``end``, ``text``, ``via`` and optionally ``author``,
    ``agenda_item``, ``option``, ``channel``, ``chat``, ``external_id``) to the gate labelled ``label``, when it
    waits for the user: an open one becomes ``discussing``, and its first message is stamped. Returns the gate
    (None: no such gate) and the message (None: the gate does not wait, so nothing was added)."""
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT g.step_id FROM task_user_gates g JOIN task_steps s ON s.id = g.step_id "
            "WHERE g.task_id = $1 AND s.step_key = $2 FOR UPDATE OF g", task_id, label)
        if row is None:
            return None, None
        gate = await conn.fetchrow(
            "UPDATE task_user_gates SET state = CASE WHEN state = 'open' THEN 'discussing' ELSE state END, "
            "first_reply_at = COALESCE(first_reply_at, now()), updated_at = now() "
            "WHERE step_id = $1 AND state = ANY($2::text[]) RETURNING step_id", row["step_id"], list(WAITING))
        if gate is None:
            return await get(task_id, label, pool=conn), None
        added = await conn.fetchrow(_ADD_MESSAGE, *_message_args(
            task_id, {**message, "step_id": row["step_id"], "role": "user"}))
        return await get(task_id, label, pool=conn), dict(added)


async def due_reminders(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The gates that wait for the user (none of the user's messages waits for the Planner, but those a turn failed
    on), in a running task, whose project reminds (``gate_reminder_hours``, NULL 24, not 0), for longer than that
    since the latest of their opening, the last message the user or the Planner wrote in them and their last
    reminder."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        """
        SELECT g.*, s.step_key, t.project_id, COALESCE(p.gate_reminder_hours, 24) AS reminder_hours,
               GREATEST(g.opened_at, g.last_reminded_at,
                        (SELECT max(m.created_at) FROM task_user_gate_messages m
                         WHERE m.step_id = g.step_id AND m.role IN ('user', 'planner'))) AS quiet_since
        FROM task_user_gates g
        JOIN task_steps s ON s.id = g.step_id
        JOIN tasks t ON t.id = g.task_id
        LEFT JOIN projects p ON p.id = t.project_id
        WHERE g.state = ANY($1::text[]) AND t.status = ANY($2::text[]) AND t.completed_at IS NULL
          AND COALESCE(p.gate_reminder_hours, 24) > 0
          AND NOT EXISTS (SELECT 1 FROM task_user_gate_messages m WHERE m.step_id = g.step_id AND m.role = 'user'
                          AND m.id > GREATEST(g.delivered_through, g.failed_through))
          AND GREATEST(g.opened_at, g.last_reminded_at,
                       (SELECT max(m.created_at) FROM task_user_gate_messages m
                        WHERE m.step_id = g.step_id AND m.role IN ('user', 'planner')))
              + make_interval(hours => COALESCE(p.gate_reminder_hours, 24)) <= now()
        ORDER BY g.opened_at
        """,
        list(WAITING), list(ACTIVE_STATUSES))
    return [_gate(row) for row in rows]


async def record_reminder(step_id: str, task_id: str, text: str, *, pool: asyncpg.Pool | None = None) -> None:
    """Note a reminder of the gate on ``step_id``: a message of code's in its discussion, and its count."""
    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(_ADD_MESSAGE.replace("RETURNING *", ""), *_message_args(
            task_id, {"step_id": step_id, "role": "looper", "kind": "reminder", "text": text}))
        await conn.execute("UPDATE task_user_gates SET reminders = reminders + 1, last_reminded_at = now(), "
                           "updated_at = now() WHERE step_id = $1", step_id)
