"""The operator sees a startup snapshot through the real v1 HTTP transport."""
import asyncio
from pathlib import Path

from looper import components
from looper.agents import api_v1
from looper.db import api_clients
from tests.test_api_v1_auth import CLIENT, CLIENT_TOKEN, _bearer, _send, _serving
from tests.test_operator_auth import TOKEN


def test_components_require_operator_and_preserve_loaded_snapshot(tmp_path, monkeypatch):
    async def find(token, **kwargs):
        return CLIENT if token == CLIENT_TOKEN else None

    async def touch(*args, **kwargs):
        pass

    monkeypatch.setattr(api_clients, "find", find)
    monkeypatch.setattr(api_clients, "touch", touch)

    async def run():
        async with _serving(tmp_path, listen=True) as server:
            expected = components.manifest_from_directory(
                Path(components.__file__).resolve().parent.parent)
            for token, status in ((None, 401), (CLIENT_TOKEN, 403), (TOKEN, 200)):
                actual, _, answer = await _send(server, "POST /api/v1/system/components",
                                                _bearer(token) if token else {})
                assert actual == status
            result = answer["result"]
            assert result["version"] == expected.version
            assert result["components"] == expected.components
            assert result["adoption"] == expected.adoption
            assert result["verification"] == expected.verification
            assert result["loaded_at"]

            def refuse_recompute(*args, **kwargs):
                raise AssertionError("an endpoint must not recompute the loaded snapshot")

            monkeypatch.setattr(components, "manifest_from_directory", refuse_recompute)
            status, headers, repeat = await _send(server, "POST /api/v1/system/components", _bearer(TOKEN))
            assert status == 200 and headers["cache-control"] == "no-store"
            assert repeat["result"] == result

    asyncio.run(run())


def test_components_are_registered_for_generated_cli():
    operation = api_v1.document()["paths"]["/system/components"]["post"]
    assert operation["operationId"] == "system.components"
    assert operation["x-looper-operator-only"] is True
    assert operation["x-looper-risk"] == "read"
    assert "200" in operation["responses"]


def test_components_get_is_refused_like_other_v1_operations(tmp_path):
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            for path in ("/api/v1/system/status", "/api/v1/system/components"):
                status, _, answer = await _send(server, f"GET {path}", _bearer(TOKEN))
                assert status == 405
                assert answer["error"] == {"code": "invalid", "message": "use POST"}

    asyncio.run(run())
