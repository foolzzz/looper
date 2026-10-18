"""Read-only previews use the launch builders and runtime-owned block ordering."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.agent_settings import AgentSettings, Harness, Layer
from looper.agents.profile_registry import BUILTIN_PROFILE, RegistryError, RegistryNotFound
from looper.agents.runtimes import SystemPromptMode
from tests.test_catalog_db import _Writer


def _dependencies(monkeypatch, runtime="claude-code", mode="append"):
    from looper.agents import prompt_preview as module
    profile = replace(BUILTIN_PROFILE, custom_prompt="  My profile identity  ",
                      runtime=replace(BUILTIN_PROFILE.runtime, type=runtime),
                      system_prompt_mode=SystemPromptMode(mode))
    monkeypatch.setattr(module.db_profiles, "get_profile", AsyncMock(return_value={"found": True}))
    monkeypatch.setattr(module.AgentProfile, "from_row", classmethod(lambda cls, row: profile))
    loader = AsyncMock(return_value=AgentSettings(harness=Harness("Project harness", Layer.PROJECT)))
    monkeypatch.setattr(module, "load_agent_settings", loader)
    monkeypatch.setattr(module.db_roles, "get_config", AsyncMock(return_value={
        "system_prompt": "Configured role", "default_prompt": "Configured role"}))
    return module, profile, loader


@pytest.mark.parametrize("runtime", ["claude-code", "codex", "grok", "omp"])
@pytest.mark.parametrize("mode", ["append", "replace"])
@pytest.mark.parametrize("role", ["planner", "worker", "reviewer", "manager"])
def test_preview_uses_profile_harness_contract_and_runtime_blocks(monkeypatch, runtime, mode, role):
    from looper.agents.system_prompt import sha256_text
    module, profile, loader = _dependencies(monkeypatch, runtime, mode)
    result = asyncio.run(module.preview(profile.id, role))
    loader.assert_awaited_once_with(role, None)
    assert result["profile"] == {"id": profile.id, "name": profile.name}
    assert result["role"] == role and result["runtime"] == runtime
    assert result["system_prompt_mode"] == mode and result["custom_prompt_set"]
    assert result["prompt_layers"]["custom_prompt"] == {"sha256": sha256_text("My profile identity")}
    assert result["prompt_layers"]["harness"]["source"] == "project"
    assert result["prompt_layers"]["task"] is None
    assert result["prompt_layers"]["contract"]["name"] == {
        "planner": "REPLY_CONTRACT", "worker": "WORK_CONTRACT", "reviewer": "REVIEW_CONTRACT",
        "manager": "TOOLS_CONTRACT"}[role]
    assert "the Task section is filled in at launch" in result["notes"]
    blocks = result["blocks"]
    assert any(
        b["owner"] == "looper" and b["text"].startswith("My profile identity\n\n")
        and "Project harness" in b["text"] for b in blocks
    )
    assert all(b["text"] is None for b in blocks if b["owner"] == "runtime")
    if runtime == "claude-code":
        assert [b["note"] for b in blocks[:2]] == [
            "billing header (fixed)", "identity line (fixed; changes with append vs replace)"]
    assert len(result["looper_prompt_sha256"]) == 64


def test_preview_project_uses_project_harness_and_background_without_writes(monkeypatch):
    module, profile, loader = _dependencies(monkeypatch)
    monkeypatch.setattr(module.db_projects, "get_project", AsyncMock(return_value={
        "id": "proj-demo", "name": "Demo", "background": "Project context", "work_dir": "/unused"}))
    monkeypatch.setattr(module.db_documents, "list_documents", AsyncMock(return_value=[{"id": "doc-1", "source_kind": "text"}]))
    result = asyncio.run(module.preview(profile.id, "worker", "proj-demo"))
    loader.assert_awaited_once_with("worker", "proj-demo")
    assert any("## Project: Demo\nProject context" in (b["text"] or "") for b in result["blocks"])
    assert any("knowledge" in note and "director" in note for note in result["notes"])
    assert result["prompt_layers"]["project"]["id"] == "proj-demo"


def test_preview_manager_has_no_project_context(monkeypatch):
    module, profile, loader = _dependencies(monkeypatch)
    get_project = AsyncMock()
    monkeypatch.setattr(module.db_projects, "get_project", get_project)
    result = asyncio.run(module.preview(profile.id, "manager", "proj-demo"))
    loader.assert_awaited_once_with("manager", None)
    get_project.assert_not_awaited()
    assert result["prompt_layers"]["project"] is None


@pytest.mark.parametrize("role", ["", "bad", "Worker"])
def test_preview_refuses_bad_role(monkeypatch, role):
    module, profile, _ = _dependencies(monkeypatch)
    with pytest.raises(RegistryError, match="role"):
        asyncio.run(module.preview(profile.id, role))


def test_preview_unknown_profile(monkeypatch):
    module, _, _ = _dependencies(monkeypatch)
    monkeypatch.setattr(module.db_profiles, "get_profile", AsyncMock(return_value=None))
    with pytest.raises(RegistryNotFound):
        asyncio.run(module.preview("agent-missing", "worker"))


@pytest.mark.parametrize("role,status", [(None, 400), ("bad", 400), ("worker", 200)])
def test_preview_endpoint(monkeypatch, role, status):
    from looper.agents.http_server import AgentHTTPServer
    module, profile, _ = _dependencies(monkeypatch)
    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=SimpleNamespace())
    writer = _Writer()
    asyncio.run(server._route_api("GET", f"/api/agent-profiles/{profile.id}/prompt-preview",
                                  {} if role is None else {"role": role}, b"", writer))
    head, _, data = writer.data.partition(b"\r\n\r\n")
    assert int(head.split()[1]) == status
    if status == 200:
        result = json.loads(data)
        assert result["prompt_layers"] and result["blocks"]
        assert any(b["text"] is None for b in result["blocks"])


def test_preview_endpoint_unknown_profile_is_404(monkeypatch):
    from looper.agents.http_server import AgentHTTPServer
    module, _, _ = _dependencies(monkeypatch)
    monkeypatch.setattr(module.db_profiles, "get_profile", AsyncMock(return_value=None))
    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=SimpleNamespace())
    writer = _Writer()
    asyncio.run(server._route_api("GET", "/api/agent-profiles/agent-missing/prompt-preview",
                                  {"role": "worker"}, b"", writer))
    assert int(writer.data.split()[1]) == 404


def test_preview_path_knowledge_reuses_launch_index(monkeypatch, tmp_path):
    from looper.agents.knowledge import StoredSet, _entry, clipped_index
    module, profile, _ = _dependencies(monkeypatch)
    (tmp_path / "guide.md").write_text("Reference")
    project = {"id": "proj-demo", "name": "Demo", "work_dir": str(tmp_path)}
    rows = [{"id": "doc-1", "source_kind": "path", "path": "guide.md", "title": "Guide", "summary": "Reference"}]
    monkeypatch.setattr(module.db_projects, "get_project", AsyncMock(return_value=project))
    monkeypatch.setattr(module.db_documents, "list_documents", AsyncMock(return_value=rows))
    result = asyncio.run(module.preview(profile.id, "worker", "proj-demo"))
    index = clipped_index([_entry(StoredSet(None, {}), project, rows[0])])
    assert any(index in (block["text"] or "") for block in result["blocks"])
    assert result["prompt_layers"]["project"]["knowledge_documents"] == 1
    assert not any("knowledge is omitted" in note for note in result["notes"])
