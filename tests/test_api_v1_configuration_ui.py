"""Configuration transfer uses JSON v1 envelopes without buffering its upload or export."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1, config_imports, config_transfer
from looper.agents.ops.clients import ClientOps
from looper.db import connection
from tests.test_api_v1_auth import CLIENT_TOKEN, _Writer, _bearer, _send, _send_raw, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN


@pytest.mark.parametrize("failure", [None, "before", "after"])
def test_export_json_envelope_and_stream_failure(tmp_path, monkeypatch, caplog, failure):
    @asynccontextmanager
    async def context(**kwargs):
        yield SimpleNamespace(transaction=context)

    monkeypatch.setattr(connection, "get_pool", lambda: SimpleNamespace(acquire=context))
    async def chunks(conn, **kwargs):
        if failure == "before":
            raise RuntimeError("snapshot failed")
        yield '{"format":"looper-config/v4","catalog":['
        if failure == "after":
            raise RuntimeError("catalog failed")
        yield '{"name":"portable","files":[]}]}'
    monkeypatch.setattr(config_transfer, "export_chunks", chunks)

    async def run():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, headers, raw = await _send_raw(server, "POST /api/v1/config/export", _bearer(TOKEN), "{}")
            assert headers["content-type"].startswith("application/json")
            if failure == "after":
                assert status == 200 and raw.count("HTTP/1.1") == 0
                with pytest.raises(ValueError):
                    json.loads(raw)
            elif failure == "before":
                assert status == 503 and json.loads(raw)["error"]["code"] == "unavailable"
            else:
                assert status == 200
                document = json.loads(raw)["result"]["document"]
                assert document["catalog"][0]["name"] == "portable"
                api_v1.validate_arguments(api_v1.find("POST", "/api/v1/config/export").result,
                                          {"document": document})
            if failure is None:
                assert (await _send(server, "GET /api/config/export", _bearer(TOKEN)))[0] == 404
            assert ("status=ok" if failure is None else "status=error:unavailable") in caplog.text
    caplog.set_level("INFO")
    asyncio.run(run())


@pytest.mark.parametrize(("body", "headers"), [
    ('{"format":"looper-config/v4"}', {"Content-Type": "text/plain"}),
    ('{"format":"looper-config/v4","runtime_root":"somewhere"}', {}),
    ('{"format":"looper-config/v4"}', {"Content-Length": "1000"}),
])
def test_upload_refusals_are_typed_and_remove_private_files(tmp_path, body, headers):
    async def run():
        async with _serving(tmp_path) as server:
            if "Content-Length" in headers:
                reader = asyncio.StreamReader()
                reader.feed_data((f"POST /api/v1/config/import_upload HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                  f"Authorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\n"
                                  f"Content-Length: 1000\r\n\r\n{body}").encode())
                reader.feed_eof()
                writer = _Writer("127.0.0.1")
                await server._handle_connection(reader, writer)
                head, _, payload = writer.data.partition(b"\r\n\r\n")
                status, answer = int(head.split()[1]), json.loads(payload)
            else:
                status, _, answer = await _send(server, "POST /api/v1/config/import_upload",
                                               {**_bearer(TOKEN), **headers}, body)
            assert status == 400 and answer["error"]["code"] == "invalid"
            assert not list((tmp_path / config_imports.DIRNAME).glob("*.json"))
    asyncio.run(run())


@pytest.mark.parametrize(("action", "flags", "body"), [
    ("import-upload", ["--format", "looper-config/v4", "--catalog", '[{"name":"portable"}]'],
     {"format": "looper-config/v4", "catalog": [{"name": "portable"}]}),
    ("import-cancel", ["--import-id", "cfgimp-example"], {"import_id": "cfgimp-example"}),
    ("import-apply", ["--import-id", "cfgimp-example"], {"import_id": "cfgimp-example"}),
    ("import-preview", ["--document", '{"format":"looper-config/v4"}'],
     {"document": {"format": "looper-config/v4"}}),
])
def test_generated_configuration_commands_use_raw_json(action, flags, body):
    document = api_v1.document()
    args = loopercli.build_parser(document).parse_args(["config", action, *flags])
    operation = args._generated
    assert operation.path == "/api/v1/config/" + action.replace("-", "_")
    assert loopercli.request_body(args, operation) == body
    declared = api_v1.find("POST", operation.path)
    assert not declared.hand and not declared.stream and declared.tool is None
    assert declared.request["additionalProperties"] is False


@pytest.mark.usefixtures("clients")
def test_cli_cancels_its_uploaded_import_over_http(tmp_path, monkeypatch, capsys):
    async def plan(data, *, apply):
        return config_transfer.Plan(applied=apply)
    monkeypatch.setattr(config_imports, "_run", plan)

    async def run():
        async with _serving(tmp_path, listen=True) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            client = tmp_path / "client.json"
            loopercli.write_client(client, f"http://127.0.0.1:{server._port}", CLIENT_TOKEN)
            argv = ["--client-file", str(client), "--json", "config"]
            try:
                assert await asyncio.to_thread(loopercli.main, [*argv, "import-upload", "--format",
                                                                "looper-config/v4"]) == 0
                import_id = json.loads(capsys.readouterr().out)["result"]["import_id"]
                staged = tmp_path / config_imports.DIRNAME / f"{import_id}.json"
                assert json.loads(staged.read_text()) == {"format": "looper-config/v4"}
                cancel = [*argv, "import-cancel", "--import-id", import_id]
                assert await asyncio.to_thread(loopercli.main, cancel) == 0
                assert json.loads(capsys.readouterr().out)["result"] == {"deleted": import_id}
                assert not staged.exists()
                assert await asyncio.to_thread(loopercli.main, cancel) == 1
                captured = capsys.readouterr()
                assert json.loads(captured.out)["error"]["code"] == "not_found"
                assert CLIENT_TOKEN not in captured.out + captured.err
            finally:
                server._config_imports.clear()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["upload", "apply", "cancel"])
def test_removed_import_routes_have_successful_v1_controls(tmp_path, monkeypatch, action):
    async def plan(data, *, apply):
        return config_transfer.Plan(applied=apply)
    monkeypatch.setattr(config_imports, "_run", plan)
    async def run():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, staged = await _send(server, "POST /api/v1/config/import_upload", _bearer(TOKEN),
                                              '{"format":"looper-config/v4"}')
            assert status == 200
            import_id = staged["result"]["import_id"]
            if action == "upload":
                method, old, body = "POST", "/api/config/imports", '{"format":"looper-config/v4"}'
            else:
                status, _, result = await _send(server, f"POST /api/v1/config/import_{action}", _bearer(TOKEN),
                                                json.dumps({"import_id": import_id}))
                assert status == 200 and result["status"] == "ok"
                method = "POST" if action == "apply" else "DELETE"
                old = f"/api/config/imports/{import_id}" + ("/apply" if action == "apply" else "")
                body = ""
            assert (await _send(server, f"{method} {old}", _bearer(TOKEN), body))[0] == 404
            server._config_imports.clear()
    asyncio.run(run())
