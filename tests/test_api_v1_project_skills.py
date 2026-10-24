"""Project-owned skill operations use generated, standard-library-only CLI commands."""

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops import OpError


@pytest.mark.parametrize(("action", "flags", "body", "risk"), [
    ("add", ["--project-id", "proj-example", "--skill",
             '{"name":"example","description":"Example instructions","content":"Check it.","enabled":false}'],
     {"project_id": "proj-example", "skill": {"name": "example", "description": "Example instructions",
                                              "content": "Check it.", "enabled": False}}, "write"),
    ("get", ["--project-id", "proj-example", "--skill-id", "skill-example"],
     {"project_id": "proj-example", "skill_id": "skill-example"}, "read"),
    ("update", ["--project-id", "proj-example", "--skill-id", "skill-example", "--fields", '{"content":"Changed."}'],
     {"project_id": "proj-example", "skill_id": "skill-example", "fields": {"content": "Changed."}}, "write"),
    ("delete", ["--project-id", "proj-example", "--skill-id", "skill-example"],
     {"project_id": "proj-example", "skill_id": "skill-example"}, "high"),
])
def test_project_skill_cli_parser_and_request(action, flags, body, risk):
    document = api_v1.document()
    operation = "project_skills." + action
    commands = {f"{head.replace('-', '_')}.{tail}": generated
                for head, tail, _, _, generated in loopercli.generated_operations(document)}
    generated = commands[operation]
    args = loopercli.build_parser(document).parse_args(["project-skills", action, *flags])
    assert generated.method == "POST" and generated.path == "/api/v1/project_skills/" + action
    assert loopercli.request_body(args, generated) == body
    op = api_v1.find("POST", generated.path)
    assert op.tool is None and op.risk == risk and op.confirms is (action == "delete")
    assert op.request["additionalProperties"] is False
    assert not document["paths"]["/project_skills/" + action]["post"].get("x-looper-cli-hand")


@pytest.mark.parametrize(("action", "body"), [
    ("add", {"project_id": 1, "skill": {"name": "a", "description": "A", "content": "A"}}),
    ("get", {"project_id": "p", "skill_id": None}),
    ("get", {"project_id": "p"}),
    ("update", {"project_id": "p", "skill_id": 1, "fields": {"name": "a"}}),
    ("update", {"project_id": "p", "skill_id": "s", "fields": {"enabled": False}}),
    ("delete", {"project_id": None, "skill_id": "s"}),
    ("delete", {"project_id": "p", "skill_id": "s", "extra": True}),
])
def test_project_skill_requests_require_string_identifiers_and_declared_fields(action, body):
    with pytest.raises(OpError):
        api_v1.validate_arguments(api_v1.find("POST", "/api/v1/project_skills/" + action).request, body)
