"""The time rules of scheduled tasks: standard 5-field cron in an IANA time zone (scheduled-tasks RFC, D8).

A rule has five fields separated by spaces: minute (0-59), hour (0-23), day of
the month (1-31), month (1-12) and day of the week (0-7, where 0 and 7 are
Sunday). A field is ``*``, a number, a range ``a-b``, any of these with a step
(``*/15``, ``1-30/5``, ``5/10`` runs from 5 to the field's end), or a list of
them separated by commas. Names (``MON``, ``JAN``), seconds and the ``L``,
``W``, ``#`` and ``?`` extensions are not supported. As in cron, when both
day fields are restricted (neither starts with ``*``) a day matches when either
does; otherwise both must match.

Fire times are wall times in the rule's zone (``zoneinfo``). A time the clock
skips (a DST gap: 02:30 on a spring-forward night) fires at the first valid
wall time after it (03:00); a time the clock repeats fires once, at its first
occurrence. Wall time to instant is therefore non-decreasing, which the
searches below rely on; several wall times that land on one instant fire once.
"""

from __future__ import annotations

import os
import re
import zoneinfo
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

# How far ahead and back a search looks for a matching day: past two leap days, so "0 0 29 2 *" is found.
HORIZON_DAYS = 366 * 8 + 2
# The longest wall-clock gap searched past for the first valid time after a skipped one (a zone has skipped a day).
_MAX_GAP_MINUTES = 2 * 24 * 60
_NUMBER = re.compile(r"[0-9]+")

_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of the month", 1, 31), ("month", 1, 12),
           ("day of the week", 0, 7))
_WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


@dataclass(frozen=True)
class CronRule:
    text: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 is Sunday
    any_day: bool  # the day-of-the-month field starts with "*"
    any_weekday: bool  # the day-of-the-week field starts with "*"

    def matches_day(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        in_month = day.day in self.days
        in_week = day.isoweekday() % 7 in self.weekdays
        if self.any_day or self.any_weekday:
            return in_month and in_week
        return in_month or in_week


def parse(text: str) -> CronRule:
    """A rule from its text; raises ``ValueError`` saying what is wrong."""
    if not isinstance(text, str):
        raise ValueError("a cron rule must be a string")
    parts = text.split()
    if len(parts) != 5:
        raise ValueError("a cron rule has 5 fields (minute hour day-of-the-month month day-of-the-week), "
                         f"got {len(parts)}: {text.strip()!r}")
    values = [_field(part, name, low, high) for part, (name, low, high) in zip(parts, _FIELDS)]
    weekdays = frozenset(day % 7 for day in values[4])
    return CronRule(
        text=" ".join(parts), minutes=tuple(sorted(values[0])), hours=tuple(sorted(values[1])),
        days=frozenset(values[2]), months=frozenset(values[3]), weekdays=weekdays,
        any_day=parts[2].startswith("*"), any_weekday=parts[4].startswith("*"),
    )


def _field(text: str, name: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for item in text.split(","):
        base, slash, step_text = item.partition("/")
        step = _number(step_text, name, item) if slash else 1
        if step < 1:
            raise ValueError(f"the {name} step in {item!r} must be at least 1")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            start, end = _number(first, name, item), _number(last, name, item)
            if start > end:
                raise ValueError(f"the {name} range {item!r} runs backwards")
        else:
            start = _number(base, name, item)
            end = high if slash else start
        for value in (start, end):
            if not low <= value <= high:
                raise ValueError(f"the {name} is {low}-{high}; {item!r} is outside it")
        values.update(range(start, end + 1, step))
    return values


def _number(text: str, name: str, item: str) -> int:
    if not _NUMBER.fullmatch(text):
        raise ValueError(f"{item!r} is not a valid {name}: use numbers, '*', ranges 'a-b', steps '/n' and lists "
                         "'a,b'")
    return int(text)


@lru_cache(maxsize=1)
def _known_zones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def zone(name: str) -> ZoneInfo:
    """The IANA time zone ``name`` (``America/Los_Angeles``, ``UTC``); raises ``ValueError`` for another name."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a time zone is an IANA name such as 'America/Los_Angeles' or 'UTC'")
    known = _known_zones()
    if known and name not in known:
        raise ValueError(f"unknown time zone {name!r}: use an IANA name such as 'America/Los_Angeles' or 'UTC'")
    try:
        return ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {name!r}: use an IANA name such as 'America/Los_Angeles' or "
                         "'UTC'") from None


def local_zone_name() -> str:
    """The server's own IANA zone: ``TZ``, else what ``/etc/localtime`` links to, else ``/etc/timezone``, else UTC."""
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    try:
        candidates.append(os.readlink("/etc/localtime").partition("zoneinfo/")[2])
    except OSError:
        pass
    try:
        candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    for name in candidates:
        try:
            zone(name)
        except ValueError:
            continue
        return name
    return "UTC"


def _exists(wall: datetime, tz: ZoneInfo) -> bool:
    return wall.replace(tzinfo=tz).astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == wall


def _instant(wall: datetime, tz: ZoneInfo) -> datetime:
    """The instant (UTC) wall time ``wall`` fires at in ``tz``: its first occurrence, or the first valid wall
    time after it when the clock skips it."""
    probe = wall
    for _ in range(_MAX_GAP_MINUTES):
        if _exists(probe, tz):
            return probe.replace(tzinfo=tz).astimezone(timezone.utc)
        probe += timedelta(minutes=1)
    return wall.replace(tzinfo=tz).astimezone(timezone.utc)


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("a moment must carry its time zone")
    return moment.astimezone(timezone.utc)


def next_fire(rule: CronRule, tz: ZoneInfo, after: datetime) -> datetime | None:
    """The first fire time strictly after ``after``, in UTC; None when the rule never fires (``0 0 31 2 *``)."""
    after = _utc(after)
    # No wall time before the minute ``after`` falls in fires after it (wall time to instant never decreases).
    start = after.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0)
    day = start.date()
    for _ in range(HORIZON_DAYS):
        if rule.matches_day(day):
            for hour in rule.hours:
                for minute in rule.minutes:
                    wall = datetime.combine(day, time(hour, minute))
                    if wall >= start and (instant := _instant(wall, tz)) > after:
                        return instant
        day += timedelta(days=1)
    return None


def last_fire(rule: CronRule, tz: ZoneInfo, at: datetime) -> datetime | None:
    """The latest fire time at or before ``at``, in UTC; None when there is none within the horizon."""
    at = _utc(at)
    # A wall time somewhat after ``at``'s can still fire before it in a repeated hour: start a day later.
    day = at.astimezone(tz).date() + timedelta(days=1)
    for _ in range(HORIZON_DAYS):
        if rule.matches_day(day):
            for hour in reversed(rule.hours):
                for minute in reversed(rule.minutes):
                    if (instant := _instant(datetime.combine(day, time(hour, minute)), tz)) <= at:
                        return instant
        day -= timedelta(days=1)
    return None


def fires_between(rule: CronRule, tz: ZoneInfo, after: datetime, until: datetime, *,
                  limit: int) -> list[datetime]:
    """The fire times in (``after``, ``until``], oldest first, at most ``limit`` of them."""
    fires: list[datetime] = []
    moment = _utc(after)
    until = _utc(until)
    while len(fires) < limit and (moment := next_fire(rule, tz, moment)) is not None and moment <= until:
        fires.append(moment)
    return fires


def next_fires(rule: CronRule, tz: ZoneInfo, after: datetime, count: int) -> list[datetime]:
    """The next ``count`` fire times after ``after``."""
    fires: list[datetime] = []
    moment: datetime | None = _utc(after)
    while len(fires) < count and (moment := next_fire(rule, tz, moment)) is not None:
        fires.append(moment)
    return fires


def too_close(rule: CronRule, tz: ZoneInfo, now: datetime, min_seconds: int,
              window: timedelta = timedelta(hours=24)) -> tuple[datetime, datetime] | None:
    """The first two consecutive fires within ``window`` after ``now`` that are less than ``min_seconds`` apart."""
    end = _utc(now) + window
    previous = next_fire(rule, tz, now)
    while previous is not None and previous <= end:
        following = next_fire(rule, tz, previous)
        if following is None or following > end:
            return None
        if (following - previous).total_seconds() < min_seconds:
            return previous, following
        previous = following
    return None


# --- Descriptions ---

_ALL_DAYS = frozenset(range(1, 32))
_ALL_WEEKDAYS = frozenset(range(7))
_ALL_MONTHS = frozenset(range(1, 13))


def describe(rule: CronRule, zone_name: str) -> str:
    """The rule in words for the dashboard ("Weekdays at 09:00 (America/Los_Angeles)", "Every 30 minutes"); the
    raw rule and zone when no wording fits."""
    raw = f"{rule.text} ({zone_name})"
    days = _days_phrase(rule) if rule.months == _ALL_MONTHS else None
    if days is None:
        return raw
    leading, trailing = days
    if (every := _every_phrase(rule)) is not None:
        return f"{every} {trailing} ({zone_name})" if trailing else every
    times = _times(rule)
    return f"{leading} at {_join(times)} ({zone_name})" if times else raw


def _days_phrase(rule: CronRule) -> tuple[str, str] | None:
    """The days a rule runs on as a leading and a trailing phrase (``("Daily", "")`` for every day); None when no
    wording fits."""
    if not rule.any_day and not rule.any_weekday:
        return None  # either day field matches: too easy to misread in words
    if rule.weekdays == _ALL_WEEKDAYS:
        if rule.days == _ALL_DAYS:
            return "Daily", ""
        if len(rule.days) > 4:
            return None
        noun = "day" if len(rule.days) == 1 else "days"
        days = _join([str(day) for day in sorted(rule.days)])
        return f"Monthly on {noun} {days}", f"on {noun} {days} of each month"
    if rule.days != _ALL_DAYS:
        return None
    if rule.weekdays == frozenset(range(1, 6)):
        return "Weekdays", "on weekdays"
    if rule.weekdays == frozenset({0, 6}):
        return "Weekends", "on weekends"
    names = _join([f"{_WEEKDAYS[day]}s" for day in sorted(rule.weekdays)])
    return names, f"on {names}"


def _every_phrase(rule: CronRule) -> str | None:
    """"Every minute", "Every 15 minutes", "Every hour at :05" or "Every 2 hours at :00"; None otherwise."""
    if rule.hours == tuple(range(24)) and (step := _step_from_zero(rule.minutes, 60)):
        return "Every minute" if step == 1 else f"Every {step} minutes"
    if len(rule.minutes) == 1 and (step := _step_from_zero(rule.hours, 24)):
        at = f":{rule.minutes[0]:02d}"
        return f"Every hour at {at}" if step == 1 else f"Every {step} hours at {at}"
    return None


def _step_from_zero(values: tuple[int, ...], size: int) -> int | None:
    """``n`` when ``values`` are exactly 0, n, 2n, ... below ``size`` and n divides ``size``."""
    if len(values) < 2 or values[0] != 0 or size % values[1] or values != tuple(range(0, size, values[1])):
        return None
    return values[1]


def _times(rule: CronRule) -> list[str] | None:
    """Each time of day the rule fires at, when there are at most 4."""
    if len(rule.minutes) * len(rule.hours) > 4:
        return None
    return [f"{hour:02d}:{minute:02d}" for hour in rule.hours for minute in rule.minutes]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"
