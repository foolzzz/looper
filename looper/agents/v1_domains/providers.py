"""Operator account management and the existing credential-free provider tools."""

from .. import profile_registry
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR
from ..projects import RequestError, HTTP_400, HTTP_404, HTTP_409
from .registry import PROVIDER, PROVIDER_ENDPOINT, PROVIDER_ENV_ENTRY, VENDOR_KIND, VENDOR_TYPE

# Input endpoints may omit the runtime-default marker; read-modify-write bodies
# retain the complete endpoint, including its provider id.
_ENDPOINT_INPUT = {**PROVIDER_ENDPOINT, "required": ["protocol", "base_url"]}
_FIELDS = {
    "name": _STR, "vendor_type": VENDOR_KIND,
    "endpoints": _array(_ENDPOINT_INPUT), "env": _array(PROVIDER_ENV_ENTRY),
    "max_concurrency": {"type": ["integer", "string", "null"]},
    "api_key": {**_NULL_STR, "writeOnly": True},
}
PROVIDER_CREATE = _object(_FIELDS, ["name", "vendor_type"], closed=True)
PROVIDER_PATCH = {**_object(_FIELDS, [], closed=True), "minProperties": 1,
                  "anyOf": [{"required": [name]} for name in _FIELDS]}
COMPACT_PROVIDER = _object({name: PROVIDER["properties"][name] for name in (
    "provider_id", "name", "vendor_type", "auth", "endpoints", "model_count",
    "profile_count", "builtin", "login", "max_concurrency")}, closed=True)
COMPACT_PROVIDER["properties"]["api_key"] = _STR
_WARNINGS = {"warnings": _array(_STR)}
_PROVIDER_RESULT = _object({"provider": PROVIDER, **_WARNINGS}, closed=True)
SCHEMAS = {"ProviderCreate": PROVIDER_CREATE, "ProviderPatch": PROVIDER_PATCH,
           "CompactProvider": COMPACT_PROVIDER}
RESULTS = {
    "providers.list": _object({"providers": _array(COMPACT_PROVIDER), "vendors": _array(VENDOR_TYPE)}, closed=True),
    "providers.register": _object({"provider": COMPACT_PROVIDER, **_WARNINGS, "note": _STR}, closed=True),
    "providers.update": _object({"provider": COMPACT_PROVIDER, **_WARNINGS}, closed=True),
}


def _op(action, summary, request, result, risk="write"):
    return Operation("provider_accounts." + action, "POST", "/api/v1/provider_accounts/" + action,
                     summary, summary, request, result, risk=risk,
                     operator_only=True, credential_write=True)


OPERATIONS = (
    _op("list", "List complete provider accounts; operator only.", _object({}, closed=True),
        _object({"providers": _array(PROVIDER)}, closed=True), "read"),
    _op("create", "Create a provider account with an optional API key; operator only.",
        PROVIDER_CREATE, _PROVIDER_RESULT),
    _op("update", "Update a provider account; omitted or *** keys keep, null clears; operator only.",
        _object({"provider_id": _STR, "fields": PROVIDER_PATCH}, closed=True), _PROVIDER_RESULT),
    _op("delete", "Delete an unused, non-builtin provider account; operator only.",
        _object({"provider_id": _STR}, closed=True), _object({"deleted": _STR}, closed=True), "high"),
    _op("clear_login_problem", "Use the selected runtime login again; operator only.",
        _object({"provider_id": _STR}, closed=True),
        _object({"provider": PROVIDER, **_WARNINGS}, ["provider"], closed=True)),
)


async def impact(server, operation, arguments, principal, headers):
    return "Remove the selected unused provider account and its stored credential?"


async def run(server, operation, arguments, principal, headers):
    action = operation.operation_id.rsplit(".", 1)[1]
    try:
        if action == "list":
            return await profile_registry.list_providers()
        if action == "create":
            return await profile_registry.create_provider(arguments)
        if action == "update":
            return await profile_registry.update_provider(arguments["provider_id"], arguments["fields"])
        if action == "delete":
            return await profile_registry.delete_provider(arguments["provider_id"])
        return await profile_registry.clear_login_problem(arguments["provider_id"])
    except profile_registry.RegistryNotFound:
        raise RequestError(HTTP_404, "provider not found") from None
    except profile_registry.RegistryConflict:
        raise RequestError(HTTP_409, "provider account conflict") from None
    except profile_registry.RegistryError:
        # Preserve public status codes without propagating credential-bearing
        # validation messages into a response, parameter audit or exception log.
        raise RequestError(HTTP_400, "invalid provider account request") from None
