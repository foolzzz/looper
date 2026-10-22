"""Release gate: converted target rows must reproduce the pinned v2 launches.

Only the SQL exercises require a disposable LOOPER_TEST_DATABASE_URL. The
negative controls always run, including when PostgreSQL is unavailable.
The two historical upgrade suites and their main fixtures remain independent;
this suite deliberately does not exercise the unfinished v2 ensure_schema path.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

import model_launch_parity as parity
import agent_launch_parity as tiers


FIXTURES = Path(__file__).parent / "fixtures"
CASES = {"claude": parity.CLAUDE_CASES, "codex": parity.CODEX_CASES,
         "omp": parity.OMP_CASES, "grok": parity.GROK_CASES}
DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")


def pinned(runtime: str) -> dict[str, Any]:
    baseline = json.loads((FIXTURES / f"model_launch_parity_v2_{runtime}.json").read_text())
    assert baseline["source"]["revision"] == parity.CLAUDE_LEGACY_REVISION
    assert baseline["captures"].keys() == CASES[runtime].keys()
    # Like the two older parity gates, account explicitly for the declared
    # Manager model-tool and Planner wait contract changes without refreshing
    # the v2 fixtures. Both transformations pin literal historical deltas.
    def updated_prompt(text, role):
        transform = tiers.model_registry_contract if role == "manager" else tiers.planner_wait_contract
        return transform({role: {"system_prompt": text}})[role]["system_prompt"]

    for case, records in baseline["captures"].items():
        # Declared record-shape delta: a saved agent_sessions row now reads back selected_profile_id (the Manager
        # session's own /profile choice), null until chosen. Only saved-session rows (not the bare {"session_id"}
        # of a resumed turn) gain it.
        for record in records:
            if isinstance(record["session"], dict) and "session_totals" in record["session"]:
                assert "selected_profile_id" not in record["session"], case
                record["session"]["selected_profile_id"] = None
        manager_launches = 2 if runtime in {"codex", "grok"} else 1
        if runtime == "grok" and parity.GROK_CASES[case][0]:
            manager_launches = 4  # refused ACP plus fallback on both turns
        for index, record in enumerate(records[:2 * manager_launches]):
            role = "manager" if index < manager_launches else "planner"
            old_agent = record["config"].get("agent_md")
            # These adapters' pinned capture roots are part of their content
            # addresses. Recover that exact input before changing the contract.
            address_root = {"codex": "/tmp/looper-codex-launch-parity-v2",
                            "omp": "/tmp/looper-omp-launch-parity-v2"}.get(runtime, "<root>")

            def prompt_hash(text):
                return hashlib.sha256(text.replace("<root>", address_root).encode()).hexdigest()

            old_hash = prompt_hash(record["prompt"])
            record["prompt"] = updated_prompt(record["prompt"], role)
            new_hash = prompt_hash(record["prompt"])
            for field in {"codex": ("agents_md",), "grok": ("agent_md", "acp_agent_md")}.get(runtime, ()):
                text = record["config"][field]
                if text and (role == "manager" or "## Reply contract (required; code parses it)" in text):
                    record["config"][field] = updated_prompt(text, role)
            if runtime == "codex":
                record["config"]["toml"] = record["config"]["toml"].replace(
                    f"instructions/{old_hash}.md", f"instructions/{new_hash}.md")
            if runtime == "omp":
                flag = "--system-prompt" if "--system-prompt" in record["argv"] else "--append-system-prompt"
                at = record["argv"].index(flag) + 1
                assert Path(record["argv"][at]).name == f"{old_hash}.md"
                record["argv"][at] = str(Path(record["argv"][at]).with_name(f"{new_hash}.md"))
            if runtime == "grok" and "--agent" in record["argv"]:
                # The fallback's agent operand retains the content-addressed
                # run home, even though HOME is the stable resident session.
                # Pin its v2 address formula and verify the old address first.
                def agent_home_hash(agent):
                    named = f"grok-home/v1\n{record['config']['toml']}\0\0{record['login_target']}\0{agent}"
                    return hashlib.sha256(named.replace("<root>", "/tmp/looper-grok-launch-parity-v2").encode()).hexdigest()
                at = record["argv"].index("--agent") + 1
                path = Path(record["argv"][at])
                assert path.parent.name == agent_home_hash(old_agent)
                record["argv"][at] = str(path.parent.with_name(agent_home_hash(record["config"]["agent_md"])) / path.name)
    return baseline["captures"]


@pytest.mark.parametrize("runtime", CASES)
def test_declared_wait_delta_keeps_planner_prompt_mutations_visible(runtime: str, tmp_path: Path) -> None:
    expected = next(iter(pinned(runtime).values()))
    planner = next(index for index, row in enumerate(expected) if "You are the Planner" in row["prompt"])
    assert expected[planner]["prompt"].count(tiers._PLANNER_WAIT_RULES) == 1
    actual = copy.deepcopy(expected)
    parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)
    actual[planner]["prompt"] = actual[planner]["prompt"].replace('"ready_when": "head_changed"',
                                                               '"ready_when": "new_comments"')
    with pytest.raises(AssertionError, match=f"launch {planner} differs"):
        parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)


def test_declared_wait_delta_changes_only_the_pinned_insertion_and_refuses_reapplication():
    records = json.loads((FIXTURES / "model_launch_parity_v2_claude.json").read_text())["captures"]["oauth-default"]
    original = records[1]["prompt"]
    expected = {"planner": {"system_prompt": original}, "worker": {"system_prompt": "unchanged"}}
    transformed = tiers.planner_wait_contract(expected)
    assert transformed["planner"]["system_prompt"].replace(tiers._PLANNER_WAIT_RULES, "") == original
    assert transformed["worker"] == expected["worker"]
    assert expected["planner"]["system_prompt"] == original
    with pytest.raises(AssertionError):
        tiers.planner_wait_contract(transformed)


@pytest.mark.parametrize("runtime", CASES)
@pytest.mark.parametrize("change", ["url", "protocol", "background", "env", "unset",
                                    "prompt", "input", "config", "price", "cap", "session", "totals"])
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
        if runtime == "claude":
            assert row["env"]["ANTHROPIC_BASE_URL"]
            row["env"]["ANTHROPIC_BASE_URL"] = "https://changed.example.test/v1/"
        elif runtime in {"codex", "omp"}:
            key = "toml" if runtime == "codex" else "models_yml"
            assert "https://proxy.example.test/" in row["config"][key]
            row["config"][key] = row["config"][key].replace(
                "https://proxy.example.test/", "https://changed.example.test/")
        else:
            # Grok's own login omits the URL; injecting an override also
            # violates parity, even if every existing config byte is intact.
            row["env"]["XAI_BASE_URL"] = "https://changed.example.test/v1/"
    elif change == "protocol":
        row["config"]["api" if runtime == "omp" else "protocol"] = (
            "openai-responses" if runtime == "claude" else "anthropic-messages")
    elif change == "background":
        row["config"]["catalog"][0]["background_model"] = "changed-helper"
    elif change == "env":
        row["env"]["API_TIMEOUT_MS" if runtime == "claude" else "CAPTURE_SETTING"] = "changed"
    elif change == "unset":
        if row["unset"]:
            row["unset"].pop()
        else:
            row["unset"].append("CODEX_API_KEY")
    elif change == "prompt":
        row["prompt"] += "\nChanged instructions."
    elif change == "input":
        row["first_input"] += "changed"
    elif change == "config":
        key = {"claude": "settings", "codex": "toml", "omp": "models_yml", "grok": "toml"}[runtime]
        row["config"][key] += "\n"
    elif change == "price":
        row["price"]["catalog"][0]["input_usd_per_mtok"] = 99
    elif change == "cap":
        row["cap"] = 99
    elif change == "session":
        row["session"]["session_id"] = "lost-session"
    else:
        row["totals"]["input_tokens"] += 1
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
    actual[0]["prompt"] += "prov-capture"
    with pytest.raises(AssertionError, match="prompt"):
        parity.assert_parity(expected, actual, **kwargs)
    assert expected == pinned("claude")["proxy-key-background-append"]


@pytest.mark.parametrize("runtime", CASES)
def test_catalog_prices_compare_by_value_after_sql_readback(runtime: str, tmp_path: Path) -> None:
    expected = next(iter(pinned(runtime).values()))
    actual = copy.deepcopy(expected)
    for row in actual:
        for field in ("config", "price"):
            for entry in row[field]["catalog"]:
                for key, value in entry.items():
                    if key.endswith("_usd_per_mtok") and value is not None:
                        entry[key] = float(value)
    parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)


@pytest.mark.parametrize("field", ["config", "price"])
@pytest.mark.parametrize("value", [True, None, "1", 1.0000000000000002])
def test_catalog_price_type_and_value_changes_still_fail(field: str, value: Any, tmp_path: Path) -> None:
    expected = pinned("claude")["oauth-default"]
    actual = copy.deepcopy(expected)
    actual[0][field]["catalog"][0]["cache_write_usd_per_mtok"] = value
    with pytest.raises(AssertionError, match="cache_write_usd_per_mtok"):
        parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)


@pytest.mark.parametrize("path", [
    ("config", "catalog", 0, "context_window"),
    ("price", "catalog", 0, "context_window"),
    ("totals", "input_tokens"),
    ("session", "session_totals", "input_tokens"),
])
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


@pytest.mark.parametrize("flag", ["--system-prompt-file", "--append-system-prompt-file"])
def test_claude_prompt_address_tracks_normalized_root(tmp_path: Path, flag: str) -> None:
    expected = copy.deepcopy(pinned("claude")["oauth-default"])
    actual = copy.deepcopy(expected)
    before, after = tmp_path / "before", tmp_path / "after"
    for rows, root in ((expected, before), (actual, after)):
        for row in rows:
            old_flag = next(value for value in ("--system-prompt-file", "--append-system-prompt-file")
                            if value in row["argv"])
            at = row["argv"].index(old_flag)
            row["argv"][at] = flag
            row["prompt"] = row["prompt"].replace("<root>", str(root))
            digest = hashlib.sha256(row["prompt"].encode("utf-8")).hexdigest()
            row["argv"][at + 1] = str(root / "runtime/.local/prompts" / f"{digest}.md")
    # Equal prompt bytes after the allowed root substitution must also have
    # equal content addresses. No fixture is regenerated to obtain this.
    parity.assert_parity(expected, actual, expected_root=before, actual_root=after)
    actual[1]["prompt"] += "\nChanged instructions."
    at = actual[1]["argv"].index(flag) + 1
    digest = hashlib.sha256(actual[1]["prompt"].encode("utf-8")).hexdigest()
    actual[1]["argv"][at] = str(after / "runtime/.local/prompts" / f"{digest}.md")
    with pytest.raises(AssertionError, match="launch 1 differs"):
        parity.assert_parity(expected, actual, expected_root=before, actual_root=after)


def test_claude_prompt_address_must_match_captured_bytes(tmp_path: Path) -> None:
    row = copy.deepcopy(pinned("claude")["oauth-default"][1])
    at = row["argv"].index("--system-prompt-file") + 1
    row["prompt"] = row["prompt"].replace("<root>", str(tmp_path))
    row["argv"][at] = str(tmp_path / "runtime/.local/prompts" / f"{'0' * 64}.md")
    with pytest.raises(ValueError, match="prompt.*hash"):
        parity.normalize(row, root=tmp_path)


@pytest.mark.parametrize("change", ["directory", "flag", "other_argument"])
def test_claude_prompt_address_does_not_hide_other_argv_changes(tmp_path: Path, change: str) -> None:
    expected = pinned("claude")["oauth-default"]
    actual = copy.deepcopy(expected)
    at = actual[1]["argv"].index("--system-prompt-file")
    if change == "directory":
        actual[1]["argv"][at + 1] = actual[1]["argv"][at + 1].replace("/prompts/", "/elsewhere/")
    elif change == "flag":
        actual[1]["argv"][at] = "--append-system-prompt-file"
    else:
        actual[1]["argv"].append(f"<root>/runtime/.local/prompts/{'0' * 64}.md")
    with pytest.raises(AssertionError, match="launch 1 differs"):
        parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)


@pytest.mark.parametrize("setting,value", [(None, None), ("context_window", 128000),
                         ("efforts", []), ("background_model", "helper"), ("input_usd_per_mtok", 0)])
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
