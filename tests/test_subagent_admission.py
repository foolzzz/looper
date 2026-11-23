"""Explorer account admission through the persistent service and both tool callers."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import admission, spawn, subagents
from looper.agents.agent_settings import Access, AgentSettings, AgentType, Launch
from looper.agents.lanes import Lane
from looper.agents.ops import subagents as tools
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.room import MemberKind, RoomService, Seat
from looper.db import agent_spawns, llm_records, projects
from tests.db_support import run_db, until

FIXTURE = Path(__file__).parent / "fixtures/subagent_admission_off.json"
BASE = "0d9e121c1eb124282c33b5e8352164f30d212425"
BRIEF = {"goal": "Locate entry point", "deliverable": "Evidence", "scope": ["."]}
REPORT = {"summary": "Source inspected", "findings": [], "open_questions": [], "confidence": "high"}


async def _setup(pool, root, monkeypatch, *, on=True, planner=False, lane=Lane.USER_TASK):
    project = await projects.create_project(name="Synthetic explorer admission", pool=pool)
    await pool.execute("UPDATE projects SET subagents='on',agent_room='off',subagent_budget_usd=.5 WHERE id=$1", project["id"])
    await pool.execute("INSERT INTO tasks(id,project_id,requirement,status,work_dir) "
                       "VALUES('task',$1,'Inspect source','running',$2)", project["id"], str(root))
    await pool.execute("INSERT INTO task_steps(id,task_id,step_index,instruction) VALUES('step','task',0,'Read')")
    owner = "planner-task" if planner else "worker-parent"
    record = await llm_records.start_record(agent_id=owner, task_id="task", pool=pool,
                                           metadata={"admission_lane": lane.value})
    settings = AgentSettings(profile=builtin_resolved_profile(), max_budget_usd=2 if on else .5)
    provider = settings.profile.resolved_model.provider.id
    await pool.execute("UPDATE llm_providers SET max_concurrency=1,name='Synthetic account' WHERE id=$1", provider)
    monkeypatch.setattr(subagents, "load_agent_settings", AsyncMock(return_value=settings))
    controller = admission.Controller()
    controller.active = on
    monkeypatch.setattr(admission, "_controller", controller)
    for name in ("slot", "register", "release", "transition", "snapshot", "lend", "hold"):
        if hasattr(controller, name):
            monkeypatch.setattr(admission, name, getattr(controller, name))
    service = subagents.SpawnService(root / "runtime", pool=pool)
    facade = tools.WorkerSubagents(service, RoomService())
    if planner:
        parent = tools._PlannerParent("task", record, root, True)
    else:
        seat = Seat("worker@s1", "worker", MemberKind.WORK, "step", "s1", owner, str(root))
        recorder = SimpleNamespace(record_id=record, _usage_so_far=SimpleNamespace(cost_usd=.01))
        facade.bind("task", seat, recorder, Launch(settings, role=AgentType.WORKER))
        parent = facade._parents[owner]
    return SimpleNamespace(pool=pool, service=service, facade=facade, parent=parent, controller=controller,
                           provider=provider, owner=owner, settings=settings, project=project)


async def _call(case, name, args, key=None):
    async def resolve():
        return case.parent
    return await case.facade.serve("task", case.owner, case.owner, resolve, name, args, key)


def _runtime(case, monkeypatch):
    """A recorded child whose completion is controlled by its spawn id."""
    gates, started = {}, []

    async def run(child, **kwargs):
        row = next(row for row in await agent_spawns.for_task("task", pool=case.pool)
                   if child.agent_id == case.service.owner_of(row["id"]))
        record = await llm_records.start_record(agent_id=child.agent_id, task_id="task", run_kind="explorer",
            parent_record_id=kwargs["parent"].record_id, pool=case.pool)
        await kwargs["on_record"](record)
        started.append(row["id"])
        await gates.setdefault(row["id"], asyncio.Event()).wait()
        output = json.dumps(REPORT)
        await llm_records.finish_record(record, status="completed", output_text=output, cost_usd=.02, pool=case.pool)
        return spawn.ChildResult(output=output, record_id=record, cost_usd=.02)
    monkeypatch.setattr(spawn, "run_child", run)
    return gates, started


async def _close(case, *jobs):
    for job in jobs:
        if not job.done():
            job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    await case.facade.stop()


def test_disabled_results_rows_and_launch_match_recorded_items(tmp_path, monkeypatch):
    async def check(pool):
        observed = await _off_observation(pool, tmp_path, monkeypatch)
        fixture = json.loads(FIXTURE.read_text())
        assert fixture["base_sha"] == BASE
        assert observed.keys() == fixture["items"].keys()
        for name, value in observed.items():
            assert value == fixture["items"][name], name
    run_db(check, monkeypatch)


async def _off_observation(pool, root, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/operator")))
    case = await _setup(pool, root, monkeypatch, on=False)
    forbidden = AsyncMock(side_effect=AssertionError("off path touched account admission"))
    monkeypatch.setattr(admission, "parent_for", forbidden)
    monkeypatch.setattr(admission, "slot", forbidden)
    class ForbiddenLock:
        async def __aenter__(self):
            raise AssertionError("off path acquired controller lock")
    monkeypatch.setattr(case.controller, "lock", ForbiddenLock())
    queries = Counter()
    def observe(name):
        original = getattr(type(pool), name)
        async def query(current, sql, *args, **kwargs):
            if current is pool:
                assert "provider_admission" not in sql and "provider_limit_windows" not in sql
                queries[sql] += 1
            return await original(current, sql, *args, **kwargs)
        monkeypatch.setattr(type(pool), name, query)
    for name in ("fetch", "fetchval", "fetchrow", "execute"):
        observe(name)
    case.facade.schedule = lambda spawn_id: None
    first = await _call(case, tools.SPAWN, {"kind": "explorer", "brief": BRIEF}, "complete")
    queued = first["result"]["spawn"]
    spawned = await agent_spawns.get(queued["id"], pool=pool)
    launch = Launch(case.settings, role=AgentType.EXPLORER, access=Access.EXPLORE)
    argv = launch.runtime.run_command("claude", cwd=Path("/workspace"), prompt_file=Path("/runtime/prompt.md"),
                                      launch=launch, max_turns=40)
    async def run(child, **kwargs):
        assert child.system_prompt.text
        observations["system_prompt_sha256"] = hashlib.sha256(child.system_prompt.text.encode()).hexdigest()
        record = await llm_records.start_record(agent_id=child.agent_id, task_id="task", run_kind="explorer",
            parent_record_id=case.parent.record_id, pool=pool, metadata=launch.record())
        await kwargs["on_record"](record)
        await llm_records.finish_record(record, status="completed", output_text=json.dumps(REPORT), cost_usd=.02, pool=pool)
        observations["record_metadata_keys"] = sorted((await llm_records.get_record(record, pool=pool))["metadata"])
        return spawn.ChildResult(output=json.dumps(REPORT), record_id=record, cost_usd=.02)
    observations = {"argv": argv, "spawn_queued": first}
    monkeypatch.setattr(spawn, "run_child", run)
    finished = await case.service.run(queued["id"])
    observations["spawn_finished"] = await _call(case, tools.SPAWN, {"kind":"explorer", "brief":BRIEF}, "complete")
    observations["wait_finished"] = await _call(case, tools.WAIT, {"spawn_ids":[queued["id"]]})
    another = await _call(case, tools.SPAWN, {"kind":"explorer", "brief":BRIEF}, "cancel")
    second = another["result"]["spawn"]["id"]
    observations["wait_queued"] = await _call(case, tools.WAIT, {"spawn_ids":[second]})
    observations["cancel_queued"] = await _call(case, tools.CANCEL, {"spawn_id":second})
    observations["wait_cancelled"] = await _call(case, tools.WAIT, {"spawn_ids":[second]})
    def row_columns(row):
        value = dict(row)
        for key in ("created_at", "finished_at", "delivered_at", "started_at", "updated_at"):
            if key in value:
                value[key] = value[key] is not None
        value["inputs"] = {**value["inputs"], "work_dir":"/workspace"}
        if value.get("child_process"):
            value["child_process"] = {"run_id":"explorer-fixed"}
        return value
    observations["row_after_spawn"] = row_columns(spawned)
    observations["row_after_finish"] = row_columns(finished)
    observations["direct_queries"] = dict(sorted(queries.items()))
    forbidden.assert_not_awaited()
    return observations


@pytest.mark.parametrize("planner,inline", [(False,False), (False,True), (True,False)])
def test_wait_lends_to_one_queued_explorer_and_returns_slot(tmp_path, monkeypatch, planner, inline):
    async def check(pool):
        case = await _setup(pool,tmp_path,monkeypatch,planner=planner)
        gates, started = _runtime(case,monkeypatch)
        await case.controller.register(case.provider,Lane.USER_TASK,case.owner)
        arrivals=[]
        acquire=case.controller.acquire
        async def track_arrival(provider,lane,owner,**kwargs):
            arrivals.append(owner)
            return await acquire(provider,lane,owner,**kwargs)
        monkeypatch.setattr(case.controller,"acquire",track_arrival)
        if planner:
            # Its earlier turn remains the node's parent; the current turn owns the account grant.
            old = case.parent.record_id
            await pool.execute("UPDATE llm_records SET status='completed',finished_at=now() WHERE id=$1",old)
            current = await llm_records.start_record(agent_id=case.owner,task_id="task",pool=pool)
        else:
            current = case.parent.record_id
        row = await case.service.admit(case.parent.caller(),BRIEF,key="first") if not planner else None
        if planner:
            # Admit before switching the parent identity to the new turn.
            await pool.execute("UPDATE llm_records SET status='running',finished_at=NULL WHERE id=$1",old)
            row = await case.service.admit(case.parent.caller(),BRIEF,key="first")
            await pool.execute("UPDATE llm_records SET status='completed',finished_at=now() WHERE id=$1",old)
            case.parent.record_id=current
        second = await case.service.admit(case.parent.caller(),BRIEF,key="second")
        args = {"kind":"explorer","brief":BRIEF,"wait_seconds":3,"key":"first"} if inline else {
            "spawn_ids":[row["id"],second["id"]],"wait_seconds":1}
        waiting=asyncio.create_task(_call(case,tools.SPAWN if inline else tools.WAIT,args,"first"))
        try:
            await until(lambda: started,"first lent explorer")
            first_id=started[0]
            pending_id=next(i for i in (row["id"],second["id"]) if i!=first_id)
            assert case.service.owner_of(first_id)==arrivals[0]
            assert case.owner not in case.controller.running
            assert len(case.controller.running)==1
            assert (await agent_spawns.get(pending_id,pool=pool))["state"]=="queued"
            gates[first_id].set()
            result=await asyncio.wait_for(waiting,5)
            assert result["status"]=="ok",result
            assert case.owner in case.controller.running
            assert not started[1:]
            waiting2=asyncio.create_task(_call(case,tools.WAIT,{"spawn_ids":[pending_id],"wait_seconds":3}))
            await until(lambda: len(started)==2,"second wait lending")
            assert started[1]==pending_id
            gates[pending_id].set()
            assert (await asyncio.wait_for(waiting2,5))["status"]=="ok"
            assert case.owner in case.controller.running
        finally:
            await _close(case,waiting)
    run_db(check,monkeypatch,timeout=20)


@pytest.mark.parametrize("reason", ["requested","operator","task_ended"])
def test_cancel_account_wait_preserves_winning_reason(tmp_path,monkeypatch,reason):
    async def check(pool):
        case=await _setup(pool,tmp_path,monkeypatch)
        case.controller.states[case.provider]=admission.Account(state=admission.AdmissionState.FROZEN,
            until=datetime.now(timezone.utc)+timedelta(hours=1))
        row=await case.service.admit(case.parent.caller(),BRIEF,key="cancel")
        running=asyncio.create_task(case.service.run(row["id"]))
        try:
            await until(lambda: case.controller.waiters or running.done(),"account wait")
            assert len(case.controller.waiters)==1 and not running.done()
            result=await case.service.cancel(row["id"],reason=agent_spawns.CancelReason(reason))
            assert result["cancel_reason"]==reason
            await until(lambda: not case.controller.waiters,"cancelled waiter cleanup")
            assert not case.controller.running
            assert (await agent_spawns.get(row["id"],pool=pool))["cancel_reason"]==reason
        finally:
            await _close(case,running)
    run_db(check,monkeypatch,timeout=10)


def test_task_class_full_precedes_account_wait(tmp_path,monkeypatch):
    async def check(pool):
        case=await _setup(pool,tmp_path,monkeypatch)
        await pool.execute("INSERT INTO task_concurrency_limits(task_id,role,max_concurrency) VALUES('task','subagent',1)")
        active=await case.service.admit(case.parent.caller(),BRIEF,key="active")
        await agent_spawns.move(active["id"],agent_spawns.SpawnState.ASKING,expect=agent_spawns.SpawnState.QUEUED,pool=pool)
        row=await case.service.admit(case.parent.caller(),BRIEF,key="queued")
        slot=AsyncMock(side_effect=AssertionError("full task tried account admission"))
        monkeypatch.setattr(admission,"slot",slot)
        assert (await case.service.run(row["id"]))["state"]=="queued"
        assert not case.controller.running and not case.controller.waiters
        slot.assert_not_called()
    run_db(check,monkeypatch)


@pytest.mark.parametrize("lane",[Lane.USER_TASK,Lane.BACKGROUND])
def test_explorer_queues_in_parent_lane_without_nested_borrow(tmp_path,monkeypatch,lane):
    async def check(pool):
        case=await _setup(pool,tmp_path,monkeypatch,lane=lane)
        gates,started=_runtime(case,monkeypatch)
        if lane is Lane.BACKGROUND:
            from looper.db import provider_limits
            monkeypatch.setattr(provider_limits,"windows",AsyncMock(return_value=[{"utilization":.95}]))
        else:
            await case.controller.register(case.provider,lane,case.owner)
        row=await case.service.admit(case.parent.caller(),BRIEF,key="lane")
        running=asyncio.create_task(case.service.run(row["id"]))
        try:
            await until(lambda: case.controller.waiters or running.done(),"explorer lane wait")
            assert len(case.controller.waiters)==1
            assert case.controller.waiters[0].lane is lane
            assert not started
            assert (await agent_spawns.get(row["id"],pool=pool))["state"]=="queued"
            assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE run_kind='explorer'")==0
        finally:
            await _close(case,running)
    run_db(check,monkeypatch,timeout=10)


def test_frozen_tool_rows_have_cached_hold_and_clear_after_reset(tmp_path,monkeypatch):
    async def check(pool):
        case=await _setup(pool,tmp_path,monkeypatch)
        reset=datetime.now(timezone.utc)+timedelta(hours=1)
        case.controller.states[case.provider]=admission.Account(state=admission.AdmissionState.FROZEN,
            until=reset+timedelta(seconds=60),until_known=True)
        case.facade.schedule=lambda spawn_id: None
        rows=[await case.service.admit(case.parent.caller(),BRIEF,key=str(i)) for i in range(3)]
        await pool.execute("UPDATE agent_spawns SET profile_id='synthetic-same-provider-profile' WHERE id=$1",
                           rows[-1]["id"])
        from looper.db import profile_registry
        lookup=AsyncMock(return_value={"provider_id":case.provider})
        held=Mock(wraps=admission.hold)
        monkeypatch.setattr(profile_registry,"get_profile",lookup)
        monkeypatch.setattr(admission,"hold",held)
        result=await _call(case,tools.WAIT,{"spawn_ids":[r["id"] for r in rows]})
        assert lookup.await_count==2  # One lookup per profile, including two rows of the same profile.
        assert held.call_count==1  # Both profiles share one account snapshot.
        for row in result["result"]["spawns"]:
            assert row["state"]=="queued" and row["held"]=="quota"
            assert row["resets_at"]==reset.isoformat()
        case.controller.states[case.provider]=admission.Account()
        result=await _call(case,tools.WAIT,{})
        assert all("held" not in row and "resets_at" not in row for row in result["result"]["spawns"])
        assert lookup.await_count==4 and held.call_count==2  # The next call reads a fresh snapshot.
    run_db(check,monkeypatch)


def test_nonblocking_wait_does_not_lend(tmp_path,monkeypatch):
    async def check(pool):
        case=await _setup(pool,tmp_path,monkeypatch)
        row=await case.service.admit(case.parent.caller(),BRIEF,key="zero")
        case.facade.schedule=lambda spawn_id: None
        forbidden=AsyncMock(side_effect=AssertionError("nonblocking wait attempted lending"))
        monkeypatch.setattr(admission,"parent_for",forbidden)
        assert (await _call(case,tools.WAIT,{"spawn_ids":[row["id"]],"wait_seconds":0}))["status"]=="ok"
        forbidden.assert_not_awaited()
    run_db(check,monkeypatch)


def test_class_precheck_error_fails_open_with_error_log(tmp_path,monkeypatch,caplog):
    async def check(pool):
        case=await _setup(pool,tmp_path,monkeypatch)
        gates,started=_runtime(case,monkeypatch)
        row=await case.service.admit(case.parent.caller(),BRIEF,key="precheck-error")
        original=type(pool).fetchval
        async def failed_precheck(current,query,*args,**kwargs):
            if current is pool and "task_concurrency_limits" in query:
                raise RuntimeError("synthetic precheck read failure")
            return await original(current,query,*args,**kwargs)
        monkeypatch.setattr(type(pool),"fetchval",failed_precheck)
        slot=AsyncMock(side_effect=AssertionError("failed precheck must skip account admission"))
        monkeypatch.setattr(admission,"slot",slot)
        running=asyncio.create_task(case.service.run(row["id"]))
        try:
            await until(lambda: started or running.done(),"fail-open child launch")
            assert started==[row["id"]]
            gates[row["id"]].set()
            assert (await asyncio.wait_for(running,5))["state"]=="answered"
            assert any(record.levelname=="ERROR" and "admission" in record.message.lower()
                       for record in caplog.records)
            slot.assert_not_called()
        finally:
            await _close(case,running)
    run_db(check,monkeypatch,timeout=10)


def test_task_class_recheck_releases_slot_after_account_queue(tmp_path,monkeypatch):
    async def check(pool):
        case=await _setup(pool,tmp_path,monkeypatch)
        await pool.execute("INSERT INTO task_concurrency_limits(task_id,role,max_concurrency) VALUES('task','subagent',1)")
        await case.controller.register(case.provider,Lane.USER_TASK,case.owner)
        active=await case.service.admit(case.parent.caller(),BRIEF,key="other")
        row=await case.service.admit(case.parent.caller(),BRIEF,key="race")
        running=asyncio.create_task(case.service.run(row["id"]))
        try:
            await until(lambda: case.controller.waiters or running.done(),"queued before task lock")
            assert len(case.controller.waiters)==1
            # The queued account waiter must not retain the task's transaction lock.
            async with asyncio.timeout(1):
                async with agent_spawns.task_lock("task",pool=pool):
                    pass
            await agent_spawns.move(active["id"],agent_spawns.SpawnState.ASKING,
                expect=agent_spawns.SpawnState.QUEUED,pool=pool)
            await case.controller.release(case.owner)
            assert (await asyncio.wait_for(running,5))["state"]=="queued"
            assert not case.controller.running and not case.controller.waiters
        finally:
            await _close(case,running)
    run_db(check,monkeypatch,timeout=10)


def test_drain_does_not_admit_explorers(tmp_path,monkeypatch):
    async def check(pool):
        case=await _setup(pool,tmp_path,monkeypatch)
        case.controller.draining=lambda:True
        row=await case.service.admit(case.parent.caller(),BRIEF,key="drain")
        running=asyncio.create_task(case.service.run(row["id"]))
        try:
            await until(lambda: case.controller.waiters or running.done(),"drained explorer")
            assert len(case.controller.waiters)==1 and not case.controller.running
            assert (await agent_spawns.get(row["id"],pool=pool))["state"]=="queued"
        finally:
            await _close(case,running)
    run_db(check,monkeypatch,timeout=10)


def test_wait_defers_lending_until_delayed_explorer_identity_is_ready(tmp_path,monkeypatch):
    async def check(pool):
        case=await _setup(pool,tmp_path,monkeypatch)
        await pool.execute("INSERT INTO task_concurrency_limits(task_id,role,max_concurrency) VALUES('task','subagent',1)")
        await case.controller.register(case.provider,Lane.USER_TASK,case.owner)
        active=await case.service.admit(case.parent.caller(),BRIEF,key="occupies-task-class")
        await agent_spawns.move(active["id"],agent_spawns.SpawnState.ASKING,
            expect=agent_spawns.SpawnState.QUEUED,pool=pool)
        first=await case.service.admit(case.parent.caller(),BRIEF,key="class-full")
        second=await case.service.admit(case.parent.caller(),BRIEF,key="delayed-build")
        gates,started=_runtime(case,monkeypatch)
        allow_first,allow_second,polled=asyncio.Event(),asyncio.Event(),asyncio.Event()
        load_settings=subagents.load_agent_settings
        async def delayed_settings(*args,**kwargs):
            if asyncio.current_task() is case.service._running.get(second["id"]):
                await allow_second.wait()
            return await load_settings(*args,**kwargs)
        monkeypatch.setattr(subagents,"load_agent_settings",delayed_settings)
        original_fetchval=type(pool).fetchval
        async def held_precheck(current,query,*args,**kwargs):
            if (current is pool and "count(*) FROM agent_spawns" in query
                    and asyncio.current_task() is case.service._running.get(first["id"])):
                await allow_first.wait()
            return await original_fetchval(current,query,*args,**kwargs)
        monkeypatch.setattr(type(pool),"fetchval",held_precheck)
        owned=case.facade._owned
        async def observed_rows(*args,**kwargs):
            rows=await owned(*args,**kwargs)
            if case.service.owner_of(first["id"]) and not case.service.owner_of(second["id"]):
                polled.set()
            return rows
        monkeypatch.setattr(case.facade,"_owned",observed_rows)
        lend=admission.lend
        lent=[]
        @asynccontextmanager
        async def observed_lend(parent,children):
            lent.append(list(children))
            async with lend(parent,children):
                yield
        monkeypatch.setattr(admission,"lend",observed_lend)
        waiting=asyncio.create_task(_call(case,tools.WAIT,{
            "spawn_ids":[first["id"],second["id"]],"wait_seconds":2}))
        try:
            await asyncio.wait_for(polled.wait(),3)
            await asyncio.sleep(0)
            assert not lent,"an unfinished build must not register a partial lending intent"
            allow_first.set()
            await until(lambda: first["id"] not in case.facade._jobs,"full-class explorer returned queued")
            assert (await agent_spawns.get(first["id"],pool=pool))["state"]=="queued"
            await case.service.cancel(active["id"])
            allow_second.set()
            await until(lambda: started or waiting.done(),"delayed explorer receives lent slot")
            assert started==[second["id"]]
            assert lent==[[case.service.owner_of(second["id"])]]
            gates[second["id"]].set()
            result=await asyncio.wait_for(waiting,5)
            assert [row["state"] for row in result["result"]["spawns"]]==["queued","answered"]
            assert case.owner in case.controller.running
        finally:
            allow_first.set()
            allow_second.set()
            await _close(case,waiting)
    run_db(check,monkeypatch,timeout=15)


def test_wait_does_not_lend_to_already_asking_explorer(tmp_path,monkeypatch):
    async def check(pool):
        case=await _setup(pool,tmp_path,monkeypatch)
        await case.controller.register(case.provider,Lane.USER_TASK,case.owner)
        row=await case.service.admit(case.parent.caller(),BRIEF,key="already-started")
        await agent_spawns.move(row["id"],agent_spawns.SpawnState.ASKING,
            expect=agent_spawns.SpawnState.QUEUED,pool=pool)
        case.service._homes[row["id"]]="explorer-already-started"
        case.facade.schedule=lambda spawn_id:None
        forbidden=AsyncMock(side_effect=AssertionError("asking explorer attempted lending"))
        monkeypatch.setattr(admission,"lend",forbidden)
        monkeypatch.setattr(admission,"parent_for",forbidden)
        try:
            result=await _call(case,tools.WAIT,{"spawn_ids":[row["id"]],"wait_seconds":1})
            assert result["status"]=="ok" and result["result"]["spawns"][0]["state"]=="asking"
            assert set(case.controller.running)=={case.owner}
            forbidden.assert_not_called()
        finally:
            await _close(case)
    run_db(check,monkeypatch,timeout=10)


def test_restart_scanner_restores_frozen_queue_and_runs_explorer_at_reset(tmp_path, monkeypatch):
    async def check(pool):
        case = await _setup(pool, tmp_path, monkeypatch, lane=Lane.BACKGROUND, planner=True)
        now = datetime.now(timezone.utc)
        reset = now + timedelta(minutes=2)
        await case.controller.transition(case.provider, admission.QuotaFailure(reset, None))
        row = await case.service.admit(case.parent.caller(), BRIEF, key="queued-before-restart")
        await pool.execute("UPDATE tasks SET status='executing' WHERE id='task'")
        await llm_records.finish_record(case.parent.record_id, status="completed", pool=pool)
        restored = admission.Controller(now=lambda: now)
        restored.active = True
        await restored.load_states()
        case.controller = restored
        monkeypatch.setattr(admission, "_controller", restored)
        for name in ("slot", "register", "release", "lend", "hold"):
            monkeypatch.setattr(admission, name, getattr(restored, name))
        gates, started = _runtime(case, monkeypatch)
        try:
            await case.facade.start()
            await until(lambda: restored.waiters, "restarted explorer account queue")
            assert restored.waiters[0].lane is Lane.BACKGROUND
            assert not started and not restored.running
            held = await _call(case, tools.WAIT, {"spawn_ids": [row["id"]]})
            queued = held["result"]["spawns"][0]
            assert queued["state"] == "queued" and queued["held"] == "quota"
            assert queued["resets_at"] == reset.isoformat()
            now = reset + timedelta(seconds=60)
            await restored.wake()
            await until(lambda: started, "explorer probe after reset")
            assert started == [row["id"]]
            gates[row["id"]].set()
            await until(lambda: not case.facade._jobs, "probe explorer completion")
            assert (await agent_spawns.get(row["id"], pool=pool))["state"] == "answered"
            assert restored.states[case.provider].state is admission.AdmissionState.OPEN
            assert admission.hold(case.provider) is None
            assert not restored.waiters and not restored.running
        finally:
            await _close(case)
    run_db(check, monkeypatch, timeout=15)


@pytest.mark.parametrize("account_admission", [False, True])
@pytest.mark.parametrize("phase", ["planning", "executing", "completed"])
def test_explorer_admission_and_launch_use_shared_active_task_phases(
    tmp_path, monkeypatch, account_admission, phase
):
    async def check(pool):
        case = await _setup(pool, tmp_path, monkeypatch, on=account_admission, planner=phase == "planning")
        await pool.execute("UPDATE tasks SET status=$1 WHERE id='task'", phase)
        if phase == "completed":
            with pytest.raises(subagents.Refused) as refused:
                await case.service.admit(case.parent.caller(), BRIEF, key="phase")
            assert refused.value.reason is subagents.RefusalReason.ROLE
            assert not case.controller.waiters and not case.controller.running
            return
        gates, started = _runtime(case, monkeypatch)
        row = await case.service.admit(case.parent.caller(), BRIEF, key="phase")
        running = asyncio.create_task(case.service.run(row["id"]))
        try:
            await until(lambda: started or running.done(), "explorer in active task phase")
            assert started == [row["id"]]
            gates[row["id"]].set()
            assert (await asyncio.wait_for(running, 5))["state"] == "answered"
        finally:
            await _close(case, running)
    run_db(check, monkeypatch, timeout=10)
