"""Completion events: a task's end is recorded where its code notice is made, and nothing sent changes."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from looper.agents import completion_events
from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
from looper.agents.task_notice import (Notice, NotifyChannel, NotifyTarget, TargetKind, split_message,
                                       web_end_text)
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import connection, manager_events as store, projects, schedules as db_schedules, tasks
from tests.db_support import DSN, run_db, v1_api
from tests.schedule_support import TELEGRAM_CHAT, ScheduleEnv, allow_telegram, run_scheduled, with_sender

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OPEN_ID = "ou_abcdefgh12"
THREAD = "chat-session-1"
LONG = "✅ 任务完成 · calc\n" + "\n".join(f"- finding {n}: " + "detail " * 20 for n in range(80))


def reference_web_text(status, title, summary, delivery_summary):
    """The text ``ManagerAgent._handle_goal_status`` made inline before it moved to ``web_end_text``."""
    if delivery_summary:
        summary = summary.removesuffix("\n\n" + delivery_summary)
    if len(summary) > 2000:
        tail = summary[-600:]
        marker = f'\n… ({len(summary)} characters omitted; full result on the task page)\n'
        head_size = 2000 - len(tail) - len(marker)
        omitted = len(summary) - head_size - len(tail)
        marker = f'\n… ({omitted} characters omitted; full result on the task page)\n'
        summary = summary[:head_size] + marker + tail
    if delivery_summary:
        summary += "\n\n" + delivery_summary
    if status == "completed":
        return f"任务「{title}」已完成：{summary}"
    if status == "failed":
        return f"任务「{title}」失败：{summary}"
    return f"任务「{title}」已取消。"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("summary,delivery", [
    ("short result", ""), ("", ""), ("x" * 5000, ""), ("body\n\nDelivered: a, b", "Delivered: a, b"),
    ("y" * 3000 + "\n\nDelivered: z", "Delivered: z"), ("z" * 1990, "tail"),
])
def test_the_web_end_text_is_what_the_manager_always_made(status, summary, delivery):
    assert web_end_text(status, "calc", summary, delivery) == reference_web_text(status, "calc", summary, delivery)


async def manager_with_task(pool, tmp_path: Path, **task: Any):
    project = await projects.create_project(name="calc", work_dir=str(tmp_path), pool=pool)
    await tasks.create_task(task_id="t1", requirement="r", project_id=project["id"], work_dir=str(tmp_path),
                            status="completed", pool=pool, **task)
    bus = MessageBus(runtime_root=tmp_path)
    await bus.start()
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    manager._bus = bus
    return manager, bus


def goal_status(status="completed", *, notice: Notice | None = None, thread: str | None = None, **payload):
    return MessageEnvelope.create(
        source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.GOAL_STATUS,
        payload={"task_id": "t1", "goal_id": "g1", "status": status, "summary": "done", **payload,
                 **({"notice": notice.payload()} if notice else {})},
        correlation_id="c1", metadata={"task_title": "calc", **({"origin_thread": thread} if thread else {})})


def sent(bus: MessageBus, agent_id: str) -> list[MessageEnvelope]:
    queue = bus.get_queue(agent_id)
    return [queue.get_nowait() for _ in range(queue.qsize())]


def notice_of(kind: str, text: str) -> Notice:
    match kind:
        case "dm":
            return Notice(NotifyChannel.LARK, NotifyTarget(TargetKind.USER, OPEN_ID), text, channel_id="channel-lark")
        case "telegram_private":
            return Notice(NotifyChannel.TELEGRAM, NotifyTarget(TargetKind.CHAT, "42"), text,
                          channel_id="channel-telegram")
        case _:
            return Notice(NotifyChannel.LARK, NotifyTarget(TargetKind.CHAT, "oc_team", mention=OPEN_ID, group=True),
                          text, channel_id="channel-lark")


async def events(pool):
    return [dict(row) for row in await pool.fetch("SELECT * FROM manager_events ORDER BY id")]


@pytest.mark.parametrize("kind,target_kind,channel_id,chat", [
    ("dm", "dm", "channel-lark", OPEN_ID), ("telegram_private", "dm", "channel-telegram", "42"),
    ("group", "group", "channel-lark", "oc_team"),
])
@pytest.mark.parametrize("text", ["✅ 任务完成 · calc\ndone\nhttp://x/t1", LONG])
def test_a_chat_tasks_end_is_recorded_and_its_notice_is_sent_unchanged(
        monkeypatch, tmp_path, kind, target_kind, channel_id, chat, text):
    async def check(pool):
        manager, bus = await manager_with_task(pool, tmp_path)
        notice = notice_of(kind, text)
        envelope = goal_status(notice=notice)
        await manager._handle_goal_status(envelope)
        (message,) = sent(bus, channel_id)
        assert message.message_type is MessageType.CHAT_NOTICE and message.payload == notice.payload()
        assert split_message(message.payload["text"]) == split_message(text)  # the channel cuts what it always cut
        (row,) = await events(pool)
        assert (row["state"], row["reason"], row["target_kind"], row["origin"], row["kind"]) == (
            "skipped", "off", target_kind, "chat", "task_end")
        assert (row["manager_id"], row["task_id"], row["channel_id"], row["chat"], row["dedupe_key"]) == (
            "manager", "t1", channel_id, chat, envelope.message_id)
        assert row["sent_at"] is not None and row["finished_at"] is not None
        assert json.loads(row["fallback"]) == [{"via": "notice", "notice": notice.payload()}]
        await bus.shutdown()

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("status,expected", [("completed", "completed"), ("failed", "failed")])
def test_a_dashboard_tasks_end_goes_back_into_its_chat_session(monkeypatch, tmp_path, status, expected):
    async def check(pool):
        manager, bus = await manager_with_task(pool, tmp_path, origin_thread=THREAD)
        envelope = goal_status(status, thread=THREAD, summary="x" * 5000, delivery_summary="Delivered: a")
        await manager._handle_goal_status(envelope)
        (reply,) = sent(bus, "channel-web")
        assert reply.message_type is MessageType.AGENT_REPLY and reply.payload["chat_session_id"] == THREAD
        assert reply.payload["text"] == reference_web_text(status, "calc", "x" * 5000, "Delivered: a")
        (row,) = await events(pool)
        assert (row["target_kind"], row["channel_id"], row["chat"], row["session_key"], row["state"],
                row["reason"]) == ("web", "channel-web", THREAD, THREAD, "skipped", "off")
        assert row["sent_at"] is not None
        await bus.shutdown()

    run_db(check, monkeypatch, timeout=30)


def test_a_replayed_end_is_recorded_once_and_not_sent_again(monkeypatch, tmp_path):
    async def check(pool):
        manager, bus = await manager_with_task(pool, tmp_path)
        envelope = goal_status(notice=notice_of("dm", "ended"))
        await manager._handle_goal_status(envelope)
        await manager._handle_goal_status(envelope)
        assert len(sent(bus, "channel-lark")) == 1 and len(await events(pool)) == 1
        # A first attempt that never handed the notice over is made up on the replay.
        await pool.execute("UPDATE manager_events SET sent_at = NULL, finished_at = NULL")
        await manager._handle_goal_status(envelope)
        assert len(sent(bus, "channel-lark")) == 1 and len(await events(pool)) == 1
        assert (await events(pool))[0]["sent_at"] is not None
        await bus.shutdown()

    run_db(check, monkeypatch, timeout=30)


def test_a_dashboard_end_replayed_is_not_told_twice(monkeypatch, tmp_path):
    async def check(pool):
        manager, bus = await manager_with_task(pool, tmp_path, origin_thread=THREAD)
        envelope = goal_status(thread=THREAD)
        await manager._handle_goal_status(envelope)
        await manager._handle_goal_status(envelope)
        assert len(sent(bus, "channel-web")) == 1 and len(await events(pool)) == 1
        await bus.shutdown()

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("web", [False, True])
def test_a_database_that_cannot_be_written_still_sends_the_notice(monkeypatch, tmp_path, web):
    async def check(pool):
        manager, bus = await manager_with_task(pool, tmp_path, **({"origin_thread": THREAD} if web else {}))

        async def broken(**_):
            raise asyncpg.PostgresConnectionError("the database went away")

        monkeypatch.setattr(store, "record", broken)
        envelope = goal_status(thread=THREAD) if web else goal_status(notice=notice_of("dm", "ended"))
        await manager._handle_goal_status(envelope)
        assert len(sent(bus, "channel-web" if web else "channel-lark")) == 1
        assert await events(pool) == []
        await bus.shutdown()

    run_db(check, monkeypatch, timeout=30)


def test_only_a_completed_or_failed_task_is_an_end(monkeypatch, tmp_path):
    async def check(pool):
        manager, bus = await manager_with_task(pool, tmp_path, origin_thread=THREAD)
        await manager._handle_goal_status(goal_status("cancelled", thread=THREAD))
        await manager._handle_goal_status(goal_status("running", thread=THREAD, phase_text="working"))
        assert len(sent(bus, "channel-web")) == 2 and await events(pool) == []
        await bus.shutdown()

    run_db(check, monkeypatch, timeout=30)


def test_a_task_that_is_not_in_the_database_is_not_recorded_and_still_notified(monkeypatch, tmp_path):
    async def check(pool):
        manager, bus = await manager_with_task(pool, tmp_path)
        envelope = goal_status(notice=notice_of("dm", "ended"))
        envelope.payload["task_id"] = "missing"
        await manager._handle_goal_status(envelope)
        assert len(sent(bus, "channel-lark")) == 1 and await events(pool) == []
        await bus.shutdown()

    run_db(check, monkeypatch, timeout=30)


# --- Schedules ---

async def schedule_run(env: ScheduleEnv, *, notify: str, status: str = "completed", target=None):
    sender = with_sender(env)
    await allow_telegram(env)
    schedule = await env.schedule(notify=notify, notify_target=target or {"channel": "telegram", "chat": TELEGRAM_CHAT})
    run_id = await env.scheduler.run_now(schedule["schedule_id"])
    run = await db_schedules.get_run(run_id)
    await env.end_task(run["task_id"], status, summary="finished the report")
    await env.scheduler.idle()
    return sender, schedule, run


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_a_schedule_runs_message_is_recorded_and_sent_unchanged(monkeypatch, tmp_path, status):
    async def test(env: ScheduleEnv):
        sender, schedule, run = await schedule_run(env, notify="always", status=status)
        saved = await db_schedules.get_run(run["id"])
        from looper.agents.schedule_notify import run_text

        expected = run_text(await db_schedules.get_schedule(schedule["schedule_id"]), "alerts", saved)
        assert [text for _, _, text in sender.sent] == [expected]
        (row,) = await events(env.pool)
        assert (row["origin"], row["schedule_run_id"], row["task_id"], row["target_kind"], row["channel_id"],
                row["chat"], row["dedupe_key"], row["state"], row["reason"], row["manager_id"]) == (
            "schedule", run["id"], run["task_id"], "dm", "channel-telegram", TELEGRAM_CHAT,
            f"schedule_run:{run['id']}", "skipped", "off", "manager")
        assert row["sent_at"] is not None
        # The run's end told again (a replay) neither sends nor records a second time.
        await env.scheduler._notifier.run_ended(run["id"])
        assert len(sender.sent) == 1 and len(await events(env.pool)) == 1

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("notify,status", [("never", "failed"), ("failures", "completed"), ("alerts", "failed")])
def test_a_run_whose_message_is_not_sent_is_not_recorded(monkeypatch, tmp_path, notify, status):
    async def test(env: ScheduleEnv):
        sender, *_ = await schedule_run(env, notify=notify, status=status)
        assert sender.sent == [] and await events(env.pool) == []

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_schedule_without_a_chat_target_records_nothing(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sender = with_sender(env)
        schedule = await env.schedule(notify="always")
        run_id = await env.scheduler.run_now(schedule["schedule_id"])
        await env.end_task((await db_schedules.get_run(run_id))["task_id"], "completed", summary="done")
        await env.scheduler.idle()
        assert sender.sent == [] and await events(env.pool) == []

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_schedules_message_is_sent_when_the_event_cannot_be_written(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        async def broken(**_):
            raise asyncpg.PostgresConnectionError("the database went away")

        monkeypatch.setattr(store, "record", broken)
        sender, *_ = await schedule_run(env, notify="always")
        assert len(sender.sent) == 1 and await events(env.pool) == []

    run_scheduled(test, monkeypatch, tmp_path)


# --- The table ---

async def insert(pool, **columns):
    values = {"manager_id": "manager", "task_id": "t1", "origin": "chat", "target_kind": "dm", "channel_id": "c",
              "chat": "u", "dedupe_key": "k", "payload": "{}", "fallback": "[]", "state": "skipped",
              "reason": "off", "kind": "task_end", **columns}
    names = list(values)
    marks = ", ".join(f"${n}" + ("::jsonb" if name in ("payload", "fallback") else "")
                      for n, name in enumerate(names, 1))
    await pool.execute(f"INSERT INTO manager_events ({', '.join(names)}) VALUES ({marks})", *values.values())


async def with_task(pool, tmp_path):
    project = await projects.create_project(name="calc", work_dir=str(tmp_path), pool=pool)
    await tasks.create_task(task_id="t1", requirement="r", project_id=project["id"], pool=pool)


@pytest.mark.parametrize("columns", [
    {"state": "skipped", "reason": None}, {"state": "pending", "reason": "off", "due_at": datetime.now(timezone.utc)},
    {"state": "pending", "reason": None}, {"state": "pending", "reason": None, "target_kind": "group",
                                            "due_at": datetime.now(timezone.utc)},
    {"state": "running", "reason": None}, {"state": "delivered", "reason": None, "target_kind": "dm"},
    {"origin": "chat", "schedule_run_id": "r1"}, {"payload": "[]"}, {"fallback": "{}"},
    {"target_kind": "channel"}, {"reason": "because"}, {"kind": "intent"},
])
def test_the_table_refuses_a_state_that_does_not_fit(monkeypatch, tmp_path, columns):
    async def check(pool):
        await with_task(pool, tmp_path)
        with pytest.raises(asyncpg.CheckViolationError):
            await insert(pool, **columns)

    run_db(check, monkeypatch, timeout=30)


def test_an_event_is_unique_by_its_dedupe_key_and_goes_with_its_task(monkeypatch, tmp_path):
    async def check(pool):
        await with_task(pool, tmp_path)
        await insert(pool)
        with pytest.raises(asyncpg.UniqueViolationError):
            await insert(pool)
        await pool.execute("DELETE FROM tasks WHERE id = 't1'")
        assert await events(pool) == []

    run_db(check, monkeypatch, timeout=30)


FRESH_INDEX = None
HAS_EVENT_COLUMN = ("SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = "
                    "'schedule_notifications' AND column_name = 'manager_event_id' "
                    "AND table_schema = current_schema())")


@pytest.mark.parametrize("before", ["no_table", "no_column"])
def test_a_database_without_the_table_or_the_outbox_column_gets_them_on_upgrade(monkeypatch, before):
    async def check(pool):
        global FRESH_INDEX
        FRESH_INDEX = await pool.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = "
                                          "'idx_schedule_notifications_manager_event' AND schemaname = current_schema()")
        assert FRESH_INDEX and "WHERE (manager_event_id IS NOT NULL)" in FRESH_INDEX
        await pool.execute("ALTER TABLE schedule_notifications DROP COLUMN manager_event_id; "
                           "DELETE FROM schema_migrations WHERE name IN ('schedule_notification_events', "
                           "'schedule_notification_events_online')")
        if before == "no_table":  # older than completion events
            await pool.execute("DROP TABLE manager_events; DELETE FROM schema_migrations WHERE name = 'manager_events'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval("SELECT to_regclass('manager_events') IS NOT NULL")
        assert await pool.fetchval(HAS_EVENT_COLUMN)
        # The outbox's new foreign key is indexed (built online), as a new database has it.
        assert await pool.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = "
                                   "'idx_schedule_notifications_manager_event' AND schemaname = current_schema()") == FRESH_INDEX

    run_db(check, monkeypatch, timeout=30)


def test_events_older_than_thirty_days_are_pruned_and_the_summary_counts_a_day(monkeypatch, tmp_path):
    async def check(pool):
        await with_task(pool, tmp_path)
        await insert(pool, dedupe_key="new")
        await insert(pool, dedupe_key="day-old", state="fallback", reason="turn_failed")
        await insert(pool, dedupe_key="month-old")
        await pool.execute("UPDATE manager_events SET created_at = now() - interval '25 hours' "
                           "WHERE dedupe_key = 'day-old'")
        await pool.execute("UPDATE manager_events SET created_at = now() - interval '31 days' "
                           "WHERE dedupe_key = 'month-old'")
        assert await store.summary() == {"window_hours": 24, "total": 1, "by_state": {"skipped": 1},
                                         "by_reason": {"off": 1}, "turn_cost_usd": 0.0, "pending": 0,
                                         "oldest_pending_seconds": None, "failing": False}
        cutoff = datetime.now(timezone.utc) - timedelta(days=store.RETENTION_DAYS)
        assert await store.prune(pool, cutoff) == 1
        assert [row["dedupe_key"] for row in await events(pool)] == ["new", "day-old"]

    run_db(check, monkeypatch, timeout=30)


def test_the_summary_is_an_operator_operation(monkeypatch, tmp_path):
    async def check(pool):
        await with_task(pool, tmp_path)
        await insert(pool)
        async with v1_api(tmp_path / "api", pool) as call:
            status, answer = await call("manager_events.summary", client=True)
            assert status == 403
            status, answer = await call("manager_events.summary")
            assert (status, answer["result"]) == (200, {"window_hours": 24, "total": 1,
                                                        "by_state": {"skipped": 1}, "by_reason": {"off": 1},
                                                        "turn_cost_usd": 0.0, "pending": 0,
                                                        "oldest_pending_seconds": None, "failing": False})

    run_db(check, monkeypatch, timeout=30)


def test_the_kind_of_a_notice_target():
    assert completion_events.target_kind(notice_of("dm", "t")) == "dm"
    assert completion_events.target_kind(notice_of("telegram_private", "t")) == "dm"
    assert completion_events.target_kind(notice_of("group", "t")) == "group"


def test_a_failed_send_leaves_sent_at_empty_and_a_replay_sends_again(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        async def prepare():
            sender = with_sender(env)
            await allow_telegram(env)
            schedule = await env.schedule(notify="always",
                                          notify_target={"channel": "telegram", "chat": TELEGRAM_CHAT})
            run_id = await env.scheduler.run_now(schedule["schedule_id"])
            return sender, run_id, (await db_schedules.get_run(run_id))["task_id"]

        sender, run_id, task_id = await prepare()
        sender.fail_with = "channel down"
        await env.end_task(task_id, "completed", summary="done")
        await env.scheduler.idle()
        (row,) = await events(env.pool)
        assert sender.sent == [] and row["sent_at"] is None
        sender.fail_with = None
        await env.scheduler._notifier.run_ended(run_id)
        assert len(sender.sent) == 1 and (await events(env.pool))[0]["sent_at"] is not None

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("hang", ["record", "mark_sent"])
def test_a_hanging_database_call_does_not_hold_back_the_notice(monkeypatch, tmp_path, hang):
    async def check(pool):
        manager, bus = await manager_with_task(pool, tmp_path)
        monkeypatch.setattr(completion_events, "DB_TIMEOUT_SECONDS", 0.2)

        async def hangs(*_, **__):
            await asyncio.sleep(30)

        monkeypatch.setattr(store, hang, hangs)
        await asyncio.wait_for(manager._handle_goal_status(goal_status(notice=notice_of("dm", "ended"))), 5)
        assert len(sent(bus, "channel-lark")) == 1
        await bus.shutdown()

    run_db(check, monkeypatch, timeout=30)


def test_the_foreign_keys_that_are_cleared_or_cascaded_have_indexes(monkeypatch):
    async def check(pool):
        found = {row["indexname"] for row in await pool.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'manager_events' AND schemaname = current_schema()")}
        assert {"idx_manager_events_task", "idx_manager_events_schedule_run", "idx_manager_events_record"} <= found

    run_db(check, monkeypatch, timeout=30)


async def _events_shape(pool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The events' and the outbox's columns and constraints (validated or not), as the catalog has them."""
    columns = await pool.fetch(
        "SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name IN ('manager_events', 'schedule_notifications') "
        "ORDER BY 1, 2")
    constraints = await pool.fetch(
        "SELECT conrelid::regclass::text AS tbl, conname, pg_get_constraintdef(oid) AS def, convalidated "
        "FROM pg_constraint WHERE conrelid IN ('manager_events'::regclass, 'schedule_notifications'::regclass) "
        "ORDER BY 1, 2")
    return [dict(r) for r in columns], [dict(r) for r in constraints]


def test_a_database_whose_events_predate_the_new_reasons_and_delivery_gets_them_validated_after_the_commit(
        monkeypatch, tmp_path):
    async def stopped(conn: Any) -> None:
        return None

    async def check(pool):
        fresh = await _events_shape(pool)
        await pool.execute(
            "ALTER TABLE manager_events DROP CONSTRAINT manager_events_reason_check, ADD CONSTRAINT "
            "manager_events_reason_check CHECK (reason IN ('off', 'group_chat', 'manager_off', 'turn_failed', "
            "'turn_timeout', 'turn_stopped', 'empty_reply', 'send_failed', 'queue_deadline', 'restart')); "
            "ALTER TABLE manager_events DROP COLUMN delivery, DROP COLUMN delivery_attempts, "
            "DROP COLUMN delivery_at, DROP COLUMN delivery_error, DROP COLUMN delivery_claim; "
            "ALTER TABLE schedule_notifications DROP COLUMN manager_event_id; "
            "DELETE FROM schema_migrations WHERE name IN ('manager_event_reasons', 'manager_event_delivery', "
            "'schedule_notification_events', 'schedule_notification_events_online', 'manager_event_reasons_online', "
            "'manager_event_delivery_online')")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        with monkeypatch.context() as patch:  # the start stops once the schema transaction committed
            patch.setattr(connection, "_finish_online", stopped)
            await connection.ensure_schema(pool)
        # Added NOT VALID under the schema transaction's lock: nothing was scanned there.
        _, constraints = await _events_shape(pool)
        assert {c["conname"] for c in constraints if not c["convalidated"]} == {
            "manager_events_reason_check", "manager_events_delivery_check",
            "schedule_notifications_manager_event_id_fkey"}
        await connection.ensure_schema(pool)  # the next start validates them after its commit
        assert await _events_shape(pool) == fresh
        assert {"manager_event_reasons_online", "manager_event_delivery_online",
                "schedule_notification_events_online"} <= {
            r["name"] for r in await pool.fetch("SELECT name FROM schema_migrations")}
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        await with_task(pool, tmp_path)
        await insert(pool, dedupe_key="k", state="skipped", reason="no_chat")
        await insert(pool, dedupe_key="k2", state="fallback", reason="target_changed", delivery="failed",
                     delivery_attempts=2)
        with pytest.raises(asyncpg.CheckViolationError):
            await insert(pool, dedupe_key="k3", state="fallback", reason="restart", delivery="lost")

    run_db(check, monkeypatch, timeout=30)
