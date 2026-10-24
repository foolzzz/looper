"""Acquisition commands come from OpenAPI and stay outside Manager's tools."""

import pytest

from looper import loopercli
from looper.agents import api_v1


@pytest.mark.parametrize(("action", "flags", "body"), [
    ("import_machine", [], {}), ("import_mcp", [], {}),
    ("upload", ["--filename", "SKILL.md", "--content-base64", "eA=="],
     {"filename": "SKILL.md", "content_base64": "eA=="}),
    ("git_preview", ["--url", "https://example.test/skills", "--ref", "main"],
     {"url": "https://example.test/skills", "ref": "main"}),
    ("git_import", ["--url", "https://example.test/skills", "--commit", "a" * 40,
                    "--items", '[{"path":"skills/example"}]'],
     {"url": "https://example.test/skills", "commit": "a" * 40, "items": [{"path": "skills/example"}]}),
    ("create_skill", ["--name", "example", "--description", "Instructions", "--content", "Reply."],
     {"name": "example", "description": "Instructions", "content": "Reply."}),
    ("create_mcp", ["--name", "example", "--transport", "stdio", "--command", "example",
                    "--env", '{"KEY":"synthetic"}'],
     {"name": "example", "transport": "stdio", "command": "example", "env": {"KEY": "synthetic"}}),
])
def test_generated_acquisition_cli_parser_and_request(action, flags, body):
    document = api_v1.document()
    generated = next(op for head, tail, _, _, op in loopercli.generated_operations(document)
                     if head == "catalog-sources" and tail == action.replace("_", "-"))
    args = loopercli.build_parser(document).parse_args(["catalog-sources", action.replace("_", "-"), *flags])
    assert generated.method == "POST" and generated.path == f"/api/v1/catalog_sources/{action}"
    assert loopercli.request_body(args, generated) == body
    operation = api_v1.find("POST", generated.path)
    assert operation.tool is None and operation.request["additionalProperties"] is False
    assert not document["paths"][generated.path.removeprefix("/api/v1")]["post"].get("x-looper-cli-hand")


def test_acquisition_credential_fields_are_write_only():
    operation = api_v1.find("POST", "/api/v1/catalog_sources/create_mcp")
    assert operation.operator_only and operation.credential_write
    for field in ("env", "headers"):
        assert operation.request["properties"][field]["writeOnly"] is True
    assert set(api_v1.find("POST", "/api/v1/catalog_sources/git_import").request["required"]) == {
        "url", "commit", "items"}
