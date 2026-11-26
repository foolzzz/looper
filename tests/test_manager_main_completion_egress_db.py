"""Main-session completion replies stay with the operator; code notices still reach task origins."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import chat_settings, session_routing
from looper.agents.completion_service import CompletionService
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.task_notice import Notice, NotifyChannel, NotifyTarget, TargetKind, web_end_text
from looper.db import chat as db_chat, projects, tasks
from tests.api_manager_support import USAGE, _manager
from tests.db_support import DSN, run_db, until
from tests.fake_model_api import ResponseScript, text_events
from tests.schedule_support import TELEGRAM_CHAT, allow_telegram, run_scheduled, with_sender

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OPERATOR = "ou_synthetic_operator"
OTHER = "ou_synthetic_other"
THREAD = "completion-egress-thread"
MODEL_REPLY = "The synthetic report is ready."


@pytest.fixture(params=[False, True], ids=["routing_off", "routing_on"])
def routing(request, monkeypatch):
    monkeypatch.setattr(session_routing, "_routing_enabled", request.param)
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
    return request.param


def drain(bus, channel):
    queue = bus.get_queue(channel)
    return [queue.get_nowait() for _ in range(queue.qsize())]


async def finish_pending(chat):
    # Exercise the real completion wake and routing worker, including the isolated DM session.
    chat.manager._running = session_routing.routing_enabled()
    service = CompletionService(bus=chat.bus, manager_running=lambda _: True,
                                running_managers=lambda: ["manager"])
    await service.run_once(now=datetime.now(timezone.utc) + timedelta(seconds=61))
    for wake in drain(chat.bus, "manager"):
        if wake.message_type is MessageType.COMPLETION_DUE:
            await chat.manager._handle(wake)


async def settled_event(pool):
    """The event once its turn settled it: the reply is queued a moment before its row says ``delivered``."""
    for _ in range(100):
        row = await pool.fetchrow("SELECT * FROM manager_events")
        if row["state"] != "running":
            return row
        await asyncio.sleep(.05)
    return row


async def task_end(pool, root, *, thread=None, notice=None):
    project = await projects.create_project(name="synthetic", work_dir=str(root), pool=pool)
    await tasks.create_task(task_id="egress-task", requirement="Synthetic report", title="report",
                            status="completed", project_id=project["id"], work_dir=str(root),
                            origin_thread=thread, pool=pool)
    return MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
        payload={"task_id": "egress-task", "status": "completed", "summary": "Synthetic result",
                 **({"notice": notice.payload()} if notice else {})},
        metadata={"task_title": "report", **({"origin_thread": thread} if thread else {}),
                  **({"reply_route": {"chat_id": "oc_synthetic_dm"}} if notice else {})})


@pytest.mark.parametrize("client", [False, True], ids=["operator_dashboard", "api_client"])
def test_completion_thread_audience(monkeypatch, tmp_path, routing, client):
    async def check(pool):
        async with _manager(pool, tmp_path, monkeypatch,
                            [ResponseScript(events=text_events(MODEL_REPLY, usage=USAGE))]) as chat:
            if client:
                await pool.execute("INSERT INTO api_clients (id, name, token_sha256) VALUES "
                                   "('synthetic-client', 'Synthetic client', 'synthetic-hash')")
            await db_chat.claim_thread(THREAD, "manager", client_id="synthetic-client" if client else None)
            end = await task_end(pool, tmp_path, thread=THREAD)
            await chat.manager._handle_goal_status(end)
            if not client:
                await finish_pending(chat)
                await until(lambda: bool(chat.bus.get_queue("channel-web").qsize()), "completion reply")
            messages = drain(chat.bus, "channel-web")
            assert len(messages) == 1
            assert messages[0].payload["chat_session_id"] == THREAD
            if client:
                assert messages[0].payload["text"] == web_end_text("completed", "report", "Synthetic result")
                await finish_pending(chat)
                assert chat.model.requests == []
            else:
                assert messages[0].payload["text"].startswith(MODEL_REPLY)
                assert len(chat.model.requests) == 1
            row = await settled_event(pool)
            assert (row["state"], row["reason"]) == (
                ("skipped", "withheld_not_operator") if client else ("delivered", None))
            assert row["sent_at"] is not None

    run_db(check, monkeypatch, timeout=120)


@pytest.mark.parametrize("recipient", ["operator", "other", "unconfigured"])
def test_completion_dm_audience(monkeypatch, tmp_path, routing, recipient):
    async def check(pool):
        async with _manager(pool, tmp_path, monkeypatch,
                            [ResponseScript(events=text_events(MODEL_REPLY, usage=USAGE))]) as chat:
            target = OPERATOR if recipient == "operator" else OTHER
            config = {"allowed_open_ids": [OPERATOR, OTHER]}
            if recipient != "unconfigured":
                config["operator_sender_id"] = OPERATOR
            await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, enabled, config) "
                               "VALUES ('channel-lark', 'lark', TRUE, $1::jsonb)", json.dumps(config))
            notice = Notice(NotifyChannel.LARK, NotifyTarget(TargetKind.USER, target), "Synthetic code notice",
                            channel_id="channel-lark")
            end = await task_end(pool, tmp_path, notice=notice)
            await chat.manager._handle_goal_status(end)
            routed_dm = routing and recipient == "other"
            allowed = recipient == "operator" or routed_dm
            await finish_pending(chat)
            if routed_dm:
                # The guard leaves a routed DM session alone: the event waits for that session's own turn.
                row = await pool.fetchrow("SELECT * FROM manager_events")
                assert row["reason"] != "withheld_not_operator" and row["state"] != "skipped"
                assert any(key.startswith("dm:") for key in chat.manager._sessions)
                return
            if allowed:
                await until(lambda: bool(chat.bus.get_queue("channel-lark").qsize()), "completion DM")
            messages = drain(chat.bus, "channel-lark")
            assert len(messages) == 1
            assert messages[0].message_type is MessageType.CHAT_NOTICE
            assert messages[0].payload["chat"] == target
            assert messages[0].payload["text"] == (
                f"{MODEL_REPLY}\n\n[/#tasks/egress-task](/#tasks/egress-task)" if allowed else notice.text)
            assert len(chat.model.requests) == int(allowed)
            row = await settled_event(pool)
            assert (row["state"], row["reason"]) == (
                ("delivered", None) if allowed else ("skipped", "withheld_not_operator"))
            assert row["sent_at"] is not None

    run_db(check, monkeypatch, timeout=120)


def test_routed_dm_completion_falls_back_with_the_code_notice(monkeypatch, tmp_path):
    """Another sender's DM session does not run the completion model; its code notice still goes out."""
    monkeypatch.setattr(session_routing, "_routing_enabled", True)
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)

    async def check(pool):
        async with _manager(pool, tmp_path, monkeypatch,
                            [ResponseScript(events=text_events(MODEL_REPLY, usage=USAGE))]) as chat:
            await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, enabled, config) "
                               "VALUES ('channel-lark', 'lark', TRUE, $1::jsonb)",
                               json.dumps({"allowed_open_ids": [OPERATOR, OTHER], "operator_sender_id": OPERATOR}))
            notice = Notice(NotifyChannel.LARK, NotifyTarget(TargetKind.USER, OTHER), "Synthetic code notice",
                            channel_id="channel-lark")
            await chat.manager._handle_goal_status(await task_end(pool, tmp_path, notice=notice))
            await finish_pending(chat)

            def dm_settled() -> bool:
                sessions = [session for key, session in chat.manager._sessions.items() if key.startswith("dm:")]
                return bool(sessions) and all(not chat.manager._router.holds(session) for session in sessions)

            await until(dm_settled, "routed DM completion")
            row = await pool.fetchrow("SELECT state, reason, delivery, sent_at, batch FROM manager_events")
            assert (row["state"], row["reason"]) == ("fallback", "session_allowlist")
            assert row["batch"] is not None and row["delivery"] == "sent" and row["sent_at"] is not None
            messages = drain(chat.bus, "channel-lark")
            assert len(messages) == 1
            assert messages[0].message_type is MessageType.CHAT_NOTICE
            assert messages[0].payload["chat"] == OTHER
            assert messages[0].payload["text"] == notice.text
            assert chat.model.requests == []

    run_db(check, monkeypatch, timeout=120)


@pytest.mark.parametrize("recipient", ["operator", "other", "unconfigured"])
def test_schedule_dm_audience(monkeypatch, tmp_path, routing, recipient):
    async def check(env):
        sender = with_sender(env)
        await allow_telegram(env)
        if recipient != "unconfigured":
            await env.pool.execute("UPDATE channel_configs SET config = config || $1::jsonb "
                                   "WHERE channel_id = 'channel-telegram'",
                                   json.dumps({"operator_sender_id": TELEGRAM_CHAT if recipient == "operator"
                                               else "1000099"}))
        schedule = await env.schedule(notify="always", notify_target={"channel": "telegram", "chat": TELEGRAM_CHAT})
        run_id = await env.scheduler.run_now(schedule["schedule_id"])
        run = await env.pool.fetchrow("SELECT * FROM schedule_runs WHERE id = $1", run_id)
        await env.end_task(run["task_id"], "completed", summary="Synthetic report")
        await env.scheduler.idle()
        # With routing on, a schedule's direct chat never runs in main, whoever its recipient is.
        allowed = recipient == "operator" or routing
        row = await env.pool.fetchrow("SELECT * FROM manager_events")
        assert (row["state"], row["reason"]) == (
            ("pending", None) if allowed else ("skipped", "withheld_not_operator"))
        assert len(sender.sent) == int(not allowed)
        if not allowed:
            assert "Synthetic report" in sender.sent[0][2]
            assert row["sent_at"] is not None
        assert await env.pool.fetchval("SELECT count(*) FROM llm_records") == 0

    run_scheduled(check, monkeypatch, tmp_path)
