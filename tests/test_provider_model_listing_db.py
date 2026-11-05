"""Stored-key model discovery and atomic checklist writes on disposable PostgreSQL."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from looper.agents.runtimes.pricing import BUILTIN_PRICES, PRICE_FIELDS
from looper.db import profile_registry as db
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
BUILTIN_NAME = next(iter(BUILTIN_PRICES))
UNKNOWN_NAME = "gateway-model"


@pytest.fixture
def model_endpoint():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status = 200 if self.headers.get("Authorization") == "Bearer test-key" else 401
            body = json.dumps({"data": [{"id": BUILTIN_NAME},
                {"id": UNKNOWN_NAME, "context_length": 64000}, {"id": "vendor@model"}]}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


async def _provider(pool, *, base_url="https://gateway.example.invalid/v1"):
    # Seed independently of account writes and avoid probing installed login CLIs.
    await pool.execute("INSERT INTO llm_providers (id, name, vendor_type, auth) "
                       "VALUES ('prov-listing', 'Listing account', 'custom', 'api_key')")
    await pool.executemany("INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) "
                          "VALUES ('prov-listing', $1, $2)",
                          [(protocol, base_url) for protocol in ("openai-responses", "openai-chat")])
    await pool.execute("INSERT INTO llm_provider_secrets (provider_id, api_key) VALUES ('prov-listing', 'test-key')")
    return "prov-listing"


def test_fetch_uses_stored_key_groups_protocols_and_reports_registration(tmp_path, monkeypatch, model_endpoint, caplog):
    url, requests = model_endpoint

    async def run(pool):
        pid = await _provider(pool, base_url=url)
        await pool.execute("INSERT INTO llm_models (id, provider_id, name, protocol) "
                           "VALUES ('model-existing', $1, $2, 'openai-chat')", pid, UNKNOWN_NAME)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("provider_accounts.fetch_models", {"provider_id": pid})
            assert status == 200, answer
            listings = answer["result"]["listings"]
            assert len(listings) == 1 and requests == ["/v1/models"]
            listing = listings[0]
            assert listing["family"] == "openai" and listing["base_url"] == url
            assert listing["protocols"] == ["openai-responses", "openai-chat"]
            assert listing["state"] == "listed" and listing["problem"] is None and not listing["truncated"]
            models = {m["name"]: m for m in listing["models"]}
            assert models[UNKNOWN_NAME]["registered"] == ["openai-chat"]
            assert models[UNKNOWN_NAME]["context_window"] == 64000
            assert not models[UNKNOWN_NAME]["builtin_price"] and models[UNKNOWN_NAME]["addable"]
            assert models[BUILTIN_NAME]["builtin_price"] and models[BUILTIN_NAME]["registered"] == []
            assert not models["vendor@model"]["addable"] and models["vendor@model"]["problem"]
            assert "test-key" not in json.dumps(answer)
            before = await db.list_models(provider_id=pid)
            await pool.execute("UPDATE llm_provider_secrets SET api_key = 'wrong-key' WHERE provider_id = $1", pid)
            status, failed = await call("provider_accounts.fetch_models", {"provider_id": pid})
            assert status == 200, failed
            assert failed["result"]["listings"][0]["state"] == "failed"
            assert "HTTP 401" in failed["result"]["listings"][0]["problem"]
            assert await db.list_models(provider_id=pid) == before
            assert "wrong-key" not in json.dumps(failed)
    run_db(run, monkeypatch, timeout=30)
    assert "test-key" not in caplog.text and "wrong-key" not in caplog.text


def test_login_fetch_is_one_skipped_listing(tmp_path, monkeypatch):
    async def run(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("provider_accounts.fetch_models", {"provider_id": "prov-codex-login"})
            assert status == 200, answer
            assert answer["result"]["listings"] == [{"family": "openai", "base_url": "",
                "protocols": ["openai-responses"], "state": "skipped",
                "problem": "a login provider's models come with Looper; fetching is for API-key providers",
                "truncated": False, "models": []}]
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("operation", ["create", "update", "legacy"])
def test_anthropic_saved_url_is_used_by_runs_and_listing(tmp_path, monkeypatch, operation):
    from looper.agents.agent_settings import Launch, load_agent_settings
    from looper.agents import model_listing, profile_registry
    from looper.db import agent_setting_layers

    root = "https://api.example.com"
    url = root + "/v1/"
    endpoints = [{"protocol": protocol, "base_url": url}
                 for protocol in ("anthropic-messages", "openai-chat")]

    async def run(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("provider_accounts.create", {"name": "Saved account",
                "vendor_type": "custom", "api_key": "test-key",
                "endpoints": [{**e, "base_url": root} for e in endpoints] if operation == "update" else endpoints})
            assert status == 200, answer
            pid = answer["result"]["provider"]["provider_id"]
            if operation == "update":
                status, answer = await call("provider_accounts.update", {
                    "provider_id": pid, "fields": {"endpoints": endpoints}})
                assert status == 200, answer
            expected = url
            if operation == "legacy":
                expected = root + "/v1"
                await pool.execute("UPDATE llm_provider_endpoints SET base_url = $1 "
                                   "WHERE provider_id = $2 AND protocol = 'anthropic-messages'", expected, pid)
                status, answer = await call("provider_accounts.update", {
                    "provider_id": pid, "fields": {"name": "Renamed legacy account"}})
                assert status == 200, answer
            stored = await db.get_provider(pid)
            assert {e["protocol"]: e["base_url"] for e in stored["endpoints"]} == {
                "anthropic-messages": expected, "openai-chat": url}
            model = (await profile_registry.create_model({"provider_id": pid, "name": "test-model",
                "protocol": "anthropic-messages"}))["model"]
            profile = (await profile_registry.create_profile({"name": "Saved profile",
                "runtime_id": "rt-claude-code", "model_id": model["model_id"]}))["profile"]
            await agent_setting_layers.put_layer("role", "worker", None, {"profile_id": profile["profile_id"]})
            assert Launch(await load_agent_settings("worker")).environment()["ANTHROPIC_BASE_URL"] == expected
            requests = []

            def answer(request):
                requests.append(("anthropic" if "x-api-key" in request.headers else "openai", request.url.path))
                return httpx.Response(200, json={"data": []})

            async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
                result = await model_listing.fetch_provider_models(pid, client=client)
            assert all(entry["state"] == "listed" for entry in result["listings"])
            assert sorted(requests) == [("anthropic", "/v1/v1/models"),
                                        ("openai", "/v1/models")]
    run_db(run, monkeypatch, timeout=30)


def test_api_preserves_anthropic_gateway_path_for_listing(tmp_path, monkeypatch):
    from looper.agents import model_listing

    url = "https://host/gateway/v1"

    async def run(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("provider_accounts.create", {"name": "Gateway account",
                "vendor_type": "custom", "endpoints": [{"protocol": "anthropic-messages", "base_url": url}]})
            assert status == 200, answer
            provider = answer["result"]["provider"]
            assert provider["endpoints"][0]["base_url"] == url
            pid = provider["provider_id"]
            assert (await db.get_provider(pid))["endpoints"][0]["base_url"] == url
            requests = []

            def answer_listing(request):
                requests.append(request.url.path)
                return httpx.Response(200, json={"data": []})

            async with httpx.AsyncClient(transport=httpx.MockTransport(answer_listing)) as client:
                result = await model_listing.fetch_provider_models(pid, client=client)
            assert result["listings"][0]["state"] == "listed"
            assert requests == ["/gateway/v1/v1/models"]

    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("name,fields,expected_source", [
    (UNKNOWN_NAME, {}, "override"),
    (BUILTIN_NAME, {}, "builtin"),
    (UNKNOWN_NAME, {"input_usd_per_mtok": 1.75, "cache_write_usd_per_mtok": None}, "override"),
])
def test_add_many_defaults_only_omitted_prices(tmp_path, monkeypatch, name, fields, expected_source):
    async def run(pool):
        pid = await _provider(pool)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("models.add_many", {"provider_id": pid,
                "models": [{"name": name, "protocol": "openai-chat", **fields}]})
            assert status == 200, answer
            result = answer["result"]
            assert result["skipped"] == [] and len(result["models"]) == 1
            model = result["models"][0]
            stored = await db.get_model(model["model_id"])
            for field in PRICE_FIELDS:
                expected = fields.get(field, None if expected_source == "builtin" else 0.0)
                assert stored[field] == expected and model[field] == expected
                price = model["prices"][field]
                assert price["override"] == expected
                assert price["source"] == ("unknown" if field in fields and expected is None else expected_source)
                assert price["effective"] == (getattr(BUILTIN_PRICES[name], field)
                    if expected_source == "builtin" else expected)
            assert bool(model["warnings"]) == (None in fields.values())
            assert result["warnings"] == model["warnings"]
    run_db(run, monkeypatch, timeout=30)


def test_duplicate_checklists_skip_in_request_and_between_dashboards(tmp_path, monkeypatch):
    async def run(pool):
        pid = await _provider(pool)
        entry = {"name": UNKNOWN_NAME, "protocol": "openai-chat"}
        async with v1_api(tmp_path, pool) as call:
            answers = await asyncio.gather(*(call("models.add_many", {"provider_id": pid, "models": [entry, entry]})
                                            for _ in range(2)))
            assert all(status == 200 for status, _ in answers), answers
            results = [answer["result"] for _, answer in answers]
            assert sorted(len(result["models"]) for result in results) == [0, 1]
            skipped = [item for result in results for item in result["skipped"]]
            assert skipped == [{**entry, "reason": "already registered"}] * 3
            assert len(await db.list_models(provider_id=pid)) == 1
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("invalid", [
    {"name": "missing-endpoint", "protocol": "anthropic-messages"},
    {"name": "invalid model name", "protocol": "openai-chat"},
])
def test_invalid_entry_rolls_back_whole_checklist(tmp_path, monkeypatch, invalid):
    async def run(pool):
        pid = await _provider(pool)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("models.add_many", {"provider_id": pid,
                "models": [{"name": UNKNOWN_NAME, "protocol": "openai-chat"}, invalid]})
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert answer["error"]["message"]
            assert await db.list_models(provider_id=pid) == []
    run_db(run, monkeypatch, timeout=30)
