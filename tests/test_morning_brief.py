"""The morning brief's pure rules: which day it covers, when it is due, and the text it sends. The service, while
its switch is off, reads that switch and stops."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from looper.agents import attention, chat_settings, morning_brief
from looper.agents.morning_brief import digest_dates, digest_dates_through, is_due, render
from looper.db import connection

ZONE = ZoneInfo("America/Los_Angeles")
MONDAY = date(2026, 11, 23)
FRIDAY = date(2026, 11, 20)


def test_triage_is_plain_text_with_local_time_and_flags_after_the_digest() -> None:
    from looper.agents.triage_model import Category
    from looper.db.triage_events import BriefEvent

    event = BriefEvent(id=42, category=Category.IMPORTANT, title="@title\nline", summary="@summary\tline",
                       flagged=True, last_sent_at=datetime(2026, 11, 23, 16, 5, tzinfo=timezone.utc),
                       chat_name="@Synthetic\nroom")
    text = render(day=MONDAY, needs=None, reminders=[], digests=[_digest()], tz=ZONE,
                  triage=[event], triage_more=2)
    assert text.endswith("进摘要的事件\nE-42 title line（Synthetic room · 08:05）：summary line"
                         "（含要求助手做事的话）（重要，未能及时告诉你）\n另有 2 条，见消息流页")
    assert "@" not in text
    before = render(day=MONDAY, needs=None, reminders=[], digests=[_digest()], tz=ZONE)
    assert text.startswith(before + "\n\n")
    assert render(day=MONDAY, needs=None, reminders=[], digests=[_digest()], tz=ZONE,
                  triage=[], triage_more=2) == before


@pytest.mark.parametrize("field", ["title", "chat_name", "summary"])
def test_triage_fields_neutralize_lark_mentions_and_markdown_links(field) -> None:
    from looper.agents.triage_model import Category
    from looper.db.triage_events import BriefEvent

    probe = '<at user_id="ou_victim">bob</at> [click](https://evil.example) ＠所有人 @所有人 @all'
    event = BriefEvent(id=42, category=Category.DIGEST, title="Title", summary="Summary", flagged=False,
                       last_sent_at=datetime(2026, 11, 23, 16, 5, tzinfo=timezone.utc),
                       chat_name="Synthetic room")
    event = replace(event, **{field: probe})
    text = render(day=MONDAY, needs=None, reminders=[], digests=[], tz=ZONE, triage=[event])
    assert text is not None
    assert "bob" in text
    assert "<at" not in text.lower()
    assert not any(mention in text for mention in ("＠所有人", "@所有人", "@all"))
    assert r"\[click\]\(https://evil\.example\)" in text
    assert re.search(r"(?<!\\)\[[^\]]*\]\(", text) is None


def test_monday_covers_the_weekend_and_another_workday_covers_itself() -> None:
    assert digest_dates(MONDAY) == (date(2026, 11, 21), date(2026, 11, 22), MONDAY)
    assert digest_dates(FRIDAY) == (FRIDAY,)
    assert digest_dates(date(2026, 11, 24)) == (date(2026, 11, 24),)


def test_a_later_day_keeps_the_first_attempts_window() -> None:
    """A refusal records no boundary, so the next day still starts where the first attempt did."""
    tuesday = date(2026, 11, 24)
    wednesday = date(2026, 11, 25)
    assert digest_dates_through(tuesday, tuesday) == (tuesday,)
    assert digest_dates_through(tuesday, wednesday) == (tuesday, wednesday)
    assert digest_dates_through(MONDAY, wednesday) == (
        date(2026, 11, 21), date(2026, 11, 22), MONDAY, tuesday, wednesday)


def test_it_is_due_on_a_workday_at_the_configured_time() -> None:
    send = time(9, 15)
    assert is_due(datetime(2026, 11, 23, 9, 15, tzinfo=ZONE), send)
    assert is_due(datetime(2026, 11, 23, 9, 16, tzinfo=ZONE), send)
    assert not is_due(datetime(2026, 11, 23, 9, 14, tzinfo=ZONE), send)
    assert not is_due(datetime(2026, 11, 22, 12, 0, tzinfo=ZONE), send)  # Sunday
    assert not is_due(datetime(2026, 11, 21, 9, 15, tzinfo=ZONE), send)  # Saturday
    assert not is_due(datetime(2026, 11, 23, 14, 59, tzinfo=ZONE), time(15, 0), time(16))
    assert is_due(datetime(2026, 11, 23, 15, 0, tzinfo=ZONE), time(15, 0), time(16))


@pytest.mark.parametrize(("hour", "minute", "expected"), [(11, 59, True), (12, 0, False), (12, 1, False), (23, 0, False)])
def test_catch_up_ends_at_noon(hour, minute, expected):
    assert is_due(datetime(2026, 11, 23, hour, minute, tzinfo=ZONE), time(9, 15)) is expected


def test_a_skipped_minute_is_due_on_the_first_tick_after_the_gap() -> None:
    """Asia/Jerusalem, Friday 2026-03-27: 01:59 is followed by 03:00. 02:15 never happens, so 03:00 is due."""
    zone = ZoneInfo("Asia/Jerusalem")
    send = time(2, 15)
    assert not is_due(datetime(2026, 3, 27, 1, 59, tzinfo=zone), send)
    first = datetime(2026, 3, 27, 0, 0, tzinfo=timezone.utc).astimezone(zone)
    assert (first.hour, first.minute) == (3, 0)
    assert is_due(first, send)


def test_a_repeated_hour_is_still_due_and_the_same_local_date() -> None:
    """Africa/Cairo, Thursday 2026-10-29: 23:30 happens twice. Both are due, and both are 2026-10-29."""
    zone = ZoneInfo("Africa/Cairo")
    send = time(23, 30)
    first = datetime(2026, 10, 29, 23, 30, tzinfo=zone, fold=0)
    second = datetime(2026, 10, 29, 23, 30, tzinfo=zone, fold=1)
    assert first.utcoffset() != second.utcoffset()
    assert first.date() == second.date() == date(2026, 10, 29)
    assert is_due(first, send, time(23, 59)) and is_due(second, send, time(23, 59))


def _reminder(hour: int, text: str) -> dict:
    return {"due_at": datetime(2026, 11, 23, hour, 0, tzinfo=ZONE), "text": text}


def _digest(**extra) -> dict:
    row = {"kind": "task_notice", "reason": "quiet_hours", "task_id": "task-1", "schedule_id": None,
           "decided_at": datetime(2026, 11, 22, 23, 10, tzinfo=ZONE)}
    row.update(extra)
    return row


def test_the_text_names_each_section_and_nothing_when_all_are_empty() -> None:
    text = render(day=MONDAY, needs="需要你处理（1 项）：\n\n等你回复的关卡 1：\n- 门",
                  reminders=[_reminder(15, "看 PR\n第二行"), _reminder(18, "x" * 200)],
                  digests=[_digest(), _digest(kind="subscription", reason="daily_cap", task_id=None,
                                              schedule_id="sched-9",
                                              decided_at=datetime(2026, 11, 23, 7, 0, tzinfo=ZONE))],
                  tz=ZONE)
    assert text is not None
    assert text.startswith("晨报 · 11-23（周一）")
    assert "需要你处理（1 项）" in text
    assert "今天的提醒\n- 15:00 看 PR 第二行" in text
    assert text.count("…") == 1  # the long reminder is clipped
    assert "推迟到这份晨报的消息" in text
    assert "任务通知（免打扰，11-22 23:10，任务 task-1）" in text
    assert "订阅推送（超过当天上限，11-23 07:00，定时 sched-9）" in text
    assert render(day=MONDAY, needs=None, reminders=[], digests=[], tz=ZONE) is None
    assert render(day=MONDAY, needs=attention.NOTHING, reminders=[], digests=[], tz=ZONE) is None
    # An unread section is not "nothing waits".
    unread = render(day=MONDAY, needs="等你回复的关卡：没能读取（db down）", reminders=[], digests=[], tz=ZONE)
    assert unread is not None and "没能读取" in unread


def test_off_reads_the_switch_and_stops(monkeypatch) -> None:
    """While the brief is off a tick does not decide, post, or open the database."""
    calls: list[str] = []

    def pool():
        calls.append("pool")
        raise AssertionError("pool")

    monkeypatch.setattr(connection, "get_pool", pool)
    monkeypatch.setattr(morning_brief.proactive_delivery, "deliver", lambda *args, **kwargs: calls.append("deliver"))
    monkeypatch.setattr(morning_brief.attention, "collect", lambda **kwargs: calls.append("collect"))
    chat_settings.set_morning_brief_enabled(False)

    async def post(*args, **kwargs):
        calls.append("post")
        raise AssertionError("post")

    service = morning_brief.MorningBriefService(
        type("Registry", (), {"post": staticmethod(post)})(),
        clock=lambda: datetime(2026, 11, 23, 9, 15, tzinfo=ZONE))
    asyncio.run(service.run_once())
    assert calls == []
