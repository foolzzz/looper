"""The Planner's explorer tools on a real PostgreSQL: the Workers' code path with the task's Planner as spawner.

Runtimes are stand-ins; no model runs. Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import subagents
from looper.agents.agent_settings import AgentSettings
from looper.agents.ops import RetrySafety
from looper.agents.ops.planner import PlannerOps
from looper.agents.ops.subagents import CANCEL, SPAWN, TOOL_NAMES, WAIT, PlannerSubagents, WorkerSubagents
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.room import RoomService
from looper.agents.subagent_delivery import mark_turn_delivered, planner_turn_note
from looper.db import agent_spawns, llm_records, projects, room as db_room
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
BRIEF = {"goal": "Locate entry point", "deliverable": "Evidence", "scope": ["."]}
STAND_IN = r'''
import json, sys, time
from pathlib import Path
turn = json.loads(sys.stdin.readline())["message"]["content"]
brief = json.loads(turn.split("\n", 1)[1])
print(json.dumps({"type":"system","subtype":"init","session_id":"synthetic",
                  "tools":["Read","Glob","Grep"],"mcp_servers":[]}), flush=True)
if brief.get("questions"):
    while not Path(brief["questions"][0]).exists():
        time.sleep(.01)
report = {"summary":Path("source.py").read_text(), "findings":[{"claim":"Found it",
          "evidence":{"path":"source.py"}}], "open_questions":[], "confidence":"high"}
print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":json.dumps(report),
                  "total_cost_usd":.02}), flush=True)
'''


class _Bus:
    def __init__(self):
        self.publish = AsyncMock()


async def _setup(pool, tmp_path, monkeypatch, *, room_on=False, bus=None, **project_settings):
    project = await projects.create_project(name="Planner explorers", pool=pool)
    await pool.execute("UPDATE projects SET subagents='on',agent_room=$2 WHERE id=$1", project["id"],
                       "on" if room_on else "off")
    for column, value in project_settings.items():
        await pool.execute(f"UPDATE projects SET {column}=$2 WHERE id=$1", project["id"], value)
    await pool.execute("INSERT INTO tasks(id,project_id,requirement,status,work_dir) "
                       "VALUES('task',$1,'Plan',$2,$3)", project["id"], "running", str(tmp_path))
    settings = AgentSettings(profile=builtin_resolved_profile(), max_budget_usd=2)
    monkeypatch.setattr(subagents, "load_agent_settings", AsyncMock(return_value=settings))
    room = RoomService(bus=bus)
    workers = WorkerSubagents(subagents.SpawnService(tmp_path, pool=pool), room)
    ops = PlannerOps(SimpleNamespace(), url="http://127.0.0.1:9")
    ops.room, ops.subagents = room, PlannerSubagents(workers)
    token = ops.issue("task")
    ops.begin_turn(token)
    record = await llm_records.start_record(agent_id="planner-task", task_id="task", pool=pool)
    return ops, workers, project, "Bearer " + token, record, token


def _only_plan(workers):
    workers.schedule = lambda spawn_id: None


def test_tools_listed_only_when_the_project_has_subagents_on(tmp_path, monkeypatch):
    async def check(pool):
        ops, workers, project, header, _, _ = await _setup(pool, tmp_path, monkeypatch)
        names = {tool["name"] for tool in await ops.listing(header)}
        assert TOOL_NAMES <= names
        safety = await ops.retry_safety(header)
        assert safety[SPAWN] == RetrySafety.IDEMPOTENT.value and safety[WAIT] == RetrySafety.READ_ONLY.value
        await pool.execute("UPDATE projects SET subagents='off' WHERE id=$1", project["id"])
        assert not TOOL_NAMES & {tool["name"] for tool in await ops.listing(header)}
        assert await ops.retry_safety(header) is None
        refused = await ops.call(header, WAIT, {})
        assert refused["status"] == "refused" and refused["reason"] == "off"
    run_db(check, monkeypatch)


def test_spawn_outside_looper_turn_needs_a_user_turn_and_writes_nothing(tmp_path, monkeypatch):
    async def check(pool):
        ops, workers, _, header, _, token = await _setup(pool, tmp_path, monkeypatch)
        _only_plan(workers)
        ops.end_turn(token)
        out = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="c1")
        assert out["status"] == "error" and out["error"]["code"] == "needs_user_turn"
        assert await pool.fetchval("SELECT count(*) FROM agent_spawns") == 0
        assert (await ops.call(header, CANCEL, {"spawn_id": 1}))["error"]["code"] == "needs_user_turn"
        assert (await ops.call(header, WAIT, {}))["status"] == "ok"  # a read works in any turn
    run_db(check, monkeypatch)


def test_same_call_id_returns_one_row_and_refusals_are_typed(tmp_path, monkeypatch):
    async def check(pool):
        ops, workers, project, header, record, _ = await _setup(pool, tmp_path, monkeypatch,
                                                                subagents_per_run=2, subagents_per_task=3,
                                                                subagent_budget_usd=.5)
        _only_plan(workers)
        first, replay = await asyncio.gather(*(
            ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="bridge-1") for _ in range(2)))
        assert first["result"]["spawn"]["id"] == replay["result"]["spawn"]["id"]
        assert await pool.fetchval("SELECT count(*) FROM agent_spawns") == 1
        row = await agent_spawns.get(first["result"]["spawn"]["id"], pool=pool)
        # The turn's record is the parent; no spend or cap is known, so no parent term takes part.
        assert row["parent_record_id"] == record and row["spawner"] == "planner" and row["step_id"] is None
        assert row["parent_spend_known"] is False
        assert (await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="bridge-2"))["status"] == "ok"
        third = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="bridge-3")
        assert third["status"] == "refused" and third["reason"] == "limit_run"
        # A new turn has its own per-run count, but the task's limit still holds.
        await pool.execute("UPDATE llm_records SET status='completed' WHERE id=$1", record)
        await llm_records.start_record(agent_id="planner-task", task_id="task", pool=pool)
        assert (await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="bridge-4"))["status"] == "ok"
        task_limit = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="bridge-5")
        assert task_limit["reason"] == "limit_task"
        await pool.execute("UPDATE projects SET subagents_per_task=10,subagent_task_budget_usd=.05 WHERE id=$1",
                           project["id"])
        low = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="bridge-6")
        assert low["reason"] == "budget"
        assert await pool.fetchval("SELECT count(*) FROM agent_spawns") == 3
    run_db(check, monkeypatch)


def test_no_running_planner_record_and_an_ended_task_are_refused(tmp_path, monkeypatch):
    async def check(pool):
        ops, workers, _, header, record, _ = await _setup(pool, tmp_path, monkeypatch)
        _only_plan(workers)
        await pool.execute("UPDATE llm_records SET status='completed' WHERE id=$1", record)
        out = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="c1")
        assert out["status"] == "refused" and out["reason"] == "role"
        record = await llm_records.start_record(agent_id="planner-task", task_id="task", pool=pool)
        planner = ops.subagents
        await pool.execute("UPDATE tasks SET status='failed' WHERE id='task'")
        direct = await planner.call("task", SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="c2", in_turn=True)
        assert direct["status"] == "refused" and direct["reason"] == "role"
        assert (await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="c3"))["error"]["code"] == "forbidden"
        assert await pool.fetchval("SELECT count(*) FROM agent_spawns") == 0
    run_db(check, monkeypatch)


def test_wait_without_ids_lists_explorers_of_earlier_planner_turns(tmp_path, monkeypatch):
    async def check(pool):
        ops, workers, _, header, first_turn, _ = await _setup(pool, tmp_path, monkeypatch)
        _only_plan(workers)
        old = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="turn-1")
        await pool.execute("UPDATE llm_records SET status='completed' WHERE id=$1", first_turn)
        await llm_records.start_record(agent_id="planner-task", task_id="task", pool=pool)
        new = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="turn-2")
        # A Worker's explorer is not the Planner's.
        worker = await llm_records.start_record(agent_id="worker-x", task_id="task", pool=pool)
        await agent_spawns.create(kind="explorer", task_id="task", step_id=None, parent_record_id=worker, brief=BRIEF,
            inputs={}, budget_usd=.5, state=agent_spawns.SpawnState.QUEUED, idempotency_key="w", spawner="worker@s1",
            delivery="tool", pool=pool)
        listed = await ops.call(header, WAIT, {})
        assert [s["id"] for s in listed["result"]["spawns"]] == [old["result"]["spawn"]["id"],
                                                                 new["result"]["spawn"]["id"]]
        cancelled = await ops.call(header, CANCEL, {"spawn_id": old["result"]["spawn"]["id"]})
        assert cancelled["result"]["spawn"]["cancel_reason"] == "requested"
    run_db(check, monkeypatch)


def test_waited_result_is_delivered_in_the_tool_answer(tmp_path, monkeypatch):
    from tests.fake_claude import fake_claude

    async def check(pool):
        (tmp_path / "source.py").write_text("planner evidence")
        ops, workers, _, header, _, _ = await _setup(pool, tmp_path, monkeypatch)
        workers.service.claude_bin = fake_claude(tmp_path / "claude", STAND_IN)
        try:
            out = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF, "wait_seconds": 15}, call_id="w")
            spawn = out["result"]["spawn"]
            assert spawn["state"] == "answered" and spawn["delivery"] == "tool"
            assert spawn["result"]["summary"] == "planner evidence" and spawn["result"]["verified"] is True
            row = await agent_spawns.get(spawn["id"], pool=pool)
            assert row["delivered_at"] is not None
            assert not await db_room.messages("task", pool=pool)
            # The explorer is one level below the Planner's turn record.
            assert await pool.fetchval("SELECT parent_record_id FROM llm_records WHERE run_kind='explorer'") == row["parent_record_id"]
        finally:
            await workers.stop()
    run_db(check, monkeypatch, timeout=45)


def test_unwaited_result_reaches_the_room_once_and_wakes_the_planner(tmp_path, monkeypatch):
    from tests.fake_claude import fake_claude

    async def check(pool):
        (tmp_path / "source.py").write_text("late evidence")
        bus = _Bus()
        ops, workers, _, header, _, _ = await _setup(pool, tmp_path, monkeypatch, room_on=True, bus=bus)
        workers.service.claude_bin = fake_claude(tmp_path / "claude", STAND_IN)
        try:
            held = {**BRIEF, "questions": [str(tmp_path / "release")]}
            out = await ops.call(header, SPAWN, {"kind": "explorer", "brief": held, "wait_seconds": 1}, call_id="late")
            spawn_id = out["result"]["spawn"]["id"]
            assert out["result"]["spawn"]["state"] == "asking" and out["result"]["spawn"]["delivery"] == "room"
            (tmp_path / "release").touch()
            async with asyncio.timeout(20):
                while not (messages := await db_room.messages("task", pool=pool)):
                    await asyncio.sleep(.02)
            await asyncio.sleep(.1)
            messages = await db_room.messages("task", pool=pool)
            assert len(messages) == 1 and messages[0]["kind"] == "spawn_result"
            assert messages[0]["audience"] == ["planner"] and messages[0]["planner_pending"] is True
            assert messages[0]["refs"] == [{"spawn_id": spawn_id}]
            assert bus.publish.await_count == 1
            full = await ops.call(header, WAIT, {"spawn_ids": [spawn_id]})
            assert full["result"]["spawns"][0]["result"]["summary"] == "late evidence"
            assert (await agent_spawns.get(spawn_id, pool=pool))["delivery"] == "room"
        finally:
            await workers.stop()
    run_db(check, monkeypatch, timeout=60)


def test_room_off_result_rides_the_next_turn_once(tmp_path, monkeypatch):
    from tests.fake_claude import fake_claude

    async def check(pool):
        (tmp_path / "source.py").write_text("next-turn evidence")
        ops, workers, _, header, _, _ = await _setup(pool, tmp_path, monkeypatch, room_on=False)
        workers.service.claude_bin = fake_claude(tmp_path / "claude", STAND_IN)
        try:
            assert await planner_turn_note("task") == ("", [])
            held = {**BRIEF, "questions": [str(tmp_path / "release")]}
            out = await ops.call(header, SPAWN, {"kind": "explorer", "brief": held}, call_id="turn")
            spawn_id = out["result"]["spawn"]["id"]
            assert await planner_turn_note("task") == ("", [])  # nothing has finished
            (tmp_path / "release").touch()
            async with asyncio.timeout(20):
                while (await agent_spawns.get(spawn_id, pool=pool))["finished_at"] is None:
                    await asyncio.sleep(.02)
            await asyncio.sleep(.1)
            row = await agent_spawns.get(spawn_id, pool=pool)
            assert row["delivery"] == "turn" and row["delivered_at"] is None
            assert not await db_room.messages("task", pool=pool)
            note, ids = await planner_turn_note("task")
            assert ids == [spawn_id] and "next-turn evidence" in note and len(note) <= 4000
            await mark_turn_delivered(ids)
            delivered = (await agent_spawns.get(spawn_id, pool=pool))["delivered_at"]
            assert delivered is not None
            assert await planner_turn_note("task") == ("", [])
            await ops.call(header, WAIT, {"spawn_ids": [spawn_id]})  # a later wait does not deliver again
            again = await agent_spawns.get(spawn_id, pool=pool)
            assert again["delivered_at"] == delivered and again["delivery"] == "turn"
        finally:
            await workers.stop()
    run_db(check, monkeypatch, timeout=60)


def test_turn_note_is_bounded_marks_unverified_and_hints_at_the_rest(tmp_path, monkeypatch):
    async def check(pool):
        ops, workers, _, header, record, _ = await _setup(pool, tmp_path, monkeypatch)
        ids = []
        for number in range(5):
            row = await agent_spawns.create(kind="explorer", task_id="task", step_id=None, parent_record_id=record,
                brief=BRIEF, inputs={}, budget_usd=.5, state=agent_spawns.SpawnState.QUEUED,
                idempotency_key=f"k{number}", spawner="planner", delivery="turn", pool=pool)
            await agent_spawns.move(row["id"], agent_spawns.SpawnState.FAILED, expect=agent_spawns.SpawnState.QUEUED,
                result={"summary": f"entry{number} " + "x" * 1400, "verified": number != 0}, detail="d", pool=pool)
            ids.append(row["id"])
        note, shown = await planner_turn_note("task")
        assert len(note) <= 4000 and 0 < len(shown) < 5 and shown == ids[:len(shown)]
        assert "Unverified" in note and "looper_agent_wait" in note
        assert f"{5 - len(shown)} more" in note
        assert (await planner_turn_note("task", on=False)) == ("", [])
        await mark_turn_delivered(shown)
        later, rest = await planner_turn_note("task")
        assert rest and not set(rest) & set(shown)
    run_db(check, monkeypatch)


def test_planner_turn_end_cancels_nothing_but_task_end_cancels_everything(tmp_path, monkeypatch):
    async def check(pool):
        ops, workers, _, header, record, _ = await _setup(pool, tmp_path, monkeypatch)
        _only_plan(workers)
        out = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="keep")
        spawn_id = out["result"]["spawn"]["id"]
        await pool.execute("UPDATE llm_records SET status='completed',finished_at=now() WHERE id=$1", record)
        scanner = asyncio.create_task(workers._scan())
        try:
            await asyncio.sleep(1.5)
            assert (await agent_spawns.get(spawn_id, pool=pool))["state"] == "queued"
            await pool.execute("UPDATE tasks SET status='failed' WHERE id='task'")
            async with asyncio.timeout(5):
                while (await agent_spawns.get(spawn_id, pool=pool))["state"] != "cancelled":
                    await asyncio.sleep(.05)
        finally:
            scanner.cancel()
            await asyncio.gather(scanner, return_exceptions=True)
        assert (await agent_spawns.get(spawn_id, pool=pool))["cancel_reason"] == "task_ended"
    run_db(check, monkeypatch, timeout=30)


def test_planner_api_passes_the_bridge_call_id():
    from looper.agents.http_server import AgentHTTPServer

    calls = []

    class Ops:
        def authorized(self, header):
            return True

        async def call(self, header, name, arguments, *, call_id=None):
            calls.append((name, arguments, call_id))
            return {"status": "ok"}

    class Writer:
        def __init__(self):
            self.data = b""

        def write(self, data):
            self.data += data

        async def drain(self):
            pass

    writer = Writer()
    asyncio.run(AgentHTTPServer._handle_planner_api(
        SimpleNamespace(_planner_ops=Ops()), "POST", "/api/agent/v1/ops/looper_agent_spawn",
        {"authorization": "Bearer planner.x", "x-looper-call": "call-9"}, json.dumps({"kind": "explorer"}).encode(), writer))
    assert calls == [("looper_agent_spawn", {"kind": "explorer"}, "call-9")] and b'"status": "ok"' in writer.data


def test_task_cap_holds_when_the_planner_turn_spend_is_unknown(tmp_path, monkeypatch):
    from looper.agents import agent_settings

    async def check(pool):
        ops, workers, project, header, _, _ = await _setup(pool, tmp_path, monkeypatch, task_budget_usd=1,
                                                           subagent_budget_usd=.5)
        _only_plan(workers)
        turn = AgentSettings(profile=builtin_resolved_profile(), max_budget_usd=.8)
        monkeypatch.setattr(agent_settings, "load_agent_settings", AsyncMock(return_value=turn))
        # Nothing is recorded yet, but the turn may still spend 0.80 of the task's 1.00.
        clipped = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="a")
        row = await agent_spawns.get(clipped["result"]["spawn"]["id"], pool=pool)
        assert row["parent_spend_known"] is False and row["budget_usd"] == pytest.approx(.2)
        monkeypatch.setattr(agent_settings, "load_agent_settings", AsyncMock(
            return_value=AgentSettings(profile=builtin_resolved_profile(), max_budget_usd=.95)))
        refused = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="b")
        assert refused["status"] == "refused" and refused["reason"] == "budget"
    run_db(check, monkeypatch)


@pytest.mark.parametrize("end", ["stopped", "failed", "cancelled", "completed", "restart"])
def test_task_end_and_restart_cancel_explorers_without_the_sweep(end, tmp_path, monkeypatch):
    from looper.db import tasks as task_rows

    async def check(pool):
        ops, workers, _, header, _, _ = await _setup(pool, tmp_path, monkeypatch)
        _only_plan(workers)
        out = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="e")
        spawn_id = out["result"]["spawn"]["id"]

        async def cascade(task_id):
            await workers.service.cascade(task_id=task_id, reason=agent_spawns.CancelReason.TASK_ENDED)
        monkeypatch.setattr(task_rows, "task_ended_listeners", [cascade])
        if end == "restart":
            # The status is only briefly not running: no sweep gets a look at it.
            await task_rows.reset_task_for_restart("task", keep_steps=True)
            await pool.execute("UPDATE tasks SET status='running' WHERE id='task'")
        else:
            await task_rows.update_task_status("task", end)
        row = await agent_spawns.get(spawn_id, pool=pool)
        assert row["state"] == "cancelled" and row["cancel_reason"] == "task_ended"
    run_db(check, monkeypatch)

