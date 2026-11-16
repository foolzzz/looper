"""Typed audience message settings at the schedule input boundaries."""

import asyncio

import pytest

from looper.agents import api_v1, schedules
from looper.agents.ops import schedules as schedule_ops
from looper.agents.projects import RequestError
from looper.agents.v1_domains import schedule_writes


@pytest.mark.parametrize("value", ["report", "deliverable"])
def test_notify_format_is_a_typed_api_and_manager_field(value):
    api_v1.validate_arguments(schedule_writes.SCHEDULE_PATCH, {"notify_format": value})
    fields, _, _ = asyncio.run(schedules._fields({"notify_format": value}, update=True))
    assert fields["notify_format"] == value
    assert schedule_ops.FIELD_WORDS["notify_format"] == "通知格式"


@pytest.mark.parametrize("value", [None, True, "audience"])
def test_invalid_notify_format_is_refused(value):
    with pytest.raises(RequestError, match="notify_format must be one of report, deliverable"):
        asyncio.run(schedules._fields({"notify_format": value}, update=True))


def test_command_schedule_refuses_deliverable():
    with pytest.raises(RequestError, match="command schedule.*notify_format.*report"):
        schedules._check_kind_fields("command", {"command": "true", "notify_format": "deliverable"})
