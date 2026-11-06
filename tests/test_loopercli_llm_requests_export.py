"""Recorded requests export through a served API, without loading the whole export into memory."""

import io
import json
import stat
import sys
from datetime import datetime

import pytest

from looper import loopercli
from tests.test_loopercli import Server, _send


@pytest.fixture
def export_cli(tmp_path):
    token = tmp_path / "operator-token"
    token.write_text("operator-test-secret\n")
    token.chmod(0o600)
    pages = [
        {"requests": [{"id": 1, "request_body": {"messages": [{"role": "user", "content": "first"}]},
                       "response_body": {"content": [{"type": "text", "text": "reply"}]},
                       "response_raw": None, "request_headers": {"authorization": "<redacted>"},
                       "record_gap": None, "outcome": "completed", "new_from_index": None}], "next_after_id": 1},
        {"requests": [{"id": 2, "request_body": {"messages": [{"role": "user", "content": "first"},
                                                                    {"role": "user", "content": "next"}]},
                       "response_body": None, "response_raw": "partial stream", "record_gap": "unparsable_stream",
                       "outcome": "client_closed", "new_from_index": 1}], "next_after_id": None},
    ]
    mode = {}

    def respond(handler, request):
        assert request[1] == "/api/v1/llm_requests/export"
        assert request[2]["authorization"] == "Bearer operator-test-secret"
        if mode.get("fail"):
            _send(handler, 403, {"status": "error", "error": {"code": "forbidden", "message": "operator only"}})
        else:
            page = pages[0 if "after_id" not in request[3] else 1]
            _send(handler, 200, {"status": "ok", "result": page})

    server = Server(respond)
    try:
        yield server, ["llm-requests", "export", "--url", server.url,
                       "--operator-token-file", str(token)], pages, mode
    finally:
        server.close()


@pytest.mark.parametrize("stdout", [False, True])
def test_export_pages_complete_jsonl_and_private_file(export_cli, tmp_path, capsys, stdout):
    server, base, pages, _ = export_cli
    destination = tmp_path / "requests.jsonl"
    assert loopercli.main([*base, "--record", "7", "--out", "-" if stdout else str(destination)]) == 0
    captured = capsys.readouterr()
    raw = captured.out if stdout else destination.read_text()
    lines = [json.loads(line) for line in raw.splitlines()]
    header, requests = lines[0], lines[1:]
    assert header == {
        "type": "llmrec_export", "format": 1, "exported_at": header["exported_at"], "filter": {"record_id": 7},
        "redaction": "header values outside the allowlist are never stored; "
                     "bodies are complete unless record_gap says otherwise",
    }
    assert datetime.fromisoformat(header["exported_at"]).utcoffset().total_seconds() == 0
    assert requests == [*pages[0]["requests"], *pages[1]["requests"]]
    assert [call[3] for call in server.requests] == [
        {"record_id": 7, "limit": 50}, {"record_id": 7, "limit": 50, "after_id": 1}]
    assert captured.err == "Exported 2 model requests\n"
    if not stdout:
        assert captured.out == ""
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize("flags,filters", [
    (["--task", "task-a"], {"task_id": "task-a"}),
    (["--agent", "agent-a"], {"agent_id": "agent-a"}),
    (["--since", "2026-01-01T00:00:00Z", "--until", "2026-02-01T00:00:00Z"],
     {"since": "2026-01-01T00:00:00Z", "until": "2026-02-01T00:00:00Z"}),
])
def test_export_filter_flags(export_cli, capsys, flags, filters):
    server, base, _, _ = export_cli
    assert loopercli.main([*base, *flags, "--out", "-"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["filter"] == filters
    assert server.requests[0][3] == {**filters, "limit": 50}


@pytest.mark.parametrize("token", ["", "lpc_client-token"])
def test_export_requires_operator_token(export_cli, monkeypatch, capsys, token):
    server, base, _, _ = export_cli
    monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    assert loopercli.main([*base[:-2], "--record", "7", "--out", "-"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "operator token is required" in captured.err
    assert not server.requests


@pytest.mark.parametrize("flags", [[], ["--until", "2026-02-01T00:00:00Z"],
                                   ["--task", "a", "--agent", "b"]])
def test_export_refuses_missing_or_conflicting_scope(export_cli, capsys, flags):
    server, base, _, _ = export_cli
    assert loopercli.main([*base, *flags, "--out", "-"]) == 2
    assert capsys.readouterr().out == ""
    assert not server.requests


def test_failed_export_removes_partial_file_and_preserves_existing_file(export_cli, tmp_path, capsys):
    server, base, _, mode = export_cli
    mode["fail"] = True
    destination = tmp_path / "requests.jsonl"
    assert loopercli.main([*base, "--record", "7", "--out", str(destination)]) == 1
    captured = capsys.readouterr()
    assert "forbidden" in captured.err and captured.out == ""
    assert not destination.exists()
    destination.write_text("keep")
    assert loopercli.main([*base, "--record", "7", "--out", str(destination)]) == 2
    assert destination.read_text() == "keep"
    assert len(server.requests) == 1
