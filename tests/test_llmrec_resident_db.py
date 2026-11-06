"""Resident turn records own requests while keeping one route and launch signature."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

import httpx
import pytest

from fake_claude import fake_claude
from tests.db_support import DSN, run_db
from tests.fake_model_api import FakeModelAPI, ResponseScript
from tests.test_llmrec_worker_db import _EVENTS
from tests.test_profile_registry_db import _ok, _project_of
from tests.test_provider_binding_db import KEY, _profile, _provider, _task

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


@asynccontextmanager
async def _recording(pool, root, *, enabled=True, include_oauth=False):
    from looper.agents.llmrec import control
    from looper.agents.llmrec.proxy import Proxy
    from looper.agents.llmrec.writer import DbSink

    sink = DbSink(pool)
    proxy = Proxy(root, sink=sink)
    port = await proxy.start()
    writer = asyncio.create_task(sink.run())

    class LocalControl(control.ProxyControl):
        def _state(self):
            return {"port": port, "pid": os.getpid(), "instance": proxy.instance}

        async def _health(self, *, cached=False):
            return proxy.stats

    control.install(LocalControl(root, "", enabled=enabled, include_oauth=include_oauth))
    try:
        yield proxy, sink
    finally:
        control.install(None)
        await proxy.close()
        await sink.close(timeout=5)
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


def _resident_executable(root: Path) -> str:
    from tests.agent_launch_parity import _CLAUDE

    body = _CLAUDE.format(out=str(root / "launch.json"))
    body = body.replace("import json, os, sys", "import json, os, sys, urllib.request")
    request = """
    body = json.dumps({'model': 'haiku', 'system': 'resident test system',
                       'messages': [{'role': 'user', 'content': message['message']['content']}],
                       'stream': True}).encode()
    req = urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'].rstrip('/') + '/v1/messages', body,
        {'content-type': 'application/json', 'authorization': 'Bearer ' + os.environ['ANTHROPIC_AUTH_TOKEN']})
    with urllib.request.urlopen(req, timeout=10) as response:
        response.read()
"""
    body = body.replace("    mid = message.get('uuid')", request + "    mid = message.get('uuid')")
    return fake_claude(root / "claude", body)


@pytest.mark.parametrize("role", ["manager", "planner"])
def test_resident_turns_keep_the_route_signature_and_between_turn_attribution(
    monkeypatch, tmp_path, role,
):
    from looper.agents.bus import MessageBus
    from looper.agents.llmrec import LlmrecRoute, ROUTES_DIR, RouteFile
    from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.agents.tiers.planner import PlannerAgent, TaskPlan
    from looper.db import llm_requests

    async def body(pool):
        scripts = [ResponseScript(events=_EVENTS) for _ in range(3)]
        async with FakeModelAPI(scripts, api_key=KEY) as upstream:
            base = upstream.base_url.removesuffix("/v1")
            executable = _resident_executable(tmp_path)
            provider = await _provider(base_url=base)
            profile = (await _profile(provider, "haiku"))["profile"]["profile_id"]
            await _ok("PUT", f"/api/agent-settings/roles/{role}", {"profile_id": profile})
            task = await _task(tmp_path)
            work = tmp_path / "w"
            work.mkdir(exist_ok=True)
            root = tmp_path / "runtime"
            bus = MessageBus(root)
            if role == "manager":
                agent = ManagerAgent(config=ManagerConfig(claude_bin=executable, work_dir=work), runtime_root=root)
                agent._bus, agent._clean_start = bus, True

                async def turn(index):
                    envelope = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
                        message_type=MessageType.USER_MESSAGE, payload={"text": "Reply briefly."},
                        metadata={"task_id": task})
                    return await agent._run_turn("Reply briefly.", envelope)
            else:
                agent = PlannerAgent(task_id=task, runtime_root=root, claude_bin=executable)
                agent._plan = TaskPlan(plan_id="request-recording-plan", goal_id="", requirement="Reply briefly.",
                                      work_dir=str(work), project_id=await _project_of(task))

                async def turn(index):
                    return await agent._run_turn("Reply briefly.")

            try:
                async with _recording(pool, root) as (proxy, sink):
                    signatures, routes, pids = [], [], []
                    for index in range(2):
                        assert await asyncio.wait_for(turn(index), 15)
                        process = agent._process
                        assert isinstance(process.launch.llmrec, LlmrecRoute)
                        signatures.append(process.signature)
                        routes.append(process.launch.llmrec)
                        pids.append(agent._claude.pid)
                        if index == 0:
                            route_path = root / ROUTES_DIR / f"{routes[0].id}.json"
                            route = RouteFile.read(route_path)
                            assert route.record_id is None and route.last_record_id is not None
                            assert route.ended_at is None
                            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                                response = await client.post(routes[0].url + "/v1/messages",
                                    headers={"authorization": f"Bearer {KEY}"},
                                    json={"model": "haiku", "messages": [{"role": "user", "content": "between"}],
                                          "stream": True})
                                response.raise_for_status()
                    assert signatures[0] == signatures[1]
                    assert routes[0] == routes[1]
                    assert pids[0] is not None and pids[0] == pids[1]
                    await proxy.close()
                    await sink.close(timeout=5)
                    records = await pool.fetch("SELECT id, metadata FROM llm_records WHERE agent_id=$1 ORDER BY id",
                                               agent.agent_id)
                    assert len(records) == 2
                    assert all(json.loads(row["metadata"])["llmrec"]["route"] == routes[0].id for row in records)
                    for record in records:
                        requests = await llm_requests.list_requests(record_id=record["id"], pool=pool)
                        assert len(requests) == 1
                    requests = await pool.fetch("SELECT record_id, after_record_id, route_id FROM llm_requests "
                                                "ORDER BY id")
                    assert [(row["record_id"], row["after_record_id"]) for row in requests] == [
                        (records[0]["id"], None), (None, records[0]["id"]), (records[1]["id"], None)]
                    assert {row["route_id"] for row in requests} == {routes[0].id}
                    assert len(upstream.requests) == 3
            finally:
                await agent._claude.stop()
                await bus.shutdown()

    run_db(body, monkeypatch, timeout=50)


@asynccontextmanager
async def _change_recording(root, sink, transition, monkeypatch):
    from looper.agents.llmrec import control
    from looper.agents.llmrec.proxy import Proxy

    controller = control.controller()
    replacement = None
    try:
        if transition in {"enable", "disable"}:
            await controller.set_enabled(transition == "enable")
        elif transition == "bypass":
            async def unavailable(*args, **kwargs):
                return None

            monkeypatch.setattr(controller, "_health", unavailable)
            monkeypatch.setattr(controller, "_start", unavailable)
        else:
            replacement = Proxy(root, sink=sink)
            port = await replacement.start()
            monkeypatch.setattr(controller, "_state", lambda: {
                "port": port, "pid": os.getpid(), "instance": replacement.instance})
        yield
    finally:
        if replacement is not None:
            await replacement.close()


@pytest.mark.parametrize("role", ["manager", "planner"])
@pytest.mark.parametrize("transition", ["enable", "disable", "bypass", "port"])
def test_claude_resident_restarts_once_when_recording_route_changes(monkeypatch, tmp_path, role, transition):
    from looper.agents.bus import MessageBus
    from looper.agents.llmrec import LlmrecRoute
    from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.agents.tiers.planner import PlannerAgent, TaskPlan

    async def body(pool):
        async with FakeModelAPI([ResponseScript(events=_EVENTS) for _ in range(3)], api_key=KEY) as upstream:
            executable = _resident_executable(tmp_path)
            provider = await _provider(base_url=upstream.base_url.removesuffix("/v1"))
            profile = (await _profile(provider, "haiku"))["profile"]["profile_id"]
            await _ok("PUT", f"/api/agent-settings/roles/{role}", {"profile_id": profile})
            task = await _task(tmp_path)
            work = tmp_path / "w"
            work.mkdir(exist_ok=True)
            root = tmp_path / "runtime"
            bus = MessageBus(root)
            if role == "manager":
                agent = ManagerAgent(config=ManagerConfig(claude_bin=executable, work_dir=work), runtime_root=root)
                agent._bus, agent._clean_start = bus, True

                async def turn():
                    envelope = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
                        message_type=MessageType.USER_MESSAGE, payload={"text": "Reply briefly."})
                    return await agent._run_turn("Reply briefly.", envelope)
            else:
                agent = PlannerAgent(task_id=task, runtime_root=root, claude_bin=executable)
                agent._plan = TaskPlan(plan_id="recording-toggle-plan", goal_id="", requirement="Reply briefly.",
                                      work_dir=str(work), project_id=await _project_of(task))

                async def turn():
                    return await agent._run_turn("Reply briefly.")

            try:
                async with _recording(pool, root, enabled=transition != "enable") as (proxy, sink):
                    assert await asyncio.wait_for(turn(), 15)
                    initial_pid, initial_signature = agent._claude.pid, agent._process.signature
                    initial_route = agent._process.launch.llmrec
                    async with _change_recording(root, sink, transition, monkeypatch):
                        assert await asyncio.wait_for(turn(), 15)
                        changed_pid, changed_signature = agent._claude.pid, agent._process.signature
                        assert changed_pid is not None and changed_pid != initial_pid
                        assert changed_signature != initial_signature
                        route = agent._process.launch.llmrec
                        assert isinstance(route, LlmrecRoute) == (transition in {"enable", "port"})
                        if transition == "port":
                            assert route.id == initial_route.id and route.url != initial_route.url
                        assert await asyncio.wait_for(turn(), 15)
                        assert agent._claude.pid == changed_pid
                        assert agent._process.signature == changed_signature
                    await proxy.close()
                    await sink.close(timeout=5)
                    records = await pool.fetch("SELECT id, metadata FROM llm_records WHERE agent_id=$1 ORDER BY id",
                                               agent.agent_id)
                    assert len(records) == len(upstream.requests) == 3
                    expected = [transition != "enable", transition in {"enable", "port"},
                                transition in {"enable", "port"}]
                    requests = await pool.fetch("SELECT record_id, after_record_id FROM llm_requests ORDER BY id")
                    assert [(row["record_id"], row["after_record_id"]) for row in requests] == [
                        (row["id"], None) for row, recorded in zip(records, expected, strict=True) if recorded]
                    for row, recorded in zip(records, expected, strict=True):
                        metadata = json.loads(row["metadata"])
                        assert (metadata.get("llmrec", {}).get("state") == "recording") == recorded
            finally:
                await agent._claude.stop()
                await bus.shutdown()

    run_db(body, monkeypatch, timeout=60)


@pytest.mark.parametrize("execution", ["process", "hosted"])
def test_consult_request_belongs_to_its_record_and_closes_the_route(monkeypatch, tmp_path, execution):
    from looper import child_env
    from looper.agents.llmrec import ROUTES_DIR, RouteFile
    from looper.agents.room import RoomService
    from tests.db_support import until
    from tests.run_handle_support import ScriptedRun
    from tests.test_room_consult_db import _CONSULTED, _host_consults, _question, _ran, _service, _settled
    from tests.test_room_db import _Pool, _room_task

    # The consult executable must use a throwaway session store, including its fork cleanup.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("SEEN", str(tmp_path / "consult.jsonl"))
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {"SEEN"})

    async def body(pool):
        async with FakeModelAPI([ResponseScript(events=_EVENTS)], api_key=KEY) as upstream:
            monkeypatch.setenv("ANTHROPIC_BASE_URL", upstream.base_url.removesuffix("/v1"))
            await _room_task(pool)
            await _ran(pool, tmp_path / "work")
            question = await _question(pool, "qty?")
            if execution == "hosted":
                class HttpRun(ScriptedRun):
                    async def start(self, prompt):
                        await super().start(prompt)
                        route = calls[0]["launch"].llmrec
                        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                            response = await client.post(route.url + "/v1/messages",
                                headers={"authorization": f"Bearer {KEY}"},
                                json={"model": "haiku", "messages": [{"role": "user", "content": prompt}],
                                      "stream": True})
                            response.raise_for_status()

                handle = HttpRun([
                    {"type": "system", "subtype": "init", "session_id": "consult-fork"},
                    {"type": "result", "subtype": "success", "total_cost_usd": 0.01,
                     "result": json.dumps({"answers": [{"reply_to": question["id"], "body": "the field is qty"}]})},
                ])
                calls = _host_consults(monkeypatch, handle)
            room = RoomService(batch_seconds=0)
            service = _service(tmp_path, room, _Pool({"worker-2": "running"}))
            request = f"""
import urllib.request
body = json.dumps({{'model': 'haiku', 'system': 'consult system',
                   'messages': [{{'role': 'user', 'content': prompt}}], 'stream': True}}).encode()
req = urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'].rstrip('/') + '/v1/messages', body,
    {{'content-type': 'application/json', 'authorization': 'Bearer ' + {KEY!r}}})
with urllib.request.urlopen(req, timeout=10) as response:
    response.read()
"""
            fake_claude(tmp_path / "claude", _CONSULTED.replace(
                'system = open(sys.argv', request + 'system = open(sys.argv'))
            try:
                async with _recording(pool, tmp_path / "runtime", include_oauth=True) as (proxy, sink):
                    member = (await room._context("task-r")).member("backend_rd@s1")
                    assert await service.ask("task-r", member, question) is None
                    consult = await _settled(pool)
                    await until(lambda: not service._running, "the consult's end")
                    assert consult["state"] == "answered", consult
                    await proxy.close()
                    await sink.close(timeout=5)
                    record = await pool.fetchrow("SELECT id, metadata, status FROM llm_records WHERE agent_id=$1",
                                                 consult["consult_run"])
                    assert record["status"] == "completed"
                    route_id = json.loads(record["metadata"])["llmrec"]["route"]
                    route = RouteFile.read(tmp_path / "runtime" / ROUTES_DIR / f"{route_id}.json")
                    assert route.ended_at and route.record_id is None and route.last_record_id == record["id"]
                    request_row = await pool.fetchrow("SELECT record_id, after_record_id, route_id FROM llm_requests")
                    assert tuple(request_row.values()) == (record["id"], None, route_id)
                    assert len(upstream.requests) == 1
                    answer = await pool.fetchrow("SELECT reply_to FROM room_messages WHERE kind='answer'")
                    assert answer["reply_to"] == question["id"]
            finally:
                await service.stop()

    run_db(body, monkeypatch, timeout=40)
