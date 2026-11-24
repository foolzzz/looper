"""Operator-only profile reads, relative vault source aliases and resident injection setting."""

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
VIEW = _object({"enabled": _BOOL}, closed=True)
OPERATIONS = (
    Operation("profile.get", "POST", "/api/v1/profile/get",
              "Read the compiled operator profile.",
              "Read the compiled operator profile. The source is the knowledge vault's profile_source note. "
              "An unavailable or invalid note produces an empty profile, never a cached value.",
              _object({}, closed=True), PROFILE, risk="read", operator_only=True, level=ActionLevel.INSIDE),
    Operation("profile_source.get", "POST", "/api/v1/profile_source/get",
              "Read the operator profile source file.",
              "Read the operator profile source file. Alias for knowledge_vault.profile_source, "
              "relative to the vault root, default 关于我.md.",
              _object({}, closed=True), SOURCE, risk="read", operator_only=True, level=ActionLevel.INSIDE),
    Operation("profile_source.update", "POST", "/api/v1/profile_source/update",
              "Set the operator profile source file.",
              "Set the operator profile source file. Alias for knowledge_vault.profile_source; the relative "
              "Markdown path must resolve to a regular file inside the vault, including through symlinks. "
              "Null restores 关于我.md. The independent resident injection switch is unchanged.",
              _object({"source": _NULL_STR}, closed=True), SOURCE,
              risk="high", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
    Operation("profile_settings.get", "POST", "/api/v1/profile_settings/get",
              "Read the Manager resident profile setting.",
              "Read the Manager resident profile setting. Injection is disabled by default.",
              _object({}, closed=True), VIEW, operator_only=True, level=ActionLevel.INSIDE),
    Operation("profile_settings.update", "POST", "/api/v1/profile_settings/update",
              "Set the Manager resident profile setting.",
              "Set the Manager resident profile setting. Null restores the disabled default. "
              "The source file remains configured in knowledge_vault.profile_source.",
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
)


def _invalid(exc: operator_profile.ProfileError) -> OpError:
    return OpError(ErrorCode.INVALID, str(exc))


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id == "profile_settings.update":
        return (f"Set {operator_profile.ENABLED_KEY} to {str(arguments['enabled'] is True).lower()}? "
                "This controls injection of the compiled profile into the Manager's private resident content.")
    if operation.operation_id != "profile_source.update":
        return None
    try:
        source = await operator_profile.bound_source(operator_profile.checked_update(arguments))
    except operator_profile.ProfileError as exc:
        raise _invalid(exc) from None
    return (f"Set knowledge_vault.profile_source to {source}? "
            "This changes the source file without changing the resident injection switch.")


async def run(server, operation, arguments, principal, headers):
    try:
        if operation.operation_id == "profile.get":
            return await operator_profile.profile_view()
        if operation.operation_id == "profile_source.get":
            return await operator_profile.source_view()
        if operation.operation_id == "profile_settings.get":
            return await operator_profile.settings_view()
        if operation.operation_id == "profile_settings.update":
            return await operator_profile.update_enabled(arguments["enabled"])
        return await operator_profile.update_source(operator_profile.checked_update(arguments))
    except operator_profile.ProfileError as exc:
        raise _invalid(exc) from None
