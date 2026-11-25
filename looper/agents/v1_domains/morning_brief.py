"""The morning brief's switch (``morning_brief.enabled``, ``morning_brief.time``, ``morning_brief.cutoff``): operator-only, separate from
the chat settings clients see."""

from .. import chat_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

_TIME = {"type": ["string", "null"], "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"}
VIEW = _object({"enabled": _BOOL, "time": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
                "cutoff": {"oneOf": [{"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
                                     _object({"kind": {"const": "end_of_day"}}, closed=True)]},
                "cutoff_source": {"type": "string", "enum": ["stored", "derived"]}},
               closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("morning_brief.get", "POST", "/api/v1/morning_brief/get",
              "Read the morning brief: whether it is on, its local send time and catch-up cutoff.",
              "Read the morning brief: whether it is on, its local send time and catch-up cutoff. "
              "It is off by default, with send time 09:15 and cutoff 12:00. cutoff_source is stored or derived. "
              'An end-of-day cutoff is {"kind":"end_of_day"}, meaning local 24:00, exclusive.',
              _object({}, closed=True), VIEW, operator_only=True,
              level=ActionLevel.INSIDE),  # Reads Looper's own morning-brief setting.
    Operation("morning_brief.update", "POST", "/api/v1/morning_brief/update",
              "Turn the morning brief on or off, or set its local send time and catch-up cutoff.",
              "Turn the morning brief on or off, or set its local send time and catch-up cutoff. "
              "Cutoff must be later than send time. Null cutoff derives noon for sends before noon, otherwise "
              "send time plus three hours, capped at local 24:00 (exclusive), read back as "
              '{"kind":"end_of_day"}. Other null fields restore defaults. Off sends nothing.',
              _object({"enabled": {"type": ["boolean", "null"]}, "time": _TIME, "cutoff": _TIME}, required=[], closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # Stores only Looper's own morning-brief setting; sends nothing.
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "morning_brief.update":
        return None
    try:
        checked = chat_settings.checked_morning_brief(arguments)
    except ValueError as exc:
        from ..ops import ErrorCode, OpError

        raise OpError(ErrorCode.INVALID, str(exc)) from None
    enabled, when, cutoff = checked["enabled"], checked["time"], checked["cutoff"]
    if isinstance(cutoff, dict):
        cutoff = "end of local day (24:00, exclusive)"
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
