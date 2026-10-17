"""The user's side of user gates (``user_gates``): the gates a task page shows, the messages the user writes in
them from the dashboard, and the reminders of the gates that wait for the user.

A message is stored first (``db.user_gates.add_user_message``, only while the
gate waits for the user) and its task's Planner is woken (``GATE_MESSAGE``),
which gives it to Claude in a [GATE] turn after the event it is in. A stopped
task's gate keeps its messages for the restart, which gives them. Ending the
discussion is a message too (kind ``end``): the next [GATE] turn must close
the gate. ``GateReminders`` notes, at its project's interval
(``gate_reminder_hours``), each gate that waits for the user, in its
discussion and the task's activity.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ..db import activity as db_activity, tasks as db_tasks, user_gates as db_user_gates
from .bus import MessageBus
from .message import AgentAddress, MessageEnvelope, MessageType, planner_address
from .projects import RequestError
from .user_gates import MESSAGE_MAX_CHARS, GateState, MessageKind

logger = logging.getLogger(__name__)

HTTP_400 = "400 Bad Request"
HTTP_404 = "404 Not Found"
HTTP_409 = "409 Conflict"
VIA_DASHBOARD = "dashboard"
# How often the reminders look for gates that have waited long enough.
REMINDER_CHECK_SEC = 300


def gate_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A gate as the API shows it: its step's label, state, agenda and how it closed, whom it waits for
    (``waiting_on``: ``user``, ``planner`` while one of the user's messages is undelivered and no turn failed on it,
    None once closed or before it opens), and how many of the user's messages a turn failed on and none has taken
    since (``not_delivered``: the user's next message sends them again)."""
    state = GateState(row["state"])
    waiting_on = None
    if state.waiting:
        waiting_on = "planner" if row.get("waiting_for_planner") else "user"
    return {
        "step": row.get("step_key"), "step_id": row["step_id"], "state": state.value, "topic": row["topic"],
        "agenda": list(row.get("agenda") or []), "findings": row.get("findings") or "",
        "summary": row.get("summary"), "resolution": row.get("resolution") or "",
        "ended_by_user": bool(row.get("ended_by_user")), "plan_change": row.get("plan_change"),
        "depends_on": list(row.get("depends_on") or []), "waiting_on": waiting_on, "turns": row.get("turns") or 0,
        "not_delivered": int(row.get("not_delivered") or 0) if state.waiting else 0,
        "reminders": row.get("reminders") or 0, "opened_at": row.get("opened_at"),
        "first_reply_at": row.get("first_reply_at"), "closed_at": row.get("closed_at"),
    }


async def gates_view(task_id: str) -> list[dict[str, Any]]:
    """The task's gates in plan order (``gate_view``)."""
    return [gate_view(row) for row in await db_user_gates.list_for_task(task_id)]


def _message_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in ("id", "role", "kind", "text", "author", "via", "agenda_item", "option",
                                          "created_at")}


async def thread(task_id: str, label: str) -> dict[str, Any]:
    """The discussion of the task's gate ``label``, oldest first; 404 for no such gate."""
    gate = await db_user_gates.get(task_id, label)
    if gate is None:
        raise RequestError(HTTP_404, f"task {task_id} has no gate {label}")
    return {"task_id": task_id, "gate": gate_view(gate),
            "messages": [_message_view(row) for row in await db_user_gates.thread(gate["step_id"])]}


def _message(data: Mapping[str, Any], gate: Mapping[str, Any] | None, *, end: bool) -> dict[str, Any]:
    """The message ``data`` asks for, checked: ``text`` (a non-empty string, at most ``MESSAGE_MAX_CHARS``; optional
    when ending), and an option the user chose (``agenda_item``, an item of the gate's agenda, and ``option``,
    one of its options)."""
    text = data.get("text", "" if end else None)
    if not isinstance(text, str) or (not end and not text.strip()):
        raise RequestError(HTTP_400, "text must be a non-empty string")
    text = text.strip()
    if len(text) > MESSAGE_MAX_CHARS:
        raise RequestError(HTTP_400, f"text has {len(text)} characters; at most {MESSAGE_MAX_CHARS}")
    item_id, option = data.get("agenda_item"), data.get("option")
    if (item_id is None) != (option is None) or end and item_id is not None:
        raise RequestError(HTTP_400, "agenda_item and option go together, on a message that is not the end")
    if item_id is not None:
        item = next((i for i in (gate or {}).get("agenda") or [] if i.get("id") == item_id), None)
        if item is None:
            raise RequestError(HTTP_400, f"agenda_item {item_id!r} is not an item of this gate's agenda")
        if option not in (item.get("options") or []):
            raise RequestError(HTTP_400, f"option {option!r} is not one of {item_id}'s options")
    return {"kind": (MessageKind.END if end else MessageKind.MESSAGE).value, "text": text,
            "agenda_item": item_id, "option": option}


async def post_message(
    task_id: str, label: str, data: Mapping[str, Any], *, has_planner: bool, bus: MessageBus, source: AgentAddress,
    via: str = VIA_DASHBOARD, end: bool = False,
) -> dict[str, Any]:
    """Add the user's message (``end``: their end of the discussion) to the task's gate ``label``, and wake its
    Planner when it runs (``has_planner``). 404 for no such task or gate, 409 for a gate that does not wait for
    the user or a task that completed, 400 for a message that is not right. ``delivered`` says what happens to
    it: ``queued`` (the Planner takes it next), or ``kept`` (the task is not running: its restart gives it)."""
    task = await db_tasks.get_task(task_id)
    if task is None:
        raise RequestError(HTTP_404, "task not found")
    if db_tasks.task_group(task.get("status")) is db_tasks.TaskGroup.COMPLETED:
        raise RequestError(HTTP_409, "the task has completed: its gates no longer take messages")
    current = await db_user_gates.get(task_id, label)
    if current is None:
        raise RequestError(HTTP_404, f"task {task_id} has no gate {label}")
    message = {**_message(data, current, end=end), "via": via}
    gate, added = await db_user_gates.add_user_message(task_id, label, message)
    if gate is None:
        raise RequestError(HTTP_404, f"task {task_id} has no gate {label}")
    if added is None:
        raise RequestError(HTTP_409, f"gate {label} is {gate['state']}: it does not wait for the user")
    if has_planner:
        await bus.publish(MessageEnvelope.create(
            source=source, target=planner_address(task_id), message_type=MessageType.GATE_MESSAGE,
            payload={"task_id": task_id, "gate": label}))
    return {"task_id": task_id, "gate": gate_view(gate), "message": _message_view(added),
            "delivered": "queued" if has_planner else "kept"}


def _hours(since: datetime | None) -> int:
    if since is None:
        return 0
    return int((datetime.now(timezone.utc) - since).total_seconds() // 3600)


class GateReminders:
    """Every ``interval`` seconds, note each gate that has waited for the user for its project's
    ``gate_reminder_hours`` since it was last quiet: a reminder in its discussion and a line in its task's
    activity."""

    def __init__(self, *, interval: float = REMINDER_CHECK_SEC) -> None:
        self._interval = interval
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("the gate reminders failed; trying again later")
            await asyncio.sleep(self._interval)

    async def run_once(self) -> int:
        """Note every reminder due now; how many."""
        due = await db_user_gates.due_reminders()
        for gate in due:
            waited = _hours(gate.get("opened_at"))
            text = f"Still waiting for you ({waited} h): {gate['topic']}"
            await db_user_gates.record_reminder(gate["step_id"], gate["task_id"], text)
            await db_activity.insert_activity(task_id=gate["task_id"], worker="planner", kind="phase",
                                              summary=f"Gate {gate['step_key']} still waits for the user "
                                                      f"(waited {waited} h); reminder noted"[:500])
        return len(due)
