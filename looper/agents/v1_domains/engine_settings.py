"""Operator-only switches of the task engine's reliability layer (the engine reliability RFC, section 9).

``off`` is the default; ``record`` classifies only; ``act`` enables bounded recovery and operator holds.
"""

from looper.db import system_settings
from .. import failures, failure_handling
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _object
from ..ops import ErrorCode, OpError
from ..ops.service import _plain

_MODE = {"type": "string", "enum": [mode.value for mode in failures.FailureTyping]}
SETTINGS = _object({"failure_typing": _object({
    "value": _MODE, "source": {"type": "string", "enum": ["setting", "built-in"]},
    "built_in": {"type": "string", "enum": [failures.FailureTyping.OFF.value]}}, closed=True)}, closed=True)
SCHEMAS = {"EngineSettings": SETTINGS}
RESULTS = {}
_UPDATE = _object({"failure_typing": {"type": ["string", "null"], "enum": [*_MODE["enum"], None],
                                      "description": "off, record, or act; null restores the built-in off"}},
                  closed=True)


def _operation(name, summary, request, risk):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), summary, summary, request, SETTINGS,
                     risk=risk, operator_only=True, confirms=risk != "read",
                     level=ActionLevel.INSIDE)  # reads or changes only Looper's own engine settings


OPERATIONS = (
    _operation("engine_settings.get", "Read the task engine's reliability switches.", _object({}, closed=True), "read"),
    _operation("engine_settings.update", "Set the task engine's reliability switches.", _UPDATE, "write"),
)


async def settings():
    stored = (await system_settings.get_settings([failures.SETTING_KEY])).get(failures.SETTING_KEY)
    return {"failure_typing": {"value": (await failures.typing_mode()).value,
                               "source": "built-in" if stored is None else "setting",
                               "built_in": failures.FailureTyping.OFF.value}}


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "engine_settings.update":
        if "failure_typing" not in arguments:
            raise OpError(ErrorCode.INVALID, "provide failure_typing (off, record, act, or null for the built-in off)")
        value = arguments["failure_typing"]
        service = failure_handling.get_service()
        if service is None:
            raise OpError(ErrorCode.UNAVAILABLE, "failure handling is not ready")
        try:
            await service.set_mode(value)
        except failure_handling.FailureHandlingError as exc:
            raise OpError(ErrorCode(exc.code), str(exc)) from None
        except Exception:
            raise OpError(ErrorCode.UNAVAILABLE, "engine mode changed but recovery could not complete; retry the operation") from None
    try:
        return _plain(await settings())
    except Exception:
        raise OpError(ErrorCode.UNAVAILABLE, "engine settings database unavailable") from None


async def impact(server, operation, arguments, principal, headers):
    before = (await settings())["failure_typing"]["value"]
    after = arguments.get("failure_typing") or "off"
    return (f"Change engine failure handling from {before} to {after}? "
            "Act enables automatic bounded retries and operator holds. Leaving act releases existing "
            "failure waits to their original Planner failure path; failure history is retained.")
