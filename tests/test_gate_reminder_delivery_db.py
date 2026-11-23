"""Gate reminders through the delivery layer, on a real PostgreSQL: with ``delivery.enabled`` off nothing changes and
nothing is recorded; on, every reminder is decided on (blocking: always sent, never counted) in its
``proactive_deliveries`` row, under its post's key, once however often its post is tried, and the receipt follows;
the courier does what a decision says; ``decide`` counts a direct chat's day in ``user.timezone`` and keeps a
message's first decision.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any

import pytest

from looper.agents import chat_settings, proactive_delivery as delivery
from looper.agents.task_notice import NotifyTarget, TargetKind
from tests.db_support import DSN, run_db
from tests.test_gate_channels_db import LARK_GROUP, LARK_USER, _Poster, _say, _task_with_gate

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

@pytest.mark.parametrize("enabled", [False, True])
def test_needs_you_notice_keeps_its_post_and_is_blocking_only_when_on(monkeypatch, enabled):
    async def test(pool):
        from looper.agents.gate_courier import GateCourier

        chat_settings.set_delivery_enabled(enabled)
        night = datetime(2026, 11, 20, 3, tzinfo=timezone.utc)
        real_decide = delivery.decide
        monkeypatch.setattr(delivery, "decide", lambda *a, **kw: real_decide(*a, **kw, now=night))
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        await _task_with_gate(pool, origin="channel-lark", chat=LARK_USER, kind="user")
        poster = _Poster()
        await GateCourier(poster).run_once()
        assert poster.posts == [{
            "channel": "channel-lark", "target": NotifyTarget(TargetKind.USER, LARK_USER),
            "text": "⏸ 需要你决定 · settings file\nstorage\n1. Which database?（选项：PostgreSQL / SQLite）\n"
                    "直接回复这条消息作答。\n[/#tasks/task-a](/#tasks/task-a)\n"
                    "/gates 查看所有等你回答的关卡；/gate 编号 回答，/gate 编号 end 结束讨论",
            "reply_to": None, "key": "gate-1"}]
        rows = await pool.fetch("SELECT source, urgency, decision, counted, outcome FROM proactive_deliveries")
        assert [tuple(row) for row in rows] == ([('task_notice', 'blocking', 'send', False, 'sent')] if enabled else [])
    run_db(test, monkeypatch)


ROWS = ("SELECT source, source_ref, urgency, channel_id, chat, chat_kind, task_id, step_id, decision, reason, counted, "
        "digest_date, decided_at IS NOT NULL AS decided, outcome, external_id, sent_at IS NOT NULL AS sent "
        "FROM proactive_deliveries ORDER BY id")


@pytest.mark.parametrize("kind", ["updated", "resumed"])
def test_waiting_question_notices_remain_blocking(monkeypatch, kind):
    async def check(pool):
        from looper.agents.gate_courier import GateCourier

        chat_settings.set_delivery_enabled(True)
        await _task_with_gate(pool, origin="channel-lark", chat=LARK_USER, kind="user")
        poster = _Poster()
        courier = GateCourier(poster)
        await courier.run_once()
        await _say("task-a", "step-g1", kind, "Synthetic question update")
        await courier.run_once()
        assert len(poster.posts) == 2
        rows = await pool.fetch("SELECT source, urgency, decision, counted, outcome "
                                "FROM proactive_deliveries ORDER BY id")
        assert [tuple(row) for row in rows] == [('task_notice', 'blocking', 'send', False, 'sent')] * 2
    run_db(check, monkeypatch)


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
        rows = [dict(row) for row in await pool.fetch(ROWS.replace("ORDER BY id", "WHERE source = 'gate_reminder'"))]
        if not enabled:
            assert rows == []  # off: nothing is recorded
            return
        # The reminder is blocking, sent and not counted; the initial needs-you notice is tested above.
        assert rows == [{"source": "gate_reminder", "source_ref": "step-g1", "urgency": "blocking",
                         "channel_id": "channel-lark", "chat": LARK_USER, "chat_kind": "direct", "task_id": "task-a",
                         "step_id": "step-g1", "decision": "send", "reason": "urgent", "counted": False,
                         "digest_date": None, "decided": True, "outcome": "sent", "external_id": "om_102",
                         "sent": True}]
        # Under the key the post used: the channel's receipt, from the metrics too, lands on the same row.
        assert await pool.fetchval("SELECT dedupe_key FROM proactive_deliveries WHERE source = 'gate_reminder'") == (
            reminder["key"])

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
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        for n in range(5):
            await pool.execute(
                "INSERT INTO proactive_deliveries (dedupe_key, source, urgency, channel_id, chat, chat_kind, decision, "
                "reason, counted, local_date, decided_at) VALUES ($1, 'task_notice', 'normal', 'channel-lark', $2, "
                "'direct', 'send', 'within_limits', true, $3, now())", f"earlier:{n}", chat, night.date())
        real_decide = delivery.decide
        monkeypatch.setattr(delivery, "decide", lambda *args, **kwargs: real_decide(*args, **kwargs, now=night))
        poster = _Poster()
        await GateCourier(poster).run_once()
        assert poster.posts[1]["text"].startswith("⏰ 还在等你")
        row = dict(await pool.fetchrow(ROWS.replace("ORDER BY id", "WHERE source = 'gate_reminder'")))
        assert (row["chat"], row["chat_kind"], row["decision"], row["reason"], row["counted"], row["outcome"]) == (
            chat, "group" if group else "direct", "send", "urgent", False, "sent")

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
        sent = "SELECT decision, outcome, sent_at IS NOT NULL FROM proactive_deliveries WHERE source = 'gate_reminder'"
        assert [tuple(row) for row in await pool.fetch(sent)] == [("send", None, False)]
        await pool.execute("UPDATE task_user_gate_messages SET post_retry_at = now() WHERE kind = 'reminder'")
        await courier.run_once()
        assert [tuple(row) for row in await pool.fetch(sent)] == [("send", "sent", True)]
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
        real_deliver = delivery.deliver
        monkeypatch.setattr(delivery, "deliver", lambda item, *args: (
            decision if item.kind is delivery.DeliveryKind.GATE_REMINDER else real_deliver(item, *args)))
        await _reminded(pool)
        poster = _Poster()
        await GateCourier(poster).run_once()
        assert [post["text"].splitlines()[0] for post in poster.posts] == ["⏸ 需要你决定 · settings file"]
        assert [tuple(row) for row in await pool.fetch(
            "SELECT kind, post_state FROM task_user_gate_messages ORDER BY id")] == [
                ("opened", "sent"), ("reminder", "none")]
        assert [tuple(row) for row in await pool.fetch(
            "SELECT decision, reason, outcome, sent_at FROM proactive_deliveries WHERE source = 'gate_reminder'")] == [
                (outcome.value, decision.reason.value, None, None)]

    run_db(test, monkeypatch)


def test_decide_counts_a_direct_chats_day_in_the_users_timezone_and_keeps_a_first_decision(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        monkeypatch.setattr(chat_settings, "_timezone", "Asia/Tokyo")
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
    from looper.agents import turn_context

    monkeypatch.setattr(chat_settings, "_timezone", "Mars/Olympus")
    assert delivery.load_policy().timezone == turn_context.server_zone()
    monkeypatch.setattr(chat_settings, "_timezone", "Asia/Tokyo")
    assert str(delivery.load_policy().timezone) == "Asia/Tokyo"


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


def test_the_table_takes_every_value_of_the_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        insert = ("INSERT INTO proactive_deliveries (dedupe_key, source, urgency, channel_id, chat, chat_kind, decision, "
                  "reason, counted, local_date, digest_date, decided_at) "
                  "VALUES ($1, $2, $3, 'channel-lark', 'ou_1', $4, $5, $6, false, '2026-11-20', NULL, now())")
        row = {"source": "task_notice", "urgency": "normal", "chat": "direct", "decision": "send",
               "reason": "within_limits"}
        values = [*({"source": k.value} for k in delivery.DeliveryKind),
                  *({"urgency": u.value} for u in delivery.Urgency), *({"chat": c.value} for c in delivery.ChatKind),
                  *({"decision": o.value} for o in delivery.Outcome), *({"reason": r.value} for r in delivery.Reason)]
        for n, change in enumerate(values):
            r = {**row, **change}
            await pool.execute(insert, f"ok:{n}", r["source"], r["urgency"], r["chat"], r["decision"], r["reason"])

    run_db(test, monkeypatch)


NOTICE = delivery.Delivery(delivery.DeliveryKind.TASK_NOTICE, delivery.Urgency.NORMAL, delivery.ChatKind.DIRECT)
AFTERNOON = datetime(2026, 11, 20, 14, 0, tzinfo=timezone.utc)  # 14:00 in UTC, the zone these tests set


async def _decide(key: str, now: datetime = AFTERNOON, chat: str = "ou_1") -> delivery.Decision:
    return await delivery.decide(NOTICE, dedupe_key=key, channel_id="channel-lark", chat=chat,
                                 last_user_message_at=None, now=now)


def test_send_points_racing_for_the_last_message_of_the_day_get_one(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        for round_ in range(5):  # several chats: a missing lock would let two through at least once
            chat = f"ou_{round_}"
            for n in range(4):
                await _decide(f"{chat}:fill:{n}", chat=chat)
            results = await asyncio.gather(*(_decide(f"{chat}:last:{n}", chat=chat) for n in range(3)))
            assert sorted(d.reason.value for d in results) == ["daily_cap", "daily_cap", "within_limits"], chat
            assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries WHERE chat = $1 AND counted",
                                       chat) == 5

    run_db(test, monkeypatch, max_size=6)


def test_one_message_decided_on_twice_at_once_gets_one_row_and_one_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        night = datetime(2026, 11, 20, 23, 0, tzinfo=timezone.utc)
        decisions = await asyncio.gather(_decide("same", AFTERNOON), _decide("same", night), _decide("same", night))
        assert len(set(decisions)) == 1  # whichever was first: the others keep its decision
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries WHERE dedupe_key = 'same'") == 1

    run_db(test, monkeypatch, max_size=6)


def test_the_daily_count_starts_again_at_local_midnight(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        monkeypatch.setattr(chat_settings, "_timezone", "Asia/Tokyo")
        # 21:00 in Tokyo on the 19th (12:00 UTC): five counted messages that day.
        evening = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
        for n in range(5):
            assert (await _decide(f"day1:{n}", evening)).counted
        assert (await _decide("day1:late", evening)).reason is delivery.Reason.DAILY_CAP
        # 09:00 in Tokyo on the 20th, still the 19th in UTC: a new local day, with room again.
        morning = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)
        assert await _decide("day2:0", morning) == delivery.Decision(delivery.Outcome.SEND,
                                                                     delivery.Reason.WITHIN_LIMITS, counted=True)

    run_db(test, monkeypatch)


def test_without_a_user_timezone_the_servers_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_settings, "_timezone", None)
    monkeypatch.setenv("TZ", "Asia/Tokyo")  # the server's own zone, as the turn context reads it
    assert str(delivery.load_policy().timezone) == "Asia/Tokyo"


def test_the_metrics_receipt_lands_on_the_decided_row_and_keeps_the_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import proactive_deliveries

        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        decided = await _decide("gate-7")
        # The channel's receipt as the metrics record it after the send, under the same key.
        await proactive_deliveries.insert({"source": "task_notice", "channel_id": "channel-lark", "chat": "ou_1",
                                           "chat_kind": "direct", "external_id": "om_9", "outcome": "sent",
                                           "dedupe_key": "gate-7"})
        await proactive_deliveries.record_receipt("gate-7", "sent", "om_other")  # the send point's: the first stands
        row = dict(await pool.fetchrow("SELECT * FROM proactive_deliveries"))
        assert (row["decision"], row["reason"], row["counted"], row["outcome"], row["external_id"]) == (
            "send", "within_limits", True, "sent", "om_9")
        assert row["decided_at"] is not None and row["sent_at"] is not None
        assert await _decide("gate-7") == decided  # tried again: the first decision
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 1

    run_db(test, monkeypatch)


def test_a_row_the_metrics_recorded_before_the_layer_was_on_gets_its_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        from looper.db import proactive_deliveries

        monkeypatch.setattr(chat_settings, "_timezone", "UTC")
        await proactive_deliveries.insert({"source": "task_notice", "channel_id": "channel-lark", "chat": "ou_1",
                                           "chat_kind": "direct", "external_id": "om_9", "outcome": "sent",
                                           "dedupe_key": "notice-1"})
        assert (await _decide("notice-1")).outcome is delivery.Outcome.SEND
        row = dict(await pool.fetchrow("SELECT * FROM proactive_deliveries"))
        assert (row["decision"], row["outcome"], row["external_id"]) == ("send", "sent", "om_9")
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 1

    run_db(test, monkeypatch)
