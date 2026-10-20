"""Launch captures must come from real tiers and a patched v2 checkout.

Only the database tests need LOOPER_TEST_DATABASE_URL. The stand-in CLI never
calls a model; all authentication below is synthetic and in temporary roots.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import site
import subprocess
import sys
import uuid
import venv
from dataclasses import replace
from itertools import count
from pathlib import Path
from typing import Any

import pytest

import agent_launch_parity as tiers
import model_launch_parity as parity


FIXTURE = Path(__file__).parent / "fixtures/model_launch_parity_v2_claude.json"
CODEX_FIXTURE = FIXTURE.with_name("model_launch_parity_v2_codex.json")
OMP_FIXTURE = FIXTURE.with_name("model_launch_parity_v2_omp.json")


@pytest.mark.parametrize("case", ["responses-default", "chat-proxy", "messages-open",
                                  "priority-all", "priority-chat", "responses-open"])
def test_omp_capture_observes_all_four_real_tiers(
    case: str, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    from looper.agents import agent_settings, profile_registry as registry
    from looper.agents.runtimes import ApiProtocol, AuthKind, adapter
    from looper.db import profile_registry as db_registry
    from unittest.mock import AsyncMock

    protocols, url, catalog, effort, mode = parity.OMP_CASES[case]
    with parity.omp_capture_root() as root:
        executable = parity.omp_executable(root)
        provider_type = getattr(registry, "LegacyProviderSpec", registry.ProviderSpec)
        profile_type = getattr(registry, "LegacyAgentProfile", registry.AgentProfile)
        provider = provider_type(
            "prov-capture", "capture", AuthKind.API_KEY,
            frozenset(ApiProtocol(value) for value in protocols), base_url=url,
            catalog=tuple(registry.CatalogModel(**entry) for entry in catalog),
            api_key="sk-test-capture", env=(("CAPTURE_SETTING", "provider"),),
        )
        profile = profile_type("profile-capture", "capture",
                               registry.RuntimeSpec("rt-omp", "omp", "omp", executable,
                                                    adapter("omp").capabilities),
                               provider, "capture-omp[1m]", effort or None)
        original = agent_settings.resolve
        monkeypatch.setattr(agent_settings, "resolve",
                            lambda *a, **kw: replace(original(*a, **kw), profile=profile))
        monkeypatch.setattr(db_registry, "get_api_key", AsyncMock(return_value="sk-test-capture"))
        agent_db.list_layers.return_value = [
            {"scope": "system", "role": None, "env": [{"key": "CAPTURE_SETTING", "value": "settings"}],
             "run_budget_usd": 0.5},
            *({"scope": "role", "role": role, "system_prompt_mode": mode, "env": []}
              for role in ("manager", "planner", "worker", "reviewer")),
        ]
        projects = [{"id": pid, "name": name, "background": background, "description": "", "work_dir": ""}
                    for pid, (name, background) in tiers.PROJECTS.items()]
        agent_db.list_projects.return_value = projects
        agent_db.get_task_project.side_effect = lambda _: projects[0]
        save = agent_db.save_session.side_effect

        async def save_session(owner: str, *args: Any, **kwargs: Any) -> None:
            await save(owner, *args, **kwargs)
            agent_db.store.sessions[owner]["provider_id"] = kwargs.get("provider_id")

        agent_db.save_session.side_effect = save_session

        async def state(index: int) -> dict[str, Any]:
            owner = "manager" if index == 0 else f"planner-{tiers.TASKS[tiers.PROJECT_A]}"
            finished = agent_db.finish_record.call_args_list[index].kwargs
            return {"session": copy.deepcopy(agent_db.store.sessions[owner]) if index < 2 else
                    {"session_id": finished["session_id"]},
                    "totals": {key: finished.get(key) for key in parity.CODEX_TOTALS}}

        with monkeypatch.context() as env:
            env.setattr("os.environ", parity.omp_environment(root))
            asyncio.run(tiers.seed_rest("unconfigured", root))
            captures = asyncio.run(parity.capture_omp_tiers(root, read_state=state))
        assert len(captures) == agent_db.finish_record.call_count == 4
        expected_api = ("openai-responses" if "openai-responses" in protocols else
                        "openai-completions" if "openai-chat" in protocols else "anthropic-messages")
        for index, record in enumerate(captures):
            assert record.keys() == parity.FIELDS
            models = json.loads(record["config"]["models_yml"])["providers"]["looper"]
            settings = json.loads(record["config"]["overlay"])
            assert models["api"] == record["config"]["api"] == expected_api
            assert models["baseUrl"] == (url or ("https://api.anthropic.com" if
                                                   expected_api == "anthropic-messages" else
                                                   "https://api.openai.com/v1"))
            model = models["models"][0]
            assert model["id"] == model["name"] == record["config"]["model"] == "capture-omp[1m]"
            assert model.get("contextWindow") == (catalog[0]["context_window"] if catalog else None)
            assert model["reasoning"] == (not catalog or catalog[0]["efforts"] != [])
            assert ("compat" in model) == (expected_api == "openai-responses")
            assert settings["modelRoles"] == dict.fromkeys(
                ("default", "smol", "slow", "plan", "vision"), "looper/capture-omp[1m]")
            assert settings["retry"] == {"modelFallback": False}
            assert record["env"]["CAPTURE_SETTING"] == "provider"
            assert record["env"]["LOOPER_PROVIDER_API_KEY"] == "sk-test-capture"
            assert "PI_CONFIG_DIR" in record["unset"]
            assert record["prompt"] and record["first_input"]
            assert record["permissions"]["read_only"] == (index == 1)
            assert record["login_target"] is None
            assert record["totals"]["input_tokens"] == 60
            assert record["totals"]["output_tokens"] == 10
            assert record["totals"]["cache_read_tokens"] == 40
            assert record["price"]["source"] == "provider_price" and record["cap"] == 0.5
            if catalog:
                assert record["totals"]["cost_usd"] == pytest.approx(0.000062)
            else:
                assert record["totals"]["cost_usd"] is None
            assert ("--system-prompt" in record["argv"]) == (mode == "replace")
        parity.assert_parity(captures, copy.deepcopy(captures), expected_root=root, actual_root=root)
        for index in range(4):
            for key, value in (("api", "mutated-protocol"), ("baseUrl", "https://changed.example.test/")):
                mutated = copy.deepcopy(captures)
                models = json.loads(mutated[index]["config"]["models_yml"])
                old = json.dumps(key) + ": " + json.dumps(models["providers"]["looper"][key])
                new = json.dumps(key) + ": " + json.dumps(value)
                models["providers"]["looper"][key] = value
                text = mutated[index]["config"]["models_yml"]
                assert text.count(old) == 1
                mutated[index]["config"]["models_yml"] = text.replace(old, new)
                assert json.loads(mutated[index]["config"]["models_yml"]) == models
                with pytest.raises(AssertionError, match="models_yml"):
                    parity.assert_parity(captures, mutated, expected_root=root, actual_root=root)
        assert OMP_FIXTURE.is_file(), "generate the OMP baseline on the patched v2 checkout with a disposable DB"
        pinned = json.loads(OMP_FIXTURE.read_text())
        parity.assert_parity(pinned["captures"][case], captures, expected_root=root, actual_root=root)


def test_omp_baseline_requires_clean_patched_source(tmp_path: Path) -> None:
    with pytest.raises(parity.LegacyBaselineUnavailable, match="checkout"):
        parity.omp_legacy_source(tmp_path / "missing")
    output = tmp_path / "baseline.json"
    output.write_text("previous baseline\n")
    with pytest.raises(parity.LegacyBaselineUnavailable, match="revision"):
        parity.generate_omp_baseline(Path(__file__).resolve().parents[1], output)
    assert output.read_text() == "previous baseline\n"


def test_omp_fixture_records_patched_source() -> None:
    pinned = json.loads(OMP_FIXTURE.read_text())
    assert pinned["source"]["revision"] == parity.OMP_LEGACY_REVISION
    repository = Path(__file__).resolve().parents[1]
    for key, expression in {"tree": "{revision}^{{tree}}",
                            "adapter_blob": "{revision}:looper/agents/runtimes/omp.py",
                            "registry_blob": "{revision}:looper/agents/profile_registry.py"}.items():
        result = subprocess.run(["git", "-C", str(repository), "rev-parse",
                                 expression.format(revision=parity.OMP_LEGACY_REVISION)],
                                check=True, capture_output=True, text=True, timeout=10)
        assert pinned["source"][key] == result.stdout.strip()
    assert pinned["captures"].keys() == parity.OMP_CASES.keys()
    assert all(len(records) == 4 for records in pinned["captures"].values())


def test_omp_generation_preserves_output_without_database_or_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parity, "omp_legacy_source", lambda _: {})
    monkeypatch.delenv("LOOPER_TEST_DATABASE_URL", raising=False)
    output = tmp_path / "baseline.json"
    output.write_text("previous baseline\n")
    with pytest.raises(parity.LegacyBaselineUnavailable, match="database"):
        parity.generate_omp_baseline(tmp_path, output)
    monkeypatch.setenv("LOOPER_TEST_DATABASE_URL", "postgresql://synthetic.invalid/disposable")
    with pytest.raises(parity.LegacyBaselineUnavailable, match="venv"):
        parity.generate_omp_baseline(tmp_path, output)
    assert output.read_text() == "previous baseline\n"


@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"),
                    reason="legacy SQL capture needs a disposable LOOPER_TEST_DATABASE_URL")
def test_old_omp_checkout_reproduces_pinned_full_contract(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "legacy"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", str(repository), str(checkout)],
                   check=True, capture_output=True, timeout=15)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", parity.OMP_LEGACY_REVISION],
                   check=True, capture_output=True, timeout=15)
    venv.EnvBuilder(with_pip=False).create(checkout / ".venv")
    packages = checkout / ".venv/lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    (packages / "test-dependencies.pth").write_text("\n".join(site.getsitepackages()) + "\n")
    original = OMP_FIXTURE.read_bytes()
    output = tmp_path / "captured.json"
    parity.generate_omp_baseline(checkout, output)
    assert json.loads(output.read_text()) == json.loads(original)
    assert OMP_FIXTURE.read_bytes() == original


@pytest.mark.parametrize("case", ["key-default", "key-proxy", "login-home", "login-codex-home"])
def test_codex_capture_observes_real_tiers_and_resume(
    case: str, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    from looper.agents import agent_settings, profile_registry as registry
    from looper.agents.runtimes import ApiProtocol, AuthKind, adapter
    from looper.db import profile_registry as db_registry
    from unittest.mock import AsyncMock

    account, url, effort, mode, explicit_home = parity.CODEX_CASES[case]
    with parity.codex_capture_root() as root:
        executable = parity.codex_executable(root)
        provider_type = getattr(registry, "LegacyProviderSpec", registry.ProviderSpec)
        profile_type = getattr(registry, "LegacyAgentProfile", registry.AgentProfile)
        provider = provider_type(
            "prov-capture", "capture", AuthKind(account), frozenset({ApiProtocol.OPENAI_RESPONSES}),
            oauth_runtime_type="codex" if account == "oauth" else None, base_url=url,
            catalog=(registry.CatalogModel(**parity.CODEX_CATALOG),), api_key="sk-test-capture",
            env=(("CAPTURE_SETTING", "provider"),),
        )
        profile = profile_type("profile-capture", "capture",
                               registry.RuntimeSpec("rt-codex", "codex", "codex", executable,
                                                    adapter("codex").capabilities),
                               provider, "capture-codex", effort or None)
        original = agent_settings.resolve
        monkeypatch.setattr(agent_settings, "resolve",
                            lambda *a, **kw: replace(original(*a, **kw), profile=profile))
        monkeypatch.setattr(db_registry, "get_api_key", AsyncMock(return_value="sk-test-capture"))
        agent_db.list_layers.return_value = [
            {"scope": "system", "role": None, "env": [{"key": "CAPTURE_SETTING", "value": "settings"}],
             "run_budget_usd": 0.5},
            *({"scope": "role", "role": role, "system_prompt_mode": mode, "env": []}
              for role in ("manager", "planner", "worker", "reviewer")),
        ]
        projects = [{"id": pid, "name": name, "background": background, "description": "", "work_dir": ""}
                    for pid, (name, background) in tiers.PROJECTS.items()]
        agent_db.list_projects.return_value = projects
        agent_db.get_task_project.side_effect = lambda _: projects[0]
        save = agent_db.save_session.side_effect

        async def save_session(owner: str, *args: Any, **kwargs: Any) -> None:
            await save(owner, *args, **kwargs)
            agent_db.store.sessions[owner]["provider_id"] = kwargs.get("provider_id")

        agent_db.save_session.side_effect = save_session

        async def read_state(index: int) -> dict[str, Any]:
            owner = "manager" if index < 2 else f"planner-{tiers.TASKS[tiers.PROJECT_A]}"
            finished = agent_db.finish_record.call_args_list[index].kwargs
            return {"session": copy.deepcopy(agent_db.store.sessions[owner]) if index < 4 else
                    {"session_id": finished["session_id"]},
                    "totals": {key: finished.get(key) for key in parity.CODEX_TOTALS}}

        with monkeypatch.context() as env:
            env.setattr("os.environ", parity.codex_environment(root, explicit_home=explicit_home))
            asyncio.run(tiers.seed_rest("unconfigured", root))
            captures = asyncio.run(parity.capture_codex_tiers(root, read_state=read_state))
        assert len(captures) == 6
        assert agent_db.finish_record.call_count == 6
        for index, record in enumerate(captures):
            assert record.keys() == parity.FIELDS
            assert record["config"]["protocol"] == "openai-responses"
            assert record["config"]["model"] == "capture-codex"
            assert record["config"]["effort"] == effort
            assert record["config"]["catalog"][0]["context_window"] == 128000
            assert record["config"]["catalog"][0]["efforts"] == ["high", "low"]
            assert record["env"]["CAPTURE_SETTING"] == "provider"
            assert record["prompt"] and record["first_input"]
            assert record["totals"]["input_tokens"] == 60
            assert record["totals"]["output_tokens"] == 10
            assert record["totals"]["cache_read_tokens"] == 40
            assert record["totals"]["cost_usd"] == pytest.approx(0.000062)
            assert record["price"]["source"] == "provider_price" and record["cap"] == 0.5
            assert record["permissions"]["read_only"] == (index in (2, 3))
            assert ("resume" in record["argv"]) == (index in (1, 3))
            assert ("--ephemeral" in record["argv"]) == (index >= 4)
            if index < 4:
                assert record["session"]["runtime_type"] == "codex"
                assert record["session"]["session_totals"]["output_tokens"] == (20 if index % 2 else 10)
            if account == "oauth":
                assert record["login_target"] == str(root / ("login" if explicit_home else "home/.codex") / "auth.json")
                assert {"CODEX_API_KEY", "CODEX_ACCESS_TOKEN"} <= set(record["unset"])
                assert "model_provider" not in record["config"]["toml"]
            else:
                assert record["login_target"] is None
                assert record["env"]["LOOPER_PROVIDER_API_KEY"] == "sk-test-capture"
                assert f'base_url = "{url or "https://api.openai.com/v1"}"' in record["config"]["toml"]
            assert ("model_instructions_file" in record["config"]["toml"]) == (mode == "replace")
        # Prove the comparator sees real launch mutations before loading the
        # expected side, which can only be generated on the old SQL checkout.
        parity.assert_parity(captures, copy.deepcopy(captures), expected_root=root, actual_root=root)
        for field, key, value in (("config", "protocol", "openai-chat"),
                                  ("config", "toml", captures[0]["config"]["toml"] +
                                   '\nbase_url = "https://changed.example.test/"\n')):
            mutated = copy.deepcopy(captures)
            mutated[0][field][key] = value
            with pytest.raises(AssertionError, match=key):
                parity.assert_parity(captures, mutated, expected_root=root, actual_root=root)
        assert CODEX_FIXTURE.is_file(), "generate the Codex baseline on the patched v2 checkout with a disposable DB"
        expected = json.loads(CODEX_FIXTURE.read_text())["captures"][case]
        parity.assert_parity(expected, captures, expected_root=root, actual_root=root)


def test_codex_baseline_requires_patched_old_source(tmp_path: Path) -> None:
    with pytest.raises(parity.LegacyBaselineUnavailable, match="checkout"):
        parity.codex_legacy_source(tmp_path / "missing")
    output = tmp_path / "baseline.json"
    output.write_text("preserve previous baseline\n")
    with pytest.raises(parity.LegacyBaselineUnavailable, match="revision"):
        parity.generate_codex_baseline(Path(__file__).resolve().parents[1], output)
    assert output.read_text() == "preserve previous baseline\n"


def test_codex_generation_preserves_output_without_database_or_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parity, "codex_legacy_source", lambda _: {})
    monkeypatch.delenv("LOOPER_TEST_DATABASE_URL", raising=False)
    output = tmp_path / "baseline.json"
    output.write_text("previous baseline\n")
    with pytest.raises(parity.LegacyBaselineUnavailable, match="database"):
        parity.generate_codex_baseline(tmp_path, output)
    monkeypatch.setenv("LOOPER_TEST_DATABASE_URL", "postgresql://synthetic.invalid/disposable")
    with pytest.raises(parity.LegacyBaselineUnavailable, match="venv"):
        parity.generate_codex_baseline(tmp_path, output)
    assert output.read_text() == "previous baseline\n"


@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"),
                    reason="legacy SQL capture needs a disposable LOOPER_TEST_DATABASE_URL")
def test_old_codex_checkout_reproduces_pinned_full_contract(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "legacy"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", str(repository), str(checkout)],
                   check=True, capture_output=True, timeout=15)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", parity.CODEX_LEGACY_REVISION],
                   check=True, capture_output=True, timeout=15)
    venv.EnvBuilder(with_pip=False).create(checkout / ".venv")
    packages = checkout / ".venv/lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    (packages / "test-dependencies.pth").write_text("\n".join(site.getsitepackages()) + "\n")
    original = CODEX_FIXTURE.read_bytes()
    output = tmp_path / "captured.json"
    parity.generate_codex_baseline(checkout, output)
    assert json.loads(output.read_text()) == json.loads(original)
    assert CODEX_FIXTURE.read_bytes() == original


def test_codex_fixture_source_is_the_patched_adapter() -> None:
    assert CODEX_FIXTURE.is_file(), "generate the Codex baseline on the patched v2 checkout with a disposable DB"
    pinned = json.loads(CODEX_FIXTURE.read_text())
    assert pinned["source"]["revision"] == parity.CODEX_LEGACY_REVISION
    repository = Path(__file__).resolve().parents[1]
    for key, expression in {
        "tree": "{revision}^{{tree}}", "adapter_blob": "{revision}:looper/agents/runtimes/codex.py",
        "registry_blob": "{revision}:looper/agents/profile_registry.py",
    }.items():
        result = subprocess.run(["git", "-C", str(repository), "rev-parse",
                                 expression.format(revision=parity.CODEX_LEGACY_REVISION)],
                                check=True, capture_output=True, text=True, timeout=10)
        assert pinned["source"][key] == result.stdout.strip()
    assert pinned["captures"].keys() == parity.CODEX_CASES.keys()
    assert all(len(records) == 6 for records in pinned["captures"].values())


@pytest.fixture
def baseline() -> dict[str, Any]:
    # Reading the pinned contract never needs a database or runs a resolver.
    assert FIXTURE.is_file(), "generate the baseline on the pinned patched v2 checkout"
    return json.loads(FIXTURE.read_text("utf-8"))


@pytest.mark.parametrize("account,url,background,teams,mode,scope",
                         parity.CLAUDE_CASES.values(), ids=parity.CLAUDE_CASES)
def test_claude_capture_observes_all_four_real_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any, baseline: dict[str, Any],
    account: str, url: str, background: str | None, teams: bool, mode: str | None, scope: str,
) -> None:
    from looper.agents import agent_settings, profile_registry as registry
    from looper.agents.runtimes import ApiProtocol, AuthKind
    from looper.db import agent_setting_layers

    assert Path(registry.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
    provider_type = getattr(registry, "LegacyProviderSpec", registry.ProviderSpec)
    profile_type = getattr(registry, "LegacyAgentProfile", registry.AgentProfile)
    catalog = registry.CatalogModel("main-model[1m]", context_window=1_000_000,
                                    input_usd_per_mtok=0.6, output_usd_per_mtok=2.2,
                                    cache_read_usd_per_mtok=0, cache_write_usd_per_mtok=1,
                                    efforts=("high", "low"), background_model=background)
    provider = provider_type("prov-capture", "capture", AuthKind(account),
                             frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                             oauth_runtime_type="claude-code" if account == "oauth" else None,
                             base_url=url, catalog=(catalog,), api_key="sk-test-capture",
                             env=(("API_TIMEOUT_MS", "3000000"),))
    profile = profile_type("profile-capture", "capture", registry.BUILTIN_RUNTIME, provider,
                           "main-model[1m]", "high", agent_teams=teams)
    monkeypatch.setattr(registry, "BUILTIN_PROFILE", profile)
    # Persistence is the existing in-memory positive control. Settings still
    # resolve by the real loader; only its secret read uses a synthetic key.
    from looper.db import profile_registry as db_registry
    from unittest.mock import AsyncMock

    monkeypatch.setattr(db_registry, "get_api_key", AsyncMock(return_value="sk-test-capture"))
    original_resolve = agent_settings.resolve

    def resolve(*args: Any, **kwargs: Any) -> Any:
        return replace(original_resolve(*args, **kwargs), profile=profile)

    monkeypatch.setattr(agent_settings, "resolve", resolve)
    rows = [{"scope": "system", "role": None, "env": [], "run_budget_usd": 0.5}]
    rows += [] if mode is None else [
        {"scope": "role" if role == "manager" else scope, "role": role, "system_prompt_mode": mode, "env": []}
        for role in ("manager", "planner", "worker", "reviewer")
    ]
    if scope == "task":
        rows += [{"scope": "role", "role": role, "system_prompt_mode": "append", "env": []}
                 for role in ("planner", "worker", "reviewer")]
    agent_db.list_layers.return_value = rows
    assert agent_setting_layers.list_layers is agent_db.list_layers
    projects = [{"id": project_id, "name": name, "background": background_text,
                 "description": "", "work_dir": ""}
                for project_id, (name, background_text) in tiers.PROJECTS.items()]
    agent_db.list_projects.return_value = projects
    agent_db.get_task_project.side_effect = lambda task_id: next(
        (project for project in projects if tiers.TASKS[project["id"]] == task_id), None)
    asyncio.run(tiers.seed_rest("unconfigured", tmp_path))
    save_session = agent_db.save_session.side_effect

    async def persist_session(agent_id: str, *args: Any, **kwargs: Any) -> None:
        await save_session(agent_id, *args, **kwargs)
        # The common memory store predates provider resume identities. Retain
        # the actual write's value here, as the legacy SQL store does.
        agent_db.store.sessions[agent_id]["provider_id"] = kwargs.get("provider_id")

    agent_db.save_session.side_effect = persist_session
    # Feed real pricing/readback a deterministic, nonzero usage report.
    monkeypatch.setattr(tiers, "_CLAUDE", tiers._CLAUDE.replace(
        "'total_cost_usd': 0,",
        "'total_cost_usd': 0.25, 'modelUsage': {{'main-model[1m]': "
        "{{'inputTokens': 100, 'outputTokens': 40, 'cacheReadInputTokens': 5, 'cacheCreationInputTokens': 2}}}},",
    ))
    monkeypatch.setattr("os.environ", {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "login"),
        "ANTHROPIC_API_KEY": "sk-test-inherited-key",
        "ANTHROPIC_AUTH_TOKEN": "sk-test-inherited-token",
        "ANTHROPIC_BASE_URL": "https://inherited.example.test/",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "inherited-side",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    })
    login = tmp_path / "login" / ".credentials.json"
    login.parent.mkdir()
    login.write_text('{"synthetic":true}\n')
    ids = count(1)
    monkeypatch.setattr("looper.agents.claude_cli.uuid4", lambda: uuid.UUID(int=next(ids)))

    def state(index: int) -> dict[str, Any]:
        owner = "manager" if index == 0 else f"planner-{tiers.TASKS[tiers.PROJECT_A]}"
        saved = agent_db.store.sessions.get(owner) if index < 2 else None
        finished = agent_db.finish_record.call_args_list[index].kwargs
        return {"session": saved if index < 2 else {"session_id": finished["session_id"]},
                "totals": {name: finished.get(name) for name in
                           ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "cost_usd")}}

    captures = asyncio.run(parity.capture_claude_tiers(tmp_path, "unconfigured", read_state=state))
    case = next(name for name, values in parity.CLAUDE_CASES.items()
                if values == (account, url, background, teams, mode, scope))
    expected = baseline["captures"][case]
    assert len(captures) == 4
    for index, record in enumerate(captures):
        # The in-memory store is a fast adapter-path control, not a substitute
        # for the SQL source capture. Compare its path-independent observations
        # to that source, rather than comparing a capture only with itself.
        for field in ("env", "unset", "permissions", "price", "cap", "session", "totals", "mcp"):
            assert record[field] == expected[index][field], (case, index, field)
        for field in ("protocol", "model", "effort", "catalog"):
            assert record["config"][field] == expected[index]["config"][field], (case, index, field)
        for field in ("prompt", "first_input", "login_target"):
            assert record[field].replace(str(tmp_path), "<root>") == expected[index][field], (case, index, field)
        assert record.keys() == parity.FIELDS
        assert record["config"]["protocol"] == "anthropic-messages"
        assert record["config"]["catalog"][0]["context_window"] == 1_000_000
        assert record["config"]["catalog"][0]["efforts"] == ["high", "low"]
        assert record["prompt"] and json.loads(record["first_input"])["message"]["content"]
        assert record["session"] and record["totals"]["cost_usd"] is not None
        assert record["totals"]["input_tokens"] == 100
        assert record["totals"]["output_tokens"] == 40
        assert record["cap"] == 0.5
        runtime_price = account == "oauth" or not url or "api.anthropic.com" in url
        assert record["price"]["source"] == ("runtime" if runtime_price else "provider_price")
        assert ("--max-budget-usd" in record["argv"]) == runtime_price
        assert record["login_target"] == str(login)
        expected_mode = mode or ("replace" if index < 2 else "append")
        assert ("--system-prompt-file" in record["argv"]) == (expected_mode == "replace")
        assert ("--teammate-mode" in record["argv"]) == (teams and index != 1)
        assert record["isolation"]["crossSessionInbound"] == "refuse"
        assert record["mcp"] is None
        if account == "api_key":
            if not url or "api.anthropic.com" in url:
                assert record["env"]["ANTHROPIC_API_KEY"] == "sk-test-capture"
                assert "ANTHROPIC_AUTH_TOKEN" in record["unset"]
                if not url:
                    assert "ANTHROPIC_BASE_URL" in record["unset"]
            else:
                assert record["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-test-capture"
                assert record["env"]["ANTHROPIC_BASE_URL"] == url
                for name in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                             "CLAUDE_CODE_SUBAGENT_MODEL"):
                    assert record["env"][name] == "main-model[1m]"
                for name in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"):
                    assert record["env"][name] == (background or "main-model[1m]")
                assert "ANTHROPIC_API_KEY" in record["unset"]
    # Positive control first; every negative starts from an actual capture.
    parity.assert_parity(captures, copy.deepcopy(captures), expected_root=tmp_path, actual_root=tmp_path)
    for field, key, value in (("config", "protocol", "openai-responses"),
                              ("env", "ANTHROPIC_BASE_URL", "https://changed.example.test/")):
        mutated = copy.deepcopy(captures)
        mutated[0][field][key] = value
        with pytest.raises(AssertionError, match=key):
            parity.assert_parity(captures, mutated, expected_root=tmp_path, actual_root=tmp_path)


def test_missing_old_checkout_blocks_instead_of_fabricating_expected(tmp_path: Path) -> None:
    with pytest.raises(parity.LegacyBaselineUnavailable, match="checkout"):
        parity.claude_legacy_source(tmp_path / "missing")


def test_new_resolver_cannot_be_the_expected_source() -> None:
    with pytest.raises(parity.LegacyBaselineUnavailable, match="revision"):
        parity.claude_legacy_source(Path(__file__).resolve().parents[1])


def test_baseline_generation_rejects_current_code_before_writing(tmp_path: Path) -> None:
    output = tmp_path / "baseline.json"
    with pytest.raises(parity.LegacyBaselineUnavailable, match="revision"):
        parity.generate_claude_baseline(Path(__file__).resolve().parents[1], output)
    assert not output.exists()


def test_baseline_generation_requires_the_old_checkouts_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parity, "claude_legacy_source", lambda _: {})
    monkeypatch.setenv("LOOPER_TEST_DATABASE_URL", "postgresql://synthetic.invalid/disposable")
    output = tmp_path / "baseline.json"
    output.write_text("previous baseline\n")
    with pytest.raises(parity.LegacyBaselineUnavailable, match="venv"):
        parity.generate_claude_baseline(tmp_path, output)
    assert output.read_text() == "previous baseline\n"


def test_only_clean_patched_v2_source_passes_the_generation_gate(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "legacy"

    def git(*args: str) -> None:
        subprocess.run(["git", *args], check=True, capture_output=True, timeout=15)

    git("clone", "--shared", "--no-checkout", str(repository), str(checkout))
    git("-C", str(checkout), "checkout", "--detach", parity.CLAUDE_LEGACY_REVISION)
    source = parity.claude_legacy_source(checkout)
    assert source["revision"] == parity.CLAUDE_LEGACY_REVISION
    assert all(len(value) == 40 for value in source.values())
    codex_source = parity.codex_legacy_source(checkout)
    assert codex_source["revision"] == parity.CODEX_LEGACY_REVISION
    assert codex_source["tree"] == source["tree"]
    assert codex_source["adapter_blob"] != source["adapter_blob"]
    omp_source = parity.omp_legacy_source(checkout)
    assert omp_source["revision"] == parity.OMP_LEGACY_REVISION
    assert omp_source["tree"] == source["tree"]
    assert omp_source["adapter_blob"] not in (source["adapter_blob"], codex_source["adapter_blob"])
    # A verified checkout alone cannot manufacture persisted session/totals.
    # Failure must preserve any existing output rather than partially refresh it.
    output = tmp_path / "baseline.json"
    output.write_text("previous baseline\n")
    with pytest.MonkeyPatch.context() as env:
        env.delenv("LOOPER_TEST_DATABASE_URL", raising=False)
        with pytest.raises(parity.LegacyBaselineUnavailable, match="database"):
            parity.generate_claude_baseline(checkout, output)
        # Exercise the isolated interpreter as well: the old imports must pass
        # before the missing-database gate, even with this checkout installed.
        child = subprocess.run(
            [sys.executable, "-I", parity.__file__, "--checkout", str(checkout), "--capture"],
            cwd=checkout, capture_output=True, text=True, timeout=15,
        )
        assert child.returncode != 0
        assert "requires a disposable test database" in child.stderr
        assert not child.stdout
        omp_child = subprocess.run(
            [sys.executable, "-I", parity.__file__, "--checkout", str(checkout), "--runtime", "omp", "--capture"],
            cwd=checkout, capture_output=True, text=True, timeout=15,
        )
        assert omp_child.returncode != 0
        assert "OMP baseline requires a disposable test database" in omp_child.stderr
        assert not omp_child.stdout
    assert output.read_text() == "previous baseline\n"
    adapter = checkout / "looper/agents/runtimes/claude_code.py"
    adapter.write_text(adapter.read_text() + "\n# synthetic adapter mutation\n")
    with pytest.raises(parity.LegacyBaselineUnavailable, match="revision"):
        parity.claude_legacy_source(checkout)
    git("-C", str(checkout), "restore", "looper/agents/runtimes/claude_code.py")
    codex = checkout / "looper/agents/runtimes/codex.py"
    codex.write_text(codex.read_text() + "\n# synthetic Codex adapter mutation\n")
    with pytest.raises(parity.LegacyBaselineUnavailable, match="revision"):
        parity.codex_legacy_source(checkout)
    git("-C", str(checkout), "restore", "looper/agents/runtimes/codex.py")
    omp = checkout / "looper/agents/runtimes/omp.py"
    omp.write_text(omp.read_text() + "\n# synthetic OMP adapter mutation\n")
    with pytest.raises(parity.LegacyBaselineUnavailable, match="revision"):
        parity.omp_legacy_source(checkout)
    git("-C", str(checkout), "restore", "looper/agents/runtimes/omp.py")
    git("-C", str(checkout), "checkout", "--detach", "95c399431")
    with pytest.raises(parity.LegacyBaselineUnavailable, match="revision"):
        parity.claude_legacy_source(checkout)
    with pytest.raises(parity.LegacyBaselineUnavailable, match="revision"):
        parity.codex_legacy_source(checkout)
    with pytest.raises(parity.LegacyBaselineUnavailable, match="revision"):
        parity.omp_legacy_source(checkout)


@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"),
                    reason="legacy SQL capture needs a disposable LOOPER_TEST_DATABASE_URL")
def test_old_checkout_captures_sql_sessions_without_refreshing_the_fixture(
    tmp_path: Path, baseline: dict[str, Any],
) -> None:
    repository = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "legacy"
    subprocess.run(["git", "clone", "--shared", "--no-checkout", str(repository), str(checkout)],
                   check=True, capture_output=True, timeout=15)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", parity.CLAUDE_LEGACY_REVISION],
                   check=True, capture_output=True, timeout=15)
    # Reuse only the test interpreter's installed dependencies, offline. The
    # isolated child verifies that Looper and the driver import from v2.
    venv.EnvBuilder(with_pip=False).create(checkout / ".venv")
    packages = checkout / ".venv/lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    (packages / "test-dependencies.pth").write_text("\n".join(site.getsitepackages()) + "\n")
    original = FIXTURE.read_bytes()
    output = tmp_path / "captured.json"
    parity.generate_claude_baseline(checkout, output)
    captured = json.loads(output.read_text("utf-8"))
    assert captured["source"] == baseline["source"]
    assert captured["captures"].keys() == parity.CLAUDE_CASES.keys()
    for case, records in captured["captures"].items():
        assert len(records) == 4
        for before, after in zip(baseline["captures"][case], records):
            assert after.keys() == parity.FIELDS
            # The full argv/settings, including root-dependent prompt hashes
            # and ancestor exclusions, remain in the capture. They are never
            # discarded or rewritten to make the pinned file pass.
            for field in parity.FIELDS - {"argv", "config", "isolation"}:
                assert after[field] == before[field], (case, field)
            for field in ("protocol", "model", "effort", "catalog"):
                assert after["config"][field] == before["config"][field]
            assert json.loads(after["config"]["settings"]) == after["isolation"]
    assert FIXTURE.read_bytes() == original


def test_patched_claude_fixture_is_pinned(baseline: dict[str, Any]) -> None:
    assert baseline["source"]["revision"] == parity.CLAUDE_LEGACY_REVISION
    repository = Path(__file__).resolve().parents[1]
    objects = {"tree": "HEAD^{tree}", "adapter_blob": "HEAD:looper/agents/runtimes/claude_code.py",
               "registry_blob": "HEAD:looper/agents/profile_registry.py"}
    for field, expression in objects.items():
        expression = expression.replace("HEAD", parity.CLAUDE_LEGACY_REVISION)
        source = subprocess.run(["git", "-C", str(repository), "rev-parse", expression],
                                check=True, capture_output=True, text=True, timeout=10)
        assert baseline["source"][field] == source.stdout.strip()
    assert baseline["captures"].keys() == parity.CLAUDE_CASES.keys()
    for case, records in baseline["captures"].items():
        assert len(records) == 4
        assert all(record.keys() == parity.FIELDS for record in records)
        account, url, background, teams, mode, _ = parity.CLAUDE_CASES[case]
        for index, record in enumerate(records):
            parity.normalize(record, root=Path("/synthetic-unused-root"))
            assert record["argv"][0].startswith("<root>/bin/claude-")
            assert record["config"]["protocol"] == "anthropic-messages"
            assert record["config"]["model"] == "main-model[1m]"
            assert record["config"]["catalog"][0]["background_model"] == background
            assert record["config"]["catalog"][0]["context_window"] == 1_000_000
            assert record["config"]["catalog"][0]["efforts"] == ["high", "low"]
            assert record["config"]["effort"] == "high"
            assert json.loads(record["config"]["settings"]) == record["isolation"]
            assert record["isolation"]["crossSessionInbound"] == "refuse"
            assert record["isolation"]["autoMemoryEnabled"] is False
            assert record["prompt"] and record["first_input"]
            assert record["login_target"] == "<root>/login/.credentials.json"
            assert ("--teammate-mode" in record["argv"]) == (teams and index != 1)
            selected_mode = mode or ("replace" if index < 2 else "append")
            assert ("--system-prompt-file" in record["argv"]) == (selected_mode == "replace")
            assert record["session"]["session_id"] == "sess-parity"
            assert record["totals"]["input_tokens"] == 100
            assert record["totals"]["output_tokens"] == 40
            assert record["cap"] == 0.5
            if account == "oauth":
                assert not any(name.startswith("ANTHROPIC_") for name in record["env"])
            elif not url:
                assert "ANTHROPIC_BASE_URL" in record["unset"]
                assert "ANTHROPIC_BASE_URL" not in record["env"]


@pytest.mark.parametrize("case", parity.CLAUDE_CASES)
@pytest.mark.parametrize("field,key,value", [
    ("config", "protocol", "openai-responses"),
    ("env", "ANTHROPIC_BASE_URL", "https://changed.example.test/messages/"),
])
def test_pinned_launch_detects_protocol_and_url_mutations(
    baseline: dict[str, Any], case: str, field: str, key: str, value: str,
) -> None:
    expected = baseline["captures"][case]
    actual = copy.deepcopy(expected)
    root = Path("/synthetic-unused-root")
    parity.assert_parity(expected, actual, expected_root=root, actual_root=root)
    for index in range(4):
        actual = copy.deepcopy(expected)
        actual[index][field][key] = value
        with pytest.raises(AssertionError, match=key):
            parity.assert_parity(expected, actual, expected_root=root, actual_root=root)
