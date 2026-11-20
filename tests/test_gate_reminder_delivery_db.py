"""Gate reminders through the delivery layer, on a real PostgreSQL: with ``delivery.enabled`` off nothing changes and
nothing is recorded; on, every reminder is decided on (blocking: always sent, never counted) and recorded once
however often its post is tried; the courier does what a decision says; ``decide`` counts a direct chat's day in
``user.timezone`` and keeps a message's first decision.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from looper.agents import chat_settings, proactive_delivery as delivery
from tests.db_support import DSN, run_db
from tests.test_gate_channels_db import LARK_GROUP, LARK_USER, _Poster, _say, _task_with_gate

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

ROWS = ("SELECT kind, urgency, channel_id, chat, chat_kind, task_id, step_id, outcome, reason, counted, "
        "digest_date, sent_at IS NOT NULL AS sent FROM proactive_deliveries ORDER BY id")


async def _reminded(pool: Any, *, group: bool = False) -> None:
    if group:
        await _task_with_gate(pool, origin="channel-lark", chat=LARK_GROUP, kind="chat", mention=LARK_USER)
    else:
        await _task_with_gate(pool, origin="channel-lark", chat=LARK_USER, kind="user")
    await _say("task-a", "step-g1", "reminder", "Still waiting for you (25 h): storage")


@pytest.mark.parametrize("enabled", [False, True])
def test_a_reminder_is_posted_the_same_with_the_switch_off_or_on(monkeypatch: pytest.MonkeyPatch,
                                                                  enabled: bool) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier

        chat_settings.set_delivery_enabled(enabled)
        await _reminded(pool)
        poster = _Poster()
        assert await GateCourier(poster).run_once() == 2
        card, reminder = poster.posts
        assert reminder["text"].splitlines()[:2] == ["⏰ 还在等你 · 已等 0 小时 · settings file", "storage"]
        assert (reminder["channel"], reminder["target"].id, reminder["reply_to"]) == ("channel-lark", LARK_USER,
                                                                                      "om_101")
        rows = [dict(row) for row in await pool.fetch(ROWS)]
        if not enabled:
            assert rows == []  # off: nothing is recorded
            return
        # On: only the reminder is a proactive message; blocking, so sent and not counted.
        assert rows == [{"kind": "gate_reminder", "urgency": "blocking", "channel_id": "channel-lark", "chat": LARK_USER,
                         "chat_kind": "direct", "task_id": "task-a", "step_id": "step-g1", "outcome": "send",
                         "reason": "urgent", "counted": False, "digest_date": None, "sent": True}]

    run_db(test, monkeypatch)


@pytest.mark.parametrize("group", [False, True])
def test_a_reminder_at_night_over_the_cap_still_goes(monkeypatch: pytest.MonkeyPatch, group: bool) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier

        chat_settings.set_delivery_enabled(True)
        await _reminded(pool, group=group)
        chat = LARK_GROUP if group else LARK_USER
        # The chat already had its day's worth of counted messages, and it is 03:00 in the user's timezone.
        night = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('user.timezone', 'UTC')")
        for n in range(5):
            await pool.execute(
                "INSERT INTO proactive_deliveries (dedupe_key, kind, urgency, channel_id, chat, chat_kind, outcome, "
                "reason, counted, local_date) VALUES ($1, 'task_notice', 'normal', 'channel-lark', $2, 'direct', "
                "'send', 'within_limits', true, $3)", f"earlier:{n}", chat, night.date())
        real_decide = delivery.decide
        monkeypatch.setattr(delivery, "decide", lambda *args, **kwargs: real_decide(*args, **kwargs, now=night))
        poster = _Poster()
        await GateCourier(poster).run_once()
        assert poster.posts[1]["text"].startswith("⏰ 还在等你")
        row = dict(await pool.fetchrow(ROWS.replace("ORDER BY id", "WHERE kind = 'gate_reminder'")))
        assert (row["chat"], row["chat_kind"], row["outcome"], row["reason"], row["counted"], row["sent"]) == (
            chat, "group" if group else "direct", "send", "urgent", False, True)

    run_db(test, monkeypatch)


def test_a_reminder_tried_again_keeps_one_decision_and_is_marked_sent_when_it_goes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier

        chat_settings.set_delivery_enabled(True)
        await _task_with_gate(pool, origin="channel-lark", chat=LARK_USER, kind="user")
        poster = _Poster()
        courier = GateCourier(poster)
        await courier.run_once()  # the card
        await _say("task-a", "step-g1", "reminder", "Still waiting for you (25 h): storage")
        poster.fail = 1
        await courier.run_once()  # the reminder's post sends nothing: tried again later
        sent = "SELECT outcome, sent_at IS NOT NULL FROM proactive_deliveries"
        assert [tuple(row) for row in await pool.fetch(sent)] == [("send", False)]
        await pool.execute("UPDATE task_user_gate_messages SET post_retry_at = now() WHERE kind = 'reminder'")
        await courier.run_once()
        assert [tuple(row) for row in await pool.fetch(sent)] == [("send", True)]
        assert [post["text"][:1] for post in poster.posts] == ["⏸", "⏰", "⏰"]

    run_db(test, monkeypatch)


@pytest.mark.parametrize("outcome", [delivery.Outcome.DIGEST, delivery.Outcome.DROP])
def test_the_courier_does_what_the_decision_says(monkeypatch: pytest.MonkeyPatch, outcome: delivery.Outcome) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier

        chat_settings.set_delivery_enabled(True)
        decision = delivery.Decision(outcome, delivery.Reason.QUIET_HOURS if outcome is delivery.Outcome.DIGEST
                                     else delivery.Reason.SILENT,
                                     digest_date=date(2026, 11, 21) if outcome is delivery.Outcome.DIGEST else None)
        monkeypatch.setattr(delivery, "deliver", lambda *args: decision)
        await _reminded(pool)
        poster = _Poster()
        await GateCourier(poster).run_once()
        assert [post["text"].splitlines()[0] for post in poster.posts] == ["⏸ 需要你决定 · settings file"]
        assert [tuple(row) for row in await pool.fetch(
            "SELECT kind, post_state FROM task_user_gate_messages ORDER BY id")] == [
                ("opened", "sent"), ("reminder", "none")]
        assert [tuple(row) for row in await pool.fetch(
            "SELECT outcome, reason, sent_at IS NULL FROM proactive_deliveries")] == [
                (outcome.value, decision.reason.value, True)]

    run_db(test, monkeypatch)


def test_decide_counts_a_direct_chats_day_in_the_users_timezone_and_keeps_a_first_decision(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('user.timezone', 'Asia/Tokyo')")
        notice = delivery.Delivery(delivery.DeliveryKind.TASK_NOTICE, delivery.Urgency.NORMAL,
                                   delivery.ChatKind.DIRECT)
        # 10:00 in Tokyo, 01:00 UTC: outside the quiet hours there, though inside them in UTC.
        morning = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)

        async def decide(key: str, chat: str = "ou_1", now: datetime = morning) -> delivery.Decision:
            return await delivery.decide(notice, dedupe_key=key, channel_id="channel-lark", chat=chat,
                                         last_user_message_at=None, now=now)

        sent = [await decide(f"notice:{n}") for n in range(5)]
        assert {(d.outcome, d.reason, d.counted) for d in sent} == {
            (delivery.Outcome.SEND, delivery.Reason.WITHIN_LIMITS, True)}
        assert await decide("notice:5") == delivery.Decision(delivery.Outcome.DIGEST, delivery.Reason.DAILY_CAP,
                                                             digest_date=date(2026, 11, 21))
        assert (await decide("notice:other", chat="ou_2")).outcome is delivery.Outcome.SEND  # another chat
        # Tried again at night, a message keeps its first decision, and no second row is written.
        night = datetime(2026, 11, 20, 14, 0, tzinfo=timezone.utc)  # 23:00 in Tokyo
        assert await decide("notice:0", now=night) == sent[0]
        assert (await decide("notice:6", now=night)).reason is delivery.Reason.QUIET_HOURS
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 8
        assert await pool.fetchval("SELECT DISTINCT local_date FROM proactive_deliveries") == date(2026, 11, 20)

    run_db(test, monkeypatch)


def test_an_unknown_user_timezone_falls_back_to_the_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents import cron

        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('user.timezone', 'Mars/Olympus')")
        assert (await delivery.load_policy()).timezone == cron.zone(cron.local_zone_name())
        await pool.execute("UPDATE system_settings SET value = 'Asia/Tokyo' WHERE key = 'user.timezone'")
        assert str((await delivery.load_policy()).timezone) == "Asia/Tokyo"

    run_db(test, monkeypatch)


def test_a_database_without_the_table_gets_it_on_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import connection

        fresh = await pool.fetch("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'proactive_deliveries' "
                                 "ORDER BY indexname")
        await pool.execute("DROP TABLE proactive_deliveries; "
                           "DELETE FROM schema_migrations WHERE name = 'proactive_deliveries'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetch("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'proactive_deliveries' "
                                "ORDER BY indexname") == fresh
        await connection.ensure_schema(pool)  # a second upgrade changes nothing

    run_db(test, monkeypatch, timeout=30)


def test_a_reminder_the_layer_cannot_decide_on_is_posted_as_with_the_switch_off(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_courier import GateCourier

        chat_settings.set_delivery_enabled(True)

        async def refused(*args: Any, **kwargs: Any) -> delivery.Decision:
            raise RuntimeError("the database refused the row")

        monkeypatch.setattr(delivery, "decide", refused)
        await _reminded(pool)
        poster = _Poster()
        await GateCourier(poster).run_once()
        assert [post["text"][:1] for post in poster.posts] == ["⏸", "⏰"]
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0

    run_db(test, monkeypatch)


def test_the_table_takes_every_value_the_code_has_and_keeps_its_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        import asyncpg

        insert = ("INSERT INTO proactive_deliveries (dedupe_key, kind, urgency, channel_id, chat, chat_kind, outcome, "
                  "reason, counted, local_date, digest_date, sent_at, replied_at) "
                  "VALUES ($1, $2, $3, 'channel-lark', 'ou_1', $4, $5, $6, $7, '2026-11-20', $8, $9, $10)")
        row = {"kind": "task_notice", "urgency": "normal", "chat": "direct", "outcome": "send",
               "reason": "within_limits", "counted": False, "digest": None, "sent": None, "replied": None}
        values = [*({"kind": k.value} for k in delivery.DeliveryKind),
                  *({"urgency": u.value} for u in delivery.Urgency), *({"chat": c.value} for c in delivery.ChatKind),
                  *({"reason": r.value} for r in delivery.Reason),
                  {"outcome": "digest", "digest": date(2026, 11, 21)}, {"outcome": "drop", "reason": "silent"},
                  {"counted": True}, {"sent": datetime.now(timezone.utc), "replied": datetime.now(timezone.utc)}]
        for n, change in enumerate(values):
            r = {**row, **change}
            await pool.execute(insert, f"ok:{n}", r["kind"], r["urgency"], r["chat"], r["outcome"], r["reason"],
                               r["counted"], r["digest"], r["sent"], r["replied"])
        refused = [{"kind": "heartbeat"}, {"reason": "because"}, {"outcome": "digest"},  # a digest needs its date
                   {"digest": date(2026, 11, 21)}, {"outcome": "drop", "counted": True},
                   {"outcome": "drop", "sent": datetime.now(timezone.utc)},
                   {"reason": "off", "counted": True}, {"reason": "off", "outcome": "drop"},
                   {"replied": datetime.now(timezone.utc)}]  # a reply to what never went
        for n, change in enumerate(refused):
            r = {**row, **change}
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(insert, f"bad:{n}", r["kind"], r["urgency"], r["chat"], r["outcome"], r["reason"],
                                   r["counted"], r["digest"], r["sent"], r["replied"])

    run_db(test, monkeypatch)
