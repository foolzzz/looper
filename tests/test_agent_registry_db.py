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
    from loopx.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            if before:
                await pool.execute(before)
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await test(pool)

    asyncio.run(_main())


async def _call(method: str, path: str, body: Any = None, *, claude: str = "claude") -> tuple[int, Any]:
    from loopx.agents.http_server import AgentHTTPServer
    from loopx.agents.tiers.task import TaskRouter

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
    from loopx.agents import runtimes
    from loopx.agents.runtimes import AuthKind, Capability, Probe
    from loopx.agents.runtimes.claude_code import ClaudeCodeRuntime

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
    from loopx.agents.agent_registry import BUILTIN_AGENT, AgentSpec
    from loopx.agents.runtimes import CLAUDE_CODE
    from loopx.db import agent_registry as db
    from loopx.db.connection import ensure_schema

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
    claude = _version_claude(tmp_path)

    async def _test(_pool: Any) -> None:
        added = (await _ok("POST", "/api/runtimes", {"name": "claude-next", "type": "claude-code",
                                                     "executable": claude}))["runtime"]
        assert (added["version"], added["probe_error"]) == ("2.1.285 (Claude Code)", None)
        assert set(added["capabilities"]) == {"run", "resident_session", "resume"}

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
    from loopx.db.connection import ensure_schema

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
