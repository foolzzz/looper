"""Versioned desktop RPC messages.

The desktop service is a local peer. A JSON field that names a uid, a pid, or
an approval is not an identity and not a grant: the transport reads the peer
from the kernel and requires a capability this process just issued. The
machine-wide lease is not part of this contract. ``acquire_machine_lease``
raises ``LeaseNotInstalled`` so a later change can install it without
pretending it is already here.

One frame is at most 4 MiB. Write actions are listed in ``NON_RETRIED``; the
client sends each of them once.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAGIC = b"LPCU"

KINDS = frozenset({
    "session.open", "observe", "act", "screenshot", "grant", "revoke", "stop", "health",
})

# Seconds. The client applies these as socket deadlines. It does not retry.
TIMEOUT_S = MappingProxyType({
    "session.open": 5.0,
    "observe": 10.0,
    "act": 15.0,
    "screenshot": 10.0,
    "grant": 5.0,
    "revoke": 5.0,
    "stop": 2.0,
    "health": 2.0,
})

# A second send would repeat a side effect. Reads are not retried either;
# this set is the one the failure rule names.
NON_RETRIED = frozenset({"act", "grant", "revoke", "stop"})

RESPONSE_CODES = frozenset({
    "ok",
    "forbidden",
    "no_effect",
    "browser_app",
    "audit_unavailable",
    "desktop_unavailable",
    "version_mismatch",
    "frame_too_large",
    "nonce_replay",
    "capability_expired",
    "peer_mismatch",
    "untrusted_identity",
    "cross_session",
    "bad_request",
    "timeout",
})

# Present on a request, these are a caller's claim about who it is. They never
# authorize a call, including when the number matches the kernel uid.
CLAIM_KEYS = frozenset({
    "approved",
    "caller_gid",
    "caller_pid",
    "caller_uid",
    "identity",
    "peer_gid",
    "peer_pid",
    "peer_uid",
    "purpose",
    "sensitive",
})

_REQUEST_KEYS = frozenset({"v", "kind", "nonce", "body", "session_id", "capability"})
_RESPONSE_KEYS = frozenset({"v", "kind", "nonce", "code", "message", "body"})
_SESSION = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_IN_USE = re.compile(r"in use by ([A-Za-z0-9_-]{1,64})\Z")
_MAX_DEPTH = 8


class ContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LeaseNotInstalled(RuntimeError):
    """No machine-wide desktop lease is installed.

    Acquire, renew, and release belong to the lease change. This function is
    the named seam and it always fails.
    """


@dataclass(frozen=True)
class Request:
    kind: str
    nonce: str
    body: Mapping
    session_id: str | None = None
    capability: str | None = None


@dataclass(frozen=True)
class Response:
    code: str
    message: str
    body: Mapping
    nonce: str
    kind: str


def in_use_code(session_id: str) -> str:
    """The typed result a second session receives. The session id is the holder."""
    if not isinstance(session_id, str) or _SESSION.fullmatch(session_id) is None:
        raise ContractError("bad_request", "session id")
    return f"in use by {session_id}"


def is_in_use(code: str) -> bool:
    return isinstance(code, str) and _IN_USE.fullmatch(code) is not None


def is_response_code(code: str) -> bool:
    return code in RESPONSE_CODES or is_in_use(code)


def acquire_machine_lease(session_id: str, owner: str, ttl_s: float) -> str:
    """Seam for the machine-wide lease. Not installed: always raises."""
    del session_id, owner, ttl_s
    raise LeaseNotInstalled("machine lease is not installed")


def encode_request(kind: str, nonce: str, body: Mapping, *,
                   session_id: str | None = None, capability: str | None = None) -> bytes:
    fields: dict[str, object] = {"v": PROTOCOL_VERSION, "kind": kind, "nonce": nonce, "body": dict(body)}
    if session_id is not None:
        fields["session_id"] = session_id
    if capability is not None:
        fields["capability"] = capability
    parse_request(fields)
    return dumps(fields)


def encode_response(response: Response) -> bytes:
    if not is_response_code(response.code):
        raise ContractError("bad_request", "code")
    if not isinstance(response.message, str) or len(response.message) > 200:
        raise ContractError("bad_request", "message")
    payload = {
        "v": PROTOCOL_VERSION,
        "kind": response.kind,
        "nonce": response.nonce,
        "code": response.code,
        "message": response.message,
        "body": dict(response.body),
    }
    return dumps(payload)


def parse_request(value: object) -> Request:
    if not isinstance(value, dict):
        raise ContractError("bad_request", "request must be an object")
    _reject_claims(value, 0)
    extra = set(value) - _REQUEST_KEYS
    if extra:
        raise ContractError("bad_request", "unknown field")
    version = value.get("v")
    if version != PROTOCOL_VERSION:
        if "v" in value:
            raise ContractError("version_mismatch", "version mismatch")
        raise ContractError("bad_request", "version")
    kind = value.get("kind")
    if kind not in KINDS:
        raise ContractError("bad_request", "unknown kind")
    nonce = value.get("nonce")
    if not isinstance(nonce, str) or _TOKEN.fullmatch(nonce) is None:
        raise ContractError("bad_request", "nonce")
    body = value.get("body")
    if not isinstance(body, dict) or any(not isinstance(key, str) for key in body):
        raise ContractError("bad_request", "body")
    session_id = value.get("session_id")
    capability = value.get("capability")
    if kind == "session.open":
        if "session_id" in value or "capability" in value or body != {}:
            raise ContractError("bad_request", "session.open carries no capability")
        session_id = None
        capability = None
    else:
        if not isinstance(session_id, str) or _SESSION.fullmatch(session_id) is None:
            raise ContractError("bad_request", "session_id")
        if not isinstance(capability, str) or _TOKEN.fullmatch(capability) is None:
            raise ContractError("bad_request", "capability")
    return Request(kind=kind, nonce=nonce, body=body, session_id=session_id, capability=capability)


def parse_response(value: object) -> Response:
    if not isinstance(value, dict):
        raise ContractError("bad_request", "response must be an object")
    if set(value) - _RESPONSE_KEYS:
        raise ContractError("bad_request", "unknown field")
    if value.get("v") != PROTOCOL_VERSION:
        raise ContractError("version_mismatch", "version mismatch")
    code = value.get("code")
    if not isinstance(code, str) or not is_response_code(code):
        raise ContractError("bad_request", "code")
    message = value.get("message", "")
    if not isinstance(message, str) or len(message) > 200:
        raise ContractError("bad_request", "message")
    body = value.get("body")
    if not isinstance(body, dict):
        raise ContractError("bad_request", "body")
    nonce = value.get("nonce", "")
    kind = value.get("kind", "")
    if not isinstance(nonce, str) or len(nonce) > 128 or not isinstance(kind, str) or len(kind) > 64:
        raise ContractError("bad_request", "response")
    return Response(code=code, message=message, body=body, nonce=nonce, kind=kind)


def dumps(value: Mapping) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ContractError("bad_request", "not encodable") from exc
    if len(raw) > MAX_FRAME_BYTES:
        raise ContractError("frame_too_large", "frame too large")
    return raw


def loads(raw: bytes) -> object:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_FRAME_BYTES:
        raise ContractError("frame_too_large", "frame too large")
    try:
        return json.loads(bytes(raw).decode("utf-8"), parse_constant=_reject_constant)
    except ContractError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ContractError("bad_request", "invalid JSON") from exc


def _reject_constant(value: str) -> None:
    raise ContractError("bad_request", "invalid JSON number")


def _reject_claims(value: object, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise ContractError("bad_request", "request is too deep")
    if isinstance(value, dict):
        for key, item in value.items():
            if key in CLAIM_KEYS:
                raise ContractError("untrusted_identity", "self-reported identity is not trusted")
            _reject_claims(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 256:
            raise ContractError("bad_request", "request is too wide")
        for item in value:
            _reject_claims(item, depth + 1)
