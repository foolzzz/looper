"""Idle checks on PostgreSQL (assistant phase 3 RFC, 4.3 to 4.8, 9.2): the service's tick, what a check settles as, the
switch and its prerequisites, the table, and the migrations. Runs only when LOOPER_TEST_DATABASE_URL points at a
disposable database.

Not here (they need the Manager's wiring, a later change): the Manager's ``idle_for``, the turn, the lane a check
runs in and what a user message does to a queued or running check.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import api_v1, attention, idle_check_settings, idle_checks as ic, proactive_delivery as delivery
from looper.agents.action_levels import ActionLevel
from looper.agents.channels.base import PostOutcome, PostReceipt
from looper.agents.message import MessageType
from looper.agents.operator_identity import operator_dm_target
from looper.db import connection, idle_checks as store, schedules as db_schedules
from tests.db_support import DSN, fresh_connection, run_db, run_with_channel_configs, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

UTC = timezone.utc
OP = "ou_operatorsynthetic"
CHANNEL = "channel-lark"
SWITCHES = (idle_check_settings.KEY, "delivery.enabled", "attention.enabled", "manager.lanes.enabled")


class Bus:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[Any] = []
        self.fail = fail

    async def publish(self, envelope: Any) -> None:
        if self.fail:
            raise RuntimeError("bus down")
        self.published.append(envelope)


class Poster:
    """``ChannelRegistry.post`` as a stand-in: records what is sent, answers as told."""

    def __init__(self, outcome: PostOutcome | Exception = PostOutcome.SENT) -> None:
        self.outcome = outcome
        self.posts: list[dict[str, Any]] = []

    async def __call__(self, channel_id, target, text, *, idempotency_key=None, **_):
        self.posts.append({"channel_id": channel_id, "chat": target.id, "text": text, "key": idempotency_key})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if self.outcome is PostOutcome.SENT:
            return PostReceipt(True, external_ids=("om_synthetic",))
        return PostReceipt(False, error="refused", outcome=self.outcome)


@pytest.fixture(autouse=True)
def plain_policy(monkeypatch):
    """No quiet hours and a UTC day: the tests choose them (``quiet``) instead of depending on the hour they run."""
    state = SimpleNamespace(quiet=None, cap=5)
    monkeypatch.setattr(delivery, "load_policy", lambda: delivery.DeliveryPolicy(
        timezone=UTC, quiet_hours=state.quiet, dm_daily_cap=state.cap))
    return state


def around_now() -> delivery.DailyWindow:
    now = datetime.now(UTC)
    return delivery.DailyWindow((now - timedelta(hours=1)).time().replace(second=0, microsecond=0),
                                (now + timedelta(hours=1)).time().replace(second=0, microsecond=0))


async def switches(pool, **values: bool) -> None:
    for key in SWITCHES:
        name = "idle" if key == idle_check_settings.KEY else key.split(".")[0]
        on = values.get(name, values.get(key, True))
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2) "
                           "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key, "true" if on else "false")


async def operator(pool, *, enabled: bool = True) -> None:
    await pool.execute("UPDATE channel_configs SET enabled = $2, config = $3 WHERE channel_id = $1", CHANNEL, enabled,
                       json.dumps({"allowed_open_ids": [OP], "operator_sender_id": OP}))


async def failing_schedule(pool, schedule_id: str = "nightly", **columns) -> None:
    await pool.execute("INSERT INTO projects (id, name) VALUES ('proj', 'web') ON CONFLICT (id) DO NOTHING")
    names = ["id", "project_id", "name", "cron", "timezone", "requirement", "consecutive_failures", *columns]
    name = "Nightly report" if schedule_id == "nightly" else schedule_id
    values = [schedule_id, "proj", name, "0 9 * * *", "UTC", "Check it", 2, *columns.values()]
    await pool.execute(f"INSERT INTO schedules ({', '.join(names)}) "
                       f"VALUES ({', '.join(f'${n + 1}' for n in range(len(names)))})", *values)


async def ready(pool) -> None:
    """Everything a check needs: the four switches, the operator's chat and a failing schedule."""
    await switches(pool)
    await operator(pool)
    await failing_schedule(pool)


def service(bus: Bus, *, idle: float | None = 3600.0, running: bool = True, **kwargs) -> ic.IdleCheckService:
    return ic.IdleCheckService(bus, idle_for=lambda: idle, manager_running=lambda: running, **kwargs)


async def rows(pool) -> list[dict[str, Any]]:
    return [dict(row) for row in await pool.fetch("SELECT * FROM idle_checks ORDER BY id")]


# --- the switch is off: nothing happens -----------------------------------------------------------------------------


def test_with_the_switch_off_many_ticks_write_nothing_publish_nothing_and_never_ask_main(monkeypatch):
    async def check(pool):
        await ready(pool)
        await pool.execute("UPDATE system_settings SET value = 'false' WHERE key = 'manager.idle_checks.enabled'")
        bus, asked = Bus(), []
        idle = ic.IdleCheckService(bus, idle_for=lambda: asked.append(1) or 7200.0, manager_running=lambda: True)
        before = await pool.fetchval("SELECT count(*) FROM proactive_deliveries")
        for _ in range(20):
            assert await idle.run_once() is None
        assert await rows(pool) == [] and bus.published == [] and asked == []
        assert await pool.fetchval("SELECT count(*) FROM llm_records") == 0
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries WHERE source = 'idle_check'") == 0
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == before
        assert idle.skips[ic.Skip.OFF] == 20
        # No row at all is off too.
        await pool.execute("DELETE FROM system_settings WHERE key = 'manager.idle_checks.enabled'")
        assert await idle.run_once() is None and await rows(pool) == []
        assert await idle_check_settings.enabled() is False

    run_with_channel_configs(check, monkeypatch)


# --- a tick that finds something starts one check ---------------------------------------------------------------


def test_a_new_item_starts_one_check_and_wakes_the_manager(monkeypatch):
    async def check(pool):
        await ready(pool)
        bus = Bus()
        idle = service(bus)
        check_id = await idle.run_once()
        (row,) = await rows(pool)
        assert (row["id"], row["state"], row["manager_id"], row["reason"]) == (check_id, "running", "manager", None)
        assert (row["channel_id"], row["chat"]) == (CHANNEL, OP)
        assert row["local_date"] == datetime.now(UTC).date()
        (item,) = json.loads(row["items"])
        assert (item["section"], item["key"], item["title"]) == ("schedules", "schedule:nightly", "Nightly report")
        assert item["facts"]["failures"] == 2 and item["facts"]["context"] == "web"
        (envelope,) = bus.published
        assert envelope.message_type is MessageType.IDLE_CHECK_DUE and envelope.payload == {"check_id": check_id}
        assert envelope.target.agent_id == "manager"
        # The very next tick is inside the two hours between checks, and starts nothing.
        assert await idle.run_once() is None and len(await rows(pool)) == 1 and len(bus.published) == 1
        assert idle.skips[ic.Skip.TOO_SOON] == 1

    run_with_channel_configs(check, monkeypatch)


def test_a_bus_that_refuses_the_wake_up_fails_the_check_instead_of_leaving_it_running(monkeypatch):
    async def check(pool):
        await ready(pool)
        assert await service(Bus(fail=True)).run_once() is None
        (row,) = await rows(pool)
        assert (row["state"], row["reason"]) == ("failed", "turn_failed") and row["finished_at"] is not None

    run_with_channel_configs(check, monkeypatch)


def test_nothing_new_starts_no_check(monkeypatch):
    async def check(pool):
        await switches(pool)
        await operator(pool)  # no failing schedule, login, intent, ledger item or lost message
        bus = Bus()
        idle = service(bus)
        assert await idle.run_once() is None
        assert await rows(pool) == [] and bus.published == []
        assert idle.skips[ic.Skip.NO_NEW_ITEMS] == 1

    run_with_channel_configs(check, monkeypatch)


def two_hours_back(pool):
    return pool.execute("UPDATE idle_checks SET created_at = created_at - interval '3 hours'")


@pytest.mark.parametrize("state,again", [("silent", False), ("sent", False), ("uncertain", False),
                                         ("deferred", True), ("failed", True), ("preempted", True)])
def test_an_item_is_named_once_unless_its_check_never_reached_the_operator(monkeypatch, state, again):
    async def check(pool):
        await ready(pool)
        bus = Bus()
        idle = service(bus)
        check_id = await idle.run_once()
        await store.settle(check_id, state, reason="turn_failed" if state == "failed" else None)
        await two_hours_back(pool)  # the next idle period, past the gap
        assert (await idle.run_once() is not None) is again
        assert len(await rows(pool)) == (2 if again else 1)

    run_with_channel_configs(check, monkeypatch)


def test_a_check_older_than_a_week_no_longer_hides_its_items(monkeypatch):
    async def check(pool):
        await ready(pool)
        idle = service(Bus())
        check_id = await idle.run_once()
        await store.settle(check_id, "silent")
        await pool.execute("UPDATE idle_checks SET created_at = created_at - interval '8 days', "
                           "local_date = local_date - 8")
        assert await idle.run_once() is not None

    run_with_channel_configs(check, monkeypatch)


def test_a_schedule_that_fails_succeeds_or_skips_its_gate_and_fails_again_is_a_new_item(monkeypatch):
    """No alert is raised: the item's age is empty and only ``last_ok_at`` tells one failing run from the next."""
    async def check(pool):
        await ready(pool)
        await pool.execute("UPDATE schedules SET consecutive_failures = 1")
        idle = service(Bus())
        for ending in ("skipped_gate", "completed"):
            check_id = await idle.run_once()
            assert check_id is not None, ending
            await store.settle(check_id, "silent")
            await two_hours_back(pool)
            assert await idle.run_once() is None  # the same failure: not again
            async with pool.acquire() as conn:
                await db_schedules.count_outcome(conn, "nightly", ending, datetime.now(UTC))  # it recovers ...
                await db_schedules.count_outcome(conn, "nightly", "failed", datetime.now(UTC))  # ... and fails again
            await two_hours_back(pool)
        assert await idle.run_once() is not None

    run_with_channel_configs(check, monkeypatch)


# --- the rules that keep a tick from checking ---------------------------------------------------------------------


async def seed_checks(pool, count: int, *, hours_ago: float, state: str = "silent") -> None:
    for _ in range(count):
        await pool.execute(
            "INSERT INTO idle_checks (manager_id, local_date, items, state, finished_at, channel_id, chat, created_at) "
            "VALUES ('manager', $1, $2::jsonb, $3, CASE WHEN $3 = 'running' THEN NULL ELSE now() END, $4, $5, "
            "now() - make_interval(hours => $6))",
            datetime.now(UTC).date(), json.dumps([{"section": "logins", "key": "login:other", "since": None,
                                                    "title": "Other", "facts": {}}]), state, CHANNEL, OP, hours_ago)


async def fill_dm_cap(pool, count: int = 5) -> None:
    for number in range(count):
        await pool.execute(
            "INSERT INTO proactive_deliveries (source, channel_id, chat, chat_kind, dedupe_key, decision, reason, "
            "counted, local_date, decided_at) VALUES ('intent', $1, $2, 'direct', $3, 'send', 'within_limits', true, "
            "$4, now())", CHANNEL, OP, f"seed-{number}", datetime.now(UTC).date())


@pytest.mark.parametrize("name,prepare,skip", [
    ("lanes off", lambda pool, policy: switches(pool, manager=False), ic.Skip.LANES_OFF),
    ("delivery off", lambda pool, policy: switches(pool, delivery=False), ic.Skip.DELIVERY_OFF),
    ("attention off", lambda pool, policy: switches(pool, attention=False), ic.Skip.ATTENTION_OFF),
    ("quiet hours", lambda pool, policy: setattr(policy, "quiet", around_now()), ic.Skip.QUIET_HOURS),
    ("four checks today", lambda pool, policy: seed_checks(pool, 4, hours_ago=5), ic.Skip.DAY_LIMIT),
    ("a check an hour ago", lambda pool, policy: seed_checks(pool, 1, hours_ago=1), ic.Skip.TOO_SOON),
    ("the direct chat is full", lambda pool, policy: fill_dm_cap(pool), ic.Skip.DM_CAP_FULL),
    ("no direct chat", lambda pool, policy: operator(pool, enabled=False), ic.Skip.NO_TARGET),
])
def test_a_tick_that_a_rule_stops_starts_nothing_and_writes_nothing(monkeypatch, plain_policy, name, prepare, skip):
    async def check(pool):
        await ready(pool)
        # Switch "manager" is the lanes switch: ``switches(manager=False)`` turns off ``manager.lanes.enabled``.
        outcome = prepare(pool, plain_policy)
        if outcome is not None:
            await outcome
        before = len(await rows(pool))
        bus, deliveries = Bus(), await pool.fetchval("SELECT count(*) FROM proactive_deliveries")
        idle = service(bus)
        assert await idle.run_once() is None, name
        assert len(await rows(pool)) == before and bus.published == []
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == deliveries
        assert idle.skips == {skip: 1}, (name, idle.skips)

    run_with_channel_configs(check, monkeypatch)


def test_a_busy_main_a_stopped_manager_and_a_drain_start_nothing(monkeypatch):
    async def check(pool):
        await ready(pool)
        for idle in (service(Bus(), idle=None), service(Bus(), idle=29 * 60.0), service(Bus(), running=False),
                     service(Bus(), drain=SimpleNamespace(draining=True))):
            assert await idle.run_once() is None
        assert await rows(pool) == []

    run_with_channel_configs(check, monkeypatch)


def test_a_stopped_manager_fails_the_checks_that_still_wait_for_it(monkeypatch):
    async def check(pool):
        await ready(pool)
        await seed_checks(pool, 1, hours_ago=0.1, state="running")
        assert await service(Bus(), running=False).run_once() is None
        (row,) = await rows(pool)
        assert (row["state"], row["reason"]) == ("failed", "manager_off")

    run_with_channel_configs(check, monkeypatch)


def test_a_restart_fails_the_checks_the_server_before_left_running(monkeypatch):
    async def check(pool):
        await ready(pool)
        await seed_checks(pool, 2, hours_ago=0.2, state="running")
        await seed_checks(pool, 1, hours_ago=5, state="sent")
        idle = service(Bus(), idle=None, interval=3600)
        await idle.start()
        await idle.stop()
        assert [(row["state"], row["reason"]) for row in await rows(pool)] == [
            ("failed", "restart"), ("failed", "restart"), ("sent", None)]

    run_with_channel_configs(check, monkeypatch)


def test_only_one_server_decides_at_a_time(monkeypatch):
    async def check(pool):
        await ready(pool)
        async with pool.acquire() as holder:
            assert await holder.fetchval("SELECT pg_try_advisory_lock($1::int, $2::int)", *ic._LOCK)
            assert await service(Bus()).run_once() is None  # another server is deciding
            await holder.execute("SELECT pg_advisory_unlock($1::int, $2::int)", *ic._LOCK)
        assert await rows(pool) == []
        assert await service(Bus()).run_once() is not None

    run_with_channel_configs(check, monkeypatch)


# --- what a check settles as -------------------------------------------------------------------------------------


async def running_check(pool, bus: Bus | None = None) -> dict[str, Any]:
    await ready(pool)
    check_id = await service(bus or Bus()).run_once()
    return await store.get(check_id)


def test_no_reply_settles_silent_and_sends_nothing(monkeypatch):
    async def check(pool):
        post, found = Poster(), await running_check(pool)
        assert await ic.deliver_result(found, "  NO_REPLY\n", post=post) == "silent"
        assert post.posts == []
        row = await store.get(found["id"])
        assert (row["state"], row["reply"], row["finished_at"] is not None) == ("silent", "NO_REPLY", True)
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0

    run_with_channel_configs(check, monkeypatch)


@pytest.mark.parametrize("reply", ["NO_REPLY, nothing new", "", "Done. NO_REPLY"])
def test_a_reply_that_mixes_no_reply_with_words_or_says_nothing_fails_and_is_not_sent(monkeypatch, reply):
    async def check(pool):
        post, found = Poster(), await running_check(pool)
        assert await ic.deliver_result(found, reply, post=post) == "failed"
        row = await store.get(found["id"])
        assert (row["state"], row["reason"]) == ("failed", "invalid_reply") and post.posts == []

    run_with_channel_configs(check, monkeypatch)


def test_a_message_goes_to_the_direct_chat_through_the_delivery_layer(monkeypatch):
    async def check(pool):
        post, found = Poster(), await running_check(pool)
        assert await ic.deliver_result(found, "日报任务连续失败 2 次。发 /schedules 看看。", post=post) == "sent"
        (sent,) = post.posts
        assert (sent["channel_id"], sent["chat"], sent["key"]) == (CHANNEL, OP, f"idle-{found['id']}")
        assert sent["text"] == f"日报任务连续失败 2 次。发 /schedules 看看。\n\n{ic.DETAILS_LINE}"
        row = await store.get(found["id"])
        assert (row["state"], row["reply"]) == ("sent", "日报任务连续失败 2 次。发 /schedules 看看。")
        delivered = await pool.fetchrow("SELECT * FROM proactive_deliveries WHERE dedupe_key = $1",
                                        f"idle-{found['id']}")
        assert (delivered["source"], delivered["decision"], delivered["reason"], delivered["counted"],
                delivered["outcome"], delivered["external_id"], delivered["chat_kind"]) == (
            "idle_check", "send", "within_limits", True, "sent", "om_synthetic", "direct")
        # It is kept in the chat, as a completion reply is, so a reply to it has its context.
        stored = await pool.fetchrow("SELECT role, content, channel FROM chat_messages WHERE message_id = $1",
                                     f"idle-{found['id']}")
        assert (stored["role"], stored["content"], stored["channel"]) == ("assistant", sent["text"], "lark")
        # It counts toward the chat's day: four more fill it.
        await fill_dm_cap(pool, 4)
        again = service(Bus())
        await pool.execute("UPDATE idle_checks SET created_at = created_at - interval '3 hours'")
        await pool.execute("UPDATE schedules SET failures_alert_at = now()")  # a new item: the alert
        assert await again.run_once() is None and again.skips == {ic.Skip.DM_CAP_FULL: 1}

    run_with_channel_configs(check, monkeypatch)


def test_a_send_that_may_have_gone_is_uncertain_and_never_sent_again(monkeypatch):
    async def check(pool):
        found = await running_check(pool)
        for post in (Poster(PostOutcome.UNKNOWN), Poster(RuntimeError("socket"))):
            await pool.execute("UPDATE idle_checks SET state = 'running', finished_at = NULL, reply = NULL")
            await pool.execute("DELETE FROM proactive_deliveries")
            assert await ic.deliver_result(found, "see /todo", post=post) == "uncertain"
            assert len(post.posts) == 1
            assert (await store.get(found["id"]))["state"] == "uncertain"
            assert await pool.fetchval("SELECT outcome FROM proactive_deliveries") == "unknown"

    run_with_channel_configs(check, monkeypatch)


def test_a_send_the_channel_refused_fails_and_its_items_come_back(monkeypatch):
    async def check(pool):
        post, found = Poster(PostOutcome.REJECTED), await running_check(pool)
        assert await ic.deliver_result(found, "see /todo", post=post) == "failed"
        row = await store.get(found["id"])
        assert (row["state"], row["reason"]) == ("failed", "send_rejected")
        assert await pool.fetchval("SELECT outcome FROM proactive_deliveries") == "rejected"
        assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE message_id = $1",
                                   f"idle-{found['id']}") == 0  # nothing reached the chat
        await two_hours_back(pool)
        assert await service(Bus()).run_once() is not None

    run_with_channel_configs(check, monkeypatch)


def test_the_delivery_layer_saying_digest_sends_nothing_and_the_item_is_not_marked_mentioned(monkeypatch, plain_policy):
    async def check(pool):
        post, found = Poster(), await running_check(pool)
        plain_policy.quiet = around_now()  # the layer's quiet hours began while the turn ran
        assert await ic.deliver_result(found, "see /todo", post=post) == "deferred"
        assert post.posts == []
        row = await store.get(found["id"])
        assert (row["state"], row["reply"]) == ("deferred", "see /todo")
        decided = await pool.fetchrow("SELECT decision, reason, digest_date FROM proactive_deliveries "
                                      "WHERE dedupe_key = $1", f"idle-{found['id']}")
        assert (decided["decision"], decided["reason"]) == ("digest", "quiet_hours")
        plain_policy.quiet = None
        await two_hours_back(pool)
        assert await service(Bus()).run_once() is not None  # the item is still new

    run_with_channel_configs(check, monkeypatch)


def test_a_delivery_decision_that_fails_sends_nothing(monkeypatch):
    async def check(pool):
        post, found = Poster(), await running_check(pool)

        async def broken(*args, **kwargs):
            raise RuntimeError("database gone")

        monkeypatch.setattr(delivery, "decide", broken)
        assert await ic.deliver_result(found, "see /todo", post=post) == "failed"
        assert post.posts == []
        row = await store.get(found["id"])
        assert (row["state"], row["reason"]) == ("failed", "delivery_failed")

    run_with_channel_configs(check, monkeypatch)


def test_a_check_ends_once(monkeypatch):
    async def check(pool):
        found = await running_check(pool)
        assert await store.settle(found["id"], "silent") is True
        assert await store.settle(found["id"], "failed", reason="restart") is False
        assert (await store.get(found["id"]))["state"] == "silent"
        with pytest.raises(ValueError):
            await store.settle(found["id"], "failed")  # a failure says why
        with pytest.raises(ValueError):
            await store.settle(found["id"], "silent", reason="restart")

    run_with_channel_configs(check, monkeypatch)


def test_three_failed_checks_in_a_row_log_a_warning(monkeypatch, caplog):
    async def check(pool):
        await ready(pool)
        for number in range(3):
            check_id = await store.insert_running(pool, manager_id="manager", local_date=datetime.now(UTC).date(),
                                                  items=[{"section": "logins", "key": f"k{number}", "since": None,
                                                          "title": "t", "facts": {}}], channel_id=CHANNEL, chat=OP)
            await ic.settle(check_id, "failed", reason="turn_failed")
        assert sum("idle checks failing" in record.message for record in caplog.records) == 1

    run_with_channel_configs(check, monkeypatch)


def test_old_checks_are_purged_but_a_running_one_stays(monkeypatch):
    async def check(pool):
        await seed_checks(pool, 2, hours_ago=100 * 24)
        await seed_checks(pool, 1, hours_ago=100 * 24, state="running")
        await seed_checks(pool, 1, hours_ago=24)
        assert await store.purge(datetime.now(UTC) - timedelta(days=store.RETENTION_DAYS)) == 2
        assert sorted(row["state"] for row in await rows(pool)) == ["running", "silent"]

    run_with_channel_configs(check, monkeypatch)


def test_the_table_refuses_rows_that_break_its_own_rules(monkeypatch):
    import asyncpg

    async def check(pool):
        day, item = datetime.now(UTC).date(), json.dumps([{"section": "logins", "key": "k"}])
        for items, state, reason, finished in (("[]", "running", None, False), (item, "failed", None, True),
                                               (item, "silent", "restart", True), (item, "running", None, True),
                                               (item, "silent", None, False), (item, "bogus", None, True),
                                               (json.dumps([{}] * 11), "silent", None, True)):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute("INSERT INTO idle_checks (manager_id, local_date, items, state, reason, channel_id, "
                                   "chat, finished_at) VALUES ('manager', $1, $2::jsonb, $3, $4, 'c', 'u', "
                                   "CASE WHEN $5 THEN now() END)", day, items, state, reason, finished)

    run_with_channel_configs(check, monkeypatch)


# --- the operator's direct chat, shared with reminders ---------------------------------------------------------------


def test_the_operator_dm_target_is_the_first_enabled_bot_of_the_default_manager_that_names_its_operator(monkeypatch):
    async def check(pool):
        assert await operator_dm_target() is None  # no bot names an operator
        await pool.execute("UPDATE channel_configs SET enabled = false, config = $2 WHERE channel_id = $1", CHANNEL,
                           json.dumps({"operator_sender_id": OP}))
        assert await operator_dm_target() is None  # a bot that is off is not one to send through
        await pool.execute("UPDATE channel_configs SET enabled = true WHERE channel_id = $1", CHANNEL)
        found = await operator_dm_target()
        assert (found.channel_id, found.chat, found.where) == (CHANNEL, OP, "你的 Lark 私聊")

    run_with_channel_configs(check, monkeypatch)


# --- the switch ---------------------------------------------------------------------------------------------------


def test_the_switch_is_the_operators_alone_and_turning_it_on_needs_its_prerequisites(monkeypatch, tmp_path):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            for action, arguments in (("get", {}), ("update", {"enabled": True})):
                operation = api_v1.find("POST", f"/api/v1/idle_check_settings/{action}")
                assert operation.operator_only and operation.tool is None and operation.level is ActionLevel.INSIDE
                status, answer = await call(f"idle_check_settings.{action}", arguments, client=True)
                assert status == 403 and answer["error"]["code"] == "forbidden"
            assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key LIKE 'manager.idle%'") == 0
            status, answer = await call("idle_check_settings.get", {})
            assert (status, answer["result"]) == (200, {"enabled": False})

            status, answer = await call("idle_check_settings.update", {"enabled": True})
            assert status == 409 and answer["error"]["code"] == "conflict"
            for missing in ("delivery.enabled", "attention.enabled", "manager.lanes.enabled"):
                assert missing in answer["error"]["message"]
            assert await idle_check_settings.enabled() is False

            await pool.execute("INSERT INTO system_settings (key, value) VALUES ('delivery.enabled', 'true'), "
                               "('attention.enabled', 'true')")
            status, answer = await call("idle_check_settings.update", {"enabled": True})
            assert status == 409 and "manager.lanes.enabled" in answer["error"]["message"]
            assert "delivery.enabled" not in answer["error"]["message"]
            assert "attention.enabled" not in answer["error"]["message"]

            await pool.execute("INSERT INTO system_settings (key, value) VALUES ('manager.lanes.enabled', 'true')")
            for arguments, expected in (({"enabled": True}, True), ({"enabled": False}, False),
                                        ({"enabled": True}, True), ({"enabled": None}, False)):
                status, answer = await call("idle_check_settings.update", arguments)
                assert (status, answer["result"]) == (200, {"enabled": expected}), answer
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key = $1",
                                       idle_check_settings.KEY) is None
            for value in ("true", 1):
                status, _ = await call("idle_check_settings.update", {"enabled": value})
                assert status == 400
            # Turning a prerequisite off later does not turn the switch off; it only stops new checks.
            await call("idle_check_settings.update", {"enabled": True})
            await pool.execute("UPDATE system_settings SET value = 'false' WHERE key = 'manager.lanes.enabled'")
            assert await idle_check_settings.enabled() is True

    run_db(check, monkeypatch)


def test_the_switch_has_its_own_operations_and_chat_settings_is_unchanged():
    from looper.agents import chat_settings

    assert not any("idle" in name for name in dir(chat_settings))
    for operation in (api_v1.find("POST", "/api/v1/idle_check_settings/get"),
                      api_v1.find("POST", "/api/v1/idle_check_settings/update")):
        assert operation.operator_only and len(operation.summary) <= 120
        assert operation.description.startswith(operation.summary)
    assert api_v1.find("POST", "/api/v1/idle_check_settings/update").confirms
    assert api_v1.find("POST", "/api/v1/idle_check_settings/update").risk == "high"


# --- the summary gains one fact; /todo and the Needs you page do not change -------------------------------------------


def test_the_schedules_section_carries_last_ok_at_and_the_todo_text_does_not_change(monkeypatch):
    async def check(pool):
        ok_at = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
        await failing_schedule(pool, "nightly", last_ok_at=ok_at)
        await failing_schedule(pool, "never-ok")
        summary = await attention.collect()
        (section,) = [s for s in summary["sections"] if s["kind"] == "schedules"]
        assert {item["key"]: item["facts"]["last_ok_at"] for item in section["items"]} == {
            "schedule:nightly": ok_at, "schedule:never-ok": None}
        assert sorted(next(item["facts"] for item in section["items"]).keys()) == [
            "alerts", "failures", "last_ok_at", "last_status"]
        # The text is the same as before the fact existed (what the main branch at ecc2eedbf produces).
        assert attention.summary_text({"total": 1, "sections": [{
            "kind": "schedules", "state": "ok", "count": 1, "error": None,
            "items": [{"key": "schedule:nightly", "title": "Nightly report", "context": "web", "since": None,
                       "href": "#schedules/nightly", "ref": None,
                       "facts": {"failures": 2, "alerts": [], "last_status": "failed", "last_ok_at": ok_at}}]}]}) == (
            "需要你处理（1 项）：\n\n失败的定时任务 1：\n- Nightly report（web；连续失败 2 次）")

    run_db(check, monkeypatch)


# --- the migrations --------------------------------------------------------------------------------------------------

_BASE_SOURCES = ("task_notice", "completion", "progress", "gate", "gate_reminder", "subscription", "intent",
                 "schedule", "advisor_alert", "autonomous", "user_reminder")


async def source_check(conn) -> tuple[str, str, bool]:
    row = await conn.fetchrow(
        "SELECT conname, pg_get_constraintdef(oid) AS definition, convalidated FROM pg_constraint "
        "WHERE conrelid = 'proactive_deliveries'::regclass AND contype = 'c' AND conname LIKE '%source%'")
    return row["conname"], row["definition"], row["convalidated"]


def values_of(definition: str) -> set[str]:
    import re

    return set(re.findall(r"'([a-z_]+)'", definition))


async def widen_like_another_migration(conn, values: tuple[str, ...]) -> None:
    """What the morning brief's and the triage's migrations do: a fixed list, written by them, without ours."""
    name, _, _ = await source_check(conn)
    await conn.execute(f"ALTER TABLE proactive_deliveries DROP CONSTRAINT {name}")
    await conn.execute("ALTER TABLE proactive_deliveries ADD CONSTRAINT proactive_deliveries_source_check CHECK "
                       f"(source IN ({', '.join(repr(value) for value in values)}))")


async def deliver_row(conn, source: str, key: str) -> None:
    await conn.execute("INSERT INTO proactive_deliveries (source, channel_id, chat, chat_kind, dedupe_key) "
                       "VALUES ($1, 'web', 'c', 'direct', $2)", source, key)


def test_a_new_database_has_the_table_and_the_widened_check_and_records_only_the_baseline():
    import asyncio

    from looper.db import bridge_upgrade

    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT to_regclass('idle_checks') IS NOT NULL")
            _, definition, validated = await source_check(conn)
            assert values_of(definition) == {*_BASE_SOURCES, "idle_check"} and validated
            await deliver_row(conn, "idle_check", "k1")
            with pytest.raises(Exception):
                await deliver_row(conn, "no_such_source", "k2")
            assert {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")} == {
                bridge_upgrade.BASELINE}
            assert not await connection.schema_change_pending(conn)

    asyncio.run(asyncio.wait_for(check(), 60))


@pytest.mark.parametrize("others", [("morning_brief",), ("morning_brief", "triage"), ("triage",), ()])
def test_the_source_check_keeps_the_values_other_migrations_added(others):
    """Applied after a database already has ``morning_brief`` (and any later value), the new check is the current
    one plus ``idle_check``: no list of ours decides what survives."""
    import asyncio

    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await widen_like_another_migration(conn, (*_BASE_SOURCES, *others))
            for source in (*_BASE_SOURCES[:3], *others):
                await deliver_row(conn, source, f"before-{source}")
            await conn.execute("DELETE FROM schema_migrations WHERE name = 'idle_check_source'")
            from looper.db.migrations import idle_check_source

            assert await idle_check_source.pending(conn)  # the object, not the marker, says so
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            _, definition, validated = await source_check(conn)
            assert values_of(definition) == {*_BASE_SOURCES, *others, "idle_check"} and validated
            assert not await idle_check_source.pending(conn) and not await connection.schema_change_pending(conn)
            assert await conn.fetchval("SELECT count(*) FROM proactive_deliveries") == 3 + len(others)
            for source in (*others, "idle_check"):
                await deliver_row(conn, source, f"after-{source}")  # still accepted
            with pytest.raises(Exception):
                await deliver_row(conn, "no_such_source", "bad")
            await connection.ensure_schema(conn)  # idempotent
            assert values_of((await source_check(conn))[1]) == {*_BASE_SOURCES, *others, "idle_check"}

    asyncio.run(asyncio.wait_for(check(), 60))


def test_a_check_rebuilt_later_without_idle_check_is_widened_again():
    """A migration that lands after this one and writes its own fixed list is repaired by the next start."""
    import asyncio

    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await widen_like_another_migration(conn, (*_BASE_SOURCES, "morning_brief"))
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert values_of((await source_check(conn))[1]) == {*_BASE_SOURCES, "morning_brief", "idle_check"}

    asyncio.run(asyncio.wait_for(check(), 60))


def test_the_table_is_added_to_a_database_that_has_not_got_it():
    import asyncio

    async def check():
        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("DROP TABLE idle_checks")
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT to_regclass('idle_checks') IS NOT NULL")
            assert "idle_checks" in {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")}
            assert not await connection.schema_change_pending(conn)

    asyncio.run(asyncio.wait_for(check(), 60))
