"""Discovery and notification commands are generated from the same v1 document."""

import pytest

from looper import loopercli
from looper.agents import api_v1


@pytest.mark.parametrize(("group", "action", "flags", "body", "risk"), [
    ("channels", "status", ["--channel-type", "lark", "--channel-id", "channel-lark-2"],
     {"channel_type": "lark", "channel_id": "channel-lark-2"}, "read"),
    ("channels", "people", ["--channel-type", "lark_bot", "--q", "Example"],
     {"channel_type": "lark_bot", "q": "Example"}, "read"),
    ("gate-settings", "get", [], {}, "read"),
    ("gate-settings", "update", ["--dashboard-target", "null"], {"dashboard_target": None}, "high"),
])
def test_generated_discovery_and_gate_commands(group, action, flags, body, risk):
    document = api_v1.document()
    generated = next(op for head, tail, _, _, op in loopercli.generated_operations(document)
                     if (head, tail) == (group, action))
    args = loopercli.build_parser(document).parse_args([group, action, *flags])
    assert loopercli.request_body(args, generated) == body
    path = "/api/v1/" + group.replace("-", "_") + "/" + action
    assert (generated.method, generated.path) == ("POST", path)
    operation = api_v1.find("POST", path)
    assert operation.risk == risk and operation.confirms is (risk == "high")
    assert operation.tool is None and not operation.hand and not operation.operator_only
    assert operation.request["additionalProperties"] is False
