"""The delivery decision (``proactive_delivery.deliver``): one row per rule and per boundary, in the rules' order.

The policy is the built-in one (quiet 22:00–08:00, 5 counted direct messages a day, 5 minutes after the user wrote)
in a timezone other than UTC, so a test that forgot to convert would fail."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from looper.agents.proactive_delivery import (
    ChatKind, DailyWindow, Decision, Delivery, DeliveryKind, DeliveryPolicy, DeliveryState, Outcome, Reason,
    SourceThrottle, Urgency, deliver, digest_date)

ZONE = ZoneInfo("Europe/Berlin")
POLICY = DeliveryPolicy(timezone=ZONE)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in November 2026, Berlin wall time (UTC+1)."""
    return datetime(2026, 11, day, hour, minute, tzinfo=ZONE)


def notice(chat: ChatKind = ChatKind.DIRECT, **extra) -> Delivery:
    return Delivery(DeliveryKind.TASK_NOTICE, Urgency.NORMAL, chat, **extra)


NIGHT = at(20, 23)  # quiet hours, before midnight: tomorrow's digest
EARLY = at(20, 7, 59)  # quiet hours, after midnight: today's digest
DAY = at(20, 14)
SOURCE_9_TO_18 = SourceThrottle(DailyWindow(time(9), time(18)), ZONE)
SOURCE_UTC_9_TO_18 = SourceThrottle(DailyWindow(time(9), time(18)), ZoneInfo("UTC"))
GATE = Delivery(DeliveryKind.GATE_REMINDER, Urgency.BLOCKING, ChatKind.DIRECT)
USER_REMINDER = Delivery(DeliveryKind.USER_REMINDER, Urgency.USER_SET, ChatKind.GROUP)
TODAY, TOMORROW = date(2026, 11, 20), date(2026, 11, 21)


@pytest.mark.parametrize(("delivery", "state", "expected"), [
    # 1. A source with nothing to tell is dropped, before anything else.
    (notice(silent=True), DeliveryState(DAY), Decision(Outcome.DROP, Reason.SILENT)),
    (replace(GATE, silent=True), DeliveryState(NIGHT, 9), Decision(Outcome.DROP, Reason.SILENT)),
    # 3. A gate holding back a task and the user's own reminder: never throttled, never counted.
    (GATE, DeliveryState(NIGHT, sent_today=9), Decision(Outcome.SEND, Reason.URGENT)),
    (GATE, DeliveryState(EARLY), Decision(Outcome.SEND, Reason.URGENT)),
    (USER_REMINDER, DeliveryState(NIGHT), Decision(Outcome.SEND, Reason.URGENT)),
    (replace(GATE, source=SOURCE_9_TO_18), DeliveryState(NIGHT), Decision(Outcome.SEND, Reason.URGENT)),
    # A gate reminder that does not block is a normal message.
    (replace(GATE, urgency=Urgency.NORMAL), DeliveryState(NIGHT),
     Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=TOMORROW)),
    # 4. Outside the source's own hours (its timezone, not the policy's): the digest, even right after the user wrote.
    (notice(source=SOURCE_9_TO_18), DeliveryState(at(20, 8, 59)),
     Decision(Outcome.DIGEST, Reason.OUTSIDE_ACTIVE_HOURS, digest_date=TOMORROW)),
    (notice(source=SOURCE_9_TO_18), DeliveryState(at(20, 18), last_user_message_at=at(20, 17, 59)),
     Decision(Outcome.DIGEST, Reason.OUTSIDE_ACTIVE_HOURS, digest_date=TOMORROW)),
    (notice(source=SOURCE_9_TO_18), DeliveryState(at(20, 9)), Decision(Outcome.SEND, Reason.WITHIN_LIMITS, True)),
    (notice(source=SOURCE_UTC_9_TO_18), DeliveryState(at(20, 9, 30)),  # 08:30 UTC
     Decision(Outcome.DIGEST, Reason.OUTSIDE_ACTIVE_HOURS, digest_date=TOMORROW)),
    (notice(source=SOURCE_UTC_9_TO_18), DeliveryState(at(20, 18, 30)),  # 17:30 UTC
     Decision(Outcome.SEND, Reason.WITHIN_LIMITS, True)),
    (notice(source=SourceThrottle(None, ZONE)), DeliveryState(DAY), Decision(Outcome.SEND, Reason.WITHIN_LIMITS, True)),
    # 5. The user wrote here in the last 5 minutes: it passes the quiet hours and the cap, uncounted.
    (notice(), DeliveryState(NIGHT, last_user_message_at=NIGHT - timedelta(minutes=5)),
     Decision(Outcome.SEND, Reason.RECENT_REPLY)),
    (notice(), DeliveryState(DAY, sent_today=5, last_user_message_at=DAY - timedelta(minutes=1)),
     Decision(Outcome.SEND, Reason.RECENT_REPLY)),
    (notice(), DeliveryState(NIGHT, last_user_message_at=NIGHT + timedelta(seconds=2)),  # clocks a little apart
     Decision(Outcome.SEND, Reason.RECENT_REPLY)),
    (notice(), DeliveryState(NIGHT, last_user_message_at=NIGHT - timedelta(minutes=5, seconds=1)),
     Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=TOMORROW)),
    # 6. Quiet hours, 22:00 included, 08:00 not: the digest of the morning the quiet hours end.
    (notice(), DeliveryState(at(20, 22)), Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=TOMORROW)),
    (notice(), DeliveryState(at(20, 21, 59)), Decision(Outcome.SEND, Reason.WITHIN_LIMITS, True)),
    (notice(), DeliveryState(EARLY), Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=TODAY)),
    (notice(), DeliveryState(at(20, 8)), Decision(Outcome.SEND, Reason.WITHIN_LIMITS, True)),
    (notice(ChatKind.GROUP), DeliveryState(NIGHT), Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=TOMORROW)),
    # In the policy's timezone: 21:30 UTC is 22:30 in Berlin, quiet; 07:30 UTC is 08:30, not.
    (notice(), DeliveryState(datetime(2026, 11, 20, 21, 30, tzinfo=timezone.utc)),
     Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=TOMORROW)),
    (notice(), DeliveryState(datetime(2026, 11, 20, 7, 30, tzinfo=timezone.utc)),
     Decision(Outcome.SEND, Reason.WITHIN_LIMITS, True)),
    # 7. A direct chat's sixth counted message of the day: tomorrow's digest. A group has no cap.
    (notice(), DeliveryState(DAY, sent_today=4), Decision(Outcome.SEND, Reason.WITHIN_LIMITS, True)),
    (notice(), DeliveryState(DAY, sent_today=5), Decision(Outcome.DIGEST, Reason.DAILY_CAP, digest_date=TOMORROW)),
    (notice(ChatKind.GROUP), DeliveryState(DAY, sent_today=50), Decision(Outcome.SEND, Reason.WITHIN_LIMITS)),
    # 8. Otherwise sent; counted only in a direct chat.
    (notice(), DeliveryState(DAY), Decision(Outcome.SEND, Reason.WITHIN_LIMITS, counted=True)),
    (notice(ChatKind.GROUP), DeliveryState(DAY), Decision(Outcome.SEND, Reason.WITHIN_LIMITS, counted=False)),
])
def test_each_rule_in_order(delivery: Delivery, state: DeliveryState, expected: Decision) -> None:
    assert deliver(delivery, POLICY, state) == expected


@pytest.mark.parametrize(("policy", "state", "expected"), [
    # No quiet hours: nothing waits for the night, and a capped message goes into the next day's digest.
    (replace(POLICY, quiet_hours=None), DeliveryState(NIGHT), Decision(Outcome.SEND, Reason.WITHIN_LIMITS, True)),
    (replace(POLICY, quiet_hours=None), DeliveryState(EARLY, sent_today=5),
     Decision(Outcome.DIGEST, Reason.DAILY_CAP, digest_date=TOMORROW)),
    # Quiet hours that do not cross midnight.
    (replace(POLICY, quiet_hours=DailyWindow(time(12), time(14))), DeliveryState(at(20, 13)),
     Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=TODAY)),
    (replace(POLICY, quiet_hours=DailyWindow(time(12), time(14))), DeliveryState(at(20, 23)),
     Decision(Outcome.SEND, Reason.WITHIN_LIMITS, True)),
    # A cap of 0: every normal direct message waits for the digest.
    (replace(POLICY, dm_daily_cap=0), DeliveryState(DAY), Decision(Outcome.DIGEST, Reason.DAILY_CAP,
                                                                   digest_date=TOMORROW)),
    # No grace after the user wrote.
    (replace(POLICY, recent_reply=timedelta(0)),
     DeliveryState(NIGHT, last_user_message_at=NIGHT - timedelta(seconds=1)),
     Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=TOMORROW)),
])
def test_the_policy_moves_each_rule(policy: DeliveryPolicy, state: DeliveryState, expected: Decision) -> None:
    assert deliver(notice(), policy, state) == expected


@pytest.mark.parametrize(("spoke", "now", "expected"), [
    # Spring forward (02:00 -> 03:00): 01:58 to 03:02 on the wall is 4 minutes.
    (datetime(2026, 3, 29, 1, 58, tzinfo=ZONE), datetime(2026, 3, 29, 3, 2, tzinfo=ZONE),
     Decision(Outcome.SEND, Reason.RECENT_REPLY)),
    # Fall back (03:00 -> 02:00): 02:00 (summer time) to 02:04 (winter time) is 64 minutes.
    (datetime(2026, 10, 25, 2, 0, tzinfo=ZONE), datetime(2026, 10, 25, 2, 4, fold=1, tzinfo=ZONE),
     Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=date(2026, 10, 25))),
    # ... and 02:58 (summer time) to 02:02 (winter time) is 4.
    (datetime(2026, 10, 25, 2, 58, tzinfo=ZONE), datetime(2026, 10, 25, 2, 2, fold=1, tzinfo=ZONE),
     Decision(Outcome.SEND, Reason.RECENT_REPLY)),
    # The same moments given in different zones.
    (datetime(2026, 3, 29, 0, 58, tzinfo=timezone.utc), datetime(2026, 3, 29, 3, 2, tzinfo=ZONE),
     Decision(Outcome.SEND, Reason.RECENT_REPLY)),
])
def test_the_grace_after_the_user_wrote_is_real_time_across_dst(spoke: datetime, now: datetime,
                                                                  expected: Decision) -> None:
    assert deliver(notice(), POLICY, DeliveryState(now, last_user_message_at=spoke)) == expected


@pytest.mark.parametrize(("now", "expected"), [
    (datetime(2026, 3, 29, 1, 30, tzinfo=ZONE), date(2026, 3, 29)),  # the night clocks go forward
    (datetime(2026, 10, 25, 7, 59, tzinfo=ZONE), date(2026, 10, 25)),  # the night clocks go back
    (datetime(2026, 11, 20, 8, 0, tzinfo=ZONE), date(2026, 11, 21)),  # the quiet hours just ended: the next ones
    (datetime(2026, 12, 31, 22, 0, tzinfo=ZONE), date(2027, 1, 1)),
    (datetime(2026, 11, 20, 23, 30, tzinfo=timezone.utc), date(2026, 11, 21)),  # 00:30 in Berlin
    (datetime(2026, 11, 20, 7, 30, tzinfo=timezone.utc), date(2026, 11, 21)),  # 08:30 in Berlin
])
def test_the_digest_is_the_local_morning_the_quiet_hours_end(now: datetime, expected: date) -> None:
    assert digest_date(POLICY, now) == expected


@pytest.mark.parametrize(("text", "start", "end"), [
    ("22:00-08:00", time(22), time(8)),
    ("09:30 - 18:00", time(9, 30), time(18)),
])
def test_a_daily_window_reads_hh_mm_dash_hh_mm(text: str, start: time, end: time) -> None:
    window = DailyWindow.parse(text)
    assert (window.start, window.end) == (start, end)
    assert DailyWindow.parse(window.text()) == window


@pytest.mark.parametrize("text", ["22:00", "22:00-08:00-09:00", "25:00-08:00", "08:00-08:00", "noon-night"])
def test_a_bad_daily_window_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        DailyWindow.parse(text)


@pytest.mark.parametrize("build", [
    lambda: DeliveryPolicy(timezone=ZONE, dm_daily_cap=-1),
    lambda: DeliveryPolicy(timezone=ZONE, dm_daily_cap=True),
    lambda: DeliveryPolicy(timezone=ZONE, recent_reply=timedelta(seconds=-1)),
    lambda: DeliveryState(datetime(2026, 11, 20, 14)),  # naive
    lambda: DeliveryState(DAY, last_user_message_at=datetime(2026, 11, 20, 14)),
    lambda: DeliveryState(DAY, sent_today=-1),
    lambda: DailyWindow(time(22, tzinfo=timezone.utc), time(8)),
])
def test_inputs_that_cannot_be_decided_on_are_refused(build) -> None:
    with pytest.raises(ValueError):
        build()


BRIEF = Delivery(DeliveryKind.MORNING_BRIEF, Urgency.NORMAL, ChatKind.DIRECT)


@pytest.mark.parametrize(("delivery", "state", "expected"), [
    # 2. Nothing to say is still dropped, ahead of the morning brief's exemption.
    (replace(BRIEF, silent=True), DeliveryState(NIGHT, sent_today=9), Decision(Outcome.DROP, Reason.SILENT)),
    # A scheduled brief obeys quiet and source hours, but is exempt from the cap.
    (BRIEF, DeliveryState(NIGHT, sent_today=9),
     Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=TOMORROW)),
    (BRIEF, DeliveryState(EARLY, sent_today=9),
     Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=EARLY.date())),
    (BRIEF, DeliveryState(DAY, sent_today=9), Decision(Outcome.SEND, Reason.WITHIN_LIMITS, counted=False)),
    (BRIEF, DeliveryState(DAY, last_user_message_at=DAY), Decision(Outcome.SEND, Reason.WITHIN_LIMITS, counted=False)),
    (BRIEF, DeliveryState(NIGHT, last_user_message_at=NIGHT),
     Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=TOMORROW)),
    (replace(BRIEF, source=SOURCE_9_TO_18), DeliveryState(at(20, 3)),
     Decision(Outcome.DIGEST, Reason.OUTSIDE_ACTIVE_HOURS, digest_date=EARLY.date())),
    # A normal notice at the same moment still waits for the digest.
    (notice(), DeliveryState(NIGHT), Decision(Outcome.DIGEST, Reason.QUIET_HOURS, digest_date=TOMORROW)),
])
def test_a_morning_brief_is_sent_uncounted_unless_it_has_nothing_to_say(delivery: Delivery, state: DeliveryState,
                                                                        expected: Decision) -> None:
    assert deliver(delivery, POLICY, state) == expected
