"""Operator routing HTTP contracts and live publication on disposable PostgreSQL."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import asyncpg
import pytest

from looper.agents import api_v1
from looper.agents.bus import MessageBus
from looper.agents.channels import chat_routing as routing
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.serve import seed_channel_configs
from looper.db import api_clients, channel_configs, chat, chat_bindings, connection
from tests.db_support import DSN, run_db
from tests.test_agent_channels import _allowlist, _tg_update
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_channel_post import _Bot
from tests.test_operator_auth import TOKEN, _request

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
BOT = "channel-telegram-201"
TARGET = "manager-review"
OTHER = "manager-other"


async def _seed(pool):
    await seed_channel_configs(pool)
    await pool.execute("""
        INSERT INTO managers (id, name, system_prompt, enabled) VALUES
          ('manager-review', 'Review', 'Review examples.', true),
          ('manager-other', 'Other', 'Other examples.', false);
        INSERT INTO channel_configs (channel_id, channel_type, display_name, manager_id)
        VALUES ('channel-telegram-201', 'telegram', 'Example bot', 'manager');
    """)


def _run(test, monkeypatch):
    run_db(test, monkeypatch, setup=_seed, timeout=30)


@asynccontextmanager
async def _api(tmp_path, pool, *, registry=True, listen=True):
    _, token = await api_clients.create("Routing validation", pool=pool)
    bus = MessageBus(runtime_root=tmp_path)
    channels = ChannelRegistry(bus) if registry else None
    if channels is not None:
        channels.bindings_changed(BOT, await routing.load_snapshot(BOT))
    async with _serving(tmp_path, listen=listen) as server:
        server._channels = channels
        server._client_ops = ClientOps(Platform(bus=bus, task_router=server._task_router, runtime_root=tmp_path))

        async def call(operation, body=None, *, client=False):
            path = "/api/v1/" + operation.replace(".", "/")
            status, _, answer = await _send(server, "POST " + path, {
                **_bearer(token if client else TOKEN), "X-Looper-Session": "routing"}, json.dumps(body or {}))
            if status == 200:
                api_v1.validate_arguments({"type": "object", "properties": {
                    "result": api_v1.find("POST", path).result}}, {"result": answer["result"]})
            return status, answer

        try:
            yield call, channels, bus
        finally:
            await bus.shutdown()


def _binding(chat="7", manager=TARGET):
    return {"channel_id": BOT, "chat_id": chat, "manager_id": manager}


def _watch_contention(registry, monkeypatch):
    """A waiter must reach the existing per-bot lock before a barrier is released."""
    lock, waiting = registry.mutation_lock(BOT), asyncio.Event()
    class WatchedLock:
        async def __aenter__(self):
            if lock.locked():
                waiting.set()
            await lock.acquire()
            return self
        async def __aexit__(self, *exc):
            lock.release()
        def locked(self):
            return lock.locked()
    watched = WatchedLock()
    monkeypatch.setattr(registry, "mutation_lock", lambda _: watched)
    return waiting


def test_binding_crud_filters_and_idempotency_have_closed_enriched_readback(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, registry, _):
            status, answer = await call("manager_chat_bindings.put", _binding())
            assert status == 200, answer
            first = answer["result"]["binding"]
            assert first == {**_binding(), "created_at": first["created_at"], "channel_type": "telegram",
                "bot_name": "Example bot", "chat_name": None, "manager_name": "Review",
                "manager_enabled": True, "manager_deleted": False}
            assert (await call("manager_chat_bindings.put", _binding()))[1]["result"]["binding"] == first
            for filters in ({}, {"channel_id": BOT}, {"manager_id": TARGET},
                            {"channel_id": BOT, "manager_id": TARGET}):
                status, listed = await call("manager_chat_bindings.list", filters)
                assert status == 200 and listed["result"]["bindings"] == [first]
            status, empty = await call("manager_chat_bindings.list", {"channel_id": BOT, "manager_id": OTHER})
            assert status == 200 and empty["result"] == {"bindings": []}
            status, rebound = await call("manager_chat_bindings.put", _binding(manager="manager"))
            assert status == 200 and rebound["result"]["binding"]["manager_id"] == "manager"
            assert rebound["result"]["binding"]["created_at"] == first["created_at"]
            for expected in (True, False):
                status, answer = await call("manager_chat_bindings.delete", {"channel_id": BOT, "chat_id": "7"})
                assert status == 200 and answer["result"] == {"channel_id": BOT, "chat_id": "7", "deleted": expected}
                assert registry.resolve_chat(BOT, "7").manager_id == "manager"
    _run(run, monkeypatch)


@pytest.mark.parametrize("operation,body", [
    ("manager_chat_bindings.list", {"channel_id": "channel-telegram-999"}),
    ("manager_chat_bindings.list", {"manager_id": "manager-missing"}),
    ("manager_chat_bindings.put", {**_binding(), "channel_id": "channel-telegram-999"}),
    ("manager_chat_bindings.put", _binding(manager="manager-missing")),
    ("manager_chat_bindings.delete", {"channel_id": "channel-telegram-999", "chat_id": "7"}),
    ("channel_chats.list", {"channel_id": "channel-telegram-999"}),
    ("channel_chats.set_policy", {"channel_id": "channel-telegram-999", "bound_chats_only": True}),
])
def test_unknown_objects_are_not_found_instead_of_empty_or_success(tmp_path, monkeypatch, operation, body):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, _, _):
            status, answer = await call(operation, body)
            assert status == 404 and answer["error"]["code"] == "not_found", answer
            assert await pool.fetchval("SELECT count(*) FROM manager_chat_bindings") == 0
    _run(run, monkeypatch)


def test_disabled_manager_can_be_bound_and_deleted_manager_is_distinct_unavailable_target(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, _, _):
            status, answer = await call("manager_chat_bindings.put", _binding(manager=OTHER))
            assert status == 200
            assert answer["result"]["binding"]["manager_enabled"] is False
            assert answer["result"]["binding"]["manager_deleted"] is False
            await pool.execute("UPDATE managers SET deleted_at = now() WHERE id = $1", OTHER)
            status, answer = await call("channel_chats.list", {"channel_id": BOT})
            chat = answer["result"]["chats"][0]
            assert status == 200 and chat["binding"]["manager_name"] == "Other"
            assert chat["binding"]["manager_deleted"] is True and chat["binding"]["manager_enabled"] is False
            assert chat["effective_manager_id"] == OTHER and chat["effective_manager_name"] == "Other"
            assert chat["effective_manager_deleted"] is True and chat["effective_manager_enabled"] is False
            assert chat["count"] is None and chat["last_at"] is None
            for operation, body in (("manager_chat_bindings.put", _binding(manager=OTHER)),
                                    ("manager_chat_bindings.list", {"manager_id": OTHER})):
                status, answer = await call(operation, body)
                assert status == 404 and answer["error"]["code"] == "not_found"
            status, answer = await call("manager_chat_bindings.list")
            assert status == 200 and answer["result"]["bindings"][0]["manager_deleted"] is True
    _run(run, monkeypatch)


def test_catalog_pages_union_with_null_observations_and_independent_latest_twenty(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, _, _):
            for number in range(100, 122):
                await chat_bindings.record_seen(BOT, str(number), "Chat " + str(number), suppressed=True)
            assert (await call("manager_chat_bindings.put", _binding("105")))[0] == 200
            assert (await call("manager_chat_bindings.put", _binding("999")))[0] == 200
            seen, after = [], None
            while True:
                request = {"channel_id": BOT, "limit": 5}
                if after is not None:
                    request["after"] = after
                status, answer = await call("channel_chats.list", request)
                result = answer["result"]
                assert status == 200 and result["channel_id"] == BOT and result["bot_manager_id"] == "manager"
                assert result["bound_chats_only"] is False
                assert [chat["chat_id"] for chat in result["unbound_chats"]] == [str(n) for n in range(121, 101, -1)]
                assert all(chat["count"] == 1 and chat["last_at"] for chat in result["unbound_chats"])
                seen.extend(result["chats"])
                if not result["has_more"]:
                    assert result["next_cursor"] is None
                    break
                after = result["next_cursor"]
                assert after == result["chats"][-1]["chat_id"]
            assert [chat["chat_id"] for chat in seen] == [str(n) for n in range(100, 122)] + ["999"]
            unseen = seen[-1]
            assert unseen["count"] is None and unseen["last_at"] is None and unseen["name"] is None
            assert unseen["binding"] is not None and unseen["effective_manager_id"] == TARGET
            resolved = next(chat for chat in seen if chat["chat_id"] == "105")
            assert resolved["binding"]["chat_name"] == "Chat 105" and resolved["count"] == 1
            fallback = seen[0]
            assert fallback["binding"] is None and fallback["effective_manager_id"] == "manager"
            assert not fallback["effective_manager_deleted"]
            assert await pool.fetchval("SELECT count(*) FROM channel_unbound_chats WHERE chat_id = '105'") == 1
    _run(run, monkeypatch)


def test_http_writes_change_the_next_fake_native_message_and_survive_registry_restart(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, registry, bus):
            await pool.execute("UPDATE managers SET enabled = true WHERE id = $1", OTHER)
            adapter = TelegramChannelAdapter(channel_id=BOT, manager_id="manager", allowlist=_allowlist(BOT, "7"),
                                            manager_running=lambda _: True)
            adapter._bus, adapter._app = bus, SimpleNamespace(bot=_Bot())
            adapter._acknowledge = Mock()
            adapter._resolve_chat = registry.resolve_chat
            async def incoming(target):
                await adapter._on_message(_tg_update(7), None)
                if target is not None:
                    assert bus.get_queue(target).get_nowait().target.agent_id == target
                assert all(bus.get_queue(manager).empty() for manager in ("manager", TARGET, OTHER))
            await incoming("manager")
            assert (await call("manager_chat_bindings.put", _binding("99")))[0] == 200
            await incoming(TARGET)
            assert (await call("manager_chat_bindings.put", _binding("99", OTHER)))[0] == 200
            await incoming(OTHER)
            assert (await call("channel_chats.set_policy", {"channel_id": BOT, "bound_chats_only": True}))[0] == 200
            assert (await call("manager_chat_bindings.delete", {"channel_id": BOT, "chat_id": "99"}))[0] == 200
            await incoming(None)
            assert (await call("channel_chats.set_policy", {"channel_id": BOT, "bound_chats_only": False}))[0] == 200
            await incoming("manager")
            assert (await call("manager_chat_bindings.put", _binding("99")))[0] == 200
            recovered = ChannelRegistry(bus)
            recovered.bindings_changed(BOT, await routing.load_snapshot(BOT))
            adapter._resolve_chat = recovered.resolve_chat
            await incoming(TARGET)
            status, answer = await call("channel_chats.list", {"channel_id": BOT})
            assert status == 200 and answer["result"]["chats"][0]["count"] == 6
    _run(run, monkeypatch)


def test_http_rebind_racing_policy_cannot_publish_stale_routing(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, registry, _):
            assert (await call("manager_chat_bindings.put", _binding()))[0] == 200
            entered, release = asyncio.Event(), asyncio.Event()
            original = chat_bindings.put_binding
            async def delayed(*args, **kwargs):
                value = await original(*args, **kwargs)
                entered.set()
                await release.wait()
                return value
            monkeypatch.setattr(chat_bindings, "put_binding", delayed)
            waiting = _watch_contention(registry, monkeypatch)
            rebind = asyncio.create_task(call("manager_chat_bindings.put", _binding(manager=OTHER)))
            await entered.wait()
            policy = asyncio.create_task(call("channel_chats.set_policy", {"channel_id": BOT, "bound_chats_only": True}))
            await waiting.wait()
            assert not policy.done()
            release.set()
            assert [status for status, _ in await asyncio.gather(rebind, policy)] == [200, 200]
            assert registry._routing[BOT] == await routing.load_snapshot(BOT)
            assert registry._routing[BOT].bound_only and registry.resolve_chat(BOT, "7").manager_id == OTHER
    _run(run, monkeypatch)


@pytest.mark.parametrize("state", ["binding", "policy"])
def test_pending_client_deletion_rechecks_operator_routing_before_confirmation(tmp_path, monkeypatch, state):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, _, _):
            status, pending = await call("channel_configs.delete", {"channel_id": BOT}, client=True)
            assert status == 202, pending
            operation, body = (("manager_chat_bindings.put", _binding()) if state == "binding" else
                              ("channel_chats.set_policy", {"channel_id": BOT, "bound_chats_only": True}))
            assert (await call(operation, body))[0] == 200
            status, answer = await call("confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden", answer
            assert await channel_configs.get_config(BOT) is not None
            assert (await call("channel_configs.delete", {"channel_id": BOT}, client=True))[0] == 403
    _run(run, monkeypatch)


def test_client_delete_without_routing_keeps_confirmation_behavior(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, registry, _):
            status, pending = await call("channel_configs.delete", {"channel_id": BOT}, client=True)
            assert status == 202, pending
            status, answer = await call("confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 200 and answer["result"] == {"deleted": BOT, "restart": True}, answer
            assert await channel_configs.get_config(BOT) is None
            assert registry._routing[BOT].bindings == {} and registry._routing[BOT].bot_manager_id == "manager"
    _run(run, monkeypatch)


@pytest.mark.parametrize("binding_first", [True, False])
def test_guarded_client_delete_and_operator_binding_serialize_in_both_lock_orders(tmp_path, monkeypatch, binding_first):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, registry, _):
            status, pending = await call("channel_configs.delete", {"channel_id": BOT}, client=True)
            assert status == 202
            entered, release = asyncio.Event(), asyncio.Event()
            if binding_first:
                original = chat_bindings.put_binding
                async def barrier(*args, **kwargs):
                    value = await original(*args, **kwargs)
                    entered.set()
                    await release.wait()
                    return value
                monkeypatch.setattr(chat_bindings, "put_binding", barrier)
            else:
                original = asyncpg.transaction.Transaction.commit
                async def barrier(transaction):
                    monkeypatch.setattr(asyncpg.transaction.Transaction, "commit", original)
                    entered.set()
                    await release.wait()
                    await original(transaction)
                monkeypatch.setattr(asyncpg.transaction.Transaction, "commit", barrier)
            waiting = _watch_contention(registry, monkeypatch)
            async def bind():
                return await call("manager_chat_bindings.put", _binding())
            async def delete():
                return await call("confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            first = asyncio.create_task(bind() if binding_first else delete())
            await entered.wait()
            second = asyncio.create_task(delete() if binding_first else bind())
            await waiting.wait()
            assert not second.done() and registry.mutation_lock(BOT).locked()
            release.set()
            first_answer, second_answer = await asyncio.gather(first, second)
            assert [first_answer[0], second_answer[0]] == ([200, 403] if binding_first else [200, 404])
            assert (await channel_configs.get_config(BOT) is not None) is binding_first
            assert registry._routing[BOT].bindings == ({"7": TARGET} if binding_first else {})
    _run(run, monkeypatch)


def test_cancelled_http_handler_drains_commit_publication_before_next_write(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool, listen=False) as (call, registry, _):
            entered, release = asyncio.Event(), asyncio.Event()
            original = asyncpg.transaction.Transaction.commit
            async def commit(transaction):
                monkeypatch.setattr(asyncpg.transaction.Transaction, "commit", original)
                entered.set()
                await release.wait()
                await original(transaction)
            monkeypatch.setattr(asyncpg.transaction.Transaction, "commit", commit)
            waiting = _watch_contention(registry, monkeypatch)
            request = asyncio.create_task(call("manager_chat_bindings.put", _binding()))
            await entered.wait()
            request.cancel()
            await asyncio.sleep(0)
            request.cancel()
            follower = asyncio.create_task(call("channel_chats.set_policy", {"channel_id": BOT, "bound_chats_only": True}))
            await waiting.wait()
            assert not request.done() and not follower.done() and registry.mutation_lock(BOT).locked()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert (await follower)[0] == 200
            assert registry._routing[BOT] == await routing.load_snapshot(BOT)
            assert registry._routing[BOT].bound_only and registry.resolve_chat(BOT, "7").manager_id == TARGET
            assert not registry.mutation_lock(BOT).locked()
            assert not any(task.get_coro().__qualname__.endswith("_commit_publish.<locals>.finish")
                           for task in asyncio.all_tasks())
    _run(run, monkeypatch)


@pytest.mark.parametrize("operation,body", [
    ("manager_chat_bindings.list", {}),
    ("manager_chat_bindings.put", _binding()),
    ("manager_chat_bindings.delete", {"channel_id": BOT, "chat_id": "7"}),
    ("channel_chats.list", {"channel_id": BOT}),
    ("channel_chats.set_policy", {"channel_id": BOT, "bound_chats_only": True}),
])
def test_database_failure_is_unavailable_and_never_an_empty_success(tmp_path, monkeypatch, operation, body):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, registry, _):
            before = registry._routing[BOT]
            async with asyncpg.create_pool(DSN, min_size=1, max_size=1,
                    server_settings={"search_path": "pg_catalog"}) as broken_pool:
                with monkeypatch.context() as patch:
                    patch.setattr(connection, "_pool", broken_pool)
                    status, answer = await call(operation, body)
            assert status == 503 and answer["error"]["code"] == "unavailable", answer
            assert registry._routing[BOT] is before
            assert await pool.fetchval("SELECT count(*) FROM manager_chat_bindings") == 0
    _run(run, monkeypatch)


def test_embedded_server_without_registry_reads_but_refuses_writes_before_storage(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool, registry=False) as (call, _, _):
            for operation, body in (("manager_chat_bindings.list", {}),
                                    ("channel_chats.list", {"channel_id": BOT})):
                assert (await call(operation, body))[0] == 200
            for operation, body in (("manager_chat_bindings.put", _binding()),
                    ("manager_chat_bindings.delete", {"channel_id": BOT, "chat_id": "7"}),
                    ("channel_chats.set_policy", {"channel_id": BOT, "bound_chats_only": True})):
                status, answer = await call(operation, body)
                assert status == 503 and answer["error"]["code"] == "unavailable"
            assert await pool.fetchval("SELECT count(*) FROM manager_chat_bindings") == 0
            assert not await pool.fetchval("SELECT bound_chats_only FROM channel_configs WHERE channel_id = $1", BOT)
    _run(run, monkeypatch)


def test_tcp_disconnect_during_commit_still_publishes_before_next_http_write(tmp_path, monkeypatch):
    async def run(pool):
        registry = ChannelRegistry(None)
        registry.bindings_changed(BOT, await routing.load_snapshot(BOT))
        async with _serving(tmp_path, listen=True) as server:
            server._channels = registry
            server._client_ops = ClientOps(Platform(bus=None, task_router=server._task_router, runtime_root=tmp_path))
            entered, release = asyncio.Event(), asyncio.Event()
            waiting = _watch_contention(registry, monkeypatch)
            original = asyncpg.transaction.Transaction.commit

            async def commit(transaction):
                monkeypatch.setattr(asyncpg.transaction.Transaction, "commit", original)
                entered.set()
                await release.wait()
                await original(transaction)

            monkeypatch.setattr(asyncpg.transaction.Transaction, "commit", commit)
            _, writer = await asyncio.open_connection("127.0.0.1", server._port)
            try:
                writer.write(_request(server._port, "POST /api/v1/manager_chat_bindings/put",
                                      _bearer(TOKEN), json.dumps(_binding())).encode())
                await writer.drain()
                await entered.wait()
                writer.close()
                await writer.wait_closed()
                follower = asyncio.create_task(_send(server, "POST /api/v1/channel_chats/set_policy",
                    _bearer(TOKEN), json.dumps({"channel_id": BOT, "bound_chats_only": True})))
                await waiting.wait()
                assert not follower.done() and registry.mutation_lock(BOT).locked()
                release.set()
                status, _, answer = await follower
                assert status == 200 and answer["result"]["bound_chats_only"] is True
                assert registry._routing[BOT] == await routing.load_snapshot(BOT)
                assert registry.resolve_chat(BOT, "7").manager_id == TARGET
                assert registry._routing[BOT].bound_only
                handlers = [task for task in asyncio.all_tasks()
                            if task.get_coro().__qualname__ == "AgentHTTPServer._handle_connection"]
                await asyncio.wait_for(asyncio.gather(*handlers), timeout=2)
                assert not any(task.get_coro().__qualname__.endswith("_commit_publish.<locals>.finish")
                               for task in asyncio.all_tasks())
                assert not registry.mutation_lock(BOT).locked()
            finally:
                release.set()
                writer.close()
                await writer.wait_closed()
    _run(run, monkeypatch)


def test_operator_delete_clears_bindings_keeps_live_policy_and_preserves_history_tasks(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, registry, _):
            assert (await call("manager_chat_bindings.put", _binding()))[0] == 200
            assert (await call("channel_chats.set_policy", {"channel_id": BOT, "bound_chats_only": True}))[0] == 200
            await chat_bindings.record_seen(BOT, "7", "Synthetic group", suppressed=True, pool=pool)
            session_id = BOT + ":7#" + TARGET
            await chat.claim_thread(session_id, TARGET, pool=pool)
            await chat.save_message(session_id=session_id, message_id="message-example", role="user",
                                    content="Synthetic public example", channel="telegram", pool=pool)
            await pool.execute("INSERT INTO projects (id, name) VALUES ('proj-example', 'Example')")
            await pool.execute("INSERT INTO tasks (id, project_id, requirement, manager_id, origin_channel_id, chat_id) "
                               "VALUES ('task-example', 'proj-example', 'Inspect example', $1, $2, '7')", TARGET, BOT)
            task_before = await pool.fetchrow("SELECT * FROM tasks WHERE id = 'task-example'")
            messages_before = await chat.list_messages(session_id, pool=pool)
            status, answer = await call("channel_configs.delete", {"channel_id": BOT})
            assert status == 200 and answer["result"] == {"deleted": BOT, "restart": True}, answer
            assert await channel_configs.get_config(BOT) is None
            assert registry._routing[BOT].bindings == {}
            assert registry._routing[BOT].bot_manager_id == "manager" and registry._routing[BOT].bound_only
            assert registry.resolve_chat(BOT, "7").action == routing.RouteAction.SILENT
            for table in ("manager_chat_bindings", "channel_seen_chats", "channel_unbound_chats"):
                assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE channel_id = $1", BOT) == 0
            assert await chat.thread_owner(session_id, pool=pool) == TARGET
            assert await chat.list_messages(session_id, pool=pool) == messages_before
            assert await pool.fetchrow("SELECT * FROM tasks WHERE id = 'task-example'") == task_before
    _run(run, monkeypatch)


def test_concurrent_puts_each_return_the_binding_written_by_that_request(tmp_path, monkeypatch):
    from looper.agents.v1_domains import chat_bindings as domain

    async def run(pool):
        async with _api(tmp_path, pool) as (call, registry, _):
            entered, release = asyncio.Event(), asyncio.Event()
            second_attempted, second_finished = asyncio.Event(), asyncio.Event()
            lock = registry.mutation_lock(BOT)
            original = domain._bindings
            reads, lock_requests = 0, 0

            def mutation_lock(channel_id):
                nonlocal lock_requests
                assert channel_id == BOT
                lock_requests += 1
                if lock_requests == 2:
                    second_attempted.set()
                return lock

            async def readback(*args, **kwargs):
                nonlocal reads
                reads += 1
                if reads == 1:
                    protected = lock.locked()
                    entered.set()
                    await release.wait()
                    # An unprotected readback lets a later write finish first;
                    # force that order so returning the later target cannot pass.
                    if not protected:
                        await second_finished.wait()
                return await original(*args, **kwargs)

            async def rebind():
                try:
                    return await call("manager_chat_bindings.put", _binding(manager=OTHER))
                finally:
                    second_finished.set()

            monkeypatch.setattr(registry, "mutation_lock", mutation_lock)
            monkeypatch.setattr(domain, "_bindings", readback)
            first = asyncio.create_task(call("manager_chat_bindings.put", _binding()))
            second = None
            try:
                await entered.wait()
                second = asyncio.create_task(rebind())
                await second_attempted.wait()
                release.set()
                first_answer, second_answer = await asyncio.gather(first, second)
                assert [first_answer[0], second_answer[0]] == [200, 200]
                first_binding = first_answer[1]["result"]["binding"]
                second_binding = second_answer[1]["result"]["binding"]
                assert first_binding["manager_id"] == TARGET
                assert second_binding["manager_id"] == OTHER
                assert first_binding["created_at"] == second_binding["created_at"]
                assert registry._routing[BOT] == await routing.load_snapshot(BOT)
                assert registry.resolve_chat(BOT, "7").manager_id == OTHER
            finally:
                release.set()
                await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    _run(run, monkeypatch)


@pytest.mark.parametrize("operation,body", [
    ("manager_chat_bindings.list", {"channel_id": "channel-web"}),
    ("manager_chat_bindings.list", {"manager_id": "planner-example"}),
    ("manager_chat_bindings.put", _binding("oc_example")),
    ("manager_chat_bindings.put", {"channel_id": "channel-lark", "chat_id": "42", "manager_id": TARGET}),
    ("manager_chat_bindings.delete", {"channel_id": BOT, "chat_id": "0"}),
    ("channel_chats.list", {"channel_id": BOT, "after": "oc_example"}),
])
def test_invalid_native_ids_and_cursor_are_rejected_without_changing_routing(tmp_path, monkeypatch, operation, body):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, registry, _):
            status, answer = await call("manager_chat_bindings.put", _binding())
            assert status == 200 and answer["result"]["binding"]["manager_id"] == TARGET
            stored = await pool.fetch("SELECT * FROM manager_chat_bindings ORDER BY channel_id, chat_id")
            snapshots = dict(registry._routing)
            status, answer = await call(operation, body)
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await pool.fetch("SELECT * FROM manager_chat_bindings ORDER BY channel_id, chat_id") == stored
            assert registry._routing == snapshots
    _run(run, monkeypatch)


@pytest.mark.parametrize("operation,body,old,new", [
    ("manager_chat_bindings.put", _binding(manager=OTHER), TARGET, OTHER),
    ("manager_chat_bindings.delete", {"channel_id": BOT, "chat_id": "7"}, TARGET, None),
    ("channel_chats.set_policy", {"channel_id": BOT, "bound_chats_only": True}, False, True),
])
def test_routing_write_boundary_log(tmp_path, monkeypatch, caplog, operation, body, old, new):
    caplog.set_level("INFO")
    async def run(pool):
        async with _api(tmp_path, pool) as (call, registry, _):
            assert (await call("manager_chat_bindings.put", _binding()))[0] == 200
            await pool.execute("UPDATE channel_configs SET config = $2::jsonb WHERE channel_id = $1",
                               BOT, json.dumps({"description": "sentinel-private-token"}))
            await chat_bindings.record_seen(BOT, "7", "sentinel-private-message", suppressed=False, pool=pool)
            registry._routing.clear()  # Stopped bots still need accurate old values.
            caplog.clear()
            assert (await call(operation, body))[0] == 200
            [record] = [r for r in caplog.records if r.msg.startswith("chat_routing_write")]
            assert record.levelname == "INFO"
            assert (record.channel_id, record.chat_id, record.action, record.principal_kind, record.outcome) == (
                BOT, body.get("chat_id"), operation, "operator", "committed")
            field = "manager_id" if "bindings" in operation else "bound_chats_only"
            assert (getattr(record, "old_" + field), getattr(record, "new_" + field)) == (old, new)
            # Read through another connection: the log accompanies committed state, not a pending write.
            stored = await routing.load_snapshot(BOT, pool=pool)
            assert stored == registry._routing[BOT]
            assert all("sentinel-private-message" not in str(r.__dict__) and "sentinel-private-token" not in str(r.__dict__)
                       and TOKEN not in str(r.__dict__)
                       for r in caplog.records)
            monkeypatch.setattr(routing.logger, "info", Mock(side_effect=RuntimeError("logging unavailable")))
            assert (await call(operation, body))[0] == 200
            assert await routing.load_snapshot(BOT, pool=pool) == registry._routing[BOT]
    _run(run, monkeypatch)
