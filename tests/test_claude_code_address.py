"""Retrieval clients use the listening server, including CLI-only address overrides."""

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit
from urllib.request import OpenerDirector

import pytest

from looper import ops_config
from looper.agents import api_v1, claude_code_entry, claude_code_mcp, operator_auth, server_address
from looper.agents.http_server import AgentHTTPServer
from tests.db_support import DSN, fresh_database_without_recording


@pytest.mark.parametrize("client", ["mcp", "status"])
@pytest.mark.parametrize("ops_port", [None, 18768])
@pytest.mark.parametrize("deleted_while_locked", [False, True])
def test_clients_refuse_missing_address_without_connecting(
        tmp_path, monkeypatch, client, ops_port, deleted_while_locked):
    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)
    ops_config.write_private(tmp_path / ops_config.FILE,
                             "[server]\n" + (f"port = {ops_port}\n" if ops_port else "# port omitted\n"))
    operator_auth.ensure_token(tmp_path)
    publication = None
    if deleted_while_locked:
        publication = server_address.Publication(tmp_path, "127.0.0.1", 18769)
        assert server_address.read(tmp_path) == ("127.0.0.1", 18769)
        (tmp_path / server_address.FILE).unlink()
        with pytest.raises(ValueError, match="already owns"):
            server_address.Publication(tmp_path, "127.0.0.1", 18770)
    connect = Mock(side_effect=AssertionError("unexpected socket connection"))
    http = Mock(side_effect=AssertionError("unexpected HTTP request"))
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(OpenerDirector, "open", http)
    output = io.StringIO()
    try:
        if client == "mcp":
            messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "vault_search", "arguments": {"query": "Atlas"}}}]
            wire = io.BytesIO()
            monkeypatch.setattr(claude_code_mcp.sys, "stdin", SimpleNamespace(
                buffer=io.BytesIO("".join(json.dumps(item) + "\n" for item in messages).encode())))
            monkeypatch.setattr(claude_code_mcp.sys, "stdout", SimpleNamespace(buffer=wire))
            assert claude_code_mcp.main(str(tmp_path), None) == 0
            replies = [json.loads(line)["result"] for line in wire.getvalue().splitlines()]
            assert replies[0]["serverInfo"]["name"] == "looper-memory"
            assert len(replies[1]["tools"]) == 3
            assert replies[2]["isError"]
            error = replies[2]["content"][0]["text"]
        else:
            monkeypatch.setattr(claude_code_entry, "settings_path", lambda: tmp_path / "settings.json")
            monkeypatch.setattr(claude_code_entry, "fetch_profile",
                                lambda *_: claude_code_entry.Fetched(claude_code_entry.OK, text="Name: Ada"))
            monkeypatch.setattr(claude_code_entry, "_claude", lambda _: "claude")
            monkeypatch.setattr(claude_code_entry, "_claude_mcp", lambda *_: SimpleNamespace(returncode=0))
            args = SimpleNamespace(runtime_root=str(tmp_path), config=None)
            assert claude_code_entry.status_command(args, out=output) == claude_code_entry.EMPTY
            error = output.getvalue()
        assert "Looper server for this runtime is not running or did not record its address" in error
        assert "looper serve" in error
    finally:
        if publication is not None:
            publication.close()
        http.assert_not_called()
        connect.assert_not_called()


@pytest.mark.parametrize("client", ["mcp", "status"])
@pytest.mark.parametrize("ops_port", [None, 18768])
def test_clients_follow_bound_address_and_refuse_stopped_server(tmp_path, monkeypatch, client, ops_port):
    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)
    ops_config.write_private(tmp_path / ops_config.FILE,
                             "[server]\n" + (f"port = {ops_port}\n" if ops_port else "# port omitted\n"))
    token = operator_auth.ensure_token(tmp_path)
    paths = []
    listening = []

    async def domain(operation, *_args, **_kwargs):
        paths.append(operation.operation_id)
        result = ({"configured": True, "manager_read": True} if operation.operation_id == "knowledge_vault.get"
                  else {"enabled": False} if operation.operation_id == "leads.settings_get" else {"matches": 7})
        return api_v1.OK, {"status": "ok", "result": result}

    original_opener = claude_code_entry.build_opener

    def guarded_opener(*args):
        opener = original_opener(*args)
        original_open = opener.open

        def open_request(request, *args, **kwargs):
            assert urlsplit(request.full_url).port != 8767
            assert urlsplit(request.full_url).port == listening[0]
            return original_open(request, *args, **kwargs)

        opener.open = open_request
        return opener

    monkeypatch.setattr(claude_code_entry, "build_opener", guarded_opener)
    # The MCP's transport also checks its destination before sending credentials.
    monkeypatch.setattr(claude_code_mcp, "build_opener", guarded_opener)

    def mcp_serve(client, *_args, **_kwargs):
        assert urlsplit(client.url).port != 8767
        assert client.call_tool("vault_search", {"query": "Atlas"}) == {"matches": 7}
        return 0

    monkeypatch.setattr(claude_code_mcp.mcp_server, "serve", mcp_serve)
    monkeypatch.setattr(claude_code_mcp.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(claude_code_mcp.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))

    def call():
        if client == "mcp":
            return claude_code_mcp.main(str(tmp_path), None)
        return claude_code_entry._retrieval_settings(SimpleNamespace(runtime_root=str(tmp_path), config=None))

    async def run():
        server = AgentHTTPServer(host="127.0.0.1", port=0, runtime_root=tmp_path, operator_token=token,
                                 web_adapter=SimpleNamespace(), task_router=SimpleNamespace(),
                                 supervisor=SimpleNamespace())
        monkeypatch.setattr(server, "_v1_domain", domain)
        await server.start()
        try:
            assert server._port != 8767
            listening.append(server._port)
            # This in-process regression deliberately omits/conflicts the ops port. Both transports
            # assert the destination before any request; the real serve smoke below pins its ops file.
            result = await asyncio.to_thread(call)
            assert result == (0 if client == "mcp" else ({"configured": True, "manager_read": True},
                                                        {"enabled": False}))
            assert paths == (["manager_retrieval.vault_search"] if client == "mcp" else
                             ["knowledge_vault.get", "leads.settings_get"])
        finally:
            await server.stop()
        with pytest.raises((ValueError, claude_code_mcp.LooperUnavailable), match="stale.*address|address.*stale"):
            await asyncio.to_thread(call)

    asyncio.run(run())


def test_crash_leaves_a_stale_record_and_restart_replaces_it(tmp_path):
    from looper.agents import server_address

    script = ("import os, sys\nfrom pathlib import Path\n"
              "from looper.agents.server_address import Publication\n"
              "publication = Publication(Path(sys.argv[1]), '127.0.0.1', 18768)\n"
              "os._exit(0)\n")
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    with pytest.raises(ValueError, match="stale"):
        server_address.read(tmp_path)
    publication = server_address.Publication(tmp_path, "127.0.0.1", 18769)
    try:
        assert server_address.read(tmp_path) == ("127.0.0.1", 18769)
        with pytest.raises(ValueError, match="already owns"):
            server_address.Publication(tmp_path, "127.0.0.1", 18770)
        assert server_address.read(tmp_path) == ("127.0.0.1", 18769)
    finally:
        publication.close()
    with pytest.raises(ValueError, match="stale"):
        server_address.read(tmp_path)


@pytest.mark.parametrize("shutdown", ["stop", "close_listener"])
@pytest.mark.parametrize("same_server", [False, True])
def test_http_address_ownership_follows_listener_lifecycle(tmp_path, shutdown, same_server):
    async def run():
        other_root = tmp_path / "other"
        other_root.mkdir()

        def server(root):
            return AgentHTTPServer(host="127.0.0.1", port=0, runtime_root=root,
                                   web_adapter=SimpleNamespace(), task_router=SimpleNamespace(),
                                   supervisor=SimpleNamespace())

        original, independent, replacement = server(tmp_path), server(other_root), server(tmp_path)
        try:
            await original.start()
            await independent.start()
            assert server_address.read(tmp_path) == ("127.0.0.1", original._port)
            assert server_address.read(other_root) == ("127.0.0.1", independent._port)
            with pytest.raises(ValueError, match="already owns"):
                await replacement.start()
            assert replacement._server is None
            assert original._server.is_serving()
            assert server_address.read(tmp_path) == ("127.0.0.1", original._port)
            if shutdown == "stop":
                await original.stop()
            else:
                original.close_listener()
            with pytest.raises(ValueError, match="stale"):
                server_address.read(tmp_path)
            if same_server:
                replacement = original
            await replacement.start()
            address = ("127.0.0.1", replacement._port)
            assert server_address.read(tmp_path) == address
            # Late cleanup by the old server must not revoke the new publication.
            if not same_server:
                await original.stop()
            assert server_address.read(tmp_path) == address
        finally:
            await original.stop()
            await independent.stop()
            await replacement.stop()
        with pytest.raises(ValueError, match="stale"):
            server_address.read(tmp_path)

    asyncio.run(run())


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_serve_mcp_and_status_share_the_isolated_address(tmp_path, monkeypatch):
    from evals.prompt_regression import runner
    from fake_claude import fake_claude
    from manager_mcp_stand_in import _BODY

    runtime, bin_dir, vault = (tmp_path / name for name in ("runtime", "bin", "vault"))
    bin_dir.mkdir()
    vault.mkdir()
    (vault / "note.md").write_text("Atlas uses the isolated server.\n")
    discovery = "import sys\nif '--mcp-config' not in sys.argv:\n    print('2.1.0')\n    sys.exit(0)\n"
    fake_claude(bin_dir / "claude", discovery + "TURNS = []\n" + _BODY)
    env = {key: value for key, value in os.environ.items()
           if key not in (*ops_config.VARIABLES, "LOOPER_AGENT_RUN")}
    env.update(HOME=str(tmp_path), CLAUDE_CONFIG_DIR=str(tmp_path / "cc"))
    port = runner.free_port()
    assert port != 8767
    render = ops_config.render
    # The harness writes this file before its first health request.
    monkeypatch.setattr(ops_config, "render", lambda ops: render(replace(ops, port=port)))
    with fresh_database_without_recording(DSN) as (database_url, _):
        with runner.running_server(database_url=database_url, runtime_root=runtime, bin_dir=bin_dir,
                                   log_path=tmp_path / "serve.log", home=tmp_path / "server-home", port=port) as api:
            assert urlsplit(api.base_url).port == port != 8767
            ops = ops_config.load(ops_config.Flags(runtime_root=runtime), require_database=False)
            assert ops.port == port
            assert urlsplit(claude_code_entry._retrieval_url(ops)).port == port != 8767
            assert api.post("/api/v1/knowledge_vault/update", {"path": str(vault), "manager_read": True})["status"] == "ok"
            message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "vault_search", "arguments": {"query": "Atlas"}}}
            result = subprocess.run([sys.executable, "-m", "looper.cli", "claude-code", "mcp",
                                     "--runtime-root", str(runtime)], input=json.dumps(message) + "\n",
                                    capture_output=True, text=True, env=env, timeout=15)
            assert result.returncode == 0, result.stderr
            tool = json.loads(result.stdout)["result"]
            assert not tool["isError"]
            assert json.loads(tool["content"][0]["text"])["results"][0]["path"] == "note.md"
            assert urlsplit(claude_code_entry._retrieval_url(ops)).port == port != 8767
            status = subprocess.run([sys.executable, "-m", "looper.cli", "claude-code", "status",
                                     "--runtime-root", str(runtime), "--claude", str(bin_dir / "claude")],
                                    capture_output=True, text=True, env=env, timeout=15)
            assert status.returncode == 0, status.stdout + status.stderr
            assert "knowledge vault for the Manager: readable" in status.stdout
        with pytest.raises(ValueError, match="stale"):
            claude_code_entry._retrieval_url(ops)
        assert ops.port == port != 8767
        messages = [{"jsonrpc": "2.0", "id": 2, "method": "initialize"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, message]
        stopped = subprocess.run([sys.executable, "-m", "looper.cli", "claude-code", "mcp",
                                  "--runtime-root", str(runtime)],
                                 input="".join(json.dumps(item) + "\n" for item in messages),
                                 capture_output=True, text=True, env=env, timeout=15)
        assert stopped.returncode == 0, stopped.stderr
        responses = [json.loads(line)["result"] for line in stopped.stdout.splitlines()]
        assert responses[0]["serverInfo"]["name"] == "looper-memory"
        assert len(responses[1]["tools"]) == 3
        assert responses[2]["isError"] and "stale" in responses[2]["content"][0]["text"]
        assert ops.port != 8767
        status = subprocess.run([sys.executable, "-m", "looper.cli", "claude-code", "status",
                                 "--runtime-root", str(runtime), "--claude", str(bin_dir / "claude")],
                                capture_output=True, text=True, env=env, timeout=15)
        assert status.returncode == 1 and "stale" in status.stdout
