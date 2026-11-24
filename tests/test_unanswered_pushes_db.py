"""Unanswered direct pushes ask once, then pause; resume clears the reason and snoozes."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import attention, cost_guard, cost_guard_settings as settings, cost_holds, unanswered_pushes
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.db import connection, cost_holds as store, schedules as db_schedules
from looper.db.migrations import cost_holds_unanswered, printed_check, schedule_pause_reasons
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "unanswered_pushes_off.json").read_text())
_MOMENT = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_unanswered(monkeypatch):
    from looper.agents import chat_settings

    monkeypatch.setattr(cost_guard, "_guard", cost_guard.Guard(settings.Config()))
    monkeypatch.setattr(settings, "_config", settings.Config())
    monkeypatch.setattr(settings, "_problem", None)
    monkeypatch.setattr(unanswered_pushes, "_last_checked", None)
    monkeypatch.setattr(unanswered_pushes, "_warned_day", None)
    chat_settings.set_metrics_enabled(False)
    cost_holds.configure(sender=None, dashboard_url="")
    yield
    chat_settings.set_metrics_enabled(False)
    cost_holds.configure(sender=None, dashboard_url="")


def _project(value, template):
    if isinstance(template, dict):
        return {key: _project(value[key], child) for key, child in template.items()}
    if isinstance(template, list):
        assert len(value) == len(template)
        return [_project(item, template[index]) for index, item in enumerate(value)]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _section(summary, kind):
    return next((section for section in summary["sections"] if section["kind"] == kind), None)


async def _schedule(pool, schedule_id, name="Morning brief"):
    await pool.execute(
        "INSERT INTO schedules(id,project_id,name,cron,timezone,requirement,enabled) "
        "VALUES ($1,'p-ask',$2,'0 9 * * *','UTC','Post the brief',true)", schedule_id, name)


async def _pushes(pool, schedule_id, count, sent_at, *, kind="direct", replied=0, source="schedule"):
    for index in range(count):
        run_id = f"{schedule_id}-{index}"
        await pool.execute(
            "INSERT INTO schedule_runs(id,schedule_id,scheduled_for,trigger,status) "
            "VALUES ($1,$2,$3,'scheduled','completed')", run_id, schedule_id, sent_at)
        replied_at = sent_at if index < replied else None
        await pool.execute(
            "INSERT INTO proactive_deliveries(source,source_ref,channel_id,chat,chat_kind,outcome,sent_at,"
            "replied_at,reply_match) VALUES ($1,$2,'web','direct-chat',$3,'sent',$4,$5,$6)",
            source, run_id, kind, sent_at, replied_at, "next_message" if replied_at else None)


async def _enable(**rule):
    from looper.agents import chat_settings

    chat_settings.set_metrics_enabled(True)
    await settings.update({"unanswered_pushes": {"enabled": True, "window_days": 3, "min_pushes": 15,
                                                 "pause_after_days": 3, "snooze_days": 30, **rule}})
    unanswered_pushes._last_checked = None


def _capture(monkeypatch):
    sent = []

    async def sender(channel_id, chat, text, **kwargs):
        sent.append({"channel_id": channel_id, "chat": chat, "text": text, **kwargs})
        return True, None

    cost_holds.configure(sender=sender, dashboard_url="https://console.example")
    monkeypatch.setattr("looper.agents.gate_service.dashboard_target", AsyncMock(return_value=SimpleNamespace(
        channel_id="web", target=SimpleNamespace(id="direct-chat", group=False))))
    return sent


def _envelope(text):
    return MessageEnvelope.create(source=channel_address("lark", "lark-bot"), target=MANAGER_ADDRESS,
        message_type=MessageType.USER_MESSAGE, payload={"text": text, "channel_context": {
            "sender_id": "operator", "chat_id": "synthetic-chat", "chat_type": "p2p"}})


def test_switch_off_matches_the_recorded_fixture(monkeypatch):
    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('p-off','Off')")
        await pool.execute(
            "INSERT INTO schedules(id,project_id,name,cron,timezone,requirement,enabled) VALUES "
            "('sched-pushed','p-off','Morning brief','0 9 * * *','UTC','Post the brief',true),"
            "('sched-disabled','p-off','Disabled brief','0 10 * * *','UTC','Stay off',false),"
            "('sched-failing','p-off','Failing brief','0 11 * * *','UTC','Has failed',true)")
        await pool.execute(
            "UPDATE schedules SET consecutive_failures=2, failures_alert_at=$2 WHERE id=$1",
            "sched-failing", datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
        sent = _MOMENT - timedelta(hours=2)
        for index in range(20):
            run_id = f"run-{index}"
            await pool.execute(
                "INSERT INTO schedule_runs(id,schedule_id,scheduled_for,trigger,status) "
                "VALUES ($1,'sched-pushed',$2,'scheduled','completed')", run_id, sent)
            await pool.execute(
                "INSERT INTO proactive_deliveries(source,source_ref,channel_id,chat,chat_kind,outcome,sent_at) "
                "VALUES ('schedule',$1,'web','direct-chat','direct','sent',$2)", run_id, sent)
        await pool.execute(
            "INSERT INTO cost_holds(scope,scope_ref,currency,limit_usd,spent_usd,reason) "
            "VALUES ('task','task-off','provider_priced_usd',2,1,'budget')")
        assert await pool.fetchval("SELECT id FROM cost_holds") == 1
        cost_guard.current().holds_need_recovery = False
        calls = {"n": 0}

        def queried():
            calls["n"] += 1
            raise AssertionError("check queried the database")

        monkeypatch.setattr(connection, "get_pool", queried)
        await unanswered_pushes.check(_MOMENT)
        assert calls["n"] == _FIXTURE["queries_on_check"] == 0
        assert unanswered_pushes._last_checked is None
        monkeypatch.setattr(connection, "get_pool", lambda: pool)

        failing = await db_schedules.failing()
        assert _project(failing, _FIXTURE["failing"]) == _FIXTURE["failing"]
        summary = await attention.collect()
        schedules = _section(summary, "schedules")
        assert _project(schedules, _FIXTURE["attention"]["schedules"]) == _FIXTURE["attention"]["schedules"]
        assert _section(summary, "cost") is _FIXTURE["attention"]["cost"]
        async with pool.acquire() as conn:
            await db_schedules.update_schedule(conn, "sched-disabled", {"enabled": True})
            resumed = dict(await conn.fetchrow(
                "SELECT id, name, enabled, pause_reason, paused_at, unanswered_snooze_until "
                "FROM schedules WHERE id='sched-disabled'"))
        assert _project(resumed, _FIXTURE["resume"]) == _FIXTURE["resume"]
        from looper.agents import cost_hold_routing as routing
        routes = {}
        for text in _FIXTURE["routes"]:
            result = await routing.route(_envelope(text), platform=None)
            routes[text] = None if result is None else {"reply": getattr(result, "reply", None)}
        assert routes == _FIXTURE["routes"]
        assert await pool.fetchval("SELECT count(*) FROM cost_holds WHERE reason='unanswered'") == 0
        assert cost_guard.current().holds_need_recovery is False

    run_db(body, monkeypatch, timeout=60)
    from looper.agents.ops.schedules import TOOLS as SCHEDULE_TOOLS
    from looper.agents.ops.service import MANAGER_TOOLS
    assert [tool.name for tool in MANAGER_TOOLS] == _FIXTURE["manager_tool_names"]
    assert {tool.name: tool.listing()["inputSchema"] for tool in SCHEDULE_TOOLS} == _FIXTURE["schedule_tool_schemas"]
    assert _FIXTURE["sha"] == "3c718301c47d3e246ffb3342d7c2bb0f47d6daed"


def test_fifteen_unanswered_direct_pushes_ask_once(monkeypatch):
    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('p-ask','Ask')")
        sent = _MOMENT - timedelta(hours=2)
        recent = _MOMENT - timedelta(minutes=10)
        await _schedule(pool, "sched-15")
        await _schedule(pool, "sched-14", "Fourteen")
        await _schedule(pool, "sched-replied", "Replied")
        await _schedule(pool, "sched-group", "Group")
        await _schedule(pool, "sched-recent", "Recent")
        await _pushes(pool, "sched-15", 15, sent)
        await _pushes(pool, "sched-14", 14, sent)
        await _pushes(pool, "sched-replied", 15, sent, replied=1)
        await _pushes(pool, "sched-group", 15, sent, kind="group")
        await _pushes(pool, "sched-recent", 15, recent)
        await _enable()
        sent_messages = _capture(monkeypatch)
        await unanswered_pushes.check(_MOMENT)
        await unanswered_pushes.check(_MOMENT)
        holds = await store.list_pending(pool=pool)
        assert [row["scope_ref"] for row in holds] == ["sched-15"]
        assert holds[0]["reason"] == "unanswered" and holds[0]["pushes"] == 15
        assert holds[0]["currency"] is None and holds[0]["limit_usd"] is None and holds[0]["spent_usd"] is None
        assert len(sent_messages) == 1
        label = f"C-{holds[0]['id']}"
        assert sent_messages[0]["text"] == (
            f"{label}：『Morning brief』3 天推了 15 条，你都没回。"
            f"还要继续推吗？回复「继续 {label}」（30 天内不再问）或「停 {label}」（暂停这个定时任务）。"
            "3 天内不回答、也没回过它的推送，会自动暂停。控制台：https://console.example/#todo")
        assert sent_messages[0]["idempotency_key"] == f"cost-hold:{holds[0]['id']}"
        # Keep the recent batch inside the later check's last 30 minutes, or it becomes eligible.
        await pool.execute(
            "UPDATE proactive_deliveries SET sent_at=$1 WHERE source_ref LIKE 'sched-recent-%'",
            _MOMENT + timedelta(minutes=50))
        await unanswered_pushes.check(_MOMENT + timedelta(hours=1))
        assert len(sent_messages) == 1
        assert len(await store.list_pending(pool=pool)) == 1

    run_db(body, monkeypatch, timeout=60)


def test_pause_expires_on_a_reply_and_resume_snoozes(monkeypatch):
    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('p-ask','Ask')")
        sent_at = _MOMENT - timedelta(hours=2)
        await _schedule(pool, "sched-pause")
        await _schedule(pool, "sched-answered", "Answered")
        await _pushes(pool, "sched-pause", 15, sent_at)
        await _pushes(pool, "sched-answered", 15, sent_at)
        await pool.execute("INSERT INTO projects(id,name,task_budget_usd) VALUES ('p-budget','Budget',2)")
        await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES "
                           "('t-budget','p-budget','Synthetic task','running')")
        await _enable()
        sent_messages = _capture(monkeypatch)
        await unanswered_pushes.check(_MOMENT)
        pause_id = await pool.fetchval("SELECT id FROM cost_holds WHERE scope_ref='sched-pause'")
        answered_id = await pool.fetchval("SELECT id FROM cost_holds WHERE scope_ref='sched-answered'")
        budget, _ = await store.create_hold("task", "t-budget", "provider_priced_usd", 2, 1, "budget",
                                            task_id="t-budget", pool=pool)
        aged = _MOMENT - timedelta(hours=25)
        await pool.execute("UPDATE cost_holds SET created_at=$2 WHERE id=ANY($1::bigint[])",
                           [pause_id, answered_id, budget["id"]], aged)
        expired = await cost_holds.expire(pool=pool)
        assert [row["id"] for row in expired] == [budget["id"]]
        assert await pool.fetchval("SELECT state FROM cost_holds WHERE id=$1", pause_id) == "pending"
        await pool.execute(
            "UPDATE proactive_deliveries SET replied_at=$1, reply_match='next_message' "
            "WHERE source_ref='sched-answered-0'", aged)
        await pool.execute("UPDATE cost_holds SET created_at=$2, asked_at=$2 WHERE id=ANY($1::bigint[])",
                           [pause_id, answered_id], _MOMENT - timedelta(days=3, minutes=1))
        await unanswered_pushes.check(_MOMENT + timedelta(hours=2))
        assert await pool.fetchval("SELECT state FROM cost_holds WHERE id=$1", answered_id) == "expired"
        assert await pool.fetchval("SELECT enabled FROM schedules WHERE id='sched-answered'") is True
        assert await pool.fetchval("SELECT pause_reason FROM schedules WHERE id='sched-answered'") is None
        pause = await pool.fetchrow("SELECT state, decided_via FROM cost_holds WHERE id=$1", pause_id)
        assert pause["state"] == "stopped" and pause["decided_via"] is None
        row = await pool.fetchrow(
            "SELECT enabled, pause_reason, paused_at, next_fire_at FROM schedules WHERE id='sched-pause'")
        assert row["enabled"] is False and row["pause_reason"] == "unanswered"
        assert row["paused_at"] == _MOMENT + timedelta(hours=2) and row["next_fire_at"] is None
        assert len(sent_messages) == 3
        assert sent_messages[-1]["text"] == (
            "『Morning brief』已自动暂停：3 天没有回答，也没回过它的推送。"
            "在控制台的「需要你处理」里点「恢复」，或让管家恢复它。")
        assert sent_messages[-1]["idempotency_key"] == f"unanswered-pause:{pause_id}"
        summary = await attention.collect()
        schedules = _section(summary, "schedules")
        paused = next(item for item in schedules["items"] if item["key"] == "schedule:sched-pause")
        assert paused["facts"]["alerts"] == ["unanswered"]
        assert all(item["key"] != "schedule:sched-answered" for item in schedules["items"])
        async with pool.acquire() as conn:
            await db_schedules.update_schedule(conn, "sched-pause", {"enabled": True}, snooze_days=30)
        resumed = await pool.fetchrow(
            "SELECT enabled, pause_reason, paused_at, unanswered_snooze_until FROM schedules WHERE id='sched-pause'")
        assert resumed["enabled"] is True and resumed["pause_reason"] is None and resumed["paused_at"] is None
        until = resumed["unanswered_snooze_until"]
        assert until > datetime.now(timezone.utc) + timedelta(days=29)
        # The new hold's created_at is the database clock. Stay near that clock so the new question is not
        # already older than pause_after_days.
        wall = datetime.now(timezone.utc)
        await pool.execute("UPDATE schedules SET unanswered_snooze_until=$1 WHERE id='sched-pause'",
                           wall + timedelta(hours=2))
        await pool.execute("UPDATE proactive_deliveries SET sent_at=$1 WHERE source_ref LIKE 'sched-pause-%'",
                           wall - timedelta(hours=2))
        await unanswered_pushes.check(wall)
        assert await pool.fetchval("SELECT count(*) FROM cost_holds WHERE scope_ref='sched-pause' "
                                   "AND state='pending'") == 0
        await pool.execute("UPDATE schedules SET unanswered_snooze_until=$1 WHERE id='sched-pause'",
                           wall - timedelta(minutes=1))
        await unanswered_pushes.check(wall + timedelta(hours=1))
        assert await pool.fetchval("SELECT count(*) FROM cost_holds WHERE scope_ref='sched-pause' "
                                   "AND state='pending'") == 1

    run_db(body, monkeypatch, timeout=90)


def test_operator_replies_and_metrics_off_leave_budget_holds_alone(monkeypatch):
    async def body(pool):
        from looper.agents import chat_settings, cost_hold_routing as routing

        await pool.execute("INSERT INTO projects(id,name) VALUES ('p-ask','Ask')")
        await _schedule(pool, "sched-go", "Keep going")
        await _schedule(pool, "sched-stop", "Stop me")
        sent_at = _MOMENT - timedelta(hours=2)
        await _pushes(pool, "sched-go", 15, sent_at)
        await _pushes(pool, "sched-stop", 15, sent_at)
        await _enable()
        _capture(monkeypatch)
        monkeypatch.setattr(routing.operator_identity, "operator_sender_id", AsyncMock(return_value="operator"))
        await unanswered_pushes.check(_MOMENT)
        go = await pool.fetchval("SELECT id FROM cost_holds WHERE scope_ref='sched-go'")
        stop = await pool.fetchval("SELECT id FROM cost_holds WHERE scope_ref='sched-stop'")
        budget, _ = await store.create_hold("global_day", "global", "provider_priced_usd", 2, 2, "budget", pool=pool)
        refused = await routing.route(_envelope(f"继续 C-{go} 5"), platform=SimpleNamespace())
        assert "不需要上限" in await refused.action()
        assert await pool.fetchval("SELECT state FROM cost_holds WHERE id=$1", go) == "pending"
        bare_budget = await routing.route(_envelope(f"继续 C-{budget['id']}"), platform=SimpleNamespace())
        assert bare_budget is None
        continued = await routing.route(_envelope(f"继续 C-{go}"), platform=SimpleNamespace())
        assert "继续" in await continued.action()
        assert await pool.fetchval("SELECT state FROM cost_holds WHERE id=$1", go) == "continued"
        assert await pool.fetchval("SELECT unanswered_snooze_until FROM schedules WHERE id='sched-go'") is not None
        stopped = await routing.route(_envelope(f"停 C-{stop}"), platform=SimpleNamespace())
        assert "停下" in await stopped.action()
        stopped_row = await pool.fetchrow(
            "SELECT enabled, pause_reason, next_fire_at FROM schedules WHERE id='sched-stop'")
        assert stopped_row["enabled"] is False and stopped_row["pause_reason"] == "operator"
        assert stopped_row["next_fire_at"] is None
        failing = await db_schedules.failing()
        assert all(row["id"] != "sched-stop" for row in failing)
        chat_settings.set_metrics_enabled(False)
        unanswered_pushes._last_checked = None
        quiet, _ = await store.create_hold("schedule", "sched-go", None, None, None, "unanswered", pushes=15,
                                           pool=pool)
        await pool.execute("UPDATE cost_holds SET created_at=$2 WHERE id=$1",
                           quiet["id"], _MOMENT - timedelta(days=4))
        before = await pool.fetchval("SELECT count(*) FROM cost_holds WHERE state='pending'")
        await unanswered_pushes.check(_MOMENT + timedelta(days=4))
        assert await pool.fetchval("SELECT count(*) FROM cost_holds WHERE state='pending'") == before
        assert await pool.fetchval("SELECT enabled FROM schedules WHERE id='sched-go'") is True

    run_db(body, monkeypatch, timeout=60)


def test_decide_and_pause_claim_the_pending_hold_exactly_once(monkeypatch):
    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('p-ask','Ask')")
        await _schedule(pool, "sched-race")
        await _enable()
        row, _ = await store.create_hold("schedule", "sched-race", None, None, None, "unanswered", pushes=15,
                                         pool=pool)
        await pool.execute("UPDATE cost_holds SET created_at=$2, asked_at=$2 WHERE id=$1",
                           row["id"], _MOMENT - timedelta(days=4))

        async def wait_lock(holder_pid):
            # A row-lock waiter shows up as an ungranted transactionid lock, not an ungranted relation lock.
            for _ in range(100):
                waiting = await pool.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks me JOIN pg_locks other "
                    "ON other.locktype = 'transactionid' AND other.transactionid = me.transactionid "
                    "AND NOT other.granted WHERE me.pid = $1 AND me.locktype = 'transactionid' "
                    "AND me.granted AND me.mode = 'ExclusiveLock')", holder_pid)
                if waiting:
                    return
                await asyncio.sleep(0.05)
            raise AssertionError("expected an ungranted lock on cost_holds")

        async with pool.acquire() as blocker:
            holder_pid = await blocker.fetchval("SELECT pg_backend_pid()")
            async with blocker.transaction():
                await blocker.fetchrow("SELECT * FROM cost_holds WHERE id=$1 FOR UPDATE", row["id"])
                paused = asyncio.create_task(unanswered_pushes.check(_MOMENT))
                await wait_lock(holder_pid)
                await blocker.execute("UPDATE cost_holds SET state='continued', decided_via='api', decided_at=now(), "
                                      "applied_at=now() WHERE id=$1", row["id"])
            await paused
        assert await pool.fetchval("SELECT state FROM cost_holds WHERE id=$1", row["id"]) == "continued"
        assert await pool.fetchval("SELECT enabled FROM schedules WHERE id='sched-race'") is True

        await pool.execute("UPDATE cost_holds SET state='pending', decided_via=NULL, decided_at=NULL, applied_at=NULL "
                           "WHERE id=$1", row["id"])

        async def lose():
            with pytest.raises(ValueError, match="已处理"):
                await cost_holds.decide(row["id"], "stop", via="api", pool=pool)

        async with pool.acquire() as blocker:
            holder_pid = await blocker.fetchval("SELECT pg_backend_pid()")
            async with blocker.transaction():
                await blocker.fetchrow("SELECT * FROM cost_holds WHERE id=$1 FOR UPDATE", row["id"])
                decision = asyncio.create_task(lose())
                await wait_lock(holder_pid)
                await blocker.execute("UPDATE cost_holds SET state='stopped', decided_at=$2, applied_at=$2 "
                                      "WHERE id=$1 AND state='pending'", row["id"], _MOMENT)
                await blocker.execute(
                    "UPDATE schedules SET enabled=false, pause_reason='unanswered', paused_at=$2, next_fire_at=NULL "
                    "WHERE id=$1", "sched-race", _MOMENT)
            await decision
        assert await pool.fetchval("SELECT state FROM cost_holds WHERE id=$1", row["id"]) == "stopped"
        assert await pool.fetchval("SELECT pause_reason FROM schedules WHERE id='sched-race'") == "unanswered"

    run_db(body, monkeypatch, timeout=60, max_size=6)


def test_a_reply_after_the_question_expires_a_push_sent_earlier(monkeypatch):
    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('p-ask','Ask')")
        await _schedule(pool, "sched-early")
        asked = _MOMENT - timedelta(days=4)
        await _pushes(pool, "sched-early", 1, asked - timedelta(minutes=10))
        row, _ = await store.create_hold("schedule", "sched-early", None, None, None, "unanswered", pushes=1,
                                         pool=pool)
        await pool.execute("UPDATE cost_holds SET created_at=$2, asked_at=$2 WHERE id=$1", row["id"], asked)
        await pool.execute(
            "UPDATE proactive_deliveries SET replied_at=$2, reply_match='next_message' WHERE source_ref=$1",
            "sched-early-0", asked + timedelta(minutes=1))
        await _enable()
        _capture(monkeypatch)
        await unanswered_pushes.check(_MOMENT)
        assert await pool.fetchval("SELECT state FROM cost_holds WHERE id=$1", row["id"]) == "expired"
        assert await pool.fetchval("SELECT enabled FROM schedules WHERE id='sched-early'") is True
        assert await pool.fetchval("SELECT pause_reason FROM schedules WHERE id='sched-early'") is None

    run_db(body, monkeypatch, timeout=60)


def test_a_question_that_times_out_is_resent_after_restart_and_does_not_pause(monkeypatch):
    root = Path(__file__).parents[1]
    serve = (root / "looper/agents/serve.py").read_text()
    holds = (root / "looper/agents/cost_holds.py").read_text()
    assert serve.index("await cost_guard.initialize(") < serve.index(
        "await cost_holds.deliver_unsent_questions(")
    initialize = holds.split("async def initialize", 1)[1].split("\nasync def ", 1)[0]
    assert "resume_unanswered_questions" not in initialize

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('p-ask','Ask')")
        await _schedule(pool, "sched-timeout")
        await _pushes(pool, "sched-timeout", 15, _MOMENT - timedelta(hours=2))
        await _enable()
        calls = {"n": 0}

        async def sender(channel_id, chat, text, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("injected")
            return True, None

        cost_holds.configure(sender=sender, dashboard_url="https://console.example")
        monkeypatch.setattr("looper.agents.gate_service.dashboard_target", AsyncMock(return_value=SimpleNamespace(
            channel_id="web", target=SimpleNamespace(id="direct-chat", group=False))))
        await unanswered_pushes.check(_MOMENT)
        hold_id = await pool.fetchval("SELECT id FROM cost_holds WHERE scope_ref='sched-timeout'")
        assert calls["n"] == 1
        assert await pool.fetchval("SELECT asked_at FROM cost_holds WHERE id=$1", hold_id) is None
        await pool.execute("UPDATE cost_holds SET created_at=$2 WHERE id=$1",
                           hold_id, _MOMENT - timedelta(days=4))
        await unanswered_pushes.check(_MOMENT + timedelta(days=4))
        # The later pass retries the undelivered question. It still timed out, so the schedule is not paused.
        assert calls["n"] == 2
        assert await pool.fetchval("SELECT asked_at FROM cost_holds WHERE id=$1", hold_id) is None
        assert await pool.fetchval("SELECT enabled FROM schedules WHERE id='sched-timeout'") is True
        # Same order as serve: cost guard starts while the sender is still unset, then the sender is configured.
        cost_holds.configure(sender=None, dashboard_url="")
        await cost_guard.initialize(day_start=lambda now: now, pool=pool)
        assert calls["n"] == 2
        assert await pool.fetchval("SELECT asked_at FROM cost_holds WHERE id=$1", hold_id) is None
        await cost_holds.deliver_unsent_questions(
            sender=sender, dashboard_url="https://console.example", pool=pool)
        assert calls["n"] == 3
        assert await pool.fetchval("SELECT asked_at FROM cost_holds WHERE id=$1", hold_id) is not None
        assert await pool.fetchval("SELECT enabled FROM schedules WHERE id='sched-timeout'") is True

    run_db(body, monkeypatch, timeout=60)


def _startup_marks(source: str) -> list[str]:
    marks = []
    for line in source.splitlines():
        text = line.strip()
        if text.startswith("await registry.register(web_adapter)"):
            marks.append("web")
        elif text.startswith("await _register_bots("):
            marks.append("bots")
        elif text.startswith("await cost_holds.deliver_unsent_questions("):
            marks.append("deliver")
        elif text.startswith("await scheduler.start("):
            marks.append("check")
    return marks


def test_an_unsent_question_is_delivered_after_every_channel_adapter_registers(monkeypatch, tmp_path):
    """Replay serve's startup marks. Deliver runs only after both bot registrations, through the real registry."""
    from looper.agents.bus import MessageBus
    from looper.agents.channels.base import ChannelAdapter, DeliveryReceipt, PostReceipt
    from looper.agents.channels.chat_routing import RoutingSnapshot
    from looper.agents.channels.registry import ChannelRegistry
    from looper.agents.message import MessageEnvelope
    from looper.agents.task_notice import NotifyTarget

    serve = (Path(__file__).parents[1] / "looper/agents/serve.py").read_text()
    marks = _startup_marks(serve)
    assert marks.count("bots") == 2 and marks.count("deliver") == 1
    assert marks.index("deliver") > max(index for index, mark in enumerate(marks) if mark == "bots")
    assert marks.index("check") > marks.index("deliver") > marks.index("web")

    class _Stub(ChannelAdapter):
        def __init__(self, channel_id: str, channel_type: str) -> None:
            self._id, self._type, self.calls = channel_id, channel_type, []

        @property
        def channel_type(self) -> str:
            return self._type

        @property
        def channel_id(self) -> str:
            return self._id

        async def start(self, bus) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def deliver(self, envelope: MessageEnvelope) -> DeliveryReceipt:
            return DeliveryReceipt(channel=self._type, success=True)

        async def notify(self, chat: str, text: str, *, idempotency_key: str | None = None) -> DeliveryReceipt:
            self.calls.append(("notify", chat, text, idempotency_key))
            return DeliveryReceipt(channel=self._type, success=True)

        async def post(self, target: NotifyTarget, text: str, *, reply_to: str | None = None,
                       idempotency_key: str | None = None) -> PostReceipt:
            return PostReceipt(True, external_ids=["m1"])

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('p-ask','Ask')")
        await _schedule(pool, "sched-adapters")
        await store.create_hold("schedule", "sched-adapters", None, None, None, "unanswered", pushes=15, pool=pool)
        await _enable()
        monkeypatch.setattr("looper.agents.channels.registry.load_snapshot", AsyncMock(
            return_value=RoutingSnapshot("channel-telegram", "manager", False, {})))
        monkeypatch.setattr("looper.agents.gate_service.dashboard_target", AsyncMock(return_value=SimpleNamespace(
            channel_id="channel-telegram", target=SimpleNamespace(id="1000001", group=False))))
        bus = MessageBus(runtime_root=tmp_path)
        registry = ChannelRegistry(bus)
        web = _Stub("channel-web", "web")
        telegram = _Stub("channel-telegram", "telegram")
        bots_seen = 0
        for mark in marks:
            if mark == "web":
                await registry.register(web)
                missing, error = await registry.notify("channel-telegram", "1000001", "too early")
                assert (missing, error) == (
                    False, "the telegram channel is not running: enable it in Settings → Channels")
            elif mark == "bots":
                bots_seen += 1
                if bots_seen == 1:
                    await registry.register(telegram)
            elif mark == "deliver":
                await cost_holds.deliver_unsent_questions(
                    sender=registry.notify, dashboard_url="https://console.example", pool=pool)
            elif mark == "check":
                unanswered_pushes._last_checked = None
                await unanswered_pushes.check(_MOMENT + timedelta(hours=2))
        assert [call[0] for call in telegram.calls] == ["notify"]
        assert await pool.fetchval(
            "SELECT asked_at FROM cost_holds WHERE scope_ref='sched-adapters'") is not None
        assert await pool.fetchval("SELECT state FROM cost_holds WHERE scope_ref='sched-adapters'") == "pending"
        assert await pool.fetchval("SELECT enabled FROM schedules WHERE id='sched-adapters'") is True

    run_db(body, monkeypatch, timeout=60)


def test_a_digested_pause_notice_is_not_sent(monkeypatch):
    async def body(pool):
        from looper.agents import chat_settings, proactive_delivery as delivery

        await pool.execute("INSERT INTO projects(id,name) VALUES ('p-ask','Ask')")
        await _schedule(pool, "sched-quiet")
        await _pushes(pool, "sched-quiet", 15, _MOMENT - timedelta(hours=2))
        await _enable()
        sent = _capture(monkeypatch)
        monkeypatch.setattr(chat_settings, "delivery_enabled", lambda: True)

        async def decide(message, **kwargs):
            if str(kwargs.get("dedupe_key", "")).startswith("unanswered-pause:"):
                return delivery.Decision(delivery.Outcome.DIGEST, delivery.Reason.QUIET_HOURS,
                                         digest_date=_MOMENT.date())
            return delivery.Decision(delivery.Outcome.SEND, delivery.Reason.URGENT)

        monkeypatch.setattr(delivery, "decide", decide)
        await unanswered_pushes.check(_MOMENT)
        hold_id = await pool.fetchval("SELECT id FROM cost_holds WHERE scope_ref='sched-quiet'")
        assert len(sent) == 1
        assert await pool.fetchval("SELECT asked_at FROM cost_holds WHERE id=$1", hold_id) is not None
        await pool.execute("UPDATE cost_holds SET created_at=$2, asked_at=$2 WHERE id=$1",
                           hold_id, _MOMENT - timedelta(days=4))
        await unanswered_pushes.check(_MOMENT + timedelta(days=4))
        assert len(sent) == 1
        assert await pool.fetchval("SELECT enabled FROM schedules WHERE id='sched-quiet'") is False
        assert await pool.fetchval("SELECT pause_reason FROM schedules WHERE id='sched-quiet'") == "unanswered"

    run_db(body, monkeypatch, timeout=60)


def test_a_reply_committed_before_the_pause_update_expires_the_hold(monkeypatch):
    async def body(pool):
        from looper.db import unanswered_pushes as pauses

        await pool.execute("INSERT INTO projects(id,name) VALUES ('p-ask','Ask')")
        await _schedule(pool, "sched-race-reply")
        asked = _MOMENT - timedelta(days=4)
        await _pushes(pool, "sched-race-reply", 1, asked)
        row, _ = await store.create_hold("schedule", "sched-race-reply", None, None, None, "unanswered", pushes=1,
                                         pool=pool)
        await pool.execute("UPDATE cost_holds SET created_at=$2, asked_at=$2 WHERE id=$1", row["id"], asked)

        async def before_write():
            async with pool.acquire() as other:
                await other.execute(
                    "UPDATE proactive_deliveries SET replied_at=$2, reply_match='next_message' WHERE source_ref=$1",
                    "sched-race-reply-0", asked + timedelta(minutes=1))

        async with pool.acquire() as conn, conn.transaction():
            outcome = await pauses.claim_pause(conn, row["id"], _MOMENT, before_write=before_write)
        assert outcome["paused"] is False and outcome["expired"] is True
        assert await pool.fetchval("SELECT state FROM cost_holds WHERE id=$1", row["id"]) == "expired"
        assert await pool.fetchval("SELECT enabled FROM schedules WHERE id='sched-race-reply'") is True

    run_db(body, monkeypatch, timeout=60, max_size=4)


def test_a_rewritten_string_literal_stays_pending_and_is_restored(monkeypatch):
    assert printed_check("CHECK ((pause_reason = 'unanswered'))") != printed_check(
        "CHECK ((pause_reason = 'UNANSWERED'))")
    assert printed_check("CHECK ((a = 1) AND (b = 2)) NOT VALID") == printed_check("check (( a = 1 ) and ( b = 2 ))")
    assert printed_check("CHECK (('a  b' = 'a  b'))") == "check(('a  b'='a  b'))"

    async def body(pool):
        await pool.execute("INSERT INTO projects(id,name) VALUES ('p-ask','Ask')")
        await _schedule(pool, "sched-literal")
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO schema_migrations (name) VALUES ('schedule_pause_reasons') ON CONFLICT DO NOTHING")
            await conn.execute("ALTER TABLE schedules DROP CONSTRAINT schedules_pause_reason_check")
            await conn.execute(
                "ALTER TABLE schedules ADD CONSTRAINT schedules_pause_reason_check CHECK "
                "(pause_reason IN ('operator', 'UNANSWERED', 'budget', 'quota')) NOT VALID")
            assert await schedule_pause_reasons.pending(conn)
        with pytest.raises(Exception, match="schedules_pause_reason_check"):
            await pool.execute(
                "UPDATE schedules SET enabled=false, pause_reason='unanswered', paused_at=now(), next_fire_at=NULL "
                "WHERE id='sched-literal'")
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await schedule_pause_reasons.pending(conn)
        await pool.execute(
            "UPDATE schedules SET enabled=false, pause_reason='unanswered', paused_at=now(), next_fire_at=NULL "
            "WHERE id='sched-literal'")
        assert await pool.fetchval("SELECT pause_reason FROM schedules WHERE id='sched-literal'") == "unanswered"
        with pytest.raises(Exception, match="schedules_pause_reason_check"):
            await pool.execute(
                "UPDATE schedules SET pause_reason='UNANSWERED' WHERE id='sched-literal'")

    run_db(body, monkeypatch, timeout=120)


def test_migration_pending_sees_a_dropped_check_while_the_marker_remains(monkeypatch):
    async def body(pool):
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO schema_migrations (name) VALUES ('schedule_pause_reasons'), ('cost_holds_unanswered') "
                "ON CONFLICT DO NOTHING")
            await conn.execute("ALTER TABLE schedules DROP CONSTRAINT schedules_pause_reason_check")
            await conn.execute("ALTER TABLE cost_holds DROP CONSTRAINT cost_holds_reason_shape")
            assert await schedule_pause_reasons.pending(conn)
            assert await cost_holds_unanswered.pending(conn)
            await schedule_pause_reasons.apply(conn, sql=None, state=None)
            await cost_holds_unanswered.apply(conn, sql=None, state=None)
            assert not await schedule_pause_reasons.pending(conn)
            assert not await cost_holds_unanswered.pending(conn)
            names = {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")}
            assert "schedule_pause_reasons" in names and "cost_holds_unanswered" in names
            assert await conn.fetchval(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='schedules_pause_reason_check'")
            assert await conn.fetchval(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='cost_holds_reason_shape'")

    run_db(body, monkeypatch, timeout=60)


def test_pending_matches_the_whole_check_and_restores_a_dropped_scope(monkeypatch):
    async def body(pool):
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO schema_migrations (name) VALUES ('schedule_pause_reasons'), ('cost_holds_unanswered') "
                "ON CONFLICT DO NOTHING")
            assert not await schedule_pause_reasons.pending(conn)
            assert not await cost_holds_unanswered.pending(conn)
            await conn.execute("ALTER TABLE cost_holds DROP CONSTRAINT cost_holds_scope_check")
            assert await cost_holds_unanswered.pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await cost_holds_unanswered.pending(conn)
        with pytest.raises(Exception, match="cost_holds_scope_check"):
            await pool.execute(
                "INSERT INTO cost_holds(scope,scope_ref,currency,limit_usd,spent_usd,reason) "
                "VALUES ('nope','x','provider_priced_usd',1,0,'budget')")

        await pool.execute("ALTER TABLE schedules DROP CONSTRAINT schedules_pause_pair_check")
        await pool.execute(
            "ALTER TABLE schedules ADD CONSTRAINT schedules_pause_pair_check "
            "CHECK ((pause_reason IS NULL) OR (paused_at IS NULL)) NOT VALID")
        async with pool.acquire() as conn:
            assert await schedule_pause_reasons.pending(conn)
            await schedule_pause_reasons.apply(conn, sql=None, state=None)
            assert not await schedule_pause_reasons.pending(conn)
        pair = await pool.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='schedules_pause_pair_check'")
        assert printed_check(pair) == printed_check(schedule_pause_reasons._PAIR_DEF)

        await pool.execute("ALTER TABLE cost_holds DROP CONSTRAINT cost_holds_reason_shape")
        await pool.execute(
            "ALTER TABLE cost_holds ADD CONSTRAINT cost_holds_reason_shape CHECK ("
            "(reason = 'unanswered') = (pushes IS NOT NULL) OR "
            "(reason = 'unanswered') = (limit_usd IS NULL) OR "
            "(limit_usd IS NULL) = (currency IS NULL) OR "
            "(limit_usd IS NULL) = (spent_usd IS NULL)) NOT VALID")
        async with pool.acquire() as conn:
            assert await cost_holds_unanswered.pending(conn)
            await cost_holds_unanswered.apply(conn, sql=None, state=None)
            assert not await cost_holds_unanswered.pending(conn)
        shape = await pool.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='cost_holds_reason_shape'")
        assert printed_check(shape) == printed_check(cost_holds_unanswered._SHAPE_DEF)

    run_db(body, monkeypatch, timeout=120)


def test_fresh_and_upgraded_holds_keep_budget_rows_valid(monkeypatch):
    async def body(pool):
        async with pool.acquire() as conn:
            assert not await schedule_pause_reasons.pending(conn)
            assert not await cost_holds_unanswered.pending(conn)
            names = {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")}
        assert "schedule_pause_reasons" not in names and "cost_holds_unanswered" not in names
        for constraint in ("schedules_pause_reason_check", "schedules_pause_pair_check",
                           "cost_holds_scope_check", "cost_holds_reason_shape"):
            assert await pool.fetchval("SELECT convalidated FROM pg_constraint WHERE conname=$1", constraint)
        await pool.execute("INSERT INTO projects(id,name) VALUES ('p-ask','Ask')")
        await _schedule(pool, "sched-old", "Old")
        await pool.execute(
            "INSERT INTO cost_holds(scope,scope_ref,currency,limit_usd,spent_usd,reason) "
            "VALUES ('task','task-old','provider_priced_usd',2,1,'budget')")
        definition = await pool.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='cost_holds_scope_check'")
        for value in ("task", "schedule_day", "role_day", "global_day", "schedule"):
            assert f"'{value}'" in definition
        await pool.execute("ALTER TABLE cost_holds DROP CONSTRAINT cost_holds_reason_shape")
        await pool.execute("ALTER TABLE cost_holds DROP COLUMN pushes")
        await pool.execute("ALTER TABLE cost_holds ALTER COLUMN currency SET NOT NULL, "
                           "ALTER COLUMN limit_usd SET NOT NULL, ALTER COLUMN spent_usd SET NOT NULL")
        await pool.execute("ALTER TABLE cost_holds DROP CONSTRAINT cost_holds_scope_check")
        await pool.execute("ALTER TABLE cost_holds ADD CONSTRAINT cost_holds_scope_check "
                           "CHECK (scope IN ('task', 'schedule_day', 'role_day', 'global_day')) NOT VALID")
        await pool.execute("ALTER TABLE schedules DROP CONSTRAINT schedules_pause_reason_check")
        await pool.execute("ALTER TABLE schedules DROP CONSTRAINT schedules_pause_pair_check")
        await pool.execute("ALTER TABLE schedules DROP COLUMN pause_reason, DROP COLUMN paused_at, "
                           "DROP COLUMN unanswered_snooze_until")
        async with pool.acquire() as conn:
            assert await schedule_pause_reasons.pending(conn)
            assert await cost_holds_unanswered.pending(conn)
            await schedule_pause_reasons.apply(conn, sql=None, state=None)
            await cost_holds_unanswered.apply(conn, sql=None, state=None)
            assert not await schedule_pause_reasons.pending(conn)
            assert not await cost_holds_unanswered.pending(conn)
        await pool.execute("ALTER TABLE cost_holds VALIDATE CONSTRAINT cost_holds_scope_check")
        await pool.execute("ALTER TABLE cost_holds VALIDATE CONSTRAINT cost_holds_reason_shape")
        await pool.execute("ALTER TABLE schedules VALIDATE CONSTRAINT schedules_pause_reason_check")
        await pool.execute("ALTER TABLE schedules VALIDATE CONSTRAINT schedules_pause_pair_check")
        assert await pool.fetchval("SELECT reason FROM cost_holds") == "budget"
        await pool.execute(
            "INSERT INTO cost_holds(scope,scope_ref,reason,pushes) VALUES ('schedule','sched-old','unanswered',15)")
        with pytest.raises(Exception, match="cost_holds_reason_shape"):
            await pool.execute(
                "INSERT INTO cost_holds(scope,scope_ref,currency,limit_usd,spent_usd,reason,pushes) "
                "VALUES ('schedule','sched-bad','provider_priced_usd',1,1,'unanswered',15)")

    run_db(body, monkeypatch, timeout=60)
