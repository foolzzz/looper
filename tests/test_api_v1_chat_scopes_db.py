"""Operator scope configuration, confirmation and post-commit reconciliation contracts."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from looper.agents import api_v1
from looper.agents.action_levels import ActionLevel
from looper.agents.ops import OpError
from looper.agents.ops.service import MANAGER_TOOLS
from looper.db import agent_sessions, manager_sessions
from tests.db_support import DSN
from tests.test_api_v1_chat_bindings_db import BOT, OTHER, TARGET, _api, _binding, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
LARK = "channel-lark-201"
CHAT = "oc_syntheticroom01"
OPEN_ID = "ou_syntheticbot01"
APP_ID = "cli_syntheticbot01"
ADDRESS = {"channel_id": LARK, "chat_id": CHAT}
OVERRIDE = {**ADDRESS, "principal_kind": "bot", "sender_ids": [OPEN_ID, APP_ID], "scope_id": "chat_only"}


async def _apply(call, operation, arguments):
    status, answer = await call(operation, arguments)
    if status == 202:
        status, answer = await call("confirm", {"confirmation_id": answer["confirmation_id"]})
    return status, answer


async def _lark(pool):
    await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, display_name, manager_id) "
                       "VALUES ($1, 'lark', 'Synthetic scope bot', 'manager')", LARK)
    await pool.execute("INSERT INTO manager_chat_bindings (channel_id, chat_id, manager_id) VALUES ($1, $2, $3)",
                       LARK, CHAT, TARGET)


def test_capability_sets_list_and_get(tmp_path, monkeypatch):
    async def run(pool):
        await pool.execute("INSERT INTO capability_sets (id, name, current_version, deleted_at) "
                           "VALUES ('set-deleted', 'Deleted', 1, now())")
        await pool.execute("INSERT INTO capability_set_versions (set_id, version, items) "
                           "VALUES ('set-deleted', 1, '[{\"kind\":\"chat\"}]')")
        async with _api(tmp_path, pool) as (call, _, _):
            status, answer = await call("capability_sets.list")
            assert status == 200, answer
            sets = {row["id"]: row for row in answer["result"]["sets"]}
            assert set(sets) == {"chat_only", "full", "legacy"}
            for scope, row in sets.items():
                assert row["builtin"] is True and row["current_version"] == 1 and row["name"]
                status, found = await call("capability_sets.get", {"id": scope})
                assert status == 200 and found["result"] == {"id": scope, "version": 1, "items": row["items"]}
                assert (await call("capability_sets.get", {"id": scope, "version": 1}))[1] == found
            assert sets["chat_only"]["items"] == [{"kind": "chat"}]
            for body in ({"id": "chat_only", "version": 999}, {"id": "set-unknown"}, {"id": "set-deleted"}):
                status, answer = await call("capability_sets.get", body)
                assert status == 404 and answer["error"]["code"] == "not_found", answer
    _run(run, monkeypatch)


@pytest.mark.parametrize("case", ["chat_only", "null", "full", "new_legacy", "migrated_legacy", "keep", "move", "move_explicit",
                                  "unknown", "deleted"])
def test_binding_default_scope(tmp_path, monkeypatch, case):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, _, _):
            assert (await call("manager_chat_bindings.put", _binding()))[0] == 200
            if case in {"null", "migrated_legacy", "keep", "move", "move_explicit"}:
                await pool.execute("UPDATE manager_chat_bindings SET legacy_allowed = true, default_scope = 'legacy', "
                                   "suggested_scope = 'chat_only' WHERE channel_id = $1", BOT)
            if case == "deleted":
                await pool.execute("INSERT INTO capability_sets (id, name, current_version, deleted_at) "
                                   "VALUES ('set-deleted', 'Deleted', 1, now())")
            request = _binding()
            if case not in {"keep", "move"}:
                request["default_scope"] = {"null": None, "new_legacy": "legacy", "migrated_legacy": "legacy",
                                            "move_explicit": "chat_only", "unknown": "set-unknown",
                                            "deleted": "set-deleted"}.get(case, case)
            if case in {"move", "move_explicit"}:
                request["manager_id"] = OTHER
            status, answer = await _apply(call, "manager_chat_bindings.put", request)
            if case in {"full", "new_legacy", "unknown", "deleted"}:
                assert status == 400 and answer["error"]["code"] == "invalid", answer
                if case == "full":
                    assert "full is only for the operator's own session" in answer["error"]["message"]
                if case == "new_legacy":
                    assert "legacy is kept only on bindings that existed before scopes" in answer["error"]["message"]
                assert await pool.fetchval("SELECT default_scope FROM manager_chat_bindings WHERE channel_id = $1", BOT) is None
                return
            assert status == 200, answer
            value = None if case in {"null", "move"} else "legacy" if case in {"keep", "migrated_legacy"} else "chat_only"
            binding = answer["result"]["binding"]
            assert binding["default_scope"] == value and binding["effective_default_scope"] == (value or "chat_only")
            assert await pool.fetchval("SELECT default_scope FROM manager_chat_bindings WHERE channel_id = $1", BOT) == value
            status, listed = await call("manager_chat_bindings.list", {"channel_id": BOT})
            assert status == 200 and listed["result"]["bindings"] == [binding]
            if case in {"move", "move_explicit"}:
                assert binding["legacy_allowed"] is False and binding["suggested_scope"] is None
    _run(run, monkeypatch)


def test_binding_response_fields(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as (call, _, _):
            status, answer = await call("manager_chat_bindings.put", _binding())
            assert status == 200, answer
            binding = answer["result"]["binding"]
            assert {key: binding[key] for key in ("default_scope", "effective_default_scope", "legacy_allowed", "suggested_scope")} == {
                "default_scope": None, "effective_default_scope": "chat_only", "legacy_allowed": False, "suggested_scope": None}
            schema = api_v1.find("POST", "/api/v1/manager_chat_bindings/put").result["properties"]["binding"]
            assert schema["additionalProperties"] is False
            api_v1.validate_arguments(schema, binding)
            with pytest.raises(OpError):
                api_v1.validate_arguments(schema, {**binding, "unexpected": True})
    _run(run, monkeypatch)


@pytest.mark.parametrize("case,code", [("person_two", "invalid"), ("bot_three", "invalid"), ("bad_format", "invalid"),
    ("full", "invalid"), ("legacy", "invalid"), ("unknown", "invalid"), ("deleted", "invalid"),
    ("unbound", "not_found"), ("held", "conflict"), ("bot_two", None), ("person_one", None)])
def test_override_put_validation(tmp_path, monkeypatch, case, code):
    async def run(pool):
        await _lark(pool)
        async with _api(tmp_path, pool) as (call, _, _):
            body = dict(OVERRIDE)
            if case == "person_two":
                body["principal_kind"] = "person"
            elif case == "person_one":
                body.update(principal_kind="person", sender_ids=[OPEN_ID])
            elif case == "bot_three":
                body["sender_ids"] = [OPEN_ID, APP_ID, "ou_syntheticthird01"]
            elif case == "bad_format":
                body["sender_ids"] = ["not-a-native-id"]
            elif case in {"full", "legacy", "unknown", "deleted"}:
                body["scope_id"] = "set-" + case if case in {"unknown", "deleted"} else case
                if case == "deleted":
                    await pool.execute("INSERT INTO capability_sets (id, name, current_version, deleted_at) "
                                       "VALUES ('set-deleted', 'Deleted', 1, now())")
            elif case == "unbound":
                body["chat_id"] = "oc_unboundroom01"
            elif case == "held":
                assert (await _apply(call, "chat_scope_overrides.put", OVERRIDE))[0] == 200
                body["sender_ids"] = ["ou_syntheticother01", APP_ID]
            status, answer = await _apply(call, "chat_scope_overrides.put", body)
            if code is not None:
                assert status == {"invalid": 400, "not_found": 404, "conflict": 409}[code], answer
                assert answer["error"]["code"] == code, answer
                if case == "held":
                    assert OPEN_ID in answer["error"]["message"]
                    assert await pool.fetchval("SELECT principal_id FROM binding_principal_scopes WHERE sender_id = $1", APP_ID) == OPEN_ID
                else:
                    assert await pool.fetchval("SELECT count(*) FROM binding_principal_scopes") == 0
                return
            assert status == 200, answer
            result = answer["result"]
            assert result["principal_id"] == OPEN_ID and result["principal_kind"] == body["principal_kind"]
            assert set(result["sender_ids"]) == set(body["sender_ids"]) and result["scope_id"] == "chat_only" and result["created_at"]
            status, answer = await call("chat_scope_overrides.list", ADDRESS)
            assert status == 200 and answer["result"]["overrides"] == [result]
            # Replacing this principal drops only its removed alias.
            status, answer = await _apply(call, "chat_scope_overrides.put", {**OVERRIDE, "sender_ids": [OPEN_ID]})
            assert status == 200 and answer["result"]["sender_ids"] == [OPEN_ID]
            assert await pool.fetchval("SELECT count(*) FROM binding_principal_scopes") == 1
    _run(run, monkeypatch)


def test_concurrent_overrides_cannot_take_the_same_sender_id(tmp_path, monkeypatch):
    async def run(pool):
        await _lark(pool)
        async with _api(tmp_path, pool) as (call, _, _):
            async def put(first):
                session = "scope-" + first
                status, pending = await call("chat_scope_overrides.put", {**OVERRIDE, "sender_ids": [first, APP_ID]},
                                             session=session)
                assert status == 202, pending
                return await call("confirm", {"confirmation_id": pending["confirmation_id"]}, session=session)
            results = await asyncio.gather(put(OPEN_ID), put("ou_syntheticother01"))
            assert sorted(status for status, _ in results) == [200, 409], results
            [winner] = [answer["result"] for status, answer in results if status == 200]
            [loser] = [answer["error"] for status, answer in results if status == 409]
            assert loser["code"] == "conflict" and winner["principal_id"] in loser["message"]
            status, listed = await call("chat_scope_overrides.list", ADDRESS)
            assert status == 200 and listed["result"]["overrides"] == [winner]
    _run(run, monkeypatch)


def test_override_delete_and_cascade(tmp_path, monkeypatch):
    async def run(pool):
        await _lark(pool)
        async with _api(tmp_path, pool) as (call, _, _):
            assert (await _apply(call, "chat_scope_overrides.put", OVERRIDE))[0] == 200
            for expected in (True, False):
                status, answer = await _apply(call, "chat_scope_overrides.delete", {**ADDRESS, "principal_id": OPEN_ID})
                assert status == 200 and answer["result"] == {"deleted": expected}
                assert await pool.fetchval("SELECT count(*) FROM binding_principal_scopes") == 0
            assert (await _apply(call, "chat_scope_overrides.put", OVERRIDE))[0] == 200
            assert await pool.fetchval("SELECT count(*) FROM binding_principal_scopes") == 2
            assert (await call("manager_chat_bindings.delete", ADDRESS))[0] == 200
            assert await pool.fetchval("SELECT count(*) FROM binding_principal_scopes") == 0
    _run(run, monkeypatch)


@pytest.mark.parametrize("stop_now", [False, True])
@pytest.mark.parametrize("running", [False, True])
def test_writes_reconcile_the_running_manager(tmp_path, monkeypatch, stop_now, running):
    async def run(pool):
        await _lark(pool)
        calls = []
        async def changed(channel_id, chat_id, *, stop_now):
            # Collect committed readback; never assert in a callback the service can catch.
            rows = await pool.fetchval("SELECT count(*) FROM binding_principal_scopes")
            scope = await pool.fetchval("SELECT default_scope FROM manager_chat_bindings WHERE channel_id = $1", LARK)
            calls.append((channel_id, chat_id, stop_now, rows, scope))
        agent = SimpleNamespace(agent_id=TARGET, scope_config_changed=changed)
        async with _api(tmp_path, pool, managers={TARGET: agent} if running else {}) as (call, _, _):
            for operation, body in (("manager_chat_bindings.put", {**ADDRESS, "manager_id": TARGET, "default_scope": "chat_only"}),
                                    ("chat_scope_overrides.put", OVERRIDE),
                                    ("chat_scope_overrides.delete", {**ADDRESS, "principal_id": OPEN_ID})):
                status, answer = await _apply(call, operation, {**body, "stop_now": stop_now})
                assert status == 200, answer
            assert calls == ([(LARK, CHAT, stop_now, count, "chat_only") for count in (0, 2, 0)] if running else [])
            assert await pool.fetchval("SELECT default_scope FROM manager_chat_bindings WHERE channel_id = $1", LARK) == "chat_only"
    _run(run, monkeypatch)


def test_reconcile_failure_does_not_roll_back_committed_write(tmp_path, monkeypatch, caplog):
    async def run(pool):
        await _lark(pool)
        async def failed(*args, **kwargs):
            raise RuntimeError("synthetic reconcile failure")
        agent = SimpleNamespace(agent_id=TARGET, scope_config_changed=failed)
        async with _api(tmp_path, pool, managers={TARGET: agent}) as (call, _, _):
            status, answer = await _apply(call, "chat_scope_overrides.put", OVERRIDE)
            assert status == 200, answer
            assert await pool.fetchval("SELECT count(*) FROM binding_principal_scopes") == 2
        assert "synthetic reconcile failure" in caplog.text
    _run(run, monkeypatch)


def test_operator_only_and_confirm(tmp_path, monkeypatch):
    async def run(pool):
        await _lark(pool)
        await pool.execute("UPDATE manager_chat_bindings SET legacy_allowed = true, default_scope = 'legacy' WHERE channel_id = $1", LARK)
        operations = [("capability_sets.list", {}), ("capability_sets.get", {"id": "chat_only"}),
                      ("manager_chat_bindings.put", {**ADDRESS, "manager_id": TARGET, "default_scope": "chat_only"}),
                      ("chat_scope_overrides.list", ADDRESS), ("chat_scope_overrides.put", OVERRIDE),
                      ("chat_scope_overrides.delete", {**ADDRESS, "principal_id": OPEN_ID})]
        async with _api(tmp_path, pool) as (call, _, _):
            for name, body in operations:
                operation = api_v1.find("POST", "/api/v1/" + name.replace(".", "/"))
                assert operation.operator_only and operation.tool is None
                assert operation.description.startswith(operation.summary) and len(operation.summary) <= 120
                assert operation.level is (ActionLevel.EXTERNAL if name == "manager_chat_bindings.put" else ActionLevel.INSIDE)
                status, answer = await call(name, body, client=True)
                assert status == 403 and answer["error"]["code"] == "forbidden", answer
                if name.endswith((".put", ".delete")):
                    assert operation.risk == "high" and operation.confirms
                    status, pending = await call(name, body)
                    assert status == 202 and pending["status"] == "needs_confirmation", pending
                    assert CHAT in pending["impact"]
                    assert "everyone admitted could run commands with the operator's system account" in pending["impact"]
                    assert "prose capability rules" in pending["impact"]
            assert await pool.fetchval("SELECT default_scope FROM manager_chat_bindings WHERE channel_id = $1", LARK) == "legacy"
            assert await pool.fetchval("SELECT count(*) FROM binding_principal_scopes") == 0
            assert all(not tool.name.startswith(("capability_sets", "chat_scope_overrides")) for tool in MANAGER_TOOLS)
    _run(run, monkeypatch)


def test_manager_sessions_list_scenario(tmp_path, monkeypatch):
    async def run(pool):
        key = f"group:{LARK}:{CHAT}~syntheticfingerprint"
        await manager_sessions.save({"manager_id": TARGET, "session_key": key, "kind": "group", "channel_id": LARK,
            "chat": CHAT, "session_id": None, "session_totals": {"cost_usd": 0.25}, "runtime_type": "claude-code",
            "profile_id": None, "provider_id": None, "clean_start": False, "pending_handoff": None,
            "scope_id": "chat_only", "scope_version": 1, "audience": "shared", "scope_fingerprint": "syntheticfingerprint"})
        await pool.execute("UPDATE manager_sessions SET retired_at = now() WHERE manager_id = $1", TARGET)
        await agent_sessions.save_session(TARGET, "scenario-main", {"cost_usd": 0.5}, runtime_type="claude-code")
        agents = {name: SimpleNamespace(agent_id=name, _sessions={}) for name in (TARGET, "manager")}
        async with _api(tmp_path, pool, managers=agents) as (call, _, _):
            status, answer = await call("manager_sessions.list", {"manager_id": TARGET})
            assert status == 200, answer
            rows = {row["key"]: row for row in answer["result"]["sessions"]}
            assert set(rows) == {"main", key}
            assert rows["main"]["scope"] is None and rows["main"]["retired"] is False and rows["main"]["cost_usd"] == 0.5
            assert rows[key]["scope"] == {"id": "chat_only", "version": 1, "audience": "shared"}
            assert rows[key]["retired"] is True and rows[key]["cost_usd"] == 0.25
            status, answer = await call("manager_sessions.list")
            assert status == 200, answer
            assert len(answer["result"]["sessions"]) == 1
            assert answer["result"]["sessions"][0]["scope"] is None and answer["result"]["sessions"][0]["retired"] is False
            status, answer = await call("manager_sessions.reset", {"manager_id": TARGET, "key": "main"})
            assert status == 400 and answer["error"]["code"] == "invalid"
    _run(run, monkeypatch)
