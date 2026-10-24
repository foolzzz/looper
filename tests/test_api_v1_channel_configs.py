"""Channel bot credentials remain operator-only; ordinary operations generate CLI commands."""

import asyncio
from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("body", ["{}", "not json", "[]", '{"bot_token":"synthetic-secret"}'])
@pytest.mark.usefixtures("clients")
def test_client_credential_request_is_refused_before_body_parsing(tmp_path, monkeypatch, action, body):
    def refused(*args, **kwargs):
        pytest.fail("client credential request reached body parsing or audit")

    async def run():
        async with _serving(tmp_path) as server:
            monkeypatch.setattr(server, "_v1_body", refused)
            status, _, answer = await _send(server, f"POST /api/v1/channel_configs/{action}",
                                           _bearer(CLIENT_TOKEN), body)
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert server._client_ops is None
    asyncio.run(run())


@pytest.mark.parametrize("action,flags,body", [
    ("list", [], {}),
    ("get", ["--channel-id", "channel-telegram"], {"channel_id": "channel-telegram"}),
    ("delete", ["--channel-id", "channel-telegram-2"], {"channel_id": "channel-telegram-2"}),
])
def test_generated_channel_cli_posts_ordinary_requests(action, flags, body, capsys):
    document = api_v1.document()
    args = loopercli.build_parser(document).parse_args(["channel-configs", action, *flags])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="test")) == 0
    path = f"/api/v1/channel_configs/{action}"
    assert calls == [("POST", path, body)]
    operation = api_v1.find("POST", path)
    assert not operation.hand and not operation.operator_only
    assert operation.request["additionalProperties"] is False
    assert operation.confirms is (action == "delete")


@pytest.mark.parametrize("action", ["create", "update"])
def test_credential_schema_is_write_only_and_outside_audit(action):
    operation = api_v1.find("POST", f"/api/v1/channel_configs/{action}")
    assert operation.operator_only and operation.credential_write and not operation.hand
    schema = operation.request["properties"]
    if action == "update":
        schema = schema["fields"]["properties"]
    assert schema["bot_token"]["writeOnly"] is True
    assert "bot_token" not in operation.result["properties"]
