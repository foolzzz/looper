"""First-delivery receipts shared by explorer tools and the Worker's task room."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ..db import agent_spawns, room as db_room
from ..db.connection import _connection
from .project_execution import ProjectExecution
from .room import LOOPER, MemberKind, MessageKind, Reason

if TYPE_CHECKING:
    from .room import RoomService
    from .subagents import SpawnService


def _summary(row: dict[str, Any]) -> str:
    result = row.get("result") or {}
    text = str(result.get("summary") or result.get("text") or row.get("detail") or row["state"])
    if result.get("verified") is False:
        text = "Unverified (structure check failed): " + text
    return text[:1500]


async def deliver_room(service: SpawnService, room: RoomService, spawn_id: int) -> bool:
    """Commit one addressed result and its receipt atomically; notify the Worker after commit.

    A failed insert leaves the result available to a later room retry or tool
    read. A tool receipt takes the same row lock, so the two paths cannot both
    claim the first delivery.
    """
    async with _connection(service.pool) as conn, conn.transaction():
        row = await conn.fetchrow("SELECT * FROM agent_spawns WHERE id=$1 FOR UPDATE", spawn_id)
        row = agent_spawns._row(row)
        if (row is None or row["kind"] != "explorer" or row["finished_at"] is None
                or row["delivery"] != "room" or row["delivered_at"] is not None or not row.get("spawner")):
            return False
        project = await conn.fetchrow("SELECT p.* FROM projects p JOIN tasks t ON t.project_id=p.id "
                                      "WHERE t.id=$1", row["task_id"])
        if (project is None or project["subagents"] != "on"
                or not ProjectExecution.from_row(project).room_on):
            return False
        body = (f"Explorer {spawn_id} ({row['state']}):\n{_summary(row)}\n"
                f"Full result: looper_agent_wait with spawn_ids=[{spawn_id}].")
        await db_room.add(row["task_id"], sender_seat=LOOPER, sender_kind=MemberKind.LOOPER.value,
            kind=MessageKind.SPAWN_RESULT.value, body=body, audience=[row["spawner"]], via="looper",
            reason=Reason.SPAWN_RESULT.value, refs=[{"spawn_id": spawn_id}], pool=conn)
        await conn.execute("UPDATE agent_spawns SET delivered_at=now(), updated_at=now() WHERE id=$1", spawn_id)
    await room.notify_spawn_result(row["task_id"], row["spawner"])
    return True


async def mark_tool_delivered(pool: Any, ids: Sequence[int]) -> None:
    """Record first delivery of terminal results returned by a tool, without changing their results."""
    if ids:
        await pool.execute("UPDATE agent_spawns SET delivered_at=now(), delivery='tool', updated_at=now() "
                           "WHERE id=ANY($1::bigint[]) AND kind='explorer' AND finished_at IS NOT NULL "
                           "AND delivered_at IS NULL", sorted(set(ids)))
