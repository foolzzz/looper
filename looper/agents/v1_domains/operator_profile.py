"""Operator-only profile reads and the source folder. Nothing here is a Manager tool.

Levels (outbound-review RFC section 4): all three are ``inside``. ``profile.get`` reads notes the operator
named and stores the compiled block in Looper; it sends nothing. The source operations only store or clear
a path in ``system_settings``.
"""

from .. import operator_profile
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _BOOL, _INT, _NULL_STR, _STR, _array, _object
from ..ops import ErrorCode, OpError

_ITEM = {"type": "object", "properties": {
    "value": _STR, "path": _STR, "heading": _STR, "line": _INT, "kind": _STR, "truncated": _BOOL},
         "required": ["value", "path", "heading", "line"], "additionalProperties": False}
_FIELD = {**_ITEM, "type": ["object", "null"]}
_FIELDS = _object({
    "name": _FIELD, "address": _FIELD, "working_hours": _FIELD, "time_zone": _FIELD, "reply_language": _FIELD,
    "communication_preferences": _array(_ITEM), "standing_rules": _array(_ITEM),
}, closed=True)
_CITE = _object({"field": _STR, "path": _STR, "heading": _STR, "line": _INT}, closed=True)
_SKIP = _object({"path": _STR, "reason": _STR}, closed=True)
PROFILE = _object({
    "configured": _BOOL, "version": {"type": ["integer", "null"]}, "cap": _INT, "text": _STR,
    "fields": _FIELDS, "omitted": _array(_CITE), "skipped": _array(_SKIP), "sources": _array(_STR),
}, closed=True)
SOURCE = _object({"source": _NULL_STR}, closed=True)
RESULTS: dict = {}
OPERATIONS = (
    Operation("profile.get", "POST", "/api/v1/profile/get",
              "Read the compiled operator profile.",
              "Read the compiled operator profile. It is empty until a source folder is set, and nothing is "
              "injected into the Manager.",
              _object({}, closed=True), PROFILE, risk="read", operator_only=True, level=ActionLevel.INSIDE),
    Operation("profile_source.get", "POST", "/api/v1/profile_source/get",
              "Read the operator profile source folder.",
              "Read the operator profile source folder. It is unset by default, and then nothing is compiled.",
              _object({}, closed=True), SOURCE, risk="read", operator_only=True, level=ActionLevel.INSIDE),
    Operation("profile_source.update", "POST", "/api/v1/profile_source/update",
              "Set the operator profile source folder.",
              "Set the operator profile source folder. An absolute directory inside the configured knowledge "
              "vault is compiled on the next read; a symlink is followed and must still resolve inside that "
              "vault. Null clears the folder and nothing is compiled. Nothing is injected into the Manager.",
              _object({"source": _NULL_STR}, closed=True), SOURCE,
              risk="high", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
)


def _invalid(exc: operator_profile.ProfileError) -> OpError:
    return OpError(ErrorCode.INVALID, str(exc))


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "profile_source.update":
        return None
    try:
        value = operator_profile.checked_update(arguments)
        shown = "unset (nothing compiled)" if value is None else await operator_profile.bound_directory(value)
    except operator_profile.ProfileError as exc:
        raise _invalid(exc) from None
    return (f"Set {operator_profile.SOURCE_KEY} to {shown}? This stores the folder. "
            "Nothing is injected into the Manager.")


async def run(server, operation, arguments, principal, headers):
    try:
        if operation.operation_id == "profile.get":
            return await operator_profile.profile_view()
        if operation.operation_id == "profile_source.get":
            return await operator_profile.source_view()
        return await operator_profile.update_source(operator_profile.checked_update(arguments))
    except operator_profile.ProfileError as exc:
        raise _invalid(exc) from None
