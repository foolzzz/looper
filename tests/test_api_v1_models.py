"""Ordinary model lookup and compatibility with the existing model Tool schema."""

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1, profile_registry
from looper.agents.ops import OpError
from looper.agents.ops.config import _MODEL_SETTINGS, _PROTOCOL
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients as clients
from tests.test_operator_auth import TOKEN


@pytest.mark.parametrize("token", [TOKEN, CLIENT_TOKEN], ids=["operator", "client"])
def test_get_normalizes_missing_service_warnings(tmp_path, monkeypatch, clients, token):
    async def get(model):
        return {"model": {"model_id": model, "name": "Exact[1m]"}}
    monkeypatch.setattr(profile_registry, "get_model", get)

    async def run():
        async with _serving(tmp_path) as server:
            status, _, answer = await _send(server, "POST /api/v1/models/get", _bearer(token),
                                            json.dumps({"model": "model-example"}))
            assert status == 200 and answer["result"] == {
                "model": {"model_id": "model-example", "name": "Exact[1m]"}, "warnings": []}
    asyncio.run(run())


@pytest.mark.parametrize("body", [{}, {"model": None}, {"model": 3}, {"model": "model-example", "extra": True}])
def test_get_requires_a_string_identifier_and_closed_request(body):
    with pytest.raises(OpError):
        api_v1.validate_arguments(api_v1.find("POST", "/api/v1/models/get").request, body)


def test_existing_operations_keep_authoritative_model_settings_and_protocols():
    for action in ("list", "create", "update", "delete"):
        operation = api_v1.find("POST", f"/api/v1/ops/models/{action}")
        assert operation.tool == "looper_models" and operation.action == action
        assert operation.request["properties"]["protocol"] == _PROTOCOL
        for field, schema in _MODEL_SETTINGS.items():
            assert operation.request["properties"][field] == schema
        assert operation.result["additionalProperties"] is False


@pytest.mark.parametrize("family,action,flags,body,path", [
    ("models", "get", ["--model", "model-example"], {"model": "model-example"}, "/api/v1/models/get"),
    ("models", "create", ["--provider", "Example", "--name", "Exact[1m]", "--protocol", "openai-chat",
                           "--context-window", "200000", "--input-usd-per-mtok", "2.5"],
     {"provider": "Example", "name": "Exact[1m]", "protocol": "openai-chat", "context_window": 200000,
      "input_usd_per_mtok": 2.5}, "/api/v1/ops/models/create"),
])
def test_generated_cli_builds_model_requests_without_server_dependencies(tmp_path, family, action, flags, body, path):
    document = api_v1.document()
    args = loopercli.build_parser(document).parse_args([family, action, *flags])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="models")) == 0
    assert calls == [("POST", path, body)]
    operation = api_v1.find("POST", path)
    assert not operation.hand
    source = tmp_path / "openapi.json"
    source.write_text(json.dumps(document))
    subprocess.run([sys.executable, "-c", """import json,sys
from looper import loopercli
args = loopercli.build_parser(json.load(open(sys.argv[1]))).parse_args(sys.argv[2:])
assert callable(args._run)
assert not any(m.startswith(('looper.agents', 'asyncpg', 'httpx')) for m in sys.modules)
""", str(source), family, action, *flags], check=True, timeout=10)
