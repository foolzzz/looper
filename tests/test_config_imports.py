"""Configuration imports through the dashboard's API: a streamed upload with its own cap, a preview, then apply.

The tests that preview or apply need PostgreSQL (LOOPER_TEST_DATABASE_URL); each
works in new schemas of its own, dropped afterwards. The server is a real
``AgentHTTPServer`` on a free port, spoken to over a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import config_imports
from looper.agents.http_server import CONFIG_IMPORTS_PATH, REQUEST_BODY_MAX_BYTES, AgentHTTPServer
from tests.test_catalog_db import DSN

needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
UNKNOWN = "cfgimp-0123456789abcdef"


@contextlib.asynccontextmanager
async def _server(runtime_root: Path) -> AsyncIterator[int]:
    server = AgentHTTPServer(port=0, web_adapter=SimpleNamespace(), task_router=SimpleNamespace(),
                             supervisor=SimpleNamespace(), static_dir=runtime_root, runtime_root=runtime_root)
    await server.start()
    try:
        yield server._server.sockets[0].getsockname()[1]
    finally:
        await server.stop()


async def _request(port: int, method: str, path: str, body: bytes | Path = b"", *, length: int | None = None,
                   headers: str = "") -> tuple[int, dict[str, Any]]:
    """Send one request (``body`` a file's path is sent piece by piece) and read the answer: status and JSON."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    size = body.stat().st_size if isinstance(body, Path) else len(body)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {size if length is None else length}\r\n{headers}\r\n".encode())
    if isinstance(body, Path):
        with body.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                writer.write(chunk)
                await writer.drain()
    else:
        writer.write(body)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=60)
    writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def _left(runtime_root: Path) -> list[str]:
    directory = runtime_root / config_imports.DIRNAME
    return sorted(os.listdir(directory)) if directory.exists() else []


# --- Without a database: the route's cap, the guard and files that are not configuration files ---


def test_only_the_import_route_takes_more_than_the_request_cap(tmp_path: Path) -> None:
    async def _test() -> None:
        async with _server(tmp_path) as port:
            # Only the headers: each answer comes without the server waiting for the body.
            other = await _request(port, "POST", "/api/catalog/upload", length=REQUEST_BODY_MAX_BYTES + 1)
            over = await _request(port, "POST", CONFIG_IMPORTS_PATH, length=config_imports.UPLOAD_MAX_BYTES + 1)
        assert other[0] == 413 and "the server takes at most 8 MiB" in other[1]["error"]
        assert config_imports.UPLOAD_MAX_BYTES == 256 * 1024 * 1024
        assert over[0] == 413 and "a configuration import takes at most 256 MiB" in over[1]["error"]
        assert _left(tmp_path) == []

    asyncio.run(_test())


def test_another_site_cannot_upload(tmp_path: Path) -> None:
    async def _test() -> None:
        async with _server(tmp_path) as port:
            status, answer = await _request(port, "POST", CONFIG_IMPORTS_PATH, length=REQUEST_BODY_MAX_BYTES * 2,
                                            headers="Origin: https://attacker.example\r\n")
        assert status == 403 and "cross-origin" in answer["error"]
        assert _left(tmp_path) == []

    asyncio.run(_test())


@pytest.mark.parametrize(("body", "error"), [
    (b"", "the request holds no file"),
    (b"PK\x03\x04 a zip", "the uploaded file is not a LoopX configuration file"),
    (b"\xff\xfe{}", "the uploaded file is not a LoopX configuration file"),
    (b"[]", "is not a loopx-config/v1 file (format: None)"),
    (b'{"format": "loopx-config/v2"}', "is not a loopx-config/v1 file (format: 'loopx-config/v2')"),
    (b'{"format": "loopx-config/v1", "catalog": {}}', "catalog must be a JSON list"),
], ids=["empty", "not-json", "not-utf8", "no-format", "newer-version", "bad-section"])
def test_a_file_that_is_not_a_configuration_file_is_refused_and_removed(
    tmp_path: Path, body: bytes, error: str,
) -> None:
    async def _test() -> None:
        async with _server(tmp_path) as port:
            status, answer = await _request(port, "POST", CONFIG_IMPORTS_PATH, body)
            assert _left(tmp_path) == []
        assert status == 400 and error in answer["error"], answer
        assert str(tmp_path) not in answer["error"]

    asyncio.run(_test())


@pytest.mark.parametrize(("method", "path"), [
    ("POST", f"{CONFIG_IMPORTS_PATH}/{UNKNOWN}/apply"), ("DELETE", f"{CONFIG_IMPORTS_PATH}/{UNKNOWN}"),
    ("POST", f"{CONFIG_IMPORTS_PATH}/not-an-id/apply"),
])
def test_an_unknown_import_is_not_found(tmp_path: Path, method: str, path: str) -> None:
    async def _test() -> tuple[int, dict[str, Any]]:
        async with _server(tmp_path) as port:
            return await _request(port, method, path)

    assert asyncio.run(_test())[0] == 404


def test_the_server_removes_files_a_previous_one_left_and_its_own_when_it_stops(tmp_path: Path) -> None:
    async def _test() -> None:
        directory = tmp_path / config_imports.DIRNAME
        directory.mkdir()
        (directory / f"{UNKNOWN}.json").write_text("{}")
        async with _server(tmp_path):
            assert _left(tmp_path) == []
            directory.mkdir()
            (directory / f"{UNKNOWN}.json").write_text("{}")
        assert not directory.exists()

    asyncio.run(_test())


# --- With a database: preview, apply, expiry ---


@contextlib.asynccontextmanager
async def _serving(monkeypatch: pytest.MonkeyPatch, pool: Any, runtime_root: Path) -> AsyncIterator[int]:
    from tests.test_config_transfer_db import _serve

    _serve(monkeypatch, pool)
    async with _server(runtime_root) as port:
        yield port


async def _export(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, big: bool) -> Path:
    """A configuration file of an installation using every section (``big``: over the request cap, too)."""
    from looper.db import library as db_library
    from tests.test_config_transfer_db import _installation, _item, _loopx, _populate

    exported = tmp_path / "config.json"
    async with _installation() as (source, source_url):
        await _populate(monkeypatch, source, tmp_path / "calc")
        if big:
            body = ("A line of a long reference document, 中文 included.\n" * 40_000)[: 1_900_000]
            async with source.acquire() as conn, conn.transaction():
                for n in range(5):
                    await db_library.insert_item(conn, _item("skill", f"big-{n}", [
                        {"path": "SKILL.md", "content": f"---\nname: big-{n}\n---\n"},
                        {"path": "reference.md", "content": body}]), origin="upload")
        code, _, err = await _loopx(monkeypatch, source_url, "config", "export", str(exported), "--include-projects")
        assert code == 0, err
    return exported


@needs_db
def test_a_file_over_the_request_cap_is_previewed_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from tests.test_config_transfer_db import _installation, _rows

    async def _test() -> None:
        exported = await _export(monkeypatch, tmp_path, big=True)
        assert exported.stat().st_size > REQUEST_BODY_MAX_BYTES
        runtime_root = tmp_path / "runtime"
        async with _installation() as (target, _), _serving(monkeypatch, target, runtime_root) as port:
            before = await _rows(target)

            status, answer = await _request(port, "POST", CONFIG_IMPORTS_PATH, exported)

            assert status == 201, answer
            assert answer["bytes"] == exported.stat().st_size
            assert answer["file"]["format"] == "loopx-config/v1" and answer["file"]["secrets"] == "omitted"
            plan = answer["plan"]
            assert not plan["applied"]
            assert plan["counts"]["catalog"] == {"added": 10, "changed": 0, "unchanged": 0, "skipped": 0}
            assert {(n["section"], n["key"]) for n in plan["need_secret"]} >= {
                ("providers", "anthropic-key"), ("catalog", "mcp:web")}
            assert all("target" not in n for n in plan["need_secret"])
            assert await _rows(target) == before
            # The file waits, readable by its owner only.
            [name] = _left(runtime_root)
            assert name == f"{answer['import_id']}.json"
            mode = stat.S_IMODE((runtime_root / config_imports.DIRNAME / name).stat().st_mode)
            assert mode == 0o600

    asyncio.run(_test())


@needs_db
def test_applying_writes_what_the_cli_apply_writes_and_removes_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from tests.test_config_transfer_db import _installation, _loopx, _plan, _state

    async def _test() -> None:
        exported = await _export(monkeypatch, tmp_path, big=False)
        runtime_root = tmp_path / "runtime"
        async with _installation() as (by_api, _), _installation() as (by_cli, cli_url):
            async with _serving(monkeypatch, by_api, runtime_root) as port:
                status, uploaded = await _request(port, "POST", CONFIG_IMPORTS_PATH, exported)
                assert status == 201, uploaded

                status, applied = await _request(port, "POST", f"{CONFIG_IMPORTS_PATH}/{uploaded['import_id']}/apply")

                assert status == 200, applied
                assert _left(runtime_root) == []
                again = await _request(port, "POST", f"{CONFIG_IMPORTS_PATH}/{uploaded['import_id']}/apply")
                assert again[0] == 404 and "upload the file again" in again[1]["error"]

            code, out, err = await _loopx(monkeypatch, cli_url, "config", "import", str(exported), "--apply", "--json")
            assert code == 0, err
            plan = applied["plan"]
            targets = {(n["section"], n["key"]): n.pop("target", None) for n in plan["need_secret"]}
            assert plan == _plan(out)
            assert plan["applied"] and plan["counts"] == uploaded["plan"]["counts"]
            assert await _state(by_api) == await _state(by_cli)

            # Each secret to fill in names the stored row whose form takes it.
            async with by_api.acquire() as conn:
                provider = await conn.fetchval("SELECT id FROM llm_providers WHERE name = 'anthropic-key'")
                web = await conn.fetchval("SELECT id FROM library_items WHERE kind = 'mcp' AND name = 'web'")
                telegram = await conn.fetchval("SELECT channel_id FROM channel_configs "
                                               "WHERE channel_type = 'telegram'")
            assert targets == {("providers", "anthropic-key"): {"kind": "provider", "id": provider},
                               ("catalog", "mcp:web"): {"kind": "mcp", "id": web},
                               ("catalog", "mcp:search"): {"kind": "mcp", "id": await by_api.fetchval(
                                   "SELECT id FROM library_items WHERE kind = 'mcp' AND name = 'search'")},
                               ("channels", "telegram"): {"kind": "channel", "id": telegram}}

    asyncio.run(_test())


@needs_db
def test_an_import_not_applied_expires_or_is_cancelled_and_its_file_goes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from tests.test_config_transfer_db import _installation, _rows

    async def _test() -> None:
        small = tmp_path / "small.json"
        small.write_text(json.dumps({"format": "loopx-config/v1", "settings": {"layers": [
            {"scope": "system", "harness_prompt": "Be brief.", "agent": None, "effort": None, "env": [],
             "run_budget_usd": None}]}}))
        runtime_root = tmp_path / "runtime"
        monkeypatch.setattr(config_imports, "TTL_SECONDS", 0.3)
        monkeypatch.setattr(config_imports, "PENDING_MAX", 2)
        async with _installation() as (target, _), _serving(monkeypatch, target, runtime_root) as port:
            before = await _rows(target)
            expiring = (await _request(port, "POST", CONFIG_IMPORTS_PATH, small))[1]["import_id"]
            cancelled = (await _request(port, "POST", CONFIG_IMPORTS_PATH, small))[1]["import_id"]
            status, answer = await _request(port, "POST", CONFIG_IMPORTS_PATH, small)
            assert status == 409 and "2 imports are pending" in answer["error"]
            assert len(_left(runtime_root)) == 2

            assert (await _request(port, "DELETE", f"{CONFIG_IMPORTS_PATH}/{cancelled}"))[0] == 200
            assert _left(runtime_root) == [f"{expiring}.json"]
            await asyncio.sleep(0.6)
            assert _left(runtime_root) == []  # removed when it expired, with no request asking
            for import_id in (expiring, cancelled):
                assert (await _request(port, "POST", f"{CONFIG_IMPORTS_PATH}/{import_id}/apply"))[0] == 404
            assert await _rows(target) == before

    asyncio.run(_test())
