"""A schedule's throttle fields (proactive delivery RFC section 5), one typed view over its row.

Two are columns of their own: ``active_hours`` (``HH:MM-HH:MM`` in the schedule's timezone, NULL all day), outside
which a firing does not happen, and ``idle_only``, which skips a firing while the schedule's Manager answers a
message (``skipped_busy``). The other two are what the schedule already has: whether a run may end without telling
anyone (``allows_silence``: a subscription's quiet word, else a ``notify`` other than ``always``) and what is checked
before a model is called (``pre_check``: an agent schedule's ``gate_command``, a subscription's window). A manual
run ("Run now") is never throttled: the user asked for it then.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from . import cron
from .proactive_delivery import DailyWindow, SourceThrottle


# As the schedules table checks it.
ACTIVE_HOURS = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]")


class PreCheck(StrEnum):
    """What decides, before a model is called, that a firing has nothing to do."""

    NONE = "none"
    GATE_COMMAND = "gate_command"  # an agent schedule's command: nothing printed is ``skipped_gate``
    WINDOW = "window"  # a subscription: nothing selected in its window is ``skipped_gate``


def active_hours(raw: Any) -> str | None:
    """``active_hours`` as stored: ``HH:MM-HH:MM`` with different ends (it may cross midnight), or None for all day;
    ``ValueError`` for anything else."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not ACTIVE_HOURS.fullmatch(raw) or raw[:5] == raw[6:]:
        raise ValueError("active_hours must be HH:MM-HH:MM with two different times (for example 09:00-18:00 or "
                         "22:00-02:00), or null for all day")
    return raw


def idle_only(raw: Any) -> bool:
    """``idle_only`` as stored; null is the default, false."""
    if raw is not None and not isinstance(raw, bool):
        raise ValueError("idle_only must be true, false or null")
    return bool(raw)


@dataclass(frozen=True)
class ScheduleThrottle:
    active_hours: DailyWindow | None
    timezone: ZoneInfo
    idle_only: bool
    allows_silence: bool
    pre_check: PreCheck

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> ScheduleThrottle:
        kind = row.get("kind") or "agent"
        hours = row.get("active_hours")
        pre_check = (PreCheck.WINDOW if kind == "subscription"
                     else PreCheck.GATE_COMMAND if kind == "agent" and row.get("gate_command") else PreCheck.NONE)
        return cls(active_hours=DailyWindow.parse(hours) if hours else None, timezone=cron.zone(row["timezone"]),
                   idle_only=bool(row.get("idle_only")),
                   allows_silence=kind == "subscription" or (row.get("notify") or "failures") != "always",
                   pre_check=pre_check)

    def in_effect(self, moment: datetime) -> bool:
        """Whether ``moment`` is inside the active hours (always, without them)."""
        return self.active_hours is None or self.active_hours.contains(moment.astimezone(self.timezone).time())

    def source(self) -> SourceThrottle:
        """What the delivery layer reads of it for a push of this schedule's."""
        return SourceThrottle(self.active_hours, self.timezone)

    def view(self) -> dict[str, Any]:
        return {"active_hours": self.active_hours.text() if self.active_hours else None, "idle_only": self.idle_only,
                "allows_silence": self.allows_silence, "pre_check": self.pre_check.value}
