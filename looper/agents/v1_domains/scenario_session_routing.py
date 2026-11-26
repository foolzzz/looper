"""Operator control of how a scenario Manager divides its resident sessions."""

from .. import managers_api
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _object
from ..ops.config import answered

_ID = {"type": "string", "minLength": 1}
_ROUTING = {"type": "string", "enum": [mode.value for mode in managers_api.ScenarioSessionRouting]}
VIEW = _object({"manager_id": _ID, "session_routing": _ROUTING}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("scenario_session_routing.get", "POST", "/api/v1/scenario_session_routing/get",
              "Read a scenario Manager's session routing.",
              "Read a scenario Manager's session routing. The default is off; the default Manager uses its global switch.",
              _object({"manager_id": _ID}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # Reads only the stored Manager definition.
    Operation("scenario_session_routing.update", "POST", "/api/v1/scenario_session_routing/update",
              "Set a scenario Manager's session routing.",
              "Set a scenario Manager's session routing. Null restores off. Per-chat sessions start fresh without history migration.",
              _object({"manager_id": _ID, "session_routing": {
                  "type": ["string", "null"], "enum": [*_ROUTING["enum"], None]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # Stores only Looper's routing policy; sends no message.
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "scenario_session_routing.update":
        return None
    value = arguments["session_routing"] or "off"
    effect = ("Each group's next turn starts a fresh session; existing history stays in main."
              if value == "per_chat" else
              "New turns return to main; group sessions remain until idle reclaim.")
    return f"Set Manager {arguments['manager_id']} session_routing to {value}? {effect}"


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "scenario_session_routing.get":
        return await answered(managers_api.scenario_session_routing_view(arguments["manager_id"]))
    return await answered(managers_api.update_scenario_session_routing(
        arguments["manager_id"], arguments["session_routing"]))
