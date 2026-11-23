"""The idle check switch is operator-only and separate from the agent-accessible chat settings."""

from .. import idle_check_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _object

VIEW = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
# Both stay inside Looper: they read or store one local setting, and call no model and send nothing.
OPERATIONS = (
    Operation("idle_check_settings.get", "POST", "/api/v1/idle_check_settings/get",
              "Read the idle check setting.",
              "Read the idle check setting. It is off by default: while it is off the Manager never checks.",
              _object({}, closed=True), VIEW, operator_only=True, level=ActionLevel.INSIDE),
    Operation("idle_check_settings.update", "POST", "/api/v1/idle_check_settings/update",
              "Set the idle check setting.",
              "Set the idle check setting. Null restores the default, off. Turning it on needs delivery.enabled, "
              "attention.enabled and manager.lanes.enabled on first, and answers conflict naming those that are off. "
              "When enabled, the running default Manager checks after 30 idle minutes when new items need you; "
              "checks stop while a prerequisite is off or the server drains.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "idle_check_settings.update":
        return None
    if arguments["enabled"]:
        return ("Turn idle checks on? When the default Manager has been idle for 30 minutes or more and something "
                "new needs you, it may say one thing in your direct chat: at most 4 times a day, never at night. "
                "If you write while a check runs, the check is interrupted; if the Manager's runtime cannot be "
                "interrupted, your message waits up to about 2 minutes.")
    return (f"Set {idle_check_settings.KEY} to "
            f"{'the built-in default, off' if arguments['enabled'] is None else 'false'}? "
            "The Manager makes no new idle checks.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "idle_check_settings.get":
        return await idle_check_settings.view()
    return await idle_check_settings.update(arguments["enabled"])
