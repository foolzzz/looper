"""The serve-owned idle service observes an operator setting change on its next tick."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from functools import partial
from types import SimpleNamespace

import pytest

from looper.agents import idle_check_settings, idle_checks, lanes_settings, proactive_delivery, serve
from looper.agents.bus import MessageBus
from looper.agents.drain import DrainState
from looper.agents.message import MessageType
from tests.db_support import DSN, run_with_channel_configs, until, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def restore_poster(monkeypatch):
    from looper.agents.tiers import idle_check_turn

    monkeypatch.setattr(idle_check_turn, "_post", idle_check_turn._post)


def test_enabling_idle_checks_reaches_the_next_tick_of_the_service_started_by_serve(monkeypatch, tmp_path):
    async def check(pool):
        await pool.execute("INSERT INTO system_settings (key, value) VALUES "
                           "('delivery.enabled', 'true'), ('attention.enabled', 'true'), "
                           "('manager.lanes.enabled', 'true')")
        await pool.execute("UPDATE channel_configs SET enabled = true, config = $1 WHERE channel_id = 'channel-lark'",
                           json.dumps({"operator_sender_id": "ou_idle_synthetic"}))
        await pool.execute("INSERT INTO projects (id, name) VALUES ('idle-project', 'Idle checks')")
        await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, "
                           "consecutive_failures) VALUES ('idle-schedule', 'idle-project', 'Synthetic report', "
                           "'0 9 * * *', 'UTC', 'Inspect report', 3)")
        bus = MessageBus(runtime_root=tmp_path)
        wakes = []
        bus.subscribe_topic(MessageType.IDLE_CHECK_DUE, lambda envelope: wakes.append(envelope))
        fleet = SimpleNamespace(default=SimpleNamespace(idle_for=lambda: 3600.0),
                                is_running=lambda manager_id: manager_id == "manager")
        monkeypatch.setattr(serve, "IdleCheckService", partial(idle_checks.IdleCheckService, interval=0.01))
        monkeypatch.setattr(proactive_delivery, "load_policy", lambda: proactive_delivery.DeliveryPolicy(
            timezone=timezone.utc, quiet_hours=None))

        async def no_send(*args, **kwargs):
            raise AssertionError("this startup test must not send a message")

        service = await serve._prepare_idle_checks(bus, fleet, DrainState(), post=no_send)
        await service.start()
        try:
            await until(lambda: service.skips[idle_checks.Skip.OFF] > 0, "the first disabled tick")
            assert await pool.fetchval("SELECT count(*) FROM idle_checks") == 0
            async with v1_api(tmp_path, pool) as call:
                status, answer = await call("idle_check_settings.update", {"enabled": True})
                assert (status, answer["result"]) == (200, {"enabled": True})
            await until(lambda: len(wakes) == 1, "the enabled tick's Manager wake")
            row = await pool.fetchrow("SELECT * FROM idle_checks WHERE id = $1", wakes[0].payload["check_id"])
            assert (row["state"], row["channel_id"], row["chat"]) == (
                "running", "channel-lark", "ou_idle_synthetic")
            assert json.loads(row["items"])[0]["key"] == "schedule:idle-schedule"
            assert wakes[0].target.agent_id == "manager"
        finally:
            await service.stop()
            await bus.shutdown()
        assert service._task is None

    run_with_channel_configs(check, monkeypatch)


def test_serve_service_tracks_the_current_default_manager_and_respects_drain(monkeypatch, tmp_path):
    async def check(pool):
        bus = MessageBus(runtime_root=tmp_path)
        fleet = SimpleNamespace(default=SimpleNamespace(idle_for=lambda: 1.0), is_running=lambda _: True)
        drain = DrainState()

        async def no_send(*args, **kwargs):
            raise AssertionError("this startup test must not send a message")

        service = await serve._prepare_idle_checks(bus, fleet, drain, post=no_send)
        try:
            assert service._idle_for() == 1.0
            fleet.default = SimpleNamespace(idle_for=lambda: 3600.0)
            assert service._idle_for() == 3600.0
            assert service._manager_running()
            fleet.is_running = lambda _: False
            assert not service._manager_running()
            assert service._drain is drain
        finally:
            await service.stop()
            await bus.shutdown()

    run_with_channel_configs(check, monkeypatch)


def test_turning_lanes_off_after_enabling_skips_checks_and_logs_once_per_day(monkeypatch, tmp_path, caplog):
    async def check(pool):
        await pool.execute("INSERT INTO system_settings (key, value) VALUES "
                           "('delivery.enabled', 'true'), ('attention.enabled', 'true'), "
                           "('manager.lanes.enabled', 'true')")
        assert await idle_check_settings.update(True) == {"enabled": True}
        await pool.execute("UPDATE system_settings SET value = 'false' WHERE key = 'manager.lanes.enabled'")
        now = datetime(2026, 11, 22, 10, tzinfo=timezone.utc)
        bus = MessageBus(runtime_root=tmp_path)
        service = idle_checks.IdleCheckService(bus, idle_for=lambda: 3600.0,
                                               manager_running=lambda: True, clock=lambda: now)
        try:
            with caplog.at_level(logging.INFO, logger="looper.agents.idle_checks"):
                assert await service.run_once() is None
                assert await service.run_once() is None
                now += timedelta(days=1)
                assert await service.run_once() is None
            assert await idle_check_settings.enabled()
            assert service.skips[idle_checks.Skip.LANES_OFF] == 3
            assert sum(record.getMessage() == "idle checks: not checking (lanes_off)"
                       for record in caplog.records) == 2
            assert await pool.fetchval("SELECT count(*) FROM idle_checks") == 0
        finally:
            await bus.shutdown()

    run_with_channel_configs(check, monkeypatch)


def test_a_graceful_stop_mid_check_is_recovered_before_the_restart_replays_it(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    from looper.agents.supervisor import AgentSupervisor
    from looper.agents.tiers import idle_check_turn
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.db import idle_checks as store
    from tests.api_manager_support import USAGE, _manager
    from tests.fake_model_api import ResponseScript, text_events

    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
    monkeypatch.setattr(proactive_delivery, "load_policy", lambda: proactive_delivery.DeliveryPolicy(
        timezone=timezone.utc, quiet_hours=None))

    async def check(pool):
        post = AsyncMock()
        monkeypatch.setattr(idle_check_turn, "_post", post)
        held = ResponseScript(events=text_events("Do not send after restart", usage=USAGE), gate=asyncio.Event())
        async with _manager(pool, tmp_path, monkeypatch, [held]) as chat:
            item = {"section": "logins", "key": "login:synthetic", "since": None,
                    "title": "Synthetic login", "facts": {}}
            check_id = await store.insert_running(pool, manager_id="manager",
                local_date=datetime.now(timezone.utc).date(), items=[item], channel_id="channel-lark",
                chat="ou_idle_synthetic")
            wake = idle_checks.due_envelope(check_id)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(wake)
            await asyncio.wait_for(held.gate_reached.wait(), 15)
            await chat.manager.stop()
            await chat.bus.shutdown()
            assert (await store.get(check_id))["state"] == "running"

            bus = MessageBus(runtime_root=tmp_path)
            manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
            supervisor = AgentSupervisor(bus, tmp_path)
            supervisor.register(manager)
            fleet = SimpleNamespace(default=manager, is_running=lambda _: True)
            service = await serve._prepare_idle_checks(bus, fleet, DrainState(), post=post)
            assert service._task is None  # no tick can create a row before replay
            try:
                await supervisor.start_all()
                await until(lambda: wake.message_id in bus.journal._delivered, "replayed wake settled")
                row = await store.get(check_id)
                assert (row["state"], row["reason"], row["items"]) == ("failed", "restart", [item])
                assert await store.mentioned_items(pool, "manager", datetime.now(timezone.utc) - timedelta(days=7)) == []
                assert len(chat.model.requests) == 1
                post.assert_not_awaited()
            finally:
                await supervisor.stop_all()
    run_with_channel_configs(check, monkeypatch)


def test_idle_recovery_failure_blocks_startup_before_replay(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    from looper.db import idle_checks as store

    async def check(pool):
        monkeypatch.setattr(store, "settle_running", AsyncMock(side_effect=RuntimeError("synthetic recovery failure")))
        bus = MessageBus(runtime_root=tmp_path)
        fleet = SimpleNamespace(default=SimpleNamespace(idle_for=lambda: 3600.0), is_running=lambda _: True)
        try:
            with pytest.raises(RuntimeError, match="synthetic recovery failure"):
                await serve._prepare_idle_checks(bus, fleet, DrainState(), post=AsyncMock())
            assert not bus._started
        finally:
            await bus.shutdown()
    run_with_channel_configs(check, monkeypatch)
