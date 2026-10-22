"""Settings → API clients: the operator's routes that create, list and revoke API clients, on a real PostgreSQL.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_usage_api_db import DSN, _run, _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _call(method: str, path: str, body: Any = None) -> tuple[int, dict[str, str], Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
    writer = _Writer()
    raw = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b""
    await server._route_api(method, path, {}, raw, writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = {key.lower(): value for key, value in (line.split(": ", 1) for line in lines[1:])}
    return int(lines[0].split()[1]), headers, json.loads(payload)


def test_create_list_and_revoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        status, headers, created = await _call("POST", "/api/clients", {"name": "my agent"})
        assert status == 201 and headers["cache-control"] == "no-store"
        token, client = created["token"], created["client"]
        assert token.startswith("lpc_") and client["name"] == "my agent"
        assert "token_sha256" not in client and token not in json.dumps(client)

        status, headers, listing = await _call("GET", "/api/clients")
        assert status == 200
        assert [item["id"] for item in listing["clients"]] == [client["id"]]
        assert token not in json.dumps(listing) and "sha256" not in json.dumps(listing)

        assert (await _call("POST", "/api/clients", {"name": "my agent"}))[0] == 409
        assert (await _call("POST", "/api/clients", {"name": " "}))[0] == 400
        assert (await _call("POST", "/api/clients", {"name": 3}))[0] == 400
        assert (await _call("POST", "/api/clients", b"not json"))[0] == 400
        assert (await _call("POST", "/api/clients", []))[0] == 400

        async with _serving(tmp_path) as server:
            status, _, body = await _send(server, "GET /api/v1/whoami", _bearer(token))
            assert status == 200 and body["result"]["client"] == {"id": client["id"], "name": "my agent"}

            status, _, revoked = await _call("DELETE", f"/api/clients/{client['id']}")
            assert status == 200 and revoked == {"revoked": client["id"]}
            status, _, missing = await _call("DELETE", f"/api/clients/{client['id']}")
            assert status == 404 and missing == {"error": f"no active API client {client['id']}"}
            assert (await _call("GET", "/api/clients"))[2] == {"clients": []}

            status, _, body = await _send(server, "GET /api/v1/whoami", _bearer(token))
            assert status == 401 and body["error"]["code"] == "unauthorized"

    _run(test, monkeypatch)
