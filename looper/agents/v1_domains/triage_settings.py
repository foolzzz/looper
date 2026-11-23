"""The event triage switch is separate from agent-accessible chat settings."""

from .. import triage_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
# Both operations stay inside Looper: they read/store a local setting, without a model call or delivery.
OPERATIONS = (
    Operation("triage_settings.get", "POST", "/api/v1/triage_settings/get",
              "Read the event triage setting.",
              "Read the event triage setting. It is disabled by default; execution is delivered separately.",
              _object({}, closed=True), VIEW, operator_only=True, level=ActionLevel.INSIDE),
    Operation("triage_settings.update", "POST", "/api/v1/triage_settings/update",
              "Set the event triage setting.",
              "Set the event triage setting. Null restores the disabled default. This foundation cannot run triage.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id == "triage_settings.update":
        return (f"Set {triage_settings.KEY} to {str(bool(arguments['enabled'])).lower()}? "
                "This stores the switch only; triage execution is not available yet.")
    return None


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "triage_settings.get":
        return await triage_settings.view()
    return await triage_settings.update(arguments["enabled"])
