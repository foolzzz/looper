"""The operator's credential for ``looper serve``'s HTTP API. Operator routes require the token from the
runtime root, as a Bearer header or a browser cookie; dashboard files are public and agent tools keep
their own tokens. A local browser's page load signs it in without passing the token to an agent.
"""

from __future__ import annotations

import enum
import hmac
import logging
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .request_guard import hostname, is_loopback

logger = logging.getLogger(__name__)

TOKEN_FILE = "operator-token"
COOKIE_PREFIX = "looper_operator_"
REFUSAL = ("this API answers the operator only: open the dashboard in a browser on the server's machine "
           "(it signs in on load), or sign in with the link /?token=<operator token>; "
           "the looper CLI sends the token itself")
WRONG = ("the operator credential does not match this server's: reload the dashboard, or read the token again")


class OperatorTokenError(RuntimeError):
    """The token file is unusable; the message says what to do."""


def token_path(runtime_root: Path) -> Path:
    return runtime_root / TOKEN_FILE


def ensure_token(runtime_root: Path) -> str:
    """Read the token, atomically creating it when missing, and keep its file private."""
    path = token_path(runtime_root)
    remedy = f"delete it and start looper serve again to make a new one: {path}"
    try:
        try:
            os.lstat(path)
        except FileNotFoundError:
            runtime_root.mkdir(parents=True, exist_ok=True)
            tmp = runtime_root / f".{TOKEN_FILE}-{secrets.token_hex(16)}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(secrets.token_urlsafe(32) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    pass  # another server made it
            finally:
                tmp.unlink()
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            raise OperatorTokenError(f"operator credential is not a regular file; {remedy}")
        if info.st_mode & 0o077:
            os.chmod(path, 0o600)
            logger.warning("tightened operator credential file to mode 0600: %s", path)
        try:
            token = path.read_text(encoding="utf-8").strip()
        except UnicodeError:
            raise OperatorTokenError(f"operator credential is not a token; {remedy}") from None
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
            raise OperatorTokenError(f"operator credential is not a token; {remedy}")
        return token
    except OSError as exc:
        raise OperatorTokenError(f"cannot use operator credential {path}: {exc}; {remedy}") from None


def read_token(runtime_root: Path) -> str | None:
    """Read a client's token without creating or validating it."""
    try:
        return token_path(runtime_root).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


class Access(enum.Enum):
    DASHBOARD = "dashboard"
    AGENT = "agent"
    OPERATOR = "operator"


@dataclass(frozen=True)
class Area:
    prefix: str
    access: Access


AREAS: tuple[Area, ...] = (Area("/api/agent/v1/", Access.AGENT), Area("/api/", Access.OPERATOR))


def access(path: str) -> Access:
    return next((area.access for area in AREAS if path.startswith(area.prefix)), Access.DASHBOARD)


def cookie_name(port: int) -> str:
    return f"{COOKIE_PREFIX}{port}"


def _bearer(headers: Mapping[str, str]) -> str | None:
    parts = headers.get("authorization", "").split(None, 1)
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None


def _cookie(headers: Mapping[str, str], port: int) -> str | None:
    for item in headers.get("cookie", "").split(";"):
        name, separator, value = item.partition("=")
        if separator and name.strip() == cookie_name(port):
            return value.strip()
    return None


def presented(headers: Mapping[str, str], *, port: int) -> str | None:
    bearer = _bearer(headers)
    return bearer if bearer is not None else _cookie(headers, port)


def is_operator(headers: Mapping[str, str], *, token: str | None, port: int) -> bool:
    return token is not None and any(
        credential is not None and hmac.compare_digest(credential.encode(), token.encode())
        for credential in (_bearer(headers), _cookie(headers, port))
    )


def signs_in(headers: Mapping[str, str], *, peer_host: str | None) -> bool:
    return (headers.get("sec-fetch-mode") == "navigate" and headers.get("sec-fetch-dest") == "document"
            and peer_host is not None and is_loopback(peer_host)
            and is_loopback(hostname(headers.get("host", "")))
            and "forwarded" not in headers and "x-forwarded-for" not in headers)


def set_cookie(token: str, *, port: int) -> str:
    return f"{cookie_name(port)}={token}; Path=/; HttpOnly; SameSite=Strict"
