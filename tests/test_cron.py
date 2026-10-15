"""The time rules of scheduled tasks: parsing, the next and last fire time in a zone's wall time, the minimum
interval and the rule in words."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import cron

LA = cron.zone("America/Los_Angeles")
SHANGHAI = cron.zone("Asia/Shanghai")  # no DST
UTC = timezone.utc


def _local(moments: list[datetime], tz) -> list[str]:
    return [moment.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z") for moment in moments]


def _next(rule: str, after: datetime, count: int, tz=LA) -> list[str]:
    return _local(cron.next_fires(cron.parse(rule), tz, after, count), tz)


@pytest.mark.parametrize(("rule", "after", "expected"), [
    # Steps, lists and ranges.
    ("*/15 * * * *", datetime(2026, 10, 15, 9, 7, tzinfo=LA),
     ["2026-10-15 09:15 PDT", "2026-10-15 09:30 PDT", "2026-10-15 09:45 PDT"]),
    ("5/20 * * * *", datetime(2026, 10, 15, 9, 0, tzinfo=LA),
     ["2026-10-15 09:05 PDT", "2026-10-15 09:25 PDT", "2026-10-15 09:45 PDT"]),
    ("0 9,17 * * *", datetime(2026, 10, 15, 10, 0, tzinfo=LA),
     ["2026-10-15 17:00 PDT", "2026-10-16 09:00 PDT", "2026-10-16 17:00 PDT"]),
    ("30 8-10/2 * * *", datetime(2026, 10, 15, 9, 0, tzinfo=LA),
     ["2026-10-15 10:30 PDT", "2026-10-16 08:30 PDT", "2026-10-16 10:30 PDT"]),
    # Weekdays only: Friday 2026-10-16, then Monday.
    ("0 9 * * 1-5", datetime(2026, 10, 16, 9, 0, tzinfo=LA),
     ["2026-10-19 09:00 PDT", "2026-10-20 09:00 PDT", "2026-10-21 09:00 PDT"]),
    # 0 and 7 are both Sunday.
    ("0 12 * * 7", datetime(2026, 10, 15, tzinfo=LA), ["2026-10-18 12:00 PDT", "2026-10-25 12:00 PDT"]),
    ("0 12 * * 0", datetime(2026, 10, 15, tzinfo=LA), ["2026-10-18 12:00 PDT", "2026-10-25 12:00 PDT"]),
    # Both day fields restricted: the 1st or any Monday (Oct 19 and 26 are Mondays).
    ("0 0 1 * 1", datetime(2026, 10, 15, tzinfo=LA),
     ["2026-10-19 00:00 PDT", "2026-10-26 00:00 PDT", "2026-11-01 00:00 PDT", "2026-11-02 00:00 PST"]),
    # A starred day field means both must match: Mondays that are odd days of the month.
    ("0 0 */2 * 1", datetime(2026, 10, 1, tzinfo=LA),
     ["2026-10-05 00:00 PDT", "2026-10-19 00:00 PDT", "2026-11-09 00:00 PST"]),
    # Month ends: the 31st skips the months without one; Feb 29 waits for leap years.
    ("0 0 31 * *", datetime(2026, 10, 15, tzinfo=LA),
     ["2026-10-31 00:00 PDT", "2026-12-31 00:00 PST", "2027-01-31 00:00 PST", "2027-03-31 00:00 PDT"]),
    ("0 0 29 2 *", datetime(2026, 1, 1, tzinfo=LA), ["2028-02-29 00:00 PST", "2032-02-29 00:00 PST"]),
    # America/Los_Angeles springs forward on 2026-03-08 (02:00 -> 03:00): 02:30 that day fires at 03:00, and the
    # skipped quarter hours fire once, at 03:00.
    ("30 2 * * *", datetime(2026, 3, 7, 12, tzinfo=LA),
     ["2026-03-08 03:00 PDT", "2026-03-09 02:30 PDT"]),
    ("*/15 * * * *", datetime(2026, 3, 8, 1, 40, tzinfo=LA),
     ["2026-03-08 01:45 PST", "2026-03-08 03:00 PDT", "2026-03-08 03:15 PDT"]),
    # It falls back on 2026-11-01 (02:00 PDT -> 01:00 PST): the repeated 01:30 fires once, the first time.
    ("30 1 * * *", datetime(2026, 10, 31, 12, tzinfo=LA),
     ["2026-11-01 01:30 PDT", "2026-11-02 01:30 PST"]),
    ("*/30 * * * *", datetime(2026, 11, 1, 0, 45, tzinfo=LA),
     ["2026-11-01 01:00 PDT", "2026-11-01 01:30 PDT", "2026-11-01 02:00 PST", "2026-11-01 02:30 PST"]),
])
def test_next_fires(rule: str, after: datetime, expected: list[str]) -> None:
    assert _next(rule, after, len(expected)) == expected


def test_a_zone_without_dst_keeps_its_wall_time() -> None:
    assert _next("30 2 * * *", datetime(2026, 3, 7, 12, tzinfo=SHANGHAI), 3, SHANGHAI) == [
        "2026-03-08 02:30 CST", "2026-03-09 02:30 CST", "2026-03-10 02:30 CST"]


def test_next_fire_is_strictly_after_and_none_for_a_rule_that_never_fires() -> None:
    rule = cron.parse("0 9 * * *")
    at_nine = datetime(2026, 10, 15, 9, 0, tzinfo=LA)
    assert cron.next_fire(rule, LA, at_nine) == at_nine.astimezone(UTC) + timedelta(days=1)
    assert cron.next_fire(rule, LA, at_nine - timedelta(seconds=1)) == at_nine.astimezone(UTC)
    assert cron.next_fire(cron.parse("0 0 30 2 *"), LA, at_nine) is None


def test_after_the_repeated_hour_the_first_occurrence_has_passed() -> None:
    # 01:10 PST, the second 01:10 of the night: 01:30 happened once already, in PDT.
    second_pass = datetime(2026, 11, 1, 9, 10, tzinfo=UTC)
    assert _local([cron.next_fire(cron.parse("30 1 * * *"), LA, second_pass)], LA) == ["2026-11-02 01:30 PST"]


@pytest.mark.parametrize(("rule", "at", "expected"), [
    ("0 9 * * 1-5", datetime(2026, 10, 19, 8, 0, tzinfo=LA), "2026-10-16 09:00 PDT"),
    ("0 9 * * 1-5", datetime(2026, 10, 19, 9, 0, tzinfo=LA), "2026-10-19 09:00 PDT"),
    # In the repeated hour, the latest fire can be at a later wall time than now's.
    ("*/20 * * * *", datetime(2026, 11, 1, 9, 15, tzinfo=UTC), "2026-11-01 01:40 PDT"),
    ("30 2 * * *", datetime(2026, 3, 8, 12, tzinfo=LA), "2026-03-08 03:00 PDT"),
])
def test_last_fire(rule: str, at: datetime, expected: str) -> None:
    assert _local([cron.last_fire(cron.parse(rule), LA, at)], LA) == [expected]


def test_fires_between_counts_and_stops_at_the_limit() -> None:
    rule = cron.parse("*/5 * * * *")
    start = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    assert len(cron.fires_between(rule, LA, start, start + timedelta(hours=1), limit=100)) == 12
    assert len(cron.fires_between(rule, LA, start, start + timedelta(hours=1), limit=5)) == 5


@pytest.mark.parametrize(("rule", "error"), [
    ("* * * *", "5 fields"),
    ("60 * * * *", "minute is 0-59"),
    ("0 24 * * *", "hour is 0-23"),
    ("0 0 0 * *", "day of the month is 1-31"),
    ("0 0 * 13 *", "month is 1-12"),
    ("0 0 * * 8", "day of the week is 0-7"),
    ("*/0 * * * *", "step"),
    ("5-1 * * * *", "backwards"),
    ("0 9 * * MON", "use numbers"),
    ("0 9 L * *", "use numbers"),
    ("1,,2 * * * *", "use numbers"),
])
def test_invalid_rules_say_what_is_wrong(rule: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        cron.parse(rule)


@pytest.mark.parametrize("name", ["Mars/Base", "america/los_angeles", "", "../etc/passwd"])
def test_unknown_zones_are_refused(name: str) -> None:
    with pytest.raises(ValueError):
        cron.zone(name)


@pytest.mark.parametrize(("rule", "min_seconds", "close"), [
    ("*/5 * * * *", 300, None),
    ("*/3 * * * *", 300, 180),
    ("0,2 9 * * *", 300, 120),
    ("* * * * *", 60, None),
    ("0 9 * * *", 300, None),
])
def test_minimum_interval_over_the_next_day(rule: str, min_seconds: int, close: int | None) -> None:
    found = cron.too_close(cron.parse(rule), LA, datetime(2026, 10, 15, tzinfo=UTC), min_seconds)
    assert (None if found is None else int((found[1] - found[0]).total_seconds())) == close


@pytest.mark.parametrize(("rule", "words"), [
    ("* * * * *", "Every minute"),
    ("*/30 * * * *", "Every 30 minutes"),
    ("0,30 * * * *", "Every 30 minutes"),
    ("5 * * * *", "Every hour at :05"),
    ("0 */2 * * *", "Every 2 hours at :00"),
    ("*/15 * * * 1-5", "Every 15 minutes on weekdays (America/Los_Angeles)"),
    ("0 9 * * *", "Daily at 09:00 (America/Los_Angeles)"),
    ("0 9,17 * * *", "Daily at 09:00 and 17:00 (America/Los_Angeles)"),
    ("0 9 * * 1-5", "Weekdays at 09:00 (America/Los_Angeles)"),
    ("0 10 * * 0,6", "Weekends at 10:00 (America/Los_Angeles)"),
    ("0 9 * * 1,3,5", "Mondays, Wednesdays and Fridays at 09:00 (America/Los_Angeles)"),
    ("15 10 * * 7", "Sundays at 10:15 (America/Los_Angeles)"),
    ("0 0 1 * *", "Monthly on day 1 at 00:00 (America/Los_Angeles)"),
    # No wording fits: the rule as written.
    ("*/7 * * * *", "*/7 * * * * (America/Los_Angeles)"),
    ("*/30 9-17 * * 1-5", "*/30 9-17 * * 1-5 (America/Los_Angeles)"),
    ("0 0 1 * 1", "0 0 1 * 1 (America/Los_Angeles)"),
    ("0 0 29 2 *", "0 0 29 2 * (America/Los_Angeles)"),
])
def test_descriptions(rule: str, words: str) -> None:
    assert cron.describe(cron.parse(rule), "America/Los_Angeles") == words
