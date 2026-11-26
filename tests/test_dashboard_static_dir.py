"""`looper serve` serves the checkout's dashboard, else the copy packaged in the wheel."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper.agents import http_server


def _dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, checkout_built: bool) -> tuple[Path, Path]:
    checkout, packaged = tmp_path / "web" / "dist", tmp_path / "looper" / "web" / "dashboard"
    for d in (checkout, packaged):
        d.mkdir(parents=True)
    (packaged / "index.html").write_text("packaged")
    if checkout_built:
        (checkout / "index.html").write_text("checkout")
    monkeypatch.setattr(http_server, "_CHECKOUT_DASHBOARD", checkout)
    monkeypatch.setattr(http_server, "_PACKAGED_DASHBOARD", packaged)
    return checkout, packaged


@pytest.mark.parametrize("checkout_built", [True, False])
def test_checkout_build_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkout_built: bool) -> None:
    monkeypatch.delenv("PYTEST_LOOPER_STATIC_DIR", raising=False)
    checkout, packaged = _dirs(tmp_path, monkeypatch, checkout_built=checkout_built)
    # An installed wheel has no checkout build and serves its packaged copy.
    assert http_server.default_static_dir() == (checkout if checkout_built else packaged)


def test_static_dir_override_beats_checkout_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout, _packaged = _dirs(tmp_path, monkeypatch, checkout_built=True)
    override = tmp_path / "staged"
    override.mkdir()
    monkeypatch.setenv("PYTEST_LOOPER_STATIC_DIR", str(override))
    assert http_server.default_static_dir() == override
    monkeypatch.delenv("PYTEST_LOOPER_STATIC_DIR")
    assert http_server.default_static_dir() == checkout


@pytest.mark.parametrize(("path", "expected"), [
    ("/../static-secret/leak.txt", 404),
    ("/%2e%2e/static-secret/leak.txt", 404),
    ("/prefix-link/leak.txt", 404),
    ("/outside-link/leak.txt", 404),
    ("/assets/app.js", 200),
])
def test_static_files_stay_inside_directory(tmp_path: Path, path: str, expected: int) -> None:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    asset = b"console.log('dashboard');"
    (static / "assets" / "app.js").write_bytes(asset)
    secret = b"outside-static-directory"
    for name, link in (("static-secret", "prefix-link"), ("private", "outside-link")):
        sibling = tmp_path / name
        sibling.mkdir()
        (sibling / "leak.txt").write_bytes(secret)
        (static / link).symlink_to(sibling, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    async def run() -> None:
        server = http_server.AgentHTTPServer(
            port=0, static_dir=static, runtime_root=runtime,
            web_adapter=SimpleNamespace(), task_router=SimpleNamespace(), supervisor=SimpleNamespace(),
        )
        await server.start()
        try:
            port = server._port
            assert port != 8767
            # Write the request target verbatim: HTTP clients may normalize dot segments.
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
                await writer.drain()
                raw = await asyncio.wait_for(reader.read(), timeout=5)
            finally:
                writer.close()
                await writer.wait_closed()
            head, separator, body = raw.partition(b"\r\n\r\n")
            assert separator
            assert int(head.split(b"\r\n", 1)[0].split()[1]) == expected
            assert secret not in body
            if expected == 200:
                assert body == asset
        finally:
            await server.stop()

    asyncio.run(run())


def _status_and_body(raw: bytes) -> tuple[int, bytes]:
    head, separator, body = raw.partition(b"\r\n\r\n")
    assert separator
    return int(head.split(b"\r\n", 1)[0].split()[1]), body


async def _raw_get(port: int, target: bytes) -> bytes:
    # Write the request target verbatim: HTTP clients may normalize dot segments.
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(b"GET " + target + f" HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await writer.drain()
        return await asyncio.wait_for(reader.read(), timeout=5)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("target", [
    pytest.param(b"/" + b"a" * 5000, id="5000-characters"),
    pytest.param(b"/" + b"b" * 300, id="300-character-segment"),
    pytest.param(b"/a\x00b", id="nul"),
])
def test_unresolvable_static_path_answers_not_found(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, target: bytes,
) -> None:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "assets" / "app.js").write_bytes(b"console.log('dashboard');")
    # Present so a path the filesystem rejects must not fall through to the dashboard page.
    (static / "index.html").write_bytes(b"dashboard-index")
    secret = b"outside-static-directory"
    sibling = tmp_path / "static-secret"
    sibling.mkdir()
    (sibling / "leak.txt").write_bytes(secret)
    (static / "prefix-link").symlink_to(sibling, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    async def run() -> None:
        server = http_server.AgentHTTPServer(
            port=0, static_dir=static, runtime_root=runtime,
            web_adapter=SimpleNamespace(), task_router=SimpleNamespace(), supervisor=SimpleNamespace(),
        )
        await server.start()
        try:
            port = server._port
            assert port != 8767
            caplog.clear()
            contained = await _raw_get(port, b"/prefix-link/leak.txt")
            contained_status, contained_body = _status_and_body(contained)
            assert contained_status == 404
            assert secret not in contained_body
            caplog.clear()
            raw = await _raw_get(port, target)
            status, body = _status_and_body(raw)
            assert status == contained_status
            assert body == contained_body
            assert b"Traceback" not in raw
            assert secret not in raw
            assert b"dashboard-index" not in raw
            warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
            assert len(warnings) <= 1
            assert all(record.exc_info is None for record in warnings)
        finally:
            await server.stop()

    asyncio.run(run())
