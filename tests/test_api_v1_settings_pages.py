"""Settings pages are ordinary, schema-generated CLI operations."""

from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1


@pytest.mark.parametrize("family,action,options,data", [
    ("scheduler_settings", "get", [], {}),
    ("scheduler_settings", "update", ["--min-interval-seconds", "1"], {"min_interval_seconds": 1}),
    ("wait_settings", "get", [], {}),
    ("wait_settings", "update", ["--max-wait-seconds", "null"], {"max_wait_seconds": None}),
    ("knowledge_vault", "get", [], {}),
    ("knowledge_vault", "update", ["--path", "null"], {"path": None}),
])
def test_generated_cli_sends_settings_page_requests(family, action, options, data):
    args = loopercli.build_parser(api_v1.document()).parse_args([family.replace("_", "-"), action, *options])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="settings")) == 0
    path = f"/api/v1/{family}/{action}"
    assert calls == [("POST", path, data)]
    op = api_v1.find("POST", path)
    assert op.tool is None and not op.hand and op.request["additionalProperties"] is False
