"""Reminder dialog checks use the turn's time, including midnight and DST boundaries."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evals.prompt_regression import dialogs


@pytest.mark.parametrize("fake", [False, True])
@pytest.mark.parametrize("now, zone, due", [
    ("2026-11-22T23:30:00+00:00", "UTC", "2026-11-23T15:00:00+00:00"),
    ("2026-11-23T00:30:00+00:00", "UTC", "2026-11-24T15:00:00+00:00"),
    # UTC has crossed midnight, while Los Angeles is still on the day before the clock change.
    ("2026-03-08T00:30:00+00:00", "America/Los_Angeles", "2026-03-08T22:00:00+00:00"),
    ("2026-11-01T00:30:00+00:00", "America/Los_Angeles", "2026-11-01T23:00:00+00:00"),
])
def test_reminder_checker_uses_the_turn_time_in_the_reminders_zone(monkeypatch, fake, now, zone, due):
    now, due = datetime.fromisoformat(now), datetime.fromisoformat(due)

    class AfterMidnight(datetime):
        @classmethod
        def now(cls, tz=None):
            return (now + timedelta(days=1)).astimezone(tz)

    monkeypatch.setattr(dialogs, "datetime", AfterMidnight)
    row = {"id": 1, "status": "pending", "timezone": zone, "due_at": due, "text": "看 PR #42"}
    monkeypatch.setattr(dialogs, "_reminder_rows", lambda _: [row])
    look = SimpleNamespace(now=now, fake=fake, reply="已记下提醒 #1",
                           turn_actions=lambda: [{"tool": "looper_reminder_create", "status": "ok", "risk": "write"}])
    assert dialogs._reminder_set(look) is None
    # A wrong date or hour must still fail, even when it is a valid future reminder.
    for offset in (timedelta(days=1), timedelta(hours=1)):
        row["due_at"] = due + offset
        assert "not 15:00 tomorrow" in dialogs._reminder_set(look)


def test_fake_reminder_date_is_fixed_in_the_server_zone(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    dialog, = dialogs.select(["reminder_tools"])
    _, arguments = dialog.says[0].fake[0]
    assert arguments["timezone"] == "UTC"
    assert arguments["at"] == "2026-11-23 15:00"
    assert dialogs.REMINDER_NOW == datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
