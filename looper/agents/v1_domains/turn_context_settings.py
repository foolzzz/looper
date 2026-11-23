"""Operator-only turn-context settings, separate from client-visible chat controls."""

from .. import chat_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _NULL_STR, _STR, _array, _object
from ..ops import ErrorCode, OpError

VIEW = _object({"turn_context_enabled": _BOOL, "timezone": _NULL_STR, "server_timezone": _STR,
                "reply_language": {"type": "string", "enum": ["zh-CN", "en"]}}, closed=True)
UPDATED = _object({**VIEW["properties"], "missing_operator_ids": _array(_object({
    "channel_id": _STR, "channel_type": _STR, "name": _STR}, closed=True))},
    required=VIEW["required"], closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("turn_context_settings.get", "POST", "/api/v1/turn_context_settings/get",
              "Read turn context, timezone and reply language settings.",
              "Read turn context, timezone and reply language settings. Turn context is disabled by default.",
              _object({}, closed=True), VIEW, operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's own turn-context settings
    Operation("turn_context_settings.update", "POST", "/api/v1/turn_context_settings/update",
              "Change named turn context settings; null restores defaults.",
              "Change named turn context settings; null restores defaults. Enabling lists bots without operator IDs; "
              "Manager wiring is separate.",
              _object({"turn_context_enabled": {"type": ["boolean", "null"]}, "timezone": _NULL_STR,
                       "reply_language": {"type": ["string", "null"], "enum": ["zh-CN", "en", None]}},
                      [], closed=True), UPDATED, risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes only Looper's own turn-context settings
)


def _checked(arguments):
    try:
        return chat_settings.checked_turn_context(arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "turn_context_settings.update":
        return None
    values = _checked(arguments)
    changes = []
    if "turn_context_enabled" in values:
        enabled = values["turn_context_enabled"] is True
        changes.append(f"Set manager.turn_context.enabled to {str(enabled).lower()}? "
                       "The turn context and speaker authorization are awaiting Manager integration.")
        if enabled:
            from ..channels.operator_ids import missing

            bots = await missing()
            if bots:
                changes.append("These bots have no operator id: " + ", ".join(bot.name for bot in bots) +
                               ". Nobody counts as the operator there; actions needing operator authorization "
                               "will be refused when turn context is active.")
    for field in ("timezone", "reply_language"):
        if field in values:
            changes.append(f"Set user.{field} to {values[field] or 'the default'}? Applies to turn context only.")
    return " ".join(changes)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "turn_context_settings.get":
        return chat_settings.turn_context_view()
    return await chat_settings.update_turn_context(_checked(arguments))
