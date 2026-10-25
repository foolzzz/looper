"""Live snapshots follow the same committed service transaction as durable routing."""
import asyncio
from dataclasses import FrozenInstanceError

import asyncpg
import pytest

from looper.agents.channels import chat_routing as routing, registry as registry_module
from looper.agents.channels.registry import ChannelRegistry
from tests.db_support import run_db

BOT = "channel-telegram-201"
TARGET = "manager-review"


class Adapter:
    channel_type = "telegram"

    def __init__(self, channel_id=BOT):
        self.channel_id, self.started = channel_id, False

    async def start(self, bus):
        self.started = True
        if self.channel_id != "channel-web":
            assert self._resolve_chat(self.channel_id, "8").manager_id == "manager"


async def seed(pool):
    await pool.execute("""
        INSERT INTO managers (id, name, system_prompt) VALUES ('manager-review', 'Review', 'Review examples.');
        INSERT INTO channel_configs (channel_id, channel_type, display_name, enabled, config, manager_id)
        VALUES ('channel-telegram-201', 'telegram', 'Example bot', false, '{}', 'manager');
    """)


def test_snapshot_failure_prevents_native_start_but_web_bypasses_routing(monkeypatch, caplog):
    async def broken(channel_id):
        raise OSError("unavailable")

    async def run():
        registry = ChannelRegistry(None)
        native, web = Adapter(), Adapter("channel-web")
        monkeypatch.setattr(registry_module, "load_snapshot", broken)
        with pytest.raises(OSError):
            await registry.register(native)
        assert not native.started and registry.get(BOT) is None
        assert BOT in caplog.text
        await registry.register(web)
        assert web.started

    asyncio.run(run())


def test_snapshot_is_immutable_and_publication_has_no_adapter_dependency():
    bindings = {"7": TARGET}
    snapshot = routing.RoutingSnapshot(BOT, "manager", False, bindings)
    bindings.clear()
    with pytest.raises(TypeError):
        snapshot.bindings["8"] = TARGET
    with pytest.raises(FrozenInstanceError):
        snapshot.bound_only = True
    registry = ChannelRegistry(None)
    for registered in (False, True):
        if registered:
            registry._channels[BOT] = Adapter()
        registry.bindings_changed(BOT, snapshot)
        assert registry.resolve_chat(BOT, "7").manager_id == TARGET


@pytest.mark.parametrize("failure", ["missing_manager", "snapshot"])
def test_rollback_keeps_both_storage_and_live_snapshot_unchanged(monkeypatch, failure):
    async def run(pool):
        registry = ChannelRegistry(None)
        await registry.register(Adapter())
        before = registry._routing[BOT]
        if failure == "snapshot":
            def invalid(row):
                raise ValueError("invalid snapshot")
            monkeypatch.setattr(routing.RoutingSnapshot, "from_storage", invalid)
        with pytest.raises((LookupError, ValueError)):
            await routing.bind_chat(BOT, "7", TARGET if failure == "snapshot" else "manager-missing",
                                    registry=registry)
        assert registry._routing[BOT] is before
        assert await pool.fetchval("SELECT count(*) FROM manager_chat_bindings") == 0

    run_db(run, monkeypatch, setup=seed, timeout=20)


def test_concurrent_services_publish_complete_serialized_snapshots(monkeypatch):
    async def run(pool):
        registry = ChannelRegistry(None)
        await registry.register(Adapter())
        await asyncio.gather(routing.bind_chat(BOT, "7", TARGET, registry=registry),
                             routing.set_chat_policy(BOT, True, registry=registry))
        snapshot = registry._routing[BOT]
        assert snapshot == await routing.load_snapshot(BOT)
        assert snapshot.bound_only and snapshot.bindings == {"7": TARGET}
        await routing.unbind_chat(BOT, "7", registry=registry)
        assert registry.resolve_chat(BOT, "7").action == routing.RouteAction.SILENT

    run_db(run, monkeypatch, setup=seed, timeout=20)


def test_repeated_cancellation_drains_commit_and_publication_before_unlock(monkeypatch):
    async def run(pool):
        registry = ChannelRegistry(None)
        await registry.register(Adapter())
        entered, release = asyncio.Event(), asyncio.Event()
        original = asyncpg.transaction.Transaction.commit

        async def commit(transaction):
            monkeypatch.setattr(asyncpg.transaction.Transaction, "commit", original)
            entered.set()
            await release.wait()
            await original(transaction)

        monkeypatch.setattr(asyncpg.transaction.Transaction, "commit", commit)
        caller = asyncio.create_task(routing.bind_chat(BOT, "7", TARGET, registry=registry))
        await entered.wait()
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        follower = asyncio.create_task(routing.set_chat_policy(BOT, True, registry=registry))
        await asyncio.sleep(0)
        assert not caller.done() and not follower.done() and registry.mutation_lock(BOT).locked()
        assert not registry._routing[BOT].bindings
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await follower
        assert registry._routing[BOT] == await routing.load_snapshot(BOT)
        assert registry._routing[BOT].bindings == {"7": TARGET}
        assert not registry.mutation_lock(BOT).locked()
        assert not any(t.get_coro().__qualname__.endswith("_commit_publish.<locals>.finish")
                       for t in asyncio.all_tasks())

    run_db(run, monkeypatch, setup=seed, timeout=20)


@pytest.mark.parametrize("guard,bound_only", [(False, True), (True, False), (True, True)])
def test_delete_serializes_with_binding_and_checks_guard_in_transaction(monkeypatch, guard, bound_only):
    async def run(pool):
        registry = ChannelRegistry(None)
        await registry.register(Adapter())
        await routing.set_chat_policy(BOT, bound_only, registry=registry)
        await routing.observe_chat(BOT, "7", suppressed=True)
        assert await pool.fetchval("SELECT count(*) FROM channel_seen_chats") == 1
        entered, release = asyncio.Event(), asyncio.Event()
        original = routing.storage.put_binding

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(routing.storage, "put_binding", delayed)
        binding = asyncio.create_task(routing.bind_chat(BOT, "7", TARGET, registry=registry))
        await entered.wait()
        removal = asyncio.create_task(routing.remove_bot_with_routing(BOT, refuse_if_routed=guard, registry=registry))
        await asyncio.sleep(0)
        assert not removal.done()
        release.set()
        await binding
        assert await removal == ("has_bindings" if guard else "deleted")
        assert await pool.fetchval("SELECT EXISTS (SELECT 1 FROM channel_configs WHERE channel_id = $1)", BOT) == guard
        snapshot = registry._routing[BOT]
        assert snapshot.bot_manager_id == "manager" and snapshot.bound_only == bound_only
        assert snapshot.bindings == ({"7": TARGET} if guard else {})
        if not guard:
            await routing.observe_chat(BOT, "7", suppressed=True)
            assert await pool.fetchval("SELECT count(*) FROM channel_seen_chats") == 0
            with pytest.raises(LookupError):
                await routing.bind_chat(BOT, "7", TARGET, registry=registry)
            assert registry._routing[BOT] is snapshot
            assert await routing.remove_bot_with_routing(BOT, registry=registry) == "missing"
        assert await routing.remove_bot_with_routing("channel-telegram", registry=registry) == "builtin"

    run_db(run, monkeypatch, setup=seed, timeout=20)


def test_serverless_write_persists_without_changing_an_existing_live_registry(monkeypatch):
    async def run(pool):
        registry = ChannelRegistry(None)
        await registry.register(Adapter())
        snapshot = await routing.bind_chat(BOT, "7", TARGET)
        assert snapshot == await routing.load_snapshot(BOT)
        assert registry.resolve_chat(BOT, "7").manager_id == "manager"
        recovered = ChannelRegistry(None)
        await recovered.register(Adapter())
        assert recovered.resolve_chat(BOT, "7").manager_id == TARGET

    run_db(run, monkeypatch, setup=seed, timeout=20)


def test_two_bots_route_the_same_native_chat_independently(monkeypatch):
    async def run(pool):
        registry = ChannelRegistry(None)
        other = "channel-telegram-202"
        await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, display_name, manager_id) "
                           "VALUES ($1, 'telegram', 'Another bot', 'manager')", other)
        for bot, target in ((BOT, TARGET), (other, "manager")):
            await registry.register(Adapter(bot))
            await routing.bind_chat(bot, "7", target, registry=registry)
            await routing.set_chat_policy(bot, True, registry=registry)
        assert registry.resolve_chat(BOT, "7") == routing.RouteDecision(routing.RouteAction.ADMIT, TARGET, True)
        assert registry.resolve_chat(other, "7") == routing.RouteDecision(routing.RouteAction.ADMIT, "manager", True)
        await routing.unbind_chat(BOT, "7", registry=registry)
        assert registry.resolve_chat(BOT, "7").action == routing.RouteAction.SILENT
        assert registry.resolve_chat(other, "7").action == routing.RouteAction.ADMIT
    run_db(run, monkeypatch, setup=seed, timeout=20)
