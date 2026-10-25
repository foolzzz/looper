"""Full dashboard settings projections and the existing compact settings tool results."""

from .. import scheduler_settings, schedules, vault, wait_service, waits
from ..api_v1 import _array, _object, _STR, _NULL_STR, _INT, _BOOL, _OBJ
from ..ops import Call, Turn
from ..ops.config import answered
from ..ops.settings import _confirming
from .global_settings import _operation

SCHEDULER_SETTING = _object({
    "value": {"type": ["integer", "null"]},
    "source": {"type": "string", "enum": ["setting", "env", "built-in"]},
    "built_in": {"type": ["integer", "null"]}, "minimum": _INT, "maximum": _INT,
}, ["value", "source", "built_in", "minimum"], closed=True)
SCHEDULER_SETTINGS = _object({
    "min_interval_seconds": _object({**SCHEDULER_SETTING["properties"], "gated_value": _INT},
        [*SCHEDULER_SETTING["required"], "gated_value"], closed=True),
    "run_timeout_seconds": SCHEDULER_SETTING,
}, closed=True)
WAIT_SETTING = _object({
    "value": _INT, "default": _INT, "min": _INT, "max": _INT,
    "source": {"type": "string", "enum": ["setting", "built-in"]},
}, closed=True)
WAIT_SETTINGS = _object({name: WAIT_SETTING for name in ("max_wait_seconds", "min_check_seconds")}, closed=True)
VAULT_SETTINGS = _object({
    "configured": _BOOL, "path": _NULL_STR, "writable": _array(_STR), "rules_file": _NULL_STR,
    "git": _BOOL, "problem": _NULL_STR,
}, closed=True)
SCHEMAS = {"SchedulerSetting": SCHEDULER_SETTING, "SchedulerSettings": SCHEDULER_SETTINGS,
           "WaitSetting": WAIT_SETTING, "WaitSettings": WAIT_SETTINGS, "VaultSettings": VAULT_SETTINGS}

_COMPACT_VAULT = _object({name: schema for name, schema in VAULT_SETTINGS["properties"].items()
                          if name != "git"}, closed=True)
RESULTS = {
    "settings.scheduler": _object({"changes": _array(_object({
        "setting": _STR, "before": _STR, "after": _STR}, closed=True))}, closed=True),
    "settings.waits": _object({"changes": _array(_object({
        "setting": _STR, "before": WAIT_SETTING, "after": WAIT_SETTING}, closed=True))}, closed=True),
    "settings.knowledge_vault": _object({"knowledge_vault": VAULT_SETTINGS}, closed=True),
    "settings.view": {"oneOf": [
        _object({"scheduler": {"type": "object", "additionalProperties": _STR}, "waits": WAIT_SETTINGS,
                 "shared_defaults": _OBJ, "roles": _OBJ, "knowledge_vault": _COMPACT_VAULT,
                 "manager_session": _OBJ},
                ["scheduler", "waits", "shared_defaults", "roles", "knowledge_vault"], closed=True),
        _object({"project": _STR, "project_layer": _OBJ, "roles": _OBJ,
                 "allowed_profiles": _array(_STR), "allowed_profiles_note": _NULL_STR}, closed=True),
    ]},
}


def _seconds(minimum, maximum=None):
    return {"type": ["integer", "null"], "minimum": minimum,
            **({"maximum": maximum} if maximum is not None else {})}


_SCHEDULER_UPDATE = _object({
    "min_interval_seconds": _seconds(1),
    "run_timeout_seconds": _seconds(1, scheduler_settings.RUN_TIMEOUT_MAX_SECONDS),
}, [], closed=True) | {"minProperties": 1}
_WAIT_UPDATE = _object({
    "max_wait_seconds": _seconds(*waits.MAX_WAIT_RANGE),
    "min_check_seconds": _seconds(*waits.MIN_CHECK_RANGE),
}, [], closed=True) | {"minProperties": 1}
_VAULT_UPDATE = _object({"path": _NULL_STR, "writable": _array(_STR), "rules_file": _NULL_STR},
                        ["path"], closed=True)

OPERATIONS = (
    _operation("scheduler_settings.get", "Read scheduler settings and their effective sources.",
               _object({}, closed=True), SCHEDULER_SETTINGS),
    _operation("scheduler_settings.update", "Update scheduler settings; null restores the fallback.",
               _SCHEDULER_UPDATE, SCHEDULER_SETTINGS, "high", True),
    _operation("wait_settings.get", "Read wait limits and their defaults.",
               _object({}, closed=True), WAIT_SETTINGS),
    _operation("wait_settings.update", "Update wait limits; null restores the built-in default.",
               _WAIT_UPDATE, WAIT_SETTINGS, "high", True),
    _operation("knowledge_vault.get", "Read knowledge vault configuration and any problem.",
               _object({}, closed=True), VAULT_SETTINGS),
    _operation("knowledge_vault.update", "Replace knowledge vault configuration; a null path clears it.",
               _VAULT_UPDATE, VAULT_SETTINGS, "high", True),
)


def _services(family):
    # Resolve the current service functions at dispatch, rather than retaining import-time copies.
    return {
        "scheduler_settings": ("scheduler", scheduler_settings.settings_view, schedules.update_scheduler_settings),
        "wait_settings": ("waits", wait_service.settings_view, wait_service.update_settings),
        "knowledge_vault": ("knowledge_vault", vault.settings_view, vault.update_settings),
    }[family]


async def impact(server, operation, arguments, principal, headers):
    family = operation.operation_id.split(".")[0]
    action = _services(family)[0]
    values = arguments
    # The service also permits empty optional fields when clearing the vault.
    if (family == "knowledge_vault" and arguments.get("path") is None
            and arguments.get("writable", []) == [] and arguments.get("rules_file") in (None, "")):
        values = {"path": None}
    # These existing risk helpers only read arguments; no Manager turn or platform is needed.
    return await _confirming(Call({"action": action, action: values}, Turn(channel=None, chat=None), None))


async def run(server, operation, arguments, principal, headers):
    family, action = operation.operation_id.split(".")
    _, view, update = _services(family)
    if action == "get":
        return await view()
    # Recheck filesystem and current limits at execution, including after client confirmation.
    await impact(server, operation, arguments, principal, headers)
    return await answered(update(arguments))
