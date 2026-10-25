"""Closed profile requests, real preview projection and generated standard-library CLI."""

import asyncio
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops import OpError
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients as clients
from tests.test_operator_auth import TOKEN
from tests.test_prompt_preview import _dependencies


@pytest.mark.parametrize("action,body", [
    ("create", {"name": "Example", "runtime_id": "rt-claude-code"}),
    ("create", {"name": "Example", "runtime_id": 3, "model_id": "model-example"}),
    ("create", {"name": "Example", "runtime_id": "rt-claude-code", "model_id": "model-example", "extra": 1}),
    ("update", {"profile_id": "agent-example", "fields": {}}),
    ("update", {"profile_id": "agent-example", "fields": {"custom_prompt": None}}),
    ("update", {"profile_id": "agent-example", "fields": {"agent_teams": "true"}}),
    ("delete", {"profile_id": None}),
    ("prompt_preview", {"profile_id": "agent-example", "role": "Worker"}),
    ("prompt_preview", {"profile_id": "agent-example", "role": "worker", "project_id": None}),
])
def test_profile_requests_reject_invalid_fields(action, body):
    with pytest.raises(OpError):
        api_v1.validate_arguments(api_v1.find("POST", "/api/v1/profiles/" + action).request, body)


@pytest.mark.parametrize("token", [TOKEN, CLIENT_TOKEN], ids=["operator", "client"])
def test_preview_preserves_json_parameters_layers_hash_sources_and_nullable_blocks(tmp_path, monkeypatch, clients, token):
    module, profile, layers = _dependencies(monkeypatch)
    project_id = "project /?& 中文"
    layers.return_value[2]["project_id"] = project_id
    monkeypatch.setattr(module.db_projects, "get_project", AsyncMock(return_value={
        "id": project_id, "name": "Example", "background": "Project context", "work_dir": None}))
    monkeypatch.setattr(module.db_documents, "list_documents", AsyncMock(return_value=[]))

    async def run():
        async with _serving(tmp_path) as server:
            path = "/api/v1/profiles/prompt_preview"
            status, _, answer = await _send(server, "POST " + path, _bearer(token), json.dumps({
                "profile_id": profile.id, "role": "worker", "project_id": project_id}))
            assert status == 200, answer
            preview = answer["result"]
            api_v1.validate_arguments(api_v1.find("POST", path).result, preview)
            assert preview["profile"] == {"id": profile.id, "name": profile.name}
            assert layers.await_args.args == (project_id,)
            assert preview["prompt_layers"]["project"]["id"] == project_id
            assert preview["prompt_layers"]["harness"]["source"] == "project"
            assert preview["role"] == "worker" and len(preview["looper_prompt_sha256"]) == 64
            text = "".join(block["text"] or "" for block in preview["blocks"] if block["owner"] == "looper")
            assert preview["looper_prompt_sha256"] == hashlib.sha256(text.encode()).hexdigest()
            assert preview["system_prompt_mode_source"] and preview["role_system_prompt_mode_source"]
            assert any(block["text"] is None for block in preview["blocks"])
            assert any("Project context" in (block["text"] or "") for block in preview["blocks"])
    asyncio.run(run())


@pytest.mark.parametrize("action,flags,body", [
    ("create", ["--name", "Example", "--runtime-id", "rt-claude-code", "--model-id", "model-example"],
     {"name": "Example", "runtime_id": "rt-claude-code", "model_id": "model-example"}),
    ("update", ["--profile-id", "agent-example", "--fields", '{"effort":null}'],
     {"profile_id": "agent-example", "fields": {"effort": None}}),
    ("delete", ["--profile-id", "agent-example"], {"profile_id": "agent-example"}),
    ("prompt-preview", ["--profile-id", "agent-example", "--role", "worker", "--project-id", "project /?&"],
     {"profile_id": "agent-example", "role": "worker", "project_id": "project /?&"}),
])
def test_generated_cli_builds_profile_requests_with_only_standard_library(tmp_path, action, flags, body):
    document = api_v1.document()
    args = loopercli.build_parser(document).parse_args(["profiles", action, *flags])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="profiles")) == 0
    path = "/api/v1/profiles/" + action.replace("-", "_")
    assert calls == [("POST", path, body)] and not api_v1.find("POST", path).hand
    source = tmp_path / "openapi.json"
    source.write_text(json.dumps(document))
    subprocess.run([sys.executable, "-c", """import json,sys
from looper import loopercli
args = loopercli.build_parser(json.load(open(sys.argv[1]))).parse_args(sys.argv[2:])
assert callable(args._run)
assert not any(m.startswith(('looper.agents', 'asyncpg', 'httpx')) for m in sys.modules)
""", str(source), "profiles", action, *flags], check=True, timeout=10)
