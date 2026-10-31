"""Server liveness stays available without querying the database; the scheduler reports completed passes."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import looper
from looper.agents import api_v1
from looper.agents.scheduler import Scheduler
from looper.db import api_clients, connection
from tests.db_support import DSN, run_db, until
from tests.schedule_support import Bus
from tests.test_api_v1_auth import TOKEN, _bearer, _send, _serving

HEALTH = "/api/v1/system/health"


def test_health_without_a_scheduler_does_not_need_the_database(tmp_path, monkeypatch):
    def unavailable():
        raise AssertionError("health must not query the database")

    monkeypatch.setattr(connection, "get_pool", unavailable)
    api_v1.find("POST", HEALTH)  # Load the registry before the fixture's short HTTP read timeout.

    async def run():
        async with _serving(tmp_path, listen=True) as server:
            before = datetime.now(timezone.utc)
            code, headers, answer = await _send(server, f"POST {HEALTH}", _bearer(TOKEN), "{}")
            assert code == 200 and headers["cache-control"] == "no-store"
            result = answer["result"]
            assert result["status"] == "ok"
            assert result["pid"] == os.getpid() and result["version"] == looper.__version__
            assert datetime.fromisoformat(result["started_at"]) <= before
            assert before <= datetime.fromisoformat(result["now"]) <= datetime.now(timezone.utc)
            assert result["scheduler"] == {
                "running": False, "tick_seconds": None, "last_tick_at": None,
                "last_ok_tick_at": None, "last_ok_tick_age_seconds": None,
                "command_runs": {"running": 0},
            }
            operation = api_v1.find("POST", HEALTH)
            assert operation.operation_id == "system.health" and operation.risk == "read"
            api_v1.validate_arguments(operation.result, result)
            code, _, _ = await _send(server, f"POST {HEALTH}", _bearer(TOKEN), '{"extra": true}')
            assert code == 400
            code, _, _ = await _send(server, f"POST {HEALTH}", {}, "{}")
            assert code == 401

    asyncio.run(run())


def test_scheduler_health_keeps_the_latest_success_when_a_later_pass_fails(monkeypatch):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    clock = SimpleNamespace(now=now)
    scheduler = Scheduler(bus=Bus(), clock=lambda: clock.now, day_zone="UTC")
    assert scheduler.health()["last_ok_tick_age_seconds"] is None
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConnectionError("database unavailable")

    async def sleep(_seconds):
        if calls == 2:
            raise asyncio.CancelledError
        clock.now += timedelta(seconds=15)

    monkeypatch.setattr(scheduler, "tick", tick)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await scheduler._loop()

    asyncio.run(run())
    health = scheduler.health()
    assert datetime.fromisoformat(health["last_tick_at"]) == now + timedelta(seconds=15)
    assert datetime.fromisoformat(health["last_ok_tick_at"]) == now
    assert health["last_ok_tick_age_seconds"] == 15
    assert health["command_runs"] == {"running": 0}


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_health_reports_a_real_scheduler_tick_and_stop_for_operator_and_client(tmp_path, monkeypatch):
    async def test(pool):
        scheduler = Scheduler(bus=Bus(), day_zone="UTC")
        _, client_token = await api_clients.create("health probe", pool=pool)
        try:
            await scheduler.start()
            await until(lambda: scheduler.health()["last_ok_tick_at"] is not None, "the first scheduler pass")
            async with _serving(tmp_path, listen=True) as server:
                server._scheduler = scheduler
                for token in (TOKEN, client_token):
                    code, _, answer = await _send(server, f"POST {HEALTH}", _bearer(token), "{}")
                    assert code == 200
                    health = answer["result"]["scheduler"]
                    assert health["running"] is True
                    assert health["tick_seconds"] == 15
                    assert health["last_tick_at"] == health["last_ok_tick_at"]
                    assert 0 <= health["last_ok_tick_age_seconds"] < 15
                    api_v1.validate_arguments(api_v1.find("POST", HEALTH).result, answer["result"])
                await scheduler.stop()
                code, _, answer = await _send(server, f"POST {HEALTH}", _bearer(TOKEN), "{}")
                assert code == 200 and answer["result"]["scheduler"]["running"] is False
                assert answer["result"]["scheduler"]["last_ok_tick_at"] == health["last_ok_tick_at"]
        finally:
            await scheduler.stop()

    run_db(test, monkeypatch, timeout=30)
