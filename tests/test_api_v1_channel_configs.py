"""Channel bot credentials remain operator-only; ordinary operations generate CLI commands."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN


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


@pytest.mark.parametrize("case,action,status,message", [
    ("duplicate", "create", 409, "this bot is already added; update the existing bot instead"),
    ("duplicate", "update", 409, "this bot is already added; update the existing bot instead"),
    ("allowlist", "create", 400, "invalid sender allowlist: use the sender's numeric Telegram user ID, such as "
     "123456789 (a negative number is a group or channel, not a sender)"),
    ("allowlist", "update", 400, "invalid sender allowlist: use the sender's numeric Telegram user ID, such as "
     "123456789 (a negative number is a group or channel, not a sender)"),
    ("manager", "create", 404, "Manager not found"),
    ("profile", "create", 400, "invalid channel configuration; check the sender allowlist and bot settings"),
])
def test_public_channel_refusals_cross_transport(tmp_path, monkeypatch, caplog, case, action, status, message):
    from looper.agents.serve import seed_channel_configs
    from tests.db_support import run_db

    secret = "123456789:synthetic-public-error-credential"

    async def run(pool):
        async with _serving(tmp_path, listen=True) as server:
            async def call(verb, body):
                code, headers, answer = await _send(server, "POST /api/v1/channel_configs/" + verb,
                                                   _bearer(TOKEN), json.dumps(body))
                assert headers["cache-control"] == "no-store"
                assert secret not in json.dumps(answer)
                return code, answer
            body = {"channel_type": "telegram", "manager_id": "manager", "bot_token": secret}
            code, created = await call("create", body)
            assert code == 200 and created["result"]["has_bot_token"] is True
            channel_id = created["result"]["channel_id"]
            if case == "duplicate" and action == "update":
                code, other = await call("create", {**body, "bot_token": "987654321:synthetic-other"})
                assert code == 200
                channel_id = other["result"]["channel_id"]
            fields = {"bot_token": secret}
            if case == "manager":
                fields["bot_token"] = "987654321:synthetic-other"
            if case == "allowlist":
                fields["config"] = {"allowed_user_ids": [secret]}
            if case == "profile":
                fields.update(channel_type="lark", config={"profile": secret})
            request = ({**body, **fields, **({"manager_id": "manager-missing"} if case == "manager" else {})}
                       if action == "create" else {"channel_id": channel_id, "fields": fields})
            code, answer = await call(action, request)
            assert code == status and answer["error"]["message"] == message, answer
            code, answer = await call("get", {"channel_id": channel_id})
            assert code == 200 and answer["result"]["has_bot_token"] is True
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
    run_db(run, monkeypatch, setup=seed_channel_configs, timeout=30)
    assert secret not in caplog.text


@pytest.mark.parametrize("action,payload,message", [
    ("create", {"channel_type": "telegram", "manager_id": "manager"},
     "a new Telegram bot needs its token: paste it from BotFather"),
    ("create", {"channel_type": "lark_bot", "manager_id": "manager", "bot_token": "synthetic-secret"},
     "a new Lark bot needs its app_id"),
    ("create", {"channel_type": "lark_bot", "manager_id": "manager", "config": {"app_id": "cli_added"}},
     "a new Lark bot needs its app secret"),
    ("create", {"channel_type": "lark", "manager_id": "manager", "config": {"profile": ""}},
     "name the lark-cli profile of this bot: the active profile belongs to the built-in Lark channel"),
    ("update", {"channel_id": "channel-telegram", "fields": {
        "manager_id": "synthetic-secret", "display_name": "Changed"}},
     "a bot's Manager cannot change: add a new bot for the other Manager and remove this one"),
])
def test_fixed_channel_validation_reasons_are_public(tmp_path, monkeypatch, action, payload, message):
    from looper.agents.serve import seed_channel_configs
    from tests.db_support import run_db

    async def run(pool):
        before = await pool.fetch("SELECT * FROM channel_configs ORDER BY channel_id")
        async with _serving(tmp_path, listen=True) as server:
            status, headers, answer = await _send(server, "POST /api/v1/channel_configs/" + action,
                                                _bearer(TOKEN), json.dumps(payload))
            assert status == 400 and answer["error"] == {"code": "invalid", "message": message}, answer
            assert headers["cache-control"] == "no-store"
            assert "synthetic-secret" not in json.dumps(answer)
        assert await pool.fetch("SELECT * FROM channel_configs ORDER BY channel_id") == before
        assert await pool.fetchval("SELECT count(*) FROM channel_secrets") == 0
    run_db(run, monkeypatch, setup=seed_channel_configs, timeout=30)


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("failure,expected,message", [
    ("400 Bad Request", 400, "invalid channel configuration; check the sender allowlist and bot settings"),
    ("409 Conflict", 409, "this bot is already added; update the existing bot instead"),
    ("500 Internal Server Error", 503, "invalid credential request"),
    ("503 Service Unavailable", 503, "invalid credential request"),
    (None, 503, "invalid credential request"),
])
def test_channel_save_never_publishes_service_exception_text(
        tmp_path, monkeypatch, action, failure, expected, message):
    from looper.agents.channels import bot_api
    from looper.agents.projects import RequestError

    secret = "123456789:synthetic-submitted-token"
    sender = "synthetic-submitted-sender"

    async def refuse(*args, **kwargs):
        text = f"unexpected private failure: {secret} {sender}"
        raise RequestError(failure, text) if failure else RuntimeError(text)

    monkeypatch.setattr(bot_api, "add_bot" if action == "create" else "update_bot", refuse)

    async def run():
        async with _serving(tmp_path, listen=True) as server:
            request = ({"channel_type": "telegram", "manager_id": sender, "bot_token": secret}
                       if action == "create" else {"channel_id": sender, "fields": {"bot_token": secret}})
            status, _, answer = await _send(server, "POST /api/v1/channel_configs/" + action,
                                           _bearer(TOKEN), json.dumps(request))
            assert status == expected and answer["error"]["message"] == message, answer
            assert all(value not in json.dumps(answer) for value in (secret, sender, "unexpected private failure"))
    asyncio.run(run())


def test_missing_channel_update_has_the_same_public_404_with_config(tmp_path, monkeypatch):
    from looper.agents.serve import seed_channel_configs
    from tests.db_support import run_db

    async def run(pool):
        before = await pool.fetch("SELECT * FROM channel_configs ORDER BY channel_id")
        answers = []
        async with _serving(tmp_path, listen=True) as server:
            for fields in ({"enabled": False}, {"enabled": False, "config": {}}):
                status, _, answer = await _send(server, "POST /api/v1/channel_configs/update", _bearer(TOKEN),
                    json.dumps({"channel_id": "synthetic-missing-channel", "fields": fields}))
                assert status == 404 and answer["error"] == {
                    "code": "not_found", "message": "channel not found"}, answer
                answers.append(answer)
        assert answers[0] == answers[1]
        assert await pool.fetch("SELECT * FROM channel_configs ORDER BY channel_id") == before
        assert await pool.fetchval("SELECT count(*) FROM channel_secrets") == 0
    run_db(run, monkeypatch, setup=seed_channel_configs, timeout=30)
