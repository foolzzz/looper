"""Operator gateway configuration against a disposable database."""

import pytest

from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def seed(pool):
    await pool.execute("""INSERT INTO llm_providers (id,name,auth,vendor_type) VALUES ('gw-provider','Gateway','api_key','custom');
        INSERT INTO llm_provider_endpoints(provider_id,protocol,base_url) VALUES
        ('gw-provider','openai-chat','https://chat.example.invalid'),('gw-provider','anthropic-messages','https://messages.example.invalid');
        INSERT INTO llm_provider_secrets VALUES ('gw-provider','provider-secret');
        INSERT INTO llm_models(id,provider_id,protocol,name) VALUES
        ('model-gw-chat','gw-provider','openai-chat','chat-model'),
        ('model-gw-messages','gw-provider','anthropic-messages','messages-model');""")


@pytest.mark.parametrize(
    "action,body",
    [
        ("get", {}),
        ("update", {"enabled": False}),
        ("alias_set", {"alias": "chat", "model_id": "model-gw-chat"}),
        ("alias_delete", {"alias": "chat"}),
        ("key_reveal", {}),
        ("key_rotate", {}),
    ],
)
def test_gateway_operations_require_operator(monkeypatch, tmp_path, action, body):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, _ = await call("gateway." + action, body, client=True)
            assert status == 403

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize(
    "alias,model", [("bad alias", "model-gw-chat"), ("chat", "model-gw-messages"), ("chat", "missing")]
)
def test_alias_validation(monkeypatch, tmp_path, alias, model):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, _ = await call("gateway.alias_set", {"alias": alias, "model_id": model})
            assert status == 400

    run_db(check, monkeypatch, setup=seed, timeout=20)


def test_alias_database_and_key_round_trip(monkeypatch, tmp_path):
    from looper.db import gateway

    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("gateway.alias_set", {"alias": "chat", "model_id": "model-gw-chat"})
            assert status == 200, answer
            assert answer["result"]["aliases"][0]["alias"] == "chat"
            target = await gateway.resolve_alias("chat", pool=pool)
            assert target["upstream_model"] == "chat-model" and target["api_key"] == "provider-secret"
            await pool.execute("UPDATE llm_providers SET enabled=false WHERE id='gw-provider'")
            assert (await gateway.resolve_alias("chat", pool=pool))["enabled"] is False
            status, answer = await call("gateway.key_rotate")
            assert status == 200, answer
            key = answer["result"]["key"]
            path = tmp_path / ".local/llmrec/gateway.key"
            assert path.read_text().strip() == key
            assert path.stat().st_mode & 0o777 == 0o600
            status, answer = await call("gateway.key_reveal")
            assert answer["result"]["key"] == key
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE tool='gateway.alias_set'") == 1
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE tool LIKE 'gateway.key_%'") == 0
            rows = await pool.fetch("SELECT row_to_json(a)::text AS value FROM manager_actions a")
            assert all(key not in row["value"] for row in rows)
            _, answer = await call("gateway.alias_delete", {"alias": "chat"})
            assert answer["result"]["aliases"] == []

    run_db(check, monkeypatch, setup=seed, timeout=20)


def test_gateway_toggle_pins_port_and_tracks_recording(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from looper.agents.llmrec.control import ProxyControl, install
    from looper.agents.llmrec.gateway import GatewayConfig
    from looper.db import system_settings

    async def check(pool):
        control = ProxyControl(tmp_path, "", enabled=False)
        monkeypatch.setattr(control, "_start", AsyncMock(return_value=True))
        monkeypatch.setattr(control, "_state", lambda: {"port": 32123, "pid": 123})
        monkeypatch.setattr(control, "_health", AsyncMock(return_value=None))
        install(control)
        try:
            async with v1_api(tmp_path, pool) as call:
                status, answer = await call("gateway.update", {"enabled": True})
                assert status == 200, answer
                assert answer["result"]["port"] == 32123
                assert answer["result"]["url"] == "http://127.0.0.1:32123/gw"
                assert GatewayConfig(tmp_path).read() == {"format": 1, "enabled": True, "record": False}
                await control.set_enabled(True)
                assert GatewayConfig(tmp_path).read()["record"] is True
                assert (await system_settings.get_settings(["gateway.port"], pool=pool))["gateway.port"] == "32123"
                await call("gateway.update", {"enabled": False})
                assert GatewayConfig(tmp_path).read()["enabled"] is False
        finally:
            install(None)

    run_db(check, monkeypatch, timeout=20)


def test_alias_configuration_export_round_trip(monkeypatch, tmp_path):
    from looper.db import gateway

    async def check(pool):
        await gateway.set_alias("chat", "model-gw-chat", pool=pool)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("config.export", {"include_secrets": True})
            assert status == 200, answer
            document = answer["result"]["document"]
            assert document["gateway"] == {
                "aliases": [
                    {"alias": "chat", "model": {"provider": "Gateway", "name": "chat-model", "protocol": "openai-chat"}}
                ]
            }
            assert not any(k in document["gateway"] for k in ("enabled", "port", "key"))
            await gateway.delete_alias("chat", pool=pool)
            status, answer = await call("config.import_preview", {"document": document}, client=True)
            assert status == 403, answer
            status, answer = await call("config.import_upload", document, client=True)
            assert status == 403, answer
            status, answer = await call("config.import_preview", {"document": document})
            assert status == 200, answer
            status, answer = await call("config.import_apply", {"import_id": answer["result"]["import_id"]})
            assert status == 200, answer
            assert not any(c["action"] == "skipped" for c in answer["result"]["plan"]["changes"])
            assert (await gateway.list_aliases(pool=pool))[0]["alias"] == "chat"

    run_db(check, monkeypatch, setup=seed, timeout=20)


@pytest.mark.parametrize("auth_failures,expected", [(0, "ok"), (1, "warn")])
def test_gateway_self_check_reports_authentication_failures(monkeypatch, tmp_path, auth_failures, expected):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from looper.agents import self_check
    from looper.agents.llmrec.control import install
    from looper.db import system_settings

    async def check(pool):
        await system_settings.put_settings({"gateway.enabled": "true", "gateway.port": "32123"}, pool=pool)
        install(
            SimpleNamespace(
                status=AsyncMock(
                    return_value={"proxy": {"state": "running", "port": 32123}, "auth_failures": auth_failures}
                )
            )
        )
        try:
            result = await self_check.gateway_check(runtime_root=tmp_path)
            assert result.status.lower() == expected
        finally:
            install(None)

    run_db(check, monkeypatch, timeout=20)


def test_gateway_busy_port_is_not_replaced(monkeypatch, tmp_path):
    import asyncio
    import socket
    from looper.agents.llmrec.control import ProxyControl
    from looper.agents import self_check
    from looper.db import system_settings

    async def check(pool):
        from looper.agents.llmrec.control import install

        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            control = ProxyControl(tmp_path, DSN, enabled=False)
            install(control)
            try:
                await control.set_gateway(True, port=port)
                await asyncio.sleep(0.1)
                assert (await control.status())["proxy"]["state"] == "unavailable"
                assert control.gateway_port == port
                assert (await system_settings.get_settings(["gateway.port"], pool=pool))["gateway.port"] == str(port)
                assert (await self_check.gateway_check(runtime_root=tmp_path)).status == "fail"
            finally:
                for process in list(control._children.values()):
                    if process.returncode is None:
                        process.terminate()
                    await process.wait()
                install(None)

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("active", [False, True])
def test_port_change_preserves_active_routes(monkeypatch, tmp_path, active):
    from unittest.mock import AsyncMock
    from looper.agents.llmrec import ROUTES_DIR, RouteFile
    from looper.agents.llmrec.control import ProxyControl, install
    from looper.db import system_settings

    async def check(pool):
        await system_settings.put_settings({"gateway.enabled": "true", "gateway.port": "32123"}, pool=pool)
        control = ProxyControl(tmp_path, "", enabled=False)
        control.gateway_enabled, control.gateway_port = True, 32123
        monkeypatch.setattr(control, "_state", lambda: {"port": 32123, "pid": 123})
        monkeypatch.setattr(control, "_health", AsyncMock(return_value={"pid": 123}))
        stop = AsyncMock()
        monkeypatch.setattr(control, "_stop", stop)
        monkeypatch.setattr(control, "_start", AsyncMock(return_value=True))
        if active:
            route = RouteFile(
                id="a" * 22,
                upstream="https://api.example.invalid",
                agent_id="worker",
                created_at="2026-01-01T00:00:00+00:00",
            )
            route.write(tmp_path / ROUTES_DIR / f"{route.id}.json")
        install(control)
        try:
            async with v1_api(tmp_path, pool) as call:
                status, answer = await call("gateway.update", {"enabled": True, "port": 32124})
                assert status == (400 if active else 200), answer
                assert (await system_settings.get_settings(["gateway.port"], pool=pool))["gateway.port"] == str(
                    32123 if active else 32124
                )
                assert stop.await_count == (0 if active else 1)
        finally:
            install(None)

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("change", ["disabled", "missing_key"])
def test_alias_rejects_unavailable_account(monkeypatch, tmp_path, change):
    async def check(pool):
        if change == "disabled":
            await pool.execute("UPDATE llm_providers SET enabled=false WHERE id='gw-provider'")
        else:
            await pool.execute("DELETE FROM llm_provider_secrets WHERE provider_id='gw-provider'")
        async with v1_api(tmp_path, pool) as call:
            status, _ = await call("gateway.alias_set", {"alias": "chat", "model_id": "model-gw-chat"})
            assert status == 400

    run_db(check, monkeypatch, setup=seed, timeout=20)


def test_gateway_alias_additive_upgrade_and_model_deletion(monkeypatch):
    from looper.db import gateway
    from looper.db.connection import ensure_schema

    async def check(pool):
        await pool.execute("DROP TABLE gateway_aliases; DELETE FROM schema_migrations WHERE name='gateway_aliases'")
        await ensure_schema(pool)
        assert await pool.fetchval("SELECT name FROM llm_models WHERE id='model-gw-chat'") == "chat-model"
        await gateway.set_alias("chat", "model-gw-chat", pool=pool)
        await pool.execute("DELETE FROM llm_models WHERE id='model-gw-chat'")
        assert await gateway.list_aliases(pool=pool) == []

    run_db(check, monkeypatch, setup=seed, timeout=20)


def test_configuration_alias_missing_reference_is_skipped(monkeypatch, tmp_path):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call(
                "config.import_preview",
                {
                    "document": {
                        "format": "looper-config/v4",
                        "gateway": {
                            "aliases": [
                                {
                                    "alias": "chat",
                                    "model": {"provider": "missing", "name": "missing", "protocol": "openai-chat"},
                                }
                            ]
                        },
                    }
                },
            )
            assert status == 200, answer
            changes = answer["result"]["plan"]["changes"]
            assert changes[0]["section"] == "gateway.aliases" and changes[0]["action"] == "skipped"
            assert "no valid model" in changes[0]["reason"]

    run_db(check, monkeypatch, timeout=20)
