"""Freeze notices use the actual channel boundary and retry unsuccessful sends."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from looper.agents import admission_notices, gate_service
from looper.agents.channels.base import PostReceipt
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.gate_service import GateChat
from looper.agents.task_notice import NotifyChannel, NotifyTarget, TargetKind
from looper.db import system_settings
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(
    not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL"
)
PROVIDER = "prov-claude-subscription"
KEY = "admission_alert_sent:91"


class Adapter:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.block = False
        self.started = asyncio.Event()

    async def notify(self, chat, text, *, idempotency_key=None):
        self.calls.append((chat, text, idempotency_key))
        self.started.set()
        if self.block:
            await asyncio.Future()
        if isinstance(self.failure, Exception):
            raise self.failure
        return PostReceipt(not self.failure, error=self.failure)


def configure(monkeypatch, adapter):
    registry = ChannelRegistry(SimpleNamespace())
    registry._channels["channel-telegram"] = adapter
    monkeypatch.setattr(admission_notices, "_sender", registry.notify)

    async def target():
        return GateChat(
            NotifyChannel.TELEGRAM,
            NotifyTarget(TargetKind.CHAT, "synthetic-notice-chat"),
            "channel-telegram",
        )

    monkeypatch.setattr(gate_service, "dashboard_target", target)
    return registry


async def alert():
    await admission_notices.freeze_alert(
        PROVIDER, 91, until=datetime.now(timezone.utc), until_known=False, waiting=2
    )


def test_real_registry_delivers_once_with_stable_idempotency_key(monkeypatch):
    async def check(pool):
        adapter = Adapter()
        configure(monkeypatch, adapter)
        await alert()
        # A new registry after restart still honors the durable event claim.
        configure(monkeypatch, adapter)
        await alert()
        assert len(adapter.calls) == 1
        assert adapter.calls[0][0] == "synthetic-notice-chat"
        assert adapter.calls[0][2] == "admission-freeze:91"
        assert "重置时间未知" in adapter.calls[0][1]
        assert await system_settings.get_settings([KEY]) == {KEY: "sent"}

    run_db(check, monkeypatch)


@pytest.mark.parametrize("failure", ["channel unavailable", RuntimeError("transport failed")])
def test_failed_send_releases_claim_and_can_retry_after_reconfiguration(monkeypatch, failure):
    async def check(pool):
        adapter = Adapter()
        adapter.failure = failure
        configure(monkeypatch, adapter)
        await alert()
        assert await system_settings.get_settings([KEY]) == {}
        adapter.failure = None
        configure(monkeypatch, adapter)
        await alert()
        assert len(adapter.calls) == 2
        assert {call[2] for call in adapter.calls} == {"admission-freeze:91"}
        assert await system_settings.get_settings([KEY]) == {KEY: "sent"}

    run_db(check, monkeypatch)


def test_cancelled_send_releases_claim_for_retry(monkeypatch):
    async def check(pool):
        adapter = Adapter()
        adapter.block = True
        configure(monkeypatch, adapter)
        task = asyncio.create_task(alert())
        try:
            await asyncio.wait_for(adapter.started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await system_settings.get_settings([KEY]) == {}
            adapter.block = False
            await alert()
            assert len(adapter.calls) == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    run_db(check, monkeypatch)


@pytest.mark.parametrize("missing", ["sender", "target"])
def test_unconfigured_delivery_does_not_claim_notice(monkeypatch, missing):
    async def check(pool):
        adapter = Adapter()
        configure(monkeypatch, adapter)
        if missing == "sender":
            monkeypatch.setattr(admission_notices, "_sender", None)
        else:
            async def no_target():
                return None
            monkeypatch.setattr(gate_service, "dashboard_target", no_target)
        await alert()
        assert await system_settings.get_settings([KEY]) == {}
        assert adapter.calls == []

    run_db(check, monkeypatch)
