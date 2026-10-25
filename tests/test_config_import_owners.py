"""Staged imports reserve capacity and can be cancelled by their own caller."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from looper.agents import config_imports, config_transfer
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients
from tests.test_operator_auth import TOKEN

DOCUMENT = {"format": "looper-config/v3"}


@pytest.fixture
def preview(monkeypatch):
    monkeypatch.setattr(config_imports, "_run", AsyncMock(return_value=config_transfer.Plan()))


def test_pending_import_limit_is_per_caller(tmp_path, clients, preview):
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                                   task_router=server._task_router, runtime_root=tmp_path))
            for _ in range(config_imports.PENDING_MAX):
                status, _, answer = await _send(server, "POST /api/v1/config/import_preview",
                                               _bearer(CLIENT_TOKEN), json.dumps({"document": DOCUMENT}))
                assert status == 200, answer
            status, _, answer = await _send(server, "POST /api/v1/config/import_preview",
                                           _bearer(CLIENT_TOKEN), json.dumps({"document": DOCUMENT}))
            assert status == 409 and answer["error"]["code"] == "conflict"
            status, _, answer = await _send(server, "POST /api/v1/config/import_preview",
                                           _bearer(TOKEN), json.dumps({"document": DOCUMENT}))
            assert status == 200, answer
            status, _, answer = await _send(server, "POST /api/v1/config/import_upload", _bearer(TOKEN),
                                           json.dumps(DOCUMENT))
            assert status == 200, answer

    asyncio.run(asyncio.wait_for(run(), timeout=10))


@pytest.mark.parametrize("token", [CLIENT_TOKEN, TOKEN])
def test_v1_import_cancel_checks_owner_and_releases_capacity(tmp_path, monkeypatch, clients, preview, token):
    monkeypatch.setattr(config_imports, "PENDING_MAX", 1)

    async def run():
        async with _serving(tmp_path, listen=True) as server:
            server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
                                                   task_router=server._task_router, runtime_root=tmp_path))
            status, _, answer = await _send(server, "POST /api/v1/config/import_preview", _bearer(token),
                                           json.dumps({"document": DOCUMENT}))
            assert status == 200, answer
            import_id = answer["result"]["import_id"]
            path = tmp_path / config_imports.DIRNAME / f"{import_id}.json"
            other = TOKEN if token == CLIENT_TOKEN else CLIENT_TOKEN
            status, _, answer = await _send(server, "POST /api/v1/config/import_cancel", _bearer(other),
                                           json.dumps({"import_id": import_id}))
            assert status == 404 and answer["error"]["code"] == "not_found"
            assert path.exists()
            status, _, _ = await _send(server, f"DELETE /api/config/imports/{import_id}", _bearer(TOKEN))
            assert status == 404 and path.exists()
            status, _, answer = await _send(server, "POST /api/v1/config/import_cancel", _bearer(token),
                                           json.dumps({"import_id": import_id}))
            assert status == 200 and answer["result"] == {"deleted": import_id}
            assert not path.exists()
            status, _, _ = await _send(server, "POST /api/v1/config/import_cancel", _bearer(token),
                                       json.dumps({"import_id": import_id}))
            assert status == 404
            status, _, answer = await _send(server, "POST /api/v1/config/import_preview", _bearer(token),
                                           json.dumps({"document": DOCUMENT}))
            assert status == 200, answer

    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_receiving_imports_reserve_only_their_callers_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(config_imports, "PENDING_MAX", 1)

    async def run():
        imports = config_imports.ConfigImports(tmp_path)
        started, release = asyncio.Event(), asyncio.Event()

        async def preview(data, *, apply):
            started.set()
            await release.wait()
            return config_transfer.Plan()

        monkeypatch.setattr(config_imports, "_run", preview)

        async def receive(owner):
            reader = asyncio.StreamReader()
            body = json.dumps(DOCUMENT).encode()
            reader.feed_data(body)
            reader.feed_eof()
            return await imports.receive(reader, len(body), owner=owner)

        receiving = asyncio.create_task(receive("client:one"))
        try:
            await started.wait()
            with pytest.raises(config_imports.ImportConflict):
                await receive("client:one")
            release.set()
            await receive("client:two")
        finally:
            release.set()
            await receiving
            imports.clear()

    asyncio.run(asyncio.wait_for(run(), timeout=10))
