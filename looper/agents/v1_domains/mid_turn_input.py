"""Operator-only control of new messages joining the default Manager's running user turn."""

from .. import chat_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("mid_turn_input.get", "POST", "/api/v1/mid_turn_input/get",
              "Read the mid-turn input setting.",
              "Read the mid-turn input setting. It is disabled by default.",
              _object({}, closed=True), VIEW, operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's own merge setting
    Operation("mid_turn_input.update", "POST", "/api/v1/mid_turn_input/update",
              "Set the mid-turn input setting.",
              "Set the mid-turn input setting. On, eligible operator messages join a running user turn "
              "on a supporting runtime. Null restores the disabled default.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # stores only Looper's merge setting; sends no message
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "mid_turn_input.update":
        return None
    value = arguments["enabled"]
    enabled = chat_settings.DEFAULT_MID_TURN_INPUT_ENABLED if value is None else value
    return (f"Set {chat_settings.MID_TURN_INPUT_KEY} to {str(enabled).lower()}"
            f"{' (built-in default)' if value is None else ''}? "
            "On, eligible new operator messages join the default Manager's running user turn on a "
            "supporting runtime; off, they queue for the next turn.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "mid_turn_input.get":
        return chat_settings.mid_turn_input_view()
    return await chat_settings.update_mid_turn_input(arguments["enabled"])
