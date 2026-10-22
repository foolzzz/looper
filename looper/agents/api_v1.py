"""``/api/v1``: the one API local agents and scripts use (RFC ``looper-client-skill`` §5.2).

This module holds what every answer shares: who is calling (``Principal``), the typed errors and their HTTP status,
and ``whoami``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import looper

from .ops import ErrorCode

API_VERSION = 1

OK = "200 OK"
ACCEPTED = "202 Accepted"

STATUS: dict[ErrorCode, str] = {
    ErrorCode.INVALID: "400 Bad Request",
    ErrorCode.UNAUTHORIZED: "401 Unauthorized",
    ErrorCode.FORBIDDEN: "403 Forbidden",
    ErrorCode.NOT_FOUND: "404 Not Found",
    ErrorCode.CONFLICT: "409 Conflict",
    ErrorCode.EXPIRED: "410 Gone",
    ErrorCode.RATE_LIMITED: "429 Too Many Requests",
    ErrorCode.UNAVAILABLE: "503 Service Unavailable",
}
_DEFAULT_STATUS = "400 Bad Request"


@dataclass(frozen=True)
class Principal:
    """Who calls: the operator (their token), or an API client (``lpc_`` token)."""

    kind: Literal["client", "operator"]
    client_id: str | None = None
    client_name: str | None = None

    @property
    def key(self) -> str:
        return f"client:{self.client_id}" if self.kind == "client" else "operator"


OPERATOR = Principal("operator")


def error(code: ErrorCode, message: str) -> tuple[str, dict[str, Any]]:
    return STATUS.get(code, _DEFAULT_STATUS), {"status": "error", "error": {"code": code.value, "message": message}}


def answer_status(answer: Mapping[str, Any]) -> str:
    """The HTTP status of an operation's answer (``ok``, ``needs_confirmation`` or ``error``)."""
    status = answer.get("status")
    if status == "ok":
        return OK
    if status == "needs_confirmation":
        return ACCEPTED
    return STATUS.get(ErrorCode(answer["error"]["code"]), _DEFAULT_STATUS)


def whoami(principal: Principal) -> dict[str, Any]:
    client = ({"id": principal.client_id, "name": principal.client_name} if principal.kind == "client" else None)
    return {"status": "ok", "result": {
        "principal": principal.kind, "client": client, "looper_version": looper.__version__,
        "api_version": API_VERSION}}
