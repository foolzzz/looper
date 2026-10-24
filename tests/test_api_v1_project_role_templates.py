"""Project role contracts and the generated standard-library CLI requests."""

from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops import OpError
from looper.agents.v1_domains import project_roles


@pytest.mark.parametrize("family,action,options,body", [
    ("project_roles", "stats", ["--project-id", "proj-example", "--since", "2026-10-01"],
     {"project_id": "proj-example", "since": "2026-10-01"}),
    ("role_templates", "list", ["--project-id", "proj-example"], {"project_id": "proj-example"}),
    ("role_templates", "put", ["--project-id", "proj-example", "--key", "tester", "--kind", "work",
        "--description", "Tests", "--role-prompt", "Write tests.", "--harness", "null", "--profile-id", "null",
        "--items", '["item-example"]'], {"project_id": "proj-example", "key": "tester", "kind": "work",
        "description": "Tests", "role_prompt": "Write tests.", "harness": None, "profile_id": None,
        "items": ["item-example"]}),
    ("role_templates", "delete", ["--project-id", "proj-example", "--key", "tester"],
     {"project_id": "proj-example", "key": "tester"}),
])
def test_generated_cli_posts_role_operations(family, action, options, body, capsys):
    args = loopercli.build_parser(api_v1.document()).parse_args([family.replace("_", "-"), action, *options])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="roles")) == 0
    assert calls == [("POST", f"/api/v1/{family}/{action}", body)]


@pytest.mark.parametrize("operation,body", [
    ("project_roles.stats", {"project_id": 1}),
    ("project_roles.stats", {"project_id": "proj-example", "since": None}),
    ("role_templates.list", {"project_id": "proj-example", "extra": True}),
    ("role_templates.delete", {"project_id": "proj-example", "key": None}),
    ("role_templates.put", {"project_id": "proj-example", "key": "tester", "kind": "work",
                            "description": "", "role_prompt": "Test.", "harness": None, "items": []}),
])
def test_request_identifiers_closed_fields_and_required_nullable_fields(operation, body):
    op = api_v1.find("POST", "/api/v1/" + operation.replace(".", "/"))
    assert op is not None and not op.hand
    with pytest.raises(OpError):
        api_v1.validate_arguments(op.request, body)


def test_named_role_schemas_are_published_without_changing_manager_tools():
    schemas = api_v1.document()["components"]["schemas"]
    for name, schema in project_roles.SCHEMAS.items():
        assert schemas[name] == schema
    assert [(op.operation_id, op.risk, op.confirms) for op in project_roles.OPERATIONS] == [
        ("project_roles.stats", "read", False), ("role_templates.list", "read", False),
        ("role_templates.put", "write", False), ("role_templates.delete", "high", True)]
    assert all(op.tool is None and not op.hand for op in project_roles.OPERATIONS)
