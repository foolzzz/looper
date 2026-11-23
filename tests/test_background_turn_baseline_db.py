"""The background-entry extraction preserves fixed completion and disabled-idle contracts."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from looper.agents import chat_settings, completion_events, idle_checks, lanes_settings, session_routing
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
from looper.db import projects, tasks
from tests.api_manager_support import USAGE, _manager
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
BASELINE = json.loads((Path(__file__).parent / "fixtures" / "background_turn_baseline.json").read_text())
assert BASELINE["commit"] == "b977f802f230e31069610bbee1bf0e012898b614"


@pytest.mark.parametrize("case", ["empty", "error", "timeout", "stop"])
def test_completion_failures_keep_record_input_metadata_notice_and_settle_order(monkeypatch, tmp_path, case):
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
    monkeypatch.setattr(chat_settings, "_metrics_enabled", False)
    monkeypatch.setattr(lanes_settings, "enabled", lambda: False)
    monkeypatch.setattr(session_routing, "routes", lambda _: False)
    if case == "timeout":
        monkeypatch.setattr(completion_events, "TURN_SECONDS", 3.0)

    async def run(pool):
        gate = asyncio.Event() if case in {"timeout", "stop"} else None
        script = (ResponseScript(status=400, error={"message": "synthetic failure"}) if case == "error" else
                  ResponseScript(events=text_events("late" if gate else "", usage=USAGE), gate=gate))
        async with _manager(pool, tmp_path, monkeypatch, [script]) as chat:
            project = await projects.create_project(name="baseline-project", work_dir=str(tmp_path), pool=pool)
            await tasks.create_task(task_id="baseline-task", requirement="baseline task", title="baseline task",
                                    project_id=project["id"], work_dir=str(tmp_path), status="completed",
                                    origin_thread="baseline-chat", pool=pool)
            await chat.manager._handle(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
                payload={"task_id": "baseline-task", "goal_id": "baseline-goal", "status": "completed",
                         "summary": "Done"}, metadata={"task_title": "baseline task",
                         "origin_thread": "baseline-chat", "origin_channel_id": "channel-web"}))
            due = completion_events.wake(MANAGER_ADDRESS,
                                        completion_events.Target("manager", "channel-web", "baseline-chat"))
            observations = []
            publish, settle = chat.bus.publish, chat.bus.settle_handled

            async def publishing(envelope):
                if envelope.message_type is MessageType.AGENT_REPLY:
                    observations.append(("notice", envelope.payload))
                await publish(envelope)

            def settling(envelope):
                if envelope.message_id == due.message_id:
                    observations.append(("settled", envelope.message_type.value))
                settle(envelope)

            monkeypatch.setattr(chat.bus, "publish", publishing)
            monkeypatch.setattr(chat.bus, "settle_handled", settling)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            chat.manager._sessions["main"].clean_start = True
            await chat.bus.publish(due)
            if case == "stop":
                await asyncio.wait_for(script.gate_reached.wait(), 20)
                token = chat.manager._sessions["main"].running_token
                assert (await chat.manager.stop_turn(token.message_id, via="web")).value == "stopping"
            await until(lambda: any(kind == "settled" for kind, _ in observations), "completion fallback settled")
            if gate is not None:
                gate.set()
            assert observations == [("notice", BASELINE["fallback_payload"]),
                                    ("settled", MessageType.COMPLETION_DUE.value)]
            event = await pool.fetchrow("SELECT * FROM manager_events")
            assert (event["state"], event["reason"]) == ("fallback", BASELINE["fallback_reasons"][case])
            assert (event["record_id"] is not None) is BASELINE["event_record_link"][case]
            record = await pool.fetchrow("SELECT * FROM llm_records WHERE agent_id = 'manager' "
                                         "AND task_id IS NULL ORDER BY id DESC LIMIT 1")
            assert record is not None
            assert record["input_text"] == BASELINE["completion_input"]
            metadata = json.loads(record["metadata"])
            expected = {**BASELINE["completion_metadata"], "batch": str(event["batch"])}
            assert metadata["completion"] == expected
            assert "lane" not in metadata and "turn_metrics" not in metadata
            assert len(chat.model.requests) == 1
            sent = json.dumps(chat.model.requests[0].body["input"], ensure_ascii=False)
            assert BASELINE["completion_input"] in sent.replace("\\n", "\n").replace('\\"', '"')

    run_db(run, monkeypatch, timeout=90)


def test_disabled_idle_ticks_keep_fixed_empty_storage_and_never_wake_a_running_manager(monkeypatch, tmp_path):
    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            for key in ("delivery.enabled", "attention.enabled", "manager.lanes.enabled"):
                await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'true') "
                                   "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            wakes = []
            publish = chat.bus.publish

            async def publishing(envelope):
                wakes.append(envelope.message_type)
                await publish(envelope)

            monkeypatch.setattr(chat.bus, "publish", publishing)
            service = idle_checks.IdleCheckService(chat.bus, idle_for=lambda: 7200.0,
                                                   manager_running=lambda: True)
            for _ in range(20):
                assert await service.run_once() is None
            assert service.skips[idle_checks.Skip.OFF] == 20
            assert wakes == [] and chat.model.requests == []
            actual = {table: await pool.fetchval(f"SELECT count(*) FROM {table}")
                      for table in BASELINE["off_counts"]}
            assert actual == BASELINE["off_counts"]

    run_db(run, monkeypatch, timeout=60)
