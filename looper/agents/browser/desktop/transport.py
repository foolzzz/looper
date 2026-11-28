"""AF_UNIX framing for the desktop RPC contract.

One request per connection. The peer uid and pid come from the kernel. A
matching uid is required to open a session and is not itself a capability:
later calls present the token ``session.open`` returned, plus a single-use
nonce. Frame length is checked before the body is read. Version mismatch,
an oversized frame, a replayed or expired nonce, a cross-session token, an
untrusted identity claim, and a peer uid that is not the one the endpoint
was started for close the connection and append a disconnect record.

Nothing in this module binds a socket unless ``Endpoint.start`` is called
with ``enabled=True``. The default is off.
"""

from __future__ import annotations

import hmac
import os
import secrets
import socket
import struct
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .contract import (
    MAGIC,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    TIMEOUT_S,
    ContractError,
    Request,
    Response,
    dumps,
    is_in_use,
    loads,
    parse_request,
    parse_response,
)

STATUS_OK = 0
STATUS_VERSION = 1
STATUS_PEER = 2

_HEADER = struct.Struct(">I")
_HANDSHAKE = struct.Struct(">HH")
Credentials = Callable[[socket.socket], "PeerCredentials"]
Handler = Callable[[str, Mapping, str], tuple[str, Mapping]]
Clock = Callable[[], float]


class TransportError(OSError):
    pass


class FrameTooLarge(TransportError):
    pass


@dataclass(frozen=True)
class PeerCredentials:
    uid: int
    gid: int
    pid: int


@dataclass(frozen=True)
class DisconnectRecord:
    reason: str
    peer_uid: int | None
    peer_pid: int | None
    detail: str


class DisconnectLog:
    """Why a connection was closed. This is not the action audit log."""

    def __init__(self) -> None:
        self.records: list[DisconnectRecord] = []

    def add(self, reason: str, peer: PeerCredentials | None, detail: str) -> None:
        self.records.append(DisconnectRecord(
            reason, None if peer is None else peer.uid, None if peer is None else peer.pid, detail))


@dataclass
class _Session:
    session_id: str
    token: str
    uid: int
    expires: float


def read_peer_credentials(sock: socket.socket) -> PeerCredentials:
    """Kernel identity of the process on the other end of ``sock``. Fail closed."""
    if sock.family != socket.AF_UNIX:
        raise TransportError("desktop RPC accepts only a unix socket")
    if os.uname().sysname == "Darwin":
        return _darwin_peer(sock)
    if os.uname().sysname == "Linux":
        return _linux_peer(sock)
    raise TransportError("peer credentials are unavailable on this platform")


def write_handshake(sock: socket.socket, status: int, *, version: int = PROTOCOL_VERSION) -> None:
    sock.sendall(MAGIC + _HANDSHAKE.pack(version, status))


def read_handshake(sock: socket.socket) -> tuple[bytes, int, int]:
    raw = _read_exact(sock, 8)
    version, status = _HANDSHAKE.unpack(raw[4:])
    return raw[:4], version, status


def write_frame(sock: socket.socket, payload: bytes) -> None:
    if len(payload) < 1 or len(payload) > MAX_FRAME_BYTES:
        raise FrameTooLarge("frame too large")
    sock.sendall(_HEADER.pack(len(payload)) + payload)


def read_frame(sock: socket.socket) -> bytes:
    """Read one frame. A declared length past the cap is not read."""
    raw_length = _read_exact(sock, 4)
    (length,) = _HEADER.unpack(raw_length)
    if length < 1 or length > MAX_FRAME_BYTES:
        raise FrameTooLarge("frame too large")
    return _read_exact(sock, length)


class Endpoint:
    """Desktop RPC listener. ``enabled`` defaults to false and then ``start`` binds nothing."""

    def __init__(self, path: str | Path, handler: Handler, log: DisconnectLog, *,
                 enabled: bool = False, expected_uid: int | None = None, ttl_s: float = 60.0,
                 clock: Clock | None = None, credentials: Credentials | None = None) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.expected_uid = os.geteuid() if expected_uid is None else expected_uid
        self.ttl_s = ttl_s
        self.log = log
        self.last_peer: PeerCredentials | None = None
        self._handler = handler
        self._clock = clock or _monotonic
        self._credentials = credentials or read_peer_credentials
        self._sessions: dict[str, _Session] = {}
        self._by_token: dict[str, str] = {}
        self._nonces: dict[str, float] = {}
        self._listen: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._bound = False

    def start(self) -> None:
        if self._listen is not None or self._thread is not None:
            raise TransportError("desktop endpoint already started")
        if not self.enabled:
            return
        if len(str(self.path).encode()) > 100:
            raise TransportError("socket path is too long")
        if self.path.exists():
            raise TransportError("socket path already exists")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        _no_sigpipe(sock)
        created = False
        try:
            sock.bind(str(self.path))
            created = True
            os.chmod(self.path, 0o600)
            sock.listen(8)
        except Exception:
            sock.close()
            if created and self.path.exists():
                self.path.unlink()
            raise
        self._bound = True
        self._listen = sock
        self._thread = threading.Thread(target=self._accept, name="desktop-rpc")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        listen = self._listen
        self._listen = None
        if listen is not None:
            listen.close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._unlink_if_bound()

    def __enter__(self) -> Endpoint:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _accept(self) -> None:
        listen = self._listen
        if listen is None:
            return
        while not self._stop.is_set():
            try:
                conn, _ = listen.accept()
            except OSError:
                return
            _no_sigpipe(conn)
            try:
                self._speak(conn)
            finally:
                conn.close()

    def _speak(self, conn: socket.socket) -> None:
        conn.settimeout(TIMEOUT_S["session.open"])
        try:
            peer = self._credentials(conn)
        except Exception as exc:
            self.log.add("peer_mismatch", None, exc.__class__.__name__)
            _reject_peer(conn)
            return
        self.last_peer = peer
        if peer.uid != self.expected_uid:
            self.log.add("peer_mismatch", peer, "uid")
            _reject_peer(conn)
            return
        try:
            magic, version, _status = read_handshake(conn)
        except (OSError, EOFError, struct.error):
            self.log.add("bad_request", peer, "handshake")
            return
        if magic != MAGIC or version != PROTOCOL_VERSION:
            self.log.add("version_mismatch", peer, "handshake")
            _send_handshake(conn, STATUS_VERSION)
            return
        _send_handshake(conn, STATUS_OK)
        try:
            payload = read_frame(conn)
        except FrameTooLarge:
            self.log.add("frame_too_large", peer, "length")
            _send_response(conn, Response("frame_too_large", "frame too large", {}, "", ""))
            return
        except (OSError, EOFError, struct.error):
            self.log.add("bad_request", peer, "frame")
            return
        try:
            decoded = loads(payload)
            request = parse_request(decoded)
        except ContractError as exc:
            self.log.add(exc.code, peer, exc.message)
            _send_response(conn, Response(exc.code, exc.message, {}, "", ""))
            return
        response = self._dispatch(request, peer)
        if response is not None:
            _send_response(conn, response)

    def _dispatch(self, request: Request, peer: PeerCredentials) -> Response | None:
        if not self._take_nonce(request.nonce):
            self.log.add("nonce_replay", peer, request.kind)
            return Response("nonce_replay", "nonce replayed", {}, request.nonce, request.kind)
        if request.kind == "session.open":
            return self._open(request, peer)
        session, failure = self._authorized(request, peer)
        if failure is not None:
            return failure
        assert session is not None
        try:
            code, body = self._handler(request.kind, request.body, session.session_id)
        except Exception as exc:
            self.log.add("bad_request", peer, exc.__class__.__name__)
            return Response("bad_request", "handler failed", {}, request.nonce, request.kind)
        if not isinstance(code, str) or not isinstance(body, dict) or not _driver_code(code):
            # The driver cannot emit transport failures such as nonce replay.
            self.log.add("bad_request", peer, "handler result")
            return Response("bad_request", "handler result", {}, request.nonce, request.kind)
        if request.kind == "stop" and code == "ok":
            self._drop(session)
        try:
            dumps({"body": body})
        except ContractError:
            self.log.add("bad_request", peer, "handler body")
            return Response("bad_request", "handler body", {}, request.nonce, request.kind)
        return Response(code, "", dict(body), request.nonce, request.kind)

    def _open(self, request: Request, peer: PeerCredentials) -> Response:
        session_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        session = _Session(session_id, token, peer.uid, self._clock() + self.ttl_s)
        self._sessions[session_id] = session
        self._by_token[token] = session_id
        body = {"session_id": session_id, "capability": token, "expires_in_ms": int(self.ttl_s * 1000)}
        return Response("ok", "", body, request.nonce, request.kind)

    def _authorized(self, request: Request, peer: PeerCredentials) -> tuple[_Session | None, Response | None]:
        assert request.session_id is not None and request.capability is not None
        owner = self._by_token.get(request.capability)
        if owner is not None and owner != request.session_id:
            self.log.add("cross_session", peer, "capability")
            return None, Response("cross_session", "capability does not cross sessions", {}, request.nonce, request.kind)
        session = self._sessions.get(request.session_id)
        if (session is None or owner is None or len(session.token) != len(request.capability)
                or not hmac.compare_digest(session.token, request.capability)):
            self.log.add("forbidden", peer, "capability")
            return None, Response("forbidden", "capability rejected", {}, request.nonce, request.kind)
        if self._clock() >= session.expires:
            self._drop(session)
            self.log.add("capability_expired", peer, "ttl")
            return None, Response("capability_expired", "capability expired", {}, request.nonce, request.kind)
        if peer.uid != session.uid:
            self.log.add("peer_mismatch", peer, "session uid")
            return None, Response("peer_mismatch", "peer uid mismatch", {}, request.nonce, request.kind)
        return session, None

    def _take_nonce(self, nonce: str) -> bool:
        now = self._clock()
        stale = [key for key, until in self._nonces.items() if until <= now]
        for key in stale:
            del self._nonces[key]
        if nonce in self._nonces:
            return False
        self._nonces[nonce] = now + self.ttl_s
        return True

    def _drop(self, session: _Session) -> None:
        self._sessions.pop(session.session_id, None)
        self._by_token.pop(session.token, None)

    def _unlink_if_bound(self) -> None:
        if not self._bound:
            return
        self._bound = False
        if self.path.exists():
            self.path.unlink()


def _monotonic() -> float:
    import time
    return time.monotonic()


def _reject_peer(conn: socket.socket) -> None:
    """Answer a rejected peer, then let the caller close.

    The client has already written its handshake. Closing while those bytes
    are still unread resets the connection, and the status is lost.
    """
    _discard_handshake(conn)
    _send_handshake(conn, STATUS_PEER)


def _discard_handshake(conn: socket.socket) -> None:
    try:
        read_handshake(conn)
    except (OSError, EOFError, struct.error):
        return


def _send_handshake(sock: socket.socket, status: int) -> None:
    try:
        write_handshake(sock, status)
    except OSError:
        return


def _send_response(sock: socket.socket, response: Response) -> None:
    try:
        encoded = dumps({
            "v": PROTOCOL_VERSION,
            "kind": response.kind,
            "nonce": response.nonce,
            "code": response.code,
            "message": response.message,
            "body": dict(response.body),
        })
        # parse so a programming error cannot put a claim or a bad code on the wire
        parse_response(loads(encoded))
        write_frame(sock, encoded)
    except (OSError, ContractError):
        return


def _read_exact(sock: socket.socket, size: int) -> bytes:
    parts = []
    got = 0
    while got < size:
        try:
            chunk = sock.recv(size - got)
        except TimeoutError as exc:
            raise TimeoutError from exc
        if not chunk:
            raise EOFError
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def _no_sigpipe(sock: socket.socket) -> None:
    if hasattr(socket, "SO_NOSIGPIPE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_NOSIGPIPE, 1)


def _darwin_peer(sock: socket.socket) -> PeerCredentials:
    import ctypes
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
    libc.getpeereid.restype = ctypes.c_int
    uid = ctypes.c_uint32()
    gid = ctypes.c_uint32()
    if libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise TransportError("peer uid unavailable")
    level = getattr(socket, "SOL_LOCAL", 0)
    raw = sock.getsockopt(level, 0x002, 4)  # LOCAL_PEERPID
    pid = struct.unpack("@i", raw)[0] if isinstance(raw, (bytes, bytearray)) else int(raw)
    return PeerCredentials(uid=int(uid.value), gid=int(gid.value), pid=pid)


_DRIVER_CODES = frozenset({"ok", "forbidden", "no_effect", "browser_app", "audit_unavailable"})


def _driver_code(code: str) -> bool:
    return code in _DRIVER_CODES or is_in_use(code)


def _linux_peer(sock: socket.socket) -> PeerCredentials:
    raw = sock.getsockopt(socket.SOL_SOCKET, 17, 12)  # SO_PEERCRED, struct ucred
    pid, uid, gid = struct.unpack("@iII", raw)
    return PeerCredentials(uid=uid, gid=gid, pid=pid)
