"""Typed task interaction operations and their generated standard-library CLI."""

from looper import loopercli
from looper.agents import api_v1
import pytest


@pytest.mark.parametrize(("name", "flags", "body"), [
    ("waits.resume", ["--task-id", "task-a", "--step", "w1"], {"task_id": "task-a", "step": "w1"}),
    ("waits.cancel", ["--task-id", "task-a", "--step", "w1"], {"task_id": "task-a", "step": "w1"}),
    ("tasks.role_update", ["--task-id", "task-a", "--key", "tester", "--role-prompt", "Check it.",
                           "--harness", "null", "--profile-id", "null", "--items", '["skill-a"]'],
     {"task_id": "task-a", "key": "tester", "role_prompt": "Check it.", "harness": None,
      "profile_id": None, "items": ["skill-a"]}),
    ("tasks.room_view", ["--task-id", "task-a", "--kind", "question", "--from", "tester", "--after", "1",
                         "--limit", "2", "--unanswered"],
     {"task_id": "task-a", "kind": "question", "from": "tester", "after": 1, "limit": 2, "unanswered": True}),
])
def test_generated_cli_preserves_typed_interaction_requests(name, flags, body):
    document = api_v1.document()
    family, action = name.split(".")
    generated = next(op for head, tail, _, _, op in loopercli.generated_operations(document)
                     if head == family and tail == action.replace("_", "-"))
    args = loopercli.build_parser(document).parse_args([family, action.replace("_", "-"), *flags])
    assert generated.method == "POST" and generated.path == "/api/v1/" + name.replace(".", "/")
    assert loopercli.request_body(args, generated) == body
    operation = api_v1.find("POST", generated.path)
    assert operation.tool is None and operation.request["additionalProperties"] is False
    assert not document["paths"][generated.path.removeprefix("/api/v1")]["post"].get("x-looper-cli-hand")


