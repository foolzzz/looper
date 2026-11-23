"""The event triage switch is separate from agent-accessible chat settings."""

from .. import triage_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _INT, _object

VIEW = _object({"enabled": _BOOL, "max_turns_per_day": _INT}, closed=True)
_MAX_TURNS = {"type": ["integer", "null"], "minimum": triage_settings.MIN_MAX_TURNS,
              "maximum": triage_settings.MAX_MAX_TURNS}
_UPDATE = _object({"enabled": {"type": ["boolean", "null"]}, "max_turns_per_day": _MAX_TURNS}, [], closed=True)
_UPDATE["anyOf"] = [{"required": ["enabled"]}, {"required": ["max_turns_per_day"]}]
RESULTS = {}
# Both operations stay inside Looper: they read/store a local setting, without a model call or delivery.
OPERATIONS = (
    Operation("triage_settings.get", "POST", "/api/v1/triage_settings/get",
              "Read the event triage setting.",
              "Read the event triage setting. It is disabled by default; confirmed subscriptions store events when enabled. "
              "max_turns_per_day is how many triage turns the Manager takes a day (default 12); later events go to the brief.",
              _object({}, closed=True), VIEW, operator_only=True, level=ActionLevel.INSIDE),
    Operation("triage_settings.update", "POST", "/api/v1/triage_settings/update",
              "Set the event triage setting.",
              "Set the event triage setting. Null restores the default; a field left out stays as it is. When enabled, confirmed "
              "triage subscriptions run and store events, and important ones are handed to the Manager, which may tell the "
              "operator's chat (at most max_turns_per_day turns a day, 1 to 100, default 12).",
              _UPDATE, VIEW,
              risk="high", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "triage_settings.update":
        return None
    parts = []
    if "enabled" in arguments:
        parts.append(f"Set {triage_settings.KEY} to {str(bool(arguments['enabled'])).lower()}? When on, confirmed triage "
                     "subscriptions run and store events, and important events reach the Manager and may reach your "
                     "direct chat; leave it off until outbound review and delivery are on.")
    if "max_turns_per_day" in arguments:
        value = arguments["max_turns_per_day"]
        parts.append(f"Set {triage_settings.MAX_TURNS_KEY} to "
                     f"{triage_settings.DEFAULT_MAX_TURNS if value is None else value}? Triage turns past it in a local "
                     "day go to the morning brief.")
    return " ".join(parts)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "triage_settings.get":
        return await triage_settings.view()
    return await triage_settings.update(arguments.get("enabled", triage_settings.UNSET),
                                        max_turns=arguments.get("max_turns_per_day", triage_settings.UNSET))
