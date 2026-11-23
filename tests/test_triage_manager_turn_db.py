"""The Manager takes triage batches as background turns on the shared entry: loopback model, synthetic posts only."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import chat_settings, completion_events, lanes_settings, proactive_delivery, triage_settings, turn_metrics
from looper.agents.channels.base import PostReceipt
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.runtimes.api.session import ApiSession
from looper.agents.tiers import triage_background
from looper.db import manager_events as store
from tests.api_manager_support import USAGE, _manager, _message
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, text_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
CHAT = "ou_synthetic_triage"
TARGET = completion_events.Target("manager", "channel-lark", CHAT)
NOW = datetime.now(timezone.utc)


def _script(text, gate=None):
    return ResponseScript(events=text_events(text, usage=USAGE), gate=gate)


@pytest.fixture(autouse=True)
def _policy(monkeypatch):
    monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
    monkeypatch.setattr(chat_settings, "_delivery_enabled", True)
    monkeypatch.setattr(lanes_settings, "enabled", lambda: False)
    monkeypatch.setattr(proactive_delivery, "load_policy", lambda: proactive_delivery.DeliveryPolicy(
        timezone=timezone.utc, quiet_hours=None))


async def _handed_off(pool, n=1):
    await triage_settings.update(True)
    await pool.execute("INSERT INTO event_sources (id, kind, name, user_open_id) VALUES ('stream', 'lark_user', 'S', $1) "
                       "ON CONFLICT DO NOTHING", CHAT)
    await pool.execute("INSERT INTO source_chats (source_id, chat_id, kind, name) VALUES "
                       "('stream', 'oc_ops', 'group', 'Ops room') ON CONFLICT DO NOTHING")
    for number in range(n):
        seq = await pool.fetchval(
            "INSERT INTO source_events (source_id, external_id, chat_id, sender_id, sender_kind, sent_at, msg_type, "
            "mentions_me, text, body_bytes, origin) VALUES ('stream', $1, 'oc_ops', 'ou_alice', 'user', $2, 'text', "
            "true, 'synthetic', 1, 'live') RETURNING seq", f"om_{uuid.uuid4().hex[:8]}", NOW)
        event_id = await pool.fetchval(
            "INSERT INTO triage_events (source_id, category, title, summary, reason, reason_kind, confidence, "
            "first_sent_at, last_sent_at) VALUES ('stream', 'important', 'Synthetic outage', 'The synthetic service "
            "is down', 'Needs a decision', 'model', 'high', $1, $1) RETURNING id", NOW)
        await pool.execute("INSERT INTO triage_event_sources (event_id, event_seq) VALUES ($1, $2)", event_id, seq)
        row = (await store.record(
            manager_id="manager", task_id=None, origin="chat", target_kind="dm", channel_id="channel-lark", chat=CHAT,
            dedupe_key=f"triage:{event_id}", payload={"version": "triage_v1", "event_id": event_id, "messages": 1},
            fallback=[], state="pending", reason=None, kind="triage", pool=pool)).row
        await pool.execute("UPDATE triage_events SET manager_event_id = $2 WHERE id = $1", event_id, row["id"])


def _wake():
    return completion_events.wake(MANAGER_ADDRESS, TARGET)


async def _states(pool):
    return [tuple(row) for row in await pool.fetch("SELECT state, reason, delivery FROM manager_events ORDER BY id")]


@pytest.mark.parametrize("reply,state,sent", [
    ("NO_REPLY", ("skipped", "manager_silent", None), False),
    ("The synthetic service is down; it needs a decision.", ("delivered", None, "sent"), True),
    ("NO_REPLY but tell them anyway", ("fallback", "empty_reply", None), False)])
def test_a_triage_batch_runs_as_a_background_turn_and_settles_by_the_reply(monkeypatch, tmp_path, reply, state, sent):
    async def run(pool):
        post = AsyncMock(return_value=PostReceipt(True, external_ids=("synthetic-triage",)))
        monkeypatch.setattr(triage_background, "_post", post)
        async with _manager(pool, tmp_path, monkeypatch, [_script(reply)]) as chat:
            await _handed_off(pool, 2)
            await chat.manager._handle(_wake())
            assert await _states(pool) == [state, state]
            record_id = await pool.fetchval("SELECT record_id FROM manager_events ORDER BY id LIMIT 1")
            await asyncio.gather(*turn_metrics._pending)  # metrics are written after the batch settles
            metadata = json.loads(await pool.fetchval("SELECT metadata FROM llm_records WHERE id=$1", record_id))
            assert metadata["triage"]["event_ids"] and metadata["turn_metrics"]["origin"] == "triage"
            prompt = json.dumps(chat.model.requests[0].body["input"])
            assert "[TRIAGE]" in prompt and "Synthetic outage" in prompt and '<untrusted source=\\"message_stream\\"' in prompt
            assert len(chat.model.requests) == 1 and not chat.manager.busy
            if sent:
                post.assert_awaited_once()
                assert post.await_args.args[2] == reply and post.await_args.kwargs["idempotency_key"].startswith("triage-")
            else:
                post.assert_not_awaited()
            await chat.manager._handle(_wake())  # nothing is left to claim: no second turn, no second message
            assert len(chat.model.requests) == 1
    run_db(run, monkeypatch, timeout=60)


def test_with_the_delivery_layer_off_a_triage_batch_is_not_taken_and_no_turn_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_delivery_enabled", False)

    async def run(pool):
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await _handed_off(pool)
            await chat.manager._handle(_wake())
            # Not claimed into a turn (completion turns are off too: the sweep settles it), and nothing is sent.
            assert await _states(pool) == [("fallback", "off", None)] and not chat.model.requests
            # The Manager turned off: no turn is taken, the batch falls back for the brief, nothing is sent.
            await _handed_off(pool)
            chat.manager._turned_off = True
            await chat.manager._handle(_wake())
            assert (await _states(pool))[-1] == ("fallback", "off", None) and not chat.model.requests
    run_db(run, monkeypatch, timeout=60)


def test_the_turn_past_the_daily_cap_falls_back_without_running_the_model(monkeypatch, tmp_path):
    async def run(pool):
        post = AsyncMock()
        monkeypatch.setattr(triage_background, "_post", post)
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await _handed_off(pool)
            await triage_settings.update(max_turns=1)
            earlier = uuid.uuid4()
            await pool.execute(
                "INSERT INTO manager_events (manager_id, kind, origin, target_kind, channel_id, chat, dedupe_key, "
                "payload, fallback, state, batch, started_at, reason, finished_at) VALUES ('manager', 'triage', 'chat', "
                "'dm', 'channel-lark', $1, 'earlier', jsonb_build_object('admitted_at', now()::text), '[]', 'skipped', $2, now(), 'manager_silent', now())",
                CHAT, earlier)
            await chat.manager._handle(_wake())
            assert (await _states(pool))[0] == ("fallback", "daily_cap", None)
            assert not chat.model.requests
            post.assert_not_awaited()
    run_db(run, monkeypatch, timeout=60)


def test_a_turn_past_its_deadline_falls_back_and_sends_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(completion_events, "TURN_SECONDS", 1.0)
    monkeypatch.setattr(completion_events, "END_SECONDS", 0.02)
    monkeypatch.setattr(ApiSession, "can_interrupt", False)

    async def run(pool):
        post = AsyncMock()
        monkeypatch.setattr(triage_background, "_post", post)
        slow = _script("Never sent", asyncio.Event())
        async with _manager(pool, tmp_path, monkeypatch, [slow]) as chat:
            await _handed_off(pool)
            await asyncio.wait_for(chat.manager._handle(_wake()), 20)
            assert await _states(pool) == [("fallback", "turn_timeout", None)]
            post.assert_not_awaited()
            assert await store.claim_deliveries(pool=pool) == []
    run_db(run, monkeypatch, timeout=60)


def test_the_user_preempts_a_triage_turn_which_falls_back_and_is_told_once(monkeypatch, tmp_path):
    monkeypatch.setattr(ApiSession, "can_interrupt", True)
    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)

    async def run(pool):
        post = AsyncMock()
        monkeypatch.setattr(triage_background, "_post", post)
        gate = asyncio.Event()
        triage = _script("A stale reply must not go out", gate)
        async with _manager(pool, tmp_path, monkeypatch, [triage, _script("User answer")]) as chat:
            await _handed_off(pool)
            settled = []
            original = chat.bus.settle_handled
            monkeypatch.setattr(chat.bus, "settle_handled", lambda e: (settled.append(e.message_id), original(e)))
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(_wake())
            await asyncio.wait_for(triage.gate_reached.wait(), 15)
            user = _message("Please answer me")
            await chat.bus.publish(user)
            await until(lambda: user.message_id in settled, "triage fallback and user reply")
            assert await _states(pool) == [("fallback", "turn_stopped", None)]
            request = json.dumps(chat.model.requests[1].body["input"])
            assert request.count(triage_background._INTERRUPTED) == 1
            post.assert_not_awaited()
    run_db(run, monkeypatch, timeout=60)


def _fleet(chat, tmp_path):
    from looper.agents.managers import ManagerFleet
    from looper.agents.supervisor import AgentSupervisor

    fleet = ManagerFleet(platform=chat.ops.platform, supervisor=AgentSupervisor(chat.bus, tmp_path),
                         runtime_root=tmp_path, url="http://127.0.0.1:1", config=chat.manager._config, broadcaster=None)
    fleet._agents[chat.manager.agent_id] = chat.manager
    return fleet


def test_a_user_message_arriving_while_the_events_load_stops_the_turn_before_any_model_call(monkeypatch, tmp_path):
    from looper.agents import triage_turn

    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)

    async def run(pool):
        post = AsyncMock()
        monkeypatch.setattr(triage_background, "_post", post)
        loading, release = asyncio.Event(), asyncio.Event()
        load = triage_turn.load

        async def slow_load(rows):
            loading.set()
            await release.wait()
            return await load(rows)

        monkeypatch.setattr(triage_turn, "load", slow_load)
        async with _manager(pool, tmp_path, monkeypatch, [_script("User answer")]) as chat:
            await _handed_off(pool)
            settled = []
            original = chat.bus.settle_handled
            monkeypatch.setattr(chat.bus, "settle_handled", lambda e: (settled.append(e.message_id), original(e)))
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(_wake())
            await asyncio.wait_for(loading.wait(), 15)
            user = _message("Please answer me")
            await chat.bus.publish(user)
            await until(lambda: user.message_id in settled, "triage fallback and user reply")
            release.set()
            assert await _states(pool) == [("fallback", "turn_stopped", None)]
            assert len(chat.model.requests) == 1  # the user's only: the triage turn never reached the model
            assert "Synthetic outage" not in json.dumps(chat.model.requests[0].body["input"])
            post.assert_not_awaited()
    run_db(run, monkeypatch, timeout=60)


def test_a_message_already_queued_before_the_turn_starts_runs_no_triage_model_call(monkeypatch, tmp_path):
    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)

    async def run(pool):
        post = AsyncMock()
        monkeypatch.setattr(triage_background, "_post", post)
        async with _manager(pool, tmp_path, monkeypatch, []) as chat:
            await _handed_off(pool)
            chat.manager._router.dispatch(chat.manager._sessions["main"], _message("Already waiting"))
            batch = uuid.uuid4()
            rows = await store.claim(*TARGET.key, batch, kind="triage")
            assert rows and await triage_settings.enabled()
            await asyncio.wait_for(triage_background.tell(chat.manager, rows, batch), 15)
            assert await _states(pool) == [("fallback", "turn_stopped", None)]
            assert chat.model.requests == []
            post.assert_not_awaited()
    run_db(run, monkeypatch, timeout=60)


def test_chat_stop_through_the_fleet_stops_a_running_triage_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(ApiSession, "can_interrupt", True)
    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
    monkeypatch.setattr(chat_settings, "_stop_enabled", True)

    async def run(pool):
        post = AsyncMock()
        monkeypatch.setattr(triage_background, "_post", post)
        gate = asyncio.Event()
        triage = _script("A stopped reply must not go out", gate)
        async with _manager(pool, tmp_path, monkeypatch, [triage]) as chat:
            await _handed_off(pool)
            await chat.bus.start()
            await chat.manager.start(chat.bus)
            await chat.bus.publish(_wake())
            await asyncio.wait_for(triage.gate_reached.wait(), 15)
            fleet = _fleet(chat, tmp_path)
            other = await fleet.stop_chat("channel-lark", "oc_actual_synthetic", via="lark", sender_id="ou_someone_else")
            assert other.matched == 0
            summary = await fleet.stop_chat("channel-lark", "oc_actual_synthetic", via="lark", sender_id=CHAT)
            assert summary.matched == summary.stopping == 1
            await until(lambda: not chat.manager._turns.live(), "the triage turn ended")
            for _ in range(100):  # the batch settles just after the token does
                if (await _states(pool))[0][0] != "running":
                    break
                await asyncio.sleep(0.05)
            assert await _states(pool) == [("fallback", "turn_stopped", None)]
            post.assert_not_awaited()
    run_db(run, monkeypatch, timeout=60)
