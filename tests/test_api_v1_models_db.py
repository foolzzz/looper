"""Authenticated model CRUD, exact identities and deletion on disposable PostgreSQL."""

import json
from contextlib import asynccontextmanager

import pytest

from looper.agents import api_v1, profile_registry
from looper.agents.bus import MessageBus
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import api_clients, profile_registry as db
from tests.db_support import DSN, run_with_models
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
CREATE = {"provider": "prov-test", "name": "example-model", "protocol": "anthropic-messages"}
SETTINGS = {"context_window": 100000, "input_usd_per_mtok": 0, "output_usd_per_mtok": 2.5,
            "cache_read_usd_per_mtok": 0.25, "cache_write_usd_per_mtok": 1.25,
            "efforts": ["low", "high"], "background_model": "helper"}


@asynccontextmanager
async def _api(tmp_path, pool):
    _, token = await api_clients.create("Model validation", pool=pool)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(Platform(bus=MessageBus(runtime_root=tmp_path),
            task_router=server._task_router, runtime_root=tmp_path))

        async def call(operation, body=None, *, client=False, session="one", method="POST"):
            if operation.startswith("/"):
                path = operation
            else:
                prefix = "/api/v1/" if operation in {"models.get", "confirm"} else "/api/v1/ops/"
                path = prefix + operation.replace(".", "/")
            status, _, answer = await _send(server, f"{method} {path}",
                {**_bearer(token if client else TOKEN), "X-Looper-Session": session}, json.dumps(body or {}))
            if status == 200 and not operation.startswith("/"):
                schema = api_v1.find(method, path).result
                api_v1.validate_arguments({"type": "object", "properties": {"result": schema}},
                                          {"result": answer["result"]})
            return status, answer

        yield call


async def _create(call, **fields):
    status, answer = await call("models.create", {**CREATE, **fields})
    assert status == 200 and answer["result"]["model"], answer
    return answer["result"]["model"]


def test_full_model_create_get_update_and_database_readback(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            created = await _create(call, **SETTINGS)
            mid = created["model_id"]
            assert all(created[key] == value for key, value in SETTINGS.items())
            assert created["endpoint"]["protocol"] == CREATE["protocol"]
            assert created["prices"]["input_usd_per_mtok"] == {"override": 0, "effective": 0, "source": "override"}
            assert created["builtin"] is False and created["profile_count"] == 0
            status, read = await call("models.get", {"model": mid}, client=True)
            assert status == 200 and read["result"] == {"model": created, "warnings": created["warnings"]}
            changes = {"context_window": 200000, "efforts": None, "background_model": None,
                       "input_usd_per_mtok": None, "output_usd_per_mtok": 3.5}
            status, updated = await call("models.update", {"model": mid, **changes}, client=True)
            assert status == 200 and updated["result"]["warnings"] == updated["result"]["model"]["warnings"]
            assert all(updated["result"]["model"][key] == value for key, value in changes.items())
            assert (await call("models.get", {"model": mid}))[1]["result"] == updated["result"]
            saved = await db.get_model(mid)
            assert all(saved[key] == value for key, value in changes.items())
    run_with_models(run, monkeypatch)


@pytest.mark.parametrize("field,value", [("provider", "prov-other"), ("name", "renamed"),
                                         ("protocol", "openai-chat"), ("runtime", "rt-claude-code")])
def test_update_rejects_identity_changes_and_preserves_settings(tmp_path, monkeypatch, field, value):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            model = await _create(call, context_window=100)
            status, answer = await call("models.update", {"model": model["model_id"], field: value, "context_window": 200})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert (await call("models.get", {"model": model["model_id"]}))[1]["result"]["model"] == model
    run_with_models(run, monkeypatch)


def test_exact_provider_name_protocol_references_select_only_the_matching_model(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            variants = [{}, {"protocol": "openai-chat"}, {"provider": "prov-other"}, {"name": "Example-model"}]
            models = [await _create(call, **fields) for fields in variants]
            assert len({m["model_id"] for m in models}) == len(variants)
            for index, (model, fields) in enumerate(zip(models, variants)):
                reference = {**CREATE, **fields}
                status, answer = await call("models.update", {"model": reference, "context_window": 100 + index})
                assert status == 200 and answer["result"]["model"]["model_id"] == model["model_id"], answer
            assert [(await db.get_model(m["model_id"]))["context_window"] for m in models] == [100, 101, 102, 103]
            status, answer = await call("models.list", {"provider": "Test account", "runtime": "claude-code"})
            assert status == 200
            assert {m["model_id"] for m in answer["result"]["models"]} == {models[0]["model_id"], models[3]["model_id"]}
    run_with_models(run, monkeypatch)


@pytest.mark.parametrize("kind,code", [("builtin", "invalid"), ("in-use", "conflict")])
def test_builtin_and_referenced_model_deletion_is_rejected(tmp_path, monkeypatch, kind, code):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            if kind == "builtin":
                status, answer = await call("models.list", {"provider": "prov-claude-subscription"})
                assert status == 200 and answer["result"]["models"]
                mid = answer["result"]["models"][0]["model_id"]
            else:
                mid = (await _create(call))["model_id"]
                await profile_registry.create_profile({"name": "Reference", "runtime_id": "rt-claude-code", "model_id": mid})
            status, answer = await call("models.delete", {"model": mid}, client=True)
            assert status == (409 if kind == "in-use" else 400) and answer["error"]["code"] == code, answer
            assert "confirmation_id" not in answer and await db.get_model(mid) is not None
    run_with_models(run, monkeypatch)


def test_client_delete_waits_for_same_session_confirmation(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            mid = (await _create(call))["model_id"]
            status, pending = await call("models.delete", {"model": mid}, client=True)
            assert status == 202 and pending["status"] == "needs_confirmation" and mid in pending["impact"]
            confirm = {"confirmation_id": pending["confirmation_id"]}
            assert await db.get_model(mid) is not None
            assert (await call("confirm", confirm, client=True, session="other"))[0] == 404
            assert await db.get_model(mid) is not None
            status, answer = await call("confirm", confirm, client=True)
            assert status == 200 and answer["result"] == {"deleted": mid}, answer
            assert await db.get_model(mid) is None
    run_with_models(run, monkeypatch)


def test_operator_delete_executes_without_transport_confirmation(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            mid = (await _create(call))["model_id"]
            status, answer = await call("models.delete", {"model": mid})
            assert status == 200 and answer["result"] == {"deleted": mid}, answer
            assert await db.get_model(mid) is None
    run_with_models(run, monkeypatch)


def test_unknown_model_returns_typed_not_found_after_successful_lookup(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            model = await _create(call)
            assert (await call("models.get", {"model": model["model_id"]}, client=True))[0] == 200
            status, answer = await call("models.get", {"model": "model-missing"}, client=True)
            assert status == 404 and answer["error"]["code"] == "not_found" and "result" not in answer, answer
    run_with_models(run, monkeypatch)


@pytest.mark.parametrize("method,action", [("GET", "list"), ("POST", "create"), ("GET", "get"),
                                           ("PUT", "update"), ("DELETE", "delete")])
def test_old_model_routes_are_404_with_matching_v1_positive_controls(tmp_path, monkeypatch, method, action):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            mid = (await _create(call))["model_id"]
            legacy = "/api/models" + ("/" + mid if action in {"get", "update", "delete"} else "")
            body = {**CREATE, "name": "another-model"} if action == "create" else {"model": mid}
            if action == "update":
                body["context_window"] = 100
            assert (await call(legacy, body, method=method))[0] == 404
            status, answer = await call("models." + action, {} if action == "list" else body)
            assert status == 200 and answer["result"], answer
    run_with_models(run, monkeypatch)
