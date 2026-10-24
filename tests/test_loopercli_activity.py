"""Generated task activity commands use the advertised JSON or SSE transport."""

import json
import threading

import pytest

from looper import loopercli
from looper.agents import api_v1
from tests.test_loopercli import Server, _send, write_client_file


@pytest.fixture
def activity_cli(tmp_path):
    mode, closed = {}, threading.Event()
    event = {"seq": 7, "task_id": "demo", "worker": None, "kind": "worker_output", "summary": "Progress",
             "metadata": None, "input_tokens": None, "output_tokens": None, "created_at": "2026-01-01T00:00:00Z"}
    def respond(handler, request):
        if request[1].endswith("openapi.json"):
            _send(handler, 200, api_v1.document())
        elif not request[1].endswith("activity_stream"):
            _send(handler, 200, {"status": "ok", "result": {"observed": request[3]}})
        elif mode.get("status") == 302:
            _send(handler, 302, {}, {"Location": server.url + "/redirect-target"})
        elif mode.get("status") == 503:
            _send(handler, 503, {"status": "error", "error": {"code": "unavailable", "message": "read failed"}})
        else:
            raw = (": heartbeat\n\nid: 7\nevent: activity\ndata: " + json.dumps(event)
                   + "\n\nevent: done\ndata: {}\n\n").encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
            handler.wfile.flush()
            handler.connection.settimeout(2)
            if handler.rfile.read(1) == b"":
                closed.set()
    server = Server(respond)
    client = write_client_file(tmp_path / "client.json", server.url)
    try:
        yield server, ["--client-file", str(client)], mode, closed, event
    finally:
        server.close()


@pytest.mark.parametrize("command,flags,body", [
    ("activity", ["--after", "3", "--limit", "2"], {"task_id": "demo", "after": 3, "limit": 2}),
    ("activity-detail", ["--seq", "7"], {"task_id": "demo", "seq": 7}),
    ("conversation", ["--before", "7", "--limit", "2"], {"task_id": "demo", "before": 7, "limit": 2}),
])
def test_generated_activity_json_commands(activity_cli, command, flags, body, capsys):
    server, base, *_ = activity_cli
    assert loopercli.main([*base, "--json", "tasks", command, "--task-id", "demo", *flags]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["observed"] == body
    assert server.requests[-1][0:4:3] == ("POST", body)
    assert server.requests[-1][1] == "/api/v1/tasks/" + command.replace("-", "_")


@pytest.mark.parametrize("as_json", [False, True])
def test_generated_activity_stream_output(activity_cli, capsys, as_json):
    server, base, _, closed, event = activity_cli
    assert loopercli.main([*base, *(["--json"] if as_json else []), "tasks", "activity-stream",
                           "--task-id", "demo", "--since", "6"]) == 0
    lines = capsys.readouterr().out.splitlines()
    if as_json:
        assert [json.loads(line) for line in lines] == [
            {"event": "activity", "data": event}, {"event": "done", "data": {}}]
    else:
        assert lines == ["activity: " + json.dumps(event), "done: {}"]
    assert server.requests[-1][2]["accept"] == "text/event-stream"
    assert server.requests[-1][3] == {"task_id": "demo", "since": 6}
    assert closed.wait(2)


def test_generated_activity_stream_interrupt_closes_connection(activity_cli, monkeypatch):
    _, base, _, closed, _ = activity_cli
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(loopercli, "print", interrupt, raising=False)
    assert loopercli.main([*base, "tasks", "activity-stream", "--task-id", "demo"]) == 130
    assert closed.wait(2)


@pytest.mark.parametrize("status,exit_code,message", [(302, 4, "does not follow redirects"),
                                                      (503, 1, "unavailable: read failed")])
def test_generated_activity_stream_refusals(activity_cli, capsys, status, exit_code, message):
    server, base, mode, *_ = activity_cli
    mode["status"] = status
    assert loopercli.main([*base, "tasks", "activity-stream", "--task-id", "demo"]) == exit_code
    assert message in capsys.readouterr().err
    assert len(server.requests) == 2
