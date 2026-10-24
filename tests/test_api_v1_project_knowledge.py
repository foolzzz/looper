"""Project knowledge exposes ordinary, schema-driven CLI operations."""

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops import OpError


@pytest.mark.parametrize(("action", "extra", "risk"), [
    ("list", {}, "read"),
    ("add", {"document": {"source_kind": "text", "title": "Guide", "summary": "Reference", "content": "Read."}}, "write"),
    ("get", {"document_id": "doc-example"}, "read"),
    ("update", {"document_id": "doc-example", "fields": {"content": "Changed."}}, "write"),
    ("delete", {"document_id": "doc-example"}, "high"),
    ("drafts", {}, "read"),
    ("confirm_draft", {"draft_id": "draft-example", "fields": {"title": "Lesson"}}, "write"),
    ("discard_draft", {"draft_id": "draft-example"}, "high"),
])
def test_generated_cli_uses_project_knowledge_operations(action, extra, risk):
    import json

    document = api_v1.document()
    body = {"project_id": "proj-example", **extra}
    flags = [part for key, value in body.items() for part in (
        "--" + key.replace("_", "-"), json.dumps(value) if isinstance(value, dict) else value)]
    commands = {f"{head.replace('-', '_')}.{tail.replace('-', '_')}": generated
                for head, tail, _, _, generated in loopercli.generated_operations(document)}
    generated = commands["project_knowledge." + action]
    args = loopercli.build_parser(document).parse_args(["project-knowledge", action.replace("_", "-"), *flags])
    assert generated.path == "/api/v1/project_knowledge/" + action and generated.method == "POST"
    assert loopercli.request_body(args, generated) == body
    op = api_v1.find("POST", generated.path)
    assert op.tool is None and op.risk == risk and op.confirms is (risk == "high")
    assert not document["paths"]["/project_knowledge/" + action]["post"].get("x-looper-cli-hand")


@pytest.mark.parametrize(("action", "body"), [
    ("list", {}), ("list", {"project_id": None}), ("drafts", {"project_id": 1}),
    ("get", {"project_id": "p", "document_id": None}),
    ("delete", {"project_id": "p", "document_id": 1}),
    ("confirm_draft", {"project_id": "p", "draft_id": None}),
    ("discard_draft", {"project_id": "p", "draft_id": 1}),
    ("update", {"project_id": "p", "document_id": "d", "fields": {"content": None}}),
    ("confirm_draft", {"project_id": "p", "draft_id": "d", "fields": {"path": "guide.md"}}),
    ("list", {"project_id": "p", "runtime_root": "/forbidden"}),
])
def test_requests_require_string_identifiers_and_closed_fields(action, body):
    with pytest.raises(OpError):
        api_v1.validate_arguments(api_v1.find("POST", "/api/v1/project_knowledge/" + action).request, body)


@pytest.mark.parametrize("field", ["title", "summary", "source_kind", "content"])
@pytest.mark.parametrize("value", [None, 1])
def test_new_document_requires_declared_string_source_fields(field, value):
    document = {"source_kind": "text", "title": "Guide", "summary": "Reference", "content": "Read.", field: value}
    with pytest.raises(OpError):
        api_v1.validate_arguments(api_v1.find("POST", "/api/v1/project_knowledge/add").request,
                                  {"project_id": "p", "document": document})
