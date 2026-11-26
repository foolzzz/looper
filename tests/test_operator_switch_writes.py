"""Operator switch writes reach their real readers without direct database edits."""

import json
from datetime import datetime, timezone

import pytest

from looper import loopercli
from looper.agents import api_v1
from tests.db_support import run_db, v1_api

# prefix, read action, write action, stored key, field, on, off, default
SWITCHES = (
    ("compaction_restore_settings", "get", "update", "compaction_restore.enabled", "enabled", True, False, False),
    ("attention", "settings_get", "settings_update", "attention.enabled", "enabled", True, False, False),
    ("reminders_settings", "get", "update", "reminders.enabled", "enabled", True, False, False),
    ("stable_context_settings", "get", "update", "manager.stable_context", "enabled", True, False, False),
    ("decision_snapshots_settings", "get", "update", "decision_snapshots.enabled", "enabled", True, False, False),
    ("leads_search_settings", "get", "update", "leads.search_mode", "mode", "fulltext", "words", "words"),
)


async def _behavior(prefix, value, call, pool, tmp_path):
    """Assert a consumer changes behavior, beyond the settings readback."""
    if prefix == "attention":
        status, answer = await call("attention.summary")
        assert status == (200 if value else 409), answer
        if value:
            assert answer["result"]["sections"]
    elif prefix == "reminders_settings":
        from looper.agents.ops import Turn
        from looper.agents.ops.reminders import TOOL_NAMES
        from looper.agents.ops.service import ManagerOps, Platform

        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="synthetic")
        names = {tool["name"] for tool in await ops.current_listing(
            Turn(channel="web", chat="synthetic", message_id="synthetic-message"))}
        assert set(TOOL_NAMES) & names == (set(TOOL_NAMES) if value else set())
    elif prefix == "compaction_restore_settings":
        from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        await manager._load_db_config()
        assert manager._compaction_restore is value
    elif prefix == "stable_context_settings":
        from looper.agents.agent_settings import resolve
        from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
        from looper.agents.tiers.state_view import STABLE_CONTEXT_CONTRACT

        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        await manager._load_db_config()
        assert (STABLE_CONTEXT_CONTRACT in manager._composed_prompt(resolve("manager", {})).text) is value
    elif prefix == "decision_snapshots_settings":
        from looper.agents.tiers.decisions import step_decision
        from looper.agents.tiers.planner import PlanStep
        from looper.agents.tiers.planner_protocol import Decision, DecisionContext, DecisionReply, ResultKind

        before = await pool.fetchval("SELECT count(*) FROM decision_snapshots")
        await step_decision("task-1", PlanStep("gate-1", 0, "Synthetic work", key="s1"),
                            DecisionContext("s1", ResultKind.WORK, True, False, 0),
                            DecisionReply(Decision.ACCEPT, "Verified", "", (), "Done"), None)
        assert await pool.fetchval("SELECT count(*) FROM decision_snapshots") == before + int(value)
    else:
        from looper.agents import leads

        found = await leads.search(words=["deployment", "absent"], db=pool)
        assert [row["summary"] for row in found["leads"]] == (["Deployment complete"] if value == "fulltext" else [])


@pytest.mark.parametrize("switch", SWITCHES, ids=lambda row: row[0])
def test_operator_sets_resets_and_changes_capability(switch, tmp_path, monkeypatch):
    prefix, read, write, key, field, on, off, default = switch

    async def check(pool):
        from tests import leads_support, ledger_support, subscription_support

        await ledger_support.seed(pool)
        if prefix == "leads_search_settings":
            await subscription_support.source(pool)
            event = await subscription_support.add(pool, "Synthetic deployment", sent_at=datetime.now(timezone.utc))
            await leads_support.lead(pool, [event], summary="Deployment complete")
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call(f"{prefix}.{read}")
            assert status == 200 and answer["result"] == {field: default}, answer
            await _behavior(prefix, default, call, pool, tmp_path)
            for value, expected in ((on, on), (off, off), (on, on), (None, default)):
                status, answer = await call(f"{prefix}.{write}", {field: value})
                assert status == 200 and answer["result"] == {field: expected}, answer
                status, answer = await call(f"{prefix}.{read}")
                assert status == 200 and answer["result"] == {field: expected}, answer
                stored = await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", key)
                assert stored == (None if value is None else str(value).lower())
                await _behavior(prefix, expected, call, pool, tmp_path)
            actions = await pool.fetch("SELECT arguments, result, status FROM manager_actions WHERE tool=$1 ORDER BY id",
                                       f"{prefix}.{write}")
            assert len(actions) == 4
            assert all(row["status"] == "ok" for row in actions)
            assert json.loads(actions[-1]["arguments"]) == {field: None}
            assert json.loads(actions[-1]["result"]) == {field: default}

    run_db(check, monkeypatch, timeout=35)


@pytest.mark.parametrize("switch", SWITCHES, ids=lambda row: row[0])
def test_non_operator_and_invalid_values_cannot_change_switch(switch, tmp_path, monkeypatch):
    prefix, read, write, key, field, on, off, default = switch

    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            for action, body in ((read, {}), (write, {field: on})):
                status, answer = await call(f"{prefix}.{action}", body, client=True)
                assert status == 403 and answer["error"]["code"] == "forbidden", answer
            for invalid in ("invalid", 1, [], {}):
                status, answer = await call(f"{prefix}.{write}", {field: invalid})
                assert status == 400 and answer["error"]["code"] == "invalid", answer
            for body in ({}, {field: on, "unknown": True}):
                status, answer = await call(f"{prefix}.{write}", body)
                assert status == 400 and answer["error"]["code"] == "invalid", answer
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key=$1", key) is None
            status, answer = await call(f"{prefix}.{read}")
            assert status == 200 and answer["result"] == {field: default}

    run_db(check, monkeypatch, timeout=25)


@pytest.mark.parametrize("switch", SWITCHES, ids=lambda row: row[0])
@pytest.mark.parametrize("choice", ("on", "off", "reset"))
def test_generated_cli_has_typed_switch_values(switch, choice):
    prefix, read, write, key, field, on, off, default = switch
    value = {"on": on, "off": off, "reset": None}[choice]
    document = api_v1.document()
    args = loopercli.build_parser(document).parse_args([
        prefix.replace("_", "-"), write.replace("_", "-"), f"--{field}", json.dumps(value)])
    operation = args._generated
    assert operation.operator_only
    assert operation.path == f"/api/v1/{prefix}/{write}"
    assert loopercli.request_body(args, operation) == {field: value}
