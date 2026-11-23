"""Explorer results are committed to their Worker's room exactly once."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.room import MemberKind, RoomService, Seat
from looper.agents.subagent_delivery import deliver_room, mark_tool_delivered
from looper.db import agent_spawns, llm_records, projects, room as db_room
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _scenario(pool, *, subagents="on", agent_room="on", state=agent_spawns.SpawnState.FAILED):
    project = await projects.create_project(name="Explorer delivery", pool=pool)
    await pool.execute("UPDATE projects SET subagents=$2, agent_room=$3 WHERE id=$1", project["id"],
                       subagents, agent_room)
    await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) "
                       "VALUES('delivery-task',$1,'Inspect source','running')", project["id"])
    await pool.execute("INSERT INTO task_steps(id,task_id,step_index,step_key,instruction,status,worker_id) "
                       "VALUES('delivery-step','delivery-task',0,'s1','Read source','dispatched','worker-delivery')")
    parent = await llm_records.start_record(agent_id="worker-delivery", task_id="delivery-task", pool=pool)
    row = await agent_spawns.create(kind="explorer", task_id="delivery-task", step_id="delivery-step",
        parent_record_id=parent, brief={"goal": "Locate evidence"}, inputs={}, budget_usd=.5,
        state=agent_spawns.SpawnState.QUEUED, idempotency_key="delivery", spawner="worker@s1", delivery="room", pool=pool)
    if state is not agent_spawns.SpawnState.QUEUED:
        row = await agent_spawns.move(row["id"], state, expect=agent_spawns.SpawnState.QUEUED,
            result={"summary": "Evidence " * 300, "verified": False}, detail="Read failed", pool=pool)
    workers = SimpleNamespace(
        room_runs=lambda task_id: {"worker-delivery": ("running", datetime.now(timezone.utc))},
        remind=AsyncMock(return_value=True))
    room = RoomService(worker_pool=workers)
    return SimpleNamespace(pool=pool), room, workers, row


def test_concurrent_room_delivery_stores_one_bounded_addressed_message_and_pushes(monkeypatch):
    async def check(pool):
        service, room, workers, row = await _scenario(pool)
        outcomes = await asyncio.gather(*(deliver_room(service, room, row["id"]) for _ in range(2)))
        assert sorted(outcomes) == [False, True]
        messages = await db_room.messages("delivery-task", pool=pool)
        assert len(messages) == 1
        message = messages[0]
        assert message["kind"] == "spawn_result" and message["sender_seat"] == "looper"
        assert message["audience"] == ["worker@s1"] and not message["planner_pending"]
        assert message["refs"] == [{"spawn_id": row["id"]}]
        assert "Unverified" in message["body"] and len(message["body"]) < 1700
        assert str(row["id"]) in message["body"] and "looper_agent_wait" in message["body"]
        stored = await agent_spawns.get(row["id"], pool=pool)
        assert stored["delivered_at"] is not None and stored["delivery"] == "room"
        workers.remind.assert_awaited_once_with("worker-delivery")
        seat = Seat("worker@s1", "worker", MemberKind.WORK, run="worker-delivery")
        inbox = await room.inbox("delivery-task", seat, peek=True)
        assert inbox["messages"][0]["spawn_id"] == row["id"]
    run_db(check, monkeypatch)


@pytest.mark.parametrize("switches", [{"subagents": None}, {"agent_room": "off"},
                                        {"state": agent_spawns.SpawnState.QUEUED}])
def test_room_delivery_leaves_disabled_or_unfinished_results_for_tool_readback(monkeypatch, switches):
    async def check(pool):
        service, room, workers, row = await _scenario(pool, **switches)
        assert not await deliver_room(service, room, row["id"])
        assert not await db_room.messages("delivery-task", pool=pool)
        assert (await agent_spawns.get(row["id"], pool=pool))["delivered_at"] is None
        workers.remind.assert_not_awaited()
    run_db(check, monkeypatch)


def test_room_write_failure_rolls_back_receipt_and_can_be_retried(monkeypatch):
    async def check(pool):
        service, room, workers, row = await _scenario(pool)
        original = db_room.add
        with monkeypatch.context() as patch:
            patch.setattr(db_room, "add", AsyncMock(side_effect=OSError("room unavailable")))
            with pytest.raises(OSError, match="room unavailable"):
                await deliver_room(service, room, row["id"])
        assert (await agent_spawns.get(row["id"], pool=pool))["delivered_at"] is None
        assert not await db_room.messages("delivery-task", pool=pool)
        assert db_room.add is original
        assert await deliver_room(service, room, row["id"])
        workers.remind.assert_awaited_once()
    run_db(check, monkeypatch)


def test_tool_receipt_wins_against_room_delivery_without_losing_result(monkeypatch):
    async def check(pool):
        service, room, workers, row = await _scenario(pool)
        await mark_tool_delivered(pool, [row["id"]])
        first = await agent_spawns.get(row["id"], pool=pool)
        assert not await deliver_room(service, room, row["id"])
        await mark_tool_delivered(pool, [row["id"]])
        second = await agent_spawns.get(row["id"], pool=pool)
        assert second["delivered_at"] == first["delivered_at"] and second["delivery"] == "tool"
        assert second["result"] == row["result"]
        assert not await db_room.messages("delivery-task", pool=pool)
        workers.remind.assert_not_awaited()
    run_db(check, monkeypatch)


def test_room_and_tool_delivery_race_has_one_first_receipt(monkeypatch):
    async def check(pool):
        service, room, workers, row = await _scenario(pool)
        await asyncio.gather(deliver_room(service, room, row["id"]), mark_tool_delivered(pool, [row["id"]]))
        stored = await agent_spawns.get(row["id"], pool=pool)
        messages = await db_room.messages("delivery-task", pool=pool)
        assert stored["delivered_at"] is not None
        assert len(messages) == (1 if stored["delivery"] == "room" else 0)
        assert stored["result"] == row["result"]
    run_db(check, monkeypatch)


def test_tool_receipt_does_not_mark_unfinished_spawn_delivered(monkeypatch):
    async def check(pool):
        service, room, workers, row = await _scenario(pool, state=agent_spawns.SpawnState.QUEUED)
        await mark_tool_delivered(pool, [row["id"]])
        assert (await agent_spawns.get(row["id"], pool=pool))["delivered_at"] is None
    run_db(check, monkeypatch)


def test_room_kind_upgrade_preserves_existing_messages_and_validates_new_kind(monkeypatch):
    async def check(pool):
        from looper.db.connection import ensure_schema

        service, room, workers, row = await _scenario(pool)
        legacy = await db_room.add("delivery-task", sender_seat="looper", sender_kind="looper",
                                   kind="notice", body="Existing message", audience=["worker@s1"], pool=pool)
        await pool.execute("DELETE FROM schema_migrations WHERE name IN "
                           "('subagent_room_delivery','subagent_room_delivery_online')")
        await pool.execute("ALTER TABLE room_messages DROP CONSTRAINT room_messages_kind_check, "
                           "ADD CONSTRAINT room_messages_kind_check CHECK (kind IN "
                           "('info','question','answer','interface','announce','notice','escalation'))")
        await ensure_schema(pool)
        assert await deliver_room(service, room, row["id"])
        messages = await db_room.messages("delivery-task", pool=pool)
        assert [(m["id"], m["kind"]) for m in messages] == [(legacy["id"], "notice"), (messages[1]["id"], "spawn_result")]
        assert await pool.fetchval("SELECT convalidated FROM pg_constraint "
                                   "WHERE conrelid='room_messages'::regclass AND conname='room_messages_kind_check'")
    run_db(check, monkeypatch)


def test_planner_result_is_stored_pending_for_the_planner_and_wakes_it_once(monkeypatch):
    async def check(pool):
        service, _, _, row = await _scenario(pool)
        await pool.execute("UPDATE agent_spawns SET spawner='planner' WHERE id=$1", row["id"])
        bus = SimpleNamespace(publish=AsyncMock())
        room = RoomService(bus=bus)
        outcomes = await asyncio.gather(*(deliver_room(service, room, row["id"]) for _ in range(2)))
        assert sorted(outcomes) == [False, True]
        message, = await db_room.messages("delivery-task", pool=pool)
        assert message["audience"] == ["planner"] and message["planner_pending"] is True
        assert message["kind"] == "spawn_result" and message["refs"] == [{"spawn_id": row["id"]}]
        assert [m["id"] for m in await db_room.planner_pending("delivery-task", pool=pool)] == [message["id"]]
        await asyncio.sleep(.05)
        assert bus.publish.await_count == 1
    run_db(check, monkeypatch)


def test_turn_receipt_and_tool_receipt_share_one_first_delivery(monkeypatch):
    from looper.agents.subagent_delivery import mark_turn_delivered

    async def check(pool):
        _, _, _, row = await _scenario(pool)
        await pool.execute("UPDATE agent_spawns SET spawner='planner',delivery='turn' WHERE id=$1", row["id"])
        await asyncio.gather(mark_turn_delivered([row["id"]]), mark_tool_delivered(pool, [row["id"]]))
        first = await agent_spawns.get(row["id"], pool=pool)
        assert first["delivered_at"] is not None and first["delivery"] in ("turn", "tool")
        await mark_turn_delivered([row["id"]])
        await mark_tool_delivered(pool, [row["id"]])
        again = await agent_spawns.get(row["id"], pool=pool)
        assert (again["delivered_at"], again["delivery"]) == (first["delivered_at"], first["delivery"])
    run_db(check, monkeypatch)
