"""The v1 operations and the OpenAPI document generated from the API client's tools (RFC §5.1–§5.3). No database."""

from __future__ import annotations

import json

import pytest

from looper import __version__
from looper.agents import api_v1
from looper.agents.ops import ErrorCode, OpError, check_arguments
from looper.agents.ops.clients import CLIENT_TOOLS

HAND = {"chat.send", "chat.messages", "chat.conversations", "confirm", "openapi"}
TYPED_TOOL_OPERATIONS = {
    "tasks.list", "tasks.get", "tasks.room", "task_pipeline", "task_create", "task_control.start", "task_control.stop",
    "task_control.restart", "task_control.start_over", "task_control.cancel", "task_update", "task_instruct",
    "gates.list", "gates.messages", "gates.reply", "gates.end", "agent_profiles.list", "agent_profiles.create",
    "agent_profiles.update", "agent_profiles.delete", "overview", "config_export",
    "settings.set", "concurrency", "schedules.list", "schedules.get", "schedules.create", "schedules.update",
    "schedules.enable", "schedules.disable", "schedules.run", "schedules.delete", "schedules.reset_planner_session",
    "catalog.list", "catalog.install", "catalog.update", "catalog.uninstall",
    "models.list", "models.create", "models.update", "models.delete", "costs"}


def _actions(tool) -> list[str] | None:
    if "action" in (tool.schema.get("required") or ()):
        return tool.schema["properties"]["action"].get("enum") or None
    return None


def test_every_tool_is_covered_by_its_operations() -> None:
    ops = api_v1.operations()
    by_tool: dict[str, list[api_v1.Operation]] = {}
    for op in ops:
        if op.tool:
            by_tool.setdefault(op.tool, []).append(op)
    assert set(by_tool) == {tool.name for tool in CLIENT_TOOLS}
    for tool in CLIENT_TOOLS:
        actions = _actions(tool)
        base = tool.name.removeprefix("looper_")
        if actions:
            assert sorted(op.action for op in by_tool[tool.name]) == sorted(actions)
            for op in by_tool[tool.name]:
                assert op.operation_id == f"{base}.{op.action}" and op.path == f"/api/v1/ops/{base}/{op.action}"
                assert "action" not in op.request["properties"]
                assert "action" not in (op.request.get("required") or ())
                assert op.risk == ("read" if op.action in tool.read_actions else tool.risk.value)
        else:
            (op,) = by_tool[tool.name]
            assert (op.operation_id, op.path, op.action) == (base, f"/api/v1/ops/{base}", None)
            assert op.request == tool.schema and op.request is not tool.schema
    names = {op.tool for op in ops}
    assert "looper_task_gate_reply" not in names and "looper_confirm" not in names
    assert not any("gate_reply" in op.operation_id or op.operation_id.startswith("confirm.") for op in ops)
    ids = [op.operation_id for op in ops]
    assert len(set(ids)) == len(ids)
    assert len({(op.method, op.path) for op in ops}) == len(ops)


def test_an_operation_does_not_share_its_schema_with_the_tool() -> None:
    gates = next(tool for tool in CLIENT_TOOLS if tool.name == "looper_gates")
    api_v1.tool_operations([gates])
    assert "action" in gates.schema["properties"] and "action" in gates.schema["required"]


def test_summary_and_escalation_note() -> None:
    for op in api_v1.operations():
        assert 0 < len(op.summary) <= 120 and op.description.startswith(op.summary.rstrip("…"))
    create = next(op for op in api_v1.operations() if op.operation_id == "task_create")
    assert create.description.endswith("POST /api/v1/confirm runs them.")
    overview = next(op for op in api_v1.operations() if op.operation_id == "overview")
    assert "needs_confirmation" not in overview.description


def test_the_document_is_shaped_as_openapi() -> None:
    doc = api_v1.document()
    assert doc["openapi"] == "3.1.0"
    assert doc["info"]["version"] == __version__ and doc["info"]["x-looper-api"] == 1
    assert "Remaining legacy /api routes are not described" in doc["info"]["description"]
    assert doc["servers"] == [{"url": "/api/v1"}] and doc["security"] == [{"bearer": []}]
    assert doc["components"]["securitySchemes"] == {"bearer": {"type": "http", "scheme": "bearer"}}
    schemas = doc["components"]["schemas"]
    assert schemas["ErrorCode"]["enum"] == [c.value for c in ErrorCode]
    assert {"OpError", "OpOk", "OpNeedsConfirmation"} <= set(schemas)
    ids, hand = [], set()
    for path, item in doc["paths"].items():
        assert path.startswith("/")
        ((method, op),) = item.items()
        ids.append(op["operationId"])
        assert op["responses"]["default"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/OpError"}
        if op.get("x-looper-cli-hand"):
            hand.add(op["operationId"])
        if method == "post":
            (session,) = op["parameters"]
            assert session["name"] == "X-Looper-Session" and session["in"] == "header" and not session["required"]
            assert session["schema"]["pattern"] == "^[A-Za-z0-9._:-]{1,64}$"
        else:
            assert "parameters" not in op and "requestBody" not in op
    assert len(set(ids)) == len(ids) and hand == HAND
    assert "whoami" in ids
    assert doc is api_v1.document()  # built once


def test_responses_say_which_calls_may_ask_for_confirmation() -> None:
    paths = api_v1.document()["paths"]
    assert "202" not in paths["/ops/tasks/list"]["post"]["responses"]
    assert "202" in paths["/ops/task_create"]["post"]["responses"]
    assert "202" in paths["/ops/gates/reply"]["post"]["responses"]
    assert "202" not in paths["/ops/gates/list"]["post"]["responses"]
    send = paths["/chat/send"]["post"]["responses"]["200"]
    assert list(send["content"]) == ["text/event-stream"]
    assert all(event in send["description"] for event in ("accepted", "delta", "status", "final", "heartbeat",
                                                          "timeout"))
    ok = paths["/ops/tasks/list"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok["allOf"][0] == {"$ref": "#/components/schemas/OpOk"}
    assert ok["allOf"][1]["properties"]["result"]["required"] == ["tasks", "more"]
    assert paths["/ops/tasks/get"]["post"]["x-looper-risk"] == "read"
    assert paths["/ops/tasks/get"]["post"]["x-looper-tool"] == "looper_tasks"
    assert paths["/ops/tasks/get"]["post"]["x-looper-action"] == "get"
    assert paths["/whoami"]["get"]["operationId"] == "whoami"
    assert "x-looper-cli-hand" not in paths["/whoami"]["get"]


def test_only_explicit_operator_operations_touch_credentials() -> None:
    for op in api_v1.operations():
        if op.operation_id.startswith("clients."):
            assert op.operation_id in {"clients.list", "clients.create", "clients.revoke"}
            assert op.operator_only and op.tool is None
        if op.operation_id.startswith("auth."):
            assert op.operation_id in {"auth.set_password", "auth.clear_password"}
            assert op.tool is None
    for tool in CLIENT_TOOLS:
        assert not any(word in tool.name for word in ("password", "token", "client"))


@pytest.mark.parametrize("op", [op for op in api_v1.operations() if op.request is not None],
                         ids=lambda op: op.operation_id)
def test_a_request_schema_checks_arguments_like_the_tool(op: api_v1.Operation) -> None:
    if op.request.get("required"):
        with pytest.raises(OpError) as raised:
            check_arguments(op.request, {})
        assert raised.value.code is ErrorCode.INVALID
    else:
        assert check_arguments(op.request, {}) == {}


def test_the_untyped_results_are_the_tool_operations_without_a_schema() -> None:
    ops = api_v1.operations()
    untyped = {op.operation_id for op in ops if op.result.get("x-looper-result-typed") is False}
    tool_ids = {op.operation_id for op in ops if op.tool}
    assert untyped == tool_ids - TYPED_TOOL_OPERATIONS
    assert TYPED_TOOL_OPERATIONS <= tool_ids
    assert not any(op.result.get("x-looper-result-typed") is False for op in ops if not op.tool)


def test_the_hand_operations_declare_their_requests() -> None:
    by_id = {op.operation_id: op for op in api_v1.operations()}
    send = by_id["chat.send"]
    assert send.stream and send.request["additionalProperties"] is False
    assert send.request["properties"]["text"] == {"type": "string", "minLength": 1, "maxLength": 32_000}
    assert send.request["properties"]["language"]["enum"] == ["zh", "en"]
    assert by_id["chat.messages"].request["required"] == ["conversation_id"]
    assert by_id["chat.messages"].result["required"] == [
        "conversation_id", "manager_id", "messages", "next_cursor", "has_more"]
    assert by_id["chat.conversations"].request == {"type": "object", "additionalProperties": False,
                                                   "properties": {}}
    assert by_id["confirm"].request["required"] == ["confirmation_id"]
    assert by_id["openapi"].method == "GET" and by_id["openapi"].request is None


def test_find_matches_method_and_path_exactly() -> None:
    assert api_v1.find("POST", "/api/v1/ops/tasks/list").operation_id == "tasks.list"
    assert api_v1.find("GET", "/api/v1/ops/tasks/list") is None
    assert api_v1.find("GET", "/api/v1/openapi.json").operation_id == "openapi"
    assert api_v1.find("POST", "/api/v1/ops/tasks/list/") is None
    assert api_v1.allowed_method("/api/v1/ops/tasks/list") == "POST"
    assert api_v1.allowed_method("/api/v1/ops/nope") is None


def test_the_document_is_json() -> None:
    text = json.dumps(api_v1.document())
    assert json.loads(text) == api_v1.document()


def test_domain_registration_extends_the_document_without_extending_tools():
    from looper.agents.v1_domains import MODULES

    assert "status" in MODULES and len(MODULES) == len(set(MODULES))
    op = api_v1.find("POST", "/api/v1/system/status")
    assert op.operation_id == "system.status" and op.tool is None and not op.hand
    assert api_v1.document()["paths"]["/system/status"]["post"]["operationId"] == op.operation_id


def test_error_details_are_optional_and_typed():
    error = api_v1.document()["components"]["schemas"]["OpError"]["properties"]["error"]
    assert error["required"] == ["code", "message"]
    details = error["properties"]["details"]
    assert details["additionalProperties"] is False and "required" not in details
    assert details["properties"] == {"manager_id": {"type": "string"}, "state": {"type": "string"},
        "phase": {"type": ["string", "null"]}, "method": {"type": ["string", "null"]}}
