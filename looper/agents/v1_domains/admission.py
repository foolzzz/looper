"""Operator controls for account admission and quota reset checks."""

from .. import admission, admission_settings
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _STR, _object
from ..ops import ErrorCode, PublicOpError
from ...db import profile_registry

SETTINGS = _object({"enabled": _BOOL}, closed=True)
ADMISSION = _object(
    {
        "state": {"enum": ["open", "frozen", "probing"]},
        "until": {"type": ["string", "null"]},
        "until_known": _BOOL,
        "waiting": _object(
            {
                lane: {"type": "integer", "minimum": 0}
                for lane in ("interactive", "user_task", "recovery", "background")
            },
            closed=True,
        ),
        "running": {"type": "integer", "minimum": 0},
        "limit": {"type": ["integer", "null"]},
    },
    closed=True,
)
RESULTS = {}
SCHEMAS = {"ProviderAdmission": ADMISSION}
OPERATIONS = (
    Operation(
        "admission_settings.get",
        "POST",
        "/api/v1/admission_settings/get",
        "Read whether account admission is enabled.",
        "Read whether account admission is enabled. Disabled by default; operator only.",
        _object({}, closed=True),
        SETTINGS,
        risk="read",
        operator_only=True,
        level=ActionLevel.INSIDE,
    ),  # reads only Looper's scheduling setting
    Operation(
        "admission_settings.update",
        "POST",
        "/api/v1/admission_settings/update",
        "Enable or disable account admission.",
        "Enable or disable account admission. Enforces account slots, lanes and quota freezes for Workers, "
        "Reviewers, consults, processors and nested runs. Planner and Manager turns are not admitted yet. "
        "Requires failure typing; null restores the disabled default.",
        _object({"enabled": {"type": ["boolean", "null"]}}, closed=True),
        SETTINGS,
        risk="high",
        confirms=True,
        operator_only=True,
        level=ActionLevel.INSIDE,
    ),  # changes only Looper's account scheduling switch
    Operation(
        "providers.limits_clear",
        "POST",
        "/api/v1/providers/limits_clear",
        "Clear a provider's quota freeze and check admission now.",
        "Clear a provider's quota freeze and check admission now. Records an operator transition to open.",
        _object({"provider_id": _STR}, closed=True),
        ADMISSION,
        risk="high",
        confirms=True,
        operator_only=True,
        level=ActionLevel.INSIDE,
    ),  # changes only Looper's stored account admission state
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id == "providers.limits_clear":
        return f"Clear the quota freeze for {arguments['provider_id']} and allow queued runs to try again?"
    return (
        f"Set {admission_settings.KEY} to {str(bool(arguments['enabled'])).lower()}? "
        "Enforces account slots, priority lanes, background tightening and quota freezes for Workers, "
        "Reviewers, consults, processors and nested runs. Planner and Manager turns are not admitted yet. "
        "Disabling releases all admission waiters immediately."
    )


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "admission_settings.get":
        return admission_settings.view()
    if operation.operation_id == "admission_settings.update":
        return await admission_settings.update(arguments["enabled"])
    provider_id = arguments["provider_id"]
    if await profile_registry.get_provider(provider_id) is None:
        raise PublicOpError(ErrorCode.NOT_FOUND, "provider not found")
    await admission.transition(provider_id, admission.OperatorClear())
    return await admission.snapshot(provider_id)
