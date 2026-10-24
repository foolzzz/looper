"""Runtimes, LLM providers and Agent Profiles against a real PostgreSQL, through the HTTP API.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards. A stand-in runtime
type (``StandIn``) plays a second adapter: one that cannot keep a resident
session, and has a login of its own. Runtimes are built in, so a test adds the
row ``looper serve`` would add for one it finds (``_runtime``).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from fake_claude import fake_claude

from looper.agents import api_v1
from looper.agents.ops.clients import ClientOps
from tests.db_support import DSN, run_db
from tests.test_catalog_db import _Writer, _global_settings_request
from tests.test_dsh_runtime import MODELS as DSH_MODELS, dsh_policy as dsh_policy

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

SECRET = "sk-test-registry-0123456789"


async def _call(method: str, path: str, body: Any = None, *, claude: str = "claude") -> tuple[int, Any]:
    from looper.agents.http_server import AgentHTTPServer
    from looper.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())), claude_bin=claude)
    method, path, body = _global_settings_request(method, path, body)
    if path == "/api/v1/tasks/create":
        from tests.test_api_v1_ops_db import _v1

        status, answer = await _v1(method, path, body, ops=ClientOps(SimpleNamespace()))
        return status, answer["result"] if answer["status"] == "ok" else {"error": answer["error"]["message"]}
    writer = _Writer()
    parsed = urlsplit(path)
    raw = b"" if body is None else json.dumps(body).encode()
    if (method == "POST" and parsed.path in ("/api/v1/registry/view", "/api/v1/runtimes/probe")) or (
            parsed.path == "/api/v1/tasks/agents" or parsed.path.startswith(
                ("/api/v1/ops/models/", "/api/v1/models/", "/api/v1/roles/", "/api/v1/agent_settings/", "/api/v1/runs/"))):
        server._client_ops = ClientOps(SimpleNamespace())
        await server._handle_v1(method, parsed.path, dict(parse_qsl(parsed.query)), {}, raw, writer, api_v1.OPERATOR)
    else:
        await server._route_api(method, parsed.path, dict(parse_qsl(parsed.query)), raw, writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    answer = json.loads(payload)
    if (method == "POST" and parsed.path in ("/api/v1/registry/view", "/api/v1/runtimes/probe")) or (
            parsed.path == "/api/v1/tasks/agents" or parsed.path.startswith(
                ("/api/v1/ops/models/", "/api/v1/models/", "/api/v1/roles/", "/api/v1/agent_settings/", "/api/v1/runs/"))):
        answer = answer["result"] if answer["status"] == "ok" else {"error": answer["error"]["message"]}
    return int(head.split()[1]), answer


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
    return await _ok("POST", "/api/v1/registry/view", {})


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


# --- DSH provider policy (test-only adapter) ---


async def _dsh_provider() -> str:
    return await _provider({"name": "gateway", "vendor_type": "custom", "endpoints": [
        {"protocol": "anthropic-messages", "base_url": "https://gateway.example/messages"}]})


@pytest.mark.parametrize("model", DSH_MODELS)
def test_dsh_policy_profile_create_edit_and_provider_update_need_no_key(
    monkeypatch: pytest.MonkeyPatch, dsh_policy: Any, model: str,
) -> None:
    from looper.db import profile_registry as db

    async def refuse_secret_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("registry configuration validation must not read API keys")

    monkeypatch.setattr(db, "get_api_key", refuse_secret_read)

    async def _test(_pool: Any) -> None:
        runtime_id = await _runtime(dsh_policy.type)
        provider_id = await _dsh_provider()
        model_id = await _model_id(provider_id, model)
        profile = (await _ok("POST", "/api/agent-profiles", {
            "name": "dsh-profile", "runtime_id": runtime_id, "model_id": model_id,
        }))["profile"]
        profile_id = profile["profile_id"]
        assert profile["model"]["name"] == model and profile["effort"] is None
        for effort in ("low", "high", "max", None):
            changed = (await _ok("PUT", f"/api/agent-profiles/{profile_id}", {"effort": effort}))["profile"]
            assert changed["effort"] == effort
        # Explicit gateway URLs survive readback, including an existing /v1 suffix.
        changed = (await _ok("PUT", f"/api/providers/{provider_id}", {"endpoints": [
            {"protocol": "anthropic-messages", "base_url": "https://gateway.example/messages/v1/"}],
            "api_key": None,
        }))["provider"]
        assert changed["endpoints"][0]["base_url"] == "https://gateway.example/messages/v1/"
        assert changed["api_key"] is None
        assert (await db.get_profile(profile_id))["model"]["name"] == model

    run_db(_test, monkeypatch)


@pytest.mark.parametrize("change", [
    {"model": "deepseek-chat"}, {"model": "deepseek-flash-20261020"}, {"model": "gpt-6.1-sol"},
    {"effort": "off"}, {"effort": "medium"}, {"effort": "xhigh"},
    {"provider_id": "prov-claude-subscription"},
])
def test_dsh_policy_refuses_invalid_profile_create_and_edit(
    monkeypatch: pytest.MonkeyPatch, dsh_policy: Any, change: dict[str, Any],
) -> None:
    from looper.db import profile_registry as db

    async def _test(_pool: Any) -> None:
        runtime_id = await _runtime(dsh_policy.type)
        provider_id = await _dsh_provider()
        valid = {"name": "dsh-profile", "runtime_id": runtime_id,
                 "model_id": await _model_id(provider_id, "deepseek-flash"), "effort": "low"}
        if "model" in change:
            invalid = {"model_id": await _model_id(provider_id, change["model"])}
        elif "provider_id" in change:
            invalid = {"model_id": await _model_id(change["provider_id"], "opus")}
        else:
            invalid = change
        status, payload = await _call("POST", "/api/agent-profiles", {**valid, **invalid})
        assert status == 400, payload
        assert len(await db.list_profiles()) == 1  # only the built-in profile
        profile_id = (await _ok("POST", "/api/agent-profiles", valid))["profile"]["profile_id"]
        before = await db.get_profile(profile_id)
        status, payload = await _call("PUT", f"/api/agent-profiles/{profile_id}", invalid)
        assert status == 400, payload
        assert await db.get_profile(profile_id) == before

    run_db(_test, monkeypatch)


@pytest.mark.parametrize("protocol,base_url", [
    ("openai-chat", "https://gateway.example"), ("openai-responses", "https://gateway.example"),
    ("anthropic-messages", "ftp://gateway.example"), ("anthropic-messages", "https://"),
])
def test_dsh_policy_refuses_incompatible_endpoints_and_rolls_back_updates(
    monkeypatch: pytest.MonkeyPatch, dsh_policy: Any, protocol: str, base_url: str,
) -> None:
    from looper.db import profile_registry as db

    async def _test(_pool: Any) -> None:
        runtime_id = await _runtime(dsh_policy.type)
        provider_id = await _dsh_provider()
        valid = {"name": "dsh-profile", "runtime_id": runtime_id,
                 "model_id": await _model_id(provider_id, "deepseek-flash")}
        profile_id = (await _ok("POST", "/api/agent-profiles", valid))["profile"]["profile_id"]
        before = await db.get_provider(provider_id)
        endpoint = {"protocol": protocol, "base_url": base_url}
        status, payload = await _call("PUT", f"/api/providers/{provider_id}", {"endpoints": [endpoint]})
        assert status == (409 if protocol != "anthropic-messages" else 400), payload
        assert await db.get_provider(provider_id) == before
        assert (await db.get_profile(profile_id))["model"]["provider_id"] == provider_id

        # A valid standalone endpoint may still be incompatible with DSH's exact protocol.
        status, payload = await _call("POST", "/api/providers", {
            "name": "other", "vendor_type": "custom", "endpoints": [endpoint]})
        if protocol == "anthropic-messages":
            assert status == 400, payload
            return
        assert status == 201, payload
        other_model = await _model_id(payload["provider"]["provider_id"], "deepseek-flash", protocol)
        for method, path, body in (
            ("POST", "/api/agent-profiles", {**valid, "name": "invalid", "model_id": other_model}),
            ("PUT", f"/api/agent-profiles/{profile_id}", {"model_id": other_model}),
        ):
            status, payload = await _call(method, path, body)
            assert status == 400, payload

    run_db(_test, monkeypatch)


def test_dsh_policy_checks_stored_effort_when_a_provider_changes(
    monkeypatch: pytest.MonkeyPatch, dsh_policy: Any,
) -> None:
    from looper.db import profile_registry as db

    async def _test(pool: Any) -> None:
        runtime_id = await _runtime(dsh_policy.type)
        provider_id = await _dsh_provider()
        profile_id = (await _ok("POST", "/api/agent-profiles", {
            "name": "dsh-profile", "runtime_id": runtime_id,
            "model_id": await _model_id(provider_id, "deepseek-flash"), "effort": "low",
        }))["profile"]["profile_id"]
        # Model a stored profile from an older policy; provider updates must not skip its effort.
        await pool.execute("UPDATE agent_profiles SET effort = 'medium' WHERE id = $1", profile_id)
        status, payload = await _call("PUT", f"/api/providers/{provider_id}", {"name": "changed"})
        assert status == 400 and "dsh-profile" in payload["error"]
        assert (await db.get_provider(provider_id))["name"] == "gateway"

    run_db(_test, monkeypatch)


# --- The built-ins ---


def test_every_database_has_the_builtin_runtime_provider_and_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.profile_registry import AgentProfile
    from looper.agents.runtimes import CLAUDE_CODE
    from looper.db import profile_registry as db
    from looper.db.connection import ensure_schema

    async def _test(pool: Any) -> None:
        await pool.execute("DELETE FROM agent_profiles WHERE builtin")  # a missing one comes back
        await ensure_schema(pool)
        await ensure_schema(pool)
        (profile,) = await db.list_profiles()
        spec = AgentProfile.from_row(profile)
        assert (spec.name, spec.runtime.id, spec.model_id) == (
            "claude-code/opus", "rt-claude-code", profile["model"]["id"])
        registry = await _registry()
        (runtime,) = registry["runtimes"]
        assert (runtime["builtin"], runtime["executable"], runtime["version"]) == (True, "", None)
        assert set(runtime["capabilities"]) == {c.value for c in CLAUDE_CODE.capabilities}
        # Claude Code's own login, Codex's (ChatGPT) and Grok's, with the models each offers as suggestions.
        claude, codex, grok = registry["providers"]
        assert (claude["name"], claude["auth"], claude["oauth_runtime_type"], claude["api_key"]) == (
            "claude-subscription", "oauth", "claude-code", None)
        assert (codex["provider_id"], codex["name"], codex["builtin"], codex["auth"], codex["oauth_runtime_type"],
                [e["protocol"] for e in codex["endpoints"]], codex["model_count"]) == (
            "prov-codex-login", "codex-login", True, "oauth", "codex", ["openai-responses"], 7)
        assert codex["suggested_models"][0] == "gpt-6.1-sol" and claude["suggested_models"] == []
        assert (grok["provider_id"], grok["name"], grok["builtin"], grok["auth"], grok["oauth_runtime_type"],
                [e["protocol"] for e in grok["endpoints"]], grok["model_count"], grok["suggested_models"][0]) == (
            "prov-grok-login", "grok-login", True, "oauth", "grok", ["openai-responses"], 4, "grok-4.7")
        assert grok["login"]["state"] == "not_logged_in"  # the tests' GROK_HOME has no login

    run_db(_test, monkeypatch)


@pytest.mark.parametrize(("method", "path", "body"), [
    ("PUT", "/api/providers/prov-claude-subscription", {"name": "renamed"}),
    ("DELETE", "/api/providers/prov-claude-subscription", None),
    ("DELETE", "/api/providers/prov-codex-login", None),
    ("DELETE", "/api/providers/prov-grok-login", None),
    ("PUT", "/api/agent-profiles/agent-claude-code-opus", {"effort": "low"}),
    ("DELETE", "/api/agent-profiles/agent-claude-code-opus", None),
])
def test_the_builtins_are_read_only(monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any) -> None:
    async def _test(_pool: Any) -> None:
        status, payload = await _call(method, path, body)
        assert status == 400 and "built-in" in payload["error"]

    run_db(_test, monkeypatch)


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

    run_db(_test, monkeypatch)


def test_re_detect_probes_a_runtime_and_adds_a_type_found_on_path_since(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.runtimes import CLAUDE_CODE

    claude = _version_claude(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    async def _test(_pool: Any) -> None:
        # The built-in Claude Code runtime probes the server's claude, and says where it is.
        builtin = (await _ok("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-claude-code"}, claude=claude))["runtime"]
        assert (builtin["version"], builtin["probe_error"]) == ("2.1.285 (Claude Code)", None)
        assert builtin["probed_at"] and set(builtin["capabilities"]) == {c.value for c in CLAUDE_CODE.capabilities}
        assert builtin["roles"] == ["manager", "planner", "worker", "reviewer"]
        assert (await _call("POST", "/api/v1/registry/view", {}, claude=claude))[1]["runtimes"][0]["path"] == claude

        # A type not on PATH has no runtime, and Re-detect says why; once it is on PATH, Re-detect adds it.
        monkeypatch.setenv("PATH", str(bin_dir))
        status, payload = await _call("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-codex"})
        assert status == 404 and "Codex (codex) is not on the server's PATH" in payload["error"]
        assert [r["runtime_id"] for r in (await _registry())["runtimes"]] == ["rt-claude-code"]
        _version_codex(bin_dir / "codex")
        codex = (await _ok("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-codex"}))["runtime"]
        assert (codex["builtin"], codex["executable"], codex["version"], codex["path"]) == (
            True, "", "codex-cli 0.162.0", str(bin_dir / "codex"))
        types = {t["type"]: t for t in (await _registry())["runtime_types"]}
        assert (types["codex"]["runtime_id"], types["codex"]["roles"]) == (
            "rt-codex", ["manager", "planner", "worker", "reviewer"])

        # One registered by hand before runtimes were built in probes its own executable.
        legacy = await _runtime("claude-code", name="claude-beta", builtin=False,
                                executable=str(tmp_path / "absent"))
        missing = (await _ok("POST", "/api/v1/runtimes/probe", {"runtime_id": legacy}))["runtime"]
        assert (missing["builtin"], missing["version"], missing["path"]) == (False, None, None)
        assert "was not found" in missing["probe_error"]
        assert (await _call("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-nowhere"}))[0] == 404

    run_db(_test, monkeypatch)


# Runtimes registered by hand before runtimes were built in: "claude-beta" runs a profile, "claude-old" none.
_CUSTOM_RUNTIMES = """
INSERT INTO agent_runtimes (id, name, type, executable, capabilities)
VALUES ('rt-custom-beta', 'claude-beta', 'claude-code', '/opt/claude-beta/bin/claude', ARRAY['run']),
       ('rt-custom-old', 'claude-old', 'claude-code', 'claude-old', ARRAY['run']);
INSERT INTO agent_profiles (id, name, runtime_id, model_id)
SELECT 'agent-beta', 'beta/haiku', 'rt-custom-beta', id FROM llm_models
WHERE provider_id = 'prov-claude-subscription' AND name = 'haiku' AND protocol = 'anthropic-messages';
"""


def test_the_upgrade_drops_custom_runtimes_no_profile_uses_once_and_keeps_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db.connection import ensure_schema

    async def _test(pool: Any) -> None:
        # A database from before: the custom rows, and the migration not run yet.
        await _model_id("prov-claude-subscription", "haiku")
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

    run_db(_test, monkeypatch)


# --- Providers: secrets stored apart and masked ---


def test_a_provider_api_key_is_stored_apart_and_never_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        provider = (await _ok("POST", "/api/providers", {
            "name": "anthropic-key",
            "api_key": SECRET,
            "vendor_type": "custom",
            "endpoints": [
                {"protocol": "anthropic-messages", "base_url": "https://api.anthropic.example"}]}))["provider"]
        provider_id = provider["provider_id"]
        assert provider["api_key"] == "***"
        assert SECRET not in json.dumps(await _registry())
        assert await pool.fetchval("SELECT api_key FROM llm_provider_secrets WHERE provider_id = $1",
                                   provider_id) == SECRET
        assert SECRET not in json.dumps([dict(r) for r in await pool.fetch("SELECT * FROM llm_providers")],
                                        default=str)

        # *** keeps the stored key; another value replaces it; null removes it.
        await _ok("PUT", f"/api/providers/{provider_id}", {"api_key": "***"})
        assert await pool.fetchval("SELECT api_key FROM llm_provider_secrets") == SECRET
        await _ok("PUT", f"/api/providers/{provider_id}", {"api_key": "sk-other"})
        assert await pool.fetchval("SELECT api_key FROM llm_provider_secrets") == "sk-other"
        cleared = (await _ok("PUT", f"/api/providers/{provider_id}", {"api_key": None}))["provider"]
        assert cleared["api_key"] is None and await pool.fetchval("SELECT count(*) FROM llm_provider_secrets") == 0

    run_db(_test, monkeypatch)


@pytest.mark.parametrize(("body", "error"), [
    ({"vendor_type": "custom", "endpoints": []}, "endpoints must be"),
    ({
        "vendor_type": "custom",
        "endpoints": [
            {"protocol": "grpc", "base_url": "https://x.invalid"}]}, "protocol must be"),
    ({"vendor_type": "custom", "auth": "token"}, "unknown field"),
    ({"vendor_type": "custom", "protocols": ["openai-chat"]}, "unknown field"),
    ({"vendor_type": "custom", "base_url": "https://x.invalid"}, "unknown field"),
    ({"vendor_type": "custom", "models": [{"name": "m"}]}, "unknown field"),
    ({
        "vendor_type": "custom",
        "endpoints": [
            {"protocol": "openai-chat", "base_url": "ftp://x"}]}, "base_url must be"),
])
def test_a_provider_that_does_not_validate_is_refused(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], error: str,
) -> None:
    async def _test(_pool: Any) -> None:
        status, payload = await _call("POST", "/api/providers", {"name": "p", **body})
        assert status == 400 and error in payload["error"], payload

    run_db(_test, monkeypatch)


# --- Agent Profiles: checked when created or changed ---


async def _provider(body: dict[str, Any]) -> str:
    return (await _ok("POST", "/api/providers", body))["provider"]["provider_id"]


async def _model_id(provider_id: str, name: str, protocol: str = "anthropic-messages") -> str:
    models = (await _ok("POST", "/api/v1/ops/models/list", {"provider": provider_id}))["models"]
    found = next((m for m in models if m["name"] == name and m["protocol"] == protocol), None)
    if found is None:
        found = (await _ok("POST", "/api/v1/ops/models/create", {
            "provider": provider_id, "name": name, "protocol": protocol}))["model"]
    return found["model_id"]


def test_a_profile_pairs_a_runtime_and_a_provider_that_can_run_together(
    monkeypatch: pytest.MonkeyPatch, stand_in: str,
) -> None:
    async def _test(_pool: Any) -> None:
        standin = await _runtime(stand_in, name="si")
        chat = await _provider({
            "name": "openai-chat",
            "vendor_type": "custom",
            "endpoints": [
                {"protocol": "openai-chat", "base_url": "https://llm.example.invalid/v1"}]})
        key = await _provider({
            "name": "anthropic-key",
            "vendor_type": "custom",
            "endpoints": [
                {"protocol": "anthropic-messages", "base_url": "https://llm.example.invalid/v1"}]})

        async def profile(runtime_id: str, provider_id: str, model: str = "claude-sonnet-4-5") -> tuple[int, Any]:
            return await _call("POST", "/api/agent-profiles", {"name": f"a-{runtime_id}-{provider_id}-{model}",
                                                               "runtime_id": runtime_id,
                                                               "model_id": await _model_id(
                                                                   provider_id, model, "openai-chat" if provider_id == chat
                                                                   else "anthropic-messages")})

        status, payload = await profile("rt-claude-code", "prov-claude-subscription", "sonnet")
        assert status == 201 and payload["profile"]["runtime_name"] == "claude-code"
        assert (await profile(standin, key))[0] == 201  # an adapter that takes API keys

        # Protocols must intersect.
        status, payload = await profile("rt-claude-code", chat)
        assert status == 400 and "exact protocol" in payload["error"]
        # An OAuth provider pairs only with the runtime type whose login it is, either way round.
        status, payload = await profile(standin, "prov-claude-subscription", "sonnet")
        assert status == 400 and "only use its own runtime" in payload["error"]
        # Claude Code runs with its own login and with an API key.
        assert (await profile("rt-claude-code", key))[0] == 201
        # Profiles select a registered model id, never an account catalog entry.
        status, payload = await _call("POST", "/api/agent-profiles", {
            "name": "missing", "runtime_id": standin, "model_id": "model-nowhere"})
        assert status == 400 and "no model" in payload["error"]
        # A runtime that takes only its own login refuses an API key.
        from looper.agents import runtimes
        from looper.agents.runtimes import AuthKind

        monkeypatch.setattr(type(runtimes.ADAPTERS[stand_in]), "auth_kinds", frozenset({AuthKind.OAUTH}))
        status, payload = await profile(standin, key, "claude-sonnet-4-5")
        assert status == 400 and "auth kind" in payload["error"]

    run_db(_test, monkeypatch)


@pytest.mark.parametrize("field", ["runtime_id", "model_id"])
@pytest.mark.parametrize("invalid", [None, "", "missing", 3, [], {}])
@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_profile_identity_fields_require_registered_ids(
    monkeypatch: pytest.MonkeyPatch, field: str, invalid: Any, method: str,
) -> None:
    async def _test(_pool: Any) -> None:
        model_id = await _model_id("prov-claude-subscription", "sonnet")
        valid = {"name": "identity", "runtime_id": "rt-claude-code", "model_id": model_id}
        if method == "PUT":
            profile = (await _ok("POST", "/api/agent-profiles", valid))["profile"]
            path = f"/api/agent-profiles/{profile['profile_id']}"
            body = {field: invalid}
        else:
            path = "/api/agent-profiles"
            body = {**valid, field: invalid}
        status, payload = await _call(method, path, body)
        assert status == 400 and f"no {field.removesuffix('_id')}" in payload["error"]
        profiles = (await _registry())["profiles"]
        if method == "PUT":
            saved = _by_name(profiles, "identity")
            assert (saved["runtime_id"], saved["model_id"]) == ("rt-claude-code", model_id)
        else:
            assert all(profile["name"] != "identity" for profile in profiles)

    run_db(_test, monkeypatch)


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
        runtime = (await _ok("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-codex"}))["runtime"]
        assert (runtime["version"], runtime["type_label"], runtime["protocols"]) == (
            "codex-cli 0.162.0", "Codex", ["openai-responses"])
        cpa = await _provider({
            "name": "cpa",
            "api_key": SECRET,
            "vendor_type": "custom",
            "endpoints": [
                {"protocol": "openai-responses", "base_url": "http://127.0.0.1:8317/v1"}]})
        chat = await _provider({
            "name": "chat-only",
            "vendor_type": "custom",
            "endpoints": [
                {"protocol": "openai-chat", "base_url": "https://llm.example.invalid/v1"}]})
        anthropic = await _provider({
            "name": "anthropic-key",
            "vendor_type": "custom",
            "endpoints": [
                {"protocol": "anthropic-messages", "base_url": "https://llm.example.invalid/v1"}]})

        async def profile(provider_id: str) -> tuple[int, Any]:
            return await _call("POST", "/api/agent-profiles", {
                "name": f"codex-{provider_id}", "runtime_id": runtime["runtime_id"],
                "model_id": await _model_id(provider_id, "gpt-5.5",
                    "openai-chat" if provider_id == chat else "anthropic-messages"
                    if provider_id in (anthropic, "prov-claude-subscription") else "openai-responses")})

        status, payload = await profile(cpa)
        assert status == 201
        # Every role can run it: the system layer selects it for all four.
        await _ok("PUT", "/api/agent-settings/system", {"profile_id": payload["profile"]["profile_id"]})
        for provider_id in (chat, anthropic, "prov-claude-subscription"):
            status, payload = await profile(provider_id)
            assert status == 400 and "exact protocol" in payload["error"]
        # Codex's own login: any model, priced at the built-in prices as a subscription equivalent.
        status, payload = await profile("prov-codex-login")
        assert status == 201 and payload["warnings"] == []
        assert payload["profile"]["cost"] == {"source": "provider_price", "equivalent": True, "unpriced": []}
        await _ok("PUT", "/api/agent-settings/system", {"profile_id": payload["profile"]["profile_id"]})

    run_db(_test, monkeypatch)


def test_the_codex_login_pairs_only_with_codex_runtimes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _version_omp(tmp_path / "omp")
    monkeypatch.setenv("PATH", str(tmp_path))

    async def _test(_pool: Any) -> None:
        omp = (await _ok("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-omp"}))["runtime"]["runtime_id"]
        for runtime_id in ("rt-claude-code", omp):
            status, payload = await _call("POST", "/api/agent-profiles", {
                "name": f"{runtime_id}-login", "runtime_id": runtime_id,
                "model_id": await _model_id("prov-codex-login", "gpt-6.1-sol", "openai-responses")})
            assert status == 400, payload
            assert "only use its own runtime" in payload["error"] or "exact protocol" in payload["error"]
        # A login problem is cleared only on the Codex login.
        status, payload = await _call("DELETE", "/api/providers/prov-claude-subscription/login-problem")
        assert status == 400 and "is not the Codex or Grok login" in payload["error"]

    run_db(_test, monkeypatch)


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

    run_db(_test, monkeypatch)


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
        runtime = (await _ok("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-omp"}))["runtime"]
        assert (runtime["version"], runtime["type_label"], runtime["protocols"]) == (
            "omp/18.4.3", "OMP", ["anthropic-messages", "openai-chat", "openai-responses"])
        providers = [
            await _provider({
                "name": "cpa",
                "api_key": SECRET,
                "vendor_type": "custom",
                "endpoints": [
                    {"protocol": "openai-responses", "base_url": "http://127.0.0.1:8317/v1"},
                    {"protocol": "openai-chat", "base_url": "http://127.0.0.1:8317/v1"}]}),
            await _provider({
                "name": "chat-only",
                "vendor_type": "custom",
                "endpoints": [
                    {"protocol": "openai-chat", "base_url": "https://llm.example.invalid/v1"}]}),
            await _provider({
                "name": "anthropic-key",
                "vendor_type": "custom",
                "endpoints": [
                    {"protocol": "anthropic-messages", "base_url": "https://llm.example.invalid/v1"}]}),
        ]

        async def profile(provider_id: str) -> tuple[int, Any]:
            return await _call("POST", "/api/agent-profiles", {
                "name": f"omp-{provider_id}", "runtime_id": runtime["runtime_id"],
                "model_id": await _model_id(provider_id, "gpt-5.5",
                    "openai-responses" if provider_id == providers[0] else "openai-chat"
                    if provider_id == providers[1] else "anthropic-messages")})

        profiles = []
        for provider_id in providers:
            status, payload = await profile(provider_id)
            assert status == 201, payload
            profiles.append(payload["profile"]["profile_id"])
        # Every role can run it: the system layer selects it for all four.
        await _ok("PUT", "/api/agent-settings/system", {"profile_id": profiles[0]})
        # The Claude subscription is Claude Code's login, and OMP's own logins are not ones Looper uses.
        status, payload = await profile("prov-claude-subscription")
        assert status == 400 and "only use its own runtime" in payload["error"]
        status, payload = await _call("POST", "/api/providers", {
            "name": "omp-login", "vendor_type": "omp-login"})
        assert status == 400 and "vendor_type must be" in payload["error"]

    run_db(_test, monkeypatch)


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

    run_db(_test, monkeypatch)


def _version_dsh(path: Path, version: str = "0.2.1-rc.2") -> str:
    """Exercise the real adapter's version/help probe without starting a model or plugins."""
    return fake_claude(path, (
        "import os, pathlib, sys\n"
        # The probe must isolate every path before invoking the executable.
        "assert pathlib.Path.cwd() == pathlib.Path(os.environ['HOME']).parent\n"
        "assert pathlib.Path(os.environ['DSH_HOME']).parent == pathlib.Path.cwd()\n"
        f"if sys.argv[1:] == ['--version']:\n    print('dsh ' + {version!r})\n    sys.exit(0)\n"
        "if sys.argv[1:] == ['--help']:\n    print('dsh --profile headless --json')\n    sys.exit(0)\n"
        "sys.exit(9)\n"
    ))


def test_dsh_path_registration_is_once_and_redetect_updates_the_persisted_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.profile_registry import register_found_runtimes
    from looper.agents.runtimes.dsh import DshRuntime, SUPPORTED_VERSION
    from looper.db import profile_registry as db

    monkeypatch.setenv("PATH", str(tmp_path))

    async def _test(pool: Any) -> None:
        status, payload = await _call("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-dsh"})
        assert status == 404 and "DSH (dsh) is not on the server's PATH" in payload["error"]
        assert await register_found_runtimes() == []
        _version_dsh(tmp_path / "dsh")
        assert await register_found_runtimes() == ["dsh"]
        runtime = _by_name((await _registry())["runtimes"], "dsh")
        assert (runtime["runtime_id"], runtime["builtin"], runtime["executable"], runtime["version"],
                runtime["probe_error"], runtime["path"], runtime["roles"]) == (
            "rt-dsh", True, "", SUPPORTED_VERSION, None, str(tmp_path / "dsh"),
            ["manager", "planner", "worker", "reviewer"])
        assert set(runtime["capabilities"]) == {c.value for c in DshRuntime.capabilities}
        original = await db.get_runtime("rt-dsh")
        assert await register_found_runtimes() == []
        assert await db.get_runtime("rt-dsh") == original

        _version_dsh(tmp_path / "dsh", "0.1.0")
        unsupported = (await _ok("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-dsh"}))["runtime"]
        assert unsupported["version"] == "0.1.0"
        assert "Unsupported DSH version" in unsupported["probe_error"]
        assert unsupported["capabilities"] == [] and unsupported["roles"] == []
        assert (await db.get_runtime("rt-dsh"))["version"] == "0.1.0"

        (tmp_path / "dsh").unlink()
        missing = (await _ok("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-dsh"}))["runtime"]
        assert missing["version"] is None and missing["path"] is None
        assert "was not found" in missing["probe_error"]
        assert missing["capabilities"] == [] and missing["roles"] == []

        _version_dsh(tmp_path / "dsh")
        restored = (await _ok("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-dsh"}))["runtime"]
        assert restored["version"] == SUPPORTED_VERSION and restored["probe_error"] is None
        assert restored["roles"] == runtime["roles"]
        assert await pool.fetchval("SELECT count(*) FROM agent_runtimes WHERE type = 'dsh'") == 1

    run_db(_test, monkeypatch)


def test_dsh_profile_and_all_four_role_selections_round_trip_through_api_and_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.agent_settings import load_agent_settings
    from looper.db import profile_registry as db

    _version_dsh(tmp_path / "dsh")
    monkeypatch.setenv("PATH", str(tmp_path))

    async def _test(pool: Any) -> None:
        await _ok("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-dsh"})
        provider_id = await _provider({"name": "DeepSeek", "vendor_type": "deepseek", "api_key": SECRET,
                                       "endpoints": [{"protocol": "anthropic-messages",
                                                      "base_url": "https://api.deepseek.com/anthropic"}]})
        profile = (await _ok("POST", "/api/agent-profiles", {
            "name": "dsh-flash", "runtime_id": "rt-dsh",
            "model_id": await _model_id(provider_id, "deepseek-flash"), "effort": "high",
        }))["profile"]
        profile_id = profile["profile_id"]
        assert profile["roles"] == ["manager", "planner", "worker", "reviewer"]
        for role in profile["roles"]:
            await _ok("PUT", f"/api/agent-settings/roles/{role}", {"profile_id": profile_id})
        view = await _ok("GET", "/api/agent-settings")
        for role in profile["roles"]:
            selected = view["roles"][role]
            assert selected["values"]["profile_id"] == profile_id
            assert selected["effective"]["profile"] == {
                "value": {"profile_id": profile_id, "name": "dsh-flash", "model": "deepseek-flash",
                          "effort": "high", "runtime": "dsh", "provider": "DeepSeek"},
                "source": "role"}
            settings = await load_agent_settings(role)
            assert settings.profile.id == profile_id
            assert settings.profile.runtime.type == "dsh"
            assert settings.profile.provider.id == provider_id
        rows = await pool.fetch("SELECT role, profile_id FROM agent_setting_layers WHERE scope = 'role'")
        assert {row["role"]: row["profile_id"] for row in rows} == {
            role: profile_id for role in profile["roles"]}
        stored = await db.get_profile(profile_id)
        assert (stored["runtime_id"], stored["model"]["provider_id"], stored["model"]["name"], stored["effort"]) == (
            "rt-dsh", provider_id, "deepseek-flash", "high")
        assert await db.get_api_key(provider_id) == SECRET
        registry = await _registry()
        assert SECRET not in json.dumps(registry)
        readback = _by_name(registry["profiles"], "dsh-flash")
        assert {layer["role"] for layer in readback["used_by"]["layers"]} == set(profile["roles"])

    run_db(_test, monkeypatch)


def test_dsh_provider_cannot_remove_the_endpoint_needed_by_its_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.db import profile_registry as db

    _version_dsh(tmp_path / "dsh")
    monkeypatch.setenv("PATH", str(tmp_path))

    async def _test(_pool: Any) -> None:
        await _ok("POST", "/api/v1/runtimes/probe", {"runtime_id": "rt-dsh"})
        provider_id = await _provider({"name": "DeepSeek", "vendor_type": "deepseek", "api_key": SECRET,
                                       "endpoints": [{"protocol": "anthropic-messages",
                                                      "base_url": "https://api.deepseek.com/anthropic"}]})
        profile_id = (await _ok("POST", "/api/agent-profiles", {
            "name": "dsh-flash", "runtime_id": "rt-dsh",
            "model_id": await _model_id(provider_id, "deepseek-flash"),
        }))["profile"]["profile_id"]
        before_provider, before_profile = await db.get_provider(provider_id), await db.get_profile(profile_id)
        # Endpoints are account settings; removing one would orphan the profile's exact model.
        status, payload = await _call("PUT", f"/api/providers/{provider_id}", {
            "name": "must-rollback", "api_key": "must-not-replace-the-key",
            "endpoints": [{"protocol": "openai-responses", "base_url": "https://other.example/v1"}],
        })
        assert status == 409 and "endpoint" in payload["error"]
        assert await db.get_provider(provider_id) == before_provider
        assert await db.get_profile(profile_id) == before_profile
        assert await db.get_api_key(provider_id) == SECRET

    run_db(_test, monkeypatch)


def _version_grok(path: Path) -> str:
    """A stand-in grok that prints its version for ``--version`` and fails on anything else."""
    return fake_claude(path, (
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n    print('grok 1.0.62 (1474f9cbdd91) [stable]')\n    sys.exit(0)\n"
        "sys.exit(9)\n"
    ))


def test_grok_on_path_becomes_a_builtin_runtime_that_pairs_only_with_its_own_login_and_runs_every_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.profile_registry import register_found_runtimes

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _version_grok(bin_dir / "grok")
    _version_codex(bin_dir / "codex")

    async def _test(_pool: Any) -> None:
        monkeypatch.setenv("PATH", str(bin_dir))
        assert await register_found_runtimes() == ["codex", "grok"]
        runtime = _by_name((await _registry())["runtimes"], "grok")
        assert (runtime["runtime_id"], runtime["builtin"], runtime["version"], runtime["type_label"],
                runtime["protocols"], runtime["roles"]) == (
            "rt-grok", True, "grok 1.0.62 (1474f9cbdd91) [stable]", "Grok", ["openai-responses"],
            ["manager", "planner", "worker", "reviewer"])

        async def profile(provider_id: str, runtime_id: str = "rt-grok") -> tuple[int, Any]:
            return await _call("POST", "/api/agent-profiles", {
                "name": f"{runtime_id}-{provider_id}", "runtime_id": runtime_id,
                "model_id": await _model_id(provider_id, "grok-4.7",
                    "anthropic-messages" if provider_id == "prov-claude-subscription" else "openai-responses")})

        # Its own login: any model, priced by Grok's own figure as a subscription equivalent, which no per-run
        # cap can stop.
        status, payload = await profile("prov-grok-login")
        assert status == 201, payload
        assert payload["profile"]["cost"] == {"source": "runtime", "equivalent": True, "unpriced": []}
        (warning,) = payload["warnings"]
        assert "per-run spend caps do not stop its runs" in warning
        await _ok("PUT", "/api/agent-settings/system", {"profile_id": payload["profile"]["profile_id"]})
        # Not another runtime's login, not an API key; and its login runs no other runtime.
        key = await _provider({
            "name": "cpa",
            "api_key": SECRET,
            "vendor_type": "custom",
            "endpoints": [
                {"protocol": "openai-responses", "base_url": "http://127.0.0.1:8317/v1"}]})
        for provider_id in ("prov-codex-login", "prov-claude-subscription", key):
            status, payload = await profile(provider_id)
            assert status == 400, payload
        status, payload = await profile("prov-grok-login", "rt-codex")
        assert status == 400 and "only use its own runtime" in payload["error"]

    run_db(_test, monkeypatch)


def _version_pi(path: Path) -> str:
    """A stand-in pi 1.3.0 whose help lists the flags the probe requires."""
    from looper.agents.runtimes.pi import PROBE_HELP_TOKENS

    help_text = " ".join(PROBE_HELP_TOKENS)
    return fake_claude(path, (
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n    print('1.3.0')\n    sys.exit(0)\n"
        "if sys.argv[1:] == ['--help']:\n"
        f"    print({help_text!r})\n    sys.exit(0)\n"
        "sys.exit(9)\n"
    ))


def test_pi_on_path_is_discovered_once_offers_every_role_and_refuses_other_auth_and_protocols(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.profile_registry import register_found_runtimes
    from looper.agents.runtimes.pi import PiRuntime

    capabilities = sorted(capability.value for capability in PiRuntime.capabilities)
    roles = ["manager", "planner", "worker", "reviewer"]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _version_pi(bin_dir / "pi")

    async def _test(_pool: Any) -> None:
        monkeypatch.setenv("PATH", str(bin_dir))
        assert await register_found_runtimes() == ["pi"]
        assert await register_found_runtimes() == []  # once
        registry = await _registry()
        runtime = _by_name(registry["runtimes"], "pi")
        assert (runtime["runtime_id"], runtime["builtin"], runtime["executable"], runtime["version"],
                runtime["type_label"], runtime["protocols"], runtime["capabilities"], runtime["roles"],
                runtime["probe_error"]) == (
            "rt-pi", True, "", "1.3.0", "pi", ["openai-chat", "openai-responses"], capabilities, roles, None)
        kind = next(item for item in registry["runtime_types"] if item["type"] == "pi")
        assert (kind["label"], kind["default_executable"], kind["runtime_id"], kind["auth_kinds"],
                kind["protocols"], kind["capabilities"], kind["roles"]) == (
            "pi", "pi", "rt-pi", ["api_key"], ["openai-chat", "openai-responses"], capabilities, roles)
        assert all(provider["oauth_runtime_type"] != "pi" for provider in registry["providers"])

        async def profile(provider_id: str, model: str = "gpt-6.1-sol") -> tuple[int, Any]:
            return await _call("POST", "/api/agent-profiles", {
                "name": f"pi-{provider_id}", "runtime_id": "rt-pi",
                "model_id": await _model_id(provider_id, model, "anthropic-messages"
                    if provider_id in (anthropic, "prov-claude-subscription") else
                    "openai-responses" if provider_id == responses else "openai-chat")})

        # Both supported protocols pair and can be selected for every role.
        made = []
        anthropic, responses = "", ""
        for name, protocols in (("responses", ["openai-responses"]), ("chat", ["openai-chat"])):
            provider_id = await _provider({"name": name, "vendor_type": "custom", "api_key": SECRET,
                                           "endpoints": [{"protocol": protocols[0],
                                                          "base_url": "https://api.example.invalid/v1"}]})
            if name == "responses":
                responses = provider_id
            status, payload = await profile(provider_id)
            assert status == 201, payload
            assert payload["profile"]["roles"] == roles
            made.append(payload["profile"]["profile_id"])
        for profile_id in made:
            await _ok("PUT", "/api/agent-settings/system", {"profile_id": profile_id})
            for role in roles:
                await _ok("PUT", f"/api/agent-settings/roles/{role}", {"profile_id": profile_id})

        # Pi has no login vendor. Its exact model protocol must be supported.
        status, payload = await _call("POST", "/api/providers", {
            "name": "pi-login", "vendor_type": "pi-login", "endpoints": []})
        assert status == 400 and "vendor_type" in payload["error"]
        anthropic = await _provider({"name": "anthropic-key", "vendor_type": "custom", "endpoints": [
            {"protocol": "anthropic-messages", "base_url": "https://messages.example.invalid"}]})
        status, payload = await profile(anthropic)
        assert status == 400 and "exact protocol" in payload["error"]
        status, payload = await profile("prov-claude-subscription")
        assert status == 400 and "exact protocol" in payload["error"]

    run_db(_test, monkeypatch)


def test_a_change_that_would_break_a_profile_is_refused_and_one_in_use_is_kept(
    monkeypatch: pytest.MonkeyPatch, stand_in: str,
) -> None:
    async def _test(_pool: Any) -> None:
        runtime_id = await _runtime(stand_in, name="si")
        provider_id = await _provider({
            "name": "key",
            "vendor_type": "custom",
            "endpoints": [
                {"protocol": "anthropic-messages", "base_url": "https://llm.example.invalid/v1"}]})
        profile = (await _ok("POST", "/api/agent-profiles", {"name": "si-sonnet", "runtime_id": runtime_id,
                                                             "model_id": await _model_id(provider_id, "sonnet"),
                                                             "effort": "low"}))["profile"]

        status, payload = await _call("PUT", f"/api/providers/{provider_id}", {"endpoints": [
            {"protocol": "openai-chat", "base_url": "https://llm.example.invalid/v1"}]})
        assert status == 409 and "models" in payload["error"]
        status, payload = await _call("PUT", f"/api/agent-profiles/{profile['profile_id']}",
                                      {"model_id": await _model_id("prov-claude-subscription", "sonnet")})
        assert status == 400 and "only use its own runtime" in payload["error"]
        renamed = (await _ok("PUT", f"/api/agent-profiles/{profile['profile_id']}",
                             {"name": "si-fast", "effort": None}))["profile"]
        assert (renamed["name"], renamed["effort"]) == ("si-fast", None)

        status, payload = await _call("DELETE", f"/api/providers/{provider_id}")
        assert status == 409 and "first" in payload["error"]
        await _ok("DELETE", f"/api/agent-profiles/{profile['profile_id']}")
        await _ok("POST", "/api/v1/ops/models/delete", {"model": profile['model_id']})
        await _ok("DELETE", f"/api/providers/{provider_id}")
        assert runtime_id in {r["runtime_id"] for r in (await _registry())["runtimes"]}

    run_db(_test, monkeypatch)


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

    run_db(_test, monkeypatch, before=_BEFORE_AGENTS)


# --- Selecting a profile: the layers and a task's overrides, checked for the roles they reach ---


async def _profile(name: str, runtime_id: str = "rt-claude-code", provider_id: str = "prov-claude-subscription",
                   model: str | None = None) -> str:
    return (await _ok("POST", "/api/agent-profiles", {
        "name": name, "runtime_id": runtime_id,
        "model_id": await _model_id(provider_id, model or name.rsplit("/", 1)[-1])}))["profile"]["profile_id"]


async def _stand_in_profile(stand_in: str) -> str:
    runtime_id = await _runtime(stand_in, name="si")
    provider_id = (await _ok("POST", "/api/providers", {
        "name": "si-key",
        "vendor_type": "custom",
        "endpoints": [
            {"protocol": "anthropic-messages", "base_url": "https://llm.example.invalid/v1"}]}))["provider"]["provider_id"]
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
                                                                                "model_id": await _model_id(si_key, "sonnet")})
        assert status == 400 and "cannot run the planner" in payload["error"]
        # A selected profile stays until nothing selects it.
        assert (await _call("DELETE", f"/api/agent-profiles/{planner}"))[0] == 409

    run_db(_test, monkeypatch)


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
            await _ok("POST", "/api/v1/tasks/create", {"requirement": "r", "project_id": calc,
                                                    "profile_overrides": {"worker": sonnet, "reviewer": sonnet}})

        profiles = {a["name"]: a for a in (await _registry())["profiles"]}
        assert profiles["claude-code/sonnet"]["used_by"] == {"layers": [
            {"scope": "system", "role": None, "project_id": None, "project_name": None},
            {"scope": "role", "role": "worker", "project_id": None, "project_name": None},
            {"scope": "project", "role": None, "project_id": calc, "project_name": "calc"},
        ], "task_count": 2, "schedule_count": 0, "allowed_in": [], "manager_session": False,
            "manager_sessions": [], "managers": [], "manager_templates": []}
        assert profiles["si/sonnet"]["used_by"]["layers"] == [
            {"scope": "role_project", "role": "reviewer", "project_id": calc, "project_name": "calc"}]
        assert profiles["claude-code/haiku"]["used_by"] == {"layers": [], "task_count": 0, "schedule_count": 0,
                                                            "allowed_in": [], "manager_session": False,
                                                            "manager_sessions": [], "managers": [], "manager_templates": []}
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
        # On a project's list for its Planners, a profile stays until the list drops it.
        await _ok("PUT", f"/api/projects/{calc}/allowed-profiles", {"profile_ids": [haiku]})
        used = {p["name"]: p["used_by"] for p in (await _registry())["profiles"]}
        assert used["claude-code/haiku"]["allowed_in"] == ["calc"]
        status, payload = await _call("DELETE", f"/api/agent-profiles/{haiku}")
        assert status == 409 and payload["error"] == (
            "Agent Profile 'claude-code/haiku' is selected by the profiles the Planners of project 'calc' may choose: "
            "select another profile there first")
        await _ok("PUT", f"/api/projects/{calc}/allowed-profiles", {"profile_ids": []})
        await _ok("DELETE", f"/api/agent-profiles/{haiku}")  # nothing selects it

    run_db(_test, monkeypatch)


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
        chose = (await _ok("POST", "/api/v1/tasks/create", {"requirement": "r", "project_id": project,
                                                         "profile_overrides": {"worker": haiku}}))
        plain = await _ok("POST", "/api/v1/tasks/create", {"requirement": "r", "project_id": project})

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
        for overrides, error in [({"manager": sonnet}, "declared schema"),
                                 ({"worker": "agent-nowhere"}, "no Agent Profile"),
                                 ({"planner": one_off}, "cannot run the planner"), ([sonnet], "declared schema")]:
            status, payload = await _call("POST", "/api/v1/tasks/create", {"requirement": "r", "project_id": project,
                                                                        "profile_overrides": overrides})
            assert status == 400 and error in payload["error"], payload
        assert (await _call("POST", "/api/v1/tasks/create", {"requirement": "r", "project_id": project,
                                                          "profile_overrides": {"worker": one_off}}))[0] == 200

    run_db(_test, monkeypatch)


def test_a_profile_that_can_no_longer_run_fails_the_run_instead_of_another_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stand_in: str,
) -> None:
    from looper.agents import runtimes

    async def _test(_pool: Any) -> None:
        one_off = await _stand_in_profile(stand_in)
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": one_off})
        project = (await _ok("POST", "/api/projects", {"name": "calc"}))["project_id"]
        task = (await _ok("POST", "/api/v1/tasks/create", {"requirement": "r", "project_id": project,
                                                        "work_dir": str(tmp_path / "w")}))["task_id"]
        monkeypatch.delitem(runtimes.ADAPTERS, stand_in)  # a version without that runtime type

        result = await _launch("worker", task, tmp_path / "w", tmp_path)
        assert "argv" not in result  # no runtime started
        assert result["outcome"]["status"] == "failed"
        assert "runs on a 'stand-in' runtime, which this version cannot run" in result["outcome"]["error"]

    run_db(_test, monkeypatch)


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
            "SELECT l.scope, l.role, l.project_id, a.name, m.name, a.runtime_id, m.provider_id, a.effort "
            "FROM agent_setting_layers l LEFT JOIN agent_profiles a ON a.id = l.profile_id "
            "LEFT JOIN llm_models m ON m.id = a.model_id ORDER BY 1, 2, 3")]

    async def _test(pool: Any) -> None:
        columns = {r["column_name"] for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'agent_setting_layers'")}
        assert "model" not in columns and "profile_id" in columns
        builtin = ("rt-claude-code", "prov-claude-subscription")
        # Each model's profile, then (the effort's upgrade) the one of each with the system's effort, which every
        # role ran with.
        assert await _layers(pool) == [
            ("project", None, "proj-a", "claude-code/opus[1m] · high", "opus[1m]", *builtin, "high"),
            ("role", "manager", None, "claude-code/opus · high", "opus", *builtin, "high"),
            ("role", "planner", None, "claude-code/haiku · high", "haiku", *builtin, "high"),
            ("role", "worker", None, "claude-code/sonnet · high", "sonnet", *builtin, "high"),
            ("system", None, None, "claude-code/sonnet · high", "sonnet", *builtin, "high"),
        ]  # the reviewer's model no longer validated (it was ignored): its layer held nothing else
        assert await pool.fetchval("SELECT count(*) FROM agent_profiles") == 8  # the built-in opus among them
        before = await _layers(pool), [tuple(r) for r in await pool.fetch("SELECT * FROM agent_profiles ORDER BY id")]
        await ensure_schema(pool)
        assert (await _layers(pool),
                [tuple(r) for r in await pool.fetch("SELECT * FROM agent_profiles ORDER BY id")]) \
            == before

    run_db(_test, monkeypatch, before=_BEFORE_LAYER_PROFILES)


# --- A provider's model catalog and concurrency: stored and checked ---


def test_a_provider_keeps_its_model_catalog_and_concurrency_with_unknowns_as_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _test(_pool: Any) -> None:
        provider = (await _ok("POST", "/api/providers", {
            "name": "glm",
            "max_concurrency": 4,
            "vendor_type": "custom",
            "endpoints": [
                {"protocol": "anthropic-messages", "base_url": "https://llm.example.invalid/v1"},
                {"protocol": "openai-chat", "base_url": "https://llm.example.invalid/v1"}]}))["provider"]
        assert provider["max_concurrency"] == 4
        assert provider["model_count"] == 0
        first = (await _ok("POST", "/api/v1/ops/models/create", {
            "provider": provider["provider_id"], "name": "glm-4.6", "protocol": "anthropic-messages",
            "context_window": 200_000, "input_usd_per_mtok": 0.6, "efforts": ["low", "high"]}))["model"]
        second = (await _ok("POST", "/api/v1/ops/models/create", {
            "provider": provider["provider_id"], "name": "glm-4.5", "protocol": "anthropic-messages"}))["model"]
        assert (first["context_window"], first["input_usd_per_mtok"], first["efforts"]) == (
            200_000, 0.6, ["low", "high"])
        assert all(second[key] is None for key in (
            "context_window", "input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok",
            "cache_write_usd_per_mtok", "efforts", "background_model"))
        unlimited = (await _ok("PUT", f"/api/providers/{provider['provider_id']}", {"max_concurrency": None}))
        assert unlimited["provider"]["max_concurrency"] is None
        builtins = [p for p in (await _registry())["providers"] if p["builtin"]]
        assert all(b["max_concurrency"] is None and b["model_count"] > 0 for b in builtins)

    run_db(_test, monkeypatch)


@pytest.mark.parametrize(("change", "error"), [
    ({"context_window": 0}, "context_window must be"),
    ({"efforts": ["Low"]}, "efforts must be"),
    ({"input_usd_per_mtok": -1}, "per million tokens"),
    ({"speed": 1}, "unknown argument"),
])
def test_model_settings_that_do_not_validate_are_refused(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any], error: str,
) -> None:
    async def _test(_pool: Any) -> None:
        provider = await _provider({"name": "p", "vendor_type": "custom", "endpoints": [
            {"protocol": "openai-chat", "base_url": "https://llm.example.invalid/v1"}]})
        status, payload = await _call("POST", "/api/v1/ops/models/create", {
            "provider": provider, "name": "m", "protocol": "openai-chat", **change})
        assert status == 400 and error in payload["error"], payload
        status, payload = await _call("PUT", f"/api/providers/{provider}", {"max_concurrency": 0})
        assert status == 400 and "max_concurrency must be" in payload["error"]

    run_db(_test, monkeypatch)
