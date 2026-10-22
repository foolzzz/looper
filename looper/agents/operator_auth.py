"""The operator's credential for ``looper serve``'s HTTP API. Operator routes require the token from the
runtime root, as a Bearer header or a browser cookie; dashboard files are public and agent tools keep
their own tokens. A local browser's page load signs it in without passing the token to an agent.
"""

from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import json
import math
import errno
import hmac
import os
import re
import secrets
import stat
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .request_guard import hostname, is_loopback

TOKEN_FILE = "operator-token"
# Non-blocking, so a FIFO in the file's place can't hang the start before fstat refuses it.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
COOKIE_PREFIX = "looper_operator_"
PASSWORD_FILE = "operator-password"
USER_PATTERN = r"[A-Za-z0-9._-]{1,64}"
PASSWORD_MIN_CHARS = 8
SESSION_TTL_SECONDS = 7 * 24 * 3600
SIGN_IN_PATHS = frozenset({"/login", "/logout"})
REFUSAL = ("this API answers the operator only: open the dashboard in a browser on the server's machine "
           "(it signs in on load), or sign in at /login with the user name and password set by "
           "looper auth set-password; the looper CLI sends the token itself")
WRONG = ("the operator credential does not match this server's: reload the dashboard, or read the token again")


class OperatorTokenError(RuntimeError):
    """A credential file is unusable; the message says what to do."""


def token_path(runtime_root: Path) -> Path:
    return runtime_root / TOKEN_FILE


def _remedy(path: Path) -> str:
    if path.name == TOKEN_FILE:
        return f"delete it and start looper serve again to make a new one: {path}"
    return f"delete it and run looper auth set-password to make a new one: {path}"


def _open_root(runtime_root: Path) -> int:
    try:
        os.makedirs(runtime_root, mode=0o700, exist_ok=True)
        fd = os.open(runtime_root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise OperatorTokenError(f"cannot open runtime root {runtime_root}: {exc}") from None
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise OperatorTokenError(f"the runtime root {runtime_root} is owned by uid {info.st_uid}, not you")
        if info.st_mode & 0o022:
            raise OperatorTokenError(f"the runtime root {runtime_root} must be owned by you and not writable by "
                                     f"group or others (chmod go-w {runtime_root})")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_private(dir_fd: int, name: str, path: Path) -> bytes | None:
    remedy = _remedy(path)
    kind = "token" if name == TOKEN_FILE else "password record"
    fd = None
    try:
        try:
            fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OperatorTokenError(f"operator credential {path} is not a regular file; {remedy}")
        if info.st_uid != os.getuid():
            raise OperatorTokenError(f"operator credential {path} is owned by uid {info.st_uid}, not you; {remedy}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise OperatorTokenError(f"operator credential {path} must have mode 0600 (chmod 600 {path})")
        if info.st_size > 4096:
            raise OperatorTokenError(f"operator credential is not a {kind}; {remedy}")
        data = b""
        while len(data) <= 4096:
            chunk = os.read(fd, 4097 - len(data))
            if not chunk:
                return data
            data += chunk
        raise OperatorTokenError(f"operator credential is not a {kind}; {remedy}")
    except OSError as exc:
        reason = "is a symbolic link" if exc.errno == errno.ELOOP else str(exc)
        raise OperatorTokenError(f"cannot use operator credential {path}: {reason}; {remedy}") from None
    finally:
        if fd is not None:
            os.close(fd)


def _write_private(dir_fd: int, name: str, data: bytes) -> None:
    tmp = f".{name}-{secrets.token_hex(16)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        try:
            while data:
                data = data[os.write(fd, data):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


def ensure_token(runtime_root: Path) -> str:
    """Read the private token, creating it without replacing a concurrent server's token."""
    path = token_path(runtime_root)
    dir_fd = _open_root(runtime_root)
    try:
        data = _read_private(dir_fd, TOKEN_FILE, path)
        if data is None:
            tmp = f".{TOKEN_FILE}-{secrets.token_hex(16)}.tmp"
            try:
                _write_private(dir_fd, tmp, (secrets.token_urlsafe(32) + "\n").encode())
                try:
                    os.link(tmp, TOKEN_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
                except FileExistsError:
                    pass
            finally:
                try:
                    os.unlink(tmp, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
            data = _read_private(dir_fd, TOKEN_FILE, path)
        try:
            token = data.decode("utf-8").strip() if data is not None else ""
        except UnicodeError:
            token = ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
            raise OperatorTokenError(f"operator credential is not a token; {_remedy(path)}")
        return token
    except OSError as exc:
        raise OperatorTokenError(f"cannot use operator credential {path}: {exc}; {_remedy(path)}") from None
    finally:
        os.close(dir_fd)


@dataclass(frozen=True)
class PasswordRecord:
    user: str
    n: int
    r: int
    p: int
    salt: bytes
    hash: bytes


def _hash(password: str, *, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def set_password(runtime_root: Path, user: str, password: str) -> None:
    if not re.fullmatch(USER_PATTERN, user):
        raise ValueError("user name must contain 1 to 64 letters, digits, dots, underscores or hyphens")
    if len(password) < PASSWORD_MIN_CHARS:
        raise ValueError(f"password must contain at least {PASSWORD_MIN_CHARS} characters")
    salt = secrets.token_bytes(16)
    record = {"user": user, "scheme": "scrypt", "n": 32768, "r": 8, "p": 1,
              "salt": base64.urlsafe_b64encode(salt).decode(),
              "hash": base64.urlsafe_b64encode(_hash(password, salt=salt, n=32768, r=8, p=1)).decode()}
    fd = _open_root(runtime_root)
    try:
        _write_private(fd, PASSWORD_FILE, (json.dumps(record) + "\n").encode())
    except OSError as exc:
        path = runtime_root / PASSWORD_FILE
        raise OperatorTokenError(f"cannot write operator credential {path}: {exc}; {_remedy(path)}") from None
    finally:
        os.close(fd)


def clear_password(runtime_root: Path) -> bool:
    fd = _open_root(runtime_root)
    try:
        try:
            os.unlink(PASSWORD_FILE, dir_fd=fd)
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(fd)


def load_password(runtime_root: Path) -> PasswordRecord | None:
    path = runtime_root / PASSWORD_FILE
    fd = _open_root(runtime_root)
    try:
        data = _read_private(fd, PASSWORD_FILE, path)
    finally:
        os.close(fd)
    if data is None:
        return None
    try:
        value = json.loads(data)
        user, n, r, p = (value[key] for key in ("user", "n", "r", "p"))
        if (value["scheme"] != "scrypt" or not isinstance(user, str) or not re.fullmatch(USER_PATTERN, user)
                or any(type(v) is not int for v in (n, r, p)) or not 2**14 <= n <= 2**20 or n & (n - 1)
                or not 1 <= r <= 32 or not 1 <= p <= 32):
            raise ValueError
        salt, digest = (base64.b64decode(value[key], altchars=b"-_", validate=True) for key in ("salt", "hash"))
        if len(salt) != 16 or len(digest) != 32:
            raise ValueError
        return PasswordRecord(user, n, r, p, salt, digest)
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        raise OperatorTokenError(f"operator credential {path} is not a password record; {_remedy(path)}") from None


def verify(record: PasswordRecord, user: str, password: str) -> bool:
    digest = _hash(password, salt=record.salt, n=record.n, r=record.r, p=record.p)
    user_matches = hmac.compare_digest(user.encode(), record.user.encode())
    hash_matches = hmac.compare_digest(digest, record.hash)
    return user_matches and hash_matches


class Sessions:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._sessions: dict[str, tuple[str, bytes, float]] = {}

    def create(self, user: str, record_hash: bytes) -> str:
        now = self._clock()
        self._sessions = {key: value for key, value in self._sessions.items() if value[2] > now}
        if len(self._sessions) >= 100:
            del self._sessions[next(iter(self._sessions))]
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = (user, record_hash, now + SESSION_TTL_SECONDS)
        return session_id

    def valid(self, session_id: str, current_record_hash: bytes) -> bool:
        session = self._sessions.get(session_id)
        return bool(session and session[2] > self._clock() and hmac.compare_digest(session[1], current_record_hash))

    def end(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class LoginLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._clients: dict[str | None, tuple[int, float]] = {}
        self._failures: deque[float] = deque()

    def retry_after(self, client: str | None) -> int:
        now = self._clock()
        while self._failures and self._failures[0] <= now - 600:
            self._failures.popleft()
        until = self._clients.get(client, (0, 0.0))[1]
        if len(self._failures) > 30:
            until = max(until, self._failures[len(self._failures) - 31] + 600)
        return max(0, math.ceil(until - now))

    def failure(self, client: str | None) -> None:
        now = self._clock()
        failures = self._clients.get(client, (0, 0.0))[0] + 1
        delay = min(900, 60 * 2 ** min(failures - 5, 4)) if failures >= 5 else 0
        self._clients[client] = (failures, now + delay)
        self._failures.append(now)

    def success(self, client: str | None) -> None:
        self._clients.pop(client, None)


def read_token(runtime_root: Path) -> str | None:
    """Read a client's token without creating or validating it."""
    try:
        return token_path(runtime_root).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


class Access(enum.Enum):
    DASHBOARD = "dashboard"
    AGENT = "agent"
    CLIENT = "client"
    OPERATOR = "operator"
    SIGN_IN = "sign_in"


@dataclass(frozen=True)
class Area:
    prefix: str
    access: Access


AREAS: tuple[Area, ...] = (
    Area("/api/agent/v1/", Access.AGENT), Area("/api/v1/", Access.CLIENT), Area("/api/", Access.OPERATOR))


def access(path: str) -> Access:
    if path in SIGN_IN_PATHS:
        return Access.SIGN_IN
    return next((area.access for area in AREAS if path.startswith(area.prefix)), Access.DASHBOARD)


def cookie_name(port: int) -> str:
    return f"{COOKIE_PREFIX}{port}"


def _bearer(headers: Mapping[str, str]) -> str | None:
    parts = headers.get("authorization", "").split(None, 1)
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None


def bearer(headers: Mapping[str, str]) -> str | None:
    """The ``Authorization: Bearer`` credential only, never a cookie."""
    return _bearer(headers)


def cookie(headers: Mapping[str, str], name: str) -> str | None:
    for item in headers.get("cookie", "").split(";"):
        key, separator, value = item.partition("=")
        if separator and key.strip() == name:
            return value.strip()
    return None


def presented(headers: Mapping[str, str], *, port: int) -> str | None:
    bearer = _bearer(headers)
    return bearer if bearer is not None else cookie(headers, cookie_name(port))


def is_operator(headers: Mapping[str, str], *, token: str | None, port: int) -> bool:
    return token is not None and any(
        credential is not None and hmac.compare_digest(credential.encode(), token.encode())
        for credential in (_bearer(headers), cookie(headers, cookie_name(port)))
    )


def signs_in(headers: Mapping[str, str], *, peer_host: str | None) -> bool:
    return (headers.get("sec-fetch-mode") == "navigate" and headers.get("sec-fetch-dest") == "document"
            and peer_host is not None and is_loopback(peer_host)
            and is_loopback(hostname(headers.get("host", "")))
            and "forwarded" not in headers and "x-forwarded-for" not in headers)


def set_cookie(token: str, *, port: int) -> str:
    return f"{cookie_name(port)}={token}; Path=/; HttpOnly; SameSite=Strict"
