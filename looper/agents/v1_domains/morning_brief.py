"""The morning brief's switch (``morning_brief.enabled``, ``morning_brief.time``, ``morning_brief.cutoff``): operator-only, separate from
the chat settings clients see."""

from .. import chat_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

_TIME = {"type": ["string", "null"], "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"}
VIEW = _object({"enabled": _BOOL, "time": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
                "cutoff": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"}},
               closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("morning_brief.get", "POST", "/api/v1/morning_brief/get",
              "Read the morning brief: whether it is on, its local send time and catch-up cutoff.",
              "Read the morning brief: whether it is on, its local send time and catch-up cutoff. "
              "It is off by default, with send time 09:15 and cutoff 12:00.",
              _object({}, closed=True), VIEW, operator_only=True,
              level=ActionLevel.INSIDE),  # Reads Looper's own morning-brief setting.
    Operation("morning_brief.update", "POST", "/api/v1/morning_brief/update",
              "Turn the morning brief on or off, or set its local send time and catch-up cutoff.",
              "Turn the morning brief on or off, or set its local send time and catch-up cutoff. "
              "Cutoff must be later than send time. Null restores that field's default. Off sends nothing.",
              _object({"enabled": {"type": ["boolean", "null"]}, "time": _TIME, "cutoff": _TIME}, required=[], closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # Stores only Looper's own morning-brief setting; sends nothing.
)


def _choice(arguments: dict, name: str, default: object) -> object:
    if name not in arguments:
        return None  # caller keeps the current value; this marks "not in the body"
    value = arguments[name]
    return default if value is None else value


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "morning_brief.update":
        return None
    current = chat_settings.morning_brief_view()
    enabled = current["enabled"] if "enabled" not in arguments else _choice(
        arguments, "enabled", chat_settings.DEFAULT_MORNING_BRIEF_ENABLED)
    when = current["time"] if "time" not in arguments else _choice(
        arguments, "time", chat_settings.DEFAULT_MORNING_BRIEF_TIME)
    cutoff = current["cutoff"] if "cutoff" not in arguments else _choice(
        arguments, "cutoff", chat_settings.DEFAULT_MORNING_BRIEF_CUTOFF)
    if not isinstance(enabled, bool) or not isinstance(when, str) or not isinstance(cutoff, str):
        return "The morning brief update is refused: enabled is true, false or null, and time/cutoff are HH:MM or null."
    return (f"Set morning_brief.enabled to {str(enabled).lower()} and morning_brief.time to {when}, "
            f"morning_brief.cutoff to {cutoff}? "
            + ("On workdays at that local time, before the cutoff and outside quiet hours, one brief goes to the operator's direct chat." if enabled else
               "The morning brief stops; nothing is sent for it."))


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "morning_brief.get":
        return chat_settings.morning_brief_view()
    try:
        return await chat_settings.update_morning_brief(arguments)
    except ValueError as exc:
        from ..ops import ErrorCode, OpError

        raise OpError(ErrorCode.INVALID, str(exc)) from None
