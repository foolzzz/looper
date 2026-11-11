"""Recording routes the API runtime and leaves URL-sensitive runtimes unchanged."""

import asyncio
import json
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest

from looper import child_env
from looper.agents import runtimes
from looper.agents.agent_settings import AgentSettings, AgentType, Launch
from looper.agents.library import AgentLibrary
from looper.agents.llmrec import LlmrecRoute, LlmrecSkip, SkipReason, no_proxy
from looper.agents.llmrec import control
from looper.agents.profile_registry import CatalogModel, LegacyProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, Launcher
from looper.agents.runtimes.api.adapter import ApiRuntime, Descriptor as ApiDescriptor
from looper.agents.runtimes.api.session import ApiSession, FORBIDDEN_ENV
from looper.agents.runtimes.codex import CodexRuntime, config_toml
from looper.agents.runtimes.dsh import DshRuntime, _launch_patch
from looper.agents.runtimes.dsh_runner import DshRunnerRuntime
from looper.agents.runtimes.omp import OmpRuntime, models_yml
from looper.agents.runtimes.pi import PiRuntime, models_json
from looper.agents.runtimes.pi_runner import PiRunnerRuntime
from tests.runtime_model_helpers import resolved_profile

UPSTREAM = "https://model.example.invalid/v1"
ROUTE = LlmrecRoute("abcdefghijklmnopqrstuv", "http://127.0.0.1:12345/r/abcdefghijklmnopqrstuv", UPSTREAM)
UNRECORDED = ("codex", "omp", "pi", "pi-runner", "dsh", "dsh-runner")


def launch_for(case, monkeypatch, *, role=AgentType.MANAGER, base_url=UPSTREAM):
    runtime = {"codex": CodexRuntime, "omp": OmpRuntime, "pi": PiRuntime, "dsh": DshRuntime,
               "api": ApiRuntime, "pi-runner": lambda: PiRunnerRuntime(None, pi=None),
               "dsh-runner": lambda: DshRunnerRuntime(None, dsh=None)}[case]()
    monkeypatch.setitem(runtimes.ADAPTERS, runtime.type, runtime)
    model = "deepseek-flash" if case.startswith("dsh") else "test-model"
    api = ApiProtocol.ANTHROPIC_MESSAGES if case.startswith("dsh") else ApiProtocol.OPENAI_RESPONSES
    provider = LegacyProviderSpec("test-provider", "Test provider", AuthKind.API_KEY, frozenset({api}),
                                  base_url=base_url, api_key="test-model-key",
                                  catalog=(CatalogModel(model, context_window=128000),))
    profile = resolved_profile("test-agent", "Test agent", RuntimeSpec("test-runtime", runtime.label, runtime.type),
                               provider, model, "high")
    return Launch(AgentSettings(profile=profile), role=role)


def configuration(case, launch, tmp_path):
    if case == "codex":
        return config_toml(launch)
    if case == "omp":
        value = models_yml(launch)
    elif case == "pi":
        value = models_json(launch)
    elif case.endswith("-runner"):
        value = asdict(launch.runtime._descriptor(launch, tmp_path / "prompt.md"))
    elif case == "dsh":
        value = _launch_patch(launch, tmp_path)
    else:
        value = launch.environment()
    return json.dumps(value, sort_keys=True)


@pytest.mark.parametrize("case", UNRECORDED)
@pytest.mark.parametrize("role", [AgentType.MANAGER, AgentType.PLANNER, AgentType.WORKER, AgentType.REVIEWER])
def test_url_sensitive_runtime_keeps_launch_and_marks_run_not_covered(case, role, tmp_path, monkeypatch):
    monkeypatch.setattr(child_env, "inherited", lambda: {
        "NO_PROXY": "upper.invalid", "no_proxy": "lower.invalid", "HTTPS_PROXY": "http://proxy.invalid:8080"})
    launch = launch_for(case, monkeypatch, role=role)
    if case == "dsh" and role in (AgentType.MANAGER, AgentType.PLANNER):
        mcp = tmp_path / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"looper": {"command": "test-bridge", "env": {
            "LOOPER_AGENT_URL": "http://127.0.0.1:12346", "LOOPER_AGENT_TOKEN": "test-token"}}}}))
        launch = replace(launch, library=AgentLibrary(mcp_config=str(mcp)))
    owner = control.ProxyControl(tmp_path, "", enabled=False)
    health = AsyncMock(side_effect=AssertionError("not-covered must not need proxy"))
    monkeypatch.setattr(owner, "_health", health)
    off = asyncio.run(owner.attach(launch, agent_id="test-agent", task_id=None, reuse=True))
    assert off is launch
    assert "llmrec" not in off.record()
    owner.enabled = True
    on = asyncio.run(owner.attach(launch, agent_id="test-agent", task_id=None, reuse=True))
    assert configuration(case, on, tmp_path) == configuration(case, off, tmp_path)
    assert on.environment() == off.environment()
    assert on.args() == off.args()
    assert on.binding_sha256 == off.binding_sha256
    assert on.record()["llmrec"] == {
        "state": "skipped", "reason": "not_covered",
        "detail": f"{launch.runtime.type} picks request details from the endpoint URL; "
                  "recording through the proxy would change its requests",
    }
    assert on.runtime.llmrec_upstream(on) is None
    health.assert_not_awaited()
    assert not owner._resident


def test_api_routed_environment_keeps_the_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(child_env, "inherited", lambda: {"NO_PROXY": "upper.invalid", "no_proxy": "lower.invalid"})
    launch = launch_for("api", monkeypatch)
    routed = replace(launch, llmrec=ROUTE)
    assert ROUTE.url in configuration("api", routed, tmp_path)
    skipped = replace(launch, llmrec=LlmrecSkip(SkipReason.PROXY_UNAVAILABLE))
    assert skipped.environment() == launch.environment()
    assert routed.binding_sha256 == launch.binding_sha256
    assert routed.runtime.llmrec_upstream(routed) == UPSTREAM
    for name in ("NO_PROXY", "no_proxy"):
        assert "127.0.0.1" in routed.environment()[name].split(",")
    assert "LOOPER_LLMREC_BASE_URL" not in launch.environment()


@pytest.mark.parametrize("initial,expected", [
    ({}, "127.0.0.1,localhost,::1"),
    ({"NO_PROXY": "example.invalid"}, "example.invalid,127.0.0.1,localhost,::1"),
    ({"no_proxy": "127.0.0.1,lower.invalid"}, "127.0.0.1,lower.invalid,localhost,::1"),
    ({"NO_PROXY": "*"}, "*"),
    ({"NO_PROXY": "upper.invalid", "no_proxy": "lower.invalid"}, "lower.invalid,127.0.0.1,localhost,::1"),
    ({"NO_PROXY": "*", "no_proxy": ""}, "127.0.0.1,localhost,::1"),
    ({"no_proxy": " lower.invalid ,lower.invalid,127.0.0.1"}, "lower.invalid,127.0.0.1,localhost,::1"),
])
def test_loopback_bypass_preserves_inheritance_and_is_idempotent(initial, expected):
    expected = {"NO_PROXY": expected, "no_proxy": expected}
    assert no_proxy(initial) == expected
    assert no_proxy(initial) == expected


@pytest.mark.parametrize("runtime_type,oauth,launcher,detail", [
    ("codex", True, Launcher.NATIVE, "codex picks request details from the endpoint URL; "
     "recording through the proxy would change its requests"),
    ("codex", True, Launcher.TTADK, "TTADK manages its own provider"),
    ("claude-code", True, Launcher.TTADK, "TTADK manages its own provider"),
    ("claude-code", False, Launcher.TTADK, "TTADK manages its own provider"),
    ("grok", True, Launcher.NATIVE, "Grok has no known endpoint override"),
    ("cursor-agent", True, Launcher.NATIVE, "Cursor's servers make the model requests"),
    ("coco", True, Launcher.NATIVE, "TRAE's servers make the model requests"),
])
@pytest.mark.parametrize("include_oauth", [False, True])
def test_not_covered_details(runtime_type, oauth, launcher, detail, include_oauth, tmp_path, monkeypatch):
    if runtime_type == "coco":
        from looper.agents.runtimes import coco_login

        monkeypatch.setattr(coco_login, "available_models", lambda executable: {"test-model": "test-model"})
    api = ApiProtocol.ANTHROPIC_MESSAGES if runtime_type == "claude-code" else ApiProtocol.OPENAI_RESPONSES
    provider = LegacyProviderSpec("test-login", "Test login", AuthKind.OAUTH if oauth else AuthKind.API_KEY,
                                  frozenset({api}), oauth_runtime_type=runtime_type if oauth else None)
    profile = resolved_profile("test-agent", "Test agent", RuntimeSpec("test-runtime", runtime_type, runtime_type,
                               launcher=launcher), provider, "test-model")
    launch = Launch(AgentSettings(profile=profile))
    owner = control.ProxyControl(tmp_path, "", enabled=True, include_oauth=include_oauth)
    monkeypatch.setattr(owner, "_health", AsyncMock(side_effect=AssertionError("not-covered must not need proxy")))
    attached = asyncio.run(owner.attach(launch, agent_id="test-agent", task_id=None))
    assert attached.llmrec == LlmrecSkip(SkipReason.NOT_COVERED, detail)
    assert launch.runtime.llmrec_upstream(launch) is None


def test_api_session_routes_the_target_but_preserves_its_descriptor(tmp_path, monkeypatch):
    launch = launch_for("api", monkeypatch)
    routed = replace(launch, llmrec=ROUTE)
    prompt = tmp_path / ".local" / "prompts" / "prompt.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("Test instructions")
    command = routed.runtime.resident_command("", cwd=tmp_path, prompt_file=prompt, launch=routed)
    descriptor = ApiDescriptor.parse(command)
    assert descriptor.base_url == UPSTREAM
    for name in FORBIDDEN_ENV:
        monkeypatch.delenv(name, raising=False)
    session = ApiSession(label="test-manager")
    asyncio.run(session.start(command, cwd=str(tmp_path), signature="test", env=routed.environment()))
    assert session._target.base_url == ROUTE.url
    assert session._descriptor == descriptor
    assert routed.record()["api"]["base_url_host"] == "model.example.invalid"
