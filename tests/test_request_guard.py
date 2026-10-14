"""The local API answers the dashboard and local clients, never another web page, and reads bounded bodies."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper.agents.http_server import REQUEST_BODY_MAX_BYTES, AgentHTTPServer
from looper.agents.request_guard import refusal

ATTACKER = "https://attacker.example"


@pytest.mark.parametrize(
    ("bind", "host", "origin"),
    [
        ("127.0.0.1", "127.0.0.1:8767", None),                       # curl, CLI
        ("127.0.0.1", "127.0.0.1:8767", "http://127.0.0.1:8767"),    # the dashboard itself
        ("127.0.0.1", "localhost:8767", "http://localhost:8767"),
        ("127.0.0.1", "localhost:5173", "http://localhost:5173"),    # vite dev proxy keeps Host and Origin
        ("127.0.0.1", "[::1]:8767", "http://[::1]:8767"),
        ("127.0.0.1", "", None),                                     # no Host: not a browser
        ("0.0.0.0", "10.0.0.5:8767", "http://10.0.0.5:8767"),        # exposed on purpose: same origin only
    ],
)
def test_allowed(bind: str, host: str, origin: str | None) -> None:
    headers = {"host": host} if host else {}
    if origin is not None:
        headers["origin"] = origin
    assert refusal(headers, bind_host=bind) is None


@pytest.mark.parametrize(
    ("bind", "host", "origin"),
    [
        ("127.0.0.1", "127.0.0.1:8767", ATTACKER),                   # a page on another site
        ("127.0.0.1", "127.0.0.1:8767", "http://127.0.0.1:9999"),    # another local app's page
        ("127.0.0.1", "127.0.0.1:8767", "null"),                     # sandboxed frame, file://
        ("127.0.0.1", "evil.example:8767", "http://evil.example:8767"),  # DNS rebinding
        ("127.0.0.1", "evil.example:8767", None),
        ("0.0.0.0", "10.0.0.5:8767", ATTACKER),
    ],
)
def test_refused(bind: str, host: str, origin: str | None) -> None:
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    assert refusal(headers, bind_host=bind)


async def _exchange(port: int, request: str) -> tuple[str, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request.encode())
    await writer.drain()
    raw = (await asyncio.wait_for(reader.read(), timeout=10)).decode(errors="replace")
    writer.close()
    head, _, body = raw.partition("\r\n\r\n")
    return head, body


def _serve(*requests: str) -> list[tuple[str, str]]:
    async def _run() -> list[tuple[str, str]]:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "index.html").write_text("<html>dashboard</html>")
            server = AgentHTTPServer(
                port=0, web_adapter=SimpleNamespace(), task_router=SimpleNamespace(),
                supervisor=SimpleNamespace(), static_dir=Path(tmp),
            )
            await server.start()
            port = server._server.sockets[0].getsockname()[1]
            try:
                return [await _exchange(port, r) for r in requests]
            finally:
                await server.stop()
    return asyncio.run(_run())


def test_server_refuses_cross_site_requests_and_sends_no_cors_headers() -> None:
    body = '{"requirement": "x", "project_id": "proj-0", "work_dir": "/tmp"}'
    (cross_post, _), (preflight, _), (same, page) = _serve(
        "POST /api/tasks/create HTTP/1.1\r\nHost: 127.0.0.1:8767\r\n"
        f"Origin: {ATTACKER}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n\r\n{body}",
        "OPTIONS /api/tasks/create HTTP/1.1\r\nHost: 127.0.0.1:8767\r\n"
        f"Origin: {ATTACKER}\r\nAccess-Control-Request-Method: POST\r\n\r\n",
        "GET / HTTP/1.1\r\nHost: 127.0.0.1:8767\r\nOrigin: http://127.0.0.1:8767\r\n\r\n",
    )
    assert cross_post.startswith("HTTP/1.1 403")
    assert preflight.startswith("HTTP/1.1 403")
    assert same.startswith("HTTP/1.1 200") and "dashboard" in page
    for head in (cross_post, preflight, same):
        assert "access-control-allow-origin" not in head.lower()


def test_a_body_over_the_cap_is_refused_before_it_is_read() -> None:
    """The cap fits the largest upload, a 2 MiB knowledge document as base64 in JSON, with room to spare."""
    assert REQUEST_BODY_MAX_BYTES == 8 * 1024 * 1024
    head = "Host: 127.0.0.1:8767\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n"
    (over, refusal), (at_cap, _) = _serve(
        # Only the headers: the answer comes without the server waiting for the body.
        "POST /api/projects/proj-0/knowledge HTTP/1.1\r\n" + head.format(REQUEST_BODY_MAX_BYTES + 1),
        "POST /api/nothing-here HTTP/1.1\r\n" + head.format(REQUEST_BODY_MAX_BYTES) + "x" * REQUEST_BODY_MAX_BYTES,
    )
    assert over.startswith("HTTP/1.1 413")
    assert "at most 8 MiB (8388608 bytes)" in json.loads(refusal)["error"]
    assert at_cap.startswith("HTTP/1.1 404")  # read whole and routed
