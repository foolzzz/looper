"""The morning brief on PostgreSQL: one workday message to the operator's direct chat, nothing when every section
is empty, and at most once across a restart and a second server.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from looper.agents import attention, chat_settings, morning_brief, proactive_delivery, turn_metrics
from looper.agents.channels.base import PostOutcome, PostReceipt
from looper.db import connection, reminders as reminder_store
from tests.db_support import DSN, run_db, run_with_channel_configs

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

ZONE = ZoneInfo("America/Los_Angeles")
OP = "ou_operatorsynthetic"
TG = "424242"
MONDAY = datetime(2026, 11, 23, 9, 15, tzinfo=ZONE)
FRIDAY = datetime(2026, 11, 20, 9, 15, tzinfo=ZONE)


class _Channel:
    def __init__(self, *outcomes: PostOutcome) -> None:
        self.outcomes, self.posts = list(outcomes), []

    async def post(self, channel_id: str, target: Any, text: str, *, reply_to: str | None = None,
                   idempotency_key: str | None = None, proactive: Any = None) -> PostReceipt:
        self.posts.append({"channel_id": channel_id, "kind": target.kind.value, "to": target.id, "text": text,
                           "key": idempotency_key, "proactive": proactive})
        outcome = self.outcomes.pop(0) if self.outcomes else PostOutcome.SENT
        if outcome is PostOutcome.SENT:
            return PostReceipt(True, external_ids=(f"om_{len(self.posts)}",))
        return PostReceipt(False, error=f"the channel answered {outcome.value}", outcome=outcome)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _summary(state: str, error: str | None = None) -> dict[str, Any]:
    return {"read_at": MONDAY, "total": 0, "sections": [
        {"kind": "gates", "state": state, "count": 0, "items": [], "error": error}]}


def _collect(summary: dict[str, Any], seen: list[Any] | None = None):
    async def collect(*, managers: Any = None) -> dict[str, Any]:
        if seen is not None:
            seen.append(managers)
        return summary
    return collect


async def _zone(pool: Any) -> None:
    await pool.execute("INSERT INTO system_settings (key, value) VALUES ('user.timezone', 'America/Los_Angeles') "
                       "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    await chat_settings.load()  # deliver() reads the zone from turn-context memory, not the row directly


async def _operator(pool: Any, channel_id: str = "channel-lark", who: str = OP, *, enabled: bool = True) -> None:
    await pool.execute("UPDATE channel_configs SET enabled = $2, config = $3 WHERE channel_id = $1", channel_id,
                       enabled, json.dumps({"operator_sender_id": who}))


async def _reminder(text: str, due_at: datetime, *, chat: str = OP, channel_id: str = "channel-lark",
                    status: str = "pending") -> None:
    row = await reminder_store.create(text=text, due_at=due_at, timezone="America/Los_Angeles",
                                      channel_id=channel_id, chat=chat, created_channel="lark",
                                      created_message_id="m-1")
    if status != "pending":
        from looper.db import connection as db
        await db.get_pool().execute(
            "UPDATE reminders SET status = $2, settled_at = now() WHERE id = $1", row["id"], status)


async def _digest(pool: Any, key: str, day: date, *, channel: str = "channel-lark", chat: str = OP,
                  kind: str = "task_notice", reason: str = "quiet_hours", hour: int = 23, minute: int = 0,
                  schedule_id: str | None = None) -> int:
    when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)
    return await pool.fetchval(
        "INSERT INTO proactive_deliveries (dedupe_key, source, urgency, channel_id, chat, chat_kind, decision, "
        "reason, counted, local_date, digest_date, decided_at, schedule_id) "
        "VALUES ($1, $2, 'normal', $3, $4, 'direct', 'digest', $5, false, $6, $6, $7, $8) RETURNING id",
        key, kind, channel, chat, reason, day, when, schedule_id)


async def _schedule(pool: Any, schedule_id: str, name: str, project_id: str, failures: int) -> None:
    await pool.execute(
        "INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, consecutive_failures) "
        "VALUES ($1, $2, $3, '0 9 * * *', 'UTC', 'Check it', $4)",
        schedule_id, project_id, name, failures)


def _service(channel: _Channel, when: datetime, *, managers: Any = None) -> morning_brief.MorningBriefService:
    chat_settings.set_morning_brief_enabled(True)
    return morning_brief.MorningBriefService(channel, clock=_Clock(when), managers=managers)


async def _rows(pool: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in await pool.fetch(
        "SELECT local_date, channel_id, chat, status, attempts FROM morning_briefs ORDER BY channel_id, chat")]


@pytest.mark.parametrize(("outbound", "failure"), [(False, None), (False, "validation"), (False, "not_started"),
                                                (True, None), (True, "validation")])
def test_lark_brief_long_key_and_pre_send_retry(monkeypatch, tmp_path, lark_cli_stand_in, failure, outbound) -> None:
    from looper.agents.bus import MessageBus
    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.channels.registry import ChannelRegistry

    async def test(pool: Any) -> None:
        await _zone(pool)
        monkeypatch.setattr(chat_settings, "_outbound_enabled", outbound)
        channel_id, chat = "channel-lark-123456789", "ou_" + "a" * 32
        await pool.execute("UPDATE channel_configs SET channel_id = $1 WHERE channel_id = 'channel-lark'", channel_id)
        await _operator(pool, channel_id, chat)
        await _digest(pool, "deferred", MONDAY.date(), channel=channel_id, chat=chat, hour=7)
        digest_id = await pool.fetchval("SELECT id FROM proactive_deliveries WHERE dedupe_key = 'deferred'")
        monkeypatch.setattr(attention, "collect", _collect(_summary("ok")))
        bus = MessageBus(runtime_root=tmp_path)
        registry = ChannelRegistry(bus)
        adapter = LarkChannelAdapter(runtime_root=tmp_path, channel_id=channel_id,
                                     allowlist=SenderAllowlist(channel_id, frozenset({chat})))
        registry._channels[channel_id] = adapter
        service = _service(registry, MONDAY)
        again = None
        try:
            if failure:
                with monkeypatch.context() as patch:
                    if failure == "validation":
                        patch.setattr(morning_brief, "_key", lambda *args: "")
                    else:
                        patch.setenv("PATH", str(tmp_path / "no-executable"))
                    await service.run_once()
                assert (await _rows(pool))[0]["status"] == "failed"
                assert await pool.fetchval("SELECT read_ids FROM morning_briefs") == []
                assert await pool.fetchval("SELECT count(*) FROM morning_brief_consumed") == 0
                assert not lark_cli_stand_in.exists()
            await service.run_once()
            await service.run_once()
            row, = await _rows(pool)
            assert (row["status"], row["attempts"]) == ("sent", 2 if failure else 1)
            assert await pool.fetchval("SELECT read_ids FROM morning_briefs") == [digest_id]
            assert await pool.fetchval("SELECT delivery_id FROM morning_brief_consumed") == digest_id
            calls = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
            send, = [call for call in calls if "+messages-send" in call]
            key = send[send.index("--idempotency-key") + 1]
            assert 0 < len(key) <= 50
            # A second adapter reuses the wire key (or the durable outbox row) without another delivery.
            again = LarkChannelAdapter(runtime_root=tmp_path, channel_id=channel_id,
                                      allowlist=SenderAllowlist(channel_id, frozenset({chat})))
            from looper.agents.reminders import target
            receipt = await again.post(target(chat), "same brief", idempotency_key=morning_brief._key(
                MONDAY.date(), channel_id, chat))
            assert receipt.success
            calls = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
            assert [c[c.index("--idempotency-key") + 1] for c in calls] == [key] * (1 if outbound else 2)
            delivered = Path(str(lark_cli_stand_in) + ".delivered.jsonl").read_text().splitlines()
            assert len(delivered) == 1
        finally:
            await adapter._channel_outbox.stop()
            if again is not None:
                await again._channel_outbox.stop()
            await bus.shutdown()

    run_with_channel_configs(test, monkeypatch)


@pytest.mark.parametrize("earlier_attempt", [False, True])
def test_skipped_day_digests_are_included_only_after_the_first_attempt(monkeypatch, earlier_attempt) -> None:
    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "synthetic unavailable")))
        channel = _Channel()
        tuesday = MONDAY + timedelta(days=1)
        wednesday = MONDAY + timedelta(days=2)
        if earlier_attempt:
            await _service(channel, MONDAY).run_once()
        await _digest(pool, "before-window", date(2026, 11, 20), hour=7)
        await _digest(pool, "skipped-day", tuesday.date(), hour=7)
        await _digest(pool, "today", wednesday.date(), hour=7)
        service = _service(channel, tuesday)
        chat_settings.set_morning_brief_enabled(False)
        await service.run_once()
        assert not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM morning_briefs WHERE local_date = $1)",
                                      tuesday.date())
        await _service(channel, wednesday).run_once()
        sent = channel.posts[-1]["text"]
        assert ("11-24 07:00" in sent) is earlier_attempt
        assert "11-25 07:00" in sent and "11-20 07:00" not in sent
        consumed = await pool.fetch(
            "SELECT dedupe_key FROM proactive_deliveries JOIN morning_brief_consumed ON delivery_id = id")
        assert {r["dedupe_key"] for r in consumed} == ({"skipped-day", "today"} if earlier_attempt else {"today"})

    run_with_channel_configs(test, monkeypatch)


def test_nothing_to_say_sends_nothing_and_is_not_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("ok")))
        calls: list[Any] = []
        real_deliver = proactive_delivery.deliver

        def deliver(delivery, policy, state):
            calls.append(delivery)
            return real_deliver(delivery, policy, state)

        monkeypatch.setattr(proactive_delivery, "deliver", deliver)
        channel = _Channel()
        await _service(channel, MONDAY).run_once()
        await _service(channel, MONDAY).run_once()
        assert channel.posts == []
        assert [call.silent for call in calls] == [True]
        assert await _rows(pool) == [{"local_date": MONDAY.date(), "channel_id": "channel-lark", "chat": OP,
                                      "status": "skipped", "attempts": 0}]
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries WHERE source = 'morning_brief'") == 0

    run_with_channel_configs(test, monkeypatch)


def test_the_brief_names_what_waits_what_is_due_today_and_what_was_deferred(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _zone(pool)
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('attention.enabled', 'false')")
        await _operator(pool)
        await _operator(pool, "channel-telegram", TG)
        await _operator(pool, "channel-lark_bot", OP, enabled=False)
        seen: list[Any] = []
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down"), seen))
        await _reminder("看 PR", datetime(2026, 11, 23, 15, 0, tzinfo=ZONE))
        await _reminder("明天的", datetime(2026, 11, 24, 9, 0, tzinfo=ZONE))
        await _reminder("别人的", datetime(2026, 11, 23, 15, 0, tzinfo=ZONE), chat="ou_someoneelse")
        await _reminder("已送出的", datetime(2026, 11, 23, 8, 0, tzinfo=ZONE), status="delivered")
        await _digest(pool, "sat", date(2026, 11, 21))
        await _digest(pool, "sun", date(2026, 11, 22), hour=3)
        await _digest(pool, "mon", date(2026, 11, 23), hour=7, reason="daily_cap")
        await _digest(pool, "fri", date(2026, 11, 20))
        await _digest(pool, "self", date(2026, 11, 23), kind="morning_brief", hour=1)
        await _digest(pool, "other-chat", date(2026, 11, 23), chat="ou_someoneelse", hour=6)
        sentinel = object()
        channel = _Channel()
        service = _service(channel, MONDAY, managers=sentinel)
        await service.run_once()
        await service.run_once()
        assert seen == [sentinel]
        by_channel = {post["channel_id"]: post for post in channel.posts}
        assert set(by_channel) == {"channel-lark", "channel-telegram"}
        lark, telegram = by_channel["channel-lark"], by_channel["channel-telegram"]
        assert lark["to"] == OP and lark["kind"] == "user"
        assert telegram["to"] == TG and telegram["kind"] == "chat"
        assert "晨报 · 11-23（周一）" in lark["text"]
        assert "等你回复的关卡：没能读取（db down）" in lark["text"]
        assert "今天的提醒\n- 15:00 看 PR" in lark["text"]
        assert "11-21 23:00" in lark["text"] and "11-22 03:00" in lark["text"] and "11-23 07:00" in lark["text"]
        for absent in ("明天的", "别人的", "已送出的", "11-20 23:00", "11-23 01:00", "11-23 06:00", "morning_brief"):
            assert absent not in lark["text"]
        assert "没能读取" in telegram["text"] and "看 PR" not in telegram["text"]
        assert "今天的提醒" not in telegram["text"]
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries WHERE source = 'morning_brief' "
                                   "AND decision = 'send'") == 0

    run_with_channel_configs(test, monkeypatch)


def test_a_friday_brief_does_not_take_thursdays_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("ok")))
        await _digest(pool, "thu", date(2026, 11, 19), hour=23)
        await _digest(pool, "fri", date(2026, 11, 20), hour=7, reason="outside_active_hours")
        channel = _Channel()
        await _service(channel, FRIDAY).run_once()
        (post,) = channel.posts
        assert "晨报 · 11-20（周五）" in post["text"]
        assert "不在生效时段，11-20 07:00" in post["text"]
        assert "11-19" not in post["text"]
        assert "需要你处理" not in post["text"] and "今天的提醒" not in post["text"]

    run_with_channel_configs(test, monkeypatch)


def test_before_the_time_and_on_a_weekend_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)

        async def collect(*, managers: Any = None) -> dict[str, Any]:
            raise AssertionError("not due")

        monkeypatch.setattr(attention, "collect", collect)
        channel = _Channel()
        await _service(channel, MONDAY - timedelta(minutes=1)).run_once()
        await _service(channel, datetime(2026, 11, 22, 12, 0, tzinfo=ZONE)).run_once()
        chat_settings.set_morning_brief_time("15:00")
        await _service(channel, datetime(2026, 11, 23, 14, 59, tzinfo=ZONE)).run_once()
        assert channel.posts == [] and await _rows(pool) == []
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        await _service(channel, datetime(2026, 11, 23, 15, 0, tzinfo=ZONE)).run_once()
        assert len(channel.posts) == 1 and channel.posts[0]["text"].startswith("晨报 · 11-23（周一）")

    run_with_channel_configs(test, monkeypatch)


def test_no_operator_chat_is_one_skip_and_no_post(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _zone(pool)
        calls: list[str] = []
        monkeypatch.setattr(proactive_delivery, "deliver", lambda *args, **kwargs: calls.append("deliver"))
        channel = _Channel()
        await _service(channel, MONDAY).run_once()
        await _service(channel, MONDAY).run_once()
        assert channel.posts == [] and calls == []
        assert await _rows(pool) == [{"local_date": MONDAY.date(), "channel_id": "", "chat": "",
                                      "status": "skipped", "attempts": 0}]

    run_with_channel_configs(test, monkeypatch)


def test_the_second_server_waits_and_neither_sends_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        channel = _Channel()
        service = _service(channel, MONDAY)
        async with pool.acquire() as holder:
            assert await holder.fetchval("SELECT pg_try_advisory_lock($1::int, $2::int)", *morning_brief._LOCK)
            await service.run_once()
            assert channel.posts == []
            await holder.execute("SELECT pg_advisory_unlock($1::int, $2::int)", *morning_brief._LOCK)
        await service.run_once()
        await service.run_once()
        assert len(channel.posts) == 1

    run_with_channel_configs(test, monkeypatch, )


def test_a_restart_mid_send_does_not_send_again(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        await pool.execute("INSERT INTO morning_briefs (local_date, channel_id, chat, status, attempts) "
                           "VALUES ($1, 'channel-lark', $2, 'sending', 1)", MONDAY.date(), OP)
        channel = _Channel()
        await _service(channel, MONDAY).run_once()
        await _service(channel, MONDAY).run_once()
        assert channel.posts == []
        assert await _rows(pool) == [{"local_date": MONDAY.date(), "channel_id": "channel-lark", "chat": OP,
                                      "status": "uncertain", "attempts": 1}]

    run_with_channel_configs(test, monkeypatch)


def test_a_refusal_is_retried_and_a_lost_answer_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        channel = _Channel(PostOutcome.REJECTED, PostOutcome.REJECTED, PostOutcome.REJECTED, PostOutcome.SENT)
        service = _service(channel, MONDAY)
        for _ in range(4):
            await service.run_once()
        assert len(channel.posts) == 3
        (row,) = await _rows(pool)
        assert row["status"] == "failed" and row["attempts"] == 3

    async def lost(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        channel = _Channel(PostOutcome.UNKNOWN, PostOutcome.SENT)
        service = _service(channel, MONDAY)
        await service.run_once()
        await service.run_once()
        assert len(channel.posts) == 1
        (row,) = await _rows(pool)
        assert row["status"] == "uncertain" and row["attempts"] == 1

    run_with_channel_configs(refused, monkeypatch)
    run_with_channel_configs(lost, monkeypatch)


def test_delivery_on_records_an_uncounted_send_and_off_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        channel = _Channel()
        await _service(channel, MONDAY).run_once()
        assert len(channel.posts) == 1
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries WHERE source = 'morning_brief'") == 0
        await pool.execute("DELETE FROM morning_briefs")
        chat_settings.set_delivery_enabled(True)
        later = MONDAY + timedelta(days=1)  # Tuesday, its own row
        await _service(channel, later).run_once()
        row = await pool.fetchrow("SELECT decision, reason, counted, dedupe_key FROM proactive_deliveries "
                                  "WHERE source = 'morning_brief'")
        assert dict(row) == {"decision": "send", "reason": "within_limits", "counted": False,
                             "dedupe_key": f"morning_brief:{later.date().isoformat()}:channel-lark:{OP}"}
        assert len(channel.posts) == 2

    run_with_channel_configs(test, monkeypatch)


def test_a_change_while_collection_is_awaited_is_rechecked_before_sending(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The switch, the channel and the operator are read again after ``attention.collect``. Off, or a disabled
    channel, sends nothing and does not consume the day. A new operator is who receives it."""

    async def switched_off(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)

        async def collect(*, managers: Any = None) -> dict[str, Any]:
            chat_settings.set_morning_brief_enabled(False)
            return _summary("error", "db down")

        monkeypatch.setattr(attention, "collect", collect)
        channel = _Channel()
        await _service(channel, MONDAY).run_once()
        assert channel.posts == []
        assert await _rows(pool) == []
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        await _service(channel, MONDAY).run_once()
        assert len(channel.posts) == 1 and channel.posts[0]["to"] == OP

    async def channel_disabled(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)

        async def collect(*, managers: Any = None) -> dict[str, Any]:
            await pool.execute("UPDATE channel_configs SET enabled = false WHERE channel_id = 'channel-lark'")
            return _summary("error", "db down")

        monkeypatch.setattr(attention, "collect", collect)
        channel = _Channel()
        await _service(channel, MONDAY).run_once()
        assert channel.posts == []
        assert await _rows(pool) == []
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        await _service(channel, MONDAY).run_once()
        assert [post["to"] for post in channel.posts] == [OP]

    async def operator_changed(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        replacement = "ou_replacementoperator"

        async def collect(*, managers: Any = None) -> dict[str, Any]:
            await pool.execute("UPDATE channel_configs SET config = $2 WHERE channel_id = $1", "channel-lark",
                               json.dumps({"operator_sender_id": replacement}))
            return _summary("error", "db down")

        monkeypatch.setattr(attention, "collect", collect)
        channel = _Channel()
        await _service(channel, MONDAY).run_once()
        assert [post["to"] for post in channel.posts] == [replacement]

    run_with_channel_configs(switched_off, monkeypatch)
    run_with_channel_configs(channel_disabled, monkeypatch)
    run_with_channel_configs(operator_changed, monkeypatch)


def test_turning_the_brief_off_during_the_last_operator_check_drops_the_claim(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The switch is read again after ``operator_targets`` returns. Turning it off during that wait, once the day
    is claimed, sends nothing and deletes the ``sending`` row, so a later tick the same day still sends."""

    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        claimed = False
        flips = 0
        real_claim = morning_brief.store.claim
        real_targets = morning_brief.operator_targets

        async def claim(conn: Any, local_date: date, channel_id: str, chat: str, now: datetime, *,
                        attempts_limit: int, read_ids: list[int] | None = None,
                        triage_ids: list[int] | None = None) -> bool:
            nonlocal claimed
            ok = await real_claim(conn, local_date, channel_id, chat, now, attempts_limit=attempts_limit,
                                  read_ids=read_ids, triage_ids=triage_ids)
            claimed = claimed or bool(ok)
            return ok

        async def targets() -> list[tuple[str, str]]:
            nonlocal flips
            found = await real_targets()
            if claimed:
                flips += 1
                chat_settings.set_morning_brief_enabled(False)
            return found

        monkeypatch.setattr(morning_brief.store, "claim", claim)
        monkeypatch.setattr(morning_brief, "operator_targets", targets)
        channel = _Channel()
        service = _service(channel, MONDAY)
        await service.run_once()
        assert flips == 1
        assert channel.posts == []
        assert await _rows(pool) == []
        monkeypatch.setattr(morning_brief.store, "claim", real_claim)
        monkeypatch.setattr(morning_brief, "operator_targets", real_targets)
        chat_settings.set_morning_brief_enabled(True)
        await service.run_once()
        assert len(channel.posts) == 1 and channel.posts[0]["to"] == OP

    run_with_channel_configs(test, monkeypatch)


def test_a_refused_retry_switched_off_keeps_that_days_digest_for_the_next(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Tuesday's first brief is refused. The same day's retry claims the row, then the switch goes off during the
    last operator check. That claim is put back as the refusal, attempt count included, so Wednesday still reads
    Tuesday's digest: the one the refusal already read, and the one held after it."""

    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        chat_settings.set_morning_brief_time("07:00")
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        tuesday = datetime(2026, 11, 24, 7, 0, tzinfo=ZONE)
        await _digest(pool, "early", tuesday.date(), hour=6, minute=30)
        channel = _Channel(PostOutcome.REJECTED)
        service = _service(channel, tuesday)
        await service.run_once()
        assert "11-24 06:30" in channel.posts[0]["text"]
        refused = {"status": "failed", "attempts": 1, "detail": "the channel answered rejected",
                   "consumed_until": None}
        assert dict(await pool.fetchrow(
            "SELECT status, attempts, detail, consumed_until FROM morning_briefs")) == refused

        taken = False
        real_claim = morning_brief.store.claim
        real_targets = morning_brief.operator_targets

        async def claim(conn: Any, local_date: date, channel_id: str, chat: str, now: datetime, *,
                        attempts_limit: int, read_ids: list[int] | None = None,
                        triage_ids: list[int] | None = None) -> bool:
            nonlocal taken
            ok = await real_claim(conn, local_date, channel_id, chat, now, attempts_limit=attempts_limit,
                                  read_ids=read_ids, triage_ids=triage_ids)
            taken = taken or bool(ok)
            return ok

        async def targets() -> list[tuple[str, str]]:
            found = await real_targets()
            if taken:
                chat_settings.set_morning_brief_enabled(False)
            return found

        monkeypatch.setattr(morning_brief.store, "claim", claim)
        monkeypatch.setattr(morning_brief, "operator_targets", targets)
        await service.run_once()
        assert taken
        assert len(channel.posts) == 1
        row = await pool.fetchrow("SELECT status, attempts, detail, consumed_until FROM morning_briefs")
        assert row is not None and dict(row) == refused
        assert await pool.fetchval("SELECT count(*) FROM morning_briefs") == 1

        monkeypatch.setattr(morning_brief.store, "claim", real_claim)
        monkeypatch.setattr(morning_brief, "operator_targets", real_targets)
        await _digest(pool, "held", tuesday.date(), hour=7, minute=30)
        wednesday = datetime(2026, 11, 25, 7, 0, tzinfo=ZONE)
        await _service(channel, wednesday).run_once()
        assert "11-24 06:30" in channel.posts[1]["text"]
        assert "11-24 07:30" in channel.posts[1]["text"]

    run_with_channel_configs(test, monkeypatch)


def test_a_message_held_after_an_early_brief_is_read_by_a_later_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tuesday's brief is 07:00. A 07:30 message quiet hours put in Tuesday's digest is still unread on Thursday
    when Wednesday did not run, and Friday does not repeat it."""

    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        chat_settings.set_morning_brief_time("07:00")
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        tuesday = datetime(2026, 11, 24, 7, 0, tzinfo=ZONE)
        await _digest(pool, "before", tuesday.date(), hour=6)
        channel = _Channel()
        await _service(channel, tuesday).run_once()
        assert "11-24 06:00" in channel.posts[0]["text"]
        await _digest(pool, "after", tuesday.date(), hour=7, minute=30)
        thursday = datetime(2026, 11, 26, 7, 0, tzinfo=ZONE)
        await _service(channel, thursday).run_once()
        assert "11-24 07:30" in channel.posts[1]["text"]
        assert "11-24 06:00" not in channel.posts[1]["text"]
        friday = datetime(2026, 11, 27, 7, 0, tzinfo=ZONE)
        await _service(channel, friday).run_once()
        assert "07:30" not in channel.posts[2]["text"]

    run_with_channel_configs(test, monkeypatch)


def test_a_rejected_first_brief_keeps_that_days_digest_on_the_next(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Tuesday is the first brief and the channel rejects it, so no boundary is recorded. Wednesday still includes
    Tuesday's digest, the one the refusal already read and the one held after it. Thursday does not repeat them."""

    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        chat_settings.set_morning_brief_time("07:00")
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        tuesday = datetime(2026, 11, 24, 7, 0, tzinfo=ZONE)
        await _digest(pool, "early", tuesday.date(), hour=6, minute=30)
        channel = _Channel(PostOutcome.REJECTED, PostOutcome.SENT)
        await _service(channel, tuesday).run_once()
        assert "11-24 06:30" in channel.posts[0]["text"]
        assert await pool.fetchval("SELECT consumed_until FROM morning_briefs") is None
        assert await pool.fetchval("SELECT count(*) FROM morning_brief_consumed") == 0
        assert list(await pool.fetchval("SELECT read_ids FROM morning_briefs")) == []
        await _digest(pool, "held", tuesday.date(), hour=7, minute=30)
        wednesday = datetime(2026, 11, 25, 7, 0, tzinfo=ZONE)
        await _service(channel, wednesday).run_once()
        assert "11-24 06:30" in channel.posts[1]["text"]
        assert "11-24 07:30" in channel.posts[1]["text"]
        thursday = datetime(2026, 11, 26, 7, 0, tzinfo=ZONE)
        await _service(channel, thursday).run_once()
        assert "11-24" not in channel.posts[2]["text"]

    run_with_channel_configs(test, monkeypatch)


def test_a_digest_committed_after_the_brief_read_it_is_in_the_next_brief_once(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A digest row inserted in a transaction the brief cannot see is not part of what that brief records.
    After the producer commits, the next brief includes it, and the one after does not repeat it. The row's
    ``decided_at`` is before the check time the brief stored."""

    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        tuesday = datetime(2026, 11, 24, 9, 15, tzinfo=ZONE)
        await _digest(pool, "seen", tuesday.date(), hour=9, minute=0)
        late_at = tuesday.replace(minute=14, second=59)
        channel = _Channel()
        async with pool.acquire() as producer:
            async with producer.transaction():
                await producer.execute(
                    "INSERT INTO proactive_deliveries (dedupe_key, source, urgency, channel_id, chat, chat_kind, "
                    "decision, reason, counted, local_date, digest_date, decided_at) "
                    "VALUES ('late', 'task_notice', 'normal', 'channel-lark', $1, 'direct', 'digest', "
                    "'quiet_hours', false, $2, $2, $3)",
                    OP, tuesday.date(), late_at)
                await _service(channel, tuesday).run_once()
                assert "11-24 09:00" in channel.posts[0]["text"]
                assert "11-24 09:14" not in channel.posts[0]["text"]
                assert await pool.fetchval("SELECT count(*) FROM morning_brief_consumed") == 1
        assert await pool.fetchval("SELECT consumed_until FROM morning_briefs") > late_at
        wednesday = datetime(2026, 11, 25, 9, 15, tzinfo=ZONE)
        await _service(channel, wednesday).run_once()
        assert "11-24 09:14" in channel.posts[1]["text"]
        assert "11-24 09:00" not in channel.posts[1]["text"]
        thursday = datetime(2026, 11, 26, 9, 15, tzinfo=ZONE)
        await _service(channel, thursday).run_once()
        assert len(channel.posts) == 3
        assert "09:14" not in channel.posts[2]["text"] and "09:00" not in channel.posts[2]["text"]
        assert sum("11-24 09:14" in post["text"] for post in channel.posts) == 1

    run_with_channel_configs(test, monkeypatch)


def test_a_digest_committed_after_the_send_is_not_consumed_when_the_sender_stops(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The producer has not committed when the brief reads, claims and sends. The producer then commits and the
    process exits before settle. Restart recovery consumes only the ids stored with the claim, so the next brief
    includes the producer's row once and the brief after that does not repeat it. The row's ``decided_at`` is at
    or before the claim, which is the scan recovery must not do."""

    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        tuesday = datetime(2026, 11, 24, 9, 15, tzinfo=ZONE)
        await _digest(pool, "seen", tuesday.date(), hour=9, minute=0)
        late_at = tuesday.replace(minute=14, second=59)
        channel = _Channel()
        service = _service(channel, tuesday)
        reached = asyncio.Event()
        committed = asyncio.Event()
        real_settle = morning_brief.store.settle

        async def stop_before_settle(*args: Any, **kwargs: Any) -> bool:
            reached.set()
            await committed.wait()
            raise RuntimeError("sender stopped before settle")

        monkeypatch.setattr(morning_brief.store, "settle", stop_before_settle)
        task: asyncio.Task[None] | None = None
        try:
            async with pool.acquire() as producer:
                async with producer.transaction():
                    await producer.execute(
                        "INSERT INTO proactive_deliveries (dedupe_key, source, urgency, channel_id, chat, "
                        "chat_kind, decision, reason, counted, local_date, digest_date, decided_at) "
                        "VALUES ('late', 'task_notice', 'normal', 'channel-lark', $1, 'direct', 'digest', "
                        "'quiet_hours', false, $2, $2, $3)",
                        OP, tuesday.date(), late_at)
                    task = asyncio.create_task(service.run_once())
                    await asyncio.wait_for(reached.wait(), 10)
            committed.set()
            with pytest.raises(RuntimeError, match="sender stopped"):
                await task
        finally:
            committed.set()
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, RuntimeError):
                    pass
        monkeypatch.setattr(morning_brief.store, "settle", real_settle)

        assert "11-24 09:00" in channel.posts[0]["text"]
        assert "09:14" not in channel.posts[0]["text"]
        claim = await pool.fetchrow("SELECT status, updated_at, read_ids FROM morning_briefs")
        assert claim["status"] == "sending"
        assert late_at <= claim["updated_at"]
        seen_id = await pool.fetchval("SELECT id FROM proactive_deliveries WHERE dedupe_key = 'seen'")
        late_id = await pool.fetchval("SELECT id FROM proactive_deliveries WHERE dedupe_key = 'late'")
        assert list(claim["read_ids"]) == [seen_id]
        assert await pool.fetchval("SELECT count(*) FROM morning_brief_consumed") == 0

        await _service(channel, tuesday).run_once()
        assert len(channel.posts) == 1
        assert await pool.fetchval("SELECT status FROM morning_briefs") == "uncertain"
        consumed = await pool.fetch("SELECT delivery_id FROM morning_brief_consumed ORDER BY delivery_id")
        assert [row["delivery_id"] for row in consumed] == [seen_id]
        assert late_id not in {row["delivery_id"] for row in consumed}

        wednesday = datetime(2026, 11, 25, 9, 15, tzinfo=ZONE)
        await _service(channel, wednesday).run_once()
        assert "11-24 09:14" in channel.posts[1]["text"]
        assert "11-24 09:00" not in channel.posts[1]["text"]
        thursday = datetime(2026, 11, 26, 9, 15, tzinfo=ZONE)
        await _service(channel, thursday).run_once()
        assert "09:14" not in channel.posts[2]["text"] and "09:00" not in channel.posts[2]["text"]
        assert sum("11-24 09:14" in post["text"] for post in channel.posts) == 1

    run_with_channel_configs(test, monkeypatch)


def test_delivery_on_records_the_receipt_on_the_decision_row(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        chat_settings.set_delivery_enabled(True)
        chat_settings.set_metrics_enabled(True)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        channel = _Channel()
        await _service(channel, MONDAY).run_once()
        await asyncio.gather(*turn_metrics._pending)
        key = f"morning_brief:{MONDAY.date().isoformat()}:channel-lark:{OP}"
        (post,) = channel.posts
        assert post["key"] == key
        assert post["proactive"].source.value == "morning_brief"
        assert post["proactive"].dedupe_key == key
        rows = await pool.fetch("SELECT dedupe_key, decision, outcome, external_id, sent_at IS NOT NULL AS sent "
                                "FROM proactive_deliveries")
        assert [dict(row) for row in rows] == [
            {"dedupe_key": key, "decision": "send", "outcome": "sent", "external_id": "om_1", "sent": True}]

    run_with_channel_configs(test, monkeypatch)


def test_a_successful_retry_replaces_a_rejected_receipt_when_metrics_are_off(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Delivery on, metrics off: the send point writes the receipt itself. A refusal records ``rejected``; the
    successful retry of the same key replaces outcome, sent_at and external_id."""

    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        chat_settings.set_delivery_enabled(True)
        chat_settings.set_metrics_enabled(False)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))

        class _Retry:
            def __init__(self) -> None:
                self.posts: list[dict[str, Any]] = []

            async def post(self, channel_id: str, target: Any, text: str, *, reply_to: str | None = None,
                           idempotency_key: str | None = None, proactive: Any = None) -> PostReceipt:
                self.posts.append({"channel_id": channel_id, "to": target.id, "key": idempotency_key,
                                   "proactive": proactive})
                if len(self.posts) == 1:
                    return PostReceipt(False, external_ids=("om_rejected",), error="refused",
                                       outcome=PostOutcome.REJECTED)
                return PostReceipt(True, external_ids=("om_sent",))

        channel = _Retry()
        service = _service(channel, MONDAY)
        await service.run_once()
        key = f"morning_brief:{MONDAY.date().isoformat()}:channel-lark:{OP}"
        assert channel.posts[0]["proactive"] is None and channel.posts[0]["key"] == key
        rejected = await pool.fetchrow(
            "SELECT decision, outcome, external_id FROM proactive_deliveries WHERE dedupe_key = $1", key)
        assert dict(rejected) == {"decision": "send", "outcome": "rejected", "external_id": "om_rejected"}
        stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await pool.execute("UPDATE proactive_deliveries SET sent_at = $1 WHERE dedupe_key = $2", stale, key)
        await service.run_once()
        row = await pool.fetchrow(
            "SELECT decision, outcome, external_id, sent_at FROM proactive_deliveries WHERE dedupe_key = $1", key)
        assert dict(row)["decision"] == "send"
        assert row["outcome"] == "sent" and row["external_id"] == "om_sent" and row["sent_at"] > stale
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries WHERE dedupe_key = $1", key) == 1

    run_with_channel_configs(test, monkeypatch)


def test_sent_and_unknown_receipts_keep_outcome_sent_at_and_external_id(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A later ``record_receipt`` leaves a receipt already recorded as ``sent`` or ``unknown`` alone: outcome,
    sent_at and external_id all stay as they were."""

    async def test(pool: Any) -> None:
        from looper.db import proactive_deliveries

        for outcome, external, sent_at in (
                ("sent", "om_kept_sent", datetime(2020, 1, 1, tzinfo=timezone.utc)),
                ("unknown", "om_kept_unknown", datetime(2020, 1, 2, tzinfo=timezone.utc))):
            key = f"morning_brief:receipt:{outcome}"
            await pool.execute(
                "INSERT INTO proactive_deliveries (dedupe_key, source, urgency, channel_id, chat, chat_kind, "
                "decision, reason, counted, local_date, outcome, external_id, sent_at) "
                "VALUES ($1, 'morning_brief', 'normal', 'channel-lark', $2, 'direct', 'send', "
                "'within_limits', false, $3, $4, $5, $6)",
                key, OP, MONDAY.date(), outcome, external, sent_at)
            before = dict(await pool.fetchrow(
                "SELECT outcome, sent_at, external_id FROM proactive_deliveries WHERE dedupe_key = $1", key))
            await proactive_deliveries.record_receipt(key, "sent", "om_later")
            after = dict(await pool.fetchrow(
                "SELECT outcome, sent_at, external_id FROM proactive_deliveries WHERE dedupe_key = $1", key))
            assert after == before == {"outcome": outcome, "sent_at": sent_at, "external_id": external}

    run_db(test, monkeypatch)


def test_a_clock_gap_sends_on_the_next_tick_and_a_repeated_hour_sends_once(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def gap(pool: Any) -> None:
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('user.timezone', 'Asia/Jerusalem') "
                           "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        await chat_settings.load()
        chat_settings.set_morning_brief_time("02:15")
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        zone = ZoneInfo("Asia/Jerusalem")
        channel = _Channel()
        await _service(channel, datetime(2026, 3, 27, 1, 59, tzinfo=zone)).run_once()
        assert channel.posts == []
        first = datetime(2026, 3, 27, 0, 0, tzinfo=timezone.utc).astimezone(zone)
        await _service(channel, first).run_once()
        assert len(channel.posts) == 1 and "03-27" in channel.posts[0]["text"]

    async def repeated(pool: Any) -> None:
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('user.timezone', 'Africa/Cairo') "
                           "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        await chat_settings.load()
        chat_settings.set_morning_brief_time("23:30")
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        zone = ZoneInfo("Africa/Cairo")
        channel = _Channel()
        await _service(channel, datetime(2026, 10, 29, 23, 30, tzinfo=zone, fold=0)).run_once()
        await _service(channel, datetime(2026, 10, 29, 23, 30, tzinfo=zone, fold=1)).run_once()
        assert len(channel.posts) == 1
        assert await _rows(pool) == [{"local_date": date(2026, 10, 29), "channel_id": "channel-lark", "chat": OP,
                                      "status": "sent", "attempts": 1}]

    run_with_channel_configs(gap, monkeypatch)
    run_with_channel_configs(repeated, monkeypatch)


def test_a_missing_or_unknown_zone_uses_the_server_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """00:20 UTC is 09:20 in Tokyo and not yet 09:15 in UTC. Only the server-zone fallback sends."""

    async def missing(pool: Any) -> None:
        monkeypatch.setattr(chat_settings, "_timezone", None)
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        channel = _Channel()
        await _service(channel, datetime(2026, 11, 23, 0, 20, tzinfo=timezone.utc)).run_once()
        assert len(channel.posts) == 1 and channel.posts[0]["text"].startswith("晨报 · 11-23（周一）")

    async def unknown(pool: Any) -> None:
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('user.timezone', 'Not/AZone') "
                           "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        await chat_settings.load()
        await _operator(pool)
        monkeypatch.setattr(attention, "collect", _collect(_summary("error", "db down")))
        channel = _Channel()
        await _service(channel, datetime(2026, 11, 23, 0, 20, tzinfo=timezone.utc)).run_once()
        assert len(channel.posts) == 1 and channel.posts[0]["text"].startswith("晨报 · 11-23（周一）")

    monkeypatch.setattr("looper.agents.turn_context.server_zone", lambda: ZoneInfo("Asia/Tokyo"))
    run_with_channel_configs(missing, monkeypatch)
    run_with_channel_configs(unknown, monkeypatch)


def test_an_old_database_gains_the_table_and_the_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        import asyncpg

        await pool.execute("ALTER TABLE proactive_deliveries DROP CONSTRAINT proactive_deliveries_source_check")
        await pool.execute(
            "ALTER TABLE proactive_deliveries ADD CONSTRAINT proactive_deliveries_source_check "
            "CHECK (source IN ('task_notice', 'completion', 'progress', 'gate', 'gate_reminder', "
            "'subscription', 'intent', 'schedule', 'advisor_alert', 'autonomous', 'user_reminder'))")
        await pool.execute("DROP TABLE morning_briefs")
        await pool.execute("DELETE FROM schema_migrations WHERE name IN ('morning_briefs', 'morning_brief_kind')")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(
                "INSERT INTO proactive_deliveries (dedupe_key, source, urgency, channel_id, chat, chat_kind, "
                "decision, reason, counted, local_date) VALUES ('brief', 'morning_brief', 'normal', "
                "'channel-lark', 'ou_1', 'direct', 'send', 'within_limits', false, '2026-11-23')")
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        await pool.execute(
            "INSERT INTO proactive_deliveries (dedupe_key, source, urgency, channel_id, chat, chat_kind, "
            "decision, reason, counted, local_date) VALUES ('brief', 'morning_brief', 'normal', "
            "'channel-lark', 'ou_1', 'direct', 'send', 'within_limits', false, '2026-11-23')")
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries WHERE source = 'morning_brief'") == 1
        assert await pool.fetchval("SELECT to_regclass('morning_briefs') IS NOT NULL")
        await pool.execute("ALTER TABLE morning_briefs DROP COLUMN consumed_until")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        assert await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'morning_briefs'::regclass "
            "AND attname = 'consumed_until' AND attnum > 0 AND NOT attisdropped)")
        await pool.execute("DROP TABLE morning_brief_consumed")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT to_regclass('morning_brief_consumed') IS NOT NULL")
        await pool.execute("ALTER TABLE morning_briefs DROP COLUMN read_ids")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        assert await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'morning_briefs'::regclass "
            "AND attname = 'read_ids' AND attnum > 0 AND NOT attisdropped)")

    run_db(test, monkeypatch)


def test_the_source_check_is_found_by_its_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """A check whose text mentions ``gate_reminder`` but is not on ``source`` is left alone. The ``source`` check
    is widened even when its own text does not mention ``gate_reminder``."""

    async def test(pool: Any) -> None:
        await pool.execute("ALTER TABLE proactive_deliveries DROP CONSTRAINT proactive_deliveries_source_check")
        await pool.execute(
            "ALTER TABLE proactive_deliveries ADD CONSTRAINT proactive_deliveries_source_check "
            "CHECK (source IN ('task_notice'))")
        await pool.execute(
            "ALTER TABLE proactive_deliveries ADD CONSTRAINT proactive_deliveries_decoy_check "
            "CHECK (source_ref IS NULL OR source_ref <> 'gate_reminder')")
        await pool.execute("DELETE FROM schema_migrations WHERE name = 'morning_brief_kind'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        await pool.execute(
            "INSERT INTO proactive_deliveries (dedupe_key, source, urgency, channel_id, chat, chat_kind, "
            "decision, reason, counted, local_date) VALUES ('brief', 'morning_brief', 'normal', "
            "'channel-lark', 'ou_1', 'direct', 'send', 'within_limits', false, '2026-11-23')")
        source = await pool.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'proactive_deliveries'::regclass AND conname = 'proactive_deliveries_source_check'")
        decoy = await pool.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'proactive_deliveries'::regclass AND conname = 'proactive_deliveries_decoy_check'")
        assert source is not None and "morning_brief" in source
        assert decoy is not None and "gate_reminder" in decoy

    run_db(test, monkeypatch)


@pytest.mark.parametrize("rules_on, referenced_digest", [(False, False), (True, False), (True, True)])
def test_a_failed_schedule_lists_its_deferred_push_once(
        monkeypatch: pytest.MonkeyPatch, rules_on: bool, referenced_digest: bool) -> None:
    """A digest row whose schedule is already a failing line joins that line. The digest section drops it, and
    the brief still consumes its id. A healthy schedule's row stays in the digest section. Two failing schedules
    do not take each other's count. The next brief does not list those rows again."""

    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        if rules_on:
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ('intents.rules.enabled', 'true')")
        await pool.execute("INSERT INTO projects (id, name) VALUES ('proj', 'Demo'), ('other', 'Other')")
        await _schedule(pool, "sched-beta", "Beta report", "other", 1 if rules_on else 2)
        await _schedule(pool, "sched-alpha", "Alpha report", "proj", 1)
        await _schedule(pool, "sched-delta", "Delta report", "proj", 1)
        await _schedule(pool, "sched-gamma", "Gamma report", "proj", 0)
        sunday = date(2026, 11, 22)
        ids = [
            await _digest(pool, "beta-a", sunday, kind="subscription", hour=23, minute=0, schedule_id="sched-beta"),
            await _digest(pool, "beta-b", sunday, kind="subscription", hour=23, minute=5, schedule_id="sched-beta"),
            await _digest(pool, "alpha", sunday, kind="subscription", hour=23, minute=10, schedule_id="sched-alpha"),
            await _digest(pool, "gamma", sunday, kind="subscription", hour=23, minute=20, schedule_id="sched-gamma"),
            await _digest(pool, "notice", sunday, hour=23, minute=40),
        ]
        if referenced_digest:
            await pool.execute("UPDATE proactive_deliveries SET source_ref = 'synthetic-subscription' WHERE id = $1",
                               ids[0])
        if rules_on:
            collect = attention.collect

            async def fail_again(*, managers: Any = None) -> dict[str, Any]:
                summary = await collect(managers=managers)
                await pool.execute("UPDATE schedules SET consecutive_failures = 2 WHERE id = 'sched-beta'")
                return summary

            monkeypatch.setattr(attention, "collect", fail_again)
        channel = _Channel()
        await _service(channel, MONDAY).run_once()
        (post,) = channel.posts
        needs, digest = post["text"].split("推迟到这份晨报的消息\n", 1)
        assert "- Beta report（Other；连续失败 2 次，另有 2 条推迟的推送）" in needs
        assert "- Alpha report（Demo；连续失败 1 次，另有 1 条推迟的推送）" in needs
        assert post["text"].count("Alpha report") == 1 and "sched-alpha" not in post["text"]
        assert "- Delta report（Demo；连续失败 1 次）" in needs
        assert "另有 2 条推迟的推送" not in needs.split("Alpha report", 1)[1]
        assert "sched-gamma" not in needs and "sched-alpha" not in needs and "sched-beta" not in needs
        assert digest == (
            "- 订阅推送（免打扰，11-22 23:20，定时 sched-gamma）\n"
            "- 任务通知（免打扰，11-22 23:40）")
        assert await pool.fetchval("SELECT read_ids FROM morning_briefs") == ids
        assert [row["delivery_id"] for row in await pool.fetch(
            "SELECT delivery_id FROM morning_brief_consumed ORDER BY delivery_id")] == sorted(ids)
        tuesday = datetime(2026, 11, 24, 9, 15, tzinfo=ZONE)
        await _service(channel, tuesday).run_once()
        later = channel.posts[1]["text"]
        assert "推迟到这份晨报的消息" not in later and "另有" not in later
        assert "- Beta report（Other；连续失败 2 次）" in later
        assert "- Alpha report（Demo；连续失败 1 次）" in later
        assert await pool.fetchval("SELECT count(*) FROM morning_brief_consumed") == len(ids)

    run_with_channel_configs(test, monkeypatch)


def test_the_brief_off_leaves_a_failed_schedules_deferred_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """While the brief is off, a tick does not send and does not consume a deferred push for a failing schedule."""

    async def test(pool: Any) -> None:
        await _zone(pool)
        await _operator(pool)
        await pool.execute("INSERT INTO projects (id, name) VALUES ('proj', 'Demo')")
        await _schedule(pool, "sched-alpha", "Alpha report", "proj", 1)
        delivery_id = await _digest(pool, "alpha", date(2026, 11, 22), kind="subscription", schedule_id="sched-alpha")
        chat_settings.set_morning_brief_enabled(False)
        channel = _Channel()
        await morning_brief.MorningBriefService(channel, clock=_Clock(MONDAY)).run_once()
        assert channel.posts == []
        assert await pool.fetchval("SELECT count(*) FROM morning_briefs") == 0
        assert await pool.fetchval("SELECT count(*) FROM morning_brief_consumed") == 0
        assert await pool.fetchval("SELECT id FROM proactive_deliveries WHERE decision = 'digest'") == delivery_id

    run_with_channel_configs(test, monkeypatch)
