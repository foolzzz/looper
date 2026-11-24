"""Quota probes retain the user message until an account can answer it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import admission, lanes_settings, session_routing
from looper.agents.message import MessageType
from tests.api_manager_support import USAGE, WEB, _manager, _message
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, call_events, failed, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
PROVIDER = "prov-api-test"


@pytest.mark.parametrize("stop", [False, True])
def test_failed_quota_probe_retains_message_until_recovery_or_stop(monkeypatch, tmp_path, stop):
    async def run(pool):
        c = admission.Controller()
        c.active = True
        monkeypatch.setattr(admission, "_controller", c)
        for name in ("slot", "transition", "wake", "snapshot", "register", "release"):
            monkeypatch.setattr(admission, name, getattr(c, name))
        monkeypatch.setattr(admission, "_freeze_alert", AsyncMock())
        monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
        monkeypatch.setattr(session_routing, "routes", lambda _: False)
        scripts = [ResponseScript(status=429, headers={"retry-after-ms": "1"}) for _ in range(3)]
        scripts.append(ResponseScript(events=text_events("Recovered answer.", usage=USAGE)))
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('engine.failure_typing','record')")
            await c.transition(PROVIDER, admission.QuotaFailure(datetime.now(timezone.utc) + timedelta(hours=1), None))
            settled = []
            original = chat.bus.settle_handled
            monkeypatch.setattr(chat.bus, "settle_handled", lambda e: (settled.append(e.message_id), original(e)))
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            user = _message("Answer after the reset.")
            await chat.bus.publish(user)
            try:
                await until(lambda: len(c.waiters) == 1, "user waits frozen")
                c.now = lambda: datetime.now(timezone.utc) + timedelta(hours=1, minutes=2)
                await c.wake()
                await until(lambda: len(chat.model.requests) == 3, "real quota probe exhausted API retries")
                await until(lambda: bool(c.waiters) or user.message_id in settled, "probe requeued or incorrectly settled")
                assert user.message_id not in settled
                assert c._state(PROVIDER).state is admission.AdmissionState.FROZEN
                assert len(c.waiters) == 1 and not c.running
                assert chat.manager._turns.find(user.message_id).admission_waiting
                rows = await pool.fetch("SELECT status,failure_class FROM llm_records")
                assert [tuple(row) for row in rows] == [("failed", "quota")]
                queue = chat.bus.get_queue(WEB.agent_id)
                assert not any(e.message_type is MessageType.AGENT_REPLY for e in queue._queue)
                assert user.message_id in [e.message_id for e in chat.bus.journal.undelivered()]
                if stop:
                    result = await chat.manager.stop_turn(user.message_id, via="test")
                    assert result.value == "stopping"
                else:
                    await c.transition(PROVIDER, admission.OperatorClear())
                await until(lambda: user.message_id in settled, "retained message answered")
                replies = [e.payload.get("text") for e in queue._queue if e.message_type is MessageType.AGENT_REPLY]
                if stop:
                    assert len(replies) == 1 and "已停止" in replies[0]
                    assert len(chat.model.requests) == 3
                else:
                    assert replies == ["Recovered answer."]
                    assert len(chat.model.requests) == 4
                assert settled.count(user.message_id) == 1
                assert not c.waiters and not c.running
            finally:
                await chat.manager.stop()
                await c.stop()

    run_db(run, monkeypatch, timeout=60)


def test_quota_probe_with_streamed_output_is_not_replayed(monkeypatch, tmp_path):
    async def run(pool):
        c = admission.Controller()
        c.active = True
        monkeypatch.setattr(admission, "_controller", c)
        for name in ("slot", "transition", "wake"):
            monkeypatch.setattr(admission, name, getattr(c, name))
        monkeypatch.setattr(admission, "_freeze_alert", AsyncMock())
        monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
        monkeypatch.setattr(session_routing, "routes", lambda _: False)
        script = ResponseScript(events=[text_events("Already displayed.")[0], failed("rate_limit_exceeded")])
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('engine.failure_typing','record')")
            await c.transition(PROVIDER, admission.QuotaFailure(None, None))
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            user = _message("Do not repeat visible output.")
            await chat.bus.publish(user)
            try:
                await until(lambda: len(c.waiters) == 1, "user waits frozen")
                c.now = lambda: datetime.now(timezone.utc) + timedelta(minutes=6)
                await c.wake()
                queue = chat.bus.get_queue(WEB.agent_id)
                await until(lambda: any(e.message_type is MessageType.AGENT_REPLY for e in queue._queue),
                            "partial probe keeps normal failure behavior")
                await until(lambda: not chat.manager.busy, "failed turn ended")
                assert len(chat.model.requests) == 1
                assert not c.waiters and not c.running
                assert c._state(PROVIDER).state is admission.AdmissionState.FROZEN
                assert await pool.fetchval("SELECT failure_class FROM llm_records") == "quota"
                assert user.message_id not in [e.message_id for e in chat.bus.journal.undelivered()]
            finally:
                await chat.manager.stop()
                await c.stop()

    run_db(run, monkeypatch, timeout=40)


def test_quota_probe_with_a_tool_call_is_not_replayed(monkeypatch, tmp_path):
    """A probe that already called a tool must fail in place. Replaying it would run the tool again."""
    async def run(pool):
        c = admission.Controller()
        c.active = True
        monkeypatch.setattr(admission, "_controller", c)
        for name in ("slot", "transition", "wake"):
            monkeypatch.setattr(admission, name, getattr(c, name))
        monkeypatch.setattr(admission, "_freeze_alert", AsyncMock())
        monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
        monkeypatch.setattr(session_routing, "routes", lambda _: False)
        scripts = [ResponseScript(events=call_events("{}", name="looper_overview"))]
        scripts.extend(ResponseScript(status=429, headers={"retry-after-ms": "1"}) for _ in range(3))
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            calls = []

            async def record_call(name, raw, **kwargs):
                calls.append(name)
                return {"status": "ok"}

            monkeypatch.setattr(chat.ops, "call", record_call)
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('engine.failure_typing','record')")
            await c.transition(PROVIDER, admission.QuotaFailure(None, None))
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            user = _message("Do not repeat a tool call.")
            await chat.bus.publish(user)
            try:
                await until(lambda: len(c.waiters) == 1, "user waits frozen")
                c.now = lambda: datetime.now(timezone.utc) + timedelta(minutes=6)
                await c.wake()
                await until(lambda: len(chat.model.requests) >= 4, "tool call then quota attempts")
                queue = chat.bus.get_queue(WEB.agent_id)
                await until(lambda: any(e.message_type is MessageType.AGENT_REPLY for e in queue._queue)
                            or (len(c.waiters) == 1 and not c.running),
                            "tool probe failed in place or was incorrectly replayed")
                assert any(e.message_type is MessageType.AGENT_REPLY for e in queue._queue)
                await until(lambda: not chat.manager.busy, "failed turn ended")
                assert calls == ["looper_overview"]
                assert len(chat.model.requests) == 4
                assert not c.waiters and not c.running
                assert c._state(PROVIDER).state is admission.AdmissionState.FROZEN
                assert await pool.fetchval("SELECT failure_class FROM llm_records") == "quota"
                assert user.message_id not in [e.message_id for e in chat.bus.journal.undelivered()]
            finally:
                await chat.manager.stop()
                await c.stop()

    run_db(run, monkeypatch, timeout=40)
