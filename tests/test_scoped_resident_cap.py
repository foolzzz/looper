"""Scoped starts share machine capacity; legacy routing and operator settings retain their boundaries."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, chat_settings, session_routing
from looper.agents.action_levels import ActionLevel
from looper.agents.managers_api import ScenarioSessionRouting
from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.tiers import manager_routing
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig, default_definition
from looper.agents.tiers.manager_session import Retirement
from looper.db import system_settings
from tests.db_support import run_db, until, v1_api


class Resident:
    def __init__(self, started=True, stops=True):
        self.started, self.stops = started, stops
        self.attempts = 0

    async def try_stop_if_idle(self):
        self.attempts += 1
        if isinstance(self.stops, Exception):
            raise self.stops
        if self.stops:
            self.started = False
        return self.stops

    async def stop(self):
        self.started = False


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_routing, "pool", manager_routing.ResidentPool())
    monkeypatch.setattr(session_routing, "_scenario_routing", {})
    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    monkeypatch.setattr(session_routing, "_max_resident", 2)
    monkeypatch.setattr(session_routing, "_max_resident_total", 3)
    monkeypatch.setattr(session_routing, "_idle_reclaim_seconds", 1800)

    def make(name="scoped-a", mode="scoped"):
        definition = replace(default_definition(), id=name, name=name, idle_timeout_seconds=0,
                             session_routing=ScenarioSessionRouting(mode))
        agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path,
                             definition=definition, clock=lambda: 0.0)
        agent._sessions["main"].claude = Resident()
        agent._send_status = AsyncMock()
        agent._running = True
        return agent

    return make


def group(agent, name="group-a", *, started=True, activity=0, stops=True):
    session = agent._router.session(SessionKey(SessionKind.GROUP, "channel-synthetic", name))
    session.claude = Resident(started, stops)
    session.session_id = "session-" + name
    session.last_activity = activity
    return session


def message(agent):
    return MessageEnvelope.create(source=channel_address("web"), target=manager_address(agent.agent_id),
                                  message_type=MessageType.USER_MESSAGE, payload={"text": "start"})


@pytest.mark.parametrize("name,mode", [("scoped-a", "scoped"), ("per-chat", "per_chat"),
                                       ("manager", "off"), ("unrouted", "off")])
def test_cap_spans_managers(fleet, name, mode):
    async def check():
        a, b = fleet(name, mode), fleet("scoped-b")
        old = group(a, activity=10)
        new = group(b, started=False, activity=20)
        await asyncio.wait_for(b._router.admit(new, message(b)), 1)
        assert not old.claude.started and old.session_id == "session-group-a"
        assert a._sessions["main"].claude.started and b._sessions["main"].claude.started
        assert manager_routing.pool.count(exclude=new) == 2
        # A successful start releases its reservation without losing its place in the count.
        new.claude.started = True
        await b._router.release(new)
        assert manager_routing.pool.count(exclude=old) == 3
    asyncio.run(check())


@pytest.mark.parametrize("cap,stopped", [(2, True), (4, False)])
def test_scoped_turn_end_uses_global_cap(fleet, cap, stopped):
    async def check():
        a, b = fleet(), fleet("scoped-b")
        ended = group(a)
        session_routing.set_max_resident_total(cap)
        session_routing.set_max_resident(1)
        await a._router._turn_ended(ended)
        assert ended.claude.started is not stopped
        assert b._sessions["main"].claude.started
    asyncio.run(check())


@pytest.mark.parametrize("stops", [False, RuntimeError("runtime refused")])
def test_failed_victim_tries_next_oldest(fleet, stops):
    async def check():
        a, b = fleet(), fleet("scoped-b")
        oldest = group(a, "oldest", activity=1, stops=stops)
        next_oldest = group(a, "next", activity=2)
        recent = group(b, "recent", activity=3)
        new = group(b, "new", started=False)
        session_routing.set_max_resident_total(5)
        await asyncio.wait_for(b._router.admit(new, None), 1)
        assert oldest.claude.started and oldest.claude.attempts == 1
        assert not next_oldest.claude.started and recent.claude.started
        await b._router.release(new)
    asyncio.run(check())


@pytest.mark.parametrize("busy_turn", [True, False])
def test_waits_once_when_none_idle(fleet, busy_turn):
    async def check():
        a, b = fleet(), fleet("scoped-b")
        busy = group(a, stops=busy_turn)
        if busy_turn:
            await busy.turn_lock.acquire()
        new = group(b, started=False)
        envelope = message(b)
        waiting = asyncio.create_task(b._router.admit(new, envelope))
        try:
            await until(lambda: b._send_status.await_count == 1, timeout=1)
            await a._router.changed()  # spurious wake does not send another status
            await asyncio.sleep(0)
            assert not waiting.done()
            if busy_turn:
                busy.turn_lock.release()
            else:
                busy.claude.stops = True
            await a._router._turn_ended(busy)
            await asyncio.wait_for(waiting, 1)
            b._send_status.assert_awaited_once_with(envelope, manager_routing.WAITING_STATUS, phase=True)
            assert not busy.claude.started
            manager_id, seconds = manager_routing.pool.waits[-1]
            assert manager_id == b.agent_id and seconds > 0
            assert manager_routing.pool.waits.maxlen == 256
            await b._router.release(new)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    asyncio.run(check())


def test_main_never_waits_or_is_stopped(fleet):
    async def check():
        a, b = fleet(), fleet("scoped-b")
        session_routing.set_max_resident_total(2)
        new = group(b, started=False)
        waiting = asyncio.create_task(b._router.admit(new, message(b)))
        try:
            await until(lambda: b._send_status.await_count == 1, timeout=1)
            assert not waiting.done()  # two main residents consume the entire cap
            assert all(s.claude.attempts == 0 for s in (a._sessions["main"], b._sessions["main"]))
            c = fleet("scoped-c")
            main = c._sessions["main"]
            main.claude.started = False
            await asyncio.wait_for(c._router.admit(main, message(c)), 1)
            main.claude.started = True
            await c._router.release(main)
            await c._router._turn_ended(main)
            assert main.claude.started and not c._send_status.called
            assert not waiting.done()
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            await b._router.release(new)
    asyncio.run(check())


@pytest.mark.parametrize("name,mode", [("manager", "off"), ("per-chat", "per_chat")])
def test_unscoped_managers_keep_their_cap(fleet, name, mode):
    async def check():
        agent = fleet(name, mode)
        session_routing.set_max_resident_total(1)
        first = group(agent)
        new = group(agent, "new", started=False)
        await asyncio.wait_for(agent._router.admit(new, None), 1)
        assert not first.claude.started  # the per-Manager cap of 2 still evicts at its boundary
        await agent._router.release(new)
        session_routing.set_max_resident(3)
        first.claude.started = True
        await asyncio.wait_for(agent._router.admit(new, None), 1)
        assert first.claude.started  # machine cap of 1 does not govern legacy admissions
        await agent._router.release(new)
    asyncio.run(check())


@pytest.mark.parametrize("mode,timeout,now,reclaimed", [
    ("scoped", 1800, 1799, False), ("scoped", 1800, 1800, True),
    ("scoped", 0, 86400, False), ("per_chat", 1800, 86400, False),
])
def test_idle_reclaim(fleet, mode, timeout, now, reclaimed):
    async def check():
        agent = fleet(mode=mode)
        session_routing.set_idle_reclaim_seconds(timeout)
        old = group(agent, "old")
        busy = group(agent, "busy")
        await busy.turn_lock.acquire()
        retired = group(agent, "retired")
        retired.retired = Retirement.SCOPE_CHANGED
        await agent.try_suspend_idle(now)
        assert old.claude.started is not reclaimed
        assert old.session_id == "session-old"
        assert busy.claude.started and retired.claude.started
        assert agent._sessions["main"].claude.started
        busy.turn_lock.release()
    asyncio.run(check())


@pytest.mark.parametrize("finished", ["release", "changed", "turn_ended"])
def test_reservations_and_process_changes_wake_other_managers(fleet, finished):
    async def check():
        a, b = fleet(), fleet("scoped-b")
        pending = group(a, "pending", started=False)
        await a._router.admit(pending, None)
        new = group(b, "new", started=False)
        waiting = asyncio.create_task(b._router.admit(new, message(b)))
        try:
            await until(lambda: b._send_status.await_count == 1, timeout=1)
            assert not waiting.done()
            if finished == "release":
                await a._router.release(pending)  # failed or cancelled start frees its reservation
            else:
                pending.claude.started = True
                await a._router.release(pending)
                pending.claude.started = False
                if finished == "changed":
                    await a._router.changed()
                else:
                    await a._router._turn_ended(pending)
            await asyncio.wait_for(waiting, 1)
            await b._router.release(new)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    asyncio.run(check())


@pytest.mark.parametrize("stop", ["idle_suspend", "shutdown"])
def test_main_process_stop_wakes_scoped_start(fleet, stop):
    async def check():
        a, b = fleet("per-chat", "per_chat"), fleet("scoped-b")
        a._definition = replace(a._definition, idle_timeout_seconds=60)
        session_routing.set_max_resident_total(2)
        new = group(b, started=False)
        waiting = asyncio.create_task(b._router.admit(new, message(b)))
        try:
            await until(lambda: b._send_status.await_count == 1, timeout=1)
            if stop == "idle_suspend":
                assert await a.try_suspend_idle(60)
            else:
                await a.stop()
            await asyncio.wait_for(waiting, 1)
            await b._router.release(new)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    asyncio.run(check())


@pytest.mark.parametrize("key,valid,invalid,default", [
    ("max_resident_total", 32, "33", 6), ("idle_reclaim_seconds", 60, "59", 1800),
])
def test_stored_limits_loaded_and_invalid_values_warn(fleet, monkeypatch, caplog, key, valid, invalid, default):
    setting = "manager.sessions." + key
    async def check():
        read = AsyncMock(return_value={setting: str(valid)})
        monkeypatch.setattr(system_settings, "get_settings", read)
        await chat_settings.load()
        assert setting in read.call_args.args[0]
        assert getattr(session_routing, key)() == valid
        for value in (invalid, "broken"):
            read.return_value = {setting: value}
            await chat_settings.load()
            assert getattr(session_routing, key)() == default
        assert setting in caplog.text
    asyncio.run(check())


def test_resident_limits_operations(fleet, tmp_path, monkeypatch):
    async def check(pool):
        session_routing.apply_stored({})
        defaults = {"max_resident_total": 6, "idle_reclaim_seconds": 1800}
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("resident_limits.get")
            assert status == 200 and answer["result"] == defaults
            for body in ({}, {"max_resident_total": 0}, {"max_resident_total": 33},
                         {"idle_reclaim_seconds": 59}, {"max_resident_total": True},
                         {"idle_reclaim_seconds": True}, {"idle_reclaim_seconds": 86401}):
                status, _ = await call("resident_limits.update", body)
                assert status == 400, body
            assert await system_settings.get_settings(list((
                session_routing.MAX_RESIDENT_TOTAL_KEY, session_routing.IDLE_RECLAIM_KEY))) == {}
            for body, expected in (({"max_resident_total": 1, "idle_reclaim_seconds": 0},
                                    {"max_resident_total": 1, "idle_reclaim_seconds": 0}),
                                   ({"max_resident_total": 32}, {"max_resident_total": 32, "idle_reclaim_seconds": 0}),
                                   ({"idle_reclaim_seconds": 86400},
                                    {"max_resident_total": 32, "idle_reclaim_seconds": 86400}),
                                   ({"max_resident_total": None, "idle_reclaim_seconds": None}, defaults)):
                status, answer = await call("resident_limits.update", body)
                assert status == 200 and answer["result"] == expected, answer
                await chat_settings.load()
                assert session_routing.max_resident_total() == expected["max_resident_total"]
                assert session_routing.idle_reclaim_seconds() == expected["idle_reclaim_seconds"]
                assert (await call("resident_limits.get"))[1]["result"] == expected
            for action in ("get", "update"):
                op = api_v1.find("POST", f"/api/v1/resident_limits/{action}")
                assert op.operator_only and op.tool is None and op.level is ActionLevel.INSIDE
                status, _ = await call(f"resident_limits.{action}", {"max_resident_total": 2} if action == "update"
                                       else {}, client=True)
                assert status == 403
            op = api_v1.find("POST", "/api/v1/resident_limits/update")
            assert op.risk == "high" and op.confirms
            impact = await api_v1.domain_for(op).impact(None, op,
                {"max_resident_total": 2, "idle_reclaim_seconds": None}, api_v1.OPERATOR, {})
            assert "2" in impact and "1800" in impact and "scoped" in impact
        assert await system_settings.get_settings([
            session_routing.MAX_RESIDENT_TOTAL_KEY, session_routing.IDLE_RECLAIM_KEY]) == {}
    run_db(check, monkeypatch, timeout=30)
