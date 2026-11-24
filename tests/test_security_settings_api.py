"""Operator security controls keep authentication, confirmation and failed reads distinct."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1, outbound_hook, untrusted
from looper import loopercli
from looper.agents.action_levels import ActionLevel
from looper.agents.ops import ErrorCode, OpError
from looper.agents.ops.clients import ClientOps
from looper.db import manager_actions, outbound_reviews, system_settings
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_operator_auth import TOKEN


def operation(name):
    found = api_v1.find("POST", "/api/v1/" + name.replace(".", "/"))
    assert found is not None
    return found


def test_security_operations_are_operator_only_inside_and_closed(tmp_path, monkeypatch):
    names = ("untrusted_fence.get", "untrusted_fence.update", "outbound_review.list")
    for name in names:
        op = operation(name)
        assert op.operator_only and op.cli and op.tool is None
        assert op.level is ActionLevel.INSIDE
        assert op.request["additionalProperties"] is False and op.result["additionalProperties"] is False
        assert op.risk == ("high" if name.endswith("update") else "read")
        assert op.confirms == name.endswith("update")
        family, action = name.split(".")
        options = ["--enabled"] if action == "update" else []
        args = loopercli.build_parser(api_v1.document()).parse_args([family.replace("_", "-"), action, *options])
        calls = []
        client = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
        assert args._run(args, SimpleNamespace(api=lambda: client, as_json=True, session="security", operator=True)) == 0
        assert calls == [("POST", op.path, {"enabled": True} if action == "update" else {})]
    writes = AsyncMock(side_effect=AssertionError("invalid request wrote settings"))
    monkeypatch.setattr(system_settings, "put_settings", writes)
    bad = [("untrusted_fence.update", body) for body in ({}, {"enabled": None}, {"enabled": "true"},
            {"enabled": 1}, {"enabled": True, "extra": 1})]
    bad += [("outbound_review.list", body) for body in ({"limit": 0}, {"limit": 101}, {"limit": True},
            {"task_id": ""}, {"task_id": "x" * 201}, {"cursor": "x" * 2049}, {"extra": 1},
            {"verdict": "pending"}, {"cursor": "not-json"})]

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for name, body in bad:
                status, _, answer = await _send(server, "POST " + operation(name).path, _bearer(TOKEN),
                                                json.dumps(body))
                assert status == 400 and answer["error"]["code"] == "invalid", answer
            writes.assert_not_awaited()
    asyncio.run(check())


@pytest.mark.usefixtures("clients")
def test_nonoperator_security_reads_and_writes_are_forbidden(tmp_path, monkeypatch):
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(return_value={}))
    monkeypatch.setattr(outbound_hook, "settings", AsyncMock(return_value={"enabled": False,
                                                                        "managers_without_hook": []}))
    monkeypatch.setattr(outbound_reviews, "list_reviews", AsyncMock(return_value=[]))
    writes = AsyncMock(side_effect=AssertionError("forbidden write reached persistence"))
    monkeypatch.setattr(system_settings, "put_settings", writes)
    monkeypatch.setattr(outbound_hook, "set_enabled", writes)

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, who = await _send(server, "GET /api/v1/whoami", _bearer(CLIENT_TOKEN))
            assert status == 200 and who["result"]["principal"] == "client"
            for name in ("outbound_review.get", "outbound_review.update", "outbound_review.list",
                         "untrusted_fence.get", "untrusted_fence.update"):
                op = operation(name)
                args = {"enabled": True} if name.endswith("update") else {}
                for headers, expected in ((_bearer(CLIENT_TOKEN), 403), (_bearer("private-agent-token"), 401),
                                          ({}, 401)):
                    status, _, answer = await _send(server, "POST " + op.path, headers, json.dumps(args))
                    assert status == expected, answer
                for method in ("run", "impact"):
                    with pytest.raises(OpError) as error:
                        await getattr(api_v1.domain_for(op), method)(server, op, args,
                                                                    api_v1.Principal("client"), {})
                    assert error.value.code is ErrorCode.FORBIDDEN
                if not name.endswith("update"):
                    assert (await _send(server, "POST " + op.path, _bearer(TOKEN), "{}"))[0] == 200
            writes.assert_not_awaited()
    asyncio.run(check())


def test_fence_default_save_and_failure_are_not_conflated(tmp_path, monkeypatch):
    stored, writes = {}, []

    async def read(keys):
        assert keys == [untrusted.ENABLED_KEY]
        return {key: stored[key] for key in keys if key in stored}

    async def write(values):
        writes.append(values)
        stored.update(values)

    monkeypatch.setattr(system_settings, "get_settings", read)
    monkeypatch.setattr(system_settings, "put_settings", write)
    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check():
        get, update = operation("untrusted_fence.get"), operation("untrusted_fence.update")
        domain = api_v1.domain_for(get)
        assert await domain.run(None, get, {}, api_v1.OPERATOR, {}) == {"enabled": False}
        for enabled in (True, False):
            impact = await domain.impact(None, update, {"enabled": enabled}, api_v1.OPERATOR, {})
            assert "external content" in impact and "review" in impact
            assert await domain.run(None, update, {"enabled": enabled}, api_v1.OPERATOR, {}) == {
                "enabled": enabled}
        assert writes == [{untrusted.ENABLED_KEY: "true"}, {untrusted.ENABLED_KEY: "false"}]
        assert outbound_hook.ENABLED_KEY not in stored
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for failed, op, body in (("get_settings", get, {}), ("put_settings", update, {"enabled": True})):
                with monkeypatch.context() as patch:
                    patch.setattr(system_settings, failed, AsyncMock(side_effect=ConnectionError("offline")))
                    status, _, answer = await _send(server, "POST " + op.path, _bearer(TOKEN), json.dumps(body))
                    assert status == 503 and answer["error"]["code"] == "unavailable"
            assert stored == {untrusted.ENABLED_KEY: "false"}
    asyncio.run(check())


def test_review_switch_rechecks_runtime_and_preserves_confirmation(tmp_path, monkeypatch):
    unavailable = []
    saved = False

    async def settings():
        return {"enabled": saved, "managers_without_hook": list(unavailable)}

    async def save(enabled):
        nonlocal saved
        if enabled and unavailable:
            raise outbound_hook.HookUnavailable("Synthetic Manager (unsupported) has no tool hook")
        saved = enabled
        return await settings()

    monkeypatch.setattr(outbound_hook, "settings", settings)
    monkeypatch.setattr(outbound_hook, "set_enabled", save)
    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))
    restart = AsyncMock()

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            server._managers = SimpleNamespace(require_tool_hooks=restart)
            # Exercise the generic pending-confirmation path as well as the normal operator call below.
            async def confirmed_call(principal, session, name, data, **kwargs):
                return await server._client_ops.call(principal, session, name, data,
                                                     require_confirmation=True, **kwargs)

            monkeypatch.setattr(server, "_call_v1", confirmed_call)
            headers = {**_bearer(TOKEN), "X-Looper-Session": "security"}
            assert (await _send(server, "POST /api/v1/outbound_review/get", headers, "{}"))[2]["result"] == {
                "enabled": False, "managers_without_hook": []}
            # Operator high-risk calls with a session preserve the server confirmation handshake.
            status, _, pending = await _send(server, "POST /api/v1/outbound_review/update", headers,
                                             '{"enabled":true}')
            assert status == 202 and pending["status"] == "needs_confirmation" and not saved
            unavailable.append({"manager_id": "manager-synthetic", "name": "Synthetic Manager",
                                "runtime": "unsupported"})
            status, _, answer = await _send(server, "POST /api/v1/confirm", headers,
                                            json.dumps({"confirmation_id": pending["confirmation_id"]}))
            assert status == 409 and "unsupported" in answer["error"]["message"] and not saved
            restart.assert_not_awaited()
            # Direct execution rechecks compatibility too; a previously displayed list grants nothing.
            op = operation("outbound_review.update")
            with pytest.raises(OpError) as error:
                await api_v1.domain_for(op).run(server, op, {"enabled": True}, api_v1.OPERATOR, {})
            assert error.value.code is ErrorCode.CONFLICT and not saved
            unavailable.clear()
            status, _, pending = await _send(server, "POST /api/v1/outbound_review/update", headers,
                                             '{"enabled":true}')
            assert status == 202 and not saved
            status, _, answer = await _send(server, "POST /api/v1/confirm", headers,
                                            json.dumps({"confirmation_id": pending["confirmation_id"]}))
            assert status == 200 and answer["result"]["enabled"] is True
            restart.assert_awaited_once()
    asyncio.run(check())
