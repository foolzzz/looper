"""Telegram startup failures release local resources without contacting Telegram."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.chat_routing import RoutingSnapshot
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.channels.telegram_inbox import TelegramInbox
from looper.agents.serve import _register_optional_channel


@pytest.mark.parametrize("failure", [None, "polling_stopped", "updater.stop", "stop", "shutdown"])
def test_stop_drains_reactions_from_already_queued_messages(tmp_path, monkeypatch, failure):
    """Use Application's real update queue and handlers, with a synthetic Telegram transport.

    A blocked /help reply keeps an ordinary received message queued until Application.stop drains it.
    No shutdown hook manufactures a reaction: the normal message handler creates it during that drain.
    """
    from telegram import Update
    from telegram.ext import Application, Updater
    from telegram.request import HTTPXRequest

    if failure == "polling_stopped":
        def fail_boundary(*args):
            raise OSError("synthetic checkpoint failure")

        monkeypatch.setattr(TelegramInbox, "polling_stopped", fail_boundary)

    async def run():
        bus = MessageBus(runtime_root=tmp_path)
        adapter = TelegramChannelAdapter(
            bot_token="0:synthetic", allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
        help_started, help_release = asyncio.Event(), asyncio.Event()
        stop_started, reaction_started, reaction_finished = (asyncio.Event() for _ in range(3))
        events, reactions, closed, transport_snapshots = [], [], [], []
        stop_app, shutdown_app = Application.stop, Application.shutdown
        shutdown_request = HTTPXRequest.shutdown

        async def request(request, url, **kwargs):
            method = url.rsplit("/", 1)[-1]
            if method == "getMe":
                result = {"id": 1, "is_bot": True, "first_name": "Synthetic", "username": "synthetic_bot"}
            elif method == "sendMessage":
                help_started.set()
                await help_release.wait()
                result = {"message_id": 10, "date": 0, "chat": {"id": 7, "type": "private"}, "text": "help"}
            elif method == "setMessageReaction":
                events.append("reaction")
                reactions.append(asyncio.current_task())
                reaction_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    events.append("reaction.finished")
                    reaction_finished.set()
            else:
                assert method == "setMyCommands", method
                result = True
            return 200, json.dumps({"ok": True, "result": result}).encode()

        async def start_polling(updater, **kwargs):
            updater._running = True

        async def stop_polling(updater):
            events.append("updater.stop")
            updater._running = False
            if failure == "updater.stop":
                raise RuntimeError("synthetic updater stop failure")

        async def stop(application):
            events.append("stop")
            stop_started.set()
            await stop_app(application)
            await reaction_started.wait()
            events.append("updates.drained")
            if failure == "stop":
                raise RuntimeError("synthetic application stop failure")

        async def shutdown(application):
            events.append("shutdown")
            transport_snapshots.append((reaction_finished.is_set(), all(task.done() for task in reactions)))
            if failure == "shutdown":
                raise RuntimeError("synthetic application shutdown failure")
            await shutdown_app(application)

        async def close(request):
            transport_snapshots.append((reaction_finished.is_set(), all(task.done() for task in reactions)))
            await shutdown_request(request)
            closed.append(request)

        monkeypatch.setattr(HTTPXRequest, "do_request", request)
        monkeypatch.setattr(HTTPXRequest, "shutdown", close)
        monkeypatch.setattr(Updater, "start_polling", start_polling)
        monkeypatch.setattr(Updater, "stop", stop_polling)
        monkeypatch.setattr(Application, "stop", stop)
        monkeypatch.setattr(Application, "shutdown", shutdown)
        stopping = None
        app = None
        owned = ()
        try:
            await adapter.start(bus)
            app, owned = adapter._app, adapter._requests

            def update(number, text):
                message = {"message_id": number, "date": int(time.time()), "chat": {"id": 7, "type": "private"},
                           "from": {"id": 7, "is_bot": False, "first_name": "Sender"}, "text": text}
                if text == "/help":
                    message["entities"] = [{"type": "bot_command", "offset": 0, "length": 5}]
                return Update.de_json({"update_id": number, "message": message}, app.bot)

            await app.update_queue.put(update(1, "/help"))
            await help_started.wait()
            await app.update_queue.put(update(2, "queued message"))
            assert app.update_queue.qsize() == 1 and not adapter._reactions._pending
            stopping = asyncio.create_task(adapter.stop())
            await stop_started.wait()
            help_release.set()
            await stopping
            assert bus.get_queue("manager").get_nowait().payload["text"] == "queued message"
            assert reactions and reaction_finished.is_set() and all(task.done() for task in reactions)
            assert not adapter._reactions._pending
            assert all(finished and done for finished, done in transport_snapshots)
            assert events.index("updates.drained") < events.index("reaction.finished") < events.index("shutdown")
            assert all(request in closed and request._client.is_closed for request in owned)
            assert adapter._app is None and adapter._bus is None and adapter._outbound_task is None
            assert not adapter._requests and not adapter.healthcheck()["healthy"]
            before = list(events)
            await adapter.stop()
            assert events == before
        finally:
            # A failing-first assertion must not leave real Application tasks or HTTP clients behind.
            help_release.set()
            if stopping and not stopping.done():
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
            await adapter._reactions.stop()
            if app and app.running:
                await stop_app(app)
            for request in owned:
                await shutdown_request(request)
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(run(), 10))


@pytest.mark.parametrize("failure", ["initialize", "start", "deleteWebhook", None])
@pytest.mark.parametrize("optional", [False, True])
def test_partial_start_releases_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None, optional: bool,
) -> None:
    async def run() -> None:
        inbox_path = tmp_path / ".local/telegram/channel-telegram/0.sqlite3"
        inbox = TelegramInbox(inbox_path)
        inbox.polling_stopped(100)
        inbox.close()
        bus = MessageBus(runtime_root=tmp_path)
        registry = ChannelRegistry(bus)
        adapter = TelegramChannelAdapter(
            bot_token="0:synthetic", allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
        error = RuntimeError(failure)
        events: list[str] = []
        app = SimpleNamespace(running=False, open=False, updater=SimpleNamespace(running=False))

        async def phase(name: str) -> None:
            events.append(name)
            if name == "initialize":
                app.open = True
            if name == failure:
                raise error
            if name == "start":
                app.running = True
            elif name == "deleteWebhook":
                app.updater.running = True
                adapter._inbox.polling_received([])
            elif name == "updater.stop":
                assert app.updater.running
                app.updater.running = False
            elif name == "stop":
                assert app.running
                app.running = False
            elif name == "shutdown":
                assert not app.running and not app.updater.running
                app.open = False

        app.add_handler = MagicMock()
        app.bot = SimpleNamespace(set_my_commands=AsyncMock())
        app.start = lambda: phase("start")
        app.stop = lambda: phase("stop")
        app.shutdown = lambda: phase("shutdown")
        app.updater.start_polling = lambda **_: phase("deleteWebhook")
        app.updater.stop = lambda: phase("updater.stop")
        builder = MagicMock()
        builder.token.return_value.build.return_value = app
        monkeypatch.setattr("telegram.ext.Application.builder", lambda: builder)

        async def pending() -> None:
            await asyncio.Event().wait()

        adapter._reactions._react = lambda *_: pending()
        adapter._reactions.add("1", "2")
        reactions = set(adapter._reactions._pending)
        outbound: list[asyncio.Task] = []

        async def initialize() -> None:
            outbound.append(adapter._outbound_task)
            await phase("initialize")

        app.initialize = initialize
        try:
            if optional:
                snapshot = RoutingSnapshot(adapter.channel_id, "manager", False, {})
                monkeypatch.setattr("looper.agents.channels.registry.load_snapshot", AsyncMock(return_value=snapshot))
                assert await _register_optional_channel(registry, adapter) is (failure is None)
            elif failure:
                with pytest.raises(RuntimeError) as caught:
                    await adapter.start(bus)
                assert caught.value is error
            else:
                await adapter.start(bus)
            if failure is None:
                assert adapter.healthcheck()["healthy"]
                if optional:
                    await registry.unregister(adapter.channel_id)
                else:
                    await adapter.stop()
            assert not app.open and not app.running and not app.updater.running
            assert ("updater.stop" in events) is (failure is None)
            assert ("stop" in events) is (failure in (None, "deleteWebhook"))
            assert "shutdown" in events
            assert all(task.done() for task in [*outbound, *reactions])
            assert not adapter._reactions._pending
            assert adapter._app is None and adapter._outbound_task is None and adapter._bus is None
            assert not adapter._requests
            assert not adapter.healthcheck()["healthy"]
            inbox = TelegramInbox(inbox_path)
            try:
                # Failed startup must not shorten the offline delivery window.
                if failure is not None:
                    assert inbox.previous_polling_stopped_at == 100
                else:
                    assert inbox.previous_polling_stopped_at > 100
            finally:
                inbox.close()
            assert registry.get(adapter.channel_id) is None
            before = list(events)
            await adapter.stop()
            await adapter.stop()
            assert events == before
        finally:
            # Reap even on the failing-first run, so asyncio.run cannot hide a leaked task.
            await adapter._reactions.stop()
            for task in outbound:
                task.cancel()
            await asyncio.gather(*outbound, return_exceptions=True)
            await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("stage", [
    "outbound", "reactions", "updater.stop", "stop", "shutdown", "get_updates_request", "request",
])
@pytest.mark.parametrize("startup_failure", [False, True])
def test_cleanup_failure_does_not_skip_later_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, startup_failure: bool,
) -> None:
    async def run() -> None:
        from telegram.request import HTTPXRequest

        bus = MessageBus(runtime_root=tmp_path)
        adapter = TelegramChannelAdapter(
            bot_token="0:synthetic", allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
        app = MagicMock(running=True)
        app.updater.running = True
        events: list[str] = []
        startup_error = RuntimeError("polling failed")

        async def cleanup(name: str) -> None:
            events.append(name)
            if name == stage:
                raise RuntimeError(f"{name} failed")

        shutdown_request = HTTPXRequest.shutdown
        requests: list[HTTPXRequest] = []

        async def close_request(request: HTTPXRequest) -> None:
            await shutdown_request(request)
            requests.append(request)
            await cleanup("get_updates_request" if len(requests) == 1 else "request")

        monkeypatch.setattr(HTTPXRequest, "shutdown", close_request)

        for obj, attr, name in ((app.updater, "stop", "updater.stop"), (app, "stop", "stop"),
                                (app, "shutdown", "shutdown")):
            monkeypatch.setattr(obj, attr, lambda name=name: cleanup(name))
        stop_reactions = adapter._reactions.stop

        async def reactions_stop() -> None:
            await stop_reactions()
            await cleanup("reactions")

        monkeypatch.setattr(adapter._reactions, "stop", reactions_stop)
        app.initialize = AsyncMock()
        app.start = AsyncMock()
        app.bot.set_my_commands = AsyncMock()
        app.updater.start_polling = AsyncMock(side_effect=startup_error if startup_failure else None)
        builder = MagicMock()
        builder.token.return_value.build.return_value = app
        monkeypatch.setattr("telegram.ext.Application.builder", lambda: builder)

        async def outbound() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                await cleanup("outbound")

        adapter._reactions._react = lambda *_: asyncio.Event().wait()
        adapter._reactions.add("1", "2")
        reactions = set(adapter._reactions._pending)
        monkeypatch.setattr(adapter, "_outbound_loop", outbound)

        async def initialize() -> None:
            await asyncio.sleep(0)  # Let the local task enter its cancellation handler.

        app.initialize.side_effect = initialize
        try:
            if startup_failure:
                with pytest.raises(RuntimeError) as caught:
                    await adapter.start(bus)
                assert caught.value is startup_error
            else:
                await adapter.start(bus)
                await adapter.stop()
            assert set(events) == {
                "outbound", "reactions", "updater.stop", "stop", "shutdown", "get_updates_request", "request",
            }
            assert all(request._client.is_closed for request in requests)
            assert all(task.done() for task in reactions)
            assert not adapter._reactions._pending and not adapter._requests
            assert adapter._app is None and adapter._outbound_task is None
            before = list(events)
            await adapter.stop()
            assert events == before or events == [*before, "reactions"]
        finally:
            if adapter._outbound_task:
                adapter._outbound_task.cancel()
                await asyncio.gather(adapter._outbound_task, return_exceptions=True)
            await bus.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["request.initialize", "getMe", "deleteWebhook"])
def test_real_application_closes_http_clients_after_partial_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from telegram.error import TimedOut
    from telegram.ext import ApplicationBuilder
    from telegram.request import HTTPXRequest

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = TelegramChannelAdapter(
            bot_token="0:synthetic", allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
        error = TimedOut(failure)
        requests: list[HTTPXRequest] = []
        apps = []
        build = ApplicationBuilder.build
        initialize = HTTPXRequest.initialize

        def build_app(builder):
            app = build(builder)
            apps.append(app)
            return app

        async def initialize_request(request):
            requests.append(request)
            await initialize(request)
            if failure == "request.initialize" and len(requests) == 1:
                raise error

        async def do_request(request, url, **kwargs):
            if url.endswith(f"/{failure}"):
                raise error
            return 200, b'{"ok":true,"result":{"id":1,"is_bot":true,"first_name":"Synthetic"}}'

        monkeypatch.setattr(ApplicationBuilder, "build", build_app)
        monkeypatch.setattr(HTTPXRequest, "initialize", initialize_request)
        monkeypatch.setattr(HTTPXRequest, "do_request", do_request)
        try:
            with pytest.raises(TimedOut) as caught:
                await adapter.start(bus)
            assert caught.value is error
            assert len(requests) == 2
            assert all(request._client.is_closed for request in requests)
            assert not apps[0].running and not apps[0].updater.running
            assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            await adapter.stop()
        finally:
            if apps and apps[0].running:
                await apps[0].stop()
            for request in requests:
                await request.shutdown()
            if adapter._outbound_task:
                adapter._outbound_task.cancel()
                await asyncio.gather(adapter._outbound_task, return_exceptions=True)
            await bus.shutdown()

    asyncio.run(run())


def test_invalid_bot_token_releases_inbox_and_transports(tmp_path, monkeypatch):
    from telegram.error import InvalidToken
    from telegram.request import HTTPXRequest

    closed = AsyncMock()
    monkeypatch.setattr(HTTPXRequest, "shutdown", closed)
    adapter = TelegramChannelAdapter(bot_token="invalid", allowlist=SenderAllowlist(
        "channel-telegram", frozenset({"7"})))

    async def run():
        with pytest.raises(InvalidToken):
            await adapter.start(MessageBus(tmp_path))
        assert adapter._inbox is None
        assert adapter._requests == ()
        assert not adapter._running
        assert closed.await_count == 2

    asyncio.run(run())
