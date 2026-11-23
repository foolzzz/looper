"""Operator-only quota observations and their opt-in collection switch."""
from .. import limit_signals
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _BOOL, _STR, _INT, _NULL_STR
from ..ops import ErrorCode, PublicOpError

_NUMBER = {"type": ["number", "null"]}
_NULL_INT = {"type": ["integer", "null"]}
WINDOW = _object({"window_name": _STR, "status": {"enum": ["allowed", "warning", "rejected"]},
    "utilization": _NUMBER, "resets_at": _NULL_STR, "window_minutes": _NULL_INT, "source": _STR,
    "record_id": _NULL_INT, "observed_at": _STR}, closed=True)
DAY = _object({"day": _STR, "runs": _INT, "input_tokens": _NULL_INT, "output_tokens": _NULL_INT,
    "cache_read_input_tokens": _NULL_INT, "cache_creation_input_tokens": _NULL_INT,
    "cost_usd": _NUMBER, "quota_failures": _NULL_INT}, closed=True)
LIMITS = _object({"enabled": _BOOL, "providers": _array(_object({"provider_id": _STR, "name": _STR,
    "cost_basis": {"enum": ["api_equivalent", "reported"]}, "admission": {"type": "null"},
    "windows": _array(WINDOW), "usage": _array(DAY)}, closed=True))}, closed=True)
SETTINGS = _object({"enabled": _BOOL}, closed=True)
RESULTS = {}
SCHEMAS = {"ProviderLimitWindow": WINDOW, "ProviderDailyUsage": DAY, "ProviderLimits": LIMITS}
OPERATIONS = (
    Operation("providers.limits", "POST", "/api/v1/providers/limits",
        "Read provider limit windows and seven days of usage; operator only.",
        "Read provider limit windows and seven days of usage; operator only.",
        _object({"provider_id": _STR}, [], closed=True), LIMITS, risk="read", operator_only=True,
        level=ActionLevel.INSIDE),  # reads stored observations and usage; no outside effect
    Operation("limit_signals_settings.get", "POST", "/api/v1/limit_signals_settings/get",
        "Read whether provider limit signal collection is enabled.",
        "Read whether provider limit signal collection is enabled.", _object({}, closed=True), SETTINGS,
        risk="read", operator_only=True,
        level=ActionLevel.INSIDE),  # reads only Looper's collection setting
    Operation("limit_signals_settings.update", "POST", "/api/v1/limit_signals_settings/update",
        "Enable or disable provider limit observations; null restores the default (off).",
        "Enable or disable provider limit observations; null restores the default (off).",
        _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), SETTINGS,
        risk="high", confirms=True, operator_only=True,
        level=ActionLevel.INSIDE),  # stores only Looper's collection switch
)


async def impact(server, operation, arguments, principal, headers):
    return f"Set {limit_signals.KEY} to {str(bool(arguments['enabled'])).lower()}? Collection only; scheduling is unchanged."


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "providers.limits":
        try:
            return await limit_signals.limits(arguments.get("provider_id"))
        except LookupError:
            raise PublicOpError(ErrorCode.NOT_FOUND, "provider not found") from None
    if operation.operation_id == "limit_signals_settings.get":
        return await limit_signals.settings_view()
    return await limit_signals.update_settings(arguments)
