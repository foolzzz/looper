"""Desktop RPC over a real unix socket, with the in-memory driver behind it."""

import os
import socket
import struct
import threading
import uuid
from pathlib import Path

import pytest

from looper.agents.browser.desktop.client import DesktopClient
from looper.agents.browser.desktop.contract import (
    MAGIC,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    dumps,
    loads,
    parse_response,
)
from looper.agents.browser.desktop.fake_driver import FakeDriver
from looper.agents.browser.desktop.transport import (
    STATUS_OK,
    STATUS_VERSION,
    DisconnectLog,
    Endpoint,
    PeerCredentials,
    read_frame,
    read_handshake,
    write_frame,
    write_handshake,
)

_NONCE = "n" * 16


def _socket() -> Path:
    path = Path("/tmp") / f"looper-desktop-{uuid.uuid4().hex[:8]}.sock"
    if path.parent != Path("/tmp") or not path.name.startswith("looper-desktop-") or not path.name.endswith(".sock"):
        raise RuntimeError(path)
    return path


def _unlink(path: Path) -> None:
    if path.parent != Path("/tmp") or not path.name.startswith("looper-desktop-") or not path.name.endswith(".sock"):
        raise RuntimeError(path)
    path.unlink(missing_ok=True)


def _boot(path: Path, *, driver=None, enabled=True, **kwargs):
    driver = FakeDriver() if driver is None else driver
    log = DisconnectLog()
    endpoint = Endpoint(path, driver.handle, log, enabled=enabled, **kwargs)
    endpoint.start()
    return endpoint, driver, log


def _open(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    sock.connect(str(path))
    write_handshake(sock, STATUS_OK, version=PROTOCOL_VERSION)
    magic, version, status = read_handshake(sock)
    assert (magic, version, status) == (MAGIC, PROTOCOL_VERSION, STATUS_OK)
    return sock


def test_fake_driver_happy_path_and_kernel_peer_uid():
    path = _socket()
    endpoint, driver, log = _boot(path)
    try:
        client = DesktopClient(path)
        opened = client.open_session()
        assert opened.code == "ok" and opened.body["session_id"] == client.session_id
        assert client.request("observe").body == {"observation_id": "obs-1", "text": "window"}
        shot = client.request("screenshot")
        assert shot.body == {"redacted": True, "password_controls": True, "custom_regions": []}
        assert client.request("act", {"name": "press"}).code == "ok"
        assert client.request("grant", {"bundle_id": "example.app"}).code == "ok"
        assert client.request("revoke", {"bundle_id": "example.app"}).code == "ok"
        assert client.request("health").body == {"status": "ok"}
        assert log.records == []
        assert client.request("stop").code == "ok"
        denied = client.request("act", {"name": "again"})
        assert denied.code == "forbidden"
        assert [record.reason for record in log.records] == ["forbidden"]
        assert driver.applied == [{"name": "press"}]
        assert [item[0] for item in driver.grants] == ["grant", "revoke"]
        assert (path.stat().st_mode & 0o777) == 0o600
        assert endpoint.last_peer is not None
        assert endpoint.last_peer.uid == os.geteuid()
        assert endpoint.last_peer.pid == os.getpid()
    finally:
        endpoint.close()
        _unlink(path)


@pytest.mark.parametrize("code", ["no_effect", "browser_app", "audit_unavailable", "forbidden", "in use by other-session"])
def test_a_scripted_failure_does_not_apply_the_action(code):
    path = _socket()
    driver = FakeDriver()
    driver.script("act", code)
    endpoint, driver, _log = _boot(path, driver=driver)
    try:
        client = DesktopClient(path)
        assert client.open_session().code == "ok"
        result = client.request("act", {"name": "press"})
        assert result.code == code
        assert driver.applied == []
    finally:
        endpoint.close()
        _unlink(path)


def test_same_uid_self_report_is_not_trusted():
    """A claim of this process's own uid is not a capability, even on a real socket."""
    path = _socket()
    endpoint, driver, log = _boot(path)
    try:
        sock = _open(path)
        try:
            frame = dumps({
                "body": {},
                "caller_uid": os.geteuid(),
                "kind": "observe",
                "nonce": _NONCE,
                "v": PROTOCOL_VERSION,
            })
            write_frame(sock, frame)
            response = parse_response(loads(read_frame(sock)))
        finally:
            sock.close()
        assert response.code == "untrusted_identity"
        assert driver.calls == []
        assert [record.reason for record in log.records] == ["untrusted_identity"]
    finally:
        endpoint.close()
        _unlink(path)


def test_a_real_capability_plus_a_matching_uid_claim_is_still_rejected():
    path = _socket()
    endpoint, driver, log = _boot(path)
    try:
        client = DesktopClient(path)
        assert client.open_session().code == "ok"
        sock = _open(path)
        try:
            frame = dumps({
                "body": {"name": "press"},
                "caller_uid": os.geteuid(),
                "capability": client.capability,
                "kind": "act",
                "nonce": "c" * 16,
                "session_id": client.session_id,
                "v": PROTOCOL_VERSION,
            })
            write_frame(sock, frame)
            response = parse_response(loads(read_frame(sock)))
        finally:
            sock.close()
        assert response.code == "untrusted_identity"
        assert driver.applied == []
        assert log.records[-1].reason == "untrusted_identity"
    finally:
        endpoint.close()
        _unlink(path)


def test_a_mismatched_peer_uid_is_disconnected():
    path = _socket()

    def other(_sock):
        return PeerCredentials(uid=0, gid=0, pid=1)

    endpoint, driver, log = _boot(path, credentials=other)
    try:
        result = DesktopClient(path).open_session()
        assert result.code == "peer_mismatch"
        assert driver.calls == []
        assert log.records[0].reason == "peer_mismatch"
        assert log.records[0].peer_uid == 0
    finally:
        endpoint.close()
        _unlink(path)


def test_a_replayed_nonce_is_rejected():
    path = _socket()
    endpoint, _driver, log = _boot(path)
    try:
        client = DesktopClient(path)
        assert client.open_session(nonce=_NONCE).code == "ok"
        replayed = client.open_session(nonce=_NONCE)
        assert replayed.code == "nonce_replay"
        assert [record.reason for record in log.records] == ["nonce_replay"]
    finally:
        endpoint.close()
        _unlink(path)


def test_an_expired_capability_is_rejected():
    path = _socket()
    clock = {"now": 0.0}
    endpoint, driver, log = _boot(path, ttl_s=10, clock=lambda: clock["now"])
    try:
        client = DesktopClient(path)
        assert client.open_session().code == "ok"
        clock["now"] = 10.0
        expired = client.request("health")
        assert expired.code == "capability_expired"
        assert driver.calls == []
        assert log.records[-1].reason == "capability_expired"
    finally:
        endpoint.close()
        _unlink(path)


def test_a_capability_does_not_cross_sessions():
    path = _socket()
    endpoint, driver, log = _boot(path)
    try:
        first = DesktopClient(path)
        second = DesktopClient(path)
        assert first.open_session().code == "ok"
        assert second.open_session().code == "ok"
        crossed = first.request("observe", session_id=second.session_id, capability=first.capability)
        assert crossed.code == "cross_session"
        assert driver.calls == []
        assert log.records[-1].reason == "cross_session"
        wrong = first.request("observe", capability="f" * 16)
        assert wrong.code == "forbidden"
        assert driver.calls == []
    finally:
        endpoint.close()
        _unlink(path)


def test_an_oversize_frame_closes_without_a_body_read():
    path = _socket()
    endpoint, driver, log = _boot(path)
    try:
        sock = _open(path)
        try:
            sock.sendall(struct.pack(">I", MAX_FRAME_BYTES + 1))
            response = parse_response(loads(read_frame(sock)))
        finally:
            sock.close()
        assert response.code == "frame_too_large"
        assert driver.calls == []
        assert log.records[-1].reason == "frame_too_large"
    finally:
        endpoint.close()
        _unlink(path)


def test_a_version_mismatch_is_rejected_at_the_handshake():
    path = _socket()
    endpoint, driver, log = _boot(path)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect(str(path))
            write_handshake(sock, STATUS_OK, version=99)
            magic, _version, status = read_handshake(sock)
        finally:
            sock.close()
        assert magic == MAGIC and status == STATUS_VERSION
        assert driver.calls == []
        assert log.records[-1].reason == "version_mismatch"
    finally:
        endpoint.close()
        _unlink(path)


def test_a_missing_socket_is_desktop_unavailable_and_stays_missing():
    path = _socket()
    try:
        assert not path.exists()
        result = DesktopClient(path).open_session()
        assert result.code == "desktop_unavailable"
        assert not path.exists()
    finally:
        _unlink(path)


def test_disabled_does_not_create_a_socket():
    path = _socket()
    endpoint, _driver, _log = _boot(path, enabled=False)
    try:
        assert endpoint.enabled is False
        assert not path.exists()
        assert DesktopClient(path).open_session().code == "desktop_unavailable"
        assert not path.exists()
    finally:
        endpoint.close()
        _unlink(path)


def test_a_timed_out_write_is_not_sent_again(monkeypatch):
    path = _socket()
    sent = []
    release = threading.Event()
    calls = []

    def counting(sock, payload):
        sent.append(payload)
        return write_frame(sock, payload)

    def handle(kind, body, session_id):
        if kind == "act":
            calls.append(kind)
            release.wait(5)
            return "ok", {}
        return FakeDriver().handle(kind, body, session_id)

    monkeypatch.setattr("looper.agents.browser.desktop.client.write_frame", counting)
    endpoint = Endpoint(path, handle, DisconnectLog(), enabled=True)
    endpoint.start()
    try:
        client = DesktopClient(path, timeouts={"act": 1.5})
        assert client.open_session().code == "ok"
        result = client.request("act", {"name": "press"})
        assert result.code == "timeout"
        assert len(sent) == 2  # session.open, then one act
        assert calls == ["act"]
    finally:
        release.set()
        endpoint.close()
        _unlink(path)
