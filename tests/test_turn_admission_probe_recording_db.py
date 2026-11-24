"""A delayed recorder never turns a failed quota probe into a consumed user message."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import admission, claude_cli, failures, lanes_settings, session_routing
from looper.agents.message import MessageType
from looper.agents.tiers.turn_token import TurnPhase
from looper.db import provider_admission
from tests.api_manager_support import USAGE, WEB, _manager, _message
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
PROVIDER = "prov-api-test"


def test_probe_quota_evidence_upgrades_an_untyped_admission_report_once(monkeypatch):
    monkeypatch.setattr(admission, "enabled", lambda: True)
    report = AsyncMock()
    monkeypatch.setattr(admission, "recorded", report)

    async def run():
        recorder = claude_cli.ClaudeRunRecorder(agent_id="manager", task_id=None,
            command=[], cwd="/workspace", input_text="Keep this question.")
        quota = failures.Failure(failures.FailureCause.RATE_LIMIT)
        await recorder.report_admission(None)
        await recorder.report_admission(quota)
        await recorder.report_admission(None)
        await recorder.report_admission(quota)
        assert [call.args[3] for call in report.await_args_list] == [None, quota]

    asyncio.run(run())


def test_cancelled_quota_admission_report_can_be_retried(monkeypatch):
    monkeypatch.setattr(admission, "enabled", lambda: True)
    report = AsyncMock(side_effect=[asyncio.CancelledError(), None])
    monkeypatch.setattr(admission, "recorded", report)

    async def run():
        recorder = claude_cli.ClaudeRunRecorder(agent_id="manager", task_id=None,
            command=[], cwd="/workspace", input_text="Keep this question.")
        quota = failures.Failure(failures.FailureCause.RATE_LIMIT)
        with pytest.raises(asyncio.CancelledError):
            await recorder.report_admission(quota)
        await recorder.report_admission(quota)
        await recorder.report_admission(quota)
        assert [call.args[3] for call in report.await_args_list] == [quota, quota]

    asyncio.run(run())


@pytest.mark.parametrize("delay,suppress_cancel", [
    ("record_close", False), ("failure_classification", False), ("failure_classification", True),
    ("admission_persistence", True),
])
def test_failed_quota_probe_survives_recording_timeout(monkeypatch, tmp_path, delay, suppress_cancel):
    async def run(pool):
        controller = admission.Controller()
        controller.active = True
        monkeypatch.setattr(admission, "_controller", controller)
        for name in ("slot", "transition", "wake"):
            monkeypatch.setattr(admission, name, getattr(controller, name))
        monkeypatch.setattr(admission, "_freeze_alert", AsyncMock())
        monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
        monkeypatch.setattr(session_routing, "routes", lambda _: False)
        monkeypatch.setattr(claude_cli, "CLOSE_TIMEOUT_SEC", 0.2)
        delayed, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def hold():
            delayed.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                if not suppress_cancel:
                    raise
                await release.wait()
            finally:
                cancelled.set()

        if delay == "record_close":
            original = claude_cli.ClaudeRunRecorder._write_end

            async def write_end(recorder, fields, basis=None):
                if not delayed.is_set():
                    await hold()
                return await original(recorder, fields, basis)

            monkeypatch.setattr(claude_cli.ClaudeRunRecorder, "_write_end", write_end)
        elif delay == "failure_classification":
            original = failures.recorded

            async def classify(runtime, result, evidence, **kwargs):
                if not delayed.is_set():
                    await hold()
                return await original(runtime, result, evidence, **kwargs)

            monkeypatch.setattr(failures, "recorded", classify)
        else:
            original = provider_admission.save

            async def persist(provider_id, previous, state, **kwargs):
                if kwargs.get("record_id") is not None and not delayed.is_set():
                    await hold()  # The initial freeze has no record; only delay the failed probe's save.
                return await original(provider_id, previous, state, **kwargs)

            monkeypatch.setattr(provider_admission, "save", persist)
        scripts = [ResponseScript(status=429, headers={"retry-after-ms": "1"}) for _ in range(3)]
        scripts.append(ResponseScript(events=text_events("Recovered despite recorder timeout.", usage=USAGE)))
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            await pool.execute("INSERT INTO system_settings (key,value) VALUES ('engine.failure_typing','record')")
            await controller.transition(PROVIDER, admission.QuotaFailure(None, None))
            settled = []
            settle = chat.bus.settle_handled

            def handled(envelope):
                settled.append(envelope.message_id)
                settle(envelope)

            monkeypatch.setattr(chat.bus, "settle_handled", handled)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            user = _message("Keep this even if the probe record cannot close.")
            await chat.bus.publish(user)
            try:
                await until(lambda: len(controller.waiters) == 1, "user waits for quota")
                controller.now = lambda: datetime.now(timezone.utc) + timedelta(minutes=6)
                await controller.wake()
                await asyncio.wait_for(delayed.wait(), 15)
                await asyncio.wait_for(cancelled.wait(), 5)
                if delay == "admission_persistence":
                    await until(lambda: (token := chat.manager._turns.find(user.message_id)) is None
                        or token.phase is TurnPhase.PREPARING,
                        "quota retry retained the token before slot cleanup waits for persistence")
                    assert user.message_id not in settled
                    assert chat.manager._turns.find(user.message_id) is not None
                    assert controller._state(PROVIDER).state is admission.AdmissionState.FROZEN
                    release.set()
                await until(lambda: bool(controller.waiters) or user.message_id in settled,
                            "failed probe either requeues or incorrectly settles")
                assert user.message_id not in settled
                assert len(chat.model.requests) == 3
                assert controller._state(PROVIDER).state is admission.AdmissionState.FROZEN
                assert len(controller.waiters) == 1 and not controller.running
                assert user.message_id in {e.message_id for e in chat.bus.journal.undelivered()}
                if delay != "admission_persistence":
                    assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE status='running'") == 1
                queue = chat.bus.get_queue(WEB.agent_id)
                assert not any(e.message_type is MessageType.AGENT_REPLY for e in queue._queue)
                await controller.wake()
                assert len(chat.model.requests) == 3  # Refrozen, rather than an immediate probe retry loop.
                await controller.transition(PROVIDER, admission.OperatorClear())
                await until(lambda: user.message_id in settled, "message answered after explicit recovery")
                assert [e.payload.get("text") for e in queue._queue if e.message_type is MessageType.AGENT_REPLY
                        ] == ["Recovered despite recorder timeout."]
                assert len(chat.model.requests) == 4 and settled.count(user.message_id) == 1
                assert not controller.waiters and not controller.running
                if suppress_cancel:
                    # The old database operation completes after recovery. Its quota result must not
                    # freeze the cleared account or apply to a newer turn's grant.
                    release.set()
                    async with asyncio.timeout(5):
                        while await pool.fetchval("SELECT count(*) FROM llm_records WHERE status='running'"):
                            await asyncio.sleep(0.02)
                    assert controller._state(PROVIDER).state is admission.AdmissionState.OPEN
            finally:
                release.set()
                await chat.manager.stop()
                await controller.stop()

    run_db(run, monkeypatch, timeout=40)
