"""Runtimes, LLM providers and Agent Profiles against a real PostgreSQL, through the HTTP API.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards. A stand-in runtime
type (``StandIn``) plays a second adapter: one that cannot keep a resident
session, and has a login of its own. Runtimes are built in, so a test adds the
row ``looper serve`` would add for one it finds (``_runtime``).
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

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

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


async def _runtime(runtime_type: str, *, name: str | None = None, builtin: bool = True, executable: str = "",
                   ) -> str:
    """The runtime row of ``runtime_type`` that ``looper serve`` adds when it finds one (``rt-<type>``), or with
    ``builtin=False`` one registered by hand before runtimes were built in; its id."""
    from looper.agents import runtimes
    from looper.db.connection import get_pool

    runtime_id = f"rt-{runtime_type}" if builtin else f"rt-custom-{name}"
    await get_pool().execute(
        "INSERT INTO agent_runtimes (id, name, type, executable, capabilities, builtin) VALUES ($1, $2, $3, $4, $5, $6)",
        runtime_id, name or runtime_type, runtime_type, executable,
        sorted(c.value for c in runtimes.ADAPTERS[runtime_type].capabilities), builtin)
    return runtime_id


def _by_name(entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(e for e in entries if e["name"] == name)


# --- The built-ins ---


def test_every_database_has_the_builtin_runtime_provider_and_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.profile_registry import BUILTIN_PROFILE, AgentProfile
    from looper.agents.runtimes import CLAUDE_CODE
    from looper.db import profile_registry as db
    from looper.db.connection import ensure_schema

    async def _test(pool: Any) -> None:
        await pool.execute("DELETE FROM agent_profiles WHERE builtin")  # a missing one comes back
        await ensure_schema(pool)
        await ensure_schema(pool)
        (profile,) = await db.list_profiles()
        assert AgentProfile.from_row(profile) == BUILTIN_PROFILE
        registry = await _registry()
        (runtime,) = registry["runtimes"]
        assert (runtime["builtin"], runtime["executable"], runtime["version"]) == (True, "", None)
        assert set(runtime["capabilities"]) == {c.value for c in CLAUDE_CODE.capabilities}
        # Claude Code's own login, and Codex's (ChatGPT), with the models a ChatGPT login offers as suggestions.
        claude, codex = registry["providers"]
        assert (claude["name"], claude["auth"], claude["oauth_runtime_type"], claude["api_key"]) == (
            "claude-subscription", "oauth", "claude-code", None)
        assert (codex["provider_id"], codex["name"], codex["builtin"], codex["auth"], codex["oauth_runtime_type"],
                codex["protocols"], codex["models"]) == (
            "prov-codex-login", "codex-login", True, "oauth", "codex", ["openai-responses"], [])
        assert codex["suggested_models"][0] == "gpt-6.1-sol" and claude["suggested_models"] == []

    _run(_test, monkeypatch)


@pytest.mark.parametrize(("method", "path", "body"), [
    ("PUT", "/api/providers/prov-claude-subscription", {"models": [{"name": "opus"}]}),
    ("DELETE", "/api/providers/prov-claude-subscription", None),
    ("DELETE", "/api/providers/prov-codex-login", None),
    ("PUT", "/api/agent-profiles/agent-claude-code-opus", {"model": "sonnet"}),
    ("DELETE", "/api/agent-profiles/agent-claude-code-opus", None),
])
def test_the_builtins_are_read_only(monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any) -> None:
    async def _test(_pool: Any) -> None:
        status, payload = await _call(method, path, body)
        assert status == 400 and "built-in" in payload["error"]

    _run(_test, monkeypatch)


# --- Runtimes: built in, detected and probed ---


@pytest.mark.parametrize(("method", "path", "body"), [
    ("POST", "/api/runtimes", {"name": "claude-next", "type": "claude-code", "executable": "/opt/claude"}),
    ("PUT", "/api/runtimes/rt-claude-code", {"executable": "/opt/claude"}),
    ("DELETE", "/api/runtimes/rt-claude-code", None),
])
def test_runtimes_cannot_be_registered_changed_or_removed(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any,
) -> None:
    async def _test(_pool: Any) -> None:
        assert (await _call(method, path, body))[0] == 404
        (runtime,) = (await _registry())["runtimes"]
        assert (runtime["runtime_id"], runtime["executable"]) == ("rt-claude-code", "")

    _run(_test, monkeypatch)


def test_re_detect_probes_a_runtime_and_adds_a_type_found_on_path_since(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.runtimes import CLAUDE_CODE

    claude = _version_claude(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    async def _test(_pool: Any) -> None:
        # The built-in Claude Code runtime probes the server's claude, and says where it is.
        builtin = (await _ok("POST", "/api/runtimes/rt-claude-code/probe", claude=claude))["runtime"]
        assert (builtin["version"], builtin["probe_error"]) == ("2.1.285 (Claude Code)", None)
        assert builtin["probed_at"] and set(builtin["capabilities"]) == {c.value for c in CLAUDE_CODE.capabilities}
        assert builtin["roles"] == ["manager", "planner", "worker", "reviewer"]
        assert (await _call("GET", "/api/registry", claude=claude))[1]["runtimes"][0]["path"] == claude

        # A type not on PATH has no runtime, and Re-detect says why; once it is on PATH, Re-detect adds it.
        monkeypatch.setenv("PATH", str(bin_dir))
        status, payload = await _call("POST", "/api/runtimes/rt-codex/probe")
        assert status == 404 and "Codex (codex) is not on the server's PATH" in payload["error"]
        assert [r["runtime_id"] for r in (await _registry())["runtimes"]] == ["rt-claude-code"]
        _version_codex(bin_dir / "codex")
        codex = (await _ok("POST", "/api/runtimes/rt-codex/probe"))["runtime"]
        assert (codex["builtin"], codex["executable"], codex["version"], codex["path"]) == (
            True, "", "codex-cli 0.162.0", str(bin_dir / "codex"))
        types = {t["type"]: t for t in (await _registry())["runtime_types"]}
        assert (types["codex"]["runtime_id"], types["codex"]["roles"]) == (
            "rt-codex", ["manager", "planner", "worker", "reviewer"])

        # One registered by hand before runtimes were built in probes its own executable.
        legacy = await _runtime("claude-code", name="claude-beta", builtin=False,
                                executable=str(tmp_path / "absent"))
        missing = (await _ok("POST", f"/api/runtimes/{legacy}/probe"))["runtime"]
        assert (missing["builtin"], missing["version"], missing["path"]) == (False, None, None)
        assert "was not found" in missing["probe_error"]
        assert (await _call("POST", "/api/runtimes/rt-nowhere/probe"))[0] == 404

    _run(_test, monkeypatch)


# Runtimes registered by hand before runtimes were built in: "claude-beta" runs a profile, "claude-old" none.
_CUSTOM_RUNTIMES = """
INSERT INTO agent_runtimes (id, name, type, executable, capabilities)
VALUES ('rt-custom-beta', 'claude-beta', 'claude-code', '/opt/claude-beta/bin/claude', ARRAY['run']),
       ('rt-custom-old', 'claude-old', 'claude-code', 'claude-old', ARRAY['run']);
INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model)
VALUES ('agent-beta', 'beta/haiku', 'rt-custom-beta', 'prov-claude-subscription', 'haiku');
"""


def test_the_upgrade_drops_custom_runtimes_no_profile_uses_once_and_keeps_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db.connection import ensure_schema

    async def _test(pool: Any) -> None:
        # A database from before: the custom rows, and the migration not run yet.
        await pool.execute(_CUSTOM_RUNTIMES)
        await pool.execute("DELETE FROM schema_migrations WHERE name = 'agent_runtimes_unused_custom_removed'")
        await ensure_schema(pool)

        runtimes = {r["name"]: r for r in (await _registry())["runtimes"]}
        assert set(runtimes) == {"claude-code", "claude-beta"}
        assert (runtimes["claude-beta"]["builtin"], runtimes["claude-beta"]["executable"],
                runtimes["claude-beta"]["profile_count"]) == (False, "/opt/claude-beta/bin/claude", 1)
        assert _by_name((await _registry())["profiles"], "beta/haiku")["runtime_id"] == "rt-custom-beta"

        # Once: a later run of the schema leaves the rows as they are.
        await pool.execute("INSERT INTO agent_runtimes (id, name, type) VALUES ('rt-custom-new', 'x', 'claude-code')")
        await ensure_schema(pool)
        assert {r["name"] for r in (await _registry())["runtimes"]} == {"claude-code", "claude-beta", "x"}

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


# --- Agent Profiles: checked when created or changed ---


async def _provider(body: dict[str, Any]) -> str:
    return (await _ok("POST", "/api/providers", body))["provider"]["provider_id"]


def test_a_profile_pairs_a_runtime_and_a_provider_that_can_run_together(
    monkeypatch: pytest.MonkeyPatch, stand_in: str,
) -> None:
    async def _test(_pool: Any) -> None:
        standin = await _runtime(stand_in, name="si")
        chat = await _provider({"name": "openai-chat", "protocols": ["openai-chat"], "auth": "api_key"})
        key = await _provider({"name": "anthropic-key", "protocols": ["anthropic-messages"], "auth": "api_key",
                               "models": [{"name": "claude-sonnet-4-5"}]})
        login = await _provider({"name": "stand-in-login", "protocols": ["anthropic-messages"], "auth": "oauth",
                                 "oauth_runtime_type": stand_in})

        async def profile(runtime_id: str, provider_id: str, model: str = "claude-sonnet-4-5") -> tuple[int, Any]:
            return await _call("POST", "/api/agent-profiles", {"name": f"a-{runtime_id}-{provider_id}-{model}",
                                                               "runtime_id": runtime_id, "provider_id": provider_id,
                                                               "model": model})

        status, payload = await profile("rt-claude-code", "prov-claude-subscription", "sonnet")
        assert status == 201 and payload["profile"]["runtime_name"] == "claude-code"
        assert (await profile(standin, key))[0] == 201  # an adapter that takes API keys
        assert (await profile(standin, login))[0] == 201  # its own login

        # Protocols must intersect.
        status, payload = await profile("rt-claude-code", chat)
        assert status == 400 and "share no protocol" in payload["error"]
        # An OAuth provider pairs only with the runtime type whose login it is, either way round.
        status, payload = await profile(standin, "prov-claude-subscription", "sonnet")
        assert status == 400 and "login of claude-code runtimes" in payload["error"]
        status, payload = await profile("rt-claude-code", login)
        assert status == 400 and "login of stand-in runtimes" in payload["error"]
        # Claude Code runs with its own login and with an API key.
        assert (await profile("rt-claude-code", key))[0] == 201
        # A provider that lists its models takes no other.
        status, payload = await profile(standin, key, "claude-opus-4-5")
        assert status == 400 and "not one of them" in payload["error"]
        # A runtime that takes only its own login refuses an API key.
        from looper.agents import runtimes
        from looper.agents.runtimes import AuthKind

        monkeypatch.setattr(type(runtimes.ADAPTERS[stand_in]), "auth_kinds", frozenset({AuthKind.OAUTH}))
        status, payload = await profile(standin, key, "claude-sonnet-4-5")
        assert status == 400 and "cannot be started with an api_key provider" in payload["error"]

    _run(_test, monkeypatch)


def _version_codex(path: Path) -> str:
    """A stand-in codex that prints its version for ``--version`` and fails on anything else."""
    return fake_claude(path, (
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n    print('codex-cli 0.162.0')\n    sys.exit(0)\n"
        "sys.exit(9)\n"
    ))


def test_codex_pairs_with_its_own_login_or_an_api_key_provider_of_openai_responses_and_runs_every_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _version_codex(tmp_path / "codex")
    monkeypatch.setenv("PATH", str(tmp_path))

    async def _test(_pool: Any) -> None:
        runtime = (await _ok("POST", "/api/runtimes/rt-codex/probe"))["runtime"]
        assert (runtime["version"], runtime["type_label"], runtime["protocols"]) == (
            "codex-cli 0.162.0", "Codex", ["openai-responses"])
        cpa = await _provider({"name": "cpa", "protocols": ["openai-responses"], "auth": "api_key",
                               "base_url": "http://127.0.0.1:8317/v1", "api_key": SECRET})
        chat = await _provider({"name": "chat-only", "protocols": ["openai-chat"], "auth": "api_key"})
        anthropic = await _provider({"name": "anthropic-key", "protocols": ["anthropic-messages"], "auth": "api_key"})

        async def profile(provider_id: str) -> tuple[int, Any]:
            return await _call("POST", "/api/agent-profiles", {
                "name": f"codex-{provider_id}", "model": "gpt-5.5", "runtime_id": runtime["runtime_id"],
                "provider_id": provider_id})

        status, payload = await profile(cpa)
        assert status == 201
        # Every role can run it: the system layer selects it for all four.
        await _ok("PUT", "/api/agent-settings/system", {"profile_id": payload["profile"]["profile_id"]})
        for provider_id in (chat, anthropic, "prov-claude-subscription"):
            status, payload = await profile(provider_id)
            assert status == 400 and "share no protocol" in payload["error"]
        # Codex's own login: any model, priced at the built-in prices as a subscription equivalent.
        status, payload = await profile("prov-codex-login")
        assert status == 201 and payload["warnings"] == []
        assert payload["profile"]["cost"] == {"source": "provider_price", "equivalent": True, "unpriced": []}
        await _ok("PUT", "/api/agent-settings/system", {"profile_id": payload["profile"]["profile_id"]})

    _run(_test, monkeypatch)


def test_the_codex_login_pairs_only_with_codex_runtimes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _version_omp(tmp_path / "omp")
    monkeypatch.setenv("PATH", str(tmp_path))

    async def _test(_pool: Any) -> None:
        omp = (await _ok("POST", "/api/runtimes/rt-omp/probe"))["runtime"]["runtime_id"]
        for runtime_id in ("rt-claude-code", omp):
            status, payload = await _call("POST", "/api/agent-profiles", {
                "name": f"{runtime_id}-login", "model": "gpt-6.1-sol", "runtime_id": runtime_id,
                "provider_id": "prov-codex-login"})
            assert status == 400, payload
            assert "pairs only with" in payload["error"] or "share no protocol" in payload["error"]
        # A login problem is cleared only on the Codex login.
        status, payload = await _call("DELETE", "/api/providers/prov-claude-subscription/login-problem")
        assert status == 400 and "is not the Codex login" in payload["error"]

    _run(_test, monkeypatch)


def test_codex_on_path_becomes_a_builtin_runtime_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.profile_registry import register_found_runtimes

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _version_codex(bin_dir / "codex")

    async def _test(_pool: Any) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        assert await register_found_runtimes() == []
        monkeypatch.setenv("PATH", str(bin_dir))
        assert await register_found_runtimes() == ["codex"]
        runtime = _by_name((await _registry())["runtimes"], "codex")
        assert (runtime["runtime_id"], runtime["builtin"], runtime["executable"], runtime["version"]) == (
            "rt-codex", True, "", "codex-cli 0.162.0")
        assert await register_found_runtimes() == []  # once

    _run(_test, monkeypatch)


def _version_omp(path: Path) -> str:
    """A stand-in omp that prints its version for ``--version`` and fails on anything else."""
    return fake_claude(path, (
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n    print('omp/18.4.3')\n    sys.exit(0)\n"
        "sys.exit(9)\n"
    ))


def test_omp_pairs_with_an_api_key_provider_of_any_of_its_apis_and_runs_every_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _version_omp(tmp_path / "omp")
    monkeypatch.setenv("PATH", str(tmp_path))

    async def _test(_pool: Any) -> None:
        runtime = (await _ok("POST", "/api/runtimes/rt-omp/probe"))["runtime"]
        assert (runtime["version"], runtime["type_label"], runtime["protocols"]) == (
            "omp/18.4.3", "OMP", ["anthropic-messages", "openai-chat", "openai-responses"])
        providers = [
            await _provider({"name": "cpa", "protocols": ["openai-responses", "openai-chat"], "auth": "api_key",
                             "base_url": "http://127.0.0.1:8317/v1", "api_key": SECRET}),
            await _provider({"name": "chat-only", "protocols": ["openai-chat"], "auth": "api_key"}),
            await _provider({"name": "anthropic-key", "protocols": ["anthropic-messages"], "auth": "api_key"}),
        ]

        async def profile(provider_id: str) -> tuple[int, Any]:
            return await _call("POST", "/api/agent-profiles", {
                "name": f"omp-{provider_id}", "model": "gpt-5.5", "runtime_id": runtime["runtime_id"],
                "provider_id": provider_id})

        profiles = []
        for provider_id in providers:
            status, payload = await profile(provider_id)
            assert status == 201, payload
            profiles.append(payload["profile"]["profile_id"])
        # Every role can run it: the system layer selects it for all four.
        await _ok("PUT", "/api/agent-settings/system", {"profile_id": profiles[0]})
        # The Claude subscription is Claude Code's login, and OMP's own logins are not ones Looper uses.
        status, payload = await profile("prov-claude-subscription")
        assert status == 400 and "pairs only with" in payload["error"]
        status, payload = await _call("POST", "/api/providers", {
            "name": "omp-login", "protocols": ["openai-responses"], "auth": "oauth", "oauth_runtime_type": "omp"})
        assert status == 400 and "OMP has no login of its own" in payload["error"]

    _run(_test, monkeypatch)


def test_omp_on_path_becomes_a_builtin_runtime_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.profile_registry import register_found_runtimes

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _version_omp(bin_dir / "omp")

    async def _test(_pool: Any) -> None:
        monkeypatch.setenv("PATH", str(bin_dir))
        assert await register_found_runtimes() == ["omp"]
        runtime = _by_name((await _registry())["runtimes"], "omp")
        assert (runtime["runtime_id"], runtime["builtin"], runtime["executable"], runtime["version"]) == (
            "rt-omp", True, "", "omp/18.4.3")
        assert await register_found_runtimes() == []  # once

    _run(_test, monkeypatch)


def test_a_change_that_would_break_a_profile_is_refused_and_one_in_use_is_kept(
    monkeypatch: pytest.MonkeyPatch, stand_in: str,
) -> None:
    async def _test(_pool: Any) -> None:
        runtime_id = await _runtime(stand_in, name="si")
        provider_id = await _provider({"name": "key", "protocols": ["anthropic-messages"], "auth": "api_key"})
        profile = (await _ok("POST", "/api/agent-profiles", {"name": "si-sonnet", "runtime_id": runtime_id,
                                                             "provider_id": provider_id, "model": "sonnet",
                                                             "effort": "low"}))["profile"]

        status, payload = await _call("PUT", f"/api/providers/{provider_id}", {"protocols": ["openai-chat"]})
        assert status == 400 and "Agent Profile 'si-sonnet' uses this provider" in payload["error"]
        status, payload = await _call("PUT", f"/api/agent-profiles/{profile['profile_id']}",
                                      {"provider_id": "prov-claude-subscription"})
        assert status == 400 and "login of claude-code runtimes" in payload["error"]
        renamed = (await _ok("PUT", f"/api/agent-profiles/{profile['profile_id']}",
                             {"name": "si-fast", "effort": None}))["profile"]
        assert (renamed["name"], renamed["effort"]) == ("si-fast", None)

        status, payload = await _call("DELETE", f"/api/providers/{provider_id}")
        assert status == 409 and "first" in payload["error"]
        await _ok("DELETE", f"/api/agent-profiles/{profile['profile_id']}")
        await _ok("DELETE", f"/api/providers/{provider_id}")
        assert runtime_id in {r["runtime_id"] for r in (await _registry())["runtimes"]}

    _run(_test, monkeypatch)


# --- LLM profiles are gone ---

# What a database had before Agent Profiles: LLM profiles and the two agent config columns that pointed at them.
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


def test_the_upgrade_removes_llm_profiles_and_keeps_the_role_configs(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db.connection import ensure_schema

    async def _test(pool: Any) -> None:
        await ensure_schema(pool)  # once only
        assert await pool.fetchval("SELECT to_regclass('llm_profiles')") is None
        columns = {r["column_name"] for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'role_configs'")}
        assert not columns & {"runtime", "llm_profile_id"}
        assert await pool.fetchval("SELECT system_prompt FROM role_configs WHERE role = 'worker'") == "Work."
        assert await pool.fetchval("SELECT 1 FROM schema_migrations WHERE name = 'llm_profiles_removed'")

        assert (await _call("GET", "/api/llm-profiles"))[0] == 404
        status, payload = await _call("PUT", "/api/roles/worker", {"runtime": "codex-cli"})
        assert status == 400 and "runtime was removed" in payload["error"]

    _run(_test, monkeypatch, before=_BEFORE_AGENTS)


# --- Selecting a profile: the layers and a task's overrides, checked for the roles they reach ---


async def _profile(name: str, runtime_id: str = "rt-claude-code", provider_id: str = "prov-claude-subscription",
                   model: str | None = None) -> str:
    return (await _ok("POST", "/api/agent-profiles", {
        "name": name, "runtime_id": runtime_id, "provider_id": provider_id,
        "model": model or name.rsplit("/", 1)[-1]}))["profile"]["profile_id"]


async def _stand_in_profile(stand_in: str) -> str:
    runtime_id = await _runtime(stand_in, name="si")
    provider_id = (await _ok("POST", "/api/providers", {"name": "si-key", "protocols": ["anthropic-messages"],
                                                        "auth": "api_key"}))["provider"]["provider_id"]
    return await _profile("si/sonnet", runtime_id, provider_id, "sonnet")


def test_a_layer_selects_a_profile_only_for_roles_its_runtime_can_run(
    monkeypatch: pytest.MonkeyPatch, stand_in: str,
) -> None:
    async def _test(_pool: Any) -> None:
        one_off = await _stand_in_profile(stand_in)
        project = (await _ok("POST", "/api/projects", {"name": "calc"}))["project_id"]

        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": one_off})
        await _ok("PUT", f"/api/projects/{project}/agent-settings/roles/reviewer", {"profile_id": one_off})
        for path in ("/api/agent-settings/roles/planner", "/api/agent-settings/roles/manager",
                     "/api/agent-settings/system", f"/api/projects/{project}/agent-settings"):
            status, payload = await _call("PUT", path, {"profile_id": one_off})
            assert status == 400 and "lacks resident_session, resume" in payload["error"], (path, payload)
        assert (await _call("PUT", "/api/agent-settings/system", {"profile_id": "agent-nowhere"}))[0] == 400

        # A profile selected for the Planner cannot move to a runtime that cannot run it.
        planner = await _profile("claude-code/sonnet")
        await _ok("PUT", "/api/agent-settings/roles/planner", {"profile_id": planner})
        si_runtime = next(r["runtime_id"] for r in (await _registry())["runtimes"] if r["name"] == "si")
        si_key = next(p["provider_id"] for p in (await _registry())["providers"] if p["name"] == "si-key")
        status, payload = await _call("PUT", f"/api/agent-profiles/{planner}", {"runtime_id": si_runtime,
                                                                                "provider_id": si_key})
        assert status == 400 and "cannot run the planner" in payload["error"]
        # A selected profile stays until nothing selects it.
        assert (await _call("DELETE", f"/api/agent-profiles/{planner}"))[0] == 409

    _run(_test, monkeypatch)


def test_where_a_profile_is_selected_is_read_back_and_keeps_it_from_being_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stand_in: str,
) -> None:
    async def _test(_pool: Any) -> None:
        sonnet, haiku = await _profile("claude-code/sonnet"), await _profile("claude-code/haiku")
        one_off = await _stand_in_profile(stand_in)
        calc = (await _ok("POST", "/api/projects", {"name": "calc", "work_dir": str(tmp_path)}))["project_id"]
        await _ok("PUT", "/api/agent-settings/system", {"profile_id": sonnet})
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": sonnet})
        await _ok("PUT", f"/api/projects/{calc}/agent-settings", {"profile_id": sonnet})
        await _ok("PUT", f"/api/projects/{calc}/agent-settings/roles/reviewer", {"profile_id": one_off})
        for _ in range(2):
            await _ok("POST", "/api/tasks/create", {"requirement": "r", "project_id": calc,
                                                    "profile_overrides": {"worker": sonnet, "reviewer": sonnet}})

        profiles = {a["name"]: a for a in (await _registry())["profiles"]}
        assert profiles["claude-code/sonnet"]["used_by"] == {"layers": [
            {"scope": "system", "role": None, "project_id": None, "project_name": None},
            {"scope": "role", "role": "worker", "project_id": None, "project_name": None},
            {"scope": "project", "role": None, "project_id": calc, "project_name": "calc"},
        ], "task_count": 2}
        assert profiles["si/sonnet"]["used_by"]["layers"] == [
            {"scope": "role_project", "role": "reviewer", "project_id": calc, "project_name": "calc"}]
        assert profiles["claude-code/haiku"]["used_by"] == {"layers": [], "task_count": 0}
        # What its runtime can run: the stand-in keeps no resident session.
        assert (profiles["claude-code/sonnet"]["roles"], profiles["si/sonnet"]["roles"]) == (
            ["manager", "planner", "worker", "reviewer"], ["worker", "reviewer"])
        # A profile's own answer has it too.
        changed = (await _ok("PUT", f"/api/agent-profiles/{one_off}", {"effort": "high"}))["profile"]
        assert changed["used_by"] == profiles["si/sonnet"]["used_by"]

        status, payload = await _call("DELETE", f"/api/agent-profiles/{sonnet}")
        assert status == 409 and payload["error"] == (
            "Agent Profile 'claude-code/sonnet' is selected by the defaults of every role, the worker role, project "
            "'calc', 2 tasks: select another profile there first")
        status, payload = await _call("DELETE", f"/api/agent-profiles/{one_off}")
        assert status == 409 and "the reviewer in project 'calc'" in payload["error"]
        await _ok("DELETE", f"/api/agent-profiles/{haiku}")  # nothing selects it

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


def test_a_tasks_profile_override_reaches_only_that_tasks_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stand_in: str,
) -> None:
    async def _test(pool: Any) -> None:
        sonnet, haiku = await _profile("claude-code/sonnet"), await _profile("claude-code/haiku")
        one_off = await _stand_in_profile(stand_in)
        await _ok("PUT", "/api/agent-settings/system", {"profile_id": sonnet})
        project = (await _ok("POST", "/api/projects", {"name": "calc", "work_dir": str(tmp_path / "w")}))["project_id"]
        chose = (await _ok("POST", "/api/tasks/create", {"requirement": "r", "project_id": project,
                                                         "profile_overrides": {"worker": haiku, "planner": None}}))
        plain = await _ok("POST", "/api/tasks/create", {"requirement": "r", "project_id": project})

        seen = {(task["task_id"] == chose["task_id"], role): await _launch(role, task["task_id"], tmp_path / "w",
                                                                           tmp_path)
                for task in (chose, plain) for role in ("planner", "worker", "reviewer")}
        assert {place: _model(s) for place, s in seen.items()} == {
            (True, "planner"): "sonnet", (True, "worker"): "haiku", (True, "reviewer"): "sonnet",
            (False, "planner"): "sonnet", (False, "worker"): "sonnet", (False, "reviewer"): "sonnet"}
        # Every run went through the Claude Code runtime with its own login: its cost is what Claude reports.
        assert {r["cost_source"] for r in await pool.fetch("SELECT cost_source FROM llm_records")} == {"runtime"}

        detail = (await _ok("GET", f"/api/tasks/{chose['task_id']}"))["profile_overrides"]
        assert {role: a["name"] for role, a in detail.items()} == {"worker": "claude-code/haiku"}
        assert (await _ok("GET", f"/api/tasks/{plain['task_id']}"))["profile_overrides"] == {}
        assert (await _call("DELETE", f"/api/agent-profiles/{haiku}"))[0] == 409  # a task chose it

        # A task chooses only for its own roles, a profile that exists and can run the role.
        for overrides, error in [({"manager": sonnet}, "not 'manager'"),
                                 ({"worker": "agent-nowhere"}, "no Agent Profile"),
                                 ({"planner": one_off}, "cannot run the planner"), ([sonnet], "must be an object")]:
            status, payload = await _call("POST", "/api/tasks/create", {"requirement": "r", "project_id": project,
                                                                        "profile_overrides": overrides})
            assert status == 400 and error in payload["error"], payload
        assert (await _call("POST", "/api/tasks/create", {"requirement": "r", "project_id": project,
                                                          "profile_overrides": {"worker": one_off}}))[0] == 201

    _run(_test, monkeypatch)


def test_a_profile_that_can_no_longer_run_fails_the_run_instead_of_another_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stand_in: str,
) -> None:
    from looper.agents import runtimes

    async def _test(_pool: Any) -> None:
        one_off = await _stand_in_profile(stand_in)
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": one_off})
        project = (await _ok("POST", "/api/projects", {"name": "calc"}))["project_id"]
        task = (await _ok("POST", "/api/tasks/create", {"requirement": "r", "project_id": project,
                                                        "work_dir": str(tmp_path / "w")}))["task_id"]
        monkeypatch.delitem(runtimes.ADAPTERS, stand_in)  # a version without that runtime type

        result = await _launch("worker", task, tmp_path / "w", tmp_path)
        assert "argv" not in result  # no runtime started
        assert result["outcome"]["status"] == "failed"
        assert "runs on a 'stand-in' runtime, which this version cannot run" in result["outcome"]["error"]

    _run(_test, monkeypatch)


# --- The upgrade: each layer's model becomes an Agent Profile ---

_BEFORE_LAYER_PROFILES = """
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


def test_the_upgrade_turns_each_layer_model_into_a_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db.connection import ensure_schema

    async def _layers(pool: Any) -> list[tuple[Any, ...]]:
        return [tuple(r) for r in await pool.fetch(
            "SELECT l.scope, l.role, l.project_id, a.name, a.model, a.runtime_id, a.provider_id, l.effort "
            "FROM agent_setting_layers l LEFT JOIN agent_profiles a ON a.id = l.profile_id ORDER BY 1, 2, 3")]

    async def _test(pool: Any) -> None:
        columns = {r["column_name"] for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'agent_setting_layers'")}
        assert "model" not in columns and "profile_id" in columns
        builtin = ("rt-claude-code", "prov-claude-subscription")
        assert await _layers(pool) == [
            ("project", None, "proj-a", "claude-code/opus[1m]", "opus[1m]", *builtin, None),
            ("role", "manager", None, "claude-code/opus", "opus", *builtin, None),  # the built-in profile
            ("role", "planner", None, "claude-code/haiku", "haiku", *builtin, None),
            ("role", "worker", None, "claude-code/sonnet", "sonnet", *builtin, None),
            ("system", None, None, "claude-code/sonnet", "sonnet", *builtin, "high"),
        ]  # the reviewer's model no longer validated (it was ignored): its layer held nothing else
        assert await pool.fetchval("SELECT count(*) FROM agent_profiles") == 4
        before = await _layers(pool), [tuple(r) for r in await pool.fetch("SELECT * FROM agent_profiles ORDER BY id")]
        await ensure_schema(pool)
        assert (await _layers(pool),
                [tuple(r) for r in await pool.fetch("SELECT * FROM agent_profiles ORDER BY id")]) \
            == before

    _run(_test, monkeypatch, before=_BEFORE_LAYER_PROFILES)


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
             "cache_read_usd_per_mtok": None, "cache_write_usd_per_mtok": None, "efforts": ["low", "high"],
             "background_model": None},
            {"name": "glm-4.5", "context_window": None, "input_usd_per_mtok": None, "output_usd_per_mtok": None,
             "cache_read_usd_per_mtok": None, "cache_write_usd_per_mtok": None, "efforts": None,
             "background_model": None}]
        unlimited = (await _ok("PUT", f"/api/providers/{provider['provider_id']}", {"max_concurrency": None}))
        assert unlimited["provider"]["max_concurrency"] is None
        builtins = [p for p in (await _registry())["providers"] if p["builtin"]]
        assert [(b["max_concurrency"], b["models"]) for b in builtins] == [(None, [])] * 2  # no limit, any model

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
