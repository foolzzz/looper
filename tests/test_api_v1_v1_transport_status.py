"""Versioned status and the domain transport's HTTP, validation and confirmation boundaries."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1, operator_auth
from looper.agents.http_server import _json_response
from looper.agents.ops.clients import ClientOps
from looper.db import api_clients, manager_actions, projects, tasks
from tests.test_api_v1_auth import CLIENT, CLIENT_TOKEN, TOKEN, _bearer, _send, _send_raw, _serving
from tests.test_api_v1_auth import clients as clients
from tests.test_operator_auth import PASSWORD, USER, _password_login

pytestmark = pytest.mark.usefixtures("clients")

STATUS = "/api/v1/system/status"
AGENT = {"healthy": False, "state": "backoff", "up": False, "restart_count": 2,
         "last_crash_at": "2026-10-22T12:00:00Z", "next_restart_at": None, "agent_type": "manager"}
WARNING = {"id": "pg_dump", "label": "Database backup", "status": "warn", "detail": "Not installed", "fix": "Install pg_dump"}
RICH = {"type": "object", "additionalProperties": False, "required": ["profile", "rows", "choice", "weight"],
        "properties": {"profile": {"type": ["string", "null"]},
                       "rows": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                 "required": ["name"], "properties": {"name": {"type": "string"}}}},
                       "choice": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
                       "weight": {"type": "number"}}}
VALID = {"profile": None, "rows": [{"name": "kept"}], "choice": 3, "weight": 0.5}


@pytest.fixture(autouse=True)
def clear_documents():
    api_v1._index.cache_clear()
    api_v1._default_document.cache_clear()
    yield
    api_v1._index.cache_clear()
    api_v1._default_document.cache_clear()


@pytest.fixture
def status_data(monkeypatch):
    async def counts():
        return [{"status": "running", "count": 2}, {"status": "waiting", "count": 3},
                {"status": "completed", "count": 4}, {"status": "ready", "count": 1}]
    monkeypatch.setattr(tasks, "status_counts", counts)


def prepare_status(server):
    server._supervisor = SimpleNamespace(agent_status=lambda: {"manager": dict(AGENT)})
    server._web_adapter = SimpleNamespace(healthcheck=lambda: {"channel": "web", "healthy": True, "active_sse_streams": 2})
    server._self_checks = [dict(WARNING)]


@pytest.mark.parametrize("identity", ["bearer", "cookie", "password", "client"])
def test_status_preserves_the_complete_projection(tmp_path, status_data, identity):
    if identity == "password":
        operator_auth.set_password(tmp_path, USER, PASSWORD)
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            prepare_status(server)
            headers = _bearer(CLIENT_TOKEN if identity == "client" else TOKEN)
            if identity == "cookie":
                headers = {"Cookie": f"{operator_auth.cookie_name(server._port)}={TOKEN}"}
            elif identity == "password":
                code, login, _ = await _password_login(server)
                assert code == 303
                headers = {"Cookie": login["set-cookie"].split(";", 1)[0]}
            code, _, answer = await _send(server, f"POST {STATUS}", headers, "{}")
            assert code == 200
            assert answer["result"] == {"drain": server._drain.snapshot(), "status": "running", "agents": {"manager": AGENT},
                "tasks": {"total": 10, "running": 5, "completed": 4},
                "channels": {"web": {"channel": "web", "healthy": True, "active_sse_streams": 2}},
                "checks": [WARNING], "can_sign_out": identity == "password",
                "turn_binding": {"enforced": False, "mismatches": 0, "by": []}}
            schema = api_v1.find("POST", STATUS).result
            api_v1.validate_arguments(schema, answer["result"])
    asyncio.run(run())


@pytest.mark.parametrize("field", ["can_sign_out", "principal", "identity", "client_id"])
def test_status_does_not_accept_identity_from_the_body(tmp_path, status_data, field):
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            prepare_status(server)
            code, _, answer = await _send(server, f"POST {STATUS}", _bearer(CLIENT_TOKEN), json.dumps({field: True}))
            assert code == 400 and answer["error"]["code"] == "invalid"
            code, _, answer = await _send(server, f"POST {STATUS}", _bearer(CLIENT_TOKEN), "{}")
            assert code == 200 and answer["result"]["can_sign_out"] is False
    asyncio.run(run())


def test_old_status_is_absent_after_authenticated_v1_positive_control(tmp_path, status_data):
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            prepare_status(server)
            assert (await _send(server, f"POST {STATUS}", _bearer(TOKEN), "{}"))[0] == 200
            assert (await _send(server, "GET /api/status", _bearer(TOKEN)))[0] == 404
    asyncio.run(run())


@pytest.mark.parametrize("script", ["release/agent-serve-wheel-smoke.py", "pi/isolated-smoke.py"])
@pytest.mark.parametrize("response", [None, {"status": "running"},
    {"status": "error", "error": {"code": "internal", "message": "unavailable"}},
    {"status": "ok", "result": {"status": "stopped"}}], ids=["ready", "legacy", "error", "stopped"])
def test_smoke_readiness_uses_v1_and_checks_the_response(tmp_path, status_data, monkeypatch, script, response):
    path = Path(__file__).resolve().parents[1] / "examples" / script
    spec = importlib.util.spec_from_file_location("status_smoke", path)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    runtime = tmp_path / "rt"
    runtime.mkdir()
    (runtime / "operator-token").write_text(TOKEN)
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: None, wait=lambda **_: None)

    def no_retry(_seconds):
        raise AssertionError("readiness request failed")

    # Keep the retry guard local: subprocess timeout waits also use time.sleep.
    monkeypatch.setattr(smoke, "time", SimpleNamespace(sleep=no_retry, monotonic=smoke.time.monotonic))

    class Ready(Exception):
        pass

    async def run():
        async with _serving(tmp_path, listen=True) as server:
            assert server._port != 8767
            prepare_status(server)
            original = server._handle_v1
            requests = []

            async def status_request(method, path, query, headers, body, writer, principal):
                requests.append((method, path, headers["content-type"], body))
                if response is None:
                    await original(method, path, query, headers, body, writer, principal)
                else:
                    writer.write(_json_response(api_v1.OK, response))
                    await writer.drain()

            monkeypatch.setattr(server, "_handle_v1", status_request)
            if script.startswith("release/"):
                async def no_projects(**_kwargs):
                    return []
                monkeypatch.setattr(projects, "list_projects", no_projects)
                (tmp_path / "index.html").write_text('<div id="root"></div>')
                monkeypatch.setattr(smoke, "free_port", lambda: server._port)
                monkeypatch.setattr(smoke.subprocess, "Popen", lambda *_, **__: process)
                call = lambda: smoke.serve_check(tmp_path / "venv", tmp_path, "unused")
            else:
                monkeypatch.setattr(smoke, "managed_process", lambda *_, **__: nullcontext(process))
                monkeypatch.setattr(smoke.shutil, "which", lambda _: None)
                original_request = smoke.request

                def request(base, path, *args):
                    if path == "/api/v1/channel_configs/list":
                        raise Ready  # Readiness succeeded; the Pi registry flow is outside this regression.
                    return original_request(base, path, *args)

                helper = SimpleNamespace(_run=lambda *_, **__: None)
                installed = (tmp_path, tmp_path / "venv/bin/python", {})
                # registry_smoke chooses its own port; route its local requests to this test server.
                original_request_base = f"http://127.0.0.1:{server._port}"
                monkeypatch.setattr(smoke, "request", lambda _base, *args: request(original_request_base, *args))
                call = lambda: smoke.registry_smoke(helper, installed, runtime, 0, "unused", tmp_path, "unused")

            if response is not None:
                with pytest.raises(AssertionError) as error:
                    await asyncio.to_thread(call)
                assert str(error.value) != "readiness request failed"
            elif script.startswith("release/"):
                await asyncio.to_thread(call)
            else:
                with pytest.raises(Ready):
                    await asyncio.to_thread(call)
            expected = [("POST", STATUS, "application/json", b"{}")]
            if response is None and script.startswith("release/"):
                # Once ready, the release smoke also reads projects through v1.
                expected.append(("POST", "/api/v1/project_admin/list", "application/json", b"{}"))
            assert requests == expected

    asyncio.run(run())


@pytest.fixture
def domain(monkeypatch, tmp_path):
    audit, callbacks = [], []
    output = tmp_path / "written.json"
    op = api_v1.Operation("transport.write", "POST", "/api/v1/transport/write", "Write values", "Write values.",
                          RICH, RICH, risk="high", confirms=True)
    async def run(server, operation, arguments, principal, headers):
        callbacks.append(("run", principal.key, headers.get("x-looper-session")))
        output.write_text(json.dumps(arguments))
        return arguments
    async def impact(server, operation, arguments, principal, headers):
        callbacks.append(("impact", principal.key, headers.get("x-looper-session")))
        return "Replace stored values"
    async def record(**fields):
        audit.append(fields)
    results = {name: schema for registered in api_v1.domains() for name, schema in registered.RESULTS.items()}
    module = SimpleNamespace(OPERATIONS=(op,), RESULTS=results, run=run, impact=impact)
    monkeypatch.setattr(api_v1, "domains", lambda: (module,))
    monkeypatch.setattr(manager_actions, "record", record)
    return SimpleNamespace(module=module, op=op, audit=audit, callbacks=callbacks, output=output)


@pytest.mark.parametrize("variant", ["normal", "duplicate_id", "duplicate_path", "typed_result", "missing_impact", "stream", "write_stream"])
def test_domain_registration_is_unique_discoverable_and_documented(domain, variant):
    if variant == "stream":
        events = {"type": "object", "properties": {"changed": {"type": "boolean"}}}
        stream = replace(domain.op, stream=True, risk="read", stream_description="Changed events.", stream_events=events)
        domain.module.OPERATIONS = (stream,)
        response = api_v1.document()["paths"]["/transport/write"]["post"]["responses"]["200"]
        assert response["description"] == "Changed events."
        assert response["content"]["text/event-stream"]["schema"] == events
        return
    if variant in ("duplicate_id", "duplicate_path", "missing_impact", "write_stream"):
        if variant == "write_stream":
            domain.module.OPERATIONS = (replace(domain.op, stream=True),)
        elif variant == "missing_impact":
            del domain.module.impact
        else:
            changes = {"path": "/api/v1/transport/other"} if variant == "duplicate_id" else {"operation_id": "transport.other"}
            domain.module.OPERATIONS += (replace(domain.op, **changes),)
        with pytest.raises(ValueError):
            api_v1.operations()
        return
    if variant == "typed_result":
        domain.module.RESULTS["projects.list"] = {"type": "object", "properties": {"projects": {"type": "array"}}}
    operation = api_v1.find("POST", domain.op.path)
    assert operation == domain.op and api_v1.domain_for(operation) is domain.module
    document = api_v1.document()
    assert api_v1.document() is document
    assert document["paths"]["/transport/write"]["post"]["operationId"] == "transport.write"
    if variant == "typed_result":
        assert next(op for op in api_v1.operations() if op.operation_id == "projects.list").result == domain.module.RESULTS["projects.list"]


def request_once(tmp_path, line, body, token=TOKEN):
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            return await _send(server, line, _bearer(token), body)
    return asyncio.run(run())


def test_operator_only_refusal_precedes_validation_callbacks_and_audit(tmp_path, domain):
    domain.module.OPERATIONS = (replace(domain.op, operator_only=True),)
    code, _, answer = request_once(tmp_path, f"POST {domain.op.path}", "not json", CLIENT_TOKEN)
    assert code == 403 and answer["error"]["code"] == "forbidden"
    assert domain.callbacks == domain.audit == [] and not domain.output.exists()


@pytest.mark.parametrize(("method", "body", "expected"), [("POST", "{", 400), ("GET", "", 405)])
def test_malformed_json_and_wrong_method_have_typed_errors(tmp_path, domain, method, body, expected):
    code, _, answer = request_once(tmp_path, f"{method} {domain.op.path}", body)
    assert code == expected and answer["error"]["code"] == "invalid"
    assert not domain.output.exists()


@pytest.mark.parametrize("change", [{"profile": "chosen", "choice": "named"}, {"profile": None},
    {"weight": True}, {"choice": False}, {"rows": [{"name": "kept", "unknown": 1}]}, {"unknown": 1}, {"omit": "profile"}])
def test_rich_schema_is_enforced_at_the_http_input_boundary(tmp_path, domain, change):
    arguments = {**VALID, **change}
    if "omit" in arguments:
        arguments.pop(arguments.pop("omit"))
    accepted = change in ({"profile": "chosen", "choice": "named"}, {"profile": None})
    code, _, answer = request_once(tmp_path, f"POST {domain.op.path}", json.dumps(arguments))
    assert code == (200 if accepted else 400)
    if accepted:
        assert answer["result"] == json.loads(domain.output.read_text()) == arguments
    else:
        assert answer["error"]["code"] == "invalid" and not domain.output.exists()


@pytest.mark.parametrize("identity", ["operator", "client"])
def test_high_write_runs_once_with_operator_or_matching_client_confirmation(tmp_path, domain, monkeypatch, identity):
    original = api_clients.find
    other = "lpc_" + "d" * 43
    async def find(token, **kwargs):
        return {**CLIENT, "id": "client-other"} if token == other else await original(token, **kwargs)
    monkeypatch.setattr(api_clients, "find", find)
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            headers = {**_bearer(TOKEN if identity == "operator" else CLIENT_TOKEN), "X-Looper-Session": "action-one"}
            code, _, answer = await _send(server, f"POST {domain.op.path}", headers, json.dumps(VALID))
            if identity == "client":
                assert code == 202 and answer["impact"] == "Replace stored values" and not domain.output.exists()
                body = json.dumps({"confirmation_id": answer["confirmation_id"]})
                for bad in ({**headers, "X-Looper-Session": "action-two"}, {**headers, **_bearer(other)}):
                    refused, _, error = await _send(server, "POST /api/v1/confirm", bad, body)
                    assert refused == 404 and error["error"]["code"] == "not_found" and not domain.output.exists()
                code, _, answer = await _send(server, "POST /api/v1/confirm", headers, body)
                repeated, _, error = await _send(server, "POST /api/v1/confirm", headers, body)
                assert repeated == 404 and error["error"]["code"] == "not_found"
            assert code == 200 and answer["result"] == json.loads(domain.output.read_text()) == VALID
            assert [item[0] for item in domain.callbacks].count("run") == 1
            assert [item["status"] for item in domain.audit] == (["needs_confirmation", "ok"] if identity == "client" else ["ok"])
    asyncio.run(run())


def test_status_has_a_generated_cli_parser_and_json_request():
    args = loopercli.build_parser(api_v1.document()).parse_args(["system", "status"])
    assert args._generated.path == STATUS
    assert loopercli.request_body(args, args._generated) == {}


@pytest.mark.parametrize("outcome", ["client", "ok", "error"])
def test_credential_domains_refuse_clients_and_never_audit_or_expose_failures(tmp_path, domain, outcome):
    domain.module.OPERATIONS = (replace(domain.op, operator_only=True, credential_write=True),)
    if outcome == "error":
        async def fail(*args):
            raise ValueError("private credential body")
        domain.module.run = fail
    code, _, answer = request_once(tmp_path, f"POST {domain.op.path}",
        "not json" if outcome == "client" else json.dumps(VALID), CLIENT_TOKEN if outcome == "client" else TOKEN)
    assert code == ({"client": 403, "ok": 200, "error": 400}[outcome])
    assert domain.audit == []
    if outcome == "error":
        assert "private credential body" not in json.dumps(answer)
    elif outcome == "client":
        assert domain.callbacks == [] and not domain.output.exists()
    else:
        assert answer["result"] == json.loads(domain.output.read_text()) == VALID


def test_domain_stream_receives_validated_body_principal_and_request_headers(tmp_path, domain):
    domain.module.OPERATIONS = (replace(domain.op, stream=True, risk="read", confirms=False),)
    async def stream(server, operation, arguments, principal, headers, writer):
        assert arguments == VALID and principal.client_id == CLIENT["id"] and headers["x-trace"] == "stream"
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\nevent: changed\ndata: {"ok":true}\n\n')
        await writer.drain()
    domain.module.stream = stream
    async def run():
        async with _serving(tmp_path, listen=True) as server:
            code, headers, body = await _send_raw(server, f"POST {domain.op.path}",
                {**_bearer(CLIENT_TOKEN), "X-Trace": "stream"}, json.dumps(VALID))
            assert code == 200 and headers["content-type"] == "text/event-stream"
            assert body == 'event: changed\ndata: {"ok":true}\n\n' and domain.audit == []
    asyncio.run(run())
