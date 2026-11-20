"""One-time reminders without a database (reminders RFC): when one is due, the words it is sent with, and which of
the due ones a tick sends or settles missed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import reminders

UTC = timezone.utc
LA = "America/Los_Angeles"
# 2026-11-20 10:00 in Los Angeles (a Friday).
NOW = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize(("at", "in_minutes", "zone", "due"), [
    ("15:00", None, LA, datetime(2026, 11, 20, 23, 0, tzinfo=UTC)),  # later today
    ("09:30", None, LA, datetime(2026, 11, 21, 17, 30, tzinfo=UTC)),  # passed today: tomorrow
    ("10:00", None, LA, datetime(2026, 11, 21, 18, 0, tzinfo=UTC)),  # now is not ahead: tomorrow
    ("2026-11-25 08:05", None, LA, datetime(2026, 11, 25, 16, 5, tzinfo=UTC)),
    ("2026-11-25T08:05", None, LA, datetime(2026, 11, 25, 16, 5, tzinfo=UTC)),
    ("15:00", None, "UTC", datetime(2026, 11, 21, 15, 0, tzinfo=UTC)),
    (None, 90, LA, NOW + timedelta(minutes=90)),
])
def test_a_reminder_is_due_at_the_time_the_user_named(at, in_minutes, zone, due):
    assert reminders.resolve_due(at=at, in_minutes=in_minutes, timezone=zone, now=NOW) == due


@pytest.mark.parametrize(("at", "in_minutes", "zone", "message"), [
    (None, None, LA, "exactly one of at and in_minutes"),
    ("15:00", 5, LA, "exactly one of at and in_minutes"),
    ("2026-11-20 09:00", None, LA, "has passed: it is 2026-11-20 10:00"),
    ("2027-12-01 09:00", None, LA, "at most a year ahead"),
    ("3pm", None, LA, "at is HH:MM"),
    ("2026-13-01 09:00", None, LA, "at is HH:MM"),
    ("2027-03-14 02:30", None, LA, "does not exist in America/Los_Angeles"),  # the clocks skip that half hour
    ("15:00", None, "Mars/Olympus", "unknown time zone"),
])
def test_a_time_that_cannot_be_kept_is_refused_saying_what_to_change(at, in_minutes, zone, message):
    with pytest.raises(ValueError, match=message):
        reminders.resolve_due(at=at, in_minutes=in_minutes, timezone=zone, now=NOW)


def test_the_user_reads_the_time_with_its_weekday_and_today_or_tomorrow():
    assert reminders.when(datetime(2026, 11, 20, 23, 0, tzinfo=UTC), LA, now=NOW) == "今天 11-20（周五）15:00"
    assert reminders.when(datetime(2026, 11, 21, 17, 30, tzinfo=UTC), LA, now=NOW) == "明天 11-21（周六）09:30"
    assert reminders.when(datetime(2026, 11, 25, 16, 5, tzinfo=UTC), LA, now=NOW) == "11-25（周三）08:05"
    assert reminders.when(datetime(2026, 11, 25, 16, 5, tzinfo=UTC), LA) == "11-25（周三）08:05"


def _reminder(**fields):
    return {"id": 1, "text": "看 PR", "timezone": LA, "created_at": datetime(2026, 11, 20, 22, 3, tzinfo=UTC),
            "due_at": datetime(2026, 11, 20, 23, 0, tzinfo=UTC), "channel_id": "channel-lark", "chat": "ou_op",
            **fields}


def test_the_message_is_code_written_from_the_users_words_and_says_when_it_is_late():
    due = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
    assert reminders.message_text(_reminder(), now=due + timedelta(seconds=30)) == "⏰ 你在 11-20 14:03 让我提醒你：看 PR"
    assert reminders.message_text(_reminder(), now=due + timedelta(hours=3)) == (
        "⏰ 你在 11-20 14:03 让我提醒你：看 PR\n（原定 11-20 15:00，Looper 没能按时发出，现在补发。）")


def test_a_tick_sends_what_is_on_time_and_of_each_chats_late_ones_only_the_latest_within_36_hours():
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    due = [
        _reminder(id=1, due_at=now - timedelta(hours=40)),  # past 36 hours: missed
        _reminder(id=2, due_at=now - timedelta(hours=3)),  # late, not the latest: missed
        _reminder(id=3, due_at=now - timedelta(hours=1)),  # the latest late one: sent
        _reminder(id=4, due_at=now - timedelta(minutes=9)),  # on time (a restart's minutes): sent
        _reminder(id=5, due_at=now - timedelta(hours=2), chat="ou_other"),  # another chat's latest: sent
        _reminder(id=6, due_at=now - timedelta(hours=50), chat="ou_third"),  # its only one, past 36 hours: missed
    ]
    plan = reminders.plan(due, now)
    assert sorted(plan.send) == [3, 4, 5]
    assert plan.missed == {1: reminders.MISSED_OLD, 2: reminders.MISSED_LATER, 6: reminders.MISSED_OLD}
    assert reminders.plan([], now) == reminders.Plan(send=(), missed={})


def test_a_users_direct_chat_is_their_lark_open_id_or_their_telegram_chat():
    lark, telegram = reminders.target("ou_op"), reminders.target("12345")
    assert (lark.kind.value, lark.id, lark.group) == ("user", "ou_op", False)
    assert (telegram.kind.value, telegram.id, telegram.group) == ("chat", "12345", False)
