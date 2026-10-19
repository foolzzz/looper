"""The operator's credential for ``looper serve``'s HTTP API. Operator routes require the token from the
runtime root, as a Bearer header or a browser cookie; dashboard files are public and agent tools keep
their own tokens. A local browser's page load signs it in without passing the token to an agent.
"""

from __future__ import annotations

import enum
import errno
import hmac
import os
import re
import secrets
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .request_guard import hostname, is_loopback

TOKEN_FILE = "operator-token"
# Non-blocking, so a FIFO in the file's place can't hang the start before fstat refuses it.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
COOKIE_PREFIX = "looper_operator_"
LOGIN_CODE_TTL_SECONDS = 300
LOGIN_CODE_REFUSAL = ("this sign-in link is not valid: it was used already, has expired (after 5 minutes) or is wrong; "
                      "run looper login-link for a new one")
REFUSAL = ("this API answers the operator only: open the dashboard in a browser on the server's machine "
           "(it signs in on load), or run looper login-link for a sign-in link; "
           "the looper CLI sends the token itself")
WRONG = ("the operator credential does not match this server's: reload the dashboard, or read the token again")


class OperatorTokenError(RuntimeError):
    """The token file is unusable; the message says what to do."""


def token_path(runtime_root: Path) -> Path:
    return runtime_root / TOKEN_FILE


def ensure_token(runtime_root: Path) -> str:
    """Validate and read the same private file descriptor, creating the token atomically if missing."""
    path = token_path(runtime_root)
    remedy = f"delete it and start looper serve again to make a new one: {path}"
    dir_fd = fd = None
    try:
        os.makedirs(runtime_root, mode=0o700, exist_ok=True)
        dir_fd = os.open(runtime_root, os.O_RDONLY | os.O_DIRECTORY)
        info = os.fstat(dir_fd)
        if info.st_uid != os.getuid():
            raise OperatorTokenError(f"the runtime root {runtime_root} is owned by uid {info.st_uid}, not you")
        if info.st_mode & 0o022:
            raise OperatorTokenError(f"the runtime root {runtime_root} must be owned by you and not writable by "
                                     f"group or others (chmod go-w {runtime_root})")
        try:
            fd = os.open(TOKEN_FILE, _READ_FLAGS, dir_fd=dir_fd)
        except FileNotFoundError:
            tmp = f".{TOKEN_FILE}-{secrets.token_hex(16)}.tmp"
            created = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
            try:
                try:
                    data = (secrets.token_urlsafe(32) + "\n").encode()
                    while data:
                        data = data[os.write(created, data):]
                    os.fsync(created)
                finally:
                    os.close(created)
                try:
                    os.link(tmp, TOKEN_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
                except FileExistsError:
                    pass  # another server made it
            finally:
                os.unlink(tmp, dir_fd=dir_fd)
            fd = os.open(TOKEN_FILE, _READ_FLAGS, dir_fd=dir_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OperatorTokenError(f"operator credential {path} is not a regular file; {remedy}")
        if info.st_uid != os.getuid():
            raise OperatorTokenError(f"operator credential {path} is owned by uid {info.st_uid}, not you; {remedy}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise OperatorTokenError(f"operator credential {path} must have mode 0600 (chmod 600 {path})")
        if info.st_size > 4096:
            raise OperatorTokenError(f"operator credential is not a token; {remedy}")
        data = b""
        while len(data) < 4096:
            chunk = os.read(fd, 4096 - len(data))
            if not chunk:
                break
            data += chunk
        try:
            token = data.decode("utf-8").strip()
        except UnicodeError:
            raise OperatorTokenError(f"operator credential is not a token; {remedy}") from None
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
            raise OperatorTokenError(f"operator credential is not a token; {remedy}")
        return token
    except OSError as exc:
        reason = "is a symbolic link" if exc.errno == errno.ELOOP else str(exc)
        raise OperatorTokenError(f"cannot use operator credential {path}: {reason}; {remedy}") from None
    finally:
        if fd is not None:
            os.close(fd)
        if dir_fd is not None:
            os.close(dir_fd)


class LoginCodes:
    """Short-lived, single-use browser sign-ins, kept only in this server."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._codes: dict[str, float] = {}

    def _prune(self) -> None:
        now = self._clock()
        self._codes = {code: expiry for code, expiry in self._codes.items() if expiry > now}

    def issue(self) -> str:
        self._prune()
        if len(self._codes) >= 20:
            del self._codes[next(iter(self._codes))]
        code = secrets.token_urlsafe(24)
        self._codes[code] = self._clock() + LOGIN_CODE_TTL_SECONDS
        return code

    def redeem(self, code: str) -> bool:
        self._prune()
        for issued in self._codes:
            if hmac.compare_digest(code.encode(), issued.encode()):
                del self._codes[issued]
                return True
        return False


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
