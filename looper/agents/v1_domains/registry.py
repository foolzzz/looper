"""Complete operator registry projection, shared with provider/model/profile domains.

Provider API keys are masked by the service; provider environment values are
returned unchanged, so the complete snapshot is restricted to the operator.
The compact client discovery tools keep their existing projections.
"""

from .. import profile_registry
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..ops.config import answered
from ..runtimes import ApiProtocol, AuthKind, VendorType as VendorKind, ROLE_CAPABILITIES
from .global_settings import ROLE, SETTING_LAYER

_NULL_NUMBER = {"type": ["number", "null"]}
_NULL_INT = {"type": ["integer", "null"]}
_STRINGS = _array(_STR)
_NULL_STRINGS = {**_STRINGS, "type": ["array", "null"]}
PROVIDER_AUTH = {"type": "string", "enum": [auth.value for auth in AuthKind]}
API_PROTOCOL = {"type": "string", "enum": [protocol.value for protocol in ApiProtocol]}
VENDOR_KIND = {"type": "string", "enum": [vendor.value for vendor in VendorKind]}
PRICE_FIELD = {"type": "string", "enum": list(profile_registry.PRICE_FIELDS)}
PRICE = _object(dict.fromkeys(profile_registry.PRICE_FIELDS, _NULL_NUMBER), closed=True)
PROVIDER_ENV_ENTRY = _object({"key": _STR, "value": _STR}, closed=True)
PROVIDER_LOGIN = _object({"state": {"type": "string", "enum": [
    "chatgpt", "logged_in", "other_method", "not_logged_in", "unknown", "unavailable"]},
    "detail": _STR}, closed=True)
PROVIDER_ENDPOINT = _object({"provider_id": _STR, "protocol": API_PROTOCOL,
    "base_url": _STR, "use_runtime_default": _BOOL},
    ["protocol", "base_url", "use_runtime_default"], closed=True)

RUNTIME_TYPE = _object({
    "type": _STR, "label": _STR, "default_executable": _STR, "runtime_id": _STR,
    "protocols": _STRINGS, "capabilities": _STRINGS, "roles": _array(ROLE),
    "auth_kinds": _array(PROVIDER_AUTH), "supported_models": _NULL_STRINGS,
    "effort_levels": _NULL_STRINGS, "in_process": _BOOL,
    "can_replace_system_prompt": _BOOL, "replace_system_prompt_refusal": _NULL_STR,
    "can_interrupt": _BOOL, "probe_error": _NULL_STR,
}, ["type", "label", "default_executable", "runtime_id", "protocols", "capabilities", "roles",
    "auth_kinds", "supported_models", "in_process", "can_replace_system_prompt",
    "replace_system_prompt_refusal", "can_interrupt"], closed=True)
RUNTIME = _object({
    "runtime_id": _STR, "name": _STR, "type": _STR, "type_label": _STR,
    "executable": _STR, "default_executable": _NULL_STR, "path": _NULL_STR,
    "in_process": _BOOL, "protocols": _STRINGS, "version": _NULL_STR,
    "capabilities": _STRINGS, "roles": _array(ROLE), "probed_at": _NULL_STR,
    "probe_error": _NULL_STR, "builtin": _BOOL, "profile_count": _INT,
    "created_at": _STR, "updated_at": _STR,
}, closed=True)
PROVIDER = _object({
    "provider_id": _STR, "name": _STR, "vendor_type": VENDOR_KIND, "auth": PROVIDER_AUTH,
    "oauth_runtime_type": _NULL_STR, "endpoints": _array(PROVIDER_ENDPOINT),
    "suggested_models": _STRINGS,
    "login": {**PROVIDER_LOGIN, "type": ["object", "null"]},
    "env": _array(PROVIDER_ENV_ENTRY), "max_concurrency": _NULL_INT,
    "api_key": _NULL_STR, "builtin": _BOOL, "endpoint_count": _INT,
    "model_count": _INT, "profile_count": _INT, "created_at": _STR, "updated_at": _STR,
}, closed=True)
MODEL_PRICE = _object({"override": _NULL_NUMBER, "effective": _NULL_NUMBER,
    "source": {"type": "string", "enum": ["override", "builtin", "unknown"]}}, closed=True)
MODEL = _object({
    "model_id": _STR, "provider_id": _STR, "provider_name": _STR, "name": _STR,
    "protocol": API_PROTOCOL, "context_window": _NULL_INT,
    **dict.fromkeys(profile_registry.PRICE_FIELDS, _NULL_NUMBER),
    "efforts": _NULL_STRINGS, "background_model": _NULL_STR,
    "catalog_order": _INT, "builtin": _BOOL, "profile_count": _INT,
    "endpoint": PROVIDER_ENDPOINT,
    "prices": _object(dict.fromkeys(profile_registry.PRICE_FIELDS, MODEL_PRICE), closed=True),
    "warnings": _STRINGS, "created_at": _STR, "updated_at": _STR,
}, closed=True)
VENDOR_MODEL = _object({name: MODEL["properties"][name] for name in (
    "name", "protocol", "context_window", "background_model", "efforts", *profile_registry.PRICE_FIELDS)},
    ["name", "protocol"], closed=True)
VENDOR_TYPE = _object({
    "vendor_type": VENDOR_KIND, "auth": PROVIDER_AUTH, "oauth_runtime_type": _NULL_STR,
    "endpoints": _array(PROVIDER_ENDPOINT), "suggested_models": _array(VENDOR_MODEL),
    "env": _array(PROVIDER_ENV_ENTRY), "requires_url_confirmation": _BOOL,
}, closed=True)
PROFILE_COST_BASIS = _object({"source": {"type": ["string", "null"],
    "enum": ["runtime", "provider_price", None]}, "equivalent": _BOOL, "unpriced": _STRINGS}, closed=True)
PROFILE_USED_BY_LAYER = _object({
    "scope": SETTING_LAYER, "role": {**ROLE, "type": ["string", "null"], "enum": [*ROLE["enum"], None]},
    "project_id": _NULL_STR, "project_name": _NULL_STR,
}, closed=True)
PROFILE_USED_BY = _object({
    "layers": _array(PROFILE_USED_BY_LAYER), "task_count": _INT, "schedule_count": _INT,
    "allowed_in": _STRINGS, "manager_session": _BOOL, "manager_sessions": _STRINGS,
    "managers": _STRINGS, "manager_templates": _STRINGS,
}, ["layers", "task_count", "schedule_count", "allowed_in"], closed=True)
PROFILE_MODEL = _object({name: MODEL["properties"][name]
                         for name in ("model_id", "name", "protocol", "provider_id")}, closed=True)
PROFILE_PROVIDER = _object({name: PROVIDER["properties"][name]
                            for name in ("provider_id", "name", "vendor_type", "auth")}, closed=True)
AGENT_PROFILE = _object({
    "profile_id": _STR, "name": _STR, "model_id": _STR, "model": PROFILE_MODEL,
    "effort": _NULL_STR, "agent_teams": _BOOL, "custom_prompt": _STR,
    "runtime_id": _STR, "runtime_name": _STR, "runtime_type": _STR,
    "provider_id": _STR, "provider_name": _STR, "provider": PROFILE_PROVIDER,
    "builtin": _BOOL, "roles": _array(ROLE), "used_by": PROFILE_USED_BY,
    "cost": PROFILE_COST_BASIS, "warnings": _STRINGS,
}, closed=True)
REGISTRY_LIMITS = _object({
    "name_max_chars": _INT, "models_max": _INT, "model_fields": _STRINGS,
    "price_fields": _array(PRICE_FIELD), "protocols": _STRINGS,
    "auth_kinds": _array(PROVIDER_AUTH), "effort_levels": _STRINGS,
    "role_capabilities": _object(dict.fromkeys(ROLE_CAPABILITIES, _STRINGS), closed=True),
    "secret_mask": _STR, "provider_env_owned": _STRINGS,
}, closed=True)
REGISTRY_PRICES = _object({"as_of": _STR, "sources": _STRINGS,
    "models": {"type": "object", "additionalProperties": PRICE}}, closed=True)
REGISTRY = _object({
    "runtime_types": _array(RUNTIME_TYPE), "runtimes": _array(RUNTIME),
    "providers": _array(PROVIDER), "models": _array(MODEL), "profiles": _array(AGENT_PROFILE),
    "vendor_types": _array(VENDOR_TYPE), "prices": REGISTRY_PRICES, "limits": REGISTRY_LIMITS,
}, closed=True)

SCHEMAS = {
    "Registry": REGISTRY, "Runtime": RUNTIME, "RuntimeType": RUNTIME_TYPE,
    "Provider": PROVIDER, "Model": MODEL, "AgentProfile": AGENT_PROFILE,
    "VendorType": VENDOR_TYPE, "RegistryLimits": REGISTRY_LIMITS,
    "ProviderAuth": PROVIDER_AUTH, "ApiProtocol": API_PROTOCOL, "VendorKind": VENDOR_KIND,
    "PriceField": PRICE_FIELD, "Price": PRICE, "ProviderEnvEntry": PROVIDER_ENV_ENTRY,
    "ProviderLogin": PROVIDER_LOGIN, "ProviderEndpoint": PROVIDER_ENDPOINT,
    "ModelPrice": MODEL_PRICE, "VendorModel": VENDOR_MODEL,
    "ProfileCostBasis": PROFILE_COST_BASIS, "ProfileUsedBy": PROFILE_USED_BY,
    "ProfileUsedByLayer": PROFILE_USED_BY_LAYER, "ProfileModel": PROFILE_MODEL,
    "ProfileProvider": PROFILE_PROVIDER, "RegistryPrices": REGISTRY_PRICES,
}
RESULTS = {}
OPERATIONS = (
    Operation("registry.view", "POST", "/api/v1/registry/view", "Read the complete operator registry.",
              "Read the complete operator registry. Includes runtimes, providers, models and profiles with unchanged provider environment values. "
              "Provider API keys remain masked. Operator identity only.",
              _object({}, closed=True), REGISTRY, risk="read", operator_only=True),
    Operation("runtimes.probe", "POST", "/api/v1/runtimes/probe", "Re-detect a runtime.",
              "Re-detect a runtime. Probe its configured executable and record its capabilities, version or probe error.",
              _object({"runtime_id": _STR}, closed=True), _object({"runtime": RUNTIME}, closed=True), risk="write"),
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "registry.view":
        return await answered(profile_registry.registry_view(server_claude=server._claude_bin))
    return await answered(profile_registry.probe_runtime(arguments["runtime_id"], server_claude=server._claude_bin))
