"""Installation metadata is public; dashboard pages and APIs retain their gates."""

import asyncio
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace

import httpx

from looper.agents.http_server import AgentHTTPServer
from looper.agents import operator_auth


def test_installation_files_are_public_without_signing_in(tmp_path):
    public = Path(__file__).resolve().parents[1] / "web" / "public"
    static = tmp_path / "static"
    shutil.copytree(public, static)
    (static / "index.html").write_text("<html>private dashboard</html>")
    token = "operator_" + "a" * 32
    operator_auth.set_password(tmp_path, "admin", "synthetic-test-password")

    async def run():
        server = AgentHTTPServer(
            port=0, operator_token=token, static_dir=static, runtime_root=tmp_path,
            web_adapter=SimpleNamespace(), task_router=SimpleNamespace(), supervisor=SimpleNamespace(),
        )
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        assert port != 8767
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                manifest = await client.get("/manifest.webmanifest")
                assert manifest.status_code == 200
                assert manifest.headers["content-type"] == "application/manifest+json"
                assert "set-cookie" not in manifest.headers
                assert manifest.json()["start_url"] == "/"
                expected = {
                    "/icon-192.png": 192,
                    "/icon-512.png": 512,
                    "/icon-maskable-512.png": 512,
                }
                assert {icon["src"] for icon in manifest.json()["icons"]} == set(expected)
                for path, size in expected.items():
                    response = await client.get(path)
                    assert response.status_code == 200, path
                    assert response.headers["content-type"] == "image/png"
                    assert "set-cookie" not in response.headers
                    assert response.content == (static / path.lstrip("/")).read_bytes()
                    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
                    assert struct.unpack(">II", response.content[16:24]) == (size, size)

                # Positive control: this endpoint accepts an actual operator credential.
                response = await client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {token}"})
                assert response.status_code == 200
                assert response.json()["result"]["principal"] == "operator"
                for headers in ({}, {"Authorization": "Bearer invalid"}):
                    response = await client.post("/api/v1/system/status", json={}, headers=headers)
                    assert response.status_code == 401
                for path in ("/", "/index.html", "/manifest.webmanifest/other", "/icon-unknown.png"):
                    response = await client.get(path)
                    assert response.status_code == 200
                    assert "Sign in" in response.text and "private dashboard" not in response.text
                    assert 'rel="manifest" href="/manifest.webmanifest"' in response.text
                    assert "manifest-src 'self'" in response.headers["content-security-policy"]
                    assert "img-src 'self'" in response.headers["content-security-policy"]
                    assert response.headers["cache-control"] == "no-store"
                    assert "set-cookie" not in response.headers
        finally:
            await server.stop()

    asyncio.run(run())
