"""Serve-side desktop RPC client.

A missing socket, a refused connection, or a failure to connect returns
``desktop_unavailable``. The client does not start a desktop service, a
browser, or a runtime computer-use tool, and it does not send a request twice.
Write actions (``NON_RETRIED``) time out as ``timeout`` after the single send.
"""

from __future__ import annotations

import secrets
import socket
import struct
from collections.abc import Mapping
from pathlib import Path

from .contract import (
    MAGIC,
    NON_RETRIED,
    PROTOCOL_VERSION,
    TIMEOUT_S,
    ContractError,
    Response,
    encode_request,
    loads,
    parse_request,
    parse_response,
)
from .transport import STATUS_OK, STATUS_PEER, STATUS_VERSION, TransportError, read_frame, read_handshake, write_frame, write_handshake

_HANDSHAKE_STATUS = {
    STATUS_VERSION: ("version_mismatch", "version mismatch"),
    STATUS_PEER: ("peer_mismatch", "peer uid mismatch"),
}


class DesktopClient:
    def __init__(self, path: str | Path, *, timeouts: Mapping[str, float] | None = None) -> None:
        self.path = Path(path)
        self.timeouts = dict(TIMEOUT_S)
        if timeouts:
            unknown = set(timeouts) - set(TIMEOUT_S)
            if unknown:
                raise ContractError("bad_request", "unknown timeout")
            self.timeouts.update(timeouts)
        self.session_id: str | None = None
        self.capability: str | None = None

    def open_session(self, *, nonce: str | None = None) -> Response:
        response = self._call("session.open", {}, nonce=nonce, session_id=None, capability=None)
        if response.code == "ok":
            session_id = response.body.get("session_id")
            capability = response.body.get("capability")
            if not isinstance(session_id, str) or not isinstance(capability, str):
                return Response("bad_request", "session.open", {}, response.nonce, response.kind)
            self.session_id = session_id
            self.capability = capability
        return response

    def request(self, kind: str, body: Mapping | None = None, *, nonce: str | None = None,
                session_id: str | None = None, capability: str | None = None) -> Response:
        """Send one request. ``act``, ``grant``, ``revoke`` and ``stop`` are never retried."""
        if kind == "session.open":
            raise ContractError("bad_request", "use open_session")
        if kind in NON_RETRIED and kind not in self.timeouts:
            raise ContractError("bad_request", "write timeout")
        sid = self.session_id if session_id is None else session_id
        cap = self.capability if capability is None else capability
        return self._call(kind, dict(body or {}), nonce=nonce, session_id=sid, capability=cap)

    def _call(self, kind: str, body: Mapping, *, nonce: str | None, session_id: str | None,
              capability: str | None) -> Response:
        token = nonce if nonce is not None else secrets.token_urlsafe(16)
        try:
            payload = encode_request(kind, token, body, session_id=session_id, capability=capability)
            parse_request(loads(payload))
        except ContractError as exc:
            return Response(exc.code, exc.message, {}, token if isinstance(token, str) else "", kind)
        try:
            return self._exchange(kind, payload, self.timeouts[kind])
        except TimeoutError:
            return Response("timeout", "timed out", {}, token, kind)

    def _exchange(self, kind: str, payload: bytes, timeout: float) -> Response:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError:
            return _unavailable(kind)
        try:
            if hasattr(socket, "SO_NOSIGPIPE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_NOSIGPIPE, 1)
            sock.settimeout(timeout)
            try:
                sock.connect(str(self.path))
            except TimeoutError:
                raise
            except OSError:
                return _unavailable(kind)
            try:
                write_handshake(sock, STATUS_OK, version=PROTOCOL_VERSION)
                magic, version, status = read_handshake(sock)
            except TimeoutError:
                raise
            except (OSError, EOFError, struct.error):
                return Response("bad_request", "handshake", {}, "", kind)
            if magic != MAGIC or version != PROTOCOL_VERSION or status != STATUS_OK:
                code, message = _HANDSHAKE_STATUS.get(status, ("version_mismatch", "version mismatch"))
                return Response(code, message, {}, "", kind)
            try:
                write_frame(sock, payload)
                raw = read_frame(sock)
            except TimeoutError:
                raise
            except (OSError, EOFError, TransportError):
                return Response("bad_request", "frame", {}, "", kind)
            try:
                return parse_response(loads(raw))
            except ContractError as exc:
                return Response(exc.code, exc.message, {}, "", kind)
        finally:
            sock.close()


def _unavailable(kind: str) -> Response:
    return Response("desktop_unavailable", "desktop unavailable", {}, "", kind)
