"""Schedule preview validation and generated CLI requests use ordinary read operations."""

import asyncio
import json

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops import OpError
from looper.agents.v1_domains import schedule_reads
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_loopercli import Server, _send as cli_send, write_client_file
from tests.test_operator_auth import TOKEN


@pytest.mark.parametrize("cron,gated,valid", [("*/5 * * * *", False, True), ("* * * * *", True, True),
                                             ("not a cron", False, False), ("* * * * *", False, False)])
def test_preview_preserves_invalid_rules_as_normal_results(tmp_path, cron, gated, valid):
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await _send(server, "POST /api/v1/schedule_admin/preview", _bearer(TOKEN),
                                            json.dumps({"cron": cron, "gated": gated, "count": 2, "timezone": "UTC"}))
            assert status == 200 and answer["status"] == "ok"
            result = answer["result"]
            api_v1.validate_arguments(schedule_reads.PREVIEW, result)
            assert result["valid"] is valid
            assert len(result["next"]) == (2 if valid else 0)
            assert bool(result["error"]) is not valid
    asyncio.run(run())


@pytest.mark.parametrize("body", [{}, {"cron": 3}, {"cron": "0 * * * *", "count": True},
    {"cron": "0 * * * *", "count": 0}, {"cron": "0 * * * *", "count": 21},
    {"cron": "0 * * * *", "timezone": None}, {"cron": "0 * * * *", "gated": "yes"},
    {"cron": "0 * * * *", "extra": 1}])
def test_malformed_preview_is_a_typed_invalid_request(tmp_path, body):
    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await _send(server, "POST /api/v1/schedule_admin/preview", _bearer(TOKEN), json.dumps(body))
            assert status == 400 and answer["error"]["code"] == "invalid"
    asyncio.run(run())


@pytest.mark.parametrize("action,args,body", [
    ("list", [], {}), ("project_list", ["--project-id", "proj-example"], {"project_id": "proj-example"}),
    ("get", ["--schedule-id", "sched-example"], {"schedule_id": "sched-example"}),
    ("preview", ["--cron", "0 * * * *", "--count", "20", "--gated"], {"cron": "0 * * * *", "count": 20, "gated": True}),
    ("runs", ["--schedule-id", "sched-example", "--limit", "200", "--skips", "hide"],
     {"schedule_id": "sched-example", "limit": 200, "skips": "hide"}),
    ("runs", ["--schedule-id", "sched-example", "--status", "failed"],
     {"schedule_id": "sched-example", "status": "failed"}),
    ("run_log", ["--run-id", "srun-example", "--offset", "0", "--limit-bytes", "1048576"],
     {"run_id": "srun-example", "offset": 0, "limit_bytes": 1048576}),
    ("run_log", ["--run-id", "srun-example", "--tail-lines", "3"],
     {"run_id": "srun-example", "tail_lines": 3}),
    ("notifications", ["--schedule-id", "sched-example", "--limit", "1"], {"schedule_id": "sched-example", "limit": 1})])
def test_generated_schedule_commands_send_the_declared_request(tmp_path, capsys, action, args, body):
    def respond(handler, request):
        cli_send(handler, 200, api_v1.document() if request[1].endswith("openapi.json")
                 else {"status": "ok", "result": {"received": True}})
    server = Server(respond)
    try:
        client = tmp_path / "client.json"
        write_client_file(client, server.url)
        assert loopercli.main(["--client-file", str(client), "--json", "schedule-admin", action.replace("_", "-"), *args]) == 0
        method, path, _, sent = server.requests[-1]
        assert (method, path, sent) == ("POST", f"/api/v1/schedule_admin/{action}", body)
        op = api_v1.find(method, path)
        assert not op.hand and op.risk == "read" and op.tool is None
        assert "202" not in api_v1.document()["paths"][path.removeprefix("/api/v1")]["post"]["responses"]
    finally:
        server.close()


@pytest.mark.parametrize("action,body", [
    ("runs", {"schedule_id": "sched-example", "status": "unknown"}),
    ("runs", {"schedule_id": "sched-example", "status": None}),
    ("run_log", {}), ("run_log", {"run_id": 1}),
    *[("run_log", {"run_id": "srun-example", key: value}) for key, value in (
        ("offset", -1), ("offset", True), ("limit_bytes", 0), ("limit_bytes", 4 * 1024 * 1024 + 1),
        ("tail_lines", 0), ("tail_lines", 5001), ("tail_lines", True))],
])
def test_run_read_requests_validate_status_and_log_bounds(action, body):
    operation = api_v1.find("POST", f"/api/v1/schedule_admin/{action}")
    assert operation is not None
    with pytest.raises(OpError):
        api_v1.validate_arguments(operation.request, body)


@pytest.mark.parametrize("status", [
    "queued", "gating", "running", "completed", "failed", "stopped", "cancelled", "timed_out",
    "gate_failed", "skipped_overlap", "skipped_missed", "skipped_gate", "skipped_budget", "succeeded", "interrupted",
])
def test_run_status_filter_accepts_every_persisted_status(status):
    operation = api_v1.find("POST", "/api/v1/schedule_admin/runs")
    api_v1.validate_arguments(operation.request, {"schedule_id": "sched-example", "status": status})


def test_run_log_uses_server_runtime_root(tmp_path, monkeypatch):
    async def read_log(run_id, arguments, runtime_root):
        assert run_id == "srun-example"
        assert arguments == {"run_id": "srun-example", "tail_lines": 3}
        assert runtime_root == tmp_path
        return {"run_id": run_id, "schedule_id": "sched-example", "size": 5, "offset": 0,
                "next_offset": None, "data_base64": "Ynl0ZXM=", "complete": True, "truncated": False}

    from types import SimpleNamespace

    monkeypatch.setattr(schedule_reads.schedules, "run_log", read_log, raising=False)
    operation = api_v1.find("POST", "/api/v1/schedule_admin/run_log")
    assert operation is not None
    result = asyncio.run(schedule_reads.run(SimpleNamespace(_runtime_root=tmp_path), operation,
                                            {"run_id": "srun-example", "tail_lines": 3}, None, {}))
    api_v1.validate_arguments(schedule_reads.RUN_LOG, result)
