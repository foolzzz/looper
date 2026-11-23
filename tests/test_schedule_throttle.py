"""A schedule's throttle fields as one typed view (``schedule_throttle``): what ``active_hours`` accepts, when a
schedule is in effect, and what the existing fields say about silence and the check before a model is called."""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from looper.agents.proactive_delivery import DailyWindow
from looper.agents.schedule_throttle import PreCheck, ScheduleThrottle, active_hours, idle_only


@pytest.mark.parametrize("raw", ["09:00-18:00", "22:00-02:00", "00:00-23:59"])
def test_active_hours_are_two_different_hh_mm_times(raw: str) -> None:
    assert active_hours(raw) == raw


@pytest.mark.parametrize("raw", [None, ""])
def test_no_active_hours_is_all_day(raw: str | None) -> None:
    assert active_hours(raw) is None


@pytest.mark.parametrize("raw", ["09:00-09:00", "9:00-18:00", "09:00", "09:00-18:00:00", "09:00 - 18:00",
                                 "24:00-08:00", "09:60-10:00", 900, True])
def test_other_active_hours_are_refused(raw: object) -> None:
    with pytest.raises(ValueError, match="active_hours must be HH:MM-HH:MM"):
        active_hours(raw)


@pytest.mark.parametrize(("raw", "expected"), [(True, True), (False, False), (None, False)])
def test_idle_only_is_a_flag_null_its_default(raw: object, expected: bool) -> None:
    assert idle_only(raw) is expected


@pytest.mark.parametrize("raw", ["yes", 1, 0])
def test_idle_only_refuses_anything_but_a_flag(raw: object) -> None:
    with pytest.raises(ValueError, match="idle_only must be"):
        idle_only(raw)


def _row(**fields: object) -> dict[str, object]:
    return {"kind": "agent", "timezone": "America/Los_Angeles", "notify": "failures", "gate_command": None,
            "active_hours": None, "idle_only": False, **fields}


@pytest.mark.parametrize(("row", "allows_silence", "pre_check"), [
    (_row(), True, PreCheck.NONE),
    (_row(notify="always"), False, PreCheck.NONE),
    (_row(gate_command="./new-alerts"), True, PreCheck.GATE_COMMAND),
    (_row(kind="command", notify="always"), False, PreCheck.NONE),
    (_row(kind="subscription", notify="always"), True, PreCheck.WINDOW),  # its quiet word
])
def test_silence_and_the_check_before_a_model_come_from_the_existing_fields(
        row: dict[str, object], allows_silence: bool, pre_check: PreCheck) -> None:
    throttle = ScheduleThrottle.of(row)
    assert (throttle.allows_silence, throttle.pre_check) == (allows_silence, pre_check)


LA = ZoneInfo("America/Los_Angeles")


@pytest.mark.parametrize(("hours", "moment", "expected"), [
    (None, datetime(2026, 1, 15, 3, 0, tzinfo=LA), True),
    ("09:00-18:00", datetime(2026, 1, 15, 9, 0, tzinfo=LA), True),
    ("09:00-18:00", datetime(2026, 1, 15, 18, 0, tzinfo=LA), False),
    ("09:00-18:00", datetime(2026, 1, 15, 8, 59, tzinfo=LA), False),
    # The schedule's own zone: 17:00 UTC is 09:00 in Los Angeles.
    ("09:00-18:00", datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc), True),
    ("09:00-18:00", datetime(2026, 1, 15, 16, 59, tzinfo=timezone.utc), False),
    ("22:00-02:00", datetime(2026, 1, 15, 23, 30, tzinfo=LA), True),
    ("22:00-02:00", datetime(2026, 1, 16, 1, 59, tzinfo=LA), True),
    ("22:00-02:00", datetime(2026, 1, 16, 2, 0, tzinfo=LA), False),
])
def test_a_schedule_is_in_effect_inside_its_active_hours_in_its_own_zone(hours: str | None, moment: datetime,
                                                                          expected: bool) -> None:
    assert ScheduleThrottle.of(_row(active_hours=hours)).in_effect(moment) is expected


def test_the_view_and_what_the_delivery_layer_reads() -> None:
    throttle = ScheduleThrottle.of(_row(active_hours="09:00-18:00", idle_only=True))
    assert throttle.view() == {"active_hours": "09:00-18:00", "idle_only": True, "allows_silence": True,
                               "pre_check": "none"}
    source = throttle.source()
    assert (source.active_hours, source.timezone) == (DailyWindow(time(9), time(18)), LA)
