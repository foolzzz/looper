"""Read-only previews use the launch builders and runtime-owned block ordering."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.profile_registry import BUILTIN_PROFILE, RegistryError, RegistryNotFound
from looper.agents.runtimes import ApiProtocol, AuthKind, Capability, adapter
from tests.runtime_model_helpers import resolved_profile
from tests.test_agent_settings import _row
from tests.test_catalog_db import _Writer


async def _preview_request(server, profile_id, fields, writer):
    from looper.agents.api_v1 import OPERATOR
    from looper.agents.ops.clients import ClientOps

    server._client_ops = ClientOps(SimpleNamespace())
    await server._handle_v1("POST", "/api/v1/profiles/prompt_preview", {}, {},
                           json.dumps({"profile_id": profile_id, **fields}).encode(), writer, OPERATOR)


def _dependencies(monkeypatch, runtime="claude-code", mode="append", role="worker"):
    from looper.agents import prompt_preview as module
    from looper.db import agent_setting_layers

    profile = replace(BUILTIN_PROFILE, custom_prompt="  My profile identity  ",
                      runtime=replace(BUILTIN_PROFILE.runtime, type=runtime,
                                      # Rendering is testable before pi's execution capabilities are enabled.
                                      capabilities=(frozenset(Capability) if runtime == "pi"
                                                    else adapter(runtime).capabilities)),
                      provider=replace(BUILTIN_PROFILE.provider,
                                       auth=AuthKind.OAUTH if runtime == "grok" else AuthKind.API_KEY,
                                       protocols=frozenset(ApiProtocol), oauth_runtime_type=runtime))
    row = {**_row(profile), "custom_prompt": profile.custom_prompt}
    monkeypatch.setattr(module.db_profiles, "get_profile", AsyncMock(return_value=row))
    # The selected launch profile is valid and independent of the profile being previewed.
    selected = replace(BUILTIN_PROFILE, provider=replace(BUILTIN_PROFILE.provider, auth=AuthKind.API_KEY))
    layers = AsyncMock(return_value=[
        {"scope": "system", "profile": _row(selected), "harness_prompt": "Shared harness"},
        {"scope": "role", "role": role, "system_prompt_mode": mode},
        {"scope": "project", "project_id": "proj-demo", "harness_prompt": "Project harness"},
    ])
    monkeypatch.setattr(agent_setting_layers, "list_layers", layers)
    monkeypatch.setattr(module.db_profiles, "get_api_key", AsyncMock(return_value="test-only"))
    monkeypatch.setattr(module.db_roles, "get_config", AsyncMock(return_value={
        "system_prompt": "Configured role", "default_prompt": "Configured role"}))
    return module, profile, layers


# The preview takes its blocks from the runtime and its contract from the role, and the two do not interact: one
# sweep over the runtimes (a worker, appending) and one over the roles (on Claude Code, replacing).
@pytest.mark.parametrize("runtime,mode,role", [
    *((runtime, "append", "worker") for runtime in ("claude-code", "codex", "grok", "omp", "pi")),
    *(("claude-code", "replace", role) for role in ("planner", "worker", "reviewer", "manager")),
])
def test_preview_uses_profile_harness_contract_and_runtime_blocks(monkeypatch, runtime, mode, role):
    from looper.agents.system_prompt import sha256_text
    module, profile, loader = _dependencies(monkeypatch, runtime, mode, role)
    result = asyncio.run(module.preview(profile.id, role))
    assert loader.await_args.args == (None,)
    assert result["profile"] == {"id": profile.id, "name": profile.name}
    assert result["role"] == role and result["runtime"] == runtime
    assert result["system_prompt_mode"] == mode
    assert result["role_system_prompt_mode"] == mode and "system_prompt_mode_applies" not in result
    assert result["custom_prompt_set"]
    assert result["prompt_layers"]["custom_prompt"] == {"sha256": sha256_text("My profile identity")}
    assert result["prompt_layers"]["harness"]["source"] == "system"
    assert result["prompt_layers"]["task"] is None
    assert result["prompt_layers"]["contract"]["name"] == {
        "planner": "REPLY_CONTRACT", "worker": "WORK_CONTRACT", "reviewer": "REVIEW_CONTRACT",
        "manager": "TOOLS_CONTRACT"}[role]
    assert "the Task section is filled in at launch" in result["notes"]
    blocks = result["blocks"]
    assert any(
        b["owner"] == "looper" and b["text"].startswith("My profile identity\n\n")
        and "Shared harness" in b["text"] for b in blocks
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
    monkeypatch.setattr(module.db_documents, "list_documents", AsyncMock(return_value=[{
        "id": "doc-1", "title": "Stored guide", "source_kind": "text"}]))
    result = asyncio.run(module.preview(profile.id, "worker", "proj-demo"))
    assert loader.await_args.args == ("proj-demo",)
    assert any("## Project: Demo\nProject context" in (b["text"] or "") for b in result["blocks"])
    assert any("knowledge" in note and "director" in note for note in result["notes"])
    assert result["prompt_layers"]["project"]["id"] == "proj-demo"
    assert result["prompt_layers"]["harness"]["source"] == "project"
    assert any("Project harness" in (b["text"] or "") for b in result["blocks"])


def test_preview_manager_has_no_project_context(monkeypatch):
    module, profile, loader = _dependencies(monkeypatch)
    get_project = AsyncMock()
    monkeypatch.setattr(module.db_projects, "get_project", get_project)
    result = asyncio.run(module.preview(profile.id, "manager", "proj-demo"))
    assert loader.await_args.args == (None,)
    get_project.assert_not_awaited()
    assert result["prompt_layers"]["project"] is None


@pytest.mark.parametrize("mode", ["append", "replace"])
@pytest.mark.parametrize("runtime", ["omp", "pi"])
def test_preview_includes_project_instructions_without_writes(monkeypatch, tmp_path, mode, runtime):
    from looper.agents.runtimes import omp

    module, profile, _ = _dependencies(monkeypatch, runtime, mode)
    (tmp_path / "AGENTS.md").write_text("Project instruction: report verification evidence.")
    # Keep this standalone fixture independent of the checkout containing pytest's temp directory.
    monkeypatch.setattr(omp, "outside_repository", lambda cwd: True)
    monkeypatch.setattr(omp.instructions, "repository_root", lambda cwd: tmp_path)
    monkeypatch.setattr(module.db_projects, "get_project", AsyncMock(return_value={
        "id": "proj-demo", "name": "Demo", "work_dir": str(tmp_path)}))
    monkeypatch.setattr(module.db_documents, "list_documents", AsyncMock(return_value=[]))
    result = asyncio.run(module.preview(profile.id, "worker", "proj-demo"))
    assert any("Project instruction: report verification evidence." in (b["text"] or "")
               for b in result["blocks"])
    assert list(tmp_path.iterdir()) == [tmp_path / "AGENTS.md"]


def test_preview_dsh_uses_bootstrap_builder_and_reports_effective_append(monkeypatch, tmp_path):
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch
    from looper.agents.runtimes import dsh
    from tests.test_dsh_runtime import _register_prompt_runtime, _node

    _register_prompt_runtime(monkeypatch)
    module, profile, layers = _dependencies(monkeypatch, "dsh", "replace")
    profile = replace(profile, model="deepseek-flash")
    module.db_profiles.get_profile.return_value = {**_row(profile), "custom_prompt": profile.custom_prompt}
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text('Project {{unknown}}: 中文 "quote"\nsecond line')
    project = {"id": "proj-demo", "name": "Demo", "work_dir": str(tmp_path)}
    monkeypatch.setattr(module.db_projects, "get_project", AsyncMock(return_value=project))
    monkeypatch.setattr(module.db_documents, "list_documents", AsyncMock(return_value=[]))
    result = asyncio.run(module.preview(profile.id, "worker", "proj-demo"))
    assert result["system_prompt_mode"] == "append"
    assert result["role_system_prompt_mode"] == "replace"
    assert layers.return_value[1]["system_prompt_mode"] == "replace"
    assert [b["owner"] for b in result["blocks"]] == ["runtime", "looper"]
    assert result["blocks"][0]["text"] is None
    text = result["blocks"][1]["text"]
    assert text.startswith("My profile identity\n\n# Runtime notes (DSH)")
    assert text.index("# Runtime notes") < text.index("Project {{unknown}}") < text.index("Project harness")
    from looper.agents.system_prompt import WORK_CONTRACT, compose_system_prompt
    looper = compose_system_prompt("Configured role", harness="Project harness", custom_prompt=profile.custom_prompt,
                                   project_name=project["name"], contract=WORK_CONTRACT)
    launch = Launch(AgentSettings(profile=profile), role=AgentType.WORKER)
    assert text == dsh._prompt_text(looper, launch, cwd=tmp_path)
    assert text.endswith(WORK_CONTRACT)
    # The actual launch materialization calls the same builder, not a preview-only formatter.
    executable = tmp_path / "dsh"
    executable.touch()
    files = dsh.bootstrap_files(str(executable), looper, launch, cwd=tmp_path)
    assert set(tmp_path.iterdir()) == {tmp_path / ".git", tmp_path / "AGENTS.md", executable}
    # Inspect the generated module through a minimal exports-only runner.
    package = tmp_path / "node_modules" / "@deepseek-ai" / "dsh-headless"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"type":"module","exports":"./index.mjs"}')
    (package / "index.mjs").write_text("export const name='headless-runner', Config={}, inject=[]; export function apply() {}")
    home = dsh.write_home(tmp_path / "local", files)
    done = _node(tmp_path, '''
const plugin = await import(process.argv[2]);
plugin.apply({systemPrompt: {section(value) {console.log(JSON.stringify(value.text));}}}, {});
''', dsh.bootstrap_entry(home)["name"])
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == text


@pytest.mark.parametrize("mode", ["append", "replace"])
def test_pi_preview_matches_the_final_prompt_file(monkeypatch, tmp_path, mode):
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch
    from looper.agents.runtimes import SystemPromptMode

    module, profile, _ = _dependencies(monkeypatch, "pi", mode)
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("Project instruction: preserve the repository boundary.")
    monkeypatch.setattr(module.db_projects, "get_project", AsyncMock(return_value={
        "id": "proj-demo", "name": "Demo", "work_dir": str(tmp_path)}))
    monkeypatch.setattr(module.db_documents, "list_documents", AsyncMock(return_value=[]))
    prompts = []
    build = module.build_system_prompt

    def capture(**kwargs):
        prompt = build(**kwargs)
        prompts.append(prompt)
        return prompt

    monkeypatch.setattr(module, "build_system_prompt", capture)
    result = asyncio.run(module.preview(profile.id, "worker", "proj-demo"))
    launch = Launch(AgentSettings(
        profile=resolved_profile(profile.id, profile.name, profile.runtime, replace(
            profile.provider, oauth_runtime_type=None, api_key="test-only", base_url="http://localhost.invalid/v1"), profile.model,
            custom_prompt=profile.custom_prompt),
        system_prompt_mode=SystemPromptMode(mode)), role=AgentType.WORKER)
    command = launch.runtime.run_command("pi", cwd=tmp_path, launch=launch,
                                         prompt_file=prompts[0].write_file(tmp_path / "runtime"))
    flag = "--system-prompt" if mode == "replace" else "--append-system-prompt"
    written = Path(command[command.index(flag) + 1]).read_bytes()
    preview = "".join(block["text"] or "" for block in result["blocks"] if block["owner"] == "looper")
    assert preview.encode("utf-8") == written


@pytest.mark.parametrize("role,expected", [("manager", "replace"), ("planner", "replace"),
                                           ("worker", "append"), ("reviewer", "append")])
def test_pi_preview_keeps_role_default_prompt_modes(monkeypatch, role, expected):
    module, profile, _ = _dependencies(monkeypatch, "pi", None, role)
    result = asyncio.run(module.preview(profile.id, role))
    assert result["system_prompt_mode"] == expected
    assert result["role_system_prompt_mode"] == expected


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
    asyncio.run(_preview_request(server, profile.id, {} if role is None else {"role": role}, writer))
    head, _, data = writer.data.partition(b"\r\n\r\n")
    assert int(head.split()[1]) == status
    if status == 200:
        result = json.loads(data)["result"]
        assert result["prompt_layers"] and result["blocks"]
        assert any(b["text"] is None for b in result["blocks"])


def test_preview_endpoint_unknown_profile_is_404(monkeypatch):
    from looper.agents.http_server import AgentHTTPServer
    module, _, _ = _dependencies(monkeypatch)
    monkeypatch.setattr(module.db_profiles, "get_profile", AsyncMock(return_value=None))
    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=SimpleNamespace())
    writer = _Writer()
    asyncio.run(_preview_request(server, "agent-missing", {"role": "worker"}, writer))
    assert int(writer.data.split()[1]) == 404


@pytest.mark.parametrize("changes,reason", [
    ({"runtime_type": "unknown-runtime"}, "unknown-runtime"),
    ({"runtime_capabilities": [Capability.RUN.value]}, "lacks"),
    ({"provider_protocols": [ApiProtocol.OPENAI_CHAT.value]}, "share no protocol"),
    ({"provider_auth": "oauth", "provider_oauth_runtime_type": "codex"}, "pairs only"),
])
def test_preview_endpoint_refuses_ineligible_profile_with_real_settings(monkeypatch, changes, reason):
    from looper.agents.http_server import AgentHTTPServer

    module, profile, layers = _dependencies(monkeypatch)
    # These rows still parse, but cannot launch as a Planner. Settings resolution is real.
    row = {**module.db_profiles.get_profile.return_value, **changes}
    module.db_profiles.get_profile.return_value = row
    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=SimpleNamespace())
    writer = _Writer()
    asyncio.run(_preview_request(server, profile.id, {"role": "planner"}, writer))
    head, _, data = writer.data.partition(b"\r\n\r\n")
    assert int(head.split()[1]) == 400
    assert reason in json.loads(data)["error"]["message"]
    assert layers.await_args.args == (None,)


@pytest.mark.parametrize("stored_kind", [None, "text", "upload"])
def test_preview_path_knowledge_reuses_launch_index(monkeypatch, tmp_path, stored_kind):
    from looper.agents.knowledge import StoredSet, _entry, clipped_index
    module, profile, _ = _dependencies(monkeypatch)
    (tmp_path / "guide.md").write_text("Reference")
    project = {"id": "proj-demo", "name": "Demo", "work_dir": str(tmp_path)}
    rows = [{"id": "doc-1", "source_kind": "path", "path": "guide.md", "title": "Guide", "summary": "Reference"}]
    if stored_kind:
        rows.append({"id": "doc-2", "source_kind": stored_kind, "title": "Stored guide"})
    monkeypatch.setattr(module.db_projects, "get_project", AsyncMock(return_value=project))
    monkeypatch.setattr(module.db_documents, "list_documents", AsyncMock(return_value=rows))
    result = asyncio.run(module.preview(profile.id, "worker", "proj-demo"))
    index = clipped_index([_entry(StoredSet(None, {}), project, rows[0])])
    assert any(index in (block["text"] or "") for block in result["blocks"])
    assert result["prompt_layers"]["project"]["knowledge_documents"] == 1
    assert not any("knowledge is omitted" in note for note in result["notes"])
    if stored_kind:
        assert any("doc-2" in note and "Stored guide" in note and "omitted" in note for note in result["notes"])
