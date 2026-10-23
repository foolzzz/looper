"""Release gate: converted target rows must reproduce the pinned v2 launches.

Only the SQL exercises require a disposable LOOPER_TEST_DATABASE_URL. The
negative controls always run, including when PostgreSQL is unavailable.
Upgrades are supported from v2.12 (older versions upgrade to v2.12 first): the
fixtures are what v2.12 started every role with, and the converted v2.13 rows
must start them the same way. The system prompt's text is not compared (the
fixtures hold ``<prompt>``): its wording is the prompt tests' business, not a
migration's. The older upgrade suites' launch snapshots (v2.5, v2.6) are gone;
their rows are proved by the row-level migration tests.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

import model_launch_parity as parity


FIXTURES = Path(__file__).parent / "fixtures"
CASES = {"claude": parity.CLAUDE_CASES, "codex": parity.CODEX_CASES,
         "omp": parity.OMP_CASES, "grok": parity.GROK_CASES}
DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")


def pinned(runtime: str) -> dict[str, Any]:
    baseline = json.loads((FIXTURES / f"model_launch_parity_v2_{runtime}.json").read_text())
    assert baseline["source"]["revision"] == parity.CLAUDE_LEGACY_REVISION
    assert baseline["captures"].keys() == CASES[runtime].keys()
    for case, records in baseline["captures"].items():
        # Declared record-shape delta: a saved agent_sessions row now reads back selected_profile_id (the Manager
        # session's own /profile choice), null until chosen. Only saved-session rows (not the bare {"session_id"}
        # of a resumed turn) gain it.
        for record in records:
            if runtime == "claude":
                # Apply the declared fix only to current expectations, without refreshing historical fixtures.
                record["argv"] = parity.with_planner_file_reads(record["argv"])
                if "--permission-mode" in record["argv"]:
                    # The pinned Planner runs in this repository-less task directory. Its former bare read
                    # grants now apply only there; keep the actual capture and comparison unmodified.
                    at = record["argv"].index("--allowedTools") + 1
                    assert record["argv"][at].startswith("Read,Glob,Grep,")
                    reads = ",".join(f"{tool}(/<root>/work/proj-0000000000a1/**)"
                                     for tool in ("Read", "Glob", "Grep"))
                    record["argv"][at] = reads + record["argv"][at][len("Read,Glob,Grep"):]
                for flag in ("--disallowedTools", "--allowedTools"):
                    record["permissions"][flag] = record["argv"][record["argv"].index(flag) + 1] \
                        if flag in record["argv"] else None
            if isinstance(record["session"], dict) and "session_totals" in record["session"]:
                assert "selected_profile_id" not in record["session"], case
                record["session"]["selected_profile_id"] = None
    return baseline["captures"]


# The comparison is one JSON diff of the normalized records, the same for every field and runtime: a change to any
# one fact must fail it, and these are the controls that it can (one change per runtime, on that runtime's own record).
@pytest.mark.parametrize("runtime,change", [("claude", "url"), ("codex", "config"), ("omp", "protocol"),
                                            ("grok", "totals")])
def test_pinned_comparison_rejects_behavior_changes(runtime: str, change: str, tmp_path: Path) -> None:
    # Start with an independent old capture and prove the positive control
    # before changing one fact. Never derive expected from the new resolver.
    case = {"claude": "proxy-key-background-append", "codex": "key-proxy",
            "omp": "chat-proxy", "grok": "acp-known"}[runtime]
    expected = pinned(runtime)[case]
    actual = copy.deepcopy(expected)
    parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)
    row = actual[0]
    if change == "url":
        assert row["env"]["ANTHROPIC_BASE_URL"]
        row["env"]["ANTHROPIC_BASE_URL"] = "https://changed.example.test/v1/"
    elif change == "config":
        assert "https://proxy.example.test/" in row["config"]["toml"]
        row["config"]["toml"] = row["config"]["toml"].replace(
            "https://proxy.example.test/", "https://changed.example.test/")
    elif change == "protocol":
        row["config"]["api"] = "anthropic-messages"
    else:
        row["totals"]["input_tokens"] += 1
    with pytest.raises(AssertionError, match="launch 0 differs"):
        parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)


@pytest.mark.parametrize("change", ["text", "flag", "directory", "source", "key", "model", "model-path"])
def test_the_prompt_text_is_not_compared_but_its_flag_and_directory_are(change: str, tmp_path: Path) -> None:
    expected = pinned("claude")["empty-url-key-replace"]
    actual = copy.deepcopy(expected)
    assert expected[0]["prompt"] == "<prompt>"
    at = actual[0]["argv"].index("--system-prompt-file")
    if change == "text":
        # Another wording is another file name and text: neither is a launch difference.
        actual[0]["prompt"] = "Another wording of the Manager's instructions.\n"
        actual[0]["argv"][at + 1] = f"{tmp_path}/runtime/.local/prompts/{'0' * 64}.md"
        parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)
        return
    if change == "flag":
        actual[0]["argv"][at] = "--append-system-prompt-file"
    elif change == "directory":
        actual[0]["argv"][at + 1] = actual[0]["argv"][at + 1].replace("/prompts/", "/elsewhere/")
    elif change == "source":
        actual[0]["prompt_layers"]["harness"]["source"] = "project"
    elif change == "model-path":
        at = actual[0]["argv"].index("--model") + 1
        actual[0]["argv"][at] = f"<root>/runtime/.local/prompts/{'0' * 64}.md"
        expected[0]["argv"][at] = f"<root>/runtime/.local/prompts/{'1' * 64}.md"
    else:
        # Arbitrary 64-hex credentials/model names are inputs, never prompt addresses.
        field = actual[0]["env"] if change == "key" else actual[0]["config"]
        key = "ANTHROPIC_API_KEY" if change == "key" else "model"
        field[key] = "0" * 64
        expected[0]["env" if change == "key" else "config"][key] = "1" * 64
    with pytest.raises(AssertionError, match="launch 0 differs"):
        parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)


@pytest.mark.parametrize("flag,value", [
    ("--allowedTools", "Skill,WebFetch,WebSearch"),
    ("--allowedTools", "Read,Glob,Grep,Skill,WebFetch,WebSearch"),
    ("--disallowedTools", "Edit,Write,NotebookEdit"),
    ("--disallowedTools", "Bash,Edit,Write,NotebookEdit"),
])
def test_current_planner_permissions_cannot_revert_to_the_legacy_rules(tmp_path: Path, flag: str, value: str) -> None:
    records = pinned("claude")["proxy-key-background-append"]
    expected = [next(row for row in records if "--permission-mode" in row["argv"])]
    actual = copy.deepcopy(expected)
    actual[0]["argv"][actual[0]["argv"].index(flag) + 1] = value
    actual[0]["permissions"][flag] = value
    with pytest.raises(AssertionError, match="launch 0 differs"):
        parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)


def test_provider_mapping_is_explicit_and_cannot_rewrite_runtime_inputs(tmp_path: Path) -> None:
    expected = pinned("claude")["proxy-key-background-append"]
    actual = copy.deepcopy(expected)
    actual[0]["session"]["provider_id"] = "prov-canonical"
    with pytest.raises(AssertionError, match="provider_id"):
        parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)
    kwargs = {"expected_root": tmp_path, "actual_root": tmp_path,
              "provider_ids": {"prov-capture": "prov-canonical"}}
    parity.assert_parity(expected, actual, **kwargs)
    actual[0]["first_input"] += "prov-capture"
    with pytest.raises(AssertionError, match="first_input"):
        parity.assert_parity(expected, actual, **kwargs)
    assert expected == pinned("claude")["proxy-key-background-append"]


def test_catalog_prices_compare_by_value_after_sql_readback(tmp_path: Path) -> None:
    expected = pinned("claude")["oauth-default"]
    actual = copy.deepcopy(expected)
    for row in actual:
        for field in ("config", "price"):
            for entry in row[field]["catalog"]:
                for key, value in entry.items():
                    if key.endswith("_usd_per_mtok") and value is not None:
                        entry[key] = float(value)
    parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)
    # A price that differs in type or by one ulp is a change.
    for value in (True, 1.0000000000000002):
        changed = copy.deepcopy(expected)
        changed[0]["price"]["catalog"][0]["cache_write_usd_per_mtok"] = value
        with pytest.raises(AssertionError, match="cache_write_usd_per_mtok"):
            parity.assert_parity(expected, changed, expected_root=tmp_path, actual_root=tmp_path)


@pytest.mark.parametrize("path", [("config", "catalog", 0, "context_window"), ("totals", "input_tokens")])
def test_numeric_representation_outside_catalog_prices_still_fails(
    path: tuple[str | int, ...], tmp_path: Path,
) -> None:
    expected = pinned("claude")["oauth-default"]
    actual = copy.deepcopy(expected)
    parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)
    parent = actual[0]
    for key in path[:-1]:
        parent = parent[key]
    assert type(parent[path[-1]]) is int
    parent[path[-1]] = float(parent[path[-1]])
    with pytest.raises(AssertionError, match=str(path[-1])):
        parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)


def test_runtime_price_config_bytes_are_not_numeric_snapshots(tmp_path: Path) -> None:
    expected = pinned("omp")["responses-default"]
    actual = copy.deepcopy(expected)
    parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)
    # OMP's generated prices are runtime-visible JSON text. Even numerically
    # equal spellings must fail here; only the internal catalogs may normalize.
    text = actual[0]["config"]["models_yml"]
    assert '"cacheWrite": 0,' in text
    actual[0]["config"]["models_yml"] = text.replace('"cacheWrite": 0,', '"cacheWrite": 0.0,')
    with pytest.raises(AssertionError, match="cacheWrite"):
        parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)


def test_claude_isolation_ancestor_changes_still_fail(tmp_path: Path) -> None:
    expected = pinned("claude")["oauth-default"]
    actual = copy.deepcopy(expected)
    row = actual[0]
    settings = row["isolation"]
    settings["claudeMdExcludes"].append(str(tmp_path / "CLAUDE.md"))
    row["config"]["settings"] = json.dumps(settings, ensure_ascii=False, separators=(",", ":"))
    row["argv"][row["argv"].index("--settings") + 1] = row["config"]["settings"]
    with pytest.raises(AssertionError, match="claudeMdExcludes"):
        parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)


@pytest.mark.parametrize("setting,value", [(None, None), ("context_window", 128000)])
def test_open_catalog_materialization_only_adds_model_identity(
    tmp_path: Path, setting: str | None, value: Any,
) -> None:
    from looper.agents.profile_registry import MODEL_FIELDS

    expected = pinned("omp")["messages-open"]
    actual = copy.deepcopy(expected)
    identity = dict.fromkeys(MODEL_FIELDS)
    identity["name"] = actual[0]["config"]["model"]
    if setting:
        identity[setting] = value
    for field in ("config", "price"):
        actual[0][field]["catalog"] = [identity.copy()]
    if setting:
        with pytest.raises(AssertionError, match=setting):
            parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)
    else:
        parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)


@asynccontextmanager
async def target_database(monkeypatch: pytest.MonkeyPatch):
    import asyncpg
    from looper.db import connection

    name = f"looper_model_parity_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(DSN, timeout=10, command_timeout=60)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{name}"')
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4, command_timeout=60,
                                        server_settings={"search_path": name})
        # Fresh target schema only. Calling this on a legacy schema is blocked
        # until the upgrade card connects conversion to ensure_schema.
        await connection.ensure_schema(pool)
        monkeypatch.setattr(connection, "_pool", pool)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        try:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        finally:
            await admin.close()


@pytest.mark.skipif(not DSN, reason="requires a disposable LOOPER_TEST_DATABASE_URL")
@pytest.mark.parametrize("runtime,case", [(runtime, case) for runtime, cases in CASES.items() for case in cases])
def test_converted_registry_reproduces_pinned_tier_launches(
    runtime: str, case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record_property,
) -> None:
    async def run() -> None:
        async with target_database(monkeypatch) as pool:
            actual, conversion = await parity.capture_converted_tiers(pool, runtime, case, tmp_path)
            expected = pinned(runtime)[case]
            parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path,
                                 provider_ids=conversion.provider_map)
            # Evidence is emitted only after every captured field passed; a
            # mismatch raises and blocks the gate, with a field-level diff.
            record_property("launch_parity", json.dumps({"runtime": runtime, "case": case,
                            "revision": parity.CLAUDE_LEGACY_REVISION, "launches": len(actual),
                            "fields": sorted(parity.FIELDS), "mismatches": 0}))

    asyncio.run(asyncio.wait_for(run(), timeout=240))


@pytest.mark.skipif(not DSN, reason="requires a disposable LOOPER_TEST_DATABASE_URL")
@pytest.mark.parametrize("role", ["manager", "planner"])
def test_new_profile_identity_preserves_saved_session(
    role: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run() -> None:
        async with target_database(monkeypatch) as pool:
            # Change the profile identity between the two real resident turns,
            # while keeping its runtime/model/provider/options identical.
            actual, _ = await parity.capture_converted_tiers(
                pool, "codex", "key-proxy", tmp_path, rename_profile_after=0 if role == "manager" else 2)
            index = 0 if role == "manager" else 2
            before, after = actual[index:index + 2]
            assert before["session"]["profile_id"] == "profile-capture"
            assert after["session"]["profile_id"] == "profile-renamed"
            assert after["session"]["session_id"] == before["session"]["session_id"]
            assert "resume" in after["argv"]
            assert after["session"]["session_totals"]["input_tokens"] == 2 * before["totals"]["input_tokens"]

    asyncio.run(asyncio.wait_for(run(), timeout=240))


@pytest.mark.skipif(not DSN, reason="requires a disposable LOOPER_TEST_DATABASE_URL")
def test_declared_account_merge_preserves_tier_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run() -> None:
        async with target_database(monkeypatch) as pool:
            actual, conversion = await parity.capture_converted_tiers(
                pool, "claude", "proxy-key-background-append", tmp_path, merge_provider=True)
            assert conversion.provider_map["prov-capture"] == "prov-canonical"
            expected = pinned("claude")["proxy-key-background-append"]
            with pytest.raises(AssertionError, match="provider_id"):
                parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)
            parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path,
                                 provider_ids=conversion.provider_map)

    asyncio.run(asyncio.wait_for(run(), timeout=240))
