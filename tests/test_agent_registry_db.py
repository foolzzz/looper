"""Runtimes, LLM providers and agents against a real PostgreSQL, through the HTTP API.

Runs only when LOOPX_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards. A stand-in runtime
type (``StandIn``) plays a second adapter: one that cannot keep a resident
session, and has a login of its own.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fake_claude import fake_claude
from tests.test_catalog_db import DSN, _fresh_schema, _Writer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")

SECRET = "sk-test-registry-0123456789"


def _run(test: Callable[[Any], Awaitable[None]], monkeypatch: pytest.MonkeyPatch, *, before: str = "") -> None:
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            if before:
                await pool.execute(before)
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await test(pool)

    asyncio.run(_main())


async def _call(method: str, path: str, body: Any = None, *, claude: str = "claude") -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())), claude_bin=claude)
    writer = _Writer()
    await server._route_api(method, path, {}, b"" if body is None else json.dumps(body).encode(), writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


async def _ok(method: str, path: str, body: Any = None, **kwargs: Any) -> Any:
    status, payload = await _call(method, path, body, **kwargs)
    assert status in (200, 201), (path, payload)
    return payload


def _version_claude(directory: Path, version: str = "2.1.285 (Claude Code)") -> str:
    """A stand-in claude that prints ``version`` for ``--version`` and fails on anything else."""
    return fake_claude(directory / "claude-versioned", (
        "import sys\n"
        f"if sys.argv[1:] == ['--version']:\n    print({version!r})\n    sys.exit(0)\n"
        "sys.exit(9)\n"
    ))


@pytest.fixture
def stand_in(monkeypatch: pytest.MonkeyPatch) -> str:
    """A second runtime type: one-off runs only, anthropic-messages, its own login and API keys."""
    from looper.agents import runtimes
    from looper.agents.runtimes import AuthKind, Capability, Probe
    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime

    class StandIn(ClaudeCodeRuntime):
        type = "stand-in"
        label = "Stand-in"
        default_executable = "stand-in"
        capabilities = frozenset({Capability.RUN})
        auth_kinds = frozenset({AuthKind.OAUTH, AuthKind.API_KEY})

        def probe(self, executable: str) -> Probe:
            return Probe("0.1", self.capabilities)

    monkeypatch.setitem(runtimes.ADAPTERS, StandIn.type, StandIn())
    return StandIn.type


async def _registry() -> dict[str, Any]:
    return await _ok("GET", "/api/registry")


def _by_name(entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(e for e in entries if e["name"] == name)


# --- The built-ins ---


def test_every_database_has_the_builtin_runtime_provider_and_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.agent_registry import BUILTIN_AGENT, AgentSpec
    from looper.agents.runtimes import CLAUDE_CODE
    from looper.db import agent_registry as db
    from looper.db.connection import ensure_schema

    async def _test(pool: Any) -> None:
        await pool.execute("DELETE FROM agents WHERE builtin")  # a missing one comes back
        await ensure_schema(pool)
        await ensure_schema(pool)
        (agent,) = await db.list_agents()
        assert AgentSpec.from_row(agent) == BUILTIN_AGENT
        registry = await _registry()
        (runtime,) = registry["runtimes"]
        assert (runtime["builtin"], runtime["executable"], runtime["version"]) == (True, "", None)
        assert set(runtime["capabilities"]) == {c.value for c in CLAUDE_CODE.capabilities}
        (provider,) = registry["providers"]
        assert (provider["auth"], provider["oauth_runtime_type"], provider["api_key"]) == ("oauth", "claude-code", None)

    _run(_test, monkeypatch)


@pytest.mark.parametrize(("method", "path", "body"), [
    ("PUT", "/api/runtimes/rt-claude-code", {"executable": "/opt/claude"}),
    ("DELETE", "/api/runtimes/rt-claude-code", None),
    ("PUT", "/api/providers/prov-claude-subscription", {"models": [{"name": "opus"}]}),
    ("DELETE", "/api/providers/prov-claude-subscription", None),
    ("PUT", "/api/agents/agent-claude-code-opus", {"model": "sonnet"}),
    ("DELETE", "/api/agents/agent-claude-code-opus", None),
])
def test_the_builtins_are_read_only(monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any) -> None:
    async def _test(_pool: Any) -> None:
        status, payload = await _call(method, path, body)
        assert status == 400 and "built-in" in payload["error"]

    _run(_test, monkeypatch)


# --- Runtimes: registered and probed ---


def test_a_runtime_is_probed_with_version_only_when_registered_and_on_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.runtimes import CLAUDE_CODE

    claude = _version_claude(tmp_path)

    async def _test(_pool: Any) -> None:
        added = (await _ok("POST", "/api/runtimes", {"name": "claude-next", "type": "claude-code",
                                                     "executable": claude}))["runtime"]
        assert (added["version"], added["probe_error"]) == ("2.1.285 (Claude Code)", None)
        assert set(added["capabilities"]) == {c.value for c in CLAUDE_CODE.capabilities}

        missing = (await _ok("POST", "/api/runtimes", {"name": "nowhere", "type": "claude-code",
                                                       "executable": str(tmp_path / "absent")}))["runtime"]
        assert missing["version"] is None and "was not found" in missing["probe_error"]

        # The built-in one probes the server's claude.
        builtin = (await _ok("POST", "/api/runtimes/rt-claude-code/probe", claude=claude))["runtime"]
        assert builtin["version"] == "2.1.285 (Claude Code)" and builtin["probed_at"]

        status, payload = await _call("POST", "/api/runtimes", {"name": "claude-next", "type": "claude-code"})
        assert status == 409
        status, payload = await _call("POST", "/api/runtimes", {"name": "x", "type": "emacs"})
        assert status == 400 and "no runtime type 'emacs'" in payload["error"]

    _run(_test, monkeypatch)


# --- Providers: secrets stored apart and masked ---


def test_a_provider_api_key_is_stored_apart_and_never_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        provider = (await _ok("POST", "/api/providers", {
            "name": "anthropic-key", "protocols": ["anthropic-messages"], "auth": "api_key", "api_key": SECRET,
            "base_url": "https://api.anthropic.example", "models": [{"name": "claude-sonnet-4-5",
                                                                     "input_usd_per_mtok": 3}]}))["provider"]
        provider_id = provider["provider_id"]
        assert provider["api_key"] == "***"
        assert SECRET not in json.dumps(await _registry())
        assert await pool.fetchval("SELECT api_key FROM llm_provider_secrets WHERE provider_id = $1",
                                   provider_id) == SECRET
        assert SECRET not in json.dumps([dict(r) for r in await pool.fetch("SELECT * FROM llm_providers")],
                                        default=str)

        # *** keeps the stored key; another value replaces it; null removes it.
        await _ok("PUT", f"/api/providers/{provider_id}", {"api_key": "***", "base_url": ""})
        assert await pool.fetchval("SELECT api_key FROM llm_provider_secrets") == SECRET
        await _ok("PUT", f"/api/providers/{provider_id}", {"api_key": "sk-other"})
        assert await pool.fetchval("SELECT api_key FROM llm_provider_secrets") == "sk-other"
        cleared = (await _ok("PUT", f"/api/providers/{provider_id}", {"api_key": None}))["provider"]
        assert cleared["api_key"] is None and await pool.fetchval("SELECT count(*) FROM llm_provider_secrets") == 0

    _run(_test, monkeypatch)


@pytest.mark.parametrize(("body", "error"), [
    ({"protocols": [], "auth": "api_key"}, "non-empty list"),
    ({"protocols": ["grpc"], "auth": "api_key"}, "non-empty list"),
    ({"protocols": ["openai-chat"], "auth": "token"}, "auth must be one of"),
    ({"protocols": ["anthropic-messages"], "auth": "oauth"}, "no runtime type"),
    ({"protocols": ["anthropic-messages"], "auth": "oauth", "oauth_runtime_type": "claude-code", "api_key": "k"},
     "takes no api_key"),
    ({"protocols": ["openai-chat"], "auth": "api_key", "base_url": "ftp://x"}, "base_url must be"),
    ({"protocols": ["openai-chat"], "auth": "api_key", "models": [{"name": "m"}, {"name": "m"}]}, "listed twice"),
])
def test_a_provider_that_does_not_validate_is_refused(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], error: str,
) -> None:
    async def _test(_pool: Any) -> None:
        status, payload = await _call("POST", "/api/providers", {"name": "p", **body})
        assert status == 400 and error in payload["error"], payload

    _run(_test, monkeypatch)


# --- Agents: checked when created or changed ---


async def _provider(body: dict[str, Any]) -> str:
    return (await _ok("POST", "/api/providers", body))["provider"]["provider_id"]


def test_an_agent_pairs_a_runtime_and_a_provider_that_can_run_together(
    monkeypatch: pytest.MonkeyPatch, stand_in: str,
) -> None:
    async def _test(_pool: Any) -> None:
        standin = (await _ok("POST", "/api/runtimes", {"name": "si", "type": stand_in}))["runtime"]["runtime_id"]
        chat = await _provider({"name": "openai-chat", "protocols": ["openai-chat"], "auth": "api_key"})
        key = await _provider({"name": "anthropic-key", "protocols": ["anthropic-messages"], "auth": "api_key",
                               "models": [{"name": "claude-sonnet-4-5"}]})
        login = await _provider({"name": "stand-in-login", "protocols": ["anthropic-messages"], "auth": "oauth",
                                 "oauth_runtime_type": stand_in})

        async def agent(runtime_id: str, provider_id: str, model: str = "claude-sonnet-4-5") -> tuple[int, Any]:
            return await _call("POST", "/api/agents", {"name": f"a-{runtime_id}-{provider_id}-{model}",
                                                       "runtime_id": runtime_id, "provider_id": provider_id,
                                                       "model": model})

        status, payload = await agent("rt-claude-code", "prov-claude-subscription", "sonnet")
        assert status == 201 and payload["agent"]["runtime_name"] == "claude-code"
        assert (await agent(standin, key))[0] == 201  # an adapter that takes API keys
        assert (await agent(standin, login))[0] == 201  # its own login

        # Protocols must intersect.
        status, payload = await agent("rt-claude-code", chat)
        assert status == 400 and "share no protocol" in payload["error"]
        # An OAuth provider pairs only with the runtime type whose login it is, either way round.
        status, payload = await agent(standin, "prov-claude-subscription", "sonnet")
        assert status == 400 and "login of claude-code runtimes" in payload["error"]
        status, payload = await agent("rt-claude-code", login)
        assert status == 400 and "login of stand-in runtimes" in payload["error"]
        # Claude Code runs only with its own login for now.
        status, payload = await agent("rt-claude-code", key)
        assert status == 400 and "cannot be started with an api_key provider" in payload["error"]
        # A provider that lists its models takes no other.
        status, payload = await agent(standin, key, "claude-opus-4-5")
        assert status == 400 and "not one of them" in payload["error"]

    _run(_test, monkeypatch)


def test_a_change_that_would_break_an_agent_is_refused_and_one_in_use_is_kept(
    monkeypatch: pytest.MonkeyPatch, stand_in: str,
) -> None:
    async def _test(_pool: Any) -> None:
        runtime_id = (await _ok("POST", "/api/runtimes", {"name": "si", "type": stand_in}))["runtime"]["runtime_id"]
        provider_id = await _provider({"name": "key", "protocols": ["anthropic-messages"], "auth": "api_key"})
        agent = (await _ok("POST", "/api/agents", {"name": "si-sonnet", "runtime_id": runtime_id,
                                                   "provider_id": provider_id, "model": "sonnet",
                                                   "effort": "low"}))["agent"]

        status, payload = await _call("PUT", f"/api/providers/{provider_id}", {"protocols": ["openai-chat"]})
        assert status == 400 and "agent 'si-sonnet' uses this provider" in payload["error"]
        status, payload = await _call("PUT", f"/api/agents/{agent['agent_id']}",
                                      {"provider_id": "prov-claude-subscription"})
        assert status == 400 and "login of claude-code runtimes" in payload["error"]
        renamed = (await _ok("PUT", f"/api/agents/{agent['agent_id']}", {"name": "si-fast", "effort": None}))["agent"]
        assert (renamed["name"], renamed["effort"]) == ("si-fast", None)

        for path in (f"/api/runtimes/{runtime_id}", f"/api/providers/{provider_id}"):
            status, payload = await _call("DELETE", path)
            assert status == 409 and "first" in payload["error"]
        await _ok("DELETE", f"/api/agents/{agent['agent_id']}")
        await _ok("DELETE", f"/api/providers/{provider_id}")
        await _ok("DELETE", f"/api/runtimes/{runtime_id}")

    _run(_test, monkeypatch)


# --- LLM profiles are gone ---

# What a database had before agents: LLM profiles and the two agent config columns that pointed at them.
_BEFORE_AGENTS = """
CREATE TABLE llm_profiles (id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, provider_kind TEXT NOT NULL,
                           model TEXT NOT NULL DEFAULT '');
CREATE TABLE agent_configs (agent_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                            role TEXT NOT NULL DEFAULT '', runtime TEXT NOT NULL DEFAULT 'claude-code',
                            llm_profile_id BIGINT REFERENCES llm_profiles(id), enabled BOOLEAN NOT NULL DEFAULT true,
                            system_prompt TEXT NOT NULL DEFAULT '', max_turns INTEGER NOT NULL DEFAULT 0,
                            timeout_seconds INTEGER NOT NULL DEFAULT 1200, max_concurrency INTEGER NOT NULL DEFAULT 1,
                            extra_args JSONB, metadata JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO llm_profiles (name, provider_kind, model) VALUES ('anthropic-default', 'anthropic', 'claude-sonnet-4');
INSERT INTO agent_configs (agent_id, role, runtime, llm_profile_id, system_prompt)
VALUES ('worker', 'worker', 'codex-cli', 1, 'Work.');
"""


def test_the_upgrade_removes_llm_profiles_and_keeps_the_agent_configs(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db.connection import ensure_schema

    async def _test(pool: Any) -> None:
        await ensure_schema(pool)  # once only
        assert await pool.fetchval("SELECT to_regclass('llm_profiles')") is None
        columns = {r["column_name"] for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'agent_configs'")}
        assert not columns & {"runtime", "llm_profile_id"}
        assert await pool.fetchval("SELECT system_prompt FROM agent_configs WHERE agent_id = 'worker'") == "Work."
        assert await pool.fetchval("SELECT 1 FROM schema_migrations WHERE name = 'llm_profiles_removed'")

        assert (await _call("GET", "/api/llm-profiles"))[0] == 404
        status, payload = await _call("PUT", "/api/agent-configs/worker", {"runtime": "codex-cli"})
        assert status == 400 and "runtime was removed" in payload["error"]

    _run(_test, monkeypatch, before=_BEFORE_AGENTS)


# --- Selecting an agent: the layers and a task's overrides, checked for the roles they reach ---


async def _agent(name: str, runtime_id: str = "rt-claude-code", provider_id: str = "prov-claude-subscription",
                 model: str | None = None) -> str:
    return (await _ok("POST", "/api/agents", {"name": name, "runtime_id": runtime_id, "provider_id": provider_id,
                                              "model": model or name.rsplit("/", 1)[-1]}))["agent"]["agent_id"]


async def _stand_in_agent(stand_in: str) -> str:
    runtime_id = (await _ok("POST", "/api/runtimes", {"name": "si", "type": stand_in}))["runtime"]["runtime_id"]
    provider_id = (await _ok("POST", "/api/providers", {"name": "si-key", "protocols": ["anthropic-messages"],
                                                        "auth": "api_key"}))["provider"]["provider_id"]
    return await _agent("si/sonnet", runtime_id, provider_id, "sonnet")


def test_a_layer_selects_an_agent_only_for_roles_its_runtime_can_run(
    monkeypatch: pytest.MonkeyPatch, stand_in: str,
) -> None:
    async def _test(_pool: Any) -> None:
        one_off = await _stand_in_agent(stand_in)
        project = (await _ok("POST", "/api/projects", {"name": "calc"}))["project_id"]

        await _ok("PUT", "/api/agent-settings/roles/worker", {"agent": one_off})
        await _ok("PUT", f"/api/projects/{project}/agent-settings/roles/reviewer", {"agent": one_off})
        for path in ("/api/agent-settings/roles/planner", "/api/agent-settings/roles/manager",
                     "/api/agent-settings/system", f"/api/projects/{project}/agent-settings"):
            status, payload = await _call("PUT", path, {"agent": one_off})
            assert status == 400 and "lacks resident_session, resume" in payload["error"], (path, payload)
        assert (await _call("PUT", "/api/agent-settings/system", {"agent": "agent-nowhere"}))[0] == 400

        # An agent selected for the Planner cannot move to a runtime that cannot run it.
        planner = await _agent("claude-code/sonnet")
        await _ok("PUT", "/api/agent-settings/roles/planner", {"agent": planner})
        si_runtime = next(r["runtime_id"] for r in (await _registry())["runtimes"] if r["name"] == "si")
        si_key = next(p["provider_id"] for p in (await _registry())["providers"] if p["name"] == "si-key")
        status, payload = await _call("PUT", f"/api/agents/{planner}", {"runtime_id": si_runtime,
                                                                        "provider_id": si_key})
        assert status == 400 and "cannot run the planner" in payload["error"]
        # A selected agent stays until nothing selects it.
        assert (await _call("DELETE", f"/api/agents/{planner}"))[0] == 409

    _run(_test, monkeypatch)


async def _launch(role: str, task_id: str, work_dir: Path, root: Path) -> dict[str, Any]:
    """Run ``role`` of task ``task_id`` the way the server does, with a stand-in claude; what it started with,
    or the outcome without a start (``{"outcome": ...}``)."""
    import agent_launch_parity as parity

    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.tiers.planner import PlannerAgent, TaskPlan
    from looper.agents.tiers.worker import WorkerConfig, WorkerPool

    out = root / f"{role}-{task_id}.json"
    claude = fake_claude(root / f"claude-{role}-{task_id}", parity._CLAUDE.format(out=str(out)))
    work_dir.mkdir(parents=True, exist_ok=True)
    if role == "planner":
        planner = PlannerAgent(task_id=task_id, runtime_root=root / "runtime", claude_bin=claude)
        project = await _project_of(task_id)
        plan = planner._plan = TaskPlan(plan_id="plan-x", goal_id="", requirement="r", work_dir=str(work_dir),
                                        project_id=project)
        try:
            await planner._run_turn(planner._plan_message(plan))
        finally:
            await planner._claude.stop()
        outcome: Any = None
    else:
        verify = role == "reviewer"
        payload: dict[str, Any] = {"task_id": "step-x", "parent_task_id": task_id, "instruction": "do it",
                                   "context": {"work_dir": str(work_dir)}}
        if verify:
            payload["work_result"] = {"status": "completed", "summary": "done", "changed_files": {}}
        outcome = await WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=root / "runtime")._execute(
            MessageEnvelope.create(source=planner_address(task_id), target=worker_address(f"{role}-x"),
                                   message_type=MessageType.VERIFY_REQUEST if verify else MessageType.WORK_ITEM,
                                   payload=payload), is_verify=verify)
    return json.loads(out.read_text()) if out.exists() else {"outcome": outcome}


async def _project_of(task_id: str) -> str:
    from looper.db import projects

    return (await projects.get_task_project(task_id))["id"]


def _model(seen: dict[str, Any]) -> str:
    argv = seen["argv"]
    return argv[argv.index("--model") + 1]


def test_a_tasks_agent_override_reaches_only_that_tasks_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stand_in: str,
) -> None:
    async def _test(pool: Any) -> None:
        sonnet, haiku = await _agent("claude-code/sonnet"), await _agent("claude-code/haiku")
        one_off = await _stand_in_agent(stand_in)
        await _ok("PUT", "/api/agent-settings/system", {"agent": sonnet})
        project = (await _ok("POST", "/api/projects", {"name": "calc", "work_dir": str(tmp_path / "w")}))["project_id"]
        chose = (await _ok("POST", "/api/tasks/create", {"requirement": "r", "project_id": project,
                                                         "agent_overrides": {"worker": haiku, "planner": None}}))
        plain = await _ok("POST", "/api/tasks/create", {"requirement": "r", "project_id": project})

        seen = {(task["task_id"] == chose["task_id"], role): await _launch(role, task["task_id"], tmp_path / "w",
                                                                           tmp_path)
                for task in (chose, plain) for role in ("planner", "worker", "reviewer")}
        assert {place: _model(s) for place, s in seen.items()} == {
            (True, "planner"): "sonnet", (True, "worker"): "haiku", (True, "reviewer"): "sonnet",
            (False, "planner"): "sonnet", (False, "worker"): "sonnet", (False, "reviewer"): "sonnet"}
        # Every run went through the Claude Code runtime with its own login: its cost is what Claude reports.
        assert {r["cost_source"] for r in await pool.fetch("SELECT cost_source FROM llm_records")} == {"runtime"}

        detail = (await _ok("GET", f"/api/tasks/{chose['task_id']}"))["agent_overrides"]
        assert {role: a["name"] for role, a in detail.items()} == {"worker": "claude-code/haiku"}
        assert (await _ok("GET", f"/api/tasks/{plain['task_id']}"))["agent_overrides"] == {}
        assert (await _call("DELETE", f"/api/agents/{haiku}"))[0] == 409  # a task chose it

        # A task chooses only for its own roles, an agent that exists and can run the role.
        for overrides, error in [({"manager": sonnet}, "not 'manager'"), ({"worker": "agent-nowhere"}, "no agent"),
                                 ({"planner": one_off}, "cannot run the planner"), ([sonnet], "must be an object")]:
            status, payload = await _call("POST", "/api/tasks/create", {"requirement": "r", "project_id": project,
                                                                        "agent_overrides": overrides})
            assert status == 400 and error in payload["error"], payload
        assert (await _call("POST", "/api/tasks/create", {"requirement": "r", "project_id": project,
                                                          "agent_overrides": {"worker": one_off}}))[0] == 201

    _run(_test, monkeypatch)


def test_an_agent_that_can_no_longer_run_fails_the_run_instead_of_another_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stand_in: str,
) -> None:
    from looper.agents import runtimes

    async def _test(_pool: Any) -> None:
        one_off = await _stand_in_agent(stand_in)
        await _ok("PUT", "/api/agent-settings/roles/worker", {"agent": one_off})
        project = (await _ok("POST", "/api/projects", {"name": "calc"}))["project_id"]
        task = (await _ok("POST", "/api/tasks/create", {"requirement": "r", "project_id": project,
                                                        "work_dir": str(tmp_path / "w")}))["task_id"]
        monkeypatch.delitem(runtimes.ADAPTERS, stand_in)  # a version without that runtime type

        result = await _launch("worker", task, tmp_path / "w", tmp_path)
        assert "argv" not in result  # no runtime started
        assert result["outcome"]["status"] == "failed"
        assert "runs on a 'stand-in' runtime, which this version cannot run" in result["outcome"]["error"]

    _run(_test, monkeypatch)


# --- The upgrade: each layer's model becomes an agent ---

_BEFORE_LAYER_AGENTS = """
CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO schema_migrations (name) VALUES ('agent_setting_layers');
CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                       work_dir TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                       updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE agent_setting_layers (
    scope TEXT NOT NULL, role TEXT, project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    harness_prompt TEXT, model TEXT, effort TEXT, env JSONB NOT NULL DEFAULT '[]'::jsonb,
    run_budget_usd DOUBLE PRECISION, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE UNIQUE INDEX idx_agent_setting_layers_place
    ON agent_setting_layers (scope, COALESCE(role, ''), COALESCE(project_id, ''));
INSERT INTO projects (id, name) VALUES ('proj-a', 'calc');
INSERT INTO agent_setting_layers (scope, role, project_id, model, effort) VALUES
    ('system', NULL, NULL, 'sonnet', 'high'),
    ('role', 'manager', NULL, 'opus', NULL),
    ('role', 'planner', NULL, ' haiku ', NULL),
    ('role', 'worker', NULL, 'sonnet', NULL),
    ('role', 'reviewer', NULL, '--resume', NULL),
    ('project', NULL, 'proj-a', 'opus[1m]', NULL);
"""


def test_the_upgrade_turns_each_layer_model_into_an_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db.connection import ensure_schema

    async def _layers(pool: Any) -> list[tuple[Any, ...]]:
        return [tuple(r) for r in await pool.fetch(
            "SELECT l.scope, l.role, l.project_id, a.name, a.model, a.runtime_id, a.provider_id, l.effort "
            "FROM agent_setting_layers l LEFT JOIN agents a ON a.id = l.agent_id ORDER BY 1, 2, 3")]

    async def _test(pool: Any) -> None:
        columns = {r["column_name"] for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'agent_setting_layers'")}
        assert "model" not in columns and "agent_id" in columns
        builtin = ("rt-claude-code", "prov-claude-subscription")
        assert await _layers(pool) == [
            ("project", None, "proj-a", "claude-code/opus[1m]", "opus[1m]", *builtin, None),
            ("role", "manager", None, "claude-code/opus", "opus", *builtin, None),  # the built-in agent
            ("role", "planner", None, "claude-code/haiku", "haiku", *builtin, None),
            ("role", "worker", None, "claude-code/sonnet", "sonnet", *builtin, None),
            ("system", None, None, "claude-code/sonnet", "sonnet", *builtin, "high"),
        ]  # the reviewer's model no longer validated (it was ignored): its layer held nothing else
        assert await pool.fetchval("SELECT count(*) FROM agents") == 4
        before = await _layers(pool), [tuple(r) for r in await pool.fetch("SELECT * FROM agents ORDER BY id")]
        await ensure_schema(pool)
        assert (await _layers(pool), [tuple(r) for r in await pool.fetch("SELECT * FROM agents ORDER BY id")]) \
            == before

    _run(_test, monkeypatch, before=_BEFORE_LAYER_AGENTS)


# --- A provider's model catalog and concurrency: stored and checked ---


def test_a_provider_keeps_its_model_catalog_and_concurrency_with_unknowns_as_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _test(_pool: Any) -> None:
        provider = (await _ok("POST", "/api/providers", {
            "name": "glm", "protocols": ["anthropic-messages", "openai-chat"], "auth": "api_key",
            "max_concurrency": 4, "models": [
                {"name": "glm-4.6", "context_window": 200_000, "input_usd_per_mtok": 0.6, "efforts": ["low", "high"]},
                {"name": "glm-4.5"}]}))["provider"]
        assert provider["max_concurrency"] == 4
        assert provider["models"] == [
            {"name": "glm-4.6", "context_window": 200_000, "input_usd_per_mtok": 0.6, "output_usd_per_mtok": None,
             "cache_read_usd_per_mtok": None, "cache_write_usd_per_mtok": None, "efforts": ["low", "high"]},
            {"name": "glm-4.5", "context_window": None, "input_usd_per_mtok": None, "output_usd_per_mtok": None,
             "cache_read_usd_per_mtok": None, "cache_write_usd_per_mtok": None, "efforts": None}]
        unlimited = (await _ok("PUT", f"/api/providers/{provider['provider_id']}", {"max_concurrency": None}))
        assert unlimited["provider"]["max_concurrency"] is None
        (builtin,) = [p for p in (await _registry())["providers"] if p["builtin"]]
        assert (builtin["max_concurrency"], builtin["models"]) == (None, [])  # no limit, any model

    _run(_test, monkeypatch)


@pytest.mark.parametrize(("change", "error"), [
    ({"max_concurrency": 0}, "max_concurrency must be"),
    ({"models": [{"name": "m", "context_window": 0}]}, "context_window must be"),
    ({"models": [{"name": "m", "efforts": ["Low"]}]}, "efforts must be"),
    ({"models": [{"name": "m", "input_usd_per_mtok": -1}]}, "per million tokens"),
    ({"models": [{"name": "m", "speed": 1}]}, "each model is"),
])
def test_a_catalog_or_concurrency_that_does_not_validate_is_refused(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any], error: str,
) -> None:
    async def _test(_pool: Any) -> None:
        status, payload = await _call("POST", "/api/providers", {"name": "p", "protocols": ["openai-chat"],
                                                                 "auth": "api_key", **change})
        assert status == 400 and error in payload["error"], payload

    _run(_test, monkeypatch)
