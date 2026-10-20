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

from looper import __version__
from looper.agents import config_imports
from looper.agents.http_server import CONFIG_IMPORTS_PATH, REQUEST_BODY_MAX_BYTES, AgentHTTPServer
from tests.test_catalog_db import DSN

needs_db = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
TOKEN = "operator-test-token"
UNKNOWN = "cfgimp-0123456789abcdef"


@pytest.fixture(autouse=True)
def _isolated_registry_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import profile_registry

    monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})


@contextlib.asynccontextmanager
async def _server(runtime_root: Path) -> AsyncIterator[int]:
    server = AgentHTTPServer(port=0, operator_token=TOKEN, web_adapter=SimpleNamespace(), task_router=SimpleNamespace(),
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
                 f"Authorization: Bearer {TOKEN}\r\n"
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
    (b"PK\x03\x04 a zip", "the uploaded file is not a Looper configuration file"),
    (b"\xff\xfe{}", "the uploaded file is not a Looper configuration file"),
    (b"[]", "is not a looper-config/v3, looper-config/v2 or looper-config/v1 file (format: None)"),
    (b'{"format": "looper-config/v99"}',
     "is not a looper-config/v3, looper-config/v2 or looper-config/v1 file (format: 'looper-config/v99')"),
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
    from tests.test_config_transfer_db import _installation, _item, _looper, _populate

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
        code, _, err = await _looper(monkeypatch, source_url, "config", "export", str(exported), "--include-projects")
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
            assert answer["file"] == {"format": "looper-config/v3", "looper_version": __version__,
                                      "exported_at": answer["file"]["exported_at"], "secrets": "omitted"}
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
    from tests.test_config_transfer_db import _installation, _looper, _plan, _state

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

            code, out, err = await _looper(monkeypatch, cli_url, "config", "import", str(exported), "--apply", "--json")
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
def test_a_file_exported_before_the_rename_is_previewed_with_its_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from tests.test_config_transfer_db import _installation

    async def _test() -> None:
        old = tmp_path / "old.json"
        old.write_text(json.dumps({"format": "loopx-config/v1", "loopx_version": "2.5.0", "secrets": "omitted",
                                   "settings": {"layers": [
                                       {"scope": "system", "harness_prompt": "Be brief.", "agent": None,
                                        "effort": None, "env": [], "run_budget_usd": None}]}}))
        async with _installation() as (target, _), _serving(monkeypatch, target, tmp_path / "runtime") as port:
            status, answer = await _request(port, "POST", CONFIG_IMPORTS_PATH, old)

        assert status == 201, answer
        assert answer["file"] == {"format": "loopx-config/v1", "looper_version": "2.5.0", "secrets": "omitted"}
        assert [(c["section"], c["key"], c["action"]) for c in answer["plan"]["changes"]] == [
            ("settings.layers", "system", "added")]

    asyncio.run(_test())


@needs_db
def test_an_import_not_applied_expires_or_is_cancelled_and_its_file_goes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from tests.test_config_transfer_db import _installation, _rows

    async def _test() -> None:
        small = tmp_path / "small.json"
        small.write_text(json.dumps({"format": "looper-config/v3", "settings": {"layers": [
            {"scope": "system", "harness_prompt": "Be brief.", "profile": None, "env": [],
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


@needs_db
def test_v3_preview_merge_apply_and_readback_share_the_cli_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from copy import deepcopy

    from tests.test_config_transfer_db import _installation, _looper, _plan, _rows
    from tests.test_config_v3_import_db import v3

    async def _test() -> None:
        path = tmp_path / "v3.json"
        data = v3()
        path.write_text(json.dumps(data))
        async with _installation() as (target, url), _serving(monkeypatch, target, tmp_path / "runtime") as port:
            before = await _rows(target)
            status, uploaded = await _request(port, "POST", CONFIG_IMPORTS_PATH, path)
            assert status == 201, uploaded
            plan = uploaded["plan"]
            assert plan["counts"]["models"] == {"added": 1, "changed": 0, "unchanged": 0, "skipped": 0}
            code, out, err = await _looper(monkeypatch, url, "config", "import", str(path), "--json")
            assert code == 0, err
            assert _plan(out) == plan
            assert await _rows(target) == before
            status, applied = await _request(port, "POST", f"{CONFIG_IMPORTS_PATH}/{uploaded['import_id']}/apply")
            assert status == 200, applied
            need = applied["plan"]["need_secret"]
            assert need == [{"section": "providers", "key": "account", "names": ["api_key"],
                             "target": {"kind": "provider", "id": await target.fetchval(
                                 "SELECT id FROM llm_providers WHERE name = 'account'")}}]
            status, registry = await _request(port, "GET", "/api/registry")
            assert status == 200, registry
            model = next(m for m in registry["models"] if m["name"] == "exact")
            profile = next(p for p in registry["profiles"] if p["name"] == "imported")
            assert profile["model_id"] == model["model_id"]
            assert model["protocol"] == "anthropic-messages" and model["input_usd_per_mtok"] == 2
            status, saved = await _request(port, "PUT", f"/api/providers/{need[0]['target']['id']}",
                                           json.dumps({"api_key": "sk-test-import-key"}).encode())
            assert status == 200, saved
            status, exported = await _request(port, "GET", "/api/config/export")
            assert status == 200 and exported["format"] == "looper-config/v3"
            assert exported["providers"][0]["api_key"] == {"secret": "omitted"}
            cli_path = tmp_path / "cli.json"
            code, _, err = await _looper(monkeypatch, url, "config", "export", str(cli_path))
            assert code == 0, err
            cli = json.loads(cli_path.read_text())
            cli.pop("exported_at")
            exported.pop("exported_at")
            assert cli == exported

            # Merge one model in place, add another, and retain the existing profile
            # when an invalid reference tries to replace it.
            changed = deepcopy(data)
            changed["models"][0]["input_usd_per_mtok"] = 4
            changed["models"].append({**changed["models"][0], "name": "another"})
            changed["profiles"][0]["model"]["name"] = "missing"
            path.write_text(json.dumps(changed))
            before = await _rows(target)
            status, merged = await _request(port, "POST", CONFIG_IMPORTS_PATH, path)
            assert status == 201, merged
            assert merged["plan"]["counts"]["models"] == {
                "added": 1, "changed": 1, "unchanged": 0, "skipped": 0}
            assert merged["plan"]["counts"]["profiles"]["skipped"] == 1
            assert next(c for c in merged["plan"]["changes"] if c["section"] == "profiles")["reason"]
            assert await _rows(target) == before
            status, answer = await _request(port, "POST", f"{CONFIG_IMPORTS_PATH}/{merged['import_id']}/apply")
            assert status == 200, answer
            status, registry = await _request(port, "GET", "/api/registry")
            assert status == 200
            updated = next(m for m in registry["models"] if m["name"] == "exact")
            assert updated["model_id"] == model["model_id"] and updated["input_usd_per_mtok"] == 4
            retained = next(p for p in registry["profiles"] if p["name"] == "imported")
            for key in ("profile_id", "model_id", "runtime_id", "effort", "agent_teams", "custom_prompt"):
                assert retained[key] == profile[key]

            # A conflicting endpoint blocks its models and profiles without changing any row.
            changed = deepcopy(data)
            changed["providers"][0]["endpoints"]["anthropic-messages"]["base_url"] = "https://conflict.invalid"
            path.write_text(json.dumps(changed))
            before = await _rows(target)
            status, conflict = await _request(port, "POST", CONFIG_IMPORTS_PATH, path)
            assert status == 201, conflict
            assert all(c["action"] == "skipped" and c["reason"] for c in conflict["plan"]["changes"])
            status, answer = await _request(port, "POST", f"{CONFIG_IMPORTS_PATH}/{conflict['import_id']}/apply")
            assert status == 200, answer
            assert await _rows(target) == before

            # Apply recomputes against the current registry, even when another
            # writer has already made the previewed change.
            data["models"][0]["input_usd_per_mtok"] = 9
            path.write_text(json.dumps(data))
            status, pending = await _request(port, "POST", CONFIG_IMPORTS_PATH, path)
            assert status == 201, pending
            assert pending["plan"]["counts"]["models"]["changed"] == 1
            await target.execute("UPDATE llm_models SET input_usd_per_mtok = 9 WHERE id = $1", model["model_id"])
            before = await _rows(target)
            status, answer = await _request(port, "POST", f"{CONFIG_IMPORTS_PATH}/{pending['import_id']}/apply")
            assert status == 200, answer
            assert answer["plan"]["counts"]["models"] == {
                "added": 0, "changed": 0, "unchanged": 1, "skipped": 0}
            assert await _rows(target) == before

    asyncio.run(asyncio.wait_for(_test(), timeout=60))


def test_apply_refuses_client_supplied_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from unittest.mock import AsyncMock

    async def _test() -> None:
        from tests.test_catalog_db import _Writer

        apply = AsyncMock(return_value={})
        monkeypatch.setattr(config_imports.ConfigImports, "apply", apply)
        server = AgentHTTPServer(port=0, operator_token=TOKEN, web_adapter=SimpleNamespace(),
                                 task_router=SimpleNamespace(), supervisor=SimpleNamespace(), runtime_root=tmp_path)
        writer = _Writer()
        await server._route_api("POST", f"{CONFIG_IMPORTS_PATH}/{UNKNOWN}/apply", {},
                                b'{"models": {"untrusted": "model-id"}, "plan": {"applied": true}}', writer)
        head, _, payload = writer.data.partition(b"\r\n\r\n")
        assert int(head.split()[1]) == 400 and "body" in json.loads(payload)["error"]
        apply.assert_not_awaited()

    asyncio.run(_test())


def test_v3_upload_previews_models_and_retention_reasons_without_a_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from unittest.mock import AsyncMock

    from looper.agents import config_transfer
    from tests.test_config_v3_import_db import v3

    async def preview(data: dict[str, Any], *, apply: bool) -> config_transfer.Plan:
        assert not apply
        return await config_transfer.import_config(None, data, apply=False)

    async def _test() -> None:
        monkeypatch.setattr(config_imports, "_run", preview)
        monkeypatch.setattr(config_transfer._Target, "read", AsyncMock(return_value=config_transfer._Target()))
        imports = config_imports.ConfigImports(tmp_path)
        data = v3()
        data["models"].append({"provider": "missing-account", "name": "retained", "protocol": "openai-chat"})
        body = json.dumps(data).encode()
        reader = asyncio.StreamReader()
        reader.feed_data(body)
        reader.feed_eof()
        try:
            uploaded = await imports.receive(reader, len(body))
            assert uploaded["file"]["format"] == "looper-config/v3"
            assert uploaded["plan"]["counts"]["models"] == {
                "added": 1, "changed": 0, "unchanged": 0, "skipped": 1}
            skipped = next(c for c in uploaded["plan"]["changes"] if c["action"] == "skipped")
            assert skipped["section"] == "models" and "missing-account" in skipped["reason"]
            assert uploaded["plan"]["need_secret"] == [
                {"section": "providers", "key": "account", "names": ["api_key"]}]
            assert len(_left(tmp_path)) == 1
            assert await imports.cancel(uploaded["import_id"]) == {"deleted": uploaded["import_id"]}
            assert _left(tmp_path) == []
        finally:
            imports.clear()

    asyncio.run(_test())


@pytest.mark.parametrize("token", [None, "invalid-operator-token"])
def test_unauthorized_v3_upload_creates_no_pending_file(tmp_path: Path, token: str | None) -> None:
    from tests.test_llm_provider_api_db import Client

    async def _test() -> None:
        server = AgentHTTPServer(port=0, operator_token=TOKEN, web_adapter=SimpleNamespace(),
                                 task_router=SimpleNamespace(), supervisor=SimpleNamespace(), runtime_root=tmp_path)
        try:
            status, _ = await Client(server).call("POST", CONFIG_IMPORTS_PATH, {"format": "looper-config/v3"},
                                                  token=token)
            assert status == 401
            assert _left(tmp_path) == []
        finally:
            await server.stop()

    asyncio.run(_test())


def test_pre_profile_v1_layers_keep_their_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from looper.agents import config_transfer

    async def _test() -> None:
        monkeypatch.setattr(config_transfer._Target, "read", AsyncMock(return_value=config_transfer._Target()))
        data = {"format": "loopx-config/v1", "settings": {"layers": [
            {"scope": "system", "model": "haiku", "effort": "low", "env": []},
            {"scope": "role", "role": "manager", "model": "opus", "env": []}]}}
        plan = await config_transfer.import_config(None, data, apply=False)
        assert not [c for c in plan.changes if c.action == config_transfer.Action.SKIPPED]
        assert {c.key for c in plan.changes if c.section == "profiles"} == {
            "claude-code/haiku", "claude-code/haiku · low", "claude-code/opus · low"}

    asyncio.run(_test())


def test_legacy_preview_keeps_a_readable_reason_and_a_separate_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from looper.agents import config_transfer, runtimes
    from tests.test_config_v3_import_db import legacy

    async def _test() -> None:
        target = config_transfer._Target(runtimes={"codex": {
            "id": "rt-codex", "name": "codex", "type": "codex", "executable": "codex", "builtin": True,
            "capabilities": [c.value for c in runtimes.adapter("codex").capabilities]}})
        monkeypatch.setattr(config_transfer._Target, "read", AsyncMock(return_value=target))
        data = legacy()
        data["providers"][0].update(protocols=["openai-responses"])
        data["profiles"][0].update(runtime="codex")
        plan = await config_transfer.import_config(None, data, apply=False)
        change = next(c for c in plan.view()["changes"] if c["section"] == "profiles")
        assert change["action"] == "skipped"
        assert "agent teams are a Claude Code feature" in change["reason"]
        assert "turn agent_teams off" in change["reason"]
        assert change["code"] == "invalid_legacy_value"
        assert change["reason"] in config_transfer.format_plan(plan, Path("config.json"))

    asyncio.run(_test())
