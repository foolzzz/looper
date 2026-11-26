"""Pilot choices are operator-only; imported and planner-owned definitions fail closed."""
from __future__ import annotations

import ast
import hashlib
import inspect
import io
import json
import subprocess
import sys
import tarfile
import textwrap
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.db_support import run_db


def off_snapshot():
    from looper.agents import agent_settings, system_prompt
    from looper.agents.ops import clients, planner, room, service
    from looper.agents.tiers import manager, planner as planner_tier, worker
    from tests.runtime_model_helpers import builtin_launch

    commands, bindings = [], []
    for role in ("manager", "planner", "worker", "reviewer"):
        launch = builtin_launch(role=agent_settings.AgentType(role))
        runtime = launch.runtime
        method = runtime.resident_command if role in ("manager", "planner") else runtime.run_command
        commands.append(method("claude", cwd=Path("/workspace"), prompt_file=Path("/runtime/system.md"), launch=launch))
        bindings.append(launch.binding_sha256)
    # These are the actual inputs that decide whether a resident process is restarted.
    signatures = []
    # The chat-scope launch member adds this one node, guarded by `scope is not None` (shown in
    # test_chat_scope_signature_off_path.py); with routing off it never runs, so main's list stays the frozen one.
    scope_node = ast.dump(ast.parse("signature += (scope.fingerprint, scope.mode.value)").body[0], include_attributes=False)
    for method in (manager.ManagerAgent._prepare_turn, planner_tier.PlannerAgent._run_turn):
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        signatures.extend(ast.dump(node, include_attributes=False) for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AugAssign)) and any(isinstance(part, ast.Name) and part.id == "signature"
                for target in ([node.target] if isinstance(node, ast.AugAssign) else node.targets)
                for part in ast.walk(target)) and ast.dump(node, include_attributes=False) != scope_node)
    values = {"prompts": [manager.DEFAULT_SYSTEM_PROMPT, planner_tier.DEFAULT_SYSTEM_PROMPT,
                worker.DEFAULT_SYSTEM_PROMPT, agent_settings.DEFAULT_HARNESS_PROMPT, system_prompt.REVIEW_CONTRACT],
        "tools": [(t.name, t.description, t.schema) for t in (*service.MANAGER_TOOLS, *clients.CLIENT_TOOLS,
                  *planner.TOOLS, *planner.ROLE_TOOLS, *room.TOOLS)],
        "commands": commands, "signatures": signatures, "bindings": bindings}
    from looper.agents.runtimes.grok import GrokRuntime
    from looper.agents.runtimes.grok_steer import GrokChannel

    values["existing_grok_reviewer"] = [inspect.getsource(method) for method in (
        GrokRuntime.launch_args, GrokRuntime.run_command, GrokRuntime.steer_command,
        GrokRuntime.run_channel, GrokChannel.start, GrokChannel.events)]
    return {name: hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            for name, value in values.items()}


@pytest.mark.parametrize("home", [None, "/home/runner"], ids=["host-home", "ci-home"])
def test_off_prompts_tools_commands_and_signatures_match_main(tmp_path, monkeypatch, home):
    if home is not None:
        monkeypatch.setenv("HOME", home)
    expected = json.loads((Path(__file__).parent / "fixtures/acp/default_off.json").read_text())
    snapshot = off_snapshot()
    assert {key: value for key, value in snapshot.items() if key != "commands"} == {
        key: value for key, value in expected["sha256"].items() if key != "commands"}
    # The frozen command digest contains the capturing host's HOME. Compare
    # command bytes with that same pinned revision under the current environment.
    # Keep the fixture intact: no new command or prompt is blessed here.
    archive = subprocess.check_output(["git", "archive", expected["base"], "looper", "tests/runtime_model_helpers.py"])
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(tmp_path, filter="data")
    # A regular package keeps unrelated installed test packages out of this readback.
    (tmp_path / "tests/__init__.py").touch()
    script = ("import ast, hashlib, inspect, json, textwrap\nfrom pathlib import Path\n"
              + inspect.getsource(off_snapshot) + '\nprint(off_snapshot()["commands"])\n')
    baseline = subprocess.check_output([sys.executable, "-c", script], cwd=tmp_path, text=True).strip()
    assert snapshot["commands"] == baseline


def test_work_on_and_task_manager_switch_off_prompts_and_tools_match_main(monkeypatch):
    """With work mode on and tasks.conversations.manager_answers off, nothing the Planner or Manager sees changes."""
    from looper.agents import task_conversations_settings as conversations

    monkeypatch.setattr(conversations, "_enabled", True)
    monkeypatch.setattr(conversations, "_manager_answers", False)
    expected = json.loads((Path(__file__).parent / "fixtures/acp/default_off.json").read_text())["sha256"]
    snapshot = off_snapshot()
    assert {key: value for key, value in snapshot.items() if key != "commands"} == {
        key: value for key, value in expected.items() if key != "commands"}


async def _seed(pool):
    # Seed stored rows directly so guards also cover pre-existing/stale bindings.
    await pool.execute("""
        INSERT INTO agent_runtimes(id,name,type,capabilities,builtin) VALUES ('rt-acp','ACP','acp',ARRAY['run'],true);
        INSERT INTO llm_providers(id,name,vendor_type,auth,oauth_runtime_type) VALUES
            ('prov-acp','Review account','acp-command','oauth','acp');
        INSERT INTO llm_provider_endpoints(provider_id,protocol,base_url) VALUES
            ('prov-acp','openai-responses','');
        INSERT INTO llm_models(id,provider_id,name,protocol) VALUES
            ('model-acp','prov-acp','review-model','openai-responses');
        INSERT INTO agent_profiles(id,name,runtime_id,model_id) VALUES
            ('profile-acp','ACP review','rt-acp','model-acp');
        INSERT INTO system_settings(key,value) VALUES
            ('acp_reviewer.enabled','true'),
            ('acp_reviewer.command','["grok","agent","stdio"]'),
            ('acp_reviewer.model','review-model'),
            ('acp_reviewer.account','Review account');
    """)


@pytest.mark.parametrize("entry", ["manager_create", "manager_update", "manager_select", "planner_profile",
    "task_role", "workflow_profile", "workflow_role", "template_profile", "fallback_source", "fallback_target"])
def test_acp_is_refused_at_each_non_operator_choice(monkeypatch, entry):
    from looper.agents import agent_settings, profile_registry, task_roles, task_template_execution, workflows
    from looper.agents.ops import config, settings, planner
    from looper.agents.ops import OpError

    async def test(pool):
        await _seed(pool)
        monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})
        if entry.startswith("manager_"):
            if entry == "manager_select":
                place = settings._Place(agent_settings.Layer.ROLE, agent_settings.AgentType.REVIEWER, None)
                with pytest.raises(OpError, match="ACP|acp"):
                    await settings._layer_data(place, {"profile": "profile-acp"})
            else:
                args = ({"action": "create", "name": "new", "runtime": "acp", "model": {"id": "model-acp"}}
                        if entry == "manager_create" else {"action": "update", "profile": "profile-acp", "name": "renamed"})
                call = SimpleNamespace(arguments=args, scoped=False, manager_id=None)
                with pytest.raises(OpError, match="ACP|acp"):
                    await config._profiles(call)
        elif entry == "planner_profile":
            monkeypatch.setattr(planner, "_task", AsyncMock(return_value={"project_id": None}))
            call = SimpleNamespace(task_id="task", arguments={"role": "reviewer", "profile_id": "profile-acp"})
            with pytest.raises(OpError, match="ACP|acp"):
                await planner._profile_set(call)
        elif entry == "task_role":
            with pytest.raises(task_roles.RoleForbidden, match="ACP|acp"):
                await task_roles.allowed_profile(None, "profile-acp", task_roles.RoleKind.REVIEW)
        elif entry.startswith("workflow"):
            raw = {"key": "review-test", "name": "Review test"}
            if entry == "workflow_profile":
                raw["profiles"] = {"reviewer": "profile-acp"}
            else:
                raw["roles"] = [{"key": "review", "kind": "review", "role_prompt": "Inspect", "profile_id": "profile-acp"}]
            with pytest.raises(workflows.WorkflowError, match="ACP|acp"):
                await workflows.check_profiles(workflows.Workflow.from_dict(raw))
        elif entry == "template_profile":
            from looper.agents.task_templates import TaskTemplate, TemplateError
            template = TaskTemplate("manager", "review", "Review", profiles={"reviewer": "profile-acp"})
            with pytest.raises(TemplateError, match="ACP|acp"):
                await task_template_execution.merged_profiles(template, {})
        else:
            from looper.db import profile_registry as db
            acp = profile_registry.AgentProfile.from_row(await db.get_profile("profile-acp"))
            builtin = profile_registry.builtin_resolved_profile()
            candidate = replace(acp, fallback_profile_id=builtin.id) if entry == "fallback_source" else replace(
                builtin, fallback_profile_id=acp.id)
            async with pool.acquire() as conn:
                with pytest.raises(profile_registry.RegistryError, match="ACP|acp"):
                    await profile_registry._check_fallbacks(conn, candidate)
        assert await pool.fetchval("SELECT name FROM agent_profiles WHERE id='profile-acp'") == "ACP review"
    run_db(test, monkeypatch)


def test_project_allowlist_cannot_reintroduce_acp(monkeypatch):
    from looper.agents.projects import choosable_profiles
    from looper.db import projects

    async def test(pool):
        await _seed(pool)
        from looper.db import profile_registry as db
        row = await db.get_profile("profile-acp")
        monkeypatch.setattr(projects, "allowed_profiles", AsyncMock(return_value=[row]))
        rows, restricted = await choosable_profiles("project")
        assert restricted and rows == []
    run_db(test, monkeypatch)


@pytest.mark.parametrize("entry", ["manager_create", "manager_update", "agents_api", "create_api"])
@pytest.mark.parametrize("wanted", ["profile-acp", "aCp ReViEw"])
def test_task_profile_entry_points_refuse_acp_without_writing(monkeypatch, tmp_path, entry, wanted):
    from looper.agents import profile_registry
    from looper.agents.bus import MessageBus
    from looper.agents.ops import Turn
    from looper.agents.ops.service import ManagerOps, Platform
    from looper.db import projects, tasks
    from tests.db_support import v1_api

    async def test(pool):
        await _seed(pool)
        monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})
        project = await projects.create_project(name="Review project", work_dir=str(tmp_path), pool=pool)
        await tasks.create_task(task_id="task-review", requirement="Inspect changes", project_id=project["id"],
                                work_dir=str(tmp_path), status="init", pool=pool)
        profiles = {"reviewer": wanted}
        if entry.startswith("manager_"):
            platform = Platform(bus=MessageBus(runtime_root=tmp_path),
                                task_router=SimpleNamespace(has_planner=lambda _: False))
            ops = ManagerOps(platform, url="http://127.0.0.1:9", token="synthetic-token")
            turn = Turn(channel="web", chat="synthetic-review", message_id="synthetic-message")
            ops.begin_turn(turn)
            try:
                if entry == "manager_create":
                    answer = await ops.call("looper_task_create", {"requirement": "Inspect changes",
                        "title": "Review", "project": project["name"], "profiles": profiles})
                else:
                    answer = await ops.call("looper_task_update", {"task_id": "task-review", "profiles": profiles})
            finally:
                ops.end_turn(turn)
            assert answer["status"] == "error", answer
            assert answer["error"]["code"] == "invalid", answer
        else:
            async with v1_api(tmp_path, pool) as call:
                if entry == "agents_api":
                    status, answer = await call("tasks.agents", {"task_id": "task-review", "profiles": profiles})
                else:
                    status, answer = await call("tasks.create", {"requirement": "Inspect changes",
                        "project_id": project["id"], "profile_overrides": profiles})
            assert status == 400, answer
        assert "ACP Reviewer pilot cannot be selected" in answer["error"]["message"], answer
        assert await pool.fetchval("SELECT count(*) FROM task_profile_overrides") == 0
        assert await pool.fetchval("SELECT count(*) FROM tasks") == 1
    run_db(test, monkeypatch)


def test_interactive_template_never_drops_acp_to_builtin(monkeypatch):
    from looper.agents import task_template_execution as execution
    from looper.agents.task_templates import TaskTemplate, TemplateError
    from looper.agents.acp_guards import AcpSelectionError

    template = TaskTemplate("manager", "review", "Review", role_templates=("review",))
    row = {"kind": "review", "role_prompt": "Inspect", "harness": None, "profile_id": "profile-acp", "items": []}
    def refused(*args):
        raise AcpSelectionError("ACP pilot cannot be selected by a task template")
    with pytest.raises(TemplateError, match="ACP"):
        execution.validate_role_dependencies(template, {"name": "Project", "planner_roles": "on"},
            {"review": row}, set(), set(), refused, interactive=True)


@pytest.mark.parametrize("reason", ["disabled", "unavailable", "unresolvable", "overlap", "pairing"])
def test_launch_refusals_record_reason_and_never_spawn(monkeypatch, tmp_path, reason):
    import asyncio
    import subprocess
    from looper.agents import acp_reviewer_settings as settings
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch
    from looper.agents.profile_registry import LegacyProviderSpec, RuntimeSpec
    from looper.agents.runtimes import ADAPTERS, ApiProtocol, AuthKind
    from looper.agents.runtimes.channel import RunStartError
    from looper.agents.runtimes import acp_policy
    from looper.agents.tiers import worker
    from tests.runtime_model_helpers import resolved_profile

    repo = tmp_path / "work"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[cli]\nuse_leader=false\n')
    snapshot = settings.AcpReviewerSettings(True, ("grok", "agent", "stdio"), "review-model", "Review account",
                                            str(config_dir), ())
    if reason == "unresolvable":
        snapshot = replace(snapshot, inspect_roots=(str(tmp_path / "missing"),))
    if reason == "overlap":
        snapshot = replace(snapshot, config_dir=str(repo))
    provider = LegacyProviderSpec("prov-acp", "Review account", AuthKind.OAUTH,
                                  frozenset({ApiProtocol.OPENAI_RESPONSES}), "acp")
    profile = resolved_profile("profile-acp", "ACP review", RuntimeSpec("rt-acp", "ACP", "acp", "",
                               ADAPTERS["acp"].capabilities), provider, "review-model")
    launch = Launch(AgentSettings(profile=profile), role=AgentType.REVIEWER, acp_reviewer=snapshot)
    current = replace(snapshot, enabled=False) if reason == "disabled" else replace(
        snapshot, model="different-model") if reason == "pairing" else snapshot
    monkeypatch.setattr(settings, "read", AsyncMock(return_value=current))
    monkeypatch.setattr(worker.vault, "load", AsyncMock(return_value=(None, None)))
    if reason == "unavailable":
        monkeypatch.setattr(acp_policy, "_preflight", lambda _: False)
    spawn = AsyncMock(side_effect=AssertionError("a refused agent must not spawn"))
    monkeypatch.setattr(worker.proc_tree, "spawn", spawn)
    handle = worker.ProcessRun(ADAPTERS["acp"], list(snapshot.command), launch=launch, cwd=repo,
                                session_dir=None, label="review")
    async def run():
        with pytest.raises(RunStartError):
            await handle.launch()
        await handle.end()
    asyncio.run(run())
    spawn.assert_not_awaited()
    record = handle.launch_record["acp_write_policy"]
    expected = {"disabled": "settings_refused", "pairing": "settings_refused",
                "unavailable": "write_policy_unavailable", "unresolvable": "unresolvable_protected_path",
                "overlap": "config_dir_overlaps"}[reason]
    assert record["reason"] == expected
    if reason == "disabled":
        assert "acp_reviewer.enabled" in record["detail"]
    assert subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"]) == b""


def test_launch_record_masks_argv_and_keeps_command_identity():
    from looper.agents.acp_reviewer_settings import AcpReviewerSettings
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch
    from looper.agents.profile_registry import LegacyProviderSpec, RuntimeSpec
    from looper.agents.runtimes import ADAPTERS, ApiProtocol, AuthKind
    from tests.runtime_model_helpers import resolved_profile

    provider = LegacyProviderSpec("prov-acp", "Review account", AuthKind.OAUTH,
                                  frozenset({ApiProtocol.OPENAI_RESPONSES}), "acp")
    profile = resolved_profile("profile-acp", "ACP review", RuntimeSpec("rt-acp", "ACP", "acp", "",
                               ADAPTERS["acp"].capabilities), provider, "review-model")
    config = AcpReviewerSettings(True, ("/example/bin/grok", "agent", "stdio", "--api-key", "synthetic-secret"),
                                 "review-model", "Review account")
    record = Launch(AgentSettings(profile=profile), role=AgentType.REVIEWER, acp_reviewer=config).record()
    assert record["acp_command"] == {"argv": ["grok", "agent", "stdio", "--api-key", "***"],
        "account": "Review account", "model": "review-model", "config_dir_is_scratch": True}
    assert "synthetic-secret" not in json.dumps(record)


def test_planner_template_profile_refusal_is_not_dropped(monkeypatch):
    from looper.agents.ops import planner, OpError

    async def test(pool):
        await _seed(pool)
        monkeypatch.setattr(planner, "_task", AsyncMock(return_value={"project_id": "project"}))
        monkeypatch.setattr(planner, "_execution", AsyncMock(return_value=SimpleNamespace(role_text_limit=8000)))
        monkeypatch.setattr(planner.db_templates, "get_template", AsyncMock(return_value={
            "template_key": "inspect", "kind": "review", "role_prompt": "Inspect", "harness": None,
            "profile_id": "profile-acp", "profile_name": "ACP review", "profile_allowed": False}))
        monkeypatch.setattr(planner.db_roles, "get", AsyncMock(return_value=None))
        call = SimpleNamespace(task_id="task", arguments={"template": "inspect"})
        with pytest.raises(OpError, match="ACP"):
            await planner._role_set(call)
    run_db(test, monkeypatch)


def test_saved_task_template_refuses_acp_before_writing(monkeypatch):
    from looper.agents import managers_api
    from looper.agents.projects import RequestError

    async def test(pool):
        await _seed(pool)
        with pytest.raises(RequestError, match="ACP"):
            await managers_api.checked_template_values("scenario-manager", "inspect", {
                "name": "Inspect", "profiles": {"reviewer": "profile-acp"}, "requirement": "Inspect changes"})
    run_db(test, monkeypatch)


def test_acp_prices_never_borrow_builtin_prices_for_a_matching_model_name():
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.profile_registry import LegacyProviderSpec, RuntimeSpec
    from looper.agents.runtimes import ADAPTERS, ApiProtocol, AuthKind
    from tests.runtime_model_helpers import resolved_profile

    provider = LegacyProviderSpec("prov-acp", "Review account", AuthKind.OAUTH,
                                  frozenset({ApiProtocol.OPENAI_RESPONSES}), "acp")
    profile = resolved_profile("profile-acp", "ACP review", RuntimeSpec("rt-acp", "ACP", "acp", "",
                               ADAPTERS["acp"].capabilities), provider, "gpt-6-sol")
    price = Launch(AgentSettings(profile=profile)).price.model_price(profile.model)
    assert price.input_usd_per_mtok is None and price.output_usd_per_mtok is None


@pytest.mark.parametrize("entry", ["profile_create", "profile_update", "profile_retarget", "global_role",
                                   "global_system", "project_role", "project_defaults"])
def test_dashboard_domains_refuse_client_selection_of_acp(monkeypatch, tmp_path, entry):
    from tests.db_support import v1_api

    async def check(pool):
        await _seed(pool)
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project-review', 'Review project')")
        builtin_model = await pool.fetchval("SELECT model_id FROM agent_profiles WHERE id='agent-claude-code-opus'")
        requests = {
            "profile_create": ("profiles.create", {"name": "Client ACP", "runtime_id": "rt-acp", "model_id": "model-acp"}),
            "profile_update": ("profiles.update", {"profile_id": "profile-acp", "fields": {"custom_prompt": "Client change"}}),
            "profile_retarget": ("profiles.update", {"profile_id": "agent-claude-code-opus", "fields": {
                "runtime_id": "rt-acp", "model_id": "model-acp"}}),
            "global_role": ("agent_settings.update_role", {"role": "reviewer", "values": {"profile_id": "profile-acp"}}),
            "global_system": ("agent_settings.update_system", {"values": {"profile_id": "profile-acp"}}),
            "project_role": ("project_agent_settings.update_role", {"project_id": "project-review", "role": "reviewer",
                                                                     "values": {"profile_id": "profile-acp"}}),
            "project_defaults": ("project_agent_settings.update", {"project_id": "project-review",
                                                                     "values": {"profile_id": "profile-acp"}}),
        }
        operation, body = requests[entry]
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call(operation, body, client=True)
            assert status == 403 and answer["error"]["code"] == "forbidden", answer
            assert "ACP" in answer["error"]["message"]
            # A normal Profile still uses this same dashboard operation for a client.
            if entry == "profile_create":
                normal = {"name": "Client regular", "runtime_id": "rt-claude-code", "model_id": builtin_model}
                status, answer = await call("profiles.create", normal, client=True)
                assert status == 200, answer
            # An operator can still configure the Reviewer through its dashboard domain.
            if entry == "global_role":
                status, answer = await call(operation, body)
                assert status == 200, answer
        if entry != "global_role":
            assert await pool.fetchval("SELECT count(*) FROM agent_setting_layers WHERE profile_id='profile-acp'") == 0
        assert await pool.fetchval("SELECT custom_prompt FROM agent_profiles WHERE id='profile-acp'") == ""
    run_db(check, monkeypatch)
