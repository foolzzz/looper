"""Global role configuration and complete inherited dashboard settings.

These DTOs are shared by settings domains; the compact Manager tools stay unchanged.
"""

from ...db import role_configs
from .. import agent_settings, api_v1
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL, _OBJ, _NULL_OBJ
from ..ops import ErrorCode, OpError
from ..ops.config import answered
from ..ops.settings import _Place, _checked, _layers, _raises_a_cap, _shown_value, LIMIT_NOTE

ROLE = {"type": "string", "enum": [role.value for role in agent_settings.AgentType]}
SETTING_LAYER = {"type": "string", "enum": ["builtin", "system", "role", "project", "role_project"]}
SETTING_SOURCE = {"type": "string", "enum": [*SETTING_LAYER["enum"], "session", "profile", "task",
                                           "planner_task", "planner_step"]}
SYSTEM_PROMPT_MODE = {"type": "string", "enum": ["append", "replace"]}
_NULL_NUMBER = {"type": ["number", "null"]}
_NULL_INT = {"type": ["integer", "null"]}
LAYER_ENV_ENTRY = _object({"key": _STR, "value": _NULL_STR}, closed=True)
LAYER_VALUES = _object({
    "harness_prompt": _NULL_STR, "profile_id": _NULL_STR, "env": _array(LAYER_ENV_ENTRY),
    "run_budget_usd": _NULL_NUMBER, "max_concurrency": _NULL_INT, "boundaries": _NULL_STR,
    "system_prompt_mode": {"type": ["string", "null"], "enum": ["append", "replace", None]},
}, ["harness_prompt", "profile_id", "env", "run_budget_usd", "max_concurrency", "boundaries"], closed=True)
# The service accepts text in numeric fields to return its normal validation errors.
LAYER_UPDATE = _object({name: {"anyOf": [schema, _STR, {"type": "null"}]}
                        for name, schema in LAYER_VALUES["properties"].items()}, [], closed=True)
EFFECTIVE_PROFILE = _object({"profile_id": _STR, "name": _STR, "model": _STR,
                             "effort": _NULL_STR, "runtime": _STR, "provider": _STR}, closed=True)


def sourced(value):
    return _object({"value": value, "source": SETTING_SOURCE}, closed=True)


EFFECTIVE_SETTINGS = _object({
    "harness_prompt": _object({**sourced(_STR)["properties"], "from_role": {"const": "worker"}},
                               ["value", "source"], closed=True),
    "profile": sourced(EFFECTIVE_PROFILE), "effort": sourced(_NULL_STR),
    "env": _array(_object({"key": _STR, **sourced(_STR)["properties"]}, closed=True)),
    "run_budget_usd": sourced(_NULL_NUMBER), "max_concurrency": sourced(_INT),
    "system_prompt_mode": SYSTEM_PROMPT_MODE,
}, ["harness_prompt", "profile", "effort", "env", "run_budget_usd", "system_prompt_mode"], closed=True)
SETTING_LIMITS = _object({
    "harness_max_chars": _INT, "env_max_entries": _INT, "builtin_harness_prompt": _STR,
    "builtin_boundaries": _STR, "builtin_model": _STR,
    "builtin_profile": _object({"profile_id": _STR, "name": _STR}, closed=True),
    "budget_max_usd": {"type": "number"}, "concurrency_max": _INT,
}, closed=True)
LAYER_STATE = _object({"values": LAYER_VALUES, "effective": EFFECTIVE_SETTINGS}, closed=True)
AGENT_SETTINGS = _object({
    "limits": SETTING_LIMITS,
    "system": _object({**LAYER_STATE["properties"], "boundaries": _object({
        "value": _STR, "source": {"enum": ["builtin", "system"]}}, closed=True)}, closed=True),
    "roles": _object({role: LAYER_STATE for role in ROLE["enum"]}, closed=True),
}, closed=True)
PROJECT_AGENT_SETTINGS = _object({
    "project_id": _STR, "limits": SETTING_LIMITS,
    "project": _object({"values": LAYER_VALUES}, closed=True),
    "roles": _object({role.value: LAYER_STATE for role in agent_settings.TASK_ROLES}, closed=True),
}, closed=True)
ROLE_CONFIG_PATCH = _object({
    "display_name": _STR, "enabled": _BOOL, "system_prompt": _STR, "max_turns": _INT,
    "timeout_seconds": _INT, "extra_args": _NULL_OBJ, "metadata": _NULL_OBJ,
}, [], closed=True)
ROLE_CONFIG_PATCH["description"] = (
    "Only role config fields are writable. role/default_prompt/created_at/updated_at are read-only; "
    "runtime/llm_profile_id were removed. For harness_prompt/model/max_concurrency use "
    "agent_settings.update_role (POST /api/v1/agent_settings/update_role); select model via profile_id.")
ROLE_CONFIG = _object({"role": ROLE, **ROLE_CONFIG_PATCH["properties"], "default_prompt": _STR,
                       "created_at": _STR, "updated_at": _STR}, closed=True)

RESULTS = {"settings.set": _object({
    "layer": _STR, "changes": _array(_object({"setting": _STR,
        "before": {"type": ["string", "number", "null"]},
        "after": {"type": ["string", "number", "null"]}}, closed=True)),
    "effective": _OBJ,
    "manager_session": _object({"selected": {"anyOf": [
        _object({"profile_id": _STR, "name": _STR}, closed=True), {"type": "null"}]}}, closed=True),
    "note": _STR,
}, ["layer", "changes", "effective"], closed=True)}


def _operation(name, description, request, result, risk="read", confirms=False):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=confirms)


OPERATIONS = (
    _operation("roles.list", "List every global role configuration.", _object({}, closed=True),
               _object({"configs": _array(ROLE_CONFIG)}, closed=True)),
    _operation("roles.get", "Read a global role configuration.", _object({"role": ROLE}, closed=True), ROLE_CONFIG),
    _operation("roles.update", "Patch a global role configuration.",
               _object({"role": ROLE, "fields": ROLE_CONFIG_PATCH}, closed=True), ROLE_CONFIG, "write"),
    _operation("roles.reset_prompt", "Reset a role's prompt to its recommended default.",
               _object({"role": ROLE}, closed=True), ROLE_CONFIG, "write"),
    _operation("agent_settings.get", "Read shared defaults and every global role's effective settings.",
               _object({}, closed=True), AGENT_SETTINGS),
    _operation("agent_settings.update_system", "Update the shared defaults every role inherits.",
               _object({"values": LAYER_UPDATE}, closed=True), AGENT_SETTINGS, "high", True),
    _operation("agent_settings.update_role", "Update a global role's inherited settings.",
               _object({"role": ROLE, "values": LAYER_UPDATE}, closed=True), AGENT_SETTINGS, "write", True),
)


def validate_arguments(schema, arguments):
    # Explain moved/read-only fields before closed-schema validation rejects the request.
    if "fields" in schema.get("properties", {}) and isinstance(arguments, dict):
        fields = arguments.get("fields")
        if isinstance(fields, dict):
            try:
                agent_settings.role_config_fields(arguments.get("role", "role"), fields)
            except agent_settings.SettingsError as exc:
                raise OpError(ErrorCode.INVALID, str(exc)) from None
    return api_v1.validate_arguments(schema, arguments)


async def layer_impact(place, values):
    """Shared defaults, concurrency and an increased effective spend cap need client confirmation."""
    checked = _checked(place, values)
    layers = await _layers(place)
    raises = _raises_a_cap(place, layers, checked)
    if place.layer is not agent_settings.Layer.SYSTEM and "max_concurrency" not in checked and not raises:
        return None
    now = layers.get((place.layer, place.role), agent_settings.EMPTY_LAYER).view()
    lines = [f"Update {place.name}:"]
    lines.extend(f"- {name}: {_shown_value(name, now.get(name), {})} → {_shown_value(name, value, {})}"
                 for name, value in checked.items())
    if raises:
        lines.append("Raise or remove the effective per-run spend cap.")
    if "max_concurrency" in checked:
        lines.append(LIMIT_NOTE)
    return "\n".join(lines)


async def impact(server, operation, arguments, principal, headers):
    if not operation.operation_id.startswith("agent_settings.update_"):
        return None
    role = agent_settings.AgentType(arguments["role"]) if "role" in arguments else None
    place = _Place(agent_settings.Layer.ROLE if role else agent_settings.Layer.SYSTEM, role, None)
    return await layer_impact(place, arguments["values"])


async def run(server, operation, arguments, principal, headers):
    name = operation.operation_id
    if name == "roles.list":
        return {"configs": await role_configs.list_configs()}
    if name.startswith("roles."):
        role = arguments["role"]
        if name == "roles.update":
            try:
                fields = agent_settings.role_config_fields(role, arguments["fields"])
            except agent_settings.SettingsError as exc:
                raise OpError(ErrorCode.INVALID, str(exc)) from None
            result = await role_configs.set_config(role, fields)
        elif name == "roles.reset_prompt":
            result = await role_configs.reset_prompt(role)
        else:
            result = await role_configs.get_config(role)
        if result is None:
            raise OpError(ErrorCode.NOT_FOUND, f"role {role!r} or its recommended prompt was not found")
        return result
    if name == "agent_settings.get":
        return await answered(agent_settings.settings_view())
    values = arguments["values"]
    if name == "agent_settings.update_system":
        return await answered(agent_settings.update_system(values))
    result = await answered(agent_settings.update_role(arguments["role"], values))
    if "max_concurrency" in values:
        await server._recheck_concurrency()
    return result
