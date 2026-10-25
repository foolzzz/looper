"""Full dashboard profiles and previews, alongside unchanged compact Manager results."""

from .. import profile_registry, prompt_preview
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _BOOL, _OBJ
from ..ops.config import answered, profile_deletion_impact
from .global_settings import ROLE
from .registry import AGENT_PROFILE, PROFILE_MODEL, PROFILE_PROVIDER, RUNTIME_TYPE

_FIELDS = {"name": _STR, "runtime_id": _STR, "model_id": _STR, "effort": _NULL_STR,
           "agent_teams": _BOOL, "custom_prompt": _STR}
PROFILE_PATCH = {**_object(_FIELDS, [], closed=True), "minProperties": 1,
                 "anyOf": [{"required": [name]} for name in _FIELDS]}
PROFILE_RESULT = _object({"profile": AGENT_PROFILE, "warnings": _array(_STR)}, closed=True)
COMPACT_PROFILE = _object({
    "profile_id": _STR, "name": _STR, "runtime": _STR, "provider": PROFILE_PROVIDER,
    "model_id": _STR, "model": PROFILE_MODEL, "effort": _NULL_STR, "agent_teams": _BOOL,
    "custom_prompt": _STR, "roles": _array(ROLE), "builtin": _BOOL, "used_by": _NULL_STR,
}, closed=True)
PROMPT_BLOCK = _object({"name": _STR, "owner": {"type": "string", "enum": ["looper", "runtime"]},
                        "text": _NULL_STR, "note": _STR}, closed=True)
PROMPT_PREVIEW = _object({
    "profile": _object({"id": _STR, "name": _STR}, closed=True), "role": _STR,
    "runtime": _STR, "system_prompt_mode": _STR, "role_system_prompt_mode": _STR,
    "system_prompt_mode_source": _STR, "role_system_prompt_mode_source": _STR,
    "custom_prompt_set": _BOOL, "looper_prompt_sha256": _STR, "prompt_layers": _OBJ,
    "blocks": _array(PROMPT_BLOCK), "notes": _array(_STR),
}, closed=True)
SCHEMAS = {"ProfilePatch": PROFILE_PATCH, "CompactProfile": COMPACT_PROFILE,
           "PromptPreview": PROMPT_PREVIEW, "PromptBlock": PROMPT_BLOCK}
_COMPACT_RESULT = _object({"profile": COMPACT_PROFILE, "warnings": _array(_STR)}, closed=True)
RESULTS = {
    "agent_profiles.list": _object({
        "profiles": _array(COMPACT_PROFILE),
        "runtimes": _array(_object({"runtime_id": _STR, "name": _STR, "type": _STR,
                                    "roles": _array(ROLE)}, closed=True)),
        "runtime_types": _array(RUNTIME_TYPE),
        "models": _array(_object({**PROFILE_MODEL["properties"], "provider_name": _STR}, closed=True)),
    }, closed=True),
    "agent_profiles.create": _COMPACT_RESULT,
    "agent_profiles.update": _COMPACT_RESULT,
    "agent_profiles.delete": _object({"deleted": _STR}, closed=True),
}


def _op(action, request, result, risk="write"):
    name = f"profiles.{action}"
    description = {"create": "Create an Agent Profile.", "update": "Patch an Agent Profile.",
                   "delete": "Delete an unused Agent Profile.",
                   "prompt_preview": "Preview an Agent Profile's prompt without starting a runtime or writing files."}[action]
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=risk == "high")


OPERATIONS = (
    _op("create", _object(_FIELDS, ["name", "runtime_id", "model_id"], closed=True), PROFILE_RESULT),
    _op("update", _object({"profile_id": _STR, "fields": PROFILE_PATCH}, closed=True), PROFILE_RESULT),
    _op("delete", _object({"profile_id": _STR}, closed=True), _object({"deleted": _STR}, closed=True), "high"),
    _op("prompt_preview", _object({"profile_id": _STR, "role": ROLE, "project_id": _STR},
                                  ["profile_id", "role"], closed=True), PROMPT_PREVIEW, "read"),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "profiles.delete":
        return None
    row = await answered(profile_registry.deletable_profile(arguments["profile_id"]))
    return profile_deletion_impact(row)


async def run(server, operation, arguments, principal, headers):
    name = operation.operation_id
    if name == "profiles.prompt_preview":
        return await answered(prompt_preview.preview(arguments["profile_id"], arguments["role"],
                                                     arguments.get("project_id")))
    if name == "profiles.create":
        return await answered(profile_registry.create_profile(arguments))
    if name == "profiles.update":
        return await answered(profile_registry.update_profile(arguments["profile_id"], arguments["fields"]))
    # The service repeats every usage check at execution, including after client confirmation.
    return await answered(profile_registry.delete_profile(arguments["profile_id"]))
