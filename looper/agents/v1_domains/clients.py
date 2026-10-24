"""Operator-only API client credentials, outside Manager tools and argument/result audit."""

from ...db import api_clients as db_api_clients
from ..api_v1 import Operation, STATUS, _array, _object, _STR
from ..ops import ErrorCode, OpError
from ..ops.service import _plain
from ..projects import RequestError

API_CLIENT = _object({
    "id": _STR, "name": _STR, "created_at": {"type": "string", "format": "date-time"},
    "last_used_at": {"type": ["string", "null"], "format": "date-time"}}, closed=True)
# The bound applies after the existing whitespace normalization, not to the raw input.
_NAME = {"type": "string", "minLength": 1,
         "pattern": rf"^\s*{db_api_clients.NAME_PATTERN.pattern}\s*$",
         "description": "Leading/trailing whitespace is stripped; the name must be 1–40 characters."}
_TOKEN = {"type": "string", "description": "Sensitive response-only secret, shown once. Never log or store it."}

RESULTS = {}
OPERATIONS = (
    Operation("clients.list", "POST", "/api/v1/clients/list", "List active API clients.",
              "List active API clients. Operator only; never returns tokens or hashes.",
              _object({}, closed=True), _object({"clients": _array(API_CLIENT)}, closed=True),
              risk="read", operator_only=True, credential_write=True),
    Operation("clients.create", "POST", "/api/v1/clients/create", "Create an API client.",
              "Create an API client. Operator only; the response-only token is sensitive, shown once and never logged.",
              _object({"name": _NAME}, closed=True), _object({"client": API_CLIENT, "token": _TOKEN}, closed=True),
              risk="write", operator_only=True, credential_write=True),
    Operation("clients.revoke", "POST", "/api/v1/clients/revoke", "Revoke an API client immediately.",
              "Revoke an API client immediately. Operator only; unknown or already revoked clients return not_found.",
              _object({"client_id": _STR}, closed=True), _object({"revoked": _STR}, closed=True),
              risk="write", operator_only=True, credential_write=True),
)


def _view(client):
    return _plain({key: client[key] for key in API_CLIENT["properties"]})


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "clients.list":
        return {"clients": [_view(client) for client in await db_api_clients.list_active()]}
    if operation.operation_id == "clients.create":
        try:
            client, token = await db_api_clients.create(arguments["name"])
        except db_api_clients.ClientNameError:
            raise OpError(ErrorCode.INVALID, "invalid client name") from None
        except db_api_clients.ClientNameTaken:
            # The credential dispatcher sanitizes service errors while preserving their status.
            raise RequestError(STATUS[ErrorCode.CONFLICT], "active client name already exists") from None
        return {"client": _view(client), "token": token}
    client_id = arguments["client_id"]
    if not await db_api_clients.revoke(client_id):
        raise RequestError(STATUS[ErrorCode.NOT_FOUND], "no active API client")
    return {"revoked": client_id}
