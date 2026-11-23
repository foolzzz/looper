"""Idle delivery recovery and returned users through a disposable serve process."""
from __future__ import annotations

import asyncio
import signal
import socket
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from fake_claude import fake_claude
from looper import db, ops_config
from looper.agents import idle_checks, operator_notes, proactive_delivery, profile_registry, serve, session_routing
from looper.agents.channels.base import ChannelAdapter, DeliveryReceipt, PostReceipt
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.db import idle_checks as store
from tests.db_support import DSN, run_db, until

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


class _Lark(ChannelAdapter):
    channel_type = "lark"
    channel_id = "channel-lark"

    def __init__(self, posts, *, hold):
        self.posts, self.hold = posts, hold
        self.posting = asyncio.Event()

    async def start(self, bus):
        self.bus = bus

    async def stop(self):
        pass

    async def deliver(self, envelope):
        return DeliveryReceipt("lark", True)

    async def post(self, target, text, *, reply_to=None, idempotency_key=None):
        self.posts.append((target.id, text, idempotency_key))
        self.posting.set()
        if self.hold:
            await asyncio.Event().wait()
        return PostReceipt(True, external_ids=("synthetic-idle-send",))

    async def receive(self):
        await self.bus.publish(MessageEnvelope.create(source=channel_address("lark"), target=MANAGER_ADDRESS,
            message_type=MessageType.USER_MESSAGE, payload={"text": "I am back", "channel_context": {
                "chat_id": "oc_synthetic_serve_dm", "sender_id": "ou_synthetic_serve", "chat_type": "p2p"}}))


@pytest.mark.parametrize("returned", [False, True], ids=["restart-during-post", "returned-during-turn"])
def test_serve_never_resends_an_interrupted_post_or_delivers_after_the_user_returns(monkeypatch, tmp_path, returned):
    # Cover restoring both an enabled and a disabled caller, including a non-default resident limit.
    monkeypatch.setattr(session_routing, "_routing_enabled", returned)
    monkeypatch.setattr(session_routing, "_max_resident", 7)

    async def run(pool):
        await pool.execute("INSERT INTO system_settings (key,value) VALUES "
            "('llmrec.enabled','false'), ('delivery.enabled','true'), ('attention.enabled','true'), "
            "('manager.lanes.enabled','true'), ('manager.idle_checks.enabled','true'), "
            "('manager.session_routing.enabled','true'), ('manager.sessions.max_resident','3')")
        requests = tmp_path / "synthetic-turns.jsonl"
        reached, release = tmp_path / "idle-reached", tmp_path / "idle-release"
        executable = fake_claude(tmp_path / "synthetic-claude", f'''
import json, pathlib, sys, time
for line in sys.stdin:
    event = json.loads(line)
    if event.get('type') != 'user':
        continue
    text = event['message']['content']
    with open({str(requests)!r}, 'a') as log:
        log.write(json.dumps(text) + '\\n')
    if '[IDLE CHECK]' in text and {returned!r}:
        pathlib.Path({str(reached)!r}).touch()
        while not pathlib.Path({str(release)!r}).exists():
            time.sleep(0.01)
    reply = 'Synthetic login needs attention.' if '[IDLE CHECK]' in text else 'Welcome back.'
    print(json.dumps({{'type':'system','subtype':'init','session_id':'synthetic-idle-session'}}), flush=True)
    print(json.dumps({{'type':'assistant','message':{{'content':[{{'type':'text','text':reply}}]}}}}), flush=True)
    print(json.dumps({{'type':'result','subtype':'success','is_error':False,
        'session_id':'synthetic-idle-session','result':reply,'total_cost_usd':0.0}}), flush=True)
''')
        posts, adapters, servers, stops = [], [], [], {}
        monkeypatch.setattr(operator_notes, "_root", str(tmp_path))
        monkeypatch.setattr(db, "init_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(db, "close_pool", AsyncMock())
        monkeypatch.setattr(profile_registry, "register_found_runtimes", AsyncMock(return_value=[]))
        monkeypatch.setattr(serve, "_enabled_bot_rows", AsyncMock(return_value=[]))
        monkeypatch.setattr(serve, "_advisory_startup_checks", AsyncMock())
        monkeypatch.setattr(serve.self_check, "check_database", AsyncMock(return_value=
            serve.self_check.Check("database", "Database", "ok", "ready")))
        monkeypatch.setattr(proactive_delivery, "load_policy", lambda: proactive_delivery.DeliveryPolicy(
            timezone=timezone.utc, quiet_hours=None))

        async def register(registry, rows, root, running, types, **kwargs):
            if "lark" in types:
                adapter = _Lark(posts, hold=not returned)
                adapters.append(adapter)
                await registry.register(adapter)

        start_http = serve.AgentHTTPServer.start

        async def http_started(server):
            await start_http(server)
            servers.append(server)

        monkeypatch.setattr(serve, "_register_bots", register)
        monkeypatch.setattr(serve.AgentHTTPServer, "start", http_started)
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback, *args: stops.update({sig: callback}))

        async def start():
            stops.clear()
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            ops = ops_config.OpsConfig(runtime_root=tmp_path, path=tmp_path / "ops.toml", database_url=DSN,
                host="127.0.0.1", port=port, operator_notes_root=str(tmp_path))
            task = asyncio.create_task(serve._run_agent_system(ops=ops, runtime_root=tmp_path, work_dir=tmp_path,
                claude_bin=executable, skip_backup=True, runner_supervisor=SimpleNamespace(stop=AsyncMock())))
            await until(lambda: signal.SIGTERM in stops or task.done(), "serve ready", timeout=30)
            if task.done():
                task.result()
            assert session_routing.routing_enabled() is True
            assert session_routing.max_resident() == 3
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(f"http://127.0.0.1:{port}/api/v1/system/status", json={},
                    headers={"Authorization": f"Bearer {servers[-1]._operator_token}"})
                assert response.status_code == 200
            return task

        task = await start()
        try:
            check_id = await store.insert_running(pool, manager_id="manager", local_date=datetime.now(timezone.utc).date(),
                items=[{"section": "logins", "key": "login:serve", "title": "Synthetic login", "since": None,
                        "facts": {"fix": "Run synthetic login"}}],
                channel_id="channel-lark", chat="ou_synthetic_serve")
            wake = idle_checks.due_envelope(check_id)
            await adapters[-1].bus.publish(wake)
            if returned:
                await until(reached.exists, "fake idle turn reached", timeout=20)
                await adapters[-1].receive()
                async def user_stored():
                    return await pool.fetchval("SELECT count(*) FROM chat_messages m "
                        "JOIN chat_direct_threads d USING (session_id) WHERE m.role='user' "
                        "AND d.channel_id='channel-lark' AND d.sender_id='ou_synthetic_serve' "
                        "AND m.session_id='lark:oc_synthetic_serve_dm'")
                deadline = loop.time() + 15
                while not await user_stored():
                    assert loop.time() < deadline, "stand-in DM must reach persisted user history"
                    await asyncio.sleep(0.01)
                release.touch()
                deadline = loop.time() + 15
                while (await store.get(check_id))["state"] == "running":
                    assert loop.time() < deadline, "idle turn must settle after the user returns"
                    await asyncio.sleep(0.01)
                assert (await store.get(check_id))["state"] == "preempted"
                assert posts == []
            else:
                await asyncio.wait_for(adapters[-1].posting.wait(), 20)
                assert (await store.get(check_id))["state"] == "sending"
            stops[signal.SIGTERM]()
            await asyncio.wait_for(task, 20)
            assert session_routing.routing_enabled() is returned
            assert session_routing.max_resident() == 7
            if not returned:
                assert (await store.get(check_id))["state"] == "sending"
                task = await start()
                assert (await store.get(check_id))["state"] == "uncertain"
                assert await pool.fetchval("SELECT outcome FROM proactive_deliveries WHERE dedupe_key=$1",
                                           f"idle-{check_id}") == "unknown"
                await adapters[-1].bus.publish(wake)
                await until(lambda: all(item.message_id != wake.message_id
                    for item in adapters[-1].bus.journal.undelivered()), "replayed wake settled")
                assert len(posts) == 1
                assert len(requests.read_text().splitlines()) == 1
                stops[signal.SIGTERM]()
                await asyncio.wait_for(task, 20)
                assert session_routing.routing_enabled() is returned
                assert session_routing.max_resident() == 7
        finally:
            release.touch()
            if not task.done():
                if signal.SIGTERM in stops:
                    stops[signal.SIGTERM]()
                await asyncio.wait_for(task, 20)

    run_db(run, monkeypatch, timeout=120, max_size=5)
