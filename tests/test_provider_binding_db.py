"""Claude Code with API-key providers, against a real PostgreSQL, through the HTTP API and the agent tiers.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own. Runs use stand-in ``claude``
executables that write down what they were started with or play a stream;
the provider endpoints are never contacted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from tests.test_profile_registry_db import _call, _launch, _ok, _run
from tests.test_catalog_db import DSN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

KEY = "sk-test-binding-0123456789"
THIRD_PARTY = "https://llm.example.invalid/api/anthropic"


async def _provider(name: str = "glm", *, base_url: str = THIRD_PARTY, api_key: str | None = KEY,
                    models: list[dict[str, Any]] | None = None, env: list[dict[str, str]] | None = None) -> str:
    body: dict[str, Any] = {"name": name, "protocols": ["anthropic-messages"], "auth": "api_key",
                            "base_url": base_url, "models": models or [], "env": env or []}
    if api_key is not None:
        body["api_key"] = api_key
    return (await _ok("POST", "/api/providers", body))["provider"]["provider_id"]


async def _profile(provider_id: str, model: str, name: str | None = None) -> dict[str, Any]:
    return await _ok("POST", "/api/agent-profiles", {"name": name or f"cc-{provider_id}-{model}",
                                                     "runtime_id": "rt-claude-code", "provider_id": provider_id,
                                                     "model": model})


async def _task(tmp_path: Path) -> str:
    project = (await _ok("POST", "/api/projects", {"name": "calc"}))["project_id"]
    return (await _ok("POST", "/api/tasks/create", {"requirement": "r", "project_id": project,
                                                    "work_dir": str(tmp_path / "w")}))["task_id"]


def _env_of(seen: dict[str, Any], *names: str) -> dict[str, str | None]:
    return {name: seen["env"].get(name) for name in names}


MAPPED = ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
          "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")


# --- The binding ---


def test_a_third_party_provider_gets_its_endpoint_key_and_every_model_alias_and_no_claude_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # The server's own environment holds an Anthropic key: it must not reach a third-party endpoint.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-operator-own")

    async def _test(pool: Any) -> None:
        provider = await _provider(models=[{"name": "glm-5.3[1m]", "background_model": "glm-5.3-flash[1m]"},
                                           {"name": "glm-4.7"}],
                                   env=[{"key": "API_TIMEOUT_MS", "value": "3000000"}])
        main = (await _profile(provider, "glm-5.3[1m]"))["profile"]["profile_id"]
        plain = (await _profile(provider, "glm-4.7"))["profile"]["profile_id"]
        task = await _task(tmp_path)
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": main, "run_budget_usd": 0.5})
        await _ok("PUT", "/api/agent-settings/roles/reviewer", {"profile_id": plain})

        worker = await _launch("worker", task, tmp_path / "w", tmp_path)
        assert _env_of(worker, "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "API_TIMEOUT_MS") \
            == {"ANTHROPIC_BASE_URL": THIRD_PARTY, "ANTHROPIC_AUTH_TOKEN": KEY, "ANTHROPIC_API_KEY": None,
                "API_TIMEOUT_MS": "3000000"}
        assert _env_of(worker, *MAPPED) == dict.fromkeys(MAPPED[:3], "glm-5.3[1m]") \
            | dict.fromkeys(MAPPED[3:], "glm-5.3-flash[1m]")
        argv = worker["argv"]
        assert argv[argv.index("--model") + 1] == "glm-5.3[1m]"
        assert "--max-budget-usd" not in argv  # Claude's prices would be wrong: Looper keeps the cap
        assert not any(KEY in arg for arg in argv)
        # Without a background model in the catalog, the agent's model serves the background requests too.
        reviewer = await _launch("reviewer", task, tmp_path / "w", tmp_path)
        assert _env_of(reviewer, *MAPPED) == dict.fromkeys(MAPPED, "glm-4.7")
        # The key is in no record.
        assert KEY not in json.dumps([dict(r) for r in await pool.fetch("SELECT * FROM llm_records")], default=str)

    _run(_test, monkeypatch)


def test_anthropics_own_api_takes_the_key_as_an_api_key_and_keeps_claude_codes_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://operator-proxy.invalid")

    async def _test(_pool: Any) -> None:
        provider = await _provider("anthropic", base_url="")
        profile = (await _profile(provider, "sonnet"))["profile"]["profile_id"]
        task = await _task(tmp_path)
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": profile, "run_budget_usd": 0.5})

        seen = await _launch("worker", task, tmp_path / "w", tmp_path)
        assert _env_of(seen, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", *MAPPED) == {
            "ANTHROPIC_API_KEY": KEY, "ANTHROPIC_AUTH_TOKEN": None, "ANTHROPIC_BASE_URL": None,
            **dict.fromkeys(MAPPED)}
        assert seen["argv"][seen["argv"].index("--max-budget-usd") + 1] == "0.5"

    _run(_test, monkeypatch)


def test_the_glm_preset_starts_claude_code_as_the_vendors_guide_says(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test(_pool: Any) -> None:
        preset = next(p for p in (await _ok("GET", "/api/registry"))["provider_presets"]
                      if p["preset_id"] == "glm-bigmodel")
        api = next(a for a in preset["apis"] if a["protocols"] == ["anthropic-messages"])
        provider = (await _ok("POST", "/api/providers", {
            "name": preset["name"], "protocols": api["protocols"], "base_url": api["base_url"], "auth": "api_key",
            "api_key": KEY, "models": preset["models"], "env": preset["env"]}))["provider"]["provider_id"]
        profile = (await _profile(provider, "glm-5.3[1m]"))["profile"]["profile_id"]
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": profile})

        seen = await _launch("worker", await _task(tmp_path), tmp_path / "w", tmp_path)
        assert _env_of(seen, "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                       "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                       "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                       "API_TIMEOUT_MS") == {
            "ANTHROPIC_BASE_URL": "https://open.bigmodel.cn/api/anthropic", "ANTHROPIC_AUTH_TOKEN": KEY,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash[1m]", "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "API_TIMEOUT_MS": "3000000"}

    _run(_test, monkeypatch)


def test_every_preset_makes_providers_whose_profiles_are_priced(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(_pool: Any) -> None:
        registry = await _ok("GET", "/api/registry")
        assert {p["preset_id"] for p in registry["provider_presets"]} >= {"glm-bigmodel", "glm-zai", "deepseek",
                                                                          "kimi-moonshot-cn", "kimi-moonshot", "cpa"}
        for preset in registry["provider_presets"]:
            for n, api in enumerate(preset["apis"]):
                anthropic = "anthropic-messages" in api["protocols"]
                provider = (await _ok("POST", "/api/providers", {
                    "name": f"{preset['preset_id']}-{n}", "protocols": api["protocols"], "base_url": api["base_url"],
                    "auth": "api_key", "api_key": KEY, "models": preset["models"],
                    "env": preset["env"] if anthropic else []}))["provider"]
                if not anthropic:
                    continue
                for model in preset["models"]:
                    answer = await _profile(provider["provider_id"], model["name"])
                    assert answer["warnings"] == [], (preset["preset_id"], model["name"])
                    assert answer["profile"]["cost"] == {"source": "provider_price", "equivalent": False, "unpriced": []}

    _run(_test, monkeypatch)


def test_a_profile_whose_models_have_no_price_is_registered_with_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(_pool: Any) -> None:
        provider = await _provider(models=[{"name": "mystery-1", "background_model": "mystery-mini"}])
        answer = await _profile(provider, "mystery-1")
        assert answer["profile"]["cost"] == {"source": "provider_price", "equivalent": False,
                                             "unpriced": ["mystery-1", "mystery-mini"]}
        assert len(answer["warnings"]) == 2 and all("spend caps do not apply" in w for w in answer["warnings"])
        # Priced in the catalog: no warning.
        priced = [{"name": "mystery-1", "input_usd_per_mtok": 1, "output_usd_per_mtok": 2,
                   "cache_read_usd_per_mtok": 0.1, "cache_write_usd_per_mtok": 1}]
        await _ok("PUT", f"/api/providers/{provider}", {"models": priced})
        again = await _ok("PUT", f"/api/agent-profiles/{answer['profile']['profile_id']}", {"name": "mystery"})
        assert again["warnings"] == []
        # Claude Code's own figures: the subscription's is an equivalent, an Anthropic key's real spend.
        registry = await _ok("GET", "/api/registry")
        builtin = next(a for a in registry["profiles"] if a["builtin"])
        assert builtin["cost"] == {"source": "runtime", "equivalent": True, "unpriced": []}
        anthropic = await _profile(await _provider("anthropic", base_url=""), "mystery-1")
        assert (anthropic["profile"]["cost"]["source"], anthropic["warnings"]) == ("runtime", [])

    _run(_test, monkeypatch)


@pytest.mark.parametrize(("env", "error"), [
    ([{"key": "ANTHROPIC_BASE_URL", "value": "x"}], "set by the Claude Code binding"),
    ([{"key": "ANTHROPIC_DEFAULT_HAIKU_MODEL", "value": "x"}], "set by the Claude Code binding"),
    ([{"key": "MY_API_TOKEN", "value": "x"}], "looks like a credential"),
])
def test_a_providers_own_env_cannot_set_what_the_binding_owns_or_hold_a_secret(
    monkeypatch: pytest.MonkeyPatch, env: list[dict[str, str]], error: str,
) -> None:
    async def _test(_pool: Any) -> None:
        status, payload = await _call("POST", "/api/providers", {"name": "p", "protocols": ["anthropic-messages"],
                                                                 "auth": "api_key", "env": env})
        assert status == 400 and error in payload["error"], payload

    _run(_test, monkeypatch)


def test_an_api_key_provider_without_a_key_fails_the_run_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test(_pool: Any) -> None:
        profile = (await _profile(await _provider(api_key=None), "glm-4.7"))["profile"]["profile_id"]
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": profile})
        result = await _launch("worker", await _task(tmp_path), tmp_path / "w", tmp_path)
        assert "argv" not in result  # nothing started
        assert result["outcome"]["status"] == "failed" and "has no API key" in result["outcome"]["error"]

    _run(_test, monkeypatch)


# --- Cost and the soft cap ---

PRICED = [{"name": "m-main", "background_model": "glm-5.3-flash", "input_usd_per_mtok": 2.0,
           "output_usd_per_mtok": 10.0, "cache_read_usd_per_mtok": 0.2, "cache_write_usd_per_mtok": 2.5}]
REPORT = json.dumps({"status": "completed", "summary": "ok", "changed_files": {}})


def _usage(input_: int, output: int, read: int, write: int) -> dict[str, int]:
    return {"inputTokens": input_, "outputTokens": output, "cacheReadInputTokens": read,
            "cacheCreationInputTokens": write}


def _playing(events: list[dict[str, Any]], *, then_sleep: float = 0.0) -> str:
    """A stand-in claude that takes its first user turn, writes ``events`` and, after ``then_sleep`` seconds
    (killed before then, when it is stopped), a successful result."""
    return (
        "import json, sys, time\n"
        "sys.stdin.readline()\n"
        f"for event in {events!r}:\n"
        "    print(json.dumps(event), flush=True)\n"
        f"time.sleep({then_sleep})\n"
        f"print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': {REPORT!r}, "
        "'session_id': 's-1', 'total_cost_usd': 0}), flush=True)\n"
        "sys.stdin.read()\n"
    )


async def _work(claude: str, task_id: str, root: Path) -> dict[str, Any]:
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.tiers.worker import WorkerConfig, WorkerPool

    (root / "w").mkdir(parents=True, exist_ok=True)
    payload = {"task_id": "step-x", "parent_task_id": task_id, "instruction": "do it",
               "context": {"work_dir": str(root / "w")}}
    return await WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=root / "runtime")._execute(
        MessageEnvelope.create(source=planner_address(task_id), target=worker_address("worker-x"),
                               message_type=MessageType.WORK_ITEM, payload=payload))


def test_a_run_is_priced_from_each_models_tokens_cache_included(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test(pool: Any) -> None:
        provider = await _provider(models=PRICED)
        profile = (await _profile(provider, "m-main"))["profile"]["profile_id"]
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": profile})
        task = await _task(tmp_path)
        # Claude Code reports 9.99 at Claude's prices; the provider's prices say otherwise.
        result = {"type": "result", "subtype": "success", "is_error": False, "result": REPORT, "session_id": "s-1",
                  "total_cost_usd": 9.99, "modelUsage": {"m-main": _usage(1_000, 2_000, 30_000, 4_000),
                                                         "glm-5.3-flash": _usage(10_000, 1_000, 0, 0)}}
        body = ("import json, sys\nsys.stdin.readline()\n"
                f"print(json.dumps({result!r}), flush=True)\nsys.stdin.read()\n")
        await _work(fake_claude(tmp_path / "claude-priced", body), task, tmp_path)

        row = await pool.fetchrow("SELECT cost_usd, cost_source, input_tokens, output_tokens, cache_read_tokens, "
                                  "cache_creation_tokens FROM llm_records")
        # m-main: 1000 * 2 + 2000 * 10 + 30000 * 0.2 + 4000 * 2.5 = 38000 per million = $0.038.
        # glm-5.3-flash (built-in prices 0.15 input, 0.50 output): 10000 * 0.15 + 1000 * 0.5 = 2000 = $0.002.
        assert dict(row) == {"cost_usd": pytest.approx(0.040), "cost_source": "provider_price", "input_tokens": 11_000,
                             "output_tokens": 3_000, "cache_read_tokens": 30_000, "cache_creation_tokens": 4_000}
        cost = (await _ok("GET", f"/api/tasks/{task}"))["cost"]
        assert (cost["cost_usd"], cost["provider_price_runs"], cost["equivalent_runs"]) == \
            (pytest.approx(0.040), 1, 0)

        # A model without a price: tokens only.
        unknown = (await _profile(await _provider("other", models=[]), "mystery-1"))["profile"]["profile_id"]
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": unknown})
        await pool.execute("DELETE FROM llm_records")
        result["modelUsage"] = {"mystery-1": _usage(5, 6, 0, 0)}
        body = ("import json, sys\nsys.stdin.readline()\n"
                f"print(json.dumps({result!r}), flush=True)\nsys.stdin.read()\n")
        await _work(fake_claude(tmp_path / "claude-unpriced", body), task, tmp_path)
        row = await pool.fetchrow("SELECT cost_usd, cost_source, input_tokens, output_tokens FROM llm_records")
        assert dict(row) == {"cost_usd": None, "cost_source": "provider_price", "input_tokens": 5, "output_tokens": 6}

    _run(_test, monkeypatch)


def _request(model: str, input_: int, output: int) -> list[dict[str, Any]]:
    """One model request as Claude Code streams it with --include-partial-messages."""
    def event(kind: str, **fields: Any) -> dict[str, Any]:
        return {"type": "stream_event", "parent_tool_use_id": None, "event": {"type": kind, **fields}}

    return [event("message_start", message={"model": model, "usage": {"input_tokens": input_, "output_tokens": 0}}),
            event("message_delta", usage={"output_tokens": output}), event("message_stop")]


INIT = {"type": "system", "subtype": "init", "session_id": "s-cap", "model": "m-main"}


def test_a_worker_over_its_cap_at_the_providers_prices_is_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def _test(pool: Any) -> None:
        profile = (await _profile(await _provider(models=PRICED), "m-main"))["profile"]["profile_id"]
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": profile, "run_budget_usd": 0.5})
        task = await _task(tmp_path)
        # 100000 input at 2.0 = $0.20 (under $0.50); then 200000 input and 1000 output = $0.41: $0.61 in all.
        claude = fake_claude(tmp_path / "claude-cap", _playing(
            [INIT, *_request("m-main", 100_000, 0), *_request("m-main", 200_000, 1_000)], then_sleep=60))
        started = time.monotonic()
        outcome = await _work(claude, task, tmp_path)
        assert time.monotonic() - started < 30  # stopped, not left to finish
        assert outcome["status"] == "failed" and "Looper stopped the run at its per-run spend cap" in outcome["error"]
        row = await pool.fetchrow("SELECT status, cost_usd, cost_source, input_tokens FROM llm_records")
        assert dict(row) == {"status": "failed", "cost_usd": pytest.approx(0.61), "cost_source": "provider_price",
                             "input_tokens": 300_000}

    _run(_test, monkeypatch)


def test_a_planner_turn_over_its_cap_is_stopped_and_its_session_counts_the_spend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.tiers.planner import PlannerAgent, PlannerTurnError, TaskPlan
    from looper.db import agent_sessions

    async def _test(_pool: Any) -> None:
        profile = (await _profile(await _provider(models=PRICED), "m-main"))["profile"]["profile_id"]
        await _ok("PUT", "/api/agent-settings/roles/planner", {"profile_id": profile, "run_budget_usd": 0.5})
        task = await _task(tmp_path)
        (tmp_path / "w").mkdir(parents=True, exist_ok=True)
        claude = fake_claude(tmp_path / "claude-cap", _playing([INIT, *_request("m-main", 300_000, 0)], then_sleep=60))
        planner = PlannerAgent(task_id=task, runtime_root=tmp_path / "runtime", claude_bin=claude)
        plan = planner._plan = TaskPlan(plan_id="plan-x", goal_id="", requirement="r", work_dir=str(tmp_path / "w"))
        with pytest.raises(PlannerTurnError, match="per-run spend cap"):
            await planner._run_turn(planner._plan_message(plan))
        assert not planner._claude.started  # killed: the next turn starts a new process
        saved = await agent_sessions.get_session(planner.agent_id)
        # The session goes on; the next process that resumes it counts from what this turn spent ($0.60).
        assert saved["session_id"] == "s-cap" and saved["session_totals"]["cost_usd"] == pytest.approx(0.6)

    _run(_test, monkeypatch)


# --- The session rule: a resident session resumes only with the same runtime type and provider ---


@pytest.mark.parametrize(("before", "after", "resumes"), [
    ("subscription", "key-a", False),
    ("key-a", "subscription", False),
    ("key-a", "key-b", False),
    ("key-a", "key-a-other-model", True),
])
def test_a_planner_resumes_its_session_only_with_the_same_runtime_type_and_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, before: str, after: str, resumes: bool,
) -> None:
    from looper.agents.tiers.planner import PlannerAgent, TaskPlan
    from looper.db import agent_sessions

    async def _turn(task: str, n: int) -> dict[str, Any]:
        """One Planner turn after a restart: a new Planner that reads the saved session, as the server does."""
        import agent_launch_parity as parity

        out = tmp_path / f"turn-{n}.json"
        claude = fake_claude(tmp_path / f"claude-{n}", parity._CLAUDE.format(out=str(out)))
        planner = PlannerAgent(task_id=task, runtime_root=tmp_path / "runtime", claude_bin=claude)
        saved = await agent_sessions.get_session(planner.agent_id) or {}
        plan = planner._plan = TaskPlan(
            plan_id="plan-x", goal_id="", requirement="r", work_dir=str(tmp_path / "w"),
            session_id=saved.get("session_id"), session_runtime=saved.get("runtime_type"),
            session_profile=saved.get("profile_id"), session_provider=saved.get("provider_id"))
        try:
            await planner._run_turn(planner._plan_message(plan))
        finally:
            await planner._claude.stop()
        return json.loads(out.read_text())

    async def _test(pool: Any) -> None:
        catalog = [{"name": "glm-5.3[1m]"}, {"name": "glm-4.7"}]
        key_a, key_b = await _provider("key-a", models=catalog), await _provider("key-b", models=catalog)
        profiles = {"subscription": "agent-claude-code-opus",
                    "key-a": (await _profile(key_a, "glm-5.3[1m]"))["profile"]["profile_id"],
                    "key-a-other-model": (await _profile(key_a, "glm-4.7"))["profile"]["profile_id"],
                    "key-b": (await _profile(key_b, "glm-5.3[1m]"))["profile"]["profile_id"]}
        providers = {"subscription": "prov-claude-subscription", "key-a": key_a, "key-a-other-model": key_a,
                     "key-b": key_b}
        task = await _task(tmp_path)
        (tmp_path / "w").mkdir(parents=True, exist_ok=True)

        await _ok("PUT", "/api/agent-settings/roles/planner", {"profile_id": profiles[before]})
        assert "--resume" not in (await _turn(task, 1))["argv"]
        await _ok("PUT", "/api/agent-settings/roles/planner", {"profile_id": profiles[after]})
        second = await _turn(task, 2)

        assert ("--resume" in second["argv"]) is resumes
        switches = [r["summary"] for r in await pool.fetch(
            "SELECT summary FROM task_journal WHERE task_id = $1 AND kind = 'session_switch'", task)]
        if resumes:
            assert switches == []
        else:  # journalled as a session switch, naming both providers
            (switch,) = switches
            assert f"a session of provider {providers[before]} cannot go on with provider {providers[after]}" \
                in switch
        saved = await agent_sessions.get_session(f"planner-{task}")
        assert (saved["runtime_type"], saved["provider_id"]) == ("claude-code", providers[after])

    _run(_test, monkeypatch)
