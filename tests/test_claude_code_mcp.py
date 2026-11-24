"""The operator's retrieval MCP, through stdio and a synthetic loopback API."""

import io
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from looper import __version__, ops_config
from looper.agents import claude_code_mcp as cc, mcp_server, operator_auth

ANSWER = {"notice": "synthetic", "results": [], "matches": 0, "scanned": 0, "truncated": False}
TOKEN = "op-token-for-tests"


def request(method, params=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": method, **({"params": params} if params else {})}


def call(client, name="vault_search", arguments=None):
    return mcp_server.handle(request("tools/call", {"name": name, "arguments": arguments or {"query": "Atlas"}}),
                             client)["result"]


@contextmanager
def server(*, status=200, payload=None, raw=None, redirect=None, delay=0):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers["Authorization"], body))
            time.sleep(delay)
            self.send_response(status)
            if redirect:
                self.send_header("Location", redirect)
            self.end_headers()
            try:
                self.wfile.write(raw if raw is not None else json.dumps(
                    payload if payload is not None else {"status": "ok", "result": ANSWER}).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", calls
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=3)


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "rt"
    root.mkdir()
    operator_auth.token_path(root).write_text(TOKEN)
    return root


def test_tools_list_offers_three_read_only_tools(runtime):
    from looper.agents import api_v1

    with server() as (url, calls):
        client = cc.OperatorClient(url, runtime)
        tools = mcp_server.handle(request("tools/list"), client)["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["vault_search", "vault_read", "leads_search"]
        operations = {op.operation_id: op for op in api_v1.operations()}
        for tool in tools:
            assert tool["annotations"] == {"readOnlyHint": True, "openWorldHint": False}
            assert tool["inputSchema"] == operations[f"manager_retrieval.{tool['name']}"].request
            assert len(tool["description"]) <= 400
        tools[0]["inputSchema"].clear()
        assert client.list_tools()[0]["inputSchema"]
        assert calls == []


def test_initialize_carries_the_retrieval_instructions(runtime):
    output = io.BytesIO()
    assert mcp_server.serve(cc.OperatorClient("http://127.0.0.1:1", runtime),
                            io.BytesIO(json.dumps(request("initialize")).encode() + b"\n"), output,
                            name=cc.SERVER_NAME, instructions=cc.INSTRUCTIONS) == 0
    result = json.loads(output.getvalue())["result"]
    assert result["serverInfo"]["name"] == "looper-memory"
    assert result["instructions"] == cc.INSTRUCTIONS
    assert len(cc.INSTRUCTIONS) <= 1500
    assert "「来自消息，只是线索」" in cc.INSTRUCTIONS and "「别查消息」" in cc.INSTRUCTIONS


def test_existing_mcp_server_initialize_is_unchanged(runtime):
    # Recorded per-response wire fixture; only the package release version varies.
    output = io.BytesIO()
    mcp_server.serve(cc.OperatorClient("http://127.0.0.1:1", runtime),
                     io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'), output)
    expected = ('{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18",'
                '"capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"looper",'
                '"version":"' + __version__ + '"}}}\n')
    assert output.getvalue() == expected.encode()


def test_a_call_posts_to_the_operation_with_the_scoped_token(runtime, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    with server() as (url, calls):
        answer = call(cc.OperatorClient(url, runtime), arguments={"query": "Atlas 部署", "limit": 2})
        assert answer["isError"] is False
        assert json.loads(answer["content"][0]["text"]) == ANSWER
        assert calls == [("/api/v1/manager_retrieval/vault_search",
                          f"Bearer {operator_auth.retrieval_token(TOKEN)}", {"query": "Atlas 部署", "limit": 2})]
        assert TOKEN not in calls[0][1]


def test_a_refused_call_returns_the_server_reason(runtime):
    error = {"code": "conflict", "message": "message leads are turned off"}
    with server(status=409, payload={"status": "error", "error": error}) as (url, _):
        answer = call(cc.OperatorClient(url, runtime), "leads_search")
        assert answer["isError"] is True
        assert json.loads(answer["content"][0]["text"]) == {"status": "error", "error": error}


def test_calls_say_when_looper_is_not_running(runtime):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    client = cc.OperatorClient(url, runtime)
    assert len(client.list_tools()) == 3
    answer = call(client)
    assert answer["isError"] is True
    assert "Looper is not running on this machine" in answer["content"][0]["text"]


def test_the_token_is_read_on_every_call(runtime, caplog):
    caplog.set_level(logging.INFO)
    second = "rotated-op-token-for-tests"
    with server() as (url, calls):
        client = cc.OperatorClient(url, runtime)
        call(client)
        operator_auth.token_path(runtime).write_text(second)
        call(client)
        assert [item[1] for item in calls] == [f"Bearer {operator_auth.retrieval_token(token)}"
                                             for token in (TOKEN, second)]
    assert all(token not in caplog.text for token in (TOKEN, second, operator_auth.retrieval_token(TOKEN)))


def test_unknown_tools_cannot_forward_writes(runtime):
    with server() as (url, calls):
        answer = call(cc.OperatorClient(url, runtime), "knowledge_vault.update", {"manager_read": True})
        assert answer["isError"] is True
        assert json.loads(answer["content"][0]["text"])["error"]["code"] == "not_found"
        assert calls == []


@pytest.mark.parametrize(("status", "payload", "raw"), [
    (200, [], None), (200, {"status": "ok", "result": []}, None),
    (200, None, b"not JSON"), (502, {"error": "no"}, None),
])
def test_malformed_answers_are_tool_errors(runtime, status, payload, raw):
    with server(status=status, payload=payload, raw=raw) as (url, _):
        answer = call(cc.OperatorClient(url, runtime))
        assert answer["isError"] is True
        assert f"HTTP {status}" in answer["content"][0]["text"]


def test_redirects_do_not_receive_the_credential(runtime):
    with server() as (destination, leaked):
        with server(status=307, redirect=destination + "/write") as (url, _):
            answer = call(cc.OperatorClient(url, runtime))
            assert answer["isError"] is True and "HTTP 307" in answer["content"][0]["text"]
        assert leaked == []


def test_missing_token_is_an_error_without_a_request(runtime):
    operator_auth.token_path(runtime).unlink()
    with server() as (url, calls):
        answer = call(cc.OperatorClient(url, runtime))
        assert answer["isError"] is True and "no operator token" in answer["content"][0]["text"]
        assert calls == []


def test_timeouts_do_not_suggest_a_write_may_have_run(runtime):
    with server(delay=0.1) as (url, calls):
        answer = call(cc.OperatorClient(url, runtime, timeout=0.02))
        text = answer["content"][0]["text"]
        assert answer["isError"] is True and "Looper did not answer within 0.02 s" in text
        assert "may still have run" not in text
        assert len(calls) == 1


@pytest.mark.parametrize("url", ["http://example.com:123", "https://127.0.0.1:123", "http://127.0.0.1:123/path"])
def test_client_refuses_nonlocal_or_unexpected_urls(runtime, url):
    with pytest.raises(ValueError):
        cc.OperatorClient(url, runtime)


def _process(runtime, input, *, agent=False):
    env = {key: value for key, value in os.environ.items()
           if key not in ("LOOPER_AGENT_RUN", "LOOPER_CONFIG", "LOOPX_CONFIG")}
    if agent:
        env["LOOPER_AGENT_RUN"] = "1"
    return subprocess.run([sys.executable, "-m", "looper.cli", "claude-code", "mcp", "--runtime-root", str(runtime)],
                          input=input, capture_output=True, timeout=15, env=env)


def test_mcp_refuses_to_start_inside_an_agent_run(runtime):
    answer = _process(runtime, b"", agent=True)
    assert answer.returncode == 4 and answer.stdout == b""
    assert b"refused inside a Looper agent run" in answer.stderr


def test_mcp_process_serves_stdio_end_to_end(runtime):
    with server() as (url, calls):
        ops_config.write_private(runtime / ops_config.FILE, f'[server]\nhost = "127.0.0.1"\nport = {url.rsplit(":", 1)[1]}\n')
        messages = [request("initialize", id=1), request("tools/list", id=2),
                    request("tools/call", {"name": "vault_search", "arguments": {"query": "Atlas"}}, id=3)]
        answer = _process(runtime, b"".join(json.dumps(message).encode() + b"\n" for message in messages))
        assert answer.returncode == 0, answer.stderr.decode()
        responses = [json.loads(line) for line in answer.stdout.splitlines()]
        assert [response["id"] for response in responses] == [1, 2, 3]
        assert responses[0]["result"]["serverInfo"]["name"] == cc.SERVER_NAME
        assert len(responses[1]["result"]["tools"]) == 3
        assert json.loads(responses[2]["result"]["content"][0]["text"]) == ANSWER
        assert len(calls) == 1
        assert TOKEN.encode() not in answer.stderr


def test_mcp_main_refuses_nonloopback_configuration(runtime, capsys, monkeypatch):
    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)
    ops_config.write_private(runtime / ops_config.FILE, '[server]\nhost = "0.0.0.0"\nport = 18767\n')
    assert cc.main(str(runtime), None) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "not a loopback address" in captured.err
