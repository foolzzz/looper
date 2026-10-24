"""Global role settings have full dashboard schemas and generated CLI requests."""

from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1


@pytest.mark.parametrize("command,options,path,body", [
    (["roles", "list"], [], "roles/list", {}),
    (["roles", "get"], ["--role", "worker"], "roles/get", {"role": "worker"}),
    (["roles", "update"], ["--role", "worker", "--fields", '{"system_prompt":"Custom"}'],
     "roles/update", {"role": "worker", "fields": {"system_prompt": "Custom"}}),
    (["roles", "reset-prompt"], ["--role", "worker"], "roles/reset_prompt", {"role": "worker"}),
    (["agent-settings", "get"], [], "agent_settings/get", {}),
    (["agent-settings", "update-system"], ["--values", '{"harness_prompt":null}'],
     "agent_settings/update_system", {"values": {"harness_prompt": None}}),
    (["agent-settings", "update-role"], ["--role", "worker", "--values", '{"env":[]}'],
     "agent_settings/update_role", {"role": "worker", "values": {"env": []}}),
])
def test_generated_cli_sends_global_settings_operations(command, options, path, body, capsys):
    args = loopercli.build_parser(api_v1.document()).parse_args([*command, *options])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="test")) == 0
    assert calls == [("POST", f"/api/v1/{path}", body)]
    op = api_v1.find("POST", f"/api/v1/{path}")
    assert op.tool is None and not op.hand and op.request["additionalProperties"] is False
