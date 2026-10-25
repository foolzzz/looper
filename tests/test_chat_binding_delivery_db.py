"""Typed configuration through native ingress, persisted ownership and original egress."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import api_v1, chat_settings
from looper.agents.bus import MessageBus
from looper.agents.channels import chat_routing
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.chat_history import record_reply
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.managers import ManagerFleet
from looper.agents.managers_api import ManagerDefinition
from looper.agents.message import MessageEnvelope, MessageType, channel_address, channel_thread_id
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.serve import seed_channel_configs
from looper.agents.supervisor import AgentSupervisor
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.agents.tiers.task import Task, TaskRouter
from looper.db import api_clients, chat, managers, projects, tasks
from tests.db_support import run_db, until
from tests.test_agent_channels import _lark_event, _tg_update
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_channel_post import _Bot
from tests.test_lark_transport import BOT_ID, OPEN_ID, StubTransport
from tests.test_manager_stop import _Session
from tests.test_operator_auth import TOKEN

REVIEW = "manager-review"


def validate_success(operation, answer):
    document = api_v1.document()
    path = "/" + operation.replace(".", "/")
    response = document["paths"][path]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    api_v1.validate_arguments(document["components"]["schemas"]["OpOk"], answer)
    api_v1.validate_arguments(response["allOf"][1]["properties"]["result"], answer["result"])


@asynccontextmanager
async def delivery(root, pool, monkeypatch, kind):
    await pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES "
                       "('manager-review', 'Review', 'Review synthetic tasks.')")
    bot_id, native_chat = f"channel-{kind}", "99" if kind == "telegram" else "oc_1"
    bus, router = MessageBus(runtime_root=root), TaskRouter(runtime_root=root)
    router._bus = bus
    supervisor = AgentSupervisor(bus, root)
    platform = Platform(bus=bus, task_router=router, supervisor=supervisor, runtime_root=root)
    fleet = ManagerFleet(platform=platform, supervisor=supervisor, runtime_root=root,
                         url="http://127.0.0.1:1", config=ManagerConfig(work_dir=root), broadcaster=None)
    for row in await managers.list_managers():
        agent = fleet._build(ManagerDefinition.from_row(row))
        agent._bus, agent._claude = bus, _Session(hold=False)
        bus.subscribe_topic(MessageType.USER_MESSAGE.value, agent._receive, consumes=True)
    monkeypatch.setattr(ManagerOps, "tools_problem", AsyncMock(return_value=None))
    registry, transport = ChannelRegistry(bus), _Bot() if kind == "telegram" else StubTransport()
    adapter = (TelegramChannelAdapter(channel_id=bot_id,
                   allowlist=SenderAllowlist(bot_id, frozenset({"7"})), manager_running=lambda _: True,
                   stop_chat=fleet.stop_chat) if kind == "telegram" else
               LarkChannelAdapter(channel_type=kind, runtime_root=root, transport=transport,
                   bot_open_id=BOT_ID, allowlist=SenderAllowlist(bot_id, frozenset({OPEN_ID})),
                   manager_running=lambda _: True, stop_chat=fleet.stop_chat))
    if kind == "telegram":
        adapter._app = SimpleNamespace(bot=transport)
        adapter._acknowledge = Mock()
    async def start(bus):
        adapter._bus = bus  # Install only the existing synthetic transport; never start an account connection.
    monkeypatch.setattr(adapter, "start", start)
    await registry.register(adapter)

    async def ingress(text, *, rejected=False, chat_id=None):
        if kind == "telegram":
            update = _tg_update(8 if rejected else 7, text, int(chat_id or native_chat))
            handler = {"/start": adapter._cmd_start, "/stop": adapter._cmd_stop}.get(text, adapter._on_message)
            await handler(update, None)
            return update
        await adapter._handle_lark_event(_lark_event("ou_rejected" if rejected else OPEN_ID, text,
                                                   chat_id=chat_id or native_chat))

    async def flush():
        output = bus.take_queue(bot_id)
        for envelope in output:
            receipt = await adapter.deliver(envelope)
            assert receipt.success, receipt
            await record_reply(kind, envelope, receipt)
        return output

    async with _serving(root, listen=True) as server:
        server._channels, server._managers = registry, fleet
        server._client_ops = ClientOps(platform)
        _, client_token = await api_clients.create("Synthetic client")
        async def call(operation, body=None, *, client=False):
            path = "/api/v1/" + operation.replace(".", "/")
            status, _, answer = await _send(server, "POST " + path,
                {**_bearer(client_token if client else TOKEN), "X-Looper-Session": "delivery"}, json.dumps(body or {}))
            if status == 200:
                validate_success(operation, answer)
            return status, answer
        async def configure(manager=None, *, policy=None, remove=False):
            body = {"channel_id": bot_id, "chat_id": native_chat}
            if policy is not None:
                operation, body = "channel_chats.set_policy", {"channel_id": bot_id, "bound_chats_only": policy}
            elif remove:
                operation = "manager_chat_bindings.delete"
            else:
                operation, body = "manager_chat_bindings.put", {**body, "manager_id": manager}
            status, answer = await call(operation, body)
            assert status == 200, answer
        try:
            yield SimpleNamespace(**locals())
        finally:
            await adapter._reactions.stop()
            for correlation in list(adapter._status._open):
                await adapter._status.finish(correlation, stopped=True)
            if kind != "telegram":
                await adapter.stop()
            for agent in fleet.agents():
                await agent.stop()
            await fleet.stop()
            await router.stop()
            await bus.shutdown()


@pytest.mark.parametrize("kind", ["telegram", "lark", "lark_bot"])
def test_rebind_keeps_inflight_reply_task_reports_and_history_with_original_owner(tmp_path, monkeypatch, kind):
    async def run(pool):
        async with delivery(tmp_path, pool, monkeypatch, kind) as d:
            await projects.create_project(name="Synthetic", work_dir=str(tmp_path))
            entered, release = asyncio.Event(), asyncio.Event()
            task_ids = []
            async def answer(agent, token, prepared):
                if prepared.message == "create synthetic task":
                    result = await agent.ops.call("looper_task_create", {
                        "requirement": "Check synthetic examples", "title": "Synthetic task", "project": "Synthetic"})
                    assert result["status"] == "ok", result
                    task_ids.append(result["result"]["task_id"])
                    entered.set()
                    await release.wait()
                return "synthetic answer: " + prepared.message
            monkeypatch.setattr(ManagerAgent, "_run_prepared", answer)
            await d.configure(REVIEW)
            await d.ingress("create synthetic task")
            original, = d.bus.take_queue(REVIEW)
            waiting = asyncio.create_task(d.fleet.get(REVIEW)._handle(original))
            try:
                await until(entered.is_set, timeout=10)
                await d.configure("manager")
            finally:
                release.set()
                await asyncio.wait_for(waiting, 10)
            replies = await d.flush()
            assert any(e.source.agent_id == REVIEW and e.message_type is MessageType.AGENT_REPLY for e in replies)
            await d.ingress("next owner")
            next_turn, = d.bus.take_queue("manager")
            await d.fleet.default._handle(next_turn)
            await d.flush()
            row = await tasks.get_task(task_ids[0])
            assert (row["manager_id"], row["origin_channel_id"], row["correlation_id"]) == (
                REVIEW, d.bot_id, original.correlation_id)
            assert row["chat_id"] == (d.native_chat if kind == "telegram" else OPEN_ID)
            await d.configure(policy=True)
            await d.configure(remove=True)
            # Restore the actual Ops-created row as a restarted router does; only planning is synthetic.
            d.router._tasks[row["id"]] = Task.from_row(row["id"], row)
            for message_type, payload in ((MessageType.TASK_ACCEPTED, {"step_count": 1}),
                                         (MessageType.GOAL_STATUS, {"status": "running", "summary": "synthetic progress"}),
                                         (MessageType.GOAL_STATUS, {"status": "completed", "summary": "synthetic end"})):
                event = MessageEnvelope.create(source=d.router.agent_address, target=d.router.agent_address,
                    message_type=message_type, payload={"task_id": row["id"], **payload},
                    correlation_id=row["correlation_id"], metadata={"origin_channel_id": row["origin_channel_id"]})
                await (d.router._handle_task_accepted(event) if message_type is MessageType.TASK_ACCEPTED
                       else d.router._handle_planner_status(event))
                report, = d.bus.take_queue(REVIEW)
                assert report.target.agent_id == REVIEW and d.bus.get_queue("manager").empty()
                await d.fleet.get(REVIEW)._handle(report)
                output = await d.flush()
                assert output and all(e.target.agent_id == d.bot_id for e in output)
                if kind == "telegram":
                    assert d.transport.sent[-1]["chat_id"] == int(d.native_chat)
                else:
                    expected = {"user_id": OPEN_ID} if payload.get("status") == "completed" else {"chat_id": d.native_chat}
                    assert expected.items() <= d.transport.calls[-1][1].items()
            await d.configure(REVIEW)
            await d.ingress("return to review")
            back, = d.bus.take_queue(REVIEW)
            await d.fleet.get(REVIEW)._handle(back)
            await d.flush()
            base = channel_thread_id(d.bot_id, d.native_chat, "manager", "manager")
            bound = channel_thread_id(d.bot_id, d.native_chat, REVIEW, "manager")
            for sid, owner, user_text in ((base, "manager", ["next owner"]),
                                         (bound, REVIEW, ["create synthetic task", "return to review"])):
                assert await chat.thread_owner(sid) == owner
                history = await chat.list_messages(sid)
                assert [m["content"] for m in history if m["role"] == "user"] == user_text
                assert len([m for m in history if m["role"] == "assistant"]) == len(user_text)
                assert any(m["content"].startswith("synthetic answer: " + user_text[0]) for m in history)
            await d.configure(policy=True)
            recovered = ChannelRegistry(d.bus)
            await recovered.register(d.adapter)
            assert recovered.resolve_chat(d.bot_id, d.native_chat).manager_id == REVIEW
            assert recovered.resolve_chat(d.bot_id, "123" if kind == "telegram" else "oc_2").action is chat_routing.RouteAction.SILENT
            assert await chat.thread_owner(bound) == REVIEW and await chat.thread_owner(base) == "manager"
            restored = await tasks.get_task(row["id"])
            for field in ("manager_id", "origin_channel_id", "correlation_id", "chat_id", "chat_kind", "chat_mention"):
                assert restored[field] == row[field]
    run_db(run, monkeypatch, setup=seed_channel_configs, timeout=40)


@pytest.mark.parametrize("kind", ["telegram", "lark", "lark_bot"])
def test_hot_bound_only_silence_and_explicit_bot_owner_admission(tmp_path, monkeypatch, kind):
    async def run(pool):
        async with delivery(tmp_path, pool, monkeypatch, kind) as d:
            await d.configure(policy=True)
            for text in ("suppressed synthetic", "/stop", "/start"):
                update = await d.ingress(text)
                if update:
                    update.message.reply_text.assert_not_awaited()
            assert not d.bus.journal.undelivered() and not d.adapter._correlation_chat
            assert await pool.fetchval("SELECT count(*) FROM chat_messages") == 0
            assert not d.adapter._reactions._pending and not d.adapter._status._open
            assert not (d.transport.sent if kind == "telegram" else d.transport.calls)
            if kind == "telegram":
                d.adapter._acknowledge.assert_not_called()
            status, answer = await d.call("channel_chats.list", {"channel_id": d.bot_id})
            catalog = answer["result"]
            assert status == 200 and catalog["chats"][0]["count"] == 3
            assert len(catalog["unbound_chats"]) == 1 and catalog["chats"][0]["name"] is None
            await d.configure("manager")
            await d.ingress("rejected sender", rejected=True)
            assert d.bus.get_queue("manager").empty()
            await d.ingress("admitted owner")
            admitted, = d.bus.take_queue("manager")
            assert admitted.metadata["channel_thread_id"] == channel_thread_id(d.bot_id, d.native_chat, "manager", "manager")
            await d.configure(remove=True)
            await d.ingress("silent again")
            assert d.bus.get_queue("manager").empty()
            # Removing admission never revokes a previously accepted response.
            await d.fleet.default._send_reply(admitted, "accepted before unbind")
            assert (await d.flush())[-1].payload["text"] == "accepted before unbind"
    run_db(run, monkeypatch, setup=seed_channel_configs, timeout=30)


def test_generated_operator_cli_discovers_and_executes_all_five_operations(tmp_path, monkeypatch, capsys):
    from looper import loopercli

    async def run(pool):
        async with delivery(tmp_path, pool, monkeypatch, "telegram") as d:
            token_file = tmp_path / "operator-token"
            token_file.write_text(TOKEN + "\n")
            token_file.chmod(0o600)
            for operation, flags in (
                ("manager_chat_bindings.put", ["--channel-id", d.bot_id, "--chat-id", d.native_chat,
                                               "--manager-id", REVIEW]),
                ("manager_chat_bindings.list", []),
                ("channel_chats.set_policy", ["--channel-id", d.bot_id, "--bound-chats-only"]),
                ("channel_chats.list", ["--channel-id", d.bot_id]),
                ("manager_chat_bindings.delete", ["--channel-id", d.bot_id, "--chat-id", d.native_chat]),
            ):
                group, action = operation.replace("_", "-").split(".")
                assert group not in loopercli.HAND_COMMANDS
                assert await asyncio.to_thread(loopercli.main, ["--json", "--client-file", str(tmp_path / "absent"),
                    group, action, *flags, "--url", f"http://127.0.0.1:{d.server._port}",
                    "--operator-token-file", str(token_file)]) == 0
                output = capsys.readouterr()
                validate_success(operation, json.loads(output.out))
                assert TOKEN not in output.out + output.err
            assert d.registry.resolve_chat(d.bot_id, d.native_chat).action is chat_routing.RouteAction.SILENT
    run_db(run, monkeypatch, setup=seed_channel_configs, timeout=30)


@pytest.mark.parametrize("kind", ["telegram", "lark", "lark_bot"])
def test_native_stop_reaches_every_owner_when_new_destination_is_off(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)
    async def run(pool):
        async with delivery(tmp_path, pool, monkeypatch, kind) as d:
            other_bot = d.bot_id + "-2"
            await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, display_name) "
                               "VALUES ($1, $2, 'Another synthetic bot')", other_bot, kind)
            d.registry.bindings_changed(other_bot, await chat_routing.load_snapshot(other_bot))
            targets, untouched, running = [], [], []
            sessions = [_Session(), _Session()]
            try:
                for owner, session in zip(("manager", REVIEW), sessions):
                    agent = d.fleet.get(owner)
                    agent._claude = session
                    await d.configure(owner)
                    for index in range(2):
                        await d.ingress("admitted synthetic turn")
                        env, = d.bus.take_queue(owner)
                        targets.append(agent.turn_for(env.message_id))
                        if index == 0:
                            running.append(asyncio.create_task(agent._handle(env)))
                            await until(session.running.is_set, timeout=10)
                    await d.ingress("different chat", chat_id="123" if kind == "telegram" else "oc_2")
                    env, = d.bus.take_queue("manager")
                    untouched.append(d.fleet.default.turn_for(env.message_id))
                    with monkeypatch.context() as patch:
                        patch.setattr(d.adapter, "_channel_id", other_bot)
                        patch.setattr(d.adapter, "_address", channel_address(kind, other_bot))
                        await d.ingress("same chat in another bot")
                    env, = d.bus.take_queue("manager")
                    untouched.append(d.fleet.default.turn_for(env.message_id))
                await pool.execute("INSERT INTO managers (id, name, system_prompt, enabled) "
                                   "VALUES ('manager-off', 'Paused', 'Synthetic examples.', false)")
                await d.configure("manager-off")
                d.adapter._manager_running = lambda _: False
                await d.ingress("/stop")
                assert all(t.stop is not None and t.stop.via == kind for t in targets)
                requests = [t.stop for t in targets]
                await d.ingress("/stop")
                assert all(t.stop is request for t, request in zip(targets, requests))
                await asyncio.wait_for(asyncio.gather(*running), 10)
                output = await d.flush()
                assert {e.source.agent_id for e in output if e.payload.get("outcome") == "stopped"} == {"manager", REVIEW}
                assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE status = 'interrupted'") == 2
                assert all(t is not None and t.stop is None for t in untouched)
                assert d.bus.get_queue("manager-off").empty()
            finally:
                for session in sessions:
                    session._stopped.set()
                await asyncio.gather(*running, return_exceptions=True)
    run_db(run, monkeypatch, setup=seed_channel_configs, timeout=30)


def test_real_client_cannot_request_or_confirm_operator_binding_operations(tmp_path, monkeypatch):
    async def run(pool):
        async with delivery(tmp_path, pool, monkeypatch, "telegram") as d:
            identity = {"channel_id": d.bot_id, "chat_id": d.native_chat}
            for operation, body in (
                ("manager_chat_bindings.list", {}),
                ("manager_chat_bindings.put", {**identity, "manager_id": REVIEW}),
                ("manager_chat_bindings.delete", identity),
                ("channel_chats.list", {"channel_id": d.bot_id}),
                ("channel_chats.set_policy", {"channel_id": d.bot_id, "bound_chats_only": True}),
            ):
                status, answer = await d.call(operation, body, client=True)
                assert status == 403 and answer["error"]["code"] == "forbidden"
                assert "confirmation_id" not in answer
                status, confirmed = await d.call("confirm", {"confirmation_id": operation}, client=True)
                assert status == 404 and confirmed["error"]["code"] == "not_found"
                assert (await d.call(operation, body))[0] == 200
    run_db(run, monkeypatch, setup=seed_channel_configs, timeout=30)
