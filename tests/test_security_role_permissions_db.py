"""Security-role configuration stays operator-only across HTTP, adapters and staged imports."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import agent_settings, api_v1, config_imports
from looper.agents.bus import MessageBus
from looper.agents.ops import ErrorCode, OpError
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.serve import seed_role_configs
from looper.agents.v1_domains import global_settings
from looper.db import role_configs
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients as clients
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
ROLE = "outbound_check"
DOCUMENT = {"format": "looper-config/v4"}
WRITES = (
    ("roles.update", {"role": ROLE, "fields": {"system_prompt": "Review the action", "timeout_seconds": 30}}),
    ("roles.reset_prompt", {"role": ROLE}),
    ("agent_settings.update_role", {"role": ROLE, "values": {"profile_id": "agent-claude-code-opus"}}),
)


@asynccontextmanager
async def serving(tmp_path):
    async with _serving(tmp_path, listen=True) as server:
        bus = MessageBus(runtime_root=tmp_path)
        server._client_ops = ClientOps(Platform(bus=bus, task_router=server._task_router, runtime_root=tmp_path))
        try:
            yield server
        finally:
            await bus.shutdown()


async def post(server, operation, data, token=CLIENT_TOKEN):
    headers = {**(_bearer(token) if token else {}), "X-Looper-Session": "security-role"}
    if operation == "config.import_upload":
        headers["Content-Type"] = "application/json"
    status, _, body = await _send(server, "POST /api/v1/" + operation.replace(".", "/"), headers, json.dumps(data))
    return status, body


async def snapshot(pool):
    return ([dict(row) for row in await pool.fetch("SELECT * FROM role_configs ORDER BY role")],
            [dict(row) for row in await pool.fetch("SELECT * FROM agent_setting_layers ORDER BY scope, role, project_id")],
            [dict(row) for row in await pool.fetch("SELECT * FROM system_settings ORDER BY key")])


def test_outbound_role_writes_require_operator_on_every_api_path(tmp_path, monkeypatch, clients):
    async def run(pool):
        await seed_role_configs(pool)
        async with serving(tmp_path) as server:
            status, _, body = await _send(server, "GET /api/v1/whoami", _bearer(CLIENT_TOKEN))
            assert status == 200 and body["result"]["principal"] == "client"
            before = await snapshot(pool)
            for operation, arguments in WRITES:
                for token, expected in ((CLIENT_TOKEN, 403), ("private-agent-token", 401), (None, 401)):
                    status, body = await post(server, operation, arguments, token)
                    assert status == expected, body
                    assert "confirmation_id" not in body
                    assert await snapshot(pool) == before
                op = api_v1.find("POST", "/api/v1/" + operation.replace(".", "/"))
                for principal in (api_v1.Principal("client", "test-client"), SimpleNamespace(kind="agent")):
                    for callback in (global_settings.impact, global_settings.run):
                        with pytest.raises(OpError) as error:
                            await callback(server, op, arguments, principal, {})
                        assert error.value.code is ErrorCode.FORBIDDEN
                assert not server._client_ops._pending
            for operation, arguments in (WRITES[0], WRITES[2]):
                status, body = await post(server, operation, arguments, TOKEN)
                assert status == 200, body
            config = await role_configs.get_config(ROLE)
            assert config["system_prompt"] == "Review the action" and config["timeout_seconds"] == 30
            assert (await agent_settings.settings_view())["roles"][ROLE]["values"]["profile_id"] == "agent-claude-code-opus"
            # Remove no-tools from the selected runtime: the existing capability validator still rejects it.
            await pool.execute("UPDATE agent_runtimes SET capabilities=array_remove(capabilities, 'no_tools') "
                               "WHERE id='rt-claude-code'")
            status, body = await post(server, WRITES[2][0], WRITES[2][1], TOKEN)
            assert status == 400, body
            status, body = await post(server, "roles.update", {"role": "worker", "fields": {"timeout_seconds": 42}})
            assert status == 200 and body["result"]["timeout_seconds"] == 42
    run_db(run, monkeypatch, timeout=40)


def test_outbound_role_import_rechecks_staged_document(tmp_path, monkeypatch, clients):
    async def run(pool):
        await seed_role_configs(pool)
        async with serving(tmp_path) as server:
            before = await snapshot(pool)
            role = {"role": ROLE, "prompt": "Imported review", "timeout_seconds": 20, "enabled": True}
            layer = {"scope": "role", "role": ROLE, "env": [{"key": "REVIEW_MODE", "value": "strict"}]}
            for section, entry in (("roles", role), ("layers", layer)):
                blocked = {**DOCUMENT, "settings": {section: [entry]}}
                for operation, data in (("config.import_upload", blocked), ("config.import_preview", {"document": blocked})):
                    status, body = await post(server, operation, data)
                    assert status == 403, body
                    assert await snapshot(pool) == before
                status, body = await post(server, "config.import_upload", DOCUMENT)
                assert status == 200, body
                import_id = body["result"]["import_id"]
                staged = tmp_path / config_imports.DIRNAME / f"{import_id}.json"
                staged.write_text(json.dumps(blocked))
                status, body = await post(server, "config.import_apply", {"import_id": import_id})
                assert status == 403 and "confirmation_id" not in body
                assert await snapshot(pool) == before
                # A previously valid confirmation must re-read the file before any section is written.
                staged.write_text(json.dumps(DOCUMENT))
                status, body = await post(server, "config.import_apply", {"import_id": import_id})
                assert status == 202, body
                confirmation = body["confirmation_id"]
                staged.write_text(json.dumps({**blocked, "settings": {**blocked["settings"], "roles": [
                    {"role": "worker", "prompt": "Must not apply", "timeout_seconds": 5, "enabled": True},
                    *([role] if section == "roles" else [])]}}))
                status, body = await post(server, "confirm", {"confirmation_id": confirmation})
                assert status == 403, body
                assert await snapshot(pool) == before
                assert (await post(server, "config.import_cancel", {"import_id": import_id}))[0] == 200
            legitimate = {**DOCUMENT, "settings": {"roles": [role], "layers": [layer]}}
            status, body = await post(server, "config.import_upload", legitimate, TOKEN)
            assert status == 200, body
            status, body = await post(server, "config.import_apply", {"import_id": body["result"]["import_id"]}, TOKEN)
            assert status == 200, body
            assert (await role_configs.get_config(ROLE))["system_prompt"] == "Imported review"
            assert (await agent_settings.settings_view())["roles"][ROLE]["values"]["env"] == layer["env"]
            saved = await snapshot(pool)
            for settings in ({"system": {"outbound_review.enabled": True, "untrusted_fence.enabled": True}},
                             {"untrusted_fence": {"enabled": True}, "outbound_review": {"enabled": True}}):
                status, body = await post(server, "config.import_upload", {**DOCUMENT, "settings": settings}, TOKEN)
                assert status == 200, body
                import_id = body["result"]["import_id"]
                status, body = await post(server, "config.import_apply", {"import_id": import_id}, TOKEN)
                assert status == 200, body
                assert await snapshot(pool) == saved
            status, body = await post(server, "config.import_upload", {**DOCUMENT, "untrusted_fence": True}, TOKEN)
            assert status == 400, body
            assert await snapshot(pool) == saved
    run_db(run, monkeypatch, timeout=40)


def test_manager_and_cli_cannot_bypass_security_role_permissions(tmp_path, monkeypatch, clients, capsys):
    from looper.agents.ops.settings import TOOLS
    from looper.agents.ops import check_arguments

    settings = next(tool for tool in TOOLS if tool.name == "looper_settings")
    for data in ({"action": "set", "role": ROLE, "values": {"timeout_seconds": 1}},
                 {"action": "outbound_review", "enabled": True},
                 {"action": "untrusted_fence", "enabled": True}):
        with pytest.raises(OpError):
            check_arguments(settings.schema, data)
    requests = []
    args = loopercli.build_parser(api_v1.document()).parse_args([
        "roles", "update", "--role", ROLE, "--fields", '{"timeout_seconds":30}'])
    transport = SimpleNamespace(call=lambda *parts: requests.append(parts) or (403, {
        "status": "error", "error": {"code": "forbidden", "message": "operator identity is required"}}))
    assert args._run(args, SimpleNamespace(api=lambda: transport, as_json=True, session="security-role")) != 0
    assert requests == [("POST", "/api/v1/roles/update", {"role": ROLE, "fields": {"timeout_seconds": 30}})]

    async def run(pool):
        await seed_role_configs(pool)
        async with serving(tmp_path) as server:
            before = await snapshot(pool)
            operation, arguments = WRITES[0]
            op = api_v1.find("POST", "/api/v1/" + operation.replace(".", "/"))
            # Even a same-session pending write created before the guard cannot elevate a client.
            from looper.agents.ops import Tool, Risk
            tool = Tool(name=operation, description="Role change", schema=op.request, risk=Risk.HIGH,
                        run=lambda call: global_settings.run(server, op, call.arguments,
                            api_v1.Principal("client", "client-0123456789ab"), {}))
            pending = server._client_ops._ask(api_v1.Principal("client", "client-0123456789ab"),
                "security-role", tool, arguments, "Change role", confirmation={})
            status, body = await post(server, "confirm", {"confirmation_id": pending.id})
            assert status == 403 and await snapshot(pool) == before
            assert (await post(server, operation, arguments))[0] == 403
            assert (await post(server, operation, arguments, TOKEN))[0] == 200
    run_db(run, monkeypatch, timeout=40)
