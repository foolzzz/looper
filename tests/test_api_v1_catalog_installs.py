"""Catalog commands are generated from the public document without changing Manager tools."""

import pytest

from looper import loopercli
from looper.agents import api_v1


@pytest.mark.parametrize(("operation", "flags", "body"), [
    ("catalog_items.list", [], {}),
    ("catalog_items.get", ["--item-id", "skill-example"], {"item_id": "skill-example"}),
    ("catalog_items.update", ["--item-id", "skill-example", "--fields", '{"description":"Changed"}'],
     {"item_id": "skill-example", "fields": {"description": "Changed"}}),
    ("catalog_items.delete", ["--item-id", "skill-example"], {"item_id": "skill-example"}),
    ("installs.create", ["--item-id", "skill-example", "--scope", "project", "--project-id", "proj-example",
                         "--roles", '["task_role"]', "--no-enabled"],
     {"item_id": "skill-example", "scope": "project", "project_id": "proj-example", "roles": ["task_role"],
      "enabled": False}),
    ("installs.update", ["--install-id", "inst-example", "--no-enabled"],
     {"install_id": "inst-example", "enabled": False}),
    ("installs.delete", ["--install-id", "inst-example"], {"install_id": "inst-example"}),
    ("installs.project", ["--project-id", "proj-example"], {"project_id": "proj-example"}),
])
def test_catalog_cli_parser_and_request(operation, flags, body):
    document = api_v1.document()
    commands = {f"{head.replace('-', '_')}.{tail}": generated
                for head, tail, _, _, generated in loopercli.generated_operations(document)}
    generated = commands[operation]
    args = loopercli.build_parser(document).parse_args([*operation.replace("_", "-").split("."), *flags])
    assert generated.method == "POST" and generated.path == "/api/v1/" + operation.replace(".", "/")
    assert loopercli.request_body(args, generated) == body
    op = next(op for op in api_v1.operations() if op.operation_id == operation)
    assert op.tool is None and op.request["additionalProperties"] is False
    assert not document["paths"][generated.path.removeprefix("/api/v1")]["post"].get("x-looper-cli-hand")
