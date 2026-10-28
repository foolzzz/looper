"""Current registry declarations launch real tiers and persist their observations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import model_launch_parity as parity
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires a disposable LOOPER_TEST_DATABASE_URL")
CASES = {"claude": parity.CLAUDE_CASES, "codex": parity.CODEX_CASES,
         "omp": parity.OMP_CASES, "grok": parity.GROK_CASES}


@pytest.mark.parametrize("runtime,case", [(runtime, case) for runtime, cases in CASES.items() for case in cases])
def test_current_four_tiers_persist_launch_sessions_accounting_and_prompt_layers(
    runtime: str, case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def check(pool):
        records = await parity.capture_tiers(pool, runtime, case, tmp_path)
        count = 6 if runtime in {"codex", "grok"} else 4
        rows = await pool.fetch("SELECT agent_id, status, metadata FROM llm_records ORDER BY id")
        assert [row["agent_id"].partition("-")[0] for row in rows] == (["manager", "manager", "planner", "planner", "worker", "reviewer"]
                                               if count == 6 else ["manager", "planner", "worker", "reviewer"])
        assert all(row["status"] == "completed" and json.loads(row["metadata"])["prompt_layers"] for row in rows)
        assert all(json.loads(row["metadata"])["llm_model"]["provider_id"] == "prov-capture" and
                   json.loads(row["metadata"])["llm_model"]["model_id"] == "model-capture" for row in rows)
        assert len(records) == (10 if runtime == "grok" and case.startswith("fallback") else count)
        model = {"claude": "main-model[1m]", "codex": "capture-codex", "omp": "capture-omp[1m]", "grok": "grok-4.7"}[runtime]
        for record in records:
            assert record["config"]["model"] == model
            assert record["session"]["session_id"]
            assert record["prompt_layers"]
            assert tuple(record["totals"][key] for key in parity.CODEX_TOTALS[:4]) == {
                "claude": (100, 40, 5, 2), "codex": (60, 10, 40, 0),
                "omp": (60, 10, 40, 0), "grok": (60, 5, 40, 0),
            }[runtime]
            assert record["price"]["source"] == ("runtime" if runtime == "grok" or
                runtime == "claude" and not case.startswith("proxy-") else "provider_price")
            assert record["cap"] == (0.005 if runtime == "grok" else 0.5)
        assert not await pool.fetchval("SELECT count(*) FROM llm_model_migration_map")

    run_db(check, monkeypatch, timeout=120, max_size=4)


@pytest.mark.parametrize("index", [0, 2], ids=["manager", "planner"])
def test_profile_rename_preserves_the_resident_session(index: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def check(pool):
        records = await parity.capture_tiers(pool, "codex", "key-proxy", tmp_path, rename_profile_after=index)
        before, after = records[index:index + 2]
        assert before["session"]["profile_id"] == "profile-capture"
        assert after["session"]["profile_id"] == "profile-capture"
        names = await pool.fetch("SELECT metadata->'profile'->>'name' AS name FROM llm_records ORDER BY id")
        assert names[index]["name"] == "capture" and names[index + 1]["name"] == "renamed"
        assert after["session"]["session_id"] == before["session"]["session_id"]
        assert "resume" in after["argv"]
        assert after["session"]["session_totals"]["input_tokens"] == 2 * before["totals"]["input_tokens"]

    run_db(check, monkeypatch, timeout=120, max_size=4)


def test_current_providers_keep_separate_model_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.agent_settings import load_agent_settings
    from looper.agents.profile_registry import AgentProfile, Endpoint, ModelSpec, ProviderSpec, RuntimeSpec
    from looper.agents.runtimes import ApiProtocol, AuthKind, VendorType, adapter

    async def check(pool):
        runtime = RuntimeSpec("rt-owned", "owned", "codex", "codex", adapter("codex").capabilities)
        for suffix in ("a", "b"):
            provider = ProviderSpec(f"provider-{suffix}", f"capture-{suffix}", VendorType.CUSTOM, AuthKind.API_KEY,
                                    api_key="sk-test-shared")
            endpoint = Endpoint(provider.id, ApiProtocol.OPENAI_RESPONSES, "https://shared.example.test/v1/")
            model = ModelSpec(f"model-{suffix}", provider.id, "same-model", endpoint.protocol)
            profile = AgentProfile(f"profile-{suffix}", f"profile-{suffix}", runtime, model.id)
            await parity.seed_current(pool, runtime, provider, endpoint, model, profile)
            await pool.execute("INSERT INTO agent_setting_layers (scope, role, profile_id) VALUES ('role',$1,$2)",
                               "worker" if suffix == "a" else "reviewer", profile.id)
        one, two = await load_agent_settings("worker"), await load_agent_settings("reviewer")
        assert one.profile.provider.id == "provider-a" and two.profile.provider.id == "provider-b"
        assert one.profile.model_id == "model-a" and two.profile.model_id == "model-b"
        assert one.profile.model == two.profile.model == "same-model"
        assert not await pool.fetchval("SELECT count(*) FROM llm_model_migration_map")

    run_db(check, monkeypatch, timeout=30)
