"""Project settings expose ordinary operations through the generated CLI."""

from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1


@pytest.mark.parametrize("command,options,path,body", [
    (["project-agent-settings", "get"], [], "project_agent_settings/get", {}),
    (["project-agent-settings", "update"], ["--values", '{"harness_prompt":null}'],
     "project_agent_settings/update", {"values": {"harness_prompt": None}}),
    (["project-agent-settings", "update-role"], ["--role", "worker", "--values", '{"env":[]}'],
     "project_agent_settings/update_role", {"role": "worker", "values": {"env": []}}),
    (["allowed-profiles", "get"], [], "allowed_profiles/get", {}),
    (["allowed-profiles", "replace"], ["--profile-ids", '["agent-sonnet"]'],
     "allowed_profiles/replace", {"profile_ids": ["agent-sonnet"]}),
])
def test_generated_cli_sends_project_settings_operations(command, options, path, body):
    args = loopercli.build_parser(api_v1.document()).parse_args(
        [*command, "--project-id", "proj-example", *options])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="settings")) == 0
    assert calls == [("POST", f"/api/v1/{path}", {"project_id": "proj-example", **body})]
    operation = api_v1.find("POST", f"/api/v1/{path}")
    assert operation.tool is None and not operation.hand
    assert operation.request["additionalProperties"] is False

