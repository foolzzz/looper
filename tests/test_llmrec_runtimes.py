"""Recording changes the transport endpoint, preserving provider binding and off-mode launches."""

import asyncio
import json
import shutil
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace

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
from looper.agents.runtimes.runner import protocol
from tests.runtime_model_helpers import resolved_profile

UPSTREAM = "https://model.example.invalid/v1"
ROUTE = LlmrecRoute("abcdefghijklmnopqrstuv", "http://127.0.0.1:12345/r/abcdefghijklmnopqrstuv", UPSTREAM)
CASES = ("codex", "omp", "pi", "pi-runner", "dsh", "dsh-runner", "api")


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


@pytest.mark.parametrize("case,role", [(case, role) for case in CASES for role in AgentType
                                       if role in (AgentType.MANAGER, AgentType.PLANNER,
                                                   AgentType.WORKER, AgentType.REVIEWER)
                                       and (case != "api" or role is AgentType.MANAGER)])
def test_routed_config_and_environment_keep_the_binding(case, role, tmp_path, monkeypatch):
    monkeypatch.setattr(child_env, "inherited", lambda: {"NO_PROXY": "upper.invalid", "no_proxy": "lower.invalid"})
    launch = launch_for(case, monkeypatch, role=role)
    if case == "dsh" and role in (AgentType.MANAGER, AgentType.PLANNER):
        mcp = tmp_path / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"looper": {"command": "test-bridge", "env": {
            "LOOPER_AGENT_URL": "http://127.0.0.1:12346", "LOOPER_AGENT_TOKEN": "test-token"}}}}))
        launch = replace(launch, library=AgentLibrary(mcp_config=str(mcp)))
    routed = replace(launch, llmrec=ROUTE)
    off = configuration(case, launch, tmp_path)
    recorded = configuration(case, routed, tmp_path)
    assert ROUTE.url in recorded and UPSTREAM not in recorded
    if case != "api":
        assert recorded.replace(ROUTE.url, UPSTREAM) == off
    skipped = replace(launch, llmrec=LlmrecSkip(SkipReason.PROXY_UNAVAILABLE))
    assert configuration(case, skipped, tmp_path) == off
    assert skipped.environment() == launch.environment()
    assert routed.binding_sha256 == launch.binding_sha256
    expected_upstream = "https://model.example.invalid" if case.startswith("dsh") else UPSTREAM
    assert routed.runtime.llmrec_upstream(routed) == expected_upstream
    env = routed.environment()
    for name in ("NO_PROXY", "no_proxy"):
        assert "127.0.0.1" in env[name].split(",")
        if case != "api":
            assert launch.environment()[name] in env[name].split(",")
    assert "LOOPER_LLMREC_BASE_URL" not in launch.environment()


@pytest.mark.parametrize("case,expected", [("codex", "https://api.openai.com/v1"),
                                           ("omp", "https://api.openai.com/v1")])
def test_runtime_default_upstream_is_preserved(case, expected, monkeypatch, tmp_path):
    launch = launch_for(case, monkeypatch, base_url="")
    assert launch.runtime.llmrec_upstream(launch) == expected
    assert expected in configuration(case, launch, tmp_path)
    assert expected not in configuration(case, replace(launch, llmrec=ROUTE), tmp_path)


@pytest.mark.parametrize("case", ["dsh", "dsh-runner"])
@pytest.mark.parametrize("role", [AgentType.MANAGER, AgentType.PLANNER])
@pytest.mark.parametrize("base_path,expected_path", [
    ("/anthropic", "/anthropic/v1/messages"),
    ("/anthropic/v1", "/anthropic/v1/messages"),
    ("/anthropic/v1/", "/anthropic/v1/messages"),
    ("/v1/", "/v1/messages"),
    ("/anthropic/v10/", "/anthropic/v10/v1/messages"),
])
def test_dsh_recording_preserves_messages_endpoint(case, role, base_path, expected_path, tmp_path, monkeypatch):
    import httpx
    from looper.agents.llmrec.proxy import Proxy
    from tests.fake_model_api import FakeModelAPI, ResponseScript
    from tests.test_llmrec_proxy import Sink, captured, events

    # Isolate both direct requests and route transport from the operator's proxy settings.
    monkeypatch.setattr(child_env, "inherited", lambda: {})

    def messages_url(launch):
        if case == "dsh":
            config = next(row["config"] for row in _launch_patch(launch, tmp_path) if row["id"] == "llm-deepseek")
            base = config["baseURL"]
        else:
            base = launch.runtime._descriptor(launch, tmp_path / "prompt.md").base_url
        # DSH's messagesApiRoot adds /v1 only when the configured root lacks it.
        base = base.rstrip("/")
        return base + ("" if base.endswith("/v1") else "/v1") + "/messages"

    async def check():
        async with FakeModelAPI([ResponseScript(events=events()) for _ in range(2)],
                                api_key="test-model-key") as upstream:
            launch = launch_for(case, monkeypatch, role=role,
                                base_url=upstream.base_url.removesuffix("/v1") + base_path)
            if case == "dsh":
                mcp = tmp_path / "mcp.json"
                mcp.write_text(json.dumps({"mcpServers": {"looper": {"command": "test-bridge", "env": {
                    "LOOPER_AGENT_URL": "http://127.0.0.1:12346", "LOOPER_AGENT_TOKEN": "test-token"}}}}))
                launch = replace(launch, library=AgentLibrary(mcp_config=str(mcp)))
            direct_url = messages_url(launch)
            sink = Sink()
            proxy = Proxy(tmp_path, sink=sink)
            port = await proxy.start()
            owner = control.ProxyControl(tmp_path, "", enabled=True)
            monkeypatch.setattr(owner, "_health", AsyncMock(return_value=True))
            monkeypatch.setattr(owner, "_state", lambda: {"port": port})
            try:
                routed = await owner.attach(launch, agent_id=role.value, task_id=None, reuse=True)
                assert isinstance(routed.llmrec, LlmrecRoute)
                assert routed.binding_sha256 == launch.binding_sha256
                body = {"model": launch.model, "messages": [{"role": "user", "content": "Hello"}]}
                async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                    for url in (direct_url, messages_url(routed)):
                        response = await client.post(url, json=body,
                                                     headers={"Authorization": "Bearer test-model-key"})
                        assert response.status_code == 200
                capture = await captured(sink)
                assert [request.path for request in upstream.requests] == [expected_path, expected_path]
                assert all(request.body == body for request in upstream.requests)
                assert capture.path == expected_path
                assert capture.record_gap is None
                assert messages_url(launch) == direct_url
                assert await owner.attach(launch, agent_id=role.value, task_id=None, reuse=True) == routed
            finally:
                await proxy.close(grace_s=0)

    asyncio.run(asyncio.wait_for(check(), timeout=10))


@pytest.mark.parametrize("initial,expected", [
    ({}, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}),
    ({"NO_PROXY": "example.invalid"}, {"NO_PROXY": "example.invalid,127.0.0.1", "no_proxy": "example.invalid,127.0.0.1"}),
    ({"no_proxy": "127.0.0.1,lower.invalid"}, {"NO_PROXY": "127.0.0.1,lower.invalid", "no_proxy": "127.0.0.1,lower.invalid"}),
    ({"NO_PROXY": "*"}, {"NO_PROXY": "*", "no_proxy": "*"}),
])
def test_loopback_bypass_preserves_inheritance_and_is_idempotent(initial, expected):
    assert no_proxy(initial) == expected
    assert no_proxy(initial) == expected


@pytest.mark.parametrize("runtime_type,oauth,launcher,detail", [
    ("codex", True, Launcher.NATIVE, "Codex's ChatGPT login requires an HTTPS origin"),
    ("codex", True, Launcher.TTADK, "TTADK manages its own provider"),
    ("claude-code", True, Launcher.TTADK, "TTADK manages its own provider"),
    ("claude-code", False, Launcher.TTADK, "TTADK manages its own provider"),
    ("grok", True, Launcher.NATIVE, "Grok has no known endpoint override"),
    ("cursor-agent", True, Launcher.NATIVE, "Cursor's servers make the model requests"),
])
@pytest.mark.parametrize("include_oauth", [False, True])
def test_not_covered_details(runtime_type, oauth, launcher, detail, include_oauth, tmp_path, monkeypatch):
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


@pytest.mark.parametrize("kind", ["pi", "dsh"])
def test_runner_protocol_accepts_route_url_in_python_and_node(kind, tmp_path):
    root = Path(__file__).resolve().parents[1]
    case = next(case for case in json.loads((root / "runners/protocol/cases.json").read_text()) if case["name"] == "new")
    params = case["message"]["params"]
    params.update(cwd=str(tmp_path), home=str(tmp_path), state_dir=str(tmp_path))
    params["provider"].update(base_url=ROUTE.url, protocol="anthropic-messages" if kind == "dsh" else "openai-responses")
    params["system_prompt"]["mode"] = "append"
    params["tools"]["builtin"] = ["read"]
    assert protocol.decode_params("session/new", params)["provider"]["base_url"] == ROUTE.url
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed; Python route URL validation passed")
    module = root / "looper/agents/runtimes/runner" / ("pi_runner" if kind == "pi" else "dsh_runner") / "src/session.ts"
    # A platform-tool refusal is after endpoint validation and before any SDK import/use.
    script = """
        const { newSession } = await import(process.argv[1]);
        const params = JSON.parse(process.argv[2]);
        const codes = [];
        for (const url of [params.provider.base_url, '']) {
          params.provider.base_url = url;
          try { await newSession(params); codes.push('unexpected-success'); }
          catch (error) { codes.push(error.code); }
        }
        process.stdout.write(JSON.stringify(codes));
    """
    result = subprocess.run([node, "--input-type=module", "-e", script, module.as_uri(), json.dumps(params)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15, check=True)
    assert json.loads(result.stdout) == [-32007, -32602]


@pytest.mark.installed
@pytest.mark.parametrize("case", ["pi", "pi-runner"])
@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://api.moonshot.cn/v1",
                                      "https://api.moonshot.ai/v1"])
@pytest.mark.parametrize("effort", ["high", "off"])
def test_pi_recording_preserves_provider_wire_requests(case, base_url, effort, tmp_path, monkeypatch):
    from tests.pi_runner_support import require_pi
    from tests.pi_runtime_support import launch as pi_launch, provider
    from looper.agents.runtimes.runner.session import Descriptor, RunnerSession

    node, pi = require_pi()
    selected = replace(pi_launch(provider(protocols=frozenset({ApiProtocol.OPENAI_CHAT}), base_url=base_url,
                                  catalog=(CatalogModel("selected", context_window=128000,
                                                        efforts=("off", "high")),)), effort=effort),
                       role=AgentType.MANAGER)
    runtime = PiRuntime() if case == "pi" else PiRunnerRuntime(None, pi=None)
    monkeypatch.setitem(runtimes.ADAPTERS, "pi", runtime)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Test instructions")
    configs = []
    for route in (None, replace(ROUTE, upstream=base_url)):
        launch = replace(selected, llmrec=route)
        if case == "pi":
            configs.append(models_json(launch))
        else:
            # Exercise the descriptor argv, host serialization and protocol decoder too.
            descriptor = Descriptor.parse(runtime._descriptor(launch, prompt).to_argv())
            lease = SimpleNamespace(open_session=AsyncMock(), release=AsyncMock())
            session = RunnerSession("test", SimpleNamespace(acquire=AsyncMock(return_value=lease)), "pi")
            asyncio.run(session.start(descriptor.to_argv(), cwd=str(tmp_path), signature=1,
                                      env={"LOOPER_PROVIDER_API_KEY": "test-key"}))
            params = {**lease.open_session.call_args.args[0], "session_key": "test",
                      "state_dir": str(tmp_path), "home": str(tmp_path)}
            configs.append(protocol.decode_params("session/new", params))
    config_path = tmp_path / "configs.json"
    config_path.write_text(json.dumps(configs))
    module = Path(__file__).resolve().parents[1] / "looper/agents/runtimes/runner/pi_runner/src"
    script = r"""
        import { readFile, mkdir, writeFile } from 'node:fs/promises';
        import { pathToFileURL } from 'node:url';
        const [module, pi, caseName, effort, configPath] = process.argv.slice(1);
        const { load } = await import(module + '/sdk.ts');
        const sdk = await load(pi);
        const { newSession, sessions, release } = await import(module + '/session.ts');
        const configs = JSON.parse(await readFile(configPath, 'utf8'));
        const requests = [];
        for (const config of configs) {
          let runtime, record, model;
          if (caseName === 'pi') {
            // Literal test key: config apiKey normally references the isolated child environment.
            config.providers.looper.apiKey = 'test-key';
            const modelsPath = new URL('./models.json', pathToFileURL(configPath));
            await writeFile(modelsPath, JSON.stringify(config));
            runtime = await sdk.agent.ModelRuntime.create({credentials: new sdk.ai.InMemoryCredentialStore(),
              modelsPath: modelsPath.pathname, refreshOnCreate: false, allowModelNetwork: false});
            if (runtime.getError()) throw new Error(runtime.getError());
          } else {
            await mkdir('sessions/test/agent', {recursive: true});
            const {session_id} = await newSession(config);
            record = sessions.get(session_id);
            runtime = record.runtime;
          }
          try {
            model = runtime.getModel('looper', 'selected');
            const context = {messages: [
              {role: 'system', content: 'Test instructions', timestamp: 0,
                toolsAdded: [{name: 'read', description: 'Read a file', parameters: {
                  type: 'object', properties: {path: {type: 'string'}}, required: ['path']}}]},
              {role: 'user', content: 'Read a file', timestamp: 0},
              {role: 'assistant', api: model.api, provider: model.provider, model: model.id,
                content: [{type: 'toolCall', id: 'call_1', name: 'read', arguments: {path: 'a'}}],
                usage: {input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2},
                stopReason: 'toolUse', timestamp: 0},
              {role: 'toolResult', toolCallId: 'call_1', toolName: 'read',
                content: [{type: 'text', text: 'file contents'}], isError: false, timestamp: 0},
              {role: 'user', content: 'Continue', timestamp: 0}
            ]};
            const result = await sdk.completions.streamSimple(model, context, {
              apiKey: 'test-key', reasoning: effort, maxTokens: 1234, sessionId: 'test',
              fetch: async (url, init) => {
                requests.push({url: String(url), body: JSON.parse(init.body)});
                return new Response('data: ' + JSON.stringify({id: 'chat_1', choices: [
                  {index: 0, delta: {content: 'Done'}, finish_reason: 'stop'}],
                  usage: {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2}}) + '\n\ndata: [DONE]\n\n',
                  {headers: {'Content-Type': 'text/event-stream'}});
              }
            }).result();
            if (result.stopReason === 'error') throw new Error(result.errorMessage);
          } finally { if (record) release(record); else runtime.dispose?.(); }
        }
        process.stdout.write(JSON.stringify(requests));
    """
    result = subprocess.run([node, "--input-type=module", "-e", script, module.as_uri(), pi, case,
                             effort, str(config_path)], cwd=tmp_path, capture_output=True, text=True,
                            timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    direct, recorded = json.loads(result.stdout)
    assert direct["url"] == base_url + "/chat/completions"
    assert recorded["url"] == ROUTE.url + "/chat/completions"
    assert direct["body"]["messages"][0]["role"] == "system"
    assert "store" not in direct["body"]
    assert direct["body"]["max_tokens"] == 1234
    if "deepseek" in base_url:
        assert direct["body"]["thinking"]["type"] == ("enabled" if effort == "high" else "disabled")
        assert direct["body"]["messages"][2]["reasoning_content"] == ""
    else:
        assert "reasoning_effort" not in direct["body"]
    assert recorded["body"] == direct["body"]
