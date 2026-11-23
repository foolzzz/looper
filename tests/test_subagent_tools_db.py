"""Worker tools use their run's authority and durable explorer reservations."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import subagents
from looper.agents.agent_settings import AgentSettings, Launch, AgentType
from looper.agents.ops import RetrySafety
from looper.agents.ops.room import RoomOps
from looper.agents.ops.subagents import SPAWN, WAIT, CANCEL, TOOL_NAMES, WorkerSubagents
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.room import MemberKind, RoomService, Seat
from looper.db import agent_spawns, llm_records, projects
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
BRIEF = {"goal": "Locate entry point", "deliverable": "Evidence", "scope": ["."]}


async def _setup(pool, tmp_path, monkeypatch, *, room_on=False):
    project = await projects.create_project(name="Worker tools", pool=pool)
    await pool.execute("UPDATE projects SET subagents='on',agent_room=$2 WHERE id=$1", project["id"],
                       "on" if room_on else "off")
    await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES('task',$1,'Explore','running')",
                       project["id"])
    await pool.execute("INSERT INTO task_steps(id,task_id,step_index,instruction) VALUES('step','task',0,'Read')")
    settings = AgentSettings(profile=builtin_resolved_profile(), max_budget_usd=2)
    monkeypatch.setattr(subagents, "load_agent_settings", AsyncMock(return_value=settings))
    service = subagents.SpawnService(tmp_path, pool=pool)
    room = RoomService()
    ops = RoomOps(room, url="http://localhost:1")
    facade = WorkerSubagents(service, room)
    ops.subagents = facade

    async def grant(run):
        record_id = await llm_records.start_record(agent_id=run, task_id="task", pool=pool)
        seat = Seat("worker@s1", "worker", MemberKind.WORK, "step", "s1", run, str(tmp_path))
        token = ops.issue("task", seat, room=room_on, subagents=True, kept=True)
        recorder = SimpleNamespace(record_id=record_id, _usage_so_far=SimpleNamespace(cost_usd=.1))
        ops.bind_subagents(run, recorder, Launch(settings, role=AgentType.WORKER))
        return "Bearer " + token, record_id

    return ops, facade, project, grant


def test_spawn_replay_ownership_bounds_and_parent_end(tmp_path, monkeypatch):
    async def check(pool):
        ops, facade, project, grant = await _setup(pool, tmp_path, monkeypatch)
        header, parent = await grant("worker-parent")
        other, _ = await grant("worker-other")
        # Hold the launch queue: every assertion reads the real reservation rows.
        facade.schedule = lambda spawn_id: None
        assert {t["name"] for t in ops.listing(header)} == TOOL_NAMES
        assert ops.retry_safety(header)[SPAWN] == RetrySafety.IDEMPOTENT.value
        first, replay = await asyncio.gather(*(
            ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="bridged-1") for _ in range(2)))
        assert first["status"] == replay["status"] == "ok"
        spawn_id = first["result"]["spawn"]["id"]
        assert spawn_id == replay["result"]["spawn"]["id"]
        assert await pool.fetchval("SELECT count(*) FROM agent_spawns") == 1
        stored = await agent_spawns.get(spawn_id, pool=pool)
        assert stored["parent_record_id"] == parent and stored["parent_spend_known"] is True
        for name, args in ((WAIT, {"spawn_ids": [spawn_id]}), (CANCEL, {"spawn_id": spawn_id})):
            assert (await ops.call(other, name, args))["error"]["code"] == "forbidden"
        for args in ({"wait_seconds": 121}, {"spawn_ids": list(range(1, 10))}, {"wait_seconds": -1}):
            assert (await ops.call(header, WAIT, args))["error"]["code"] == "invalid"
        waited = await ops.call(header, WAIT, {})
        assert waited["result"]["spawns"][0]["id"] == spawn_id
        assert waited["result"]["remaining"]["per_run"] == 3
        missing_id = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF})
        assert missing_id["error"]["code"] == "invalid"
        await ops.end_subagents("worker-parent")
        assert not ops.authorized(header)
        ended = await agent_spawns.get(spawn_id, pool=pool)
        assert ended["state"] == "cancelled" and ended["cancel_reason"] == "parent_ended"
        assert (await ops.call(header, WAIT, {}))["error"]["code"] == "forbidden"
    run_db(check, monkeypatch)


def test_tool_result_timeout_cancel_and_setting_revocation(tmp_path, monkeypatch):
    async def check(pool):
        ops, facade, project, grant = await _setup(pool, tmp_path, monkeypatch)
        header, parent = await grant("worker-parent")
        facade.schedule = lambda spawn_id: None
        result = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF, "key": "model-key"},
                                call_id="first")
        spawn_id = result["result"]["spawn"]["id"]
        retry = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF, "key": "model-key"},
                               call_id="second")
        assert retry["result"]["spawn"]["id"] == spawn_id
        await agent_spawns.move(spawn_id, agent_spawns.SpawnState.FAILED, expect=agent_spawns.SpawnState.QUEUED,
            failure_class="environment", failure_cause="start_failure", detail="runtime absent", pool=pool)
        waited = await ops.call(header, WAIT, {"spawn_ids": [spawn_id], "wait_seconds": 1})
        assert waited["result"]["spawns"][0]["failure"] == {"class": "environment", "cause": "start_failure"}
        row = await agent_spawns.get(spawn_id, pool=pool)
        assert row["delivery"] == "tool" and row["delivered_at"] is not None
        queued = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="cancel-me")
        cancelled = await ops.call(header, CANCEL, {"spawn_id": queued["result"]["spawn"]["id"], "reason": "No need"})
        assert cancelled["result"]["spawn"]["cancel_reason"] == "requested"
        await pool.execute("UPDATE projects SET subagents='off' WHERE id=$1", project["id"])
        off = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="after-off")
        assert off["status"] == "refused" and off["reason"] == "off"
        assert await pool.fetchval("SELECT count(*) FROM agent_spawns") == 2
    run_db(check, monkeypatch)


@pytest.mark.parametrize("kind,enabled", [(MemberKind.WORK, False), (MemberKind.REVIEW, True),
                                         (MemberKind.PLANNER, True), (MemberKind.LOOPER, True)])
def test_only_enabled_work_grants_receive_tools(kind, enabled, tmp_path):
    ops = RoomOps(RoomService(), url="http://localhost:1")
    header = "Bearer " + ops.issue("task", Seat("seat", "worker", kind, run="run"), room=False,
                                   subagents=enabled)
    assert not (TOOL_NAMES & {tool["name"] for tool in ops.listing(header)})
    assert asyncio.run(ops.call(header, WAIT, {}))["error"]["code"] == "forbidden"


def test_grant_recovery_keeps_tools_but_requires_parent_binding(tmp_path, monkeypatch):
    async def check(pool):
        ops, facade, _, grant = await _setup(pool, tmp_path, monkeypatch)
        header, _ = await grant("worker-parent")
        restored = RoomOps(RoomService(), url="http://localhost:1")
        restored.subagents = facade
        restored.import_pending(ops.export_run("worker-parent"))
        assert {t["name"] for t in restored.listing(header)} == TOOL_NAMES
        assert (await restored.call(header, WAIT, {}))["error"]["code"] == "unavailable"
    run_db(check, monkeypatch)


def test_binding_uses_the_recorded_runtime_directory(tmp_path):
    facade = WorkerSubagents(SimpleNamespace(), RoomService())
    seat = Seat("worker@s1", "worker", MemberKind.WORK, run="worker-parent", work_dir=str(tmp_path))
    recorder = SimpleNamespace(record_id=1, _cwd=str(tmp_path / "verification"),
                               _usage_so_far=SimpleNamespace(cost_usd=None))
    launch = SimpleNamespace(max_budget_usd=None)
    facade.bind("task", seat, recorder, launch)
    assert facade._parents[seat.run].caller().work_dir == tmp_path / "verification"


def test_tools_launch_real_standin_wait_push_and_reap_child_on_parent_end(tmp_path, monkeypatch):
    from tests.fake_claude import fake_claude
    from looper.agents import proc_tree
    from looper.db import room as db_room

    cli = fake_claude(tmp_path / "claude", r'''
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
text = json.dumps(report)
print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":text,
                  "total_cost_usd":.02}), flush=True)
''')

    async def check(pool):
        (tmp_path / "source.py").write_text("local evidence")
        ops, facade, _, grant = await _setup(pool, tmp_path, monkeypatch, room_on=True)
        facade.service.claude_bin = cli
        header, parent = await grant("worker-parent")
        try:
            waited = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF, "wait_seconds": 10},
                                    call_id="inline")
            assert waited["status"] == "ok", waited
            first = waited["result"]["spawn"]
            assert first["state"] == "answered" and first["result"]["verified"] is True
            assert first["result"]["summary"] == "local evidence" and first["delivery"] == "tool"
            assert not await db_room.messages("task", pool=pool)
            held = {**BRIEF, "questions": ["release"]}
            pending = await ops.call(header, SPAWN, {"kind": "explorer", "brief": held, "wait_seconds": 1},
                                     call_id="push")
            second = pending["result"]["spawn"]
            assert second["state"] == "asking" and second["delivery"] == "room"
            (tmp_path / "release").touch()
            async with asyncio.timeout(15):
                while not (messages := await db_room.messages("task", pool=pool)):
                    await asyncio.sleep(.01)
            assert len(messages) == 1 and messages[0]["refs"] == [{"spawn_id": second["id"]}]
            held = {**BRIEF, "questions": ["never-release"]}
            last = await ops.call(header, SPAWN, {"kind": "explorer", "brief": held}, call_id="parent-ends")
            spawn_id = last["result"]["spawn"]["id"]
            async with asyncio.timeout(15):
                while True:
                    active = await agent_spawns.get(spawn_id, pool=pool)
                    if (active.get("child_process") or {}).get("pid"):
                        break
                    await asyncio.sleep(.01)
            identity = active["child_process"]
            await ops.end_subagents("worker-parent")
            cancelled = await agent_spawns.get(spawn_id, pool=pool)
            assert cancelled["state"] == "cancelled" and cancelled["cancel_reason"] == "parent_ended"
            assert await asyncio.to_thread(proc_tree._start_time, identity["pid"]) != identity["started"]
            assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE run_kind='explorer'") == 3
            assert await pool.fetchval("SELECT bool_and(parent_record_id=$1) FROM llm_records "
                                       "WHERE run_kind='explorer'", parent)
        finally:
            await facade.stop()
    run_db(check, monkeypatch, timeout=45)


async def _leave_finishing(pool, facade, ops, header):
    import json

    facade.schedule = lambda spawn_id: None
    reply = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="completed-child")
    spawn_id = reply["result"]["spawn"]["id"]
    await agent_spawns.move(spawn_id, agent_spawns.SpawnState.ASKING,
                            expect=agent_spawns.SpawnState.QUEUED, pool=pool)
    await agent_spawns.move(spawn_id, agent_spawns.SpawnState.FINISHING,
        expect=agent_spawns.SpawnState.ASKING, result={"child": {"cost_usd": .02,
        "output": json.dumps({"summary": "Finished evidence", "findings": [],
                              "open_questions": [], "confidence": "high"})}}, pool=pool)
    return spawn_id


@pytest.mark.parametrize("entry,parent_running", [("end", False), ("scan", False), ("scan", True)])
def test_finishing_completes_and_releases_budget_after_parent_end(entry, parent_running, tmp_path, monkeypatch):
    from looper.db import room as db_room

    async def check(pool):
        ops, facade, _, grant = await _setup(pool, tmp_path, monkeypatch, room_on=True)
        header, parent = await grant("worker-parent")
        spawn_id = await _leave_finishing(pool, facade, ops, header)
        if not parent_running:
            await pool.execute("UPDATE llm_records SET status='completed',finished_at=now() WHERE id=$1", parent)
        if entry == "end":
            await ops.end_subagents("worker-parent")
        else:
            # A transient writeback failure must be retried by the live scanner,
            # without recover() or restarting the service.
            finish = facade.service.finish
            attempts = 0
            async def flaky_finish(child_id):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("synthetic writeback failure")
                return await finish(child_id)
            monkeypatch.setattr(facade.service, "finish", flaky_finish)
            scanner = asyncio.create_task(facade._scan())
            try:
                async with asyncio.timeout(3):
                    while (await agent_spawns.get(spawn_id, pool=pool))["delivered_at"] is None:
                        await asyncio.sleep(.01)
            finally:
                scanner.cancel()
                await asyncio.gather(scanner, return_exceptions=True)
            assert attempts == 2
        row = await agent_spawns.get(spawn_id, pool=pool)
        assert row["state"] == "answered" and row["cancel_reason"] is None
        assert row["spent_usd"] == .02 and row["finished_at"] is not None
        usage = await agent_spawns.commitment("task", parent, pool=pool)
        assert usage["task_reserved"] == 0 and usage["task_committed"] == .02
        messages = await db_room.messages("task", pool=pool)
        assert len(messages) == 1 and messages[0]["refs"] == [{"spawn_id": spawn_id}]
    run_db(check, monkeypatch)


def test_spawn_zero_wait_replay_claims_completed_tool_result(tmp_path, monkeypatch):
    from looper.db import room as db_room

    async def check(pool):
        ops, facade, _, grant = await _setup(pool, tmp_path, monkeypatch, room_on=True)
        header, _ = await grant("worker-parent")
        spawn_id = await _leave_finishing(pool, facade, ops, header)
        await facade.service.finish(spawn_id)
        replay = await ops.call(header, SPAWN,
            {"kind": "explorer", "brief": BRIEF, "wait_seconds": 0}, call_id="completed-child")
        assert replay["result"]["spawn"]["result"]["summary"] == "Finished evidence"
        row = await agent_spawns.get(spawn_id, pool=pool)
        assert row["delivery"] == "tool" and row["delivered_at"] is not None
        assert not await db_room.messages("task", pool=pool)
    run_db(check, monkeypatch)


def test_non_keeper_shutdown_uses_parent_ended_reason(tmp_path, monkeypatch):
    async def check(pool):
        ops, facade, _, grant = await _setup(pool, tmp_path, monkeypatch)
        header, _ = await grant("worker-parent")
        facade.schedule = lambda spawn_id: None
        reply = await ops.call(header, SPAWN, {"kind": "explorer", "brief": BRIEF}, call_id="shutdown")
        await facade.stop()
        row = await agent_spawns.get(reply["result"]["spawn"]["id"], pool=pool)
        assert row["state"] == "cancelled" and row["cancel_reason"] == "parent_ended"
    run_db(check, monkeypatch)
